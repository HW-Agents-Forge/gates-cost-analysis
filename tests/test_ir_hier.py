"""Dead logic across the hierarchy (ir/hier.py; IR.md 8, COST_MODEL.md 4.3; validation defect D2).

An instance whose outputs reach no output of the top is removed with its subtree; a partly observed instance is
costed through a context variant of the child (``key{obs:port=mask}``) whose output roots are the observed bits.
"""
import os
import random
import unittest
from unittest import mock

from gatecost.cost import build_cost_tree, load_profile
from gatecost.ir import Builder, Design, export_design
from gatecost.ir.hier import context_key
from tests.elab_helpers import elab, top_module
from tests.ir_sim import Sim

P = load_profile()

CORE = """
module core(input clk, input [31:0] d, output [31:0] q);
  reg [31:0] r;
  always @(posedge clk) r <= r + d;
  assign q = r;
endmodule
"""

# two independent outputs: a 32-bit adder and a 16x16 multiplier
DUAL = """
module dual(input [31:0] a, input [31:0] b, output [31:0] s, output [31:0] p);
  assign s = a + b;
  assign p = a[15:0] * b[15:0];
endmodule
"""


def cost(src: str, top: str, **kw):
    res = elab(src, top=top, **kw)
    tree = build_cost_tree(res.design, P)
    return tree, res


def no_hier():
    return mock.patch.dict(os.environ, {"GATECOST_NO_HIER": "1"})


def unobs(res, key):
    m = res.design.modules[key]
    return [d.message for d in m.diagnostics if d.code == "UNOBSERVABLE_LOGIC"]


class DeadInstances(unittest.TestCase):
    def test_dead_loop_through_instance_costs_nothing(self):
        # the repro of D2: the core's result only feeds the core back, the top output is a constant
        src = CORE + """
module top #(parameter EN = 0)(input clk, input [31:0] d, output [31:0] o);
  wire [31:0] q;
  core u(.clk(clk), .d(q ^ d), .q(q));
  assign o = EN ? q : 32'd0;
endmodule
"""
        tree, res = cost(src, "top")
        self.assertEqual(tree.total().typ, 0)
        self.assertNotIn("core", tree.instance_counts)
        self.assertEqual([n.op for n in top_module(res).nodes], [])
        self.assertTrue(any("1 unobservable instance" in m for m in unobs(res, res.top_key)), res.design.diagnostics)
        with no_hier():
            old, _ = cost(src, "top")
        self.assertGreater(old.total().typ, 400_000)        # 469 GE before the fix

    def test_instance_with_unconnected_output(self):
        tree, _ = cost(CORE + "module top(input clk, input [31:0] d, output [31:0] o);\n"
                              "  core u(.clk(clk), .d(d), .q());\n  assign o = d;\nendmodule\n", "top")
        self.assertEqual(tree.total().typ, 0)

    def test_whole_subtree_is_removed(self):
        src = CORE + """
module mid(input clk, input [31:0] d, output [31:0] q);
  core c0(.clk(clk), .d(d), .q(q));
endmodule
module top(input clk, input [31:0] d, output o);
  wire [31:0] q;
  mid m(.clk(clk), .d(d), .q(q));
  assign o = 1'b0;
endmodule
"""
        tree, _ = cost(src, "top")
        self.assertEqual(tree.total().typ, 0)
        self.assertEqual(sorted(tree.instance_counts), ["top"])

    def test_observed_output_is_costed_exactly_as_before(self):
        src = CORE + "module top(input clk, input [31:0] d, output [31:0] o);\n" \
                     "  core u(.clk(clk), .d(d), .q(o));\nendmodule\n"
        tree, res = cost(src, "top")
        with no_hier():
            old, res_old = cost(src, "top")
        self.assertEqual(tree.total(), old.total())
        self.assertGreater(tree.total().typ, 0)
        self.assertEqual(export_design(res.design), export_design(res_old.design))

    def test_chained_instances_feeding_an_observed_one_are_kept(self):
        src = CORE + """
module top(input clk, input [31:0] d, output [31:0] o);
  wire [31:0] q0, q1;
  core u0(.clk(clk), .d(d), .q(q0));
  core u1(.clk(clk), .d(q0), .q(q1));
  assign o = q1;
endmodule
"""
        tree, _ = cost(src, "top")
        self.assertEqual(tree.instance_counts["core"], 2)
        with no_hier():
            old, _ = cost(src, "top")
        self.assertEqual(tree.total(), old.total())

    def test_side_effects_keep_an_unobserved_instance(self):
        # a child holding an instance of a module absent from the sources (a black box) is never removed
        src = """
module wrap(input [7:0] a, output [7:0] y);
  vendor_cell v(.a(a), .y(y));
endmodule
module top(input [7:0] a, output o);
  wire [7:0] y;
  wrap w(.a(a), .y(y));
  assign o = 1'b0;
endmodule
"""
        tree, res = cost(src, "top")
        self.assertEqual(tree.instance_counts.get("wrap{obs:y=0x0}"), 1)
        self.assertEqual([n.op for n in res.design.modules["wrap{obs:y=0x0}"].nodes], ["BLACKBOX"])


