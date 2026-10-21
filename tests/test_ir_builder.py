"""IR.md section 12: builder rules (4.1 hash-consing, 4.2 folding, 4.3 identities, 4.4 x)."""
import random
import unittest

from gatecost.ir import Builder, Const, SigSpec, eval_const

from tests.ir_dsl import Mod, E, cat, nodes_of, ops_of


def to_signed(v, w):
    return v - (1 << w) if v >> (w - 1) & 1 else v


class IdentityRules(unittest.TestCase):
    """One fixture per rule of IR.md 4.3."""

    def setUp(self):
        self.m = Mod("id")
        self.x = self.m.inp("x", 8)
        self.y = self.m.inp("y", 8)
        self.s = self.m.inp("s", 1)

    def assertWire(self, e, expect):
        self.assertEqual(e.s, expect.s)
        self.assertEqual(self.m.b.module.nodes, [])

    def assertConst(self, e, value, width=8):
        self.assertEqual(e.s.const_value(), Const(value, width))
        self.assertEqual(self.m.b.module.nodes, [])

    def test_add_zero(self):
        self.assertWire(self.x + self.m.c(0, 8), self.x)
        self.assertWire(self.m.c(0, 8) + self.x, self.x)

    def test_sub_zero(self):
        self.assertWire(self.x - self.m.c(0, 8), self.x)

    def test_or_zero_xor_zero(self):
        self.assertWire(self.x | self.m.c(0, 8), self.x)
        self.assertWire(self.x ^ self.m.c(0, 8), self.x)

    def test_shift_by_zero(self):
        self.assertWire(self.x << self.m.c(0, 3), self.x)

    def test_and_zero_mul_zero(self):
        self.assertConst(self.x & self.m.c(0, 8), 0)
        self.assertConst(self.x * self.m.c(0, 8), 0)

    def test_and_ones_mul_one(self):
        self.assertWire(self.x & self.m.c(0xFF, 8), self.x)
        self.assertWire(self.x * self.m.c(1, 8), self.x)

    def test_xor_self_sub_self(self):
        self.assertConst(self.x ^ self.x, 0)
        self.assertConst(self.x - self.x, 0)

    def test_and_or_self(self):
        self.assertWire(self.x & self.x, self.x)
        self.assertWire(self.x | self.x, self.x)

    def test_double_not(self):
        nn = ~(~self.x)
        self.assertEqual(nn.s, self.x.s)
        # the inner NOT is left unused (removed by the sweep at finalize)
        ir = self.m.done(analyze=False)
        self.assertEqual(ir.nodes, [])

    def test_mux_constant_select(self):
        self.assertWire(self.m.c(1, 1).mux(self.x, self.y), self.y)
        self.assertWire(self.m.c(0, 1).mux(self.x, self.y), self.x)

    def test_mux_same_inputs(self):
        self.assertWire(self.s.mux(self.x, self.x), self.x)

    def test_mux_zero_one_one_bit_is_select(self):
        self.assertWire(self.s.mux(self.m.c(0, 1), self.m.c(1, 1)), self.s)

    def test_mux_constant_data_is_logic_on_select(self):
        r = self.s.mux(self.m.c(0b0011, 4), self.m.c(0b0101, 4))
        # per bit: (1,1)->1  (1,0)->~s  (0,1)->s  (0,0)->0
        ops = ops_of(self.m.b.module)
        self.assertEqual(ops, ["NOT"])                 # one shared NOT of the select
        self.assertEqual(r.w, 4)
        self.assertEqual(r.s[0].const_value(), Const(1, 1))
        self.assertEqual(r.s[3].const_value(), Const(0, 1))
        self.assertEqual(r.s[2].chunks[0].net.name, "s")

    def test_eq_self_is_one(self):
        e = self.x == self.x
        self.assertEqual(e.s.const_value(), Const(1, 1))
        self.assertEqual((self.x != self.x).s.const_value(), Const(0, 1))

    def test_mul_power_of_two_is_shift_wiring(self):
        r = self.x * self.m.c(4, 8)
        self.assertEqual(self.m.b.module.nodes, [])
        self.assertEqual(r.s, cat(self.x[0:6], self.m.c(0, 2)).s)

    def test_unsigned_div_mod_power_of_two_is_wiring(self):
        d = self.x // self.m.c(4, 8)
        mo = self.x % self.m.c(8, 8)
        self.assertEqual(self.m.b.module.nodes, [])
        self.assertEqual(d.s, self.x[2:8].zext(8).s)
        self.assertEqual(mo.s, self.x[0:3].zext(8).s)

    def test_signed_div_power_of_two_stays_a_node(self):
        r = self.x.sg() // self.m.c(4, 8).sg()
        self.assertEqual(ops_of(self.m.b.module), ["DIV"])

    def test_shift_by_constant_is_wiring(self):
        for amt in (0, 1, 3, 7, 9):
            m = Mod("sh")
            x = m.inp("x", 8)
            for name, op in (("shl", lambda a, k: a << k), ("shr", lambda a, k: a >> k)):
                op(x, m.c(amt, 4))
            self.assertEqual(m.b.module.nodes, [])

    def test_dont_care_operand_collapses_to_other_operand(self):
        xx = self.m.c(0, 8, 0xFF)
        self.assertWire(self.x + xx, self.x)
        self.assertWire(self.x | xx, self.x)
        self.assertWire(self.s.mux(self.x, xx), self.x)
        self.assertWire(self.s.mux(xx, self.x), self.x)

    def test_tristate_mux_keeps_z_arm(self):
        z = SigSpec.const(0, 8, 0xFF)
        r = self.m.b.mux(self.s.s, z, self.x.s, width=8, tristate=True)
        n = nodes_of(self.m.b.module, "MUX")
        self.assertEqual(len(n), 1)
        self.assertTrue(n[0].attrs["tristate"])

    def test_no_global_rewriting(self):
        # (a + b) + c is not re-associated
        t = (self.x + self.y) + self.s.zext(8)
        self.assertEqual(ops_of(self.m.b.module), ["ADD", "ADD"])


