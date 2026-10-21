"""IR.md section 12: keys and canonical hashes."""
import unittest

from gatecost.ir import Builder, Const, LowerBlock, Origin, SigSpec, analyze_ir, dump

from tests.ir_dsl import E, Mod, cat, nodes_of


def build_design(rename=False, swap=False, reorder=False, reformat=False, width=8, const=3,
                 op="ADD", extra_decl=False):
    """y = (a OP b) * c + K ; r <= t ; where t is a named internal wire."""
    m = Mod("keys")
    names = ("tmp_wire", "u") if rename else ("t", "v")
    decls = [("a", 8), ("b", 8), ("c", 8)]
    if reorder:
        decls.reverse()
    if extra_decl:
        m.b.net(4, "unused_decl")
    ports = {n: m.inp(n, w) for n, w in decls}
    a, b, c = ports["a"], ports["b"], ports["c"]
    clk = m.inp("clk", 1)
    org = (lambda line: Origin("x.v", line + (50 if reformat else 0), 3 if reformat else 1))
    first, second = (b, a) if swap else (a, b)
    t = m.wire(names[0], width)
    s1 = m.b.op(op, first.s, second.s, width=width, origin=org(3))
    m.b.connect(t.s.chunks[0].net, s1)
    if reorder:
        y2 = m.b.mul(t.s, c.s, width, origin=org(4))
        v = m.wire(names[1], width)
        m.b.connect(v.s.chunks[0].net, y2)
    else:
        v = m.wire(names[1], width)
        y2 = m.b.mul(c.s, t.s, width, origin=org(4))
        m.b.connect(v.s.chunks[0].net, y2)
    y = m.b.add(v.s, SigSpec.const(const, width), width, origin=org(5))
    q = m.b.net(width, "r")
    m.b.dff(t.s, clk.s, q=q)
    if reorder:
        m.out("z", E(m, q.sig))
        m.out("y", E(m, y))
    else:
        m.out("y", E(m, y))
        m.out("z", E(m, q.sig))
    return m.done()


def shapes(ir):
    return sorted(n.shape for n in ir.nodes)


class KeyInvariance(unittest.TestCase):
    def setUp(self):
        self.base = build_design()

    def assertSame(self, other):
        self.assertEqual(shapes(other), shapes(self.base))
        self.assertEqual(other.canonical_hash, self.base.canonical_hash)

    def test_base_is_stable(self):
        self.assertSame(build_design())

    def test_renaming_internal_nets(self):
        self.assertSame(build_design(rename=True))

    def test_reordering_declarations_and_statements(self):
        self.assertSame(build_design(reorder=True))
        self.assertSame(build_design(extra_decl=True))

    def test_reformatting_changes_only_locations(self):
        other = build_design(reformat=True)
        self.assertSame(other)
        self.assertNotEqual([n.origin for n in other.nodes], [n.origin for n in self.base.nodes])

    def test_swapping_commutative_operands(self):
        self.assertSame(build_design(swap=True))

    def test_swapping_commutative_operands_in_every_commutative_op(self):
        for op in ("ADD", "MUL", "AND", "OR", "XOR", "XNOR"):
            self.assertEqual(build_design(op=op, swap=True).canonical_hash,
                             build_design(op=op).canonical_hash, op)

    def test_swapping_noncommutative_operands_changes_hash(self):
        self.assertNotEqual(build_design(op="SUB", swap=True).canonical_hash,
                            build_design(op="SUB").canonical_hash)

    def test_changing_a_width_a_constant_or_an_operator(self):
        h = self.base.canonical_hash
        self.assertNotEqual(build_design(width=9).canonical_hash, h)
        self.assertNotEqual(build_design(const=4).canonical_hash, h)
        self.assertNotEqual(build_design(op="SUB").canonical_hash, h)
        self.assertNotEqual(build_design(op="XOR").canonical_hash, h)

    def test_module_hash_depends_on_parameters_and_port_list(self):
        def mk(params=None, port_name="y", w=4):
            m = Mod("p", params)
            a = m.inp("a", w)
            m.out(port_name, a + a.sg(False)[0:w])
            return m.done()
        self.assertEqual(mk().canonical_hash, mk().canonical_hash)
        self.assertNotEqual(mk({"W": 4}).canonical_hash, mk({"W": 5}).canonical_hash)
        self.assertNotEqual(mk(port_name="q").canonical_hash, mk().canonical_hash)

    def test_module_name_is_not_part_of_the_hash(self):
        a = Mod("one")
        x = a.inp("x", 4)
        a.out("y", x + x)
        b = Mod("two")
        x = b.inp("x", 4)
        b.out("y", x + x)
        self.assertEqual(a.done().canonical_hash, b.done().canonical_hash)

    def test_output_wiring_is_part_of_the_hash(self):
        def mk(swap):
            m = Mod("o")
            a, b = m.inp("a", 4), m.inp("b", 4)
            s, d = a + b, a - b
            m.out("y1", d if swap else s)
            m.out("y2", s if swap else d)
            return m.done()
        self.assertNotEqual(mk(False).canonical_hash, mk(True).canonical_hash)

    def test_shape_key_ignores_internal_wire_names_but_not_port_names(self):
        def mk(port):
            m = Mod("s")
            a = m.inp(port, 4)
            m.out("y", a + m.inp("b", 4))
            return m.done()
        self.assertNotEqual(shapes(mk("a")), shapes(mk("z")))

    def test_shape_key_distinguishes_constants_by_value_and_width(self):
        def mk(v, w):
            m = Mod("s")
            a = m.inp("a", 8)
            m.out("y", a + m.c(v, w).zext(8))
            return m.done()
        self.assertEqual(shapes(mk(3, 4)), shapes(mk(3, 4)))
        self.assertNotEqual(shapes(mk(3, 4)), shapes(mk(4, 4)))

    def test_register_merge_count_is_part_of_the_hash(self):
        def mk(n):
            m = Mod("r")
            d, clk = m.inp("d", 4), m.inp("clk", 1)
            qs = []
            for i in range(n):
                q = m.b.net(4, f"q{i}")
                m.b.dff(d.s, clk.s, q=q)
                qs.append(E(m, q.sig))
            m.out("y", qs[0] ^ qs[-1])
            return m.done(analyze=True)
        self.assertNotEqual(mk(1).canonical_hash, mk(2).canonical_hash)

    def test_hash_is_16_bytes_and_hex_exportable(self):
        self.assertEqual(len(self.base.canonical_hash), 16)
        self.assertEqual(len(self.base.nodes[0].shape), 16)


