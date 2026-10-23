"""Cost-tree builder on hand-built IR designs (COST_MODEL.md 4, SPEC.md 5.5)."""
import unittest

from gatecost.cost import (
    build_cost_tree, category_totals, cost_op, load_profile, module_totals, tree_confidence,
)
from gatecost.ir import Builder, Const, Design, LowerBlock, Origin, SigSpec, analyze_ir
from tests.ir_designs import rich_module
from tests.ir_dsl import Mod

P = load_profile()


def design_of(*mods, top=None):
    d = Design(top or mods[0].key)
    for m in mods:
        d.add(m)
    return d


def single(m):
    return build_cost_tree(design_of(m), P)


def ops_of(tree, op=None):
    m = next(iter(tree.modules.values()))
    return [o for o in m.ops if op is None or o.op == op]


class Basic(unittest.TestCase):
    def test_single_add_matches_cost_op(self):
        m = Mod("t")
        a, b = m.inp("a", 32), m.inp("b", 32)
        m.out("y", a + b)
        t = single(m.done())
        (o,) = ops_of(t)
        self.assertEqual(o.op, "ADD")
        self.assertEqual(o.cost, cost_op("ADD", {"n": 32}, P).ge3)
        self.assertEqual(o.description, "ADD w32")
        self.assertEqual(t.total(), o.cost)

    def test_truncated_add_uses_effective_width(self):
        m = Mod("t")
        a, b = m.inp("a", 32), m.inp("b", 32)
        m.out("y", (a + b).trunc(16))
        (o,) = ops_of(single(m.done()))
        self.assertEqual(o.cost.typ, cost_op("ADD", {"n": 16}, P).typ)

    def test_mul(self):
        m = Mod("t")
        a, b = m.inp("a", 16), m.inp("b", 16)
        m.out("y", a.mul(b, 32))
        (o,) = ops_of(single(m.done()))
        self.assertEqual(o.description, "MUL 16x16")
        self.assertEqual(o.cost, cost_op("MUL", {"n": 16, "m": 16}, P).ge3)
        self.assertGreater(o.sigma, 0)

    def test_constant_mul_cheaper(self):
        m = Mod("t")
        a = m.inp("a", 16)
        m.out("y", a.mul(m.c(10, 16), 32))
        (o,) = ops_of(single(m.done()))
        self.assertLess(o.cost.typ, cost_op("MUL", {"n": 16, "m": 16}, P).typ)

    def test_unused_logic_free(self):
        m = Mod("t")
        a, b = m.inp("a", 8), m.inp("b", 8)
        _ = a * b
        m.out("y", a & b)
        t = single(m.done())
        self.assertEqual([o.op for o in ops_of(t)], ["AND"])

    def test_register_counts(self):
        m = Mod("t")
        d, clk = m.inp("d", 36), m.inp("clk", 1)
        q = m.b.net(36, "q")
        m.b.dff(d.s, clk.s, q=q)
        m.b.port("y", "out", 36)
        m.b.connect(m.b.module.port("y"), q.sig)
        (op,) = ops_of(single(m.done()))
        self.assertEqual(op.description, "DFF x36 (register stage)")
        self.assertEqual(op.category, "seq")

    def test_fusion_cheaper_than_unfused(self):
        def make():
            m = Mod("t")
            a, b, c = m.inp("a", 16), m.inp("b", 16), m.inp("c", 32)
            m.out("y", a.mul(b, 32).add(c, 32))
            return m
        fused = single(make().done())
        unf = single(make().done(fusion=False))
        self.assertEqual([o.op for o in ops_of(fused)], ["SUM"])
        self.assertLess(fused.total().typ, unf.total().typ)
        self.assertEqual(fused.total().max, fused.total().max)
        self.assertLessEqual(fused.total().min, fused.total().typ)

    def test_fusion_off_in_profile(self):
        m = Mod("t")
        a, b, c = m.inp("a", 16), m.inp("b", 16), m.inp("c", 32)
        m.out("y", a.mul(b, 32).add(c, 32))
        ir = m.done()
        prof = load_profile("generic-asic", {"policy": {"datapath_fusion": False}})
        t = build_cost_tree(design_of(ir), prof)
        self.assertEqual(sorted(o.op for o in ops_of(t)), ["ADD", "MUL"])

    def test_sharing_saves_and_is_additive(self):
        def make(share):
            m = Mod("t")
            a, b, c, d = (m.inp(n, 16) for n in "abcd")
            sel = m.inp("sel", 2)
            blk = LowerBlock(m.b, "comb", origin=Origin("t.v", 1, 1))
            y = m.b.net(32, "y")
            blk.assign(y, SigSpec.const(0, 32))
            blk.case_(sel.s, [([SigSpec.const(0, 2)], lambda: blk.assign(y, m.b.mul(a.s, b.s, 32))),
                              ([SigSpec.const(1, 2)], lambda: blk.assign(y, m.b.mul(c.s, d.s, 32))),
                              ([SigSpec.const(2, 2)], lambda: blk.assign(y, m.b.mul(a.s, d.s, 32)))])
            blk.finish()
            o = m.b.port("o", "out", 32)
            m.b.connect(o, y.sig)
            return m.done()
        prof_off = load_profile("generic-asic", {"policy": {"resource_sharing": "off"}})
        on = build_cost_tree(design_of(make(1)), P)
        off = build_cost_tree(design_of(make(1)), prof_off)
        self.assertLess(on.total().typ, off.total().typ)
        self.assertEqual(on.total().max, off.total().max)
        muls = [o for o in ops_of(on) if o.op == "MUL"]
        self.assertEqual(sum(1 for o in muls if o.cost.typ > 0), 1)
        self.assertTrue(all("shared" in o.tags for o in muls))
        one = cost_op("MUL", {"n": 16, "m": 16}, P)
        self.assertLess(on.total().typ, 2 * one.typ * 1.0 + 3 * 10 ** 6)
        # a 3-way shared MUL is cheaper than 2 separate MULs
        self.assertLess(sum(o.cost.typ for o in muls), 2 * one.typ)

    def test_duplicate_flops_merged_min_typ_kept_max(self):
        m = Mod("t")
        d, clk = m.inp("d", 8), m.inp("clk", 1)
        q1 = m.b.dff(d.s, clk.s)
        q2 = m.b.dff(d.s, clk.s)
        o1 = m.b.port("o1", "out", 8)
        o2 = m.b.port("o2", "out", 8)
        m.b.connect(o1, q1)
        m.b.connect(o2, q2)
        (op,) = ops_of(single(m.done()))
        one = cost_op("DFF", {"n": 8}, P)
        self.assertEqual(op.cost.typ, one.typ)
        self.assertEqual(op.cost.max, 2 * one.max)

    def test_enable_not_double_counted(self):
        m = Mod("t")
        d, en, clk = m.inp("d", 8), m.inp("en", 1), m.inp("clk", 1)
        q = m.b.net(8, "q")
        blk = LowerBlock(m.b, "clocked", clock=clk.s.chunks[0].net)
        blk.if_(en.s, lambda: blk.assign(q, d.s, True))
        blk.finish()
        m.b.connect(m.b.port("o", "out", 8), q.sig)
        t = single(m.done())
        self.assertEqual([o.op for o in ops_of(t)], ["DFF"])
        self.assertEqual(ops_of(t)[0].cost, cost_op("DFF", {"n": 8, "enable": True}, P).ge3)


