"""Masked merge (builder rule, IR.md section 4): ``(x & ~m) | (y & m)`` and ``x ^ ((x ^ y) & m)`` become
``m ? y : x`` MUX nodes, one per run of bits selected by the same signal bit.  Exactness (exhaustive and
sequential simulation, against the un-rewritten IR), grouping of the select bits, patterns left alone,
clock-enable recognition on a byte-enable register bank.  Also the linear guard of the named keys on a node
with many inputs (``keys.role_of``)."""
from __future__ import annotations

import itertools
import random
import unittest
from collections import Counter
from unittest import mock

from gatecost.ir import builder as builder_mod
from gatecost.ir import keys
from gatecost.ir.sigspec import Slice
from tests.elab_helpers import elab, top_module
from tests.ir_dsl import E, Mod, cat, nodes_of
from tests.ir_sim import Sim

W = 4
OR_FORMS = {
    "x&~m|y&m": lambda x, y, m: (x & ~m) | (y & m),
    "~m&x|m&y": lambda x, y, m: (~m & x) | (m & y),
    "y&m|x&~m": lambda x, y, m: (y & m) | (x & ~m),
    "m&y|~m&x": lambda x, y, m: (m & y) | (~m & x),
    "x&m|y&~m (inverted mask)": lambda x, y, m: (y & ~m) | (x & m),
}
XOR_FORMS = {
    "x^((x^y)&m)": lambda x, y, m: x ^ ((x ^ y) & m),
    "((y^x)&m)^x": lambda x, y, m: ((y ^ x) & m) ^ x,
    "x^(m&(x^y))": lambda x, y, m: x ^ (m & (x ^ y)),
}


def _ref(form: str, x: int, y: int, m: int) -> int:
    if "inverted" in form:
        return ((x & m) | (y & ~m)) & 15
    return ((y & m) | (x & ~m)) & 15


class ExhaustiveForms(unittest.TestCase):
    def build(self, fn, free=True):
        md = Mod("t")
        x, y, m = md.inp("x", W), md.inp("y", W), md.inp("m", W)
        md.out("o", fn(x, y, m))
        return md.done(analyze=False)

    def check(self, forms):
        for name, fn in forms.items():
            with self.subTest(form=name):
                ir = self.build(fn)
                ops = Counter(n.op for n in ir.nodes)
                self.assertEqual(set(ops), {"MUX"}, ops)          # AND / NOT / OR / XOR swept
                self.assertEqual(ops["MUX"], W)                   # free mask: one 1-bit MUX per bit
                s = Sim(ir)
                for x, y, m in itertools.product(range(16), repeat=3):
                    got = s.outputs(s.comb({"x": x, "y": y, "m": m}))["o"]
                    self.assertEqual(got, _ref(name, x, y, m), (x, y, m))

    def test_or_forms_are_exact_muxes(self):
        self.check(OR_FORMS)

    def test_xor_forms_are_exact_muxes(self):
        self.check(XOR_FORMS)

    def test_not_a_mask_is_left_alone(self):
        cases = {
            "different masks": lambda x, y, m: (x & ~m) | (y & (m ^ x)),
            "no complement": lambda x, y, m: (x & m) | (y & m),
            "or of ors": lambda x, y, m: (x | ~m) | (y | m),
            "xor not through x": lambda x, y, m: y ^ ((x ^ m) & y),
        }
        for name, fn in cases.items():
            with self.subTest(case=name):
                ops = Counter(n.op for n in self.build(fn).nodes)
                self.assertNotIn("MUX", ops)

    def test_named_result_spanning_several_selects_is_left_alone(self):
        md = Mod("t")
        x, y, m = md.inp("x", W), md.inp("y", W), md.inp("m", W)
        nm = md.b.not_(m.s)
        a1, a2 = md.b.and_(x.s, nm), md.b.and_(y.s, m.s)
        o = md.b.op("OR", a1, a2, width=W, name="merged")
        md.b.connect(md.b.port("o", "out", W), o)
        ops = Counter(n.op for n in md.done(analyze=False).nodes)
        self.assertEqual(ops["OR"], 1)
        self.assertNotIn("MUX", ops)


