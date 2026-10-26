"""Select trees flattened per bit (``ir.muxtree``, cost ``MUXTREE``).

Regression (open-source validation, verilog-i2c FSMs against Yosys + Nangate 45): nested ``if`` / ``case`` of a
state machine are lowered to trees of MUX / PMUX / PRIORITY nodes in which every inner node repeats the hold value,
``c1 ? (c2 ? a : h) : h``; each level was costed as a full-width mux (i2c_slave 1028 GE, i2c_init 2327 GE against
583 / 1140 measured), whereas the function is ``(c1 & c2) ? a : h``: one mux per bit over the *distinct* leaves
and one gate per path condition, shared by every bit (and by every signal that tests the same conditions).
"""
import random
import unittest

from gatecost.cost import build_cost_tree, cost_op, load_profile
from gatecost.ir import Design, SigSpec
from gatecost.ir.muxtree import flatten_mux_trees, mark_mux_trees
from gatecost.ir.export import export_module
from tests.ir_dsl import Mod
from tests.ir_sim import Sim

P = load_profile()


def design_of(m):
    d = Design(m.key)
    d.add(m)
    return d


def costs(m, flatten=True):
    """{op id: CostOp} and the total of a single analyzed module (``flatten=False``: trees ignored)."""
    if not flatten:
        m.mux_trees = []
    t = build_cost_tree(design_of(m), P)
    mc = next(iter(t.modules.values()))
    return {o.id: o for o in mc.ops}, t.total()


def hold_tree(w=8):
    """y = c1 ? (c2 ? a : h) : h"""
    m = Mod("t")
    c1, c2 = m.inp("c1", 1), m.inp("c2", 1)
    a, h = m.inp("a", w), m.inp("h", w)
    m.out("y", c1.mux(h, c2.mux(h, a)))
    return m.done()


# ------------------------------------------------------------------------------------------- semantics

def _arm_true(sim, vals, nd, arm):
    s = sim.sig(nd.input("S"), vals)
    if nd.op == "MUX":
        return (s & 1) == arm
    k = nd.attrs["k"]
    if arm == k:
        return s == 0
    if nd.op == "PMUX":
        return (s >> arm) & 1 == 1
    return (s >> arm) & 1 == 1 and s & ((1 << arm) - 1) == 0      # PRIORITY: no earlier select


def _cond(sim, vals, tree, path):
    return all(_arm_true(sim, vals, tree.nodes[path[:i]], path[i]) for i in range(len(path)))


def _leaf_val(vals, leaf):
    if leaf[0] == "c":
        return leaf[1]
    return (vals.get(leaf[1], 0) >> leaf[2]) & 1


class RandomTrees(unittest.TestCase):
    """Flattened tree = original tree, by simulation, on random trees of depth 2-4."""

    def _random_module(self, rng):
        m = Mod("r")
        W = 4
        vars_ = [m.inp(n, W) for n in ("h", "a", "b", "c")]
        sel = m.inp("s", 24)
        onehot = []
        counter = [0]

        def leaf():
            r = rng.random()
            if r < 0.15:
                return m.c(rng.randrange(1 << W), W)
            if r < 0.2:
                return m.c(0, W, x=(1 << W) - 1)
            if r < 0.5:
                return vars_[0]                      # the hold value, repeated
            return rng.choice(vars_)

        def gen(d):
            if d == 0 or (d < 3 and rng.random() < 0.25):
                return leaf()
            op = rng.choice(("MUX", "PMUX", "PRIORITY"))
            if op == "MUX":
                i = rng.randrange(24)
                return sel[i].mux(gen(d - 1), gen(d - 1))
            k = rng.randrange(2, 4)
            cases = [gen(d - 1).s for _ in range(k)]
            dflt = gen(d - 1).s
            counter[0] += 1
            if op == "PMUX":
                p = m.inp(f"p{counter[0]}", k)
                onehot.append((f"p{counter[0]}", k))
                ss = p.s
            else:
                ss = SigSpec.concat([sel[rng.randrange(24)].s for _ in range(k)])
            fn = m.b.pmux if op == "PMUX" else m.b.priority
            from tests.ir_dsl import E
            return E(m, fn(dflt, cases, ss, width=W))

        m.out("y", gen(rng.randrange(2, 5)))
        if rng.random() < 0.5:
            m.out("z", gen(rng.randrange(2, 4)))
        return m.done(), onehot

    def test_flattening_is_exact(self):
        rng = random.Random(1234)
        checked = 0
        for _ in range(150):
            ir, onehot = self._random_module(rng)
            trees = flatten_mux_trees(ir)
            sim = Sim(ir)
            for _ in range(24):
                ins = {p.name: rng.getrandbits(p.width) for p in ir.ports if p.port == "in"}
                for name, k in onehot:            # PMUX selects are one-hot (or none), as the IR assumes
                    ins[name] = 0 if rng.random() < 0.3 else 1 << rng.randrange(k)
                vals = sim.comb(ins)
                for t in trees:
                    out = vals.get(t.root.outputs[0].id, 0)
                    for b, leaves in t.bits:
                        hits = [lf for lf, ps in leaves.items() for p in ps if _cond(sim, vals, t, p)]
                        self.assertEqual(len(hits), 1, "the path conditions must partition the inputs")
                        if hits[0][0] != "x":
                            self.assertEqual(_leaf_val(vals, hits[0]), (out >> b) & 1)
                            checked += 1
        self.assertGreater(checked, 1000)

    def test_flattening_never_costs_more_and_keeps_scenarios_ordered(self):
        rng = random.Random(99)
        for _ in range(60):
            ir, _ = self._random_module(rng)
            ops, tot = costs(ir)
            for o in ops.values():
                self.assertLessEqual(o.cost.min, o.cost.typ)
                self.assertLessEqual(o.cost.typ, o.cost.max)
            ir2, _ = self._random_module(random.Random(rng.random()))
            _, flat = costs(ir2)
            ir2.mux_trees = []
            _, plain = costs(ir2, flatten=False)
            self.assertLessEqual(flat.min, plain.min)
            self.assertLessEqual(flat.typ, plain.typ)
            self.assertLessEqual(flat.max, plain.max)


