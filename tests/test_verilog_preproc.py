"""Preprocessor: macros, conditionals, includes, translate_off, line map."""
import os
import tempfile
import unittest

from gatecost.frontend.common import DiagSink, source_from_text
from gatecost.frontend.verilog import parse_files, parse_text
from gatecost.frontend.verilog.limits import Limits
from gatecost.frontend.verilog.preproc import Preprocessor


def pp(text, path="t.v", **kw):
    sink = DiagSink()
    p = Preprocessor(sink=sink, **kw)
    r = p.process(source_from_text(text, path))
    return r, sink.items


def lines(r):
    return r.text.split("\n")


def codes(diags):
    return [(d.code, d.line) for d in diags]


class Macros(unittest.TestCase):
    def test_object_macro(self):
        r, d = pp("`define W 8\nwire [`W-1:0] x;\n")
        self.assertEqual(lines(r)[1], "wire [8-1:0] x;")
        self.assertEqual(d, [])

    def test_function_macro_and_default(self):
        r, d = pp("`define ADD(a, b=2) ((a)+(b))\nx = `ADD(1);\ny = `ADD(1, 3);\nz = `ADD(1, );\n")
        self.assertEqual(lines(r)[1:4], ["x = ((1)+(2));", "y = ((1)+(3));", "z = ((1)+(2));"])

    def test_args_with_commas_in_parens_and_braces(self):
        r, d = pp("`define F(a, b) a|b\nx = `F(f(1,2), {3,4});\n")
        self.assertEqual(lines(r)[1], "x = f(1,2)|{3,4};")
        self.assertEqual(d, [])

    def test_nested_macros(self):
        r, _ = pp("`define A 1\n`define B `A + `A\n`define C(x) x + `B\ny = `C(`A);\n")
        self.assertEqual(lines(r)[3], "y = 1 + 1 + 1;")

    def test_token_pasting_and_stringify(self):
        r, _ = pp("`define CAT(a,b) a``_``b\n`define S(x) `\"x`\"\nwire `CAT(p, q);\ns = `S(hi);\n")
        self.assertEqual(lines(r)[2:4], ["wire p_q;", 's = "hi";'])

    def test_multiline_define_keeps_line_count(self):
        r, _ = pp("`define L a + \\\n b + \\\n c\nx = `L;\ny;\n")
        self.assertEqual(lines(r)[3], "x = a +   b +   c;")
        self.assertEqual(r.linemap.resolve(5), ("t.v", 5))
        self.assertEqual(lines(r)[4], "y;")

    def test_define_comment_stripped(self):
        r, _ = pp('`define X 5 // five\n`define Y "a // b"\nx = `X; y = `Y;\n')
        self.assertEqual(lines(r)[2], 'x = 5; y = "a // b";')

    def test_undef(self):
        r, d = pp("`define A 1\n`undef A\nx = `A;\n")
        self.assertEqual(codes(d), [("UNDEFINED_MACRO", 3)])
        self.assertEqual(lines(r)[2], "x = ;")

    def test_undefined_macro_location(self):
        r, d = pp("a;\n  b `NOPE c;\n")
        self.assertEqual((d[0].code, d[0].line, d[0].column, d[0].severity), ("UNDEFINED_MACRO", 2, 5, "warning"))
        self.assertTrue(d[0].affects_cost)

    def test_recursion_guard(self):
        r, d = pp("`define A `A + 1\nx = `A;\n")
        self.assertEqual([c for c, _ in codes(d)], ["MACRO_ERROR"])

    def test_depth_limit(self):
        defs = "".join(f"`define M{i} `M{i + 1}\n" for i in range(10)) + "`define M10 7\nx = `M0;\n"
        r, d = pp(defs, limits=Limits(macro_depth=5))
        self.assertIn("LIMIT_REACHED", [c for c, _ in codes(d)])
        r2, d2 = pp(defs)
        self.assertEqual(lines(r2)[11], "x = 7;")
        self.assertEqual(d2, [])

    def test_wrong_arg_count(self):
        r, d = pp("`define F(a,b) a+b\nx = `F(1);\ny = `F(1,2,3);\n")
        self.assertEqual([c for c, _ in codes(d)], ["MACRO_ERROR", "MACRO_ERROR"])

    def test_function_macro_without_parens(self):
        r, d = pp("`define F(a) a\nx = `F;\n")
        self.assertEqual([c for c, _ in codes(d)], ["MACRO_ERROR"])

    def test_macro_in_string_not_expanded(self):
        r, d = pp('`define A 1\nx = "`A";\n')
        self.assertEqual(lines(r)[1], 'x = "`A";')
        self.assertEqual(d, [])

    def test_macro_in_comment_not_expanded(self):
        r, d = pp("`define A 1\n// `NOPE\n/* `NOPE */ x;\n")
        self.assertEqual(d, [])

    def test_file_line_builtins(self):
        r, _ = pp("\n\nx = `__LINE__; y = `__FILE__;\n", path="dir/f.v")
        self.assertEqual(lines(r)[2], 'x = 3; y = "dir/f.v";')

    def test_command_line_defines(self):
        r, _ = pp("`ifdef FOO\na\n`else\nb\n`endif\nx = `V;\n", defines={"FOO": None, "V": "5"})
        self.assertEqual(lines(r)[1], "a")
        self.assertEqual(lines(r)[5], "x = 5;")

    def test_args_spanning_lines_keep_alignment(self):
        r, _ = pp("`define F(a,b) a+b\nx = `F(1,\n  2);\ny;\n")
        self.assertEqual(lines(r)[1], "x = 1+2")
        self.assertEqual(lines(r)[2], ";")
        self.assertEqual(lines(r)[3], "y;")
        self.assertEqual(r.linemap.resolve(2), ("t.v", 2))
        self.assertEqual(r.linemap.resolve(3), ("t.v", 3))
        self.assertEqual(r.linemap.resolve(4), ("t.v", 4))