class SelectGrouping(unittest.TestCase):
    def test_replicated_byte_mask_gives_one_mux_per_byte(self):
        md = Mod("t")
        x, y, be = md.inp("x", 32), md.inp("y", 32), md.inp("be", 4)
        m = cat(*[be[k:k + 1] for k in (3, 3, 3, 3, 3, 3, 3, 3)], *[be[2:3]] * 8, *[be[1:2]] * 8, *[be[0:1]] * 8)
        md.out("o", (x & ~m) | (y & m))
        ir = md.done(analyze=False)
        muxes = nodes_of(ir, "MUX")
        self.assertEqual(len(muxes), 4)
        self.assertEqual(sorted(n.outputs[0].width for n in muxes), [8, 8, 8, 8])
        sels = sorted(dict(n.inputs)["S"].chunks[0].lo for n in muxes)
        self.assertEqual(sels, [0, 1, 2, 3])
        self.assertTrue(all(dict(n.inputs)["S"].chunks[0].net.name == "be" for n in muxes))

    def test_mask_wire_is_followed_through_its_wiring(self):
        md = Mod("t")
        x, y, be = md.inp("x", 16), md.inp("y", 16), md.inp("be", 2)
        m = md.wire("m", 16)
        md.b.connect(m.s, cat(*[be[1:2]] * 8, *[be[0:1]] * 8).s)
        md.out("o", (x & ~m) | (y & m))
        ir = md.done(analyze=False)
        muxes = nodes_of(ir, "MUX")
        self.assertEqual(len(muxes), 2)
        s = Sim(ir)
        rnd = random.Random(3)
        for _ in range(200):
            xv, yv, bv = rnd.getrandbits(16), rnd.getrandbits(16), rnd.getrandbits(2)
            mv = (0xFF if bv & 1 else 0) | (0xFF00 if bv & 2 else 0)
            got = s.outputs(s.comb({"x": xv, "y": yv, "be": bv}))["o"]
            self.assertEqual(got, (xv & ~mv & 0xFFFF) | (yv & mv))

    def test_constant_mask_bits_select_directly(self):
        md = Mod("t")
        x, y, s = md.inp("x", 4), md.inp("y", 4), md.inp("s", 1)
        m = cat(s, s, md.c(1, 1), md.c(0, 1))       # bits 3..0 = s s 1 0
        md.out("o", (x & ~m) | (y & m))
        ir = md.done(analyze=False)
        self.assertEqual([n.outputs[0].width for n in nodes_of(ir, "MUX")], [2])
        sim = Sim(ir)
        for xv, yv, sv in itertools.product(range(16), range(16), range(2)):
            mv = 0b0010 | (0b1100 if sv else 0)
            got = sim.outputs(sim.comb({"x": xv, "y": yv, "s": sv}))["o"]
            self.assertEqual(got, (xv & ~mv & 15) | (yv & mv))


