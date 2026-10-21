"""IR.md sections 1 and 8: Design, instance counts, constant-port keys, recursion."""
import unittest

from gatecost.ir import Builder, Const, Design, SigSpec, analyze_ir, module_key

from tests.ir_dsl import E, Mod, nodes_of, ops_of


def leaf(name="leaf", width=8, params=None, const_ports=None):
    b = Builder(name, params, const_ports)
    a = b.port("a", "in", width)
    en = b.port("en", "in", 1)
    y = b.port("y", "out", width)
    b.connect(y, b.mux(en.sig, a.sig, b.add(a.sig, SigSpec.const(1, width), width), width=width))
    return b.finalize()


def parent(name, children):
    """children: list of (child key, instance name, count)."""
    b = Builder(name)
    a = b.port("a", "in", 8)
    y = b.port("y", "out", 8)
    x = a.sig
    for key, inst, count in children:
        outs = b.inst(key, inst, {"a": x, "en": SigSpec.const(1, 1)}, {"y": 8}, count=count)
        x = outs["y"]
    b.connect(y, x)
    return b.finalize()


class DesignTests(unittest.TestCase):
    def test_keys_are_canonical(self):
        self.assertEqual(Design.make_key("m"), "m")
        self.assertEqual(Design.make_key("m", {"B": 2, "A": 1}), "m(A=1,B=2)")
        self.assertEqual(Design.make_key("m", {"W": 8}, {"en": Const(1, 1)}), "m(W=8)[en=1'h1]")
        self.assertEqual(module_key("m", {}, {"k": Const(2, 4, 1)}), "m[k=4'b001x]")

    def test_instance_counts_multiply_along_hierarchy(self):
        d = Design("top")
        d.add(leaf())
        d.add(parent("mid", [("leaf", "u0", 1), ("leaf", "u1", 1)]))
        d.add(parent("top", [("mid", "m0", 3)]))
        self.assertEqual(d.instance_counts(), {"top": 1, "mid": 3, "leaf": 6})

    def test_instance_arrays_multiply_by_their_size(self):
        d = Design("top")
        d.add(leaf())
        d.add(parent("top", [("leaf", "gen", 4)]))
        self.assertEqual(d.instance_counts()["leaf"], 4)

    def test_unreferenced_modules_have_no_count(self):
        d = Design("top")
        d.add(leaf())
        d.add(leaf("orphan"))
        d.add(parent("top", [("leaf", "u", 1)]))
        counts = d.instance_counts()
        self.assertNotIn("orphan", counts)

    def test_instance_names_are_identity_but_counts_ignore_them(self):
        # instances are matched by hierarchical path (DIFF.md 3), so the instance name is part
        # of the parent's canonical hash; the counts do not depend on it
        d1, d2, d3 = Design("top"), Design("top"), Design("top")
        for d, nm in ((d1, "a"), (d2, "zzz"), (d3, "a")):
            d.add(leaf())
            d.add(parent("top", [("leaf", nm, 2)]))
            d.analyze()
        self.assertEqual(d1.instance_counts(), d2.instance_counts())
        self.assertEqual(d1.get("top").canonical_hash, d3.get("top").canonical_hash)
        self.assertNotEqual(d1.get("top").canonical_hash, d2.get("top").canonical_hash)

    def test_instance_count_is_part_of_the_parent_hash(self):
        def mk(n):
            d = Design("top")
            d.add(leaf())
            d.add(parent("top", [("leaf", "u", n)]))
            d.analyze()
            return d.get("top").canonical_hash
        self.assertNotEqual(mk(1), mk(2))

    def test_recursion_is_detected_and_cut(self):
        d = Design("a")
        d.add(parent("a", [("b", "ub", 1)]))
        d.add(parent("b", [("a", "ua", 1)]))
        diags = d.detect_recursion()
        self.assertEqual([x.code for x in diags], ["RECURSIVE_INSTANCE"])
        counts = d.instance_counts()
        self.assertEqual(counts, {"a": 1, "b": 1})
        # the cut instance is the second occurrence (b instantiates a)
        inst = nodes_of(d.get("b"), "INST")[0]
        self.assertTrue(inst.tags["recursive"])
        self.assertIn("RECURSIVE_INSTANCE", [x.code for x in d.get("b").diagnostics])

    def test_direct_self_recursion(self):
        d = Design("a")
        d.add(parent("a", [("a", "u", 1)]))
        d.detect_recursion()
        self.assertEqual(d.instance_counts(), {"a": 1})

    def test_missing_module_is_reported(self):
        d = Design("top")
        d.add(parent("top", [("nowhere", "u", 1)]))
        self.assertEqual(d.missing_modules(), ["nowhere"])
        self.assertEqual(d.instance_counts(), {"top": 1})

    def test_blackbox_instances_are_nodes_without_internals(self):
        b = Builder("top")
        a = b.port("a", "in", 4)
        y = b.port("y", "out", 4)
        outs = b.blackbox("acme_pll", "u_pll", {"i": a.sig}, {"o": 4})
        b.connect(y, outs["o"])
        m = b.finalize()
        analyze_ir(m)
        (bb,) = nodes_of(m, "BLACKBOX")
        self.assertEqual(bb.attrs["name"], "acme_pll")
        self.assertEqual(bb.attrs["inst"], "u_pll")