class HashConsing(unittest.TestCase):
    def setUp(self):
        self.m = Mod("hc")
        self.a = self.m.inp("a", 8)
        self.b = self.m.inp("b", 8)

    def test_equal_subexpressions_merge(self):
        e1 = self.a + self.b
        e2 = self.a + self.b
        self.assertEqual(e1.s, e2.s)
        self.assertEqual(len(self.m.b.module.nodes), 1)

    def test_different_subexpressions_stay_different(self):
        self.a + self.b
        self.a - self.b
        self.b - self.a
        self.a + self.b.zext(8)[0:7].zext(8)
        self.assertEqual(len(self.m.b.module.nodes), 4)

    def test_commutative_operands_either_order(self):
        for op in ("ADD", "MUL", "AND", "OR", "XOR", "XNOR"):
            m = Mod("c")
            a, b = m.inp("a", 8), m.inp("b", 8)
            r1 = m.b.op(op, a.s, b.s, width=8)
            r2 = m.b.op(op, b.s, a.s, width=8)
            self.assertEqual(r1, r2, op)
            self.assertEqual(len(m.b.module.nodes), 1, op)
        m = Mod("c")
        a, b = m.inp("a", 8), m.inp("b", 8)
        self.assertEqual((a == b).s, (b == a).s)
        self.assertEqual((a != b).s, (b != a).s)
        self.assertEqual(len(m.b.module.nodes), 2)

    def test_noncommutative_order_matters(self):
        for op in ("SUB", "DIV", "MOD"):
            m = Mod("c")
            a, b = m.inp("a", 8), m.inp("b", 8)
            r1 = m.b.op(op, a.s, b.s, width=8)
            r2 = m.b.op(op, b.s, a.s, width=8)
            self.assertNotEqual(r1, r2, op)
        m = Mod("c")
        a, b = m.inp("a", 8), m.inp("b", 8)
        self.assertNotEqual((a < b).s, (b < a).s)

    def test_attributes_distinguish(self):
        m = Mod("c")
        a = m.inp("a", 4)
        b = m.inp("b", 4)
        u = m.b.mul(a.s, b.s, 8)
        s = m.b.mul(a.s, b.s, 8, sa=True, sb=True)
        self.assertNotEqual(u, s)

    def test_identical_flops_merge_and_count(self):
        m = Mod("ff")
        d = m.inp("d", 8)
        clk = m.inp("clk", 1)
        q1 = m.b.net(8, "q1")
        q2 = m.b.net(8, "q2")
        m.b.dff(d.s, clk.s, q=q1)
        m.b.dff(d.s, clk.s, q=q2)
        m.out("y1", E(m, q1.sig))
        m.out("y2", E(m, q2.sig))
        ir = m.done(analyze=False)
        dffs = nodes_of(ir, "DFF")
        self.assertEqual(len(dffs), 1)
        self.assertEqual(dffs[0].merge_count, 2)
        self.assertEqual(len(ir.conns), 3)        # q2 <- q1.. plus two outputs

    def test_different_flops_not_merged(self):
        m = Mod("ff")
        d = m.inp("d", 8)
        e = m.inp("e", 8)
        clk = m.inp("clk", 1)
        m.b.dff(d.s, clk.s)
        m.b.dff(e.s, clk.s)
        m.b.dff(d.s, clk.s, clk_edge="neg")
        m.b.dff(d.s, clk.s, init=3)
        self.assertEqual(len(m.b.module.nodes), 4)