BE_SRC = {
    "be_en": """
module t (input wire clk, input wire rstn, input wire we, input wire [3:0] be, input wire [31:0] d, output reg [31:0] q);
  wire [31:0] m = {{8{be[3]}}, {8{be[2]}}, {8{be[1]}}, {8{be[0]}}};
  always @(posedge clk or negedge rstn)
    if (!rstn) q <= 32'd0;
    else if (we) q <= (q & ~m) | (d & m);
endmodule""",
    "be_en_commuted": """
module t (input wire clk, input wire rstn, input wire we, input wire [3:0] be, input wire [31:0] d, output reg [31:0] q);
  wire [31:0] m = {{8{be[3]}}, {8{be[2]}}, {8{be[1]}}, {8{be[0]}}};
  always @(posedge clk or negedge rstn)
    if (!rstn) q <= 32'd0;
    else if (we) q <= (m & d) | (~m & q);
endmodule""",
    "be_noen": """
module t (input wire clk, input wire rstn, input wire we, input wire [3:0] be, input wire [31:0] d, output reg [31:0] q);
  wire [31:0] m = {{8{be[3]}}, {8{be[2]}}, {8{be[1]}}, {8{be[0]}}};
  always @(posedge clk or negedge rstn)
    if (!rstn) q <= 32'd0;
    else q <= (q & ~m) | (d & m);
endmodule""",
    "be_xor": """
module t (input wire clk, input wire rstn, input wire we, input wire [3:0] be, input wire [31:0] d, output reg [31:0] q);
  wire [31:0] m = {{8{be[3]}}, {8{be[2]}}, {8{be[1]}}, {8{be[0]}}};
  always @(posedge clk or negedge rstn)
    if (!rstn) q <= 32'd0;
    else if (we) q <= q ^ ((q ^ d) & m);
endmodule""",
    "be_late_wire": """
module t (input wire clk, input wire rstn, input wire we, input wire [3:0] be, input wire [31:0] d, output reg [31:0] q);
  wire [31:0] m;
  always @(posedge clk or negedge rstn)
    if (!rstn) q <= 32'd0;
    else if (we) q <= (q & ~m) | (d & m);
  assign m = {{8{be[3]}}, {8{be[2]}}, {8{be[1]}}, {8{be[0]}}};
endmodule""",
    "free_mask": """
module t (input wire clk, input wire rstn, input wire we, input wire [3:0] be, input wire [31:0] d, output reg [31:0] q);
  wire [31:0] m = {be, be, ~be, d[31:28], be ^ d[3:0], d[11:0]};
  always @(posedge clk or negedge rstn)
    if (!rstn) q <= 32'd0;
    else if (we) q <= (q & ~m) | (d & m);
endmodule""",
}
BE_TERNARY = """
module t (input wire clk, input wire rstn, input wire we, input wire [3:0] be, input wire [31:0] d, output reg [31:0] q);
  integer k;
  always @(posedge clk or negedge rstn)
    if (!rstn) q <= 32'd0;
    else if (we) for (k = 0; k < 4; k = k + 1) if (be[k]) q[8*k +: 8] <= d[8*k +: 8];
endmodule"""


def _ref_q(name: str, q: int, we: int, be: int, d: int) -> int:
    if name == "free_mask":
        m = (be << 28) | (be << 24) | ((~be & 15) << 20) | (((d >> 28) & 15) << 16) | (((be ^ d) & 15) << 12) | (d & 0xFFF)
    else:
        m = sum(0xFF << (8 * k) for k in range(4) if (be >> k) & 1)
    if name != "be_noen" and not we:
        return q
    return (q & ~m & 0xFFFFFFFF) | (d & m)


def _shape(m) -> list:
    return sorted((n.op, n.outputs[0].width if n.outputs else 0, tuple(sorted(dict(n.inputs))))
                  for n in m.nodes)


