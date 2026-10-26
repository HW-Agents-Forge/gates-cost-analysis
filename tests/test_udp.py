"""Verilog user-defined primitives lowered to logic, and unterminated block comments."""
import itertools
import random
import unittest

from gatecost.frontend.verilog import parse_text
from gatecost.frontend.vhdl.lexer import lex as vhdl_lex
from tests.elab_helpers import codes, comb, elab, ops, top_module
from tests.ir_sim import Sim

MUX = """
primitive mux2(o, a, b, s);
  output o; input a, b, s;
  table
    // a b s : o
    0 ? 0 : 0;
    1 ? 0 : 1;
    ? 0 1 : 0;
    ? 1 1 : 1;
    0 0 x : 0;
    1 1 x : 1;
  endtable
endprimitive
"""

DFF = """
primitive dff(q, d, clk);
  output q; reg q; input d, clk;
  table
    0 r : ? : 0;
    1 r : ? : 1;
    ? f : ? : -;
    * ? : ? : -;
  endtable
endprimitive
"""

LATCH = """
primitive lat(q, d, g);
  output q; reg q; input d, g;
  table
    1 1 : ? : 1;
    0 1 : ? : 0;
    ? 0 : ? : -;
  endtable
endprimitive
"""


class CombUdp(unittest.TestCase):
    def test_mux_truth_table(self):
        m = top_module(elab(MUX, top="mux2"))
        for a, b, s in itertools.product((0, 1), repeat=3):
            self.assertEqual(comb(m, a=a, b=b, s=s)["o"], b if s else a, (a, b, s))
        # minimised: the consensus term a&b is not needed; the sum of products (a & ~s) | (b & s) is then the
        # builder's masked merge, one MUX (IR.md section 4)
        self.assertEqual(ops(m), {"MUX": 1})

    def test_instance_is_costed_not_a_black_box(self):
        r = elab(MUX + "module t(input a, b, s, output y); mux2 u(y, a, b, s); endmodule", top="t")
        self.assertNotIn("MISSING_MODULE", codes(r))
        names = {m.name for m in r.design.modules.values()}
        self.assertIn("mux2", names)
        self.assertNotIn("BLACKBOX", ops(top_module(r)))

    def test_ansi_header_unnamed_instance_and_delay(self):
        src = """
primitive nand2(output o, input a, b);
  table 0 ? : 1; ? 0 : 1; 1 1 : 0; endtable
endprimitive
module t(input a, b, output y, z);
  nand2 (y, a, b);
  nand2 #1 u(z, a, y);
endmodule"""
        r = elab(src, top="t")
        self.assertNotIn("MISSING_MODULE", codes(r))
        m = top_module(elab(src, top="nand2"))
        for a, b in itertools.product((0, 1), repeat=2):
            self.assertEqual(comb(m, a=a, b=b)["o"], 1 - (a & b))

    def test_udp_is_never_a_root(self):
        r = elab(MUX + "module t(input a, b, s, output y); mux2 u(y, a, b, s); endmodule")
        self.assertEqual(top_module(r).name, "t")
        r = elab(MUX + "module t(input a, output y); assign y = a; endmodule")
        self.assertEqual(top_module(r).name, "t")

    def test_random_tables_match(self):
        rnd = random.Random(7)
        for n in (2, 3, 4, 5):
            for _ in range(4):
                tt = [rnd.randint(0, 1) for _ in range(1 << n)]
                ports = [f"i{k}" for k in range(n)]
                rows = "\n".join(
                    " ".join(str((v >> k) & 1) for k in range(n)) + f" : {tt[v]};" for v in range(1 << n))
                src = (f"primitive p(o, {', '.join(ports)}); output o; input {', '.join(ports)};\n"
                       f"table\n{rows}\nendtable\nendprimitive")
                m = top_module(elab(src, top="p"))
                for v in range(1 << n):
                    got = comb(m, **{p: (v >> k) & 1 for k, p in enumerate(ports)})["o"]
                    self.assertEqual(got, tt[v], (n, tt, v))

    def test_constant_and_x_rows(self):
        src = "primitive c(o, a); output o; input a; table 0 : 1; 1 : 1; x : 0; endtable endprimitive"
        m = top_module(elab(src, top="c"))
        self.assertEqual(comb(m, a=0)["o"], 1)
        self.assertEqual(comb(m, a=1)["o"], 1)
        self.assertEqual(ops(m), {})