class NamedKeys(unittest.TestCase):
    def test_target_and_role_path(self):
        m = Mod("nk")
        a, b, c, s = m.inp("a", 8), m.inp("b", 8), m.inp("c", 8), m.inp("s", 1)
        prod = a.mul(b, 8)
        m.out("y", s.mux(c, prod))
        ir = m.done()
        mul = nodes_of(ir, "MUL")[0]
        self.assertEqual(mul.named_key, ("nk", "y", "then", "MUL"))
        mux = nodes_of(ir, "MUX")[0]
        self.assertEqual(mux.named_key, ("nk", "y", "", "MUX"))

    def test_named_wire_is_the_target(self):
        m = Mod("nk")
        a, b = m.inp("a", 8), m.inp("b", 8)
        t = m.wire("sum_ab", 8)
        m.b.connect(t.s.chunks[0].net, (a + b).s)
        m.out("y", t.add(a, 8).add(b, 8))
        ir = analyze_ir(m.b.finalize(), fusion=False)
        add = [n for n in ir.nodes if n.named_key[1] == "sum_ab"]
        self.assertEqual(len(add), 1)

    def test_register_target_and_inputs(self):
        m = Mod("nk")
        d, clk, a = m.inp("d", 8), m.inp("clk", 1), m.inp("a", 8)
        q = m.b.net(8, "count")
        m.b.dff((d + a).s, clk.s, q=q)
        m.out("y", E(m, q.sig))
        ir = m.done()
        dff = nodes_of(ir, "DFF")[0]
        self.assertEqual(dff.named_key, ("nk", "count", "", "DFF"))
        add = nodes_of(ir, "ADD")[0]
        self.assertEqual(add.named_key, ("nk", "count", "d", "ADD"))

    def test_commutative_roles_use_canonical_order(self):
        def mk(swap):
            m = Mod("nk")
            a, b, c = m.inp("a", 8), m.inp("b", 8), m.inp("c", 8)
            inner = a.mul(b, 8)
            outer = (c + inner) if swap else (inner + c)
            m.out("y", outer)
            return m.done(fusion=False)
        k1 = sorted(n.named_key for n in mk(False).nodes)
        k2 = sorted(n.named_key for n in mk(True).nodes)
        self.assertEqual(k1, k2)

    def test_duplicate_keys_get_stable_ordinals(self):
        m = Mod("nk")
        a, b, c, d = (m.inp(n, 8) for n in "abcd")
        m.out("y", E(m, SigSpec.concat([(a + b).s, (c + d).s])))
        ir = m.done()
        keys = sorted(n.named_key for n in ir.nodes)
        self.assertEqual(len(set(keys)), len(keys))


if __name__ == "__main__":
    unittest.main()