class ConstantFolding(unittest.TestCase):
    """Folding agrees with an independent Python evaluation on random operands."""

    def py(self, op, a, b, wa, wb, w, sa=False, sb=False):
        if op in ("ADD", "SUB", "MUL"):
            ea = to_signed(a, wa) if sa and wa < w else a
            eb = to_signed(b, wb) if sb and wb < w else b
            # operands are extended to w when narrower, truncated when wider
            ea &= (1 << w) - 1
            eb &= (1 << w) - 1
            r = {"ADD": ea + eb, "SUB": ea - eb, "MUL": ea * eb}[op]
            return r & ((1 << w) - 1)
        if op in ("AND", "OR", "XOR", "XNOR"):
            ea, eb = a & ((1 << w) - 1), b & ((1 << w) - 1)
            r = {"AND": ea & eb, "OR": ea | eb, "XOR": ea ^ eb, "XNOR": ~(ea ^ eb)}[op]
            return r & ((1 << w) - 1)
        raise AssertionError(op)

    def test_random_arithmetic_and_bitwise(self):
        rnd = random.Random(1234)
        b = Builder("f")
        for _ in range(400):
            op = rnd.choice(["ADD", "SUB", "MUL", "AND", "OR", "XOR", "XNOR"])
            wa, wb, w = rnd.randint(1, 20), rnd.randint(1, 20), rnd.randint(1, 24)
            a, c = rnd.getrandbits(wa), rnd.getrandbits(wb)
            sa, sb = rnd.random() < 0.5, rnd.random() < 0.5
            r = b.op(op, SigSpec.const(a, wa), SigSpec.const(c, wb), width=w,
                     sa=sa if op in ("ADD", "SUB", "MUL") else False,
                     sb=sb if op in ("ADD", "SUB", "MUL") else False)
            cv = r.const_value()
            self.assertIsNotNone(cv, op)
            if op in ("ADD", "SUB", "MUL"):
                exp = self.py(op, a, c, wa, wb, w, sa, sb)
            else:
                exp = self.py(op, a, c, wa, wb, w)
            self.assertEqual(cv.value, exp, (op, a, c, wa, wb, w, sa, sb))
            self.assertEqual(cv.width, w)
        self.assertEqual(b.module.nodes, [])

    def test_random_compare_and_reduce(self):
        rnd = random.Random(99)
        b = Builder("f")
        for _ in range(300):
            w = rnd.randint(1, 16)
            a, c = rnd.getrandbits(w), rnd.getrandbits(w)
            sg = rnd.random() < 0.5
            sa, sc = (to_signed(a, w), to_signed(c, w)) if sg else (a, c)
            for op, f in (("LT", sa < sc), ("LE", sa <= sc), ("GT", sa > sc), ("GE", sa >= sc)):
                r = b.op(op, SigSpec.const(a, w), SigSpec.const(c, w), width=1, sa=sg, sb=sg)
                self.assertEqual(r.const_value(), Const(int(f), 1), (op, a, c, w, sg))
            self.assertEqual(b.op("EQ", SigSpec.const(a, w), SigSpec.const(c, w), width=1).const_value().value, int(a == c))
            self.assertEqual(b.red_xor(SigSpec.const(a, w)).const_value().value, bin(a).count("1") & 1)
            self.assertEqual(b.red_or(SigSpec.const(a, w)).const_value().value, int(a != 0))
            self.assertEqual(b.red_and(SigSpec.const(a, w)).const_value().value, int(a == (1 << w) - 1))

    def test_random_shifts(self):
        rnd = random.Random(7)
        b = Builder("f")
        for _ in range(300):
            w = rnd.randint(1, 20)
            a = rnd.getrandbits(w)
            n = rnd.randint(0, 25)
            m = (1 << w) - 1
            self.assertEqual(b.shl(SigSpec.const(a, w), SigSpec.const(n, 5), w).const_value().value,
                             (a << n) & m)
            self.assertEqual(b.shr(SigSpec.const(a, w), SigSpec.const(n, 5), w).const_value().value, a >> n)
            self.assertEqual(b.sar(SigSpec.const(a, w), SigSpec.const(n, 5), w).const_value().value,
                             (to_signed(a, w) >> n) & m)
            k = n % w
            self.assertEqual(b.rol(SigSpec.const(a, w), SigSpec.const(n, 5), w).const_value().value,
                             ((a << k) | (a >> (w - k))) & m if k else a)
            self.assertEqual(b.ror(SigSpec.const(a, w), SigSpec.const(n, 5), w).const_value().value,
                             ((a >> k) | (a << (w - k))) & m if k else a)

    def test_div_mod_pow_neg_decode_rom(self):
        b = Builder("f")
        for a, c in ((17, 5), (255, 16), (7, 0), (0, 3)):
            q = b.div(SigSpec.const(a, 8), SigSpec.const(c, 8), 8).const_value()
            r = b.mod(SigSpec.const(a, 8), SigSpec.const(c, 8), 8).const_value()
            if c == 0:
                self.assertEqual(q.xmask, 0xFF)
                self.assertEqual(r.xmask, 0xFF)
            else:
                self.assertEqual((q.value, r.value), (a // c, a % c))
        sq = b.div(SigSpec.const(0xF9, 8), SigSpec.const(2, 8), 8, sa=True, sb=True).const_value()
        self.assertEqual(sq.value, (-3) & 0xFF)       # truncates to zero
        sm = b.mod(SigSpec.const(0xF9, 8), SigSpec.const(2, 8), 8, sa=True, sb=True).const_value()
        self.assertEqual(sm.value, (-1) & 0xFF)
        self.assertEqual(b.pow(SigSpec.const(3, 8), 4, 8).const_value().value, 81)
        self.assertEqual(b.neg(SigSpec.const(5, 8), 8).const_value().value, 251)
        self.assertEqual(b.decode(SigSpec.const(5, 3)).const_value(), Const(1 << 5, 8))
        self.assertEqual(b.rom(SigSpec.const(2, 2), [9, 8, 7, 6], 4).const_value(), Const(7, 4))
        self.assertEqual(b.encode(SigSpec.const(0b0100, 4)).const_value(), Const(2, 2))
        self.assertEqual(b.module.nodes, [])

    def test_folding_with_dont_cares(self):
        b = Builder("f")
        x = SigSpec.const(0b1010, 4, xmask=0b0100)       # 1x10 -> value 1010 with bit2 = x
        zero = SigSpec.const(0, 4)
        r = b.and_(x, zero, 4).const_value()
        self.assertEqual(r, Const(0, 4))                  # 0 & x = 0
        r = b.or_(SigSpec.const(0b1000, 4), SigSpec.const(0, 4, xmask=0b0001), 4).const_value()
        self.assertEqual((r.value, r.xmask), (0b1000, 0b0001))
        r = b.add(x, SigSpec.const(1, 4), 4).const_value()
        self.assertEqual(r.xmask, 0xF)                    # arithmetic on x is all-x
        r = b.eq(SigSpec.const(0b01, 2, xmask=0b10), SigSpec.const(0b00, 2)).const_value()
        self.assertEqual(r, Const(0, 1))                  # known bit differs
        r = b.eq(SigSpec.const(0b01, 2, xmask=0b10), SigSpec.const(0b01, 2)).const_value()
        self.assertEqual(r.xmask, 1)                      # unknown

    def test_eval_const_matches_builder(self):
        c = eval_const("ADD", [Const(200, 8), Const(100, 8)], {}, 8)
        self.assertEqual(c, Const(44, 8))


class Helpers(unittest.TestCase):
    def test_logical_helpers(self):
        m = Mod("h")
        a, b = m.inp("a", 4), m.inp("b", 4)
        m.out("y", E(m, m.b.land(a.s, m.b.lnot(b.s))))
        ir = m.done()
        from tests.ir_sim import Sim
        sim = Sim(ir)
        for av in range(16):
            for bv in range(16):
                out = sim.outputs(sim.comb({"a": av, "b": bv}))["y"]
                self.assertEqual(out, int(av != 0 and bv == 0))

    def test_index_mux(self):
        from tests.ir_sim import Sim
        for n in (1, 2, 3, 5, 8):
            m = Mod("ix")
            arms = [m.inp(f"x{i}", 6) for i in range(n)]
            idx = m.inp("i", max(1, (n - 1).bit_length()))
            m.out("y", E(m, m.b.index_mux([x.s for x in arms], idx.s)))
            ir = m.done()
            self.assertEqual(sum(1 for nd in ir.nodes if nd.op == "MUX"), n - 1)
            sim = Sim(ir)
            for i in range(n):
                vec = {f"x{k}": 10 + k for k in range(n)}
                vec["i"] = i
                self.assertEqual(sim.outputs(sim.comb(vec))["y"], 10 + i)


class NodeShapes(unittest.TestCase):
    def test_operand_normalisation_to_result_width(self):
        m = Mod("n")
        a = m.inp("a", 4)
        b = m.inp("b", 4)
        r = m.b.add(a.s, b.s, 8, sa=True, sb=False)
        n = m.b.module.nodes[0]
        self.assertEqual([s.width for _, s in n.inputs], [8, 8])
        self.assertTrue(n.attrs["sa"])
        self.assertFalse(n.attrs["sb"])
        self.assertEqual(n.inputs[0][1].sign_rep, 4) if n.inputs[0][1].chunks[0].net.name == "a" else None

    def test_pmux_collapses(self):
        m = Mod("p")
        s = m.inp("s", 2)
        a, b, c = m.inp("a", 4), m.inp("b", 4), m.inp("c", 4)
        # constant zero select arm is dropped; one remaining arm becomes a MUX
        r = m.b.pmux(a.s, [b.s, c.s], [s.s[0], SigSpec.const(0, 1)])
        self.assertEqual(ops_of(m.b.module), ["MUX"])
        # all arms equal to the default need no node
        m2 = Mod("p")
        s2 = m2.inp("s", 2)
        a2 = m2.inp("a", 4)
        r2 = m2.b.priority(a2.s, [a2.s, a2.s], s2.s)
        self.assertEqual(r2, a2.s)
        self.assertEqual(m2.b.module.nodes, [])


if __name__ == "__main__":
    unittest.main()