class SeqUdp(unittest.TestCase):
    def test_edge_sensitive_becomes_dff(self):
        r = elab(DFF, top="dff")
        m = top_module(r)
        self.assertEqual(ops(m), {"DFF": 1})
        self.assertNotIn("MISSING_MODULE", codes(r))
        s = Sim(m)
        s.step({"d": 1, "clk": 1})
        self.assertEqual(s.outputs(s.comb({"d": 0, "clk": 0}))["q"], 1)
        s.step({"d": 0, "clk": 1})
        self.assertEqual(s.outputs(s.comb({"d": 1, "clk": 0}))["q"], 0)

    def test_negedge_with_enable_and_hold(self):
        src = """
primitive edff(q, d, en, clk);
  output q; reg q; input d, en, clk;
  table
    1 1 (10) : ? : 1;
    0 1 (10) : ? : 0;
    ? 0 (10) : ? : -;
  endtable
endprimitive"""
        r = elab(src, top="edff")
        m = top_module(r)
        dffs = [n for n in m.nodes if n.op == "DFF"]
        self.assertEqual(len(dffs), 1)
        s = Sim(m)
        s.step({"d": 1, "en": 1, "clk": 0})
        s.step({"d": 0, "en": 0, "clk": 0})
        self.assertEqual(s.outputs(s.comb({"d": 0, "en": 0, "clk": 0}))["q"], 1)
        s.step({"d": 0, "en": 1, "clk": 0})
        self.assertEqual(s.outputs(s.comb({"d": 0, "en": 0, "clk": 0}))["q"], 0)

    def test_level_sensitive_becomes_latch(self):
        r = elab(LATCH, top="lat")
        m = top_module(r)
        self.assertEqual(ops(m), {"LATCH": 1})
        s = Sim(m)
        self.assertEqual(s.outputs(s.comb({"d": 1, "g": 1}))["q"], 1)
        self.assertEqual(s.outputs(s.comb({"d": 0, "g": 0}))["q"], 1)     # holds
        self.assertEqual(s.outputs(s.comb({"d": 0, "g": 1}))["q"], 0)

    def test_instances_of_sequential_udps(self):
        src = DFF + LATCH + """
module t(input d, c, output q, l);
  dff f(q, d, c);
  lat (l, d, c);
endmodule"""
        r = elab(src, top="t")
        self.assertNotIn("MISSING_MODULE", codes(r))
        self.assertEqual(ops(top_module(r)), {"INST": 2})


class UnsupportedUdp(unittest.TestCase):
    def test_async_set_reset_stays_black_box_with_warning(self):
        src = """
primitive dffr(q, d, clk, r);
  output q; reg q; input d, clk, r;
  table
    ? ? 1 : ? : 0;
    0 r 0 : ? : 0;
    1 r 0 : ? : 1;
  endtable
endprimitive
module t(input d, c, r, output q); dffr u(q, d, c, r); endmodule"""
        res = elab(src, top="t")
        self.assertIn("MISSING_MODULE", codes(res))
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(res))
        msgs = " ".join(d.message for d in res.diagnostics)
        self.assertIn("asynchronous", msgs)
        self.assertIn("dffr", msgs)

    def test_toggle_is_unsupported(self):
        src = """
primitive tff(q, t, clk);
  output q; reg q; input t, clk;
  table
    1 r : 0 : 1;
    1 r : 1 : 0;
  endtable
endprimitive
module top(input t, c, output q); tff u(q, t, c); endmodule"""
        res = elab(src, top="top")
        self.assertIn("MISSING_MODULE", codes(res))


class UnterminatedComment(unittest.TestCase):
    def test_verilog_preproc_warns_at_start_line(self):
        d = parse_text("module m;\n  wire a;\n  /* never closed\n  wire b;\nendmodule\n", "x.v")
        hits = [x for x in d.diagnostics if "unterminated block comment" in x.message]
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0].code, hits[0].line, hits[0].severity), ("PREPROC_ERROR", 3, "warning"))

    def test_verilog_terminated_comment_is_silent(self):
        d = parse_text("module m; /* ok */ wire a; /**/ endmodule\n", "x.v")
        self.assertFalse([x for x in d.diagnostics if "unterminated" in x.message])
        d = parse_text("module m; wire a; /*/ endmodule\n", "x.v")
        self.assertTrue([x for x in d.diagnostics if "unterminated block comment" in x.message])

    def test_vhdl_lexer_reports_unterminated(self):
        _, _, errors = vhdl_lex("a\nb /* never closed\nc")
        self.assertEqual(errors, [(2, 3, "unterminated block comment")])
        _, _, errors = vhdl_lex("a /* closed */ b")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