class Hierarchy(unittest.TestCase):
    def make(self):
        c = Mod("child")
        a, b = c.inp("a", 16), c.inp("b", 16)
        c.out("y", a.mul(b, 32))
        child = c.done()
        t = Builder("top")
        x, y = t.port("x", "in", 16), t.port("y", "in", 16)
        o = t.port("o", "out", 32)
        res = t.inst(child.key, "u0", {"a": x.sig, "b": y.sig}, {"y": 32}, count=3)
        t.connect(o, res["y"])
        top = t.finalize()
        analyze_ir(top)
        return design_of(top, child, top=top.key), child

    def test_counts_and_totals(self):
        d, child = self.make()
        t = build_cost_tree(d, P)
        self.assertEqual(t.instance_counts[child.key], 3)
        mul = cost_op("MUL", {"n": 16, "m": 16}, P)
        self.assertEqual(t.total().typ, 3 * mul.typ)
        tot = module_totals(t)
        self.assertEqual(tot[t.top_key][1].typ, 3 * mul.typ)
        self.assertEqual(tot[t.top_key][0].typ, 0)
        self.assertEqual(t.modules[t.top_key].instances[0].count, 3)
        self.assertEqual(category_totals(t)["mul"], 3 * mul.typ)

    def test_unused_module_ignored(self):
        d, child = self.make()
        u = Mod("unused")
        a = u.inp("a", 32)
        u.out("y", a + a)
        d.add(u.done())
        t = build_cost_tree(d, P)
        self.assertNotIn("unused", t.modules)

    def test_missing_module_and_blackbox(self):
        t = Builder("top")
        x = t.port("x", "in", 8)
        o = t.port("o", "out", 8)
        o2 = t.port("o2", "out", 8)
        r = t.inst("ghost", "u1", {"a": x.sig}, {"y": 8})
        t.connect(o, r["y"])
        r2 = t.blackbox("acme_pll", "u2", {"a": x.sig}, {"y": 8})
        t.connect(o2, r2["y"])
        top = t.finalize()
        analyze_ir(top)
        prof = load_profile("generic-asic", {"blackbox": {"acme_pll": 4200}})
        tree = build_cost_tree(design_of(top), prof)
        ops = tree.modules[top.name].ops
        self.assertEqual(sorted(o.description for o in ops), ["BLACKBOX acme_pll", "BLACKBOX ghost"])
        self.assertEqual(tree.total().typ, 4_200_000)
        self.assertIn(top.name, tree.unsupported_in)
        self.assertEqual(tree_confidence(tree, prof).level, "low")
        self.assertTrue(any(d.code == "MISSING_MODULE" for d in tree.modules[top.name].diagnostics))