class PartialObservation(unittest.TestCase):
    def test_single_observed_bit(self):
        src = DUAL + """
module top(input [31:0] a, input [31:0] b, output o);
  wire [31:0] s, p;
  dual u(.a(a), .b(b), .s(s), .p(p));
  assign o = s[0];
endmodule
"""
        tree, res = cost(src, "top")
        vk = context_key("dual", (("p", 0), ("s", 1)))
        self.assertEqual(vk, "dual{obs:p=0x0,s=0x1}")
        self.assertEqual(sorted(tree.instance_counts), sorted([res.top_key, vk]))
        self.assertNotIn("dual", res.design.modules)            # the only context took over the plain IR
        ops = sorted(o.op for o in tree.modules[vk].ops)
        self.assertNotIn("MUL", ops)
        self.assertLess(tree.total().typ, 10_000)               # one XOR, not a 32-bit adder and a multiplier
        self.assertTrue(unobs(res, vk))

    def test_instances_with_different_observations(self):
        src = DUAL + """
module top(input [31:0] a, input [31:0] b, output [31:0] o, output [31:0] o2, output [31:0] o3);
  wire [31:0] s0, p0, s1, p1, s2, p2;
  dual u0(.a(a), .b(b), .s(s0), .p(p0));
  dual u1(.a(b), .b(a), .s(s1), .p(p1));
  dual u2(.a(a ^ b), .b(b), .s(s2), .p(p2));
  assign o = s0 ^ p0;
  assign o2 = s1;
  assign o3 = s2[7:0];
endmodule
"""
        tree, res = cost(src, "top")
        full, only_s, low_s = "dual", "dual{obs:p=0x0}", "dual{obs:p=0x0,s=0xff}"
        self.assertEqual({k: tree.instance_counts[k] for k in (full, only_s, low_s)},
                         {full: 1, only_s: 1, low_s: 1})
        tot = {k: sum(o.cost.typ for o in tree.modules[k].ops) for k in (full, only_s, low_s)}
        self.assertGreater(tot[full], tot[only_s])
        self.assertGreater(tot[only_s], tot[low_s])
        self.assertGreater(tot[low_s], 0)
        names = {n.attrs["inst"]: n.attrs.get("ctx_key") for n in top_module(res).nodes if n.op == "INST"}
        self.assertEqual(names, {"u0": None, "u1": only_s, "u2": low_s})
        # the instance key in the parent does not depend on the context: the parent hash is that of a full run
        with no_hier():
            _, old = cost(src, "top")
        self.assertEqual(top_module(res).canonical_hash, top_module(old).canonical_hash)

    def test_variant_count_is_capped(self):
        # beyond ``max_context_variants`` partly observed instances are costed as fully observed (conservative)
        from gatecost.ir import hier
        from gatecost.ir.limits import Limits
        src = DUAL + """
module top(input [31:0] a, input [31:0] b, output [31:0] o, output [31:0] o2, output [31:0] o3);
  wire [31:0] s0, p0, s1, p1, s2, p2;
  dual u0(.a(a), .b(b), .s(s0), .p(p0));
  dual u1(.a(b), .b(a), .s(s1), .p(p1));
  dual u2(.a(a ^ b), .b(b), .s(s2), .p(p2));
  assign o = s0;
  assign o2 = s1[3:0];
  assign o3 = s2[7:0];
endmodule
"""
        with mock.patch.object(hier, "DEFAULT_LIMITS", Limits(max_context_variants=1)):
            tree, res = cost(src, "top")
        self.assertEqual(tree.instance_counts, {res.top_key: 1, "dual": 2, "dual{obs:p=0x0}": 1})

    def test_variant_matches_the_original_on_observed_bits(self):
        # safety: the pruned child computes the same observed bits as the full child, cycle by cycle
        child = """
module blk(input clk, input [7:0] a, input [7:0] b, output [7:0] x, output [7:0] y, output [3:0] z);
  reg [7:0] acc, cnt;
  always @(posedge clk) begin acc <= acc + (a ^ b); cnt <= cnt + 8'd1; end
  assign x = acc;
  assign y = (a * b) ^ cnt;
  assign z = acc[7:4] + b[3:0];
endmodule
"""
        src = child + """
module top(input clk, input [7:0] a, input [7:0] b, output [5:0] o);
  wire [7:0] x, y; wire [3:0] z;
  blk u(.clk(clk), .a(a), .b(b), .x(x), .y(y), .z(z));
  assign o = {z[1:0], x[3:0]};
endmodule
"""
        res = elab(src, top="top")
        vk = "blk{obs:x=0xf,y=0x0,z=0x3}"
        self.assertIn(vk, res.design.modules)
        ref = elab(child, top="blk").design.modules["blk"]
        var = res.design.modules[vk]
        self.assertLess(len(var.nodes), len(ref.nodes))
        rnd = random.Random(7)
        s_ref, s_var = Sim(ref), Sim(var)
        for _ in range(64):
            ins = {"a": rnd.randrange(256), "b": rnd.randrange(256)}
            o_ref, o_var = s_ref.step(ins), s_var.step(ins)
            for port, mk in (("x", 0xF), ("z", 0x3)):
                self.assertEqual(o_ref[port] & mk, o_var[port] & mk, (port, ins))


