"""IR.md section 10 / 12: limits are reached by generated fixtures and handled cleanly."""
import unittest

from gatecost.ir import Builder, Design, Limits, SigSpec, analyze_ir, dump, DEFAULT_LIMITS

from tests.ir_dsl import E


class LimitTests(unittest.TestCase):
    def test_defaults_are_the_documented_ones(self):
        self.assertEqual(DEFAULT_LIMITS.max_nodes, 2_000_000)
        self.assertEqual(DEFAULT_LIMITS.max_nets, 2_000_000)
        self.assertEqual(DEFAULT_LIMITS.max_net_width, 65_536)
        self.assertEqual(DEFAULT_LIMITS.max_mem_depth, 1_048_576)

    def check_blackboxed(self, m, what):
        self.assertTrue(m.is_blackbox)
        self.assertEqual(m.limit_reached, what)
        self.assertEqual([d.code for d in m.diagnostics if d.code == "LIMIT_REACHED"], ["LIMIT_REACHED"])
        self.assertTrue(m.diagnostics[0].affects_cost)
        self.assertEqual(m.nodes, [])
        self.assertEqual([p.name for p in m.ports], ["a", "y"])
        analyze_ir(m)                      # analysis continues on the black box
        self.assertIn("blackbox", dump(m))

    def test_node_limit(self):
        b = Builder("big", limits=Limits(max_nodes=50))
        a = b.port("a", "in", 8)
        y = b.port("y", "out", 8)
        x = a.sig
        for i in range(200):
            x = b.add(x, SigSpec.const(i + 1, 8), 8)
        b.connect(y, x)
        self.check_blackboxed(b.finalize(), "node count")

    def test_net_count_limit(self):
        b = Builder("big", limits=Limits(max_nets=30))
        a = b.port("a", "in", 8)
        y = b.port("y", "out", 8)
        for i in range(100):
            b.net(4, f"w{i}")
        self.check_blackboxed(b.finalize(), "net count")

    def test_net_width_limit_boundary(self):
        b = Builder("wide")
        a = b.port("a", "in", 65536)                 # exactly at the limit: fine
        y = b.port("y", "out", 65536)
        b.connect(y, b.not_(a.sig))
        m = b.finalize()
        self.assertFalse(m.is_blackbox)
        analyze_ir(m)
        b = Builder("wider")
        a = b.port("a", "in", 65537)
        y = b.port("y", "out", 65537)
        b.connect(y, a.sig)
        m = b.finalize()
        self.assertTrue(m.is_blackbox)
        self.assertEqual(m.limit_reached, "net width")

    def test_memory_depth_limit(self):
        b = Builder("m")
        b.port("a", "in", 8)
        b.port("y", "out", 8)
        mem = b.memory("ok", 1_048_576, 8)           # at the limit
        self.assertFalse(b.failed)
        mem2 = b.memory("toobig", 1_048_577, 8)
        self.assertTrue(b.failed)
        m = b.finalize()
        self.assertTrue(m.is_blackbox)
        self.assertEqual(m.limit_reached, "memory depth")

    def test_builder_keeps_going_after_a_limit(self):
        b = Builder("big", limits=Limits(max_nodes=3))
        a = b.port("a", "in", 8)
        x = a.sig
        for i in range(10):
            x = b.mul(x, SigSpec.const(3 + i, 8), 8)       # never raises
        self.assertEqual(x.width, 8)
        self.assertTrue(b.failed)
        self.assertEqual(len(b.module.diagnostics), 1)

    def test_blackboxed_module_in_a_design(self):
        b = Builder("big", limits=Limits(max_nodes=2))
        a = b.port("a", "in", 8)
        y = b.port("y", "out", 8)
        x = a.sig
        for i in range(5):
            x = b.add(x, SigSpec.const(i + 1, 8), 8)
        child = b.finalize()
        top = Builder("top")
        t_a = top.port("a", "in", 8)
        t_y = top.port("y", "out", 8)
        outs = top.inst(child.key, "u", {"a": t_a.sig}, {"y": 8})
        top.connect(t_y, outs["y"])
        d = Design("top")
        d.add(top.finalize())
        d.add(child)
        d.analyze()
        self.assertEqual(d.instance_counts(), {"top": 1, "big": 1})
        self.assertTrue(d.get("big").is_blackbox)

    def test_large_module_builds_and_analyzes(self):
        b = Builder("large")
        a = b.port("a", "in", 16)
        c = b.port("c", "in", 16)
        y = b.port("y", "out", 16)
        x = a.sig
        for i in range(20000):
            x = b.xor(b.add(x, c.sig, 16), SigSpec.const(i & 0xFFFF, 16), 16)
        b.connect(y, x)
        m = b.finalize()
        analyze_ir(m)
        self.assertGreater(len(m.nodes), 30000)
        self.assertEqual(len(m.canonical_hash), 16)

    def test_very_wide_operations_work_at_the_limit(self):
        b = Builder("w")
        a = b.port("a", "in", 4096)
        c = b.port("c", "in", 4096)
        y = b.port("y", "out", 4096)
        b.connect(y, b.add(a.sig, c.sig, 4096))
        m = b.finalize()
        analyze_ir(m)
        self.assertEqual(m.nodes[0].eff["w"], 4096)


if __name__ == "__main__":
    unittest.main()