class Determinism(unittest.TestCase):
    def test_rich_module_invariants(self):
        m = rich_module()
        t = build_cost_tree(design_of(m), P)
        mod = t.modules[m.key]
        s = [sum(getattr(o.cost, f) for o in mod.ops) for f in ("min", "typ", "max")]
        tot = t.total()
        self.assertEqual([tot.min, tot.typ, tot.max], s)
        for o in mod.ops:
            self.assertLessEqual(o.cost.min, o.cost.typ)
            self.assertLessEqual(o.cost.typ, o.cost.max)
            self.assertTrue(o.description)
        ids = [o.id for o in mod.ops]
        self.assertEqual(ids, sorted(set(ids)))
        t2 = build_cost_tree(design_of(rich_module()), P)
        self.assertEqual(t.modules[m.key].ops, t2.modules[m.key].ops)

    def test_reordering_same_cost(self):
        a = build_cost_tree(design_of(rich_module(1)), P).total()
        b = build_cost_tree(design_of(rich_module(2)), P).total()
        self.assertEqual(a, b)

    def test_confidence_levels(self):
        m = Mod("t")
        a, b = m.inp("a", 32), m.inp("b", 32)
        m.out("y", a // b)
        t = single(m.done())
        self.assertEqual(tree_confidence(t, P).level, "low")  # poor-quality share
        m = Mod("t")
        a, b = m.inp("a", 32), m.inp("b", 32)
        m.out("y", a & b)
        self.assertIn(tree_confidence(single(m.done()), P).level, ("high", "medium"))


if __name__ == "__main__":
    unittest.main()