class Conditionals(unittest.TestCase):
    def test_synthesis_defined_by_default(self):
        r, _ = pp("`ifdef SYNTHESIS\nyes\n`endif\n`ifdef SIMULATION\nsim\n`endif\n`ifdef FORMAL\nformal\n`endif\n")
        t = r.text.split()
        self.assertEqual(t, ["yes"])

    def test_nested_and_elsif_else(self):
        src = """`define B
`ifdef A
a
`elsif B
 `ifndef C
b_c
 `else
b_noc
 `endif
`else
other
`endif
"""
        r, d = pp(src)
        self.assertEqual(r.text.split(), ["b_c"])
        self.assertEqual(d, [])
        r, _ = pp("`define A\n" + src.split("\n", 1)[1])
        self.assertEqual(r.text.split(), ["a"])
        r, _ = pp(src.replace("`define B\n", ""))
        self.assertEqual(r.text.split(), ["other"])

    def test_inactive_region_directives_ignored(self):
        r, d = pp("`ifdef NO\n`define X 1\n`include \"missing.vh\"\n`NOPE\n`endif\nx = `X;\n")
        self.assertEqual(codes(d), [("UNDEFINED_MACRO", 6)])

    def test_unbalanced(self):
        _, d = pp("`endif\n`else\n`ifdef A\n")
        self.assertEqual([c for c, _ in codes(d)], ["PREPROC_ERROR"] * 3)

    def test_line_counts_preserved(self):
        r, _ = pp("`ifdef NO\n1\n2\n`else\nthree\n`endif\nfour\n")
        self.assertEqual(r.text.count("\n"), 7)
        self.assertEqual(lines(r)[4], "three")
        self.assertEqual(lines(r)[6], "four")


class Pragmas(unittest.TestCase):
    def test_translate_off_variants(self):
        for off, on in (("synopsys translate_off", "synopsys translate_on"),
                        ("pragma translate_off", "pragma translate_on"),
                        ("synthesis translate_off", "synthesis translate_on")):
            r, d = pp(f"a\n// {off}\nhidden\n// {on}\nb\n")
            self.assertEqual(r.text.split(), ["a", "b"], off)
            self.assertEqual([(x.code, x.severity, x.line) for x in d], [("IGNORED_REGION", "info", 2)])

    def test_block_comment_pragma(self):
        r, d = pp("a\n/* synopsys translate_off */ hidden /* synopsys translate_on */ b\n")
        self.assertEqual(r.text.split(), ["a", "b"])

    def test_unterminated_region(self):
        r, d = pp("a\n// synopsys translate_off\nhidden\n")
        self.assertEqual(r.text.split(), ["a"])
        self.assertEqual(d[0].code, "IGNORED_REGION")

    def test_full_case_pragma_becomes_attribute(self):
        r, _ = pp("case (x) // synopsys full_case parallel_case\n")
        self.assertIn("(* full_case, parallel_case *)", r.text)

    def test_default_nettype_recorded(self):
        r, _ = pp("`default_nettype none\nmodule m; endmodule\n`default_nettype wire\n")
        self.assertEqual(r.nettype, [(1, "none"), (3, "wire")])

    def test_ignored_directives(self):
        r, d = pp("`timescale 1ns/1ps\n`celldefine\n`pragma protect begin\n`line 1 \"x\" 0\nwire w;\n`resetall\n")
        self.assertEqual(r.text.split(), ["wire", "w;"])
        self.assertEqual(d, [])