class ConstantPortTests(unittest.TestCase):
    def test_constant_port_gives_a_uniquified_key(self):
        k0 = Design.make_key("leaf")
        k1 = Design.make_key("leaf", {}, {"en": Const(1, 1)})
        k2 = Design.make_key("leaf", {}, {"en": Const(0, 1)})
        self.assertEqual(len({k0, k1, k2}), 3)
        m1 = leaf(const_ports={"en": Const(1, 1)})
        self.assertEqual(m1.key, k1)
        self.assertEqual(m1.const_ports, (("en", (1, 1, 0)),))

    def test_signature_from_instance_inputs(self):
        sig = Design.const_port_signature({"a": SigSpec.const(5, 4), "x": SigSpec.const(0, 2, 1),
                                           "en": SigSpec.const(1, 1)})
        self.assertEqual(sig, {"a": Const(5, 4), "en": Const(1, 1)})

    def test_export_design(self):
        from gatecost.ir import export_design
        d = Design("top")
        d.add(leaf())
        d.add(parent("top", [("leaf", "u0", 2)]))
        d.analyze()
        data = export_design(d)
        self.assertEqual(data["instance_counts"], {"top": 1, "leaf": 2})
        self.assertEqual(data["edges"][0]["child"], "leaf")
        self.assertEqual(sorted(data["modules"]), ["leaf", "top"])

    def test_known_information_is_set_before_the_passes_and_folds_the_child(self):
        free = leaf()
        analyze_ir(free)
        tied = leaf(const_ports={"en": Const(0, 1)})
        analyze_ir(tied)
        # en = 0 selects the unmodified input: the incrementer is dead and the mux is a wire
        self.assertEqual(ops_of(free), ["ADD", "MUX"])
        self.assertEqual(ops_of(tied), [])
        tied1 = leaf(const_ports={"en": Const(1, 1)})
        analyze_ir(tied1)
        self.assertEqual(ops_of(tied1), ["ADD"])

    def test_different_constants_give_different_hashes(self):
        hs = set()
        for c in (None, Const(0, 1), Const(1, 1)):
            m = leaf(const_ports={"en": c} if c is not None else None)
            analyze_ir(m)
            hs.add(m.canonical_hash)
        self.assertEqual(len(hs), 3)

    def test_parameter_set_is_part_of_the_key_and_hash(self):
        a, b = leaf(params={"W": 8}), leaf(params={"W": 9}, width=9)
        analyze_ir(a)
        analyze_ir(b)
        self.assertNotEqual(a.key, b.key)
        self.assertNotEqual(a.canonical_hash, b.canonical_hash)

    def test_constant_register_in_child_after_constant_reset_port(self):
        # a register whose reset value port is tied: still a register (D is not constant)
        b = Builder("c", const_ports={"rv": Const(5, 4)})
        clk, rst = b.port("clk", "in", 1), b.port("rst", "in", 1)
        d = b.port("d", "in", 4)
        rv = b.port("rv", "in", 4)
        y = b.port("y", "out", 4)
        q = b.net(4, "q")
        b.dff(d.sig, clk.sig, arst=rst.sig, arst_value=0, q=q)
        b.connect(y, b.add(q.sig, rv.sig, 4))
        m = b.finalize()
        analyze_ir(m)
        self.assertEqual(ops_of(m), ["ADD", "DFF"])
        add = nodes_of(m, "ADD")[0]
        self.assertIn(5, [v["const"] for v in add.eff["ops"].values()])


if __name__ == "__main__":
    unittest.main()