class Stability(unittest.TestCase):
    SRC = DUAL + CORE + """
module top(input clk, input [31:0] a, input [31:0] b, output [31:0] o);
  wire [31:0] s, p, q;
  dual u(.a(a), .b(b), .s(s), .p(p));
  core c(.clk(clk), .d(p), .q(q));
  assign o = s;
endmodule
"""

    def test_deterministic(self):
        a, ra = cost(self.SRC, "top")
        b, rb = cost(self.SRC, "top")
        self.assertEqual(sorted(ra.design.modules), sorted(rb.design.modules))
        self.assertEqual(export_design(ra.design), export_design(rb.design))
        self.assertEqual(a.total(), b.total())

    def test_reanalysis_keeps_the_contexts(self):
        tree, res = cost(self.SRC, "top")
        res.design.analyze()                              # second call: every module in its recorded context
        self.assertEqual(build_cost_tree(res.design, P).total(), tree.total())
        self.assertEqual(sorted(tree.instance_counts), sorted([res.top_key, "dual{obs:p=0x0}"]))

    def test_stub_in_a_partial_context_is_unresolved(self):
        # changed-cone elaboration leaves unchanged children unbuilt (stubs, absent from the design): one whose
        # outputs are not all observed has a context-dependent cost, the pipeline must elaborate it
        def top(observe):
            b = Builder("top")
            a = b.port("a", "in", 8)
            y = b.port("y", "out", 8)
            o1 = b.inst("stub_full", "u1", {"a": a.sig}, {"y": 8})["y"]
            o2 = b.inst("stub_part", "u2", {"a": a.sig}, {"y": 8})["y"]
            b.connect(y, b.xor(o1, o2.slice(0, 4).zext(8) if not observe else o2, 8))
            return b.finalize()

        for observe, expect in ((True, ()), (False, ("stub_part",))):
            d = Design("top")
            d.add(top(observe))
            d.analyze()
            self.assertEqual(d.unresolved_contexts, expect)
            self.assertEqual([n.op for n in d.modules["top"].nodes].count("INST"), 2)


if __name__ == "__main__":
    unittest.main()
