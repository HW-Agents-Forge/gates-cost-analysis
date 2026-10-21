"""IR.md section 12: invariants checked after every build."""
import unittest

from gatecost.ir import (
    Builder, Const, IRInvariantError, SigSpec, Slice, analyze_ir, check_module,
)
from gatecost.ir.core import Node

from tests.ir_dsl import E, Mod, nodes_of


class InvariantTests(unittest.TestCase):
    def test_valid_modules_have_no_problems(self):
        m = Mod("ok")
        a, b = m.inp("a", 8), m.inp("b", 8)
        m.out("y", a + b)
        ir = m.done()
        self.assertEqual(check_module(ir), [])

    def test_multiple_drivers_keep_first_and_report(self):
        b = Builder("md")
        a, c = b.port("a", "in", 8), b.port("c", "in", 8)
        y = b.port("y", "out", 8)
        b.connect(y, a.sig)
        b.connect(y, c.sig)                     # second driver
        m = b.finalize()
        self.assertEqual([d.code for d in m.diagnostics], ["MULTIPLE_DRIVERS"])
        self.assertTrue(m.diagnostics[0].affects_cost)
        self.assertEqual(len(m.conns), 1)
        self.assertEqual(m.conns[0].src, a.sig)
        self.assertEqual(check_module(m), [])

    def test_partial_overlap_keeps_undriven_part_of_second_driver(self):
        b = Builder("md")
        a, c = b.port("a", "in", 8), b.port("c", "in", 8)
        y = b.port("y", "out", 8)
        b.connect(SigSpec.of(y, 0, 4), a.sig[0:4])
        b.connect(y, c.sig)                     # bits 0..3 conflict, 4..7 are free
        m = b.finalize()
        self.assertEqual(len(m.conns), 2)
        self.assertEqual(check_module(m), [])
        self.assertEqual(m.diagnostics[0].code, "MULTIPLE_DRIVERS")

    def test_driving_an_input_port_is_a_multiple_driver(self):
        b = Builder("md")
        a, c = b.port("a", "in", 8), b.port("c", "in", 8)
        b.connect(a, c.sig)
        m = b.finalize()
        self.assertEqual(m.diagnostics[0].code, "MULTIPLE_DRIVERS")

    def test_node_and_connection_on_one_net(self):
        b = Builder("md")
        a, c = b.port("a", "in", 8), b.port("c", "in", 8)
        y = b.port("y", "out", 8)
        q = b.net(8, "q")
        d1 = b.dff(a.sig, c.sig[0], q=q)
        b.connect(q, c.sig)
        m = b.finalize()
        self.assertEqual(m.diagnostics[0].code, "MULTIPLE_DRIVERS")

    def test_chunk_outside_net_is_reported(self):
        m = Mod("bad")
        a = m.inp("a", 8)
        m.out("y", a)
        ir = m.done(analyze=False)
        net = ir.port("a")
        bad = SigSpec((Slice(net, 4, 8),))                  # [4+:8] of an 8-bit net
        y = ir.port("y")
        ir.conns[0].src = bad
        self.assertTrue(any("outside net" in p for p in check_module(ir)))

    def test_foreign_net_is_reported(self):
        m = Mod("bad")
        a = m.inp("a", 8)
        m.out("y", a)
        ir = m.done(analyze=False)
        other = Builder("other")
        ghost = other.net(8, "ghost")
        ir.conns[0].src = ghost.sig
        self.assertTrue(any("not in module" in p for p in check_module(ir)))

    def test_width_inconsistency_is_reported(self):
        m = Mod("bad")
        a, b = m.inp("a", 8), m.inp("b", 8)
        m.out("y", a + b)
        ir = m.done(analyze=False)
        add = nodes_of(ir, "ADD")[0]
        add.inputs = (("A", add.inputs[0][1].slice(0, 4)), add.inputs[1])
        self.assertTrue(any("ADD" in p and "width" in p for p in check_module(ir)))

    def test_builder_finalize_raises_on_corruption(self):
        b = Builder("bad")
        a = b.port("a", "in", 8)
        y = b.port("y", "out", 8)
        b.connect(y, a.sig)
        b.module.conns[0].src = SigSpec((Slice(a, 6, 8),))
        with self.assertRaises(IRInvariantError):
            b.finalize()

    def test_builder_can_turn_violations_into_diagnostics(self):
        b = Builder("bad")
        a = b.port("a", "in", 8)
        y = b.port("y", "out", 8)
        b.connect(y, a.sig)
        b.module.conns[0].src = SigSpec((Slice(a, 6, 8),))
        m = b.finalize(check="diag")
        self.assertTrue(any(d.code == "INTERNAL" for d in m.diagnostics))

    def test_every_net_driver_is_its_node(self):
        m = Mod("ok")
        a, b = m.inp("a", 8), m.inp("b", 8)
        m.out("y", (a + b) * b)
        ir = m.done()
        for n in ir.nodes:
            for o in n.outputs:
                self.assertIs(o.driver, n)


class CombinationalLoops(unittest.TestCase):
    def test_loop_is_reported_and_cut(self):
        b = Builder("loop")
        a = b.port("a", "in", 8)
        y = b.port("y", "out", 8)
        w1 = b.net(8, "w1")
        w2 = b.net(8, "w2")
        b.connect(w1, b.add(w2.sig, a.sig, 8))
        b.connect(w2, b.xor(w1.sig, a.sig, 8))
        b.connect(y, w2.sig)
        m = b.finalize()
        codes = [d.code for d in m.diagnostics]
        self.assertIn("COMBINATIONAL_LOOP", codes)
        self.assertEqual(check_module(m), [])
        analyze_ir(m)                                       # analysis still works

    def test_register_feedback_is_not_a_loop(self):
        m = Mod("cnt")
        clk = m.inp("clk", 1)
        q = m.b.net(8, "q")
        m.b.dff(m.b.add(q.sig, SigSpec.const(1, 8), 8), clk.s, q=q)
        m.out("y", E(m, q.sig))
        ir = m.done()
        self.assertEqual([d for d in ir.diagnostics if d.code == "COMBINATIONAL_LOOP"], [])

    def test_self_loop(self):
        b = Builder("self")
        a = b.port("a", "in", 1)
        y = b.port("y", "out", 1)
        w = b.net(1, "w")
        b.connect(w, b.and_(w.sig, a.sig, 1))
        b.connect(y, w.sig)
        m = b.finalize()
        self.assertIn("COMBINATIONAL_LOOP", [d.code for d in m.diagnostics])
        self.assertEqual(check_module(m), [])

    def test_bitwise_dependency_chain_on_one_bus_is_not_a_loop(self):
        # c[i+1] = c[i] & a[i]  written on a single bus: bit-level, no loop
        b = Builder("chain")
        a = b.port("a", "in", 4)
        y = b.port("y", "out", 5)
        c = b.net(5, "c")
        b.connect(SigSpec.of(c, 0, 1), SigSpec.const(1, 1))
        for i in range(4):
            b.connect(SigSpec.of(c, i + 1, 1), b.and_(SigSpec.of(c, i, 1), a.sig[i], 1))
        b.connect(y, c.sig)
        m = b.finalize()
        self.assertEqual([d for d in m.diagnostics if d.code == "COMBINATIONAL_LOOP"], [])
        self.assertEqual(check_module(m), [])


if __name__ == "__main__":
    unittest.main()