class Includes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def w(self, name, text):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def test_include_and_line_map(self):
        inc = self.w("defs.vh", "`define W 8\nwire inc_a;\nwire inc_b;\n")
        top = self.w("top.v", "module m;\n`include \"defs.vh\"\nwire [`W:0] x;\nendmodule\n")
        d = parse_files([top])
        self.assertEqual(d.diagnostics, ())
        m = d.modules[0]
        names = {i.name: i.loc for i in m.items}
        self.assertEqual(names["inc_a"][0], inc)
        self.assertEqual(names["inc_a"][1], 2)
        self.assertEqual(names["inc_b"][1], 3)
        self.assertEqual(names["x"][0], top)
        self.assertEqual(names["x"][1], 3)
        self.assertEqual(d.file_hashes.keys(), {top, inc})

    def test_include_dirs_and_order(self):
        self.w("inc/a.vh", "wire from_inc;\n")
        self.w("a.vh", "wire from_local;\n")
        top = self.w("top.v", "module m;\n`include \"a.vh\"\nendmodule\n")
        d = parse_files([top], incdirs=[os.path.join(self.dir, "inc")])
        self.assertEqual([i.name for i in d.modules[0].items], ["from_local"])
        os.remove(os.path.join(self.dir, "a.vh"))
        d = parse_files([top], incdirs=[os.path.join(self.dir, "inc")])
        self.assertEqual([i.name for i in d.modules[0].items], ["from_inc"])

    def test_include_not_found(self):
        top = self.w("top.v", "module m;\n`include \"nope.vh\"\nwire w;\nendmodule\n")
        d = parse_files([top])
        self.assertEqual([(x.code, x.line) for x in d.diagnostics], [("INCLUDE_NOT_FOUND", 2)])
        self.assertEqual([i.name for i in d.modules[0].items], ["w"])

    def test_include_cycle(self):
        self.w("a.vh", "`include \"b.vh\"\nwire a;\n")
        self.w("b.vh", "`include \"a.vh\"\nwire b;\n")
        top = self.w("top.v", "module m;\n`include \"a.vh\"\nendmodule\n")
        d = parse_files([top])
        self.assertEqual([x.code for x in d.diagnostics], ["INCLUDE_CYCLE"])
        self.assertEqual(sorted(i.name for i in d.modules[0].items), ["a", "b"])

    def test_self_include(self):
        self.w("a.vh", "`include \"a.vh\"\nwire a;\n")
        top = self.w("top.v", "module m;\n`include \"a.vh\"\nendmodule\n")
        d = parse_files([top])
        self.assertEqual([x.code for x in d.diagnostics], ["INCLUDE_CYCLE"])

    def test_include_depth_limit(self):
        for i in range(6):
            self.w(f"i{i}.vh", f"`include \"i{i + 1}.vh\"\n" if i < 5 else "wire deep;\n")
        top = self.w("top.v", "module m;\n`include \"i0.vh\"\nendmodule\n")
        d = parse_files([top], limits=Limits(include_depth=3))
        self.assertIn("LIMIT_REACHED", [x.code for x in d.diagnostics])
        d = parse_files([top])
        self.assertEqual(d.diagnostics, ())

    def test_macros_persist_across_files(self):
        a = self.w("a.v", "`define T 4\n")
        b = self.w("b.v", "module m; wire [`T:0] w; endmodule\n")
        d = parse_files([a, b])
        self.assertEqual(d.diagnostics, ())
        d = parse_files([b, a])
        self.assertEqual([x.code for x in d.diagnostics][:1], ["UNDEFINED_MACRO"])