class ByteEnableRegister(unittest.TestCase):
    def run_seq(self, m, name, seed=1):
        s = Sim(m)
        rnd = random.Random(seed)
        q = 0
        for _ in range(150):
            inp = {"rstn": 1 if rnd.random() > 0.05 else 0, "we": rnd.randrange(2), "be": rnd.getrandbits(4),
                   "d": rnd.getrandbits(32)}
            out = s.step(inp)
            self.assertEqual(out["q"], q)
            q = 0 if not inp["rstn"] else _ref_q(name, q, inp["we"], inp["be"], inp["d"])

    def test_sequential_equivalence_with_and_without_rewrite(self):
        for name, src in BE_SRC.items():
            with self.subTest(design=name):
                self.run_seq(top_module(elab(src, top="t")), name)
                with mock.patch.object(builder_mod, "_masked_merge_or", lambda *a: None), \
                        mock.patch.object(builder_mod, "_masked_merge_xor", lambda *a: None):
                    self.run_seq(top_module(elab(src, top="t")), name)

    def test_byte_enable_register_is_four_enabled_bytes(self):
        ref = _shape(top_module(elab(BE_TERNARY, top="t")))
        for name in ("be_en", "be_en_commuted", "be_xor"):
            with self.subTest(design=name):
                m = top_module(elab(BE_SRC[name], top="t"))
                dffs = nodes_of(m, "DFF")
                self.assertEqual(sorted(n.outputs[0].width for n in dffs), [8, 8, 8, 8])
                self.assertTrue(all("EN" in dict(n.inputs) for n in dffs))
                self.assertEqual(Counter(n.op for n in m.nodes), Counter({"DFF": 4, "AND": 4}))
                self.assertEqual(_shape(m), ref)

    def test_without_enable_the_byte_enable_is_the_clock_enable(self):
        m = top_module(elab(BE_SRC["be_noen"], top="t"))
        self.assertEqual(Counter(n.op for n in m.nodes), Counter({"DFF": 4}))
        for n in m.nodes:
            en = dict(n.inputs)["EN"].single_slice()
            self.assertIsInstance(en, Slice)
            self.assertEqual((en.net.name, en.width), ("be", 1))

    def test_free_mask_gives_bitwise_enables(self):
        m = top_module(elab("""
module t (input wire clk, input wire rstn, input wire [31:0] m, input wire [31:0] d, output reg [31:0] q);
  always @(posedge clk or negedge rstn)
    if (!rstn) q <= 32'd0;
    else q <= (q & ~m) | (d & m);
endmodule""", top="t"))
        self.assertEqual(Counter(n.op for n in m.nodes), Counter({"DFF": 32}))

    def test_plain_bitwise_logic_without_the_pattern_is_unchanged(self):
        src = """
module t (input wire clk, input wire [3:0] be, input wire [31:0] d, e, output reg [31:0] q);
  wire [31:0] m = {{8{be[3]}}, {8{be[2]}}, {8{be[1]}}, {8{be[0]}}};
  always @(posedge clk) q <= (q & ~m) | (d & e);
endmodule"""
        m = top_module(elab(src, top="t"))
        self.assertEqual(Counter(n.op for n in m.nodes), Counter({"DFF": 1, "AND": 2, "OR": 1, "NOT": 1}))


class RoleOfIsLinear(unittest.TestCase):
    """``keys._named_keys`` used to rebuild the input dict of the consumer node for every consumer edge:
    quadratic on a node with thousands of inputs (a flat net of 8000 instances whose 8001-entry array became a
    MEM with 8001 ports took 6.8 s instead of 1.4 s).  Built directly (a black box with ``n`` inputs, each fed
    by an unnamed net) so that the guard does not depend on the elaborator's choices.  Counted, not timed."""

    @staticmethod
    def module(n: int):
        md = Mod("wide")
        x = md.inp("x", n)
        ins = {f"p{i:05d}": md.b.not_(x.s[i]) for i in range(n)}
        outs = md.b.blackbox("cell", "u", ins, {"y": 1})
        md.out("y", E(md, outs["y"]))
        return md.done(analyze=False)

    def work(self, n: int) -> int:
        m = self.module(n)
        keys.compute_keys(m)
        h = m.canonical_hash
        count = [0]

        def counting_dict(*a, **k):
            d = dict(*a, **k)
            count[0] += len(d)
            return d
        with mock.patch.object(keys, "dict", counting_dict, create=True):
            keys.compute_keys(m)
        self.assertEqual(m.canonical_hash, h)       # same keys, bit for bit
        return count[0]

    def test_named_keys_are_linear_in_the_inputs_of_a_node(self):
        small, large = self.work(200), self.work(800)
        self.assertLess(large, 40 * 800)            # was about 800 * 800
        self.assertLess(large / small, 8)           # 4 for a linear law, 16 for a quadratic one


if __name__ == "__main__":
    unittest.main()