# ------------------------------------------------------------------------------------------- structure

class Structure(unittest.TestCase):
    def test_hold_tree_record(self):
        m = hold_tree(8)
        (t,) = m.mux_trees
        self.assertEqual(len(t["nodes"]), 1)
        self.assertEqual(t["bits"], [[2, 0, 0, 2, 8]])         # leaves {a, h} on 8 bits, default h
        self.assertEqual((t["sel_and"], t["sel_or"]), (1, 0))   # c1 & c2

    def test_shared_node_is_not_absorbed(self):
        m = Mod("t")
        c1, c2 = m.inp("c1", 1), m.inp("c2", 1)
        a, h = m.inp("a", 8), m.inp("h", 8)
        inner = c2.mux(h, a)
        m.out("y", c1.mux(h, inner))
        m.out("z", inner)                                     # fanout 2
        ir = m.done()
        self.assertEqual(ir.mux_trees, [])
        ops, tot = costs(ir)
        self.assertFalse(any("mux_absorbed" in o.tags or "mux_tree" in o.tags for o in ops.values()))
        self.assertEqual(tot.typ, 2 * cost_op("MUX", {"k": 2, "w": 8}, P).typ)

    def test_select_use_is_not_absorbed(self):
        m = Mod("t")
        c1, c2 = m.inp("c1", 1), m.inp("c2", 1)
        a, h = m.inp("a", 8), m.inp("h", 8)
        s = c2.mux(c1, h[0])                                   # 1-bit mux used as a select only
        m.out("y", s.mux(h, a))
        self.assertEqual(m.done().mux_trees, [])

    def test_priority_negation_path(self):
        # PRIORITY(default h, case0 a if s0, case1 (c ? b : h) if s1): b needs s1 & ~s0 & c
        m = Mod("t")
        s0, s1, c = m.inp("s0", 1), m.inp("s1", 1), m.inp("c", 1)
        a, b, h = m.inp("a", 4), m.inp("b", 4), m.inp("h", 4)
        from tests.ir_dsl import E
        y = E(m, m.b.priority(h.s, [a.s, c.mux(h, b).s], [s0.s, s1.s], width=4))
        m.out("y", y)
        ir = m.done()
        (t,) = ir.mux_trees
        self.assertEqual(t["bits"], [[3, 0, 0, 2, 4]])
        # one AND for (case1 path) & c, one masking AND (s1 & ~s0) of the priority root
        self.assertEqual((t["sel_and"], t["sel_or"]), (2, 0))
        (ft,) = flatten_mux_trees(ir)
        sim = Sim(ir)
        for v in range(64):
            ins = {"s0": v & 1, "s1": (v >> 1) & 1, "c": (v >> 2) & 1, "a": 3, "b": 5, "h": 9}
            vals = sim.comb(ins)
            for _b, leaves in ft.bits:
                hits = [lf for lf, ps in leaves.items() for p in ps if _cond(sim, vals, ft, p)]
                self.assertEqual(len(hits), 1)

    def test_records_do_not_depend_on_declaration_order(self):
        def build(order):
            m = Mod("t")
            ports = {}
            for n in order:
                ports[n] = m.inp(n, 1 if n.startswith("c") else 8)
            c1, c2, c3 = ports["c1"], ports["c2"], ports["c3"]
            a, b, h = ports["a"], ports["b"], ports["h"]
            m.out("y", c1.mux(h, c2.mux(c3.mux(h, b), a)))
            return m.done()
        x = build(["c1", "c2", "c3", "a", "b", "h"])
        y = build(["h", "b", "a", "c3", "c2", "c1"])
        strip = lambda ts: [(t["bits"], t["sel_and"], t["sel_or"], len(t["nodes"])) for t in ts]
        self.assertEqual(strip(x.mux_trees), strip(y.mux_trees))
        self.assertEqual(export_module(x)["canonical_hash"], export_module(y)["canonical_hash"])
        self.assertEqual(costs(x)[1], costs(y)[1])

    def test_pass_changes_no_key(self):
        m = hold_tree(8)
        before = (m.canonical_hash, [(n.shape, n.named_key) for n in m.nodes])
        mark_mux_trees(m)
        self.assertEqual(before, (m.canonical_hash, [(n.shape, n.named_key) for n in m.nodes]))