class LineMap(unittest.TestCase):
    def test_macro_use_site_attribution(self):
        src = "`define MUL(a,b) a * b\nmodule m;\n  wire [7:0] y = `MUL(p,\n      q);\n  wire z;\nendmodule\n"
        d = parse_text(src, "f.v")
        self.assertEqual(d.diagnostics, ())
        items = {i.name: i for i in d.modules[0].items}
        self.assertEqual(items["y"].loc[:2], ("f.v", 3))
        self.assertEqual(items["y"].init.loc[:2], ("f.v", 3))      # expansion attributed to the use site
        self.assertEqual(items["z"].loc[:2], ("f.v", 5))           # lines after a multi-line use stay aligned
        self.assertEqual(d.macro_uses[("f.v", 3)], [("MUL", "f.v", 1)])

    def test_macro_defined_in_include_secondary_location(self):
        with tempfile.TemporaryDirectory() as t:
            with open(os.path.join(t, "m.vh"), "w") as f:
                f.write("\n\n`define ONE 1\n")
            with open(os.path.join(t, "top.v"), "w") as f:
                f.write("`include \"m.vh\"\nmodule m;\n wire [`ONE:0] w;\nendmodule\n")
            d = parse_files([os.path.join(t, "top.v")])
        w = d.modules[0].items[0]
        self.assertTrue(w.loc[0].endswith("top.v"))
        self.assertEqual(w.loc[1], 3)
        (name, dfile, dline), = d.macro_uses[(w.loc[0], 3)]
        self.assertTrue(dfile.endswith("m.vh"))
        self.assertEqual((name, dline), ("ONE", 3))

    def test_lines_after_excluded_regions(self):
        src = "module m;\n`ifdef NO\nwire a;\nwire b;\n`else\nwire c;\n`endif\n// synopsys translate_off\nwire d;\n// synopsys translate_on\nwire e;\nendmodule\n"
        d = parse_text(src, "f.v")
        got = {i.name: i.loc[1] for i in d.modules[0].items}
        self.assertEqual(got, {"c": 6, "e": 11})
        self.assertEqual(d.modules[0].end_line, 12)

    def test_columns(self):
        d = parse_text("module m;\n   wire  w;\nendmodule\n", "f.v")
        self.assertEqual(d.modules[0].items[0].loc, ("f.v", 2, 10))

    def test_parse_error_line_after_include(self):
        with tempfile.TemporaryDirectory() as t:
            with open(os.path.join(t, "i.vh"), "w") as f:
                f.write("wire ok1;\nwire = ;\n")
            top = os.path.join(t, "top.v")
            with open(top, "w") as f:
                f.write("module m;\n`include \"i.vh\"\nendmodule\n")
            d = parse_files([top])
        (e,) = [x for x in d.diagnostics if x.code == "PARSE_ERROR"]
        self.assertTrue(e.file.endswith("i.vh"))
        self.assertEqual(e.line, 2)


class Reader(unittest.TestCase):
    def test_latin1_fallback_and_crlf(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "a.v")
            with open(p, "wb") as f:
                f.write(b"// caf\xe9\r\nmodule m;\r\nendmodule\r\n")
            d = parse_files([p])
        self.assertEqual(len(d.modules), 1)
        self.assertEqual([x.code for x in d.diagnostics], ["ENCODING_FALLBACK"])
        self.assertEqual(d.modules[0].end_line, 3)

    def test_bom_and_hash(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "a.v")
            with open(p, "wb") as f:
                f.write(b"\xef\xbb\xbfmodule m; endmodule\n")
            d1 = parse_files([p])
            with open(p, "wb") as f:
                f.write(b"module m;  endmodule\n")
            d2 = parse_files([p])
        self.assertEqual(d1.diagnostics, ())
        self.assertNotEqual(d1.file_hashes[p], d2.file_hashes[p])
        self.assertEqual(len(d1.file_hashes[p]), 64)

    def test_missing_file(self):
        d = parse_files(["/nonexistent/x.v"])
        self.assertEqual([x.code for x in d.diagnostics], ["SOURCE_NOT_FOUND"])


if __name__ == "__main__":
    unittest.main()
