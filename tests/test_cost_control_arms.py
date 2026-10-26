"""Control logic: constant arms of PMUX / PRIORITY and nested reductions (COST_MODEL.md 3.2, 3.3).

Regressions (defect D4, open-source validation against Yosys + Nangate 45):

* a PMUX / PRIORITY bit whose arm is a constant (``state_next = S_IDLE`` in a case arm, ``valid_next = 0`` in
  an ``if``) was costed as a full variable arm (half an AOI22 or a MUX2), whereas ``s & 0`` vanishes and
  ``s & 1`` is the select line itself;
* ``z[i] = ^a[W-1:i]`` (Gray to binary, ``cc_gray_to_binary``) was costed as ``W`` independent XOR trees,
  ``W(W-1)/2`` gates (240 GE at W = 16 against 48.7 GE measured), whereas each reduction reuses the next one.
"""
import random
import unittest

from gatecost.cost import build_cost_tree, cost_op, load_profile
from tests.elab_helpers import elab

P = load_profile()


def ge(op, **a):
    c = cost_op(op, a, P)
    return c.min / 1000, c.typ / 1000, c.max / 1000


class ConstArmFormula(unittest.TestCase):
    def test_all_variable_profile_is_the_plain_formula(self):
        for op in ("PMUX", "PRIORITY"):
            for k, w in ((2, 1), (5, 16), (7, 3)):
                plain = cost_op(op, {"k": k, "w": w}, P)
                prof = cost_op(op, {"k": k, "w": w, "arms": [[[2] * (k + 1), w]]}, P)
                self.assertEqual((plain.min, plain.typ, plain.max), (prof.min, prof.typ, prof.max), (op, k, w))

    def test_pmux_constant_arms(self):
        arm, chain = 1.67 / 2, 0.75
        # 3 arms + default, all variable: 4 AOI inputs, 1 merge
        self.assertAlmostEqual(ge("PMUX", k=3, w=1)[1], 4 * arm + 1 * chain, delta=0.01)
        # arms 0, 1, x and default x: 2 AOI inputs, the const-1 select and the AOI output merged (1 merge)
        self.assertAlmostEqual(ge("PMUX", k=3, w=1, arms=[[[0, 1, 2, 2], 1]])[1], 2 * arm + 1 * chain, delta=0.01)
        # a single variable arm, the others 0: sel & x
        self.assertAlmostEqual(ge("PMUX", k=3, w=1, arms=[[[0, 0, 2, 0], 1]])[1], 1.33, delta=0.01)
        # only constant arms: an OR of the select lines of the 1-arms
        self.assertAlmostEqual(ge("PMUX", k=3, w=1, arms=[[[1, 1, 0, 0], 1]])[1], 1 * chain, delta=0.01)
        # arms equal to the default are dropped: 1, 0, 1 with default 1 is ~s2, the none-selected line
        self.assertAlmostEqual(ge("PMUX", k=3, w=1, arms=[[[1, 0, 1, 1], 1]])[1], 0.0, delta=0.01)

    def test_pmux_shared_arms(self):
        arm, chain = 1.67 / 2, 0.75
        # arms x, x, y and default z: x shares one AOI input, its two selects ORed once (1 merged input)
        one = ge("PMUX", k=3, w=1, arms=[[[3, 3, 2, 2], 1]])[1]
        self.assertAlmostEqual(one, 3 * arm + 1 * chain + 1 * chain, delta=0.01)
        # on 8 bits the select OR is paid once
        eight = ge("PMUX", k=3, w=8, arms=[[[3, 3, 2, 2], 8]])[1]
        self.assertAlmostEqual(eight, 8 * (3 * arm + chain) + chain, delta=0.02)
        # an arm carrying the default (hold) is dropped: x, hold, y with default hold
        self.assertAlmostEqual(ge("PMUX", k=3, w=1, arms=[[[2, 3, 2, 3], 1]])[1], 3 * arm + 1 * chain, delta=0.01)

    def test_priority_constant_arms(self):
        # s1 ? 0 : (s2 ? x : y): an AND2 and a MUX2 instead of two MUX2 (typ scale 0.9), plus the select chain
        lo, ty, hi = ge("PRIORITY", k=2, w=1, arms=[[[0, 2, 2], 1]])
        self.assertAlmostEqual(ty, 0.9 * (1.33 + 2.33) + 1.33, delta=0.01)
        # s1 ? 0 : (s2 ? 0 : x) is (s1 | s2) ? 0 : x: one AND2
        self.assertAlmostEqual(ge("PRIORITY", k=2, w=1, arms=[[[0, 0, 2], 1]])[1], 0.9 * 1.33 + 1.33, delta=0.01)
        self.assertAlmostEqual(ge("PRIORITY", k=2, w=1)[1], 0.9 * 2 * 2.33 + 1.33, delta=0.01)
        # s1 ? 1 : (s2 ? 0 : 0): the inner mux is constant 0 (a wire), the outer is the select (half an INV)
        self.assertAlmostEqual(ge("PRIORITY", k=2, w=1, arms=[[[1, 0, 0], 1]])[1], 0.9 * 0.335 + 1.33, delta=0.01)

    def test_priority_shared_arms(self):
        # s1 ? h : (s2 ? x : h): the inner mux is s2 ? x : h, the outer arm h is not the rest -> 2 MUX2
        self.assertAlmostEqual(ge("PRIORITY", k=2, w=1, arms=[[[3, 2, 3], 1]])[1], 0.9 * 2 * 2.33 + 1.33, delta=0.01)
        # s1 ? x : (s2 ? h : h) and s1 ? x : (s2 ? x : h): one MUX2 each
        self.assertAlmostEqual(ge("PRIORITY", k=2, w=1, arms=[[[2, 3, 3], 1]])[1], 0.9 * 2.33 + 1.33, delta=0.01)
        self.assertAlmostEqual(ge("PRIORITY", k=2, w=1, arms=[[[3, 3, 2], 1]])[1], 0.9 * 2.33 + 1.33, delta=0.01)

    def test_constant_arms_never_cost_more_and_keep_the_order(self):
        rng = random.Random(4)
        for _ in range(400):
            op = rng.choice(("PMUX", "PRIORITY"))
            k = rng.randint(1, 9)
            w = rng.randint(1, 12)
            hist = {}
            for _b in range(w):
                kinds = tuple(rng.choice((0, 1, 2, 2, 3, 4)) for _ in range(k + 1))
                hist[kinds] = hist.get(kinds, 0) + 1
            arms = [[list(x), n] for x, n in sorted(hist.items())]
            c = cost_op(op, {"k": k, "w": w, "arms": arms}, P)
            v = cost_op(op, {"k": k, "w": w}, P)
            self.assertTrue(0 <= c.min <= c.typ <= c.max, (op, k, w, arms))
            self.assertLessEqual(c.typ, v.typ, (op, k, w, arms))
            self.assertLessEqual(c.min, v.min, (op, k, w, arms))
            self.assertLessEqual(c.max, v.max, (op, k, w, arms))

    def test_bad_profile_is_rejected(self):
        with self.assertRaises(ValueError):
            cost_op("PMUX", {"k": 3, "w": 2, "arms": [[[2, 2, 2], 2]]}, P)      # 3 kinds for 3 arms + default
        with self.assertRaises(ValueError):
            cost_op("PMUX", {"k": 2, "w": 2, "arms": [[[2, 2, 2], 1]]}, P)      # covers 1 bit of 2
        with self.assertRaises(ValueError):
            cost_op("PRIORITY", {"k": 2, "w": 1, "arms": [[[2, -1, 2], 1]]}, P)  # negative kind