# ------------------------------------------------------------------------------------------- cost

class Cost(unittest.TestCase):
    def test_hold_tree_exact_cost(self):
        m = hold_tree(8)
        ops, tot = costs(m)
        root = next(o for o in ops.values() if "mux_tree" in o.tags)
        inner = next(o for o in ops.values() if "mux_absorbed" in o.tags)
        self.assertEqual(inner.cost.typ, 0)
        # (c1 & c2) ? a : h: 8 MUX2 + 1 AND2
        exp = cost_op("MUXTREE", {"bits": [[2, 0, 0, 2, 8]], "sel_and": 1}, P)
        self.assertEqual(exp.typ, 8 * P.P["MUX2"] + P.P["AND2"])
        self.assertEqual(root.cost, exp.ge3)
        self.assertEqual(tot, exp.ge3)
        self.assertEqual(root.members, (inner.id,))
        _, plain = costs(hold_tree(8), flatten=False)
        self.assertEqual(plain.typ, 2 * 8 * P.P["MUX2"])

    def test_constant_leaves(self):
        # leaves {x, 0, 1}, default x: 2 AND2 / OR2 per bit, no mux
        c = cost_op("MUXTREE", {"bits": [[1, 1, 1, 2, 1]]}, P)
        self.assertEqual(c.typ, 2 * P.P["AND2"])
        # leaves {x, y, 0}, default 0: x is sel & x (AND2), y a MUX2
        c = cost_op("MUXTREE", {"bits": [[2, 1, 0, 0, 1]]}, P)
        self.assertEqual(c.typ, P.P["AND2"] + P.P["MUX2"])

    def test_monotone(self):
        prev = None
        for v in range(1, 9):
            c = cost_op("MUXTREE", {"bits": [[v, 0, 0, 2, 8]], "sel_and": v}, P)
            self.assertLessEqual(c.min, c.typ)
            self.assertLessEqual(c.typ, c.max)
            if prev is not None:
                self.assertGreater(c.typ, prev.typ)
                self.assertGreaterEqual(c.min, prev.min)
                self.assertGreaterEqual(c.max, prev.max)
            prev = c
        for w in (1, 2, 8, 33):
            a = cost_op("MUXTREE", {"bits": [[3, 1, 0, 2, w]]}, P)
            b = cost_op("MUXTREE", {"bits": [[3, 1, 0, 2, w + 1]]}, P)
            self.assertLess(a.typ, b.typ)

    def test_invalid_profile_is_an_error(self):
        for bad in ([[0, 0, 0, 2, 1]], [[1, 0, 0, 1, 1]], [[1, 2, 0, 2, 1]], [["x"]]):
            with self.assertRaises(ValueError):
                cost_op("MUXTREE", {"bits": bad}, P)

    def test_conditions_shared_between_trees(self):
        # two signals assigned under the same nested condition: c1 & c2 is built once
        m = Mod("t")
        c1, c2 = m.inp("c1", 1), m.inp("c2", 1)
        a, b, h1, h2 = (m.inp(n, 4) for n in ("a", "b", "h1", "h2"))
        m.out("y1", c1.mux(h1, c2.mux(h1, a)))
        m.out("y2", c1.mux(h2, c2.mux(h2, b)))
        ir = m.done()
        self.assertEqual(len(ir.mux_trees), 2)
        self.assertEqual(ir.mux_trees[0]["gates"][0][0], ir.mux_trees[1]["gates"][0][0])
        ops, tot = costs(ir)
        self.assertEqual(tot.typ, 8 * P.P["MUX2"] + P.P["AND2"])
        self.assertEqual(sorted(o.facts["mux_tree"]["sel_and"] for o in ops.values() if "mux_tree" in o.tags), [0, 1])

    def test_no_tree_design_unchanged(self):
        m = Mod("t")
        c1 = m.inp("c1", 1)
        a, h = m.inp("a", 8), m.inp("h", 8)
        m.out("y", c1.mux(h, a))
        ir = m.done()
        self.assertEqual(ir.mux_trees, [])
        self.assertEqual(export_module(ir)["mux_trees"], [])


if __name__ == "__main__":
    unittest.main()
