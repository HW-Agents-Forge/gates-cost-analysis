"""IR.md section 12: procedural lowering fixtures."""
import random
import unittest

from gatecost.ir import Builder, Const, LowerBlock, Origin, SigSpec, analyze_ir, dump

from tests.ir_dsl import nodes_of, ops_of
from tests.ir_sim import Sim


def comb_module(build, ports):
    """ports: list of (name, dir, width)."""
    b = Builder("t")
    nets = {n: b.port(n, d, w) for n, d, w in ports}
    blk = LowerBlock(b, "comb")
    build(b, blk, nets)
    blk.finish()
    return b.finalize()


def k(v, w):
    return SigSpec.const(v, w)


class IfCaseLowering(unittest.TestCase):
    def test_if_else_gives_mux_with_one_group_per_statement(self):
        def build(b, blk, n):
            blk.if_(n["c"].sig, lambda: (blk.assign(n["y"], n["a"].sig), blk.assign(n["z"], n["a"].sig)),
                    lambda: (blk.assign(n["y"], n["b"].sig), blk.assign(n["z"], k(0, 8))))
        m = comb_module(build, [("c", "in", 1), ("a", "in", 8), ("b", "in", 8),
                                ("y", "out", 8), ("z", "out", 8)])
        muxes = nodes_of(m, "MUX")
        self.assertEqual(len(muxes), 2)
        self.assertEqual(muxes[0].tags["group"], muxes[1].tags["group"])
        self.assertEqual(m.diagnostics, [])

    def test_two_statements_two_groups(self):
        def build(b, blk, n):
            blk.if_(n["c"].sig, lambda: blk.assign(n["y"], n["a"].sig), lambda: blk.assign(n["y"], n["b"].sig))
            blk.if_(n["d"].sig, lambda: blk.assign(n["y"], n["a"].sig))
        m = comb_module(build, [("c", "in", 1), ("d", "in", 1), ("a", "in", 8), ("b", "in", 8),
                                ("y", "out", 8)])
        groups = {x.tags["group"] for x in nodes_of(m, "MUX")}
        self.assertEqual(len(groups), 2)

    def test_same_value_in_both_branches_needs_no_node(self):
        def build(b, blk, n):
            blk.if_(n["c"].sig, lambda: blk.assign(n["y"], n["a"].sig), lambda: blk.assign(n["y"], n["a"].sig))
        m = comb_module(build, [("c", "in", 1), ("a", "in", 8), ("y", "out", 8)])
        self.assertEqual(m.nodes, [])

    def test_only_differing_bits_get_a_mux(self):
        def build(b, blk, n):
            blk.assign(n["y"], n["a"].sig)
            blk.if_(n["c"].sig, lambda: blk.assign(n["y"].sig[0:4], n["b"].sig[0:4]))
        m = comb_module(build, [("c", "in", 1), ("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        mux = nodes_of(m, "MUX")
        self.assertEqual(len(mux), 1)
        self.assertEqual(mux[0].outputs[0].width, 4)

    def test_constant_condition_executes_only_taken_branch(self):
        def build(b, blk, n):
            blk.if_(k(1, 1), lambda: blk.assign(n["y"], n["a"].sig), lambda: blk.assign(n["y"], n["b"].sig))
        m = comb_module(build, [("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        self.assertEqual(m.nodes, [])

    def test_elsif_chain_is_priority(self):
        def build(b, blk, n):
            blk.if_chain([(n["s"].sig, lambda: blk.assign(n["y"], n["a"].sig)),
                          (n["t"].sig, lambda: blk.assign(n["y"], n["b"].sig))],
                         lambda: blk.assign(n["y"], k(0, 8)))
        m = comb_module(build, [("s", "in", 1), ("t", "in", 1), ("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m), ["PRIORITY"])
        n = m.nodes[0]
        self.assertEqual(n.attrs["k"], 2)
        # first condition has the highest priority: S = {t, s}, B = {b, a}
        self.assertEqual(dict(n.inputs)["S"].chunks[0].net.name, "s")

    def test_elsif_on_distinct_constants_is_pmux(self):
        def build(b, blk, n):
            sel = n["s"].sig
            blk.if_chain([(b.eq(sel, k(0, 2)), lambda: blk.assign(n["y"], n["a"].sig)),
                          (b.eq(sel, k(1, 2)), lambda: blk.assign(n["y"], n["b"].sig))],
                         lambda: blk.assign(n["y"], k(0, 8)))
        m = comb_module(build, [("s", "in", 2), ("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m).count("PMUX"), 1)
        self.assertNotIn("PRIORITY", ops_of(m))

    def test_case_is_pmux_and_decodes_as_eq_with_constants(self):
        def build(b, blk, n):
            blk.case_(n["s"].sig, [([k(0, 2)], lambda: blk.assign(n["y"], n["a"].sig)),
                                   ([k(1, 2)], lambda: blk.assign(n["y"], n["b"].sig)),
                                   ([k(2, 2), k(3, 2)], lambda: blk.assign(n["y"], n["c"].sig))],
                      None)
        m = comb_module(build, [("s", "in", 2), ("a", "in", 8), ("b", "in", 8), ("c", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m).count("PMUX"), 1)
        self.assertEqual(m.diagnostics, [])         # exhaustive: no default, no latch
        pm = nodes_of(m, "PMUX")[0]
        self.assertEqual(pm.attrs["k"], 2)          # last arm became the default

    def test_default_assignment_then_case_is_one_pmux(self):
        def build(b, blk, n):
            blk.assign(n["y"], n["a"].sig)
            blk.case_(n["s"].sig, [([k(0, 2)], lambda: blk.assign(n["y"], n["b"].sig)),
                                   ([k(1, 2)], lambda: blk.assign(n["y"], n["c"].sig))])
        m = comb_module(build, [("s", "in", 2), ("a", "in", 8), ("b", "in", 8), ("c", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m).count("PMUX"), 1)
        self.assertNotIn("MUX", ops_of(m))
        pm = nodes_of(m, "PMUX")[0]
        self.assertEqual(dict(pm.inputs)["A"].chunks[0].net.name, "a")

    def test_overlapping_labels_give_priority(self):
        def build(b, blk, n):
            blk.case_(n["s"].sig, [([k(1, 2)], lambda: blk.assign(n["y"], n["a"].sig)),
                                   ([SigSpec.const(0, 2, xmask=0b01)], lambda: blk.assign(n["y"], n["b"].sig))],
                      lambda: blk.assign(n["y"], k(0, 8)))
        m = comb_module(build, [("s", "in", 2), ("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m).count("PRIORITY"), 1)

    def test_casez_wildcards_reduce_the_compare(self):
        def build(b, blk, n):
            blk.case_(n["s"].sig, [([SigSpec.const(0b100, 4, xmask=0b0011)], lambda: blk.assign(n["y"], n["a"].sig))],
                      lambda: blk.assign(n["y"], k(0, 8)))
        m = comb_module(build, [("s", "in", 4), ("a", "in", 8), ("y", "out", 8)])
        eq = nodes_of(m, "EQ")[0]
        # only the two known bits are compared
        self.assertEqual(eq.inputs[0][1].width, 2)

    def test_case_with_all_wildcard_label_is_always_taken(self):
        def build(b, blk, n):
            blk.case_(n["s"].sig, [([SigSpec.const(0, 2, xmask=0b11)], lambda: blk.assign(n["y"], n["a"].sig))],
                      lambda: blk.assign(n["y"], n["b"].sig))
        m = comb_module(build, [("s", "in", 2), ("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        self.assertEqual(m.nodes, [])

    def test_nested_if_in_case(self):
        def build(b, blk, n):
            def arm0():
                blk.if_(n["c"].sig, lambda: blk.assign(n["y"], n["a"].sig), lambda: blk.assign(n["y"], n["b"].sig))
            blk.case_(n["s"].sig, [([k(0, 1)], arm0), ([k(1, 1)], lambda: blk.assign(n["y"], k(0, 8)))])
        m = comb_module(build, [("s", "in", 1), ("c", "in", 1), ("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        self.assertEqual(m.diagnostics, [])
        self.assertGreaterEqual(len(nodes_of(m)), 2)

    def test_blocking_assignments_are_visible_to_later_reads(self):
        def build(b, blk, n):
            t = b.net(8, "t")
            blk.assign(t, b.add(n["a"].sig, n["b"].sig, 8))
            blk.assign(n["y"], b.mul(blk.read(t), n["a"].sig, 8))
        m = comb_module(build, [("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m), ["ADD", "MUL"])
        mul = nodes_of(m, "MUL")[0]
        nets = {c.net.driver.op for _, s in mul.inputs for c in s.chunks
                if hasattr(c, "net") and hasattr(c.net.driver, "op")}
        self.assertIn("ADD", nets)

    def test_assign_indexed_dynamic_bit(self):
        def build(b, blk, n):
            blk.assign(n["y"], k(0, 4))
            blk.assign_indexed(n["y"], n["i"].sig, 1, 4, n["v"].sig)
        m = comb_module(build, [("i", "in", 2), ("v", "in", 1), ("y", "out", 4)])
        sim = Sim(m)
        for i in range(4):
            for v in (0, 1):
                out = sim.outputs(sim.comb({"i": i, "v": v}))
                self.assertEqual(out["y"], v << i)

    def test_functional_equivalence_random_case(self):
        rnd = random.Random(5)

        def build(b, blk, n):
            blk.assign(n["y"], n["a"].sig)
            blk.case_(n["s"].sig, [([k(0, 3)], lambda: blk.assign(n["y"], n["b"].sig)),
                                   ([k(1, 3), k(2, 3)], lambda: blk.assign(n["y"], b.add(n["a"].sig, n["b"].sig, 8))),
                                   ([k(5, 3)], lambda: blk.if_(n["c"].sig, lambda: blk.assign(n["y"], k(77, 8))))])
        m = comb_module(build, [("s", "in", 3), ("c", "in", 1), ("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        sim = Sim(m)
        for _ in range(300):
            s, c, a, bb = rnd.getrandbits(3), rnd.getrandbits(1), rnd.getrandbits(8), rnd.getrandbits(8)
            exp = a
            if s == 0:
                exp = bb
            elif s in (1, 2):
                exp = (a + bb) & 0xFF
            elif s == 5 and c:
                exp = 77
            self.assertEqual(sim.outputs(sim.comb({"s": s, "c": c, "a": a, "b": bb}))["y"], exp)


class LatchInference(unittest.TestCase):
    def test_incomplete_if_infers_latch(self):
        def build(b, blk, n):
            blk.if_(n["en"].sig, lambda: blk.assign(n["y"], n["a"].sig))
        m = comb_module(build, [("en", "in", 1), ("a", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m), ["LATCH"])
        la = m.nodes[0]
        self.assertTrue(la.attrs["inferred"])
        self.assertEqual(dict(la.inputs)["EN"].chunks[0].net.name, "en")
        self.assertEqual([d.code for d in m.diagnostics], ["INFERRED_LATCH"])
        self.assertFalse(m.diagnostics[0].affects_cost)

    def test_nested_incomplete_assignment(self):
        def build(b, blk, n):
            def then():
                blk.if_(n["b"].sig, lambda: blk.assign(n["y"], n["a"].sig))
            blk.if_(n["c"].sig, then, lambda: blk.assign(n["y"], k(0, 8)))
        m = comb_module(build, [("c", "in", 1), ("b", "in", 1), ("a", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m).count("LATCH"), 1)
        sim = Sim(m)
        # enable = !c | b   (latch holds only when c && !b)
        la = nodes_of(m, "LATCH")[0]
        for c in (0, 1):
            for bb in (0, 1):
                sim.state.clear()
                vals = sim.comb({"c": c, "b": bb, "a": 9})
                en = sim.sig(dict(la.inputs)["EN"], vals)
                self.assertEqual(en, int((not c) or bb))

    def test_complete_case_has_no_latch(self):
        def build(b, blk, n):
            blk.case_(n["s"].sig, [([k(0, 1)], lambda: blk.assign(n["y"], n["a"].sig))],
                      lambda: blk.assign(n["y"], k(1, 8)))
        m = comb_module(build, [("s", "in", 1), ("a", "in", 8), ("y", "out", 8)])
        self.assertEqual(nodes_of(m, "LATCH"), [])

    def test_partial_latch_bits(self):
        def build(b, blk, n):
            blk.assign(n["y"].sig[0:4], n["a"].sig[0:4])
            blk.if_(n["en"].sig, lambda: blk.assign(n["y"].sig[4:8], n["a"].sig[4:8]))
        m = comb_module(build, [("en", "in", 1), ("a", "in", 8), ("y", "out", 8)])
        la = nodes_of(m, "LATCH")
        self.assertEqual(len(la), 1)
        self.assertEqual(la[0].outputs[0].width, 4)

    def test_case_without_default_infers_latch(self):
        def build(b, blk, n):
            blk.case_(n["s"].sig, [([k(0, 2)], lambda: blk.assign(n["y"], n["a"].sig)),
                                   ([k(1, 2)], lambda: blk.assign(n["y"], n["b"].sig))])
        m = comb_module(build, [("s", "in", 2), ("a", "in", 8), ("b", "in", 8), ("y", "out", 8)])
        self.assertEqual(ops_of(m).count("LATCH"), 1)


class ClockedLowering(unittest.TestCase):
    def reg_module(self, body, async_resets=(), extra=(), width=8, clk_edge="pos"):
        b = Builder("t")
        clk = b.port("clk", "in", 1)
        rst = b.port("rst", "in", 1)
        en = b.port("en", "in", 1)
        d = b.port("d", "in", width)
        y = b.port("y", "out", width)
        q = b.net(width, "q")
        blk = LowerBlock(b, "clocked", clock=clk, async_resets=[(rst, lvl) for lvl in async_resets],
                         clk_edge=clk_edge)
        body(b, blk, q, dict(clk=clk, rst=rst, en=en, d=d))
        blk.finish()
        b.connect(y, q)
        m = b.finalize()
        return b, m

    def test_plain_register(self):
        b, m = self.reg_module(lambda b, blk, q, p: blk.assign(q, p["d"].sig, True))
        self.assertEqual(ops_of(m), ["DFF"])
        d = m.nodes[0]
        self.assertEqual(d.attrs["reset_kind"], "none")
        self.assertEqual(sorted(dict(d.inputs)), ["CLK", "D"])
        self.assertEqual(d.outputs[0].name, "q")

    def test_enable_recognition_creates_no_mux(self):
        b, m = self.reg_module(lambda b, blk, q, p: blk.if_(p["en"].sig, lambda: blk.assign(q, p["d"].sig, True)))
        self.assertEqual(ops_of(m), ["DFF"])
        d = m.nodes[0]
        self.assertTrue(d.attrs["enable_recognized"])
        self.assertEqual(dict(d.inputs)["EN"].chunks[0].net.name, "en")
        self.assertEqual(dict(d.inputs)["D"].chunks[0].net.name, "d")

    def test_inverted_enable(self):
        # if (!en) hold; else q <= d
        b, m = self.reg_module(lambda b, blk, q, p: blk.if_(b.not_(p["en"].sig), None,
                                                            lambda: blk.assign(q, p["d"].sig, True)))
        self.assertEqual(ops_of(m), ["DFF"])
        self.assertEqual(dict(m.nodes[0].inputs)["EN"].chunks[0].net.name, "en")

    def test_enable_written_as_explicit_feedback_mux(self):
        b, m = self.reg_module(lambda b, blk, q, p: blk.assign(
            q, b.mux(p["en"].sig, q.sig, p["d"].sig, 8), True))
        self.assertEqual(ops_of(m), ["DFF"])
        self.assertTrue(m.nodes[0].attrs["enable_recognized"])

    def test_two_nested_enables_are_anded(self):
        def body(b, blk, q, p):
            blk.if_(p["en"].sig, lambda: blk.if_(p["rst"].sig, lambda: blk.assign(q, p["d"].sig, True)))
        b, m = self.reg_module(body)
        self.assertEqual(sorted(ops_of(m)), ["AND", "DFF"])

    def test_sync_reset(self):
        def body(b, blk, q, p):
            blk.if_(p["rst"].sig, lambda: blk.assign(q, k(0, 8), True), lambda: blk.assign(q, p["d"].sig, True))
        b, m = self.reg_module(body)
        self.assertEqual(ops_of(m), ["DFF"])
        a = m.nodes[0].attrs
        self.assertEqual(a["reset_kind"], "sync")
        self.assertEqual(a["reset_value"], Const(0, 8))
        self.assertEqual(a["srst_polarity"], 1)

    def test_sync_reset_active_low_and_nonzero_value(self):
        def body(b, blk, q, p):
            blk.if_(p["rst"].sig, lambda: blk.assign(q, p["d"].sig, True), lambda: blk.assign(q, k(0xA5, 8), True))
        b, m = self.reg_module(body)
        a = m.nodes[0].attrs
        self.assertEqual((a["reset_kind"], a["srst_polarity"], a["reset_value"]), ("sync", 0, Const(0xA5, 8)))

    def test_sync_reset_with_enable(self):
        def body(b, blk, q, p):
            blk.if_(p["rst"].sig, lambda: blk.assign(q, k(0, 8), True),
                    lambda: blk.if_(p["en"].sig, lambda: blk.assign(q, p["d"].sig, True)))
        b, m = self.reg_module(body)
        self.assertEqual(ops_of(m), ["DFF"])
        d = m.nodes[0]
        self.assertEqual(sorted(dict(d.inputs)), ["CLK", "D", "EN", "SRST"])

    def test_enable_outside_reset_keeps_reset_as_logic(self):
        def body(b, blk, q, p):
            blk.if_(p["en"].sig, lambda: blk.if_(p["rst"].sig, lambda: blk.assign(q, k(0, 8), True),
                                                 lambda: blk.assign(q, p["d"].sig, True)))
        b, m = self.reg_module(body)
        d = nodes_of(m, "DFF")[0]
        self.assertIn("EN", dict(d.inputs))
        self.assertNotIn("SRST", dict(d.inputs))
        self.assertEqual(ops_of(m).count("MUX"), 1)

    def test_async_reset_active_low_from_sensitivity(self):
        def body(b, blk, q, p):
            blk.if_(b.not_(p["rst"].sig), lambda: blk.assign(q, k(0, 8), True),
                    lambda: blk.assign(q, p["d"].sig, True))
        b, m = self.reg_module(body, async_resets=(0,))
        self.assertEqual(ops_of(m), ["DFF"])
        a = m.nodes[0].attrs
        self.assertEqual((a["reset_kind"], a["arst_polarity"]), ("async", 0))

    def test_async_reset_active_high_and_set_value(self):
        def body(b, blk, q, p):
            blk.if_(p["rst"].sig, lambda: blk.assign(q, k(0xFF, 8), True),
                    lambda: blk.if_(p["en"].sig, lambda: blk.assign(q, p["d"].sig, True)))
        b, m = self.reg_module(body, async_resets=(1,))
        self.assertEqual(ops_of(m), ["DFF"])
        a = m.nodes[0].attrs
        self.assertEqual((a["reset_kind"], a["arst_polarity"], a["reset_value"]), ("async", 1, Const(0xFF, 8)))
        self.assertIn("EN", dict(m.nodes[0].inputs))

    def test_async_reset_condition_written_as_comparison(self):
        def body(b, blk, q, p):
            blk.if_(b.eq(p["rst"].sig, k(0, 1)), lambda: blk.assign(q, k(0, 8), True),
                    lambda: blk.assign(q, p["d"].sig, True))
        b, m = self.reg_module(body, async_resets=(0,))
        self.assertEqual(ops_of(m), ["DFF"])
        self.assertEqual(m.nodes[0].attrs["reset_kind"], "async")

    def test_async_then_sync_then_enable_in_that_order(self):
        b = Builder("t")
        clk, arst, srst, en, d = (b.port(n, "in", w) for n, w in
                                  (("clk", 1), ("arst", 1), ("srst", 1), ("en", 1), ("d", 8)))
        q = b.net(8, "q")
        blk = LowerBlock(b, "clocked", clock=clk, async_resets=[(arst, 1)])
        blk.if_(arst.sig, lambda: blk.assign(q, k(1, 8), True),
                lambda: blk.if_(srst.sig, lambda: blk.assign(q, k(2, 8), True),
                                lambda: blk.if_(en.sig, lambda: blk.assign(q, d.sig, True))))
        blk.finish()
        y = b.port("y", "out", 8)
        b.connect(y, q)
        m = b.finalize()
        self.assertEqual(ops_of(m), ["DFF"])
        a = m.nodes[0].attrs
        self.assertEqual(a["reset_kind"], "both")
        self.assertEqual((a["reset_value"], a["srst_value"]), (Const(1, 8), Const(2, 8)))
        self.assertTrue(a["enable_recognized"])

    def test_negative_edge_clock(self):
        b, m = self.reg_module(lambda b, blk, q, p: blk.assign(q, p["d"].sig, True), clk_edge="neg")
        self.assertEqual(m.nodes[0].attrs["clk_edge"], "neg")

    def test_nonblocking_reads_old_value(self):
        b = Builder("t")
        clk = b.port("clk", "in", 1)
        x = b.net(4, "x")
        z = b.net(4, "z")
        blk = LowerBlock(b, "clocked", clock=clk)
        blk.assign(x, z.sig, True)
        blk.assign(z, x.sig, True)       # swap: both read the old values
        blk.finish()
        y = b.port("y", "out", 4)
        b.connect(y, x)
        m = b.finalize()
        dffs = nodes_of(m, "DFF")
        self.assertEqual(len(dffs), 2)
        ds = sorted(dict(d.inputs)["D"].chunks[0].net.name or "?" for d in dffs)
        self.assertEqual(ds, ["x", "z"])

    def test_blocking_in_clocked_block_sees_new_value(self):
        b = Builder("t")
        clk = b.port("clk", "in", 1)
        a = b.port("a", "in", 4)
        t = b.net(4, "t")
        q = b.net(4, "q")
        blk = LowerBlock(b, "clocked", clock=clk)
        blk.mark_variable(t)
        blk.assign(t, b.add(a.sig, k(1, 4), 4))
        blk.assign(q, blk.read(t), True)
        blk.finish()
        y = b.port("y", "out", 4)
        b.connect(y, q)
        m = b.finalize()
        self.assertEqual(sorted(ops_of(m)), ["ADD", "DFF"])

    def test_partial_register_bits_with_different_controls(self):
        def body(b, blk, q, p):
            blk.if_(p["en"].sig, lambda: blk.assign(q.sig[0:4], p["d"].sig[0:4], True))
            blk.assign(q.sig[4:8], p["d"].sig[4:8], True)
        b, m = self.reg_module(body)
        dffs = nodes_of(m, "DFF")
        self.assertEqual(len(dffs), 2)
        self.assertEqual([d.outputs[0].width for d in dffs], [4, 4])
        self.assertEqual(sorted("EN" in dict(d.inputs) for d in dffs), [False, True])

    def test_register_function_matches_reference_model(self):
        rnd = random.Random(11)

        def body(b, blk, q, p):
            blk.if_(p["rst"].sig, lambda: blk.assign(q, k(3, 8), True),
                    lambda: blk.if_(p["en"].sig, lambda: blk.assign(q, b.add(q.sig, p["d"].sig, 8), True)))
        b, m = self.reg_module(body)
        analyze_ir(m)
        sim = Sim(m)
        ref = 3                 # registers with a reset start in their reset state
        for _ in range(200):
            rst, en, d = rnd.getrandbits(1), rnd.getrandbits(1), rnd.getrandbits(8)
            out = sim.step({"rst": rst, "en": en, "d": d})
            self.assertEqual(out["y"], ref)
            if rst:
                ref = 3
            elif en:
                ref = (ref + d) & 0xFF

    def test_async_register_function(self):
        rnd = random.Random(12)

        def body(b, blk, q, p):
            blk.if_(b.not_(p["rst"].sig), lambda: blk.assign(q, k(9, 8), True),
                    lambda: blk.assign(q, b.add(q.sig, k(1, 8), 8), True))
        b, m = self.reg_module(body, async_resets=(0,))
        sim = Sim(m)
        ref = 9
        for _ in range(100):
            rst_n = int(rnd.random() < 0.9)
            sim.apply_async({"rst": rst_n})
            if not rst_n:
                ref = 9
            out = sim.step({"rst": rst_n})
            self.assertEqual(out["y"], ref)
            if rst_n:
                ref = (ref + 1) & 0xFF

    def test_memory_write_enabled_by_path_condition(self):
        b = Builder("t")
        clk, we, sel = (b.port(n, "in", 1) for n in ("clk", "we", "sel"))
        a = b.port("a", "in", 4)
        d = b.port("d", "in", 8)
        y = b.port("y", "out", 8)
        mem = b.memory("ram", 16, 8)
        b.connect(y, mem.read(a.sig))
        blk = LowerBlock(b, "clocked", clock=clk)
        blk.if_(we.sig, lambda: blk.if_(sel.sig, lambda: blk.mem_write(mem, a.sig, d.sig)))
        blk.finish()
        m = b.finalize()
        mn = nodes_of(m, "MEM")[0]
        self.assertEqual(mn.attrs["wports"], 1)
        self.assertEqual(mn.attrs["rports"], 1)
        self.assertEqual(mn.attrs["read_style"], "async")
        en = dict(mn.inputs)["w0_en"]
        self.assertEqual(en.chunks[0].net.driver.op, "AND")

    def test_long_elsif_ladder_is_one_priority_node(self):
        def body(b, blk, q, p):
            arms = [(b.eq(p["d"].sig[0:8], k(i, 8)), (lambda i=i: blk.assign(q, k(i, 8), True)))
                    for i in range(1, 200)]
            blk.if_chain(arms)
        b, m = self.reg_module(body)
        analyze_ir(m)
        self.assertEqual(nodes_of(m, "PMUX")[0].attrs["k"], 199)   # distinct constants: exclusive
        self.assertIn("EN", dict(nodes_of(m, "DFF")[0].inputs))

    def test_deeply_nested_ifs(self):
        def body(b, blk, q, p):
            def nest(i):
                if i == 0:
                    blk.assign(q, p["d"].sig, True)
                else:
                    blk.if_(p["en"].sig if i % 2 else p["rst"].sig, lambda: nest(i - 1))
            nest(60)
        b, m = self.reg_module(body)
        self.assertEqual(ops_of(m).count("DFF"), 1)
        self.assertIn("EN", dict(nodes_of(m, "DFF")[0].inputs))

    def test_each_style_gives_the_same_dump(self):
        """Verilog-, SystemVerilog- and VHDL-shaped call sequences give identical IR."""

        def verilog(b, blk, q, p):
            # always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else if (en) q <= d;
            blk.if_(b.not_(p["rst"].sig), lambda: blk.assign(q, k(0, 8), True),
                    lambda: blk.if_(p["en"].sig, lambda: blk.assign(q, p["d"].sig, True)))

        def systemverilog(b, blk, q, p):
            # always_ff, `if (rst_n == 1'b0) ... else if (en == 1'b1)`
            blk.if_(b.eq(p["rst"].sig, k(0, 1)), lambda: blk.assign(q, k(0, 8), True),
                    lambda: blk.if_(b.eq(p["en"].sig, k(1, 1)), lambda: blk.assign(q, p["d"].sig, True)))

        def vhdl(b, blk, q, p):
            # if rst_n = '0' then q <= (others => '0'); elsif rising_edge(clk) then if en = '1' then q <= d;
            blk.if_chain([(b.eq(p["rst"].sig, k(0, 1)), lambda: blk.assign(q, k(0, 8), True)),
                          (k(1, 1), lambda: blk.if_(b.eq(p["en"].sig, k(1, 1)),
                                                    lambda: blk.assign(q, p["d"].sig, True)))])

        dumps = []
        for style in (verilog, systemverilog, vhdl):
            b, m = self.reg_module(style, async_resets=(0,))
            analyze_ir(m)
            dumps.append(dump(m, normalize=True))
        self.assertEqual(dumps[0], dumps[1])
        self.assertEqual(dumps[0], dumps[2])
        self.assertIn("DFF", dumps[0])
        self.assertIn("ARST=rst[0]", dumps[0])

    def test_enable_styles_give_the_same_dump(self):
        def a(b, blk, q, p):
            blk.if_(p["en"].sig, lambda: blk.assign(q, p["d"].sig, True))

        def c(b, blk, q, p):
            blk.assign(q, b.mux(p["en"].sig, q.sig, p["d"].sig, 8), True)

        def d(b, blk, q, p):
            blk.if_(b.not_(p["en"].sig), None, lambda: blk.assign(q, p["d"].sig, True))

        outs = []
        for f in (a, c, d):
            b, m = self.reg_module(f)
            analyze_ir(m)
            outs.append(dump(m, normalize=True))
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[0], outs[2])

    def test_pmux_hold_recognized_as_enable(self):
        def body(b, blk, q, p):
            blk.case_(p["d"].sig[0:2], [([k(0, 2)], lambda: blk.assign(q, k(1, 8), True)),
                                        ([k(1, 2)], lambda: blk.assign(q, k(2, 8), True))])
        b, m = self.reg_module(body)
        d = nodes_of(m, "DFF")[0]
        self.assertIn("EN", dict(d.inputs))
        self.assertTrue(d.attrs["enable_recognized"])
        sim = Sim(m)
        ref = 0
        for dd in (0, 1, 2, 3, 1, 0, 3):
            out = sim.step({"d": dd})
            self.assertEqual(out["y"], ref)
            if dd == 0:
                ref = 1
            elif dd == 1:
                ref = 2


if __name__ == "__main__":
    unittest.main()