FSM = """module f(input clk, input go, input stop, input [1:0] st, input [7:0] d, output reg [2:0] nx, output reg v);
  always @* begin
    nx = 3'd0; v = 1'b0;
    case (st)
      2'd0: begin nx = go ? 3'd1 : 3'd0; v = 1'b0; end
      2'd1: begin nx = 3'd2; v = d[0]; end
      2'd2: begin nx = stop ? 3'd4 : 3'd2; v = 1'b1; end
    endcase
  end
endmodule
"""


class ConstArmsInRtl(unittest.TestCase):
    def test_next_state_case_uses_constant_arms(self):
        res = elab(FSM, top="f")
        t = build_cost_tree(res.design, P)
        sel = [o for m in t.modules.values() for o in m.ops if o.op in ("PMUX", "PRIORITY")]
        self.assertTrue(sel)
        for o in sel:
            k, w = o.facts["struct"]["k"], o.effective_widths[0]
            plain = cost_op(o.op, {"k": k, "w": w}, P)
            self.assertLess(o.cost.typ, plain.typ, o.description)   # every arm of nx and v is (partly) constant


def gray(width: int) -> str:
    return (f"module g(input [{width - 1}:0] a, output [{width - 1}:0] z);\n"
            f"  genvar i;\n  for (i = 0; i < {width}; i = i + 1) begin : b\n"
            f"    assign z[i] = ^a[{width - 1}:i];\n  end\nendmodule\n")


class NestedReductions(unittest.TestCase):
    def total(self, src, top):
        return build_cost_tree(elab(src, top=top).design, P).total()

    def test_gray_to_binary_is_a_prefix_chain(self):
        t = self.total(gray(16), "g")
        # 15 chained XOR stages: min the serial chain (15 * 1.67), typ (1 + alpha) * 14 + 1 XOR (alpha = 0.4)
        self.assertAlmostEqual(t.min / 1000, 15 * 1.67, delta=0.05)
        self.assertAlmostEqual(t.typ / 1000, 2.0 + 14 * 1.4 * 2.0, delta=0.05)
        self.assertLess(t.typ / 1000, 60)                     # Yosys + Nangate 45: 48.7 GE (it was 240)
        self.assertGreater(t.max / 1000, 48.7)

    def test_chain_cost_grows_linearly(self):
        a = self.total(gray(16), "g").typ
        b = self.total(gray(32), "g").typ
        self.assertLess(b, 2.2 * a)

    def test_independent_reductions_are_unchanged(self):
        src = ("module h(input [15:0] a, output y0, output y1);\n"
               "  assign y0 = ^a[7:0];\n  assign y1 = ^a[15:8];\nendmodule\n")
        t = self.total(src, "h")
        self.assertAlmostEqual(t.typ / 1000, 2 * 7 * 2.0, delta=0.01)

    def test_nested_or_reuses_the_inner_reduction(self):
        src = ("module h(input [15:0] a, output y0, output y1);\n"
               "  assign y0 = |a[7:0];\n  assign y1 = |a;\nendmodule\n")
        both = self.total(src, "h").typ
        inner = cost_op("RED_OR", {"n": 8}, P).typ
        outer = cost_op("RED_OR", {"n": 16}, P).typ
        self.assertLess(both, inner + outer)

    def test_chain_op_capped_by_the_plain_reduction(self):
        from gatecost.cost.logic import reduce_chain_op, reduce_op
        for kind in ("XOR", "AND"):
            for w in range(2, 40):
                for n_in in range(1, w + 1):
                    c, alone = reduce_chain_op(kind, n_in, w, P), reduce_op(kind, w, P)
                    self.assertTrue(c.min <= c.typ <= c.max)
                    self.assertLessEqual(c.min, alone.min)
                    self.assertLessEqual(c.typ, alone.typ)
                    self.assertLessEqual(c.max, alone.max)


if __name__ == "__main__":
    unittest.main()
