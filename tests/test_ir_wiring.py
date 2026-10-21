"""IR.md section 12: wiring (slice, concat, replicate, extension) creates no nodes."""
import unittest

from gatecost.ir import Builder, Const, SigSpec

from tests.ir_dsl import Mod, ops_of


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.m = Mod("w")
        self.a = self.m.inp("a", 8)
        self.b = self.m.inp("b", 4)

    def test_slice_concat_replicate_extend_make_no_nodes(self):
        a, b = self.a, self.b
        e = a[2:6]
        self.assertEqual(e.w, 4)
        c = a.s.cat(b.s)                 # a is the low part
        self.assertEqual(c.width, 12)
        r = b.s.repeat(3)
        self.assertEqual(r.width, 12)
        self.assertEqual(a.zext(16).w, 16)
        self.assertEqual(a.sext(16).w, 16)
        self.assertEqual(a.trunc(3).w, 3)
        self.m.out("y", a.zext(16))
        ir = self.m.done()
        self.assertEqual(ir.nodes, [])

    def test_slices_merge_adjacent_chunks(self):
        a = self.a
        s = SigSpec.concat([a.s[0:4], a.s[4:8]])
        self.assertEqual(len(s.chunks), 1)
        self.assertEqual(s, a.s)

    def test_constants_merge(self):
        s = SigSpec.concat([SigSpec.const(1, 2), SigSpec.const(2, 3)])
        self.assertEqual(len(s.chunks), 1)
        self.assertEqual(s.chunks[0].width, 5)
        self.assertEqual(s.chunks[0].value, 1 | (2 << 2))

    def test_bit_select_and_part_select_ranges(self):
        a = self.a
        self.assertEqual(a.s[3].width, 1)
        with self.assertRaises(ValueError):
            a.s.slice(6, 4)

    def test_sign_rep_of_sign_extension(self):
        a = self.a
        self.assertEqual(a.s.sign_rep, 0)
        self.assertEqual(a.sext(16).s.sign_rep, 8)
        self.assertEqual(a.sext(9).s.sign_rep, 1)
        # zero extension: the top k-1 zeros copy the zero below them
        self.assertEqual(a.zext(16).s.sign_rep, 7)

    def test_sign_rep_of_constants(self):
        self.assertEqual(SigSpec.const(0xFF, 8).sign_rep, 7)
        self.assertEqual(SigSpec.const(0b0110, 4).sign_rep, 0)
        self.assertEqual(SigSpec.const(0b0011, 4).sign_rep, 1)
        self.assertEqual(SigSpec.const(0, 4, xmask=0b1111).sign_rep, 0)  # x never copies

    def test_replicated_bit_counts_as_sign_rep(self):
        a = self.a
        msb = a.s[7]
        s = SigSpec.concat([a.s, msb, msb, msb])
        self.assertEqual(s.sign_rep, 3)

    def test_const_xmask_canonical(self):
        c = Const(0b1111, 4, xmask=0b0101)
        self.assertEqual(c.value, 0b1010)
        self.assertEqual(c.kmask, 0b1010)
        self.assertIs(Const(0b1010, 4, 0b0101), c)         # interned

    def test_concat_lvalue_connect_splits_source(self):
        m = Mod("cl")
        a = m.inp("a", 8)
        hi = m.out("hi", w=4)
        lo = m.out("lo", w=4)
        m.b.connect(SigSpec.concat([lo.s, hi.s]), a.s)
        ir = m.done()
        self.assertEqual(ir.nodes, [])
        self.assertEqual(len(ir.conns), 2)

    def test_wiring_only_design_costs_nothing(self):
        m = Mod("wire")
        a = m.inp("a", 8)
        m.out("y", a.sext(12)[2:10])
        ir = m.done()
        self.assertEqual(ops_of(ir), [])


if __name__ == "__main__":
    unittest.main()
