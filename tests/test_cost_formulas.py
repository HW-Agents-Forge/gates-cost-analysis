"""Exact formula values, constant and width handling, sanity bands (COST_MODEL.md section 7)."""
import unittest

from gatecost.cost import cost_op, load_profile
from gatecost.cost.arith import csd

from . import cost_ref as ref

P = load_profile()


def ge(op, **attrs):
    c = cost_op(op, attrs, P)
    return c.min / 1000, c.typ / 1000, c.max / 1000


class Exact(unittest.TestCase):
    def check(self, got, want, msg=""):
        for g, w, s in zip(got, want, ("min", "typ", "max")):
            self.assertAlmostEqual(g, w, delta=0.01, msg=f"{msg} {s}: got {got} want {want}")

    def test_add(self):
        for n in (1, 8, 32, 64):
            self.check(ge("ADD", n=n), ref.add(n), f"ADD {n}")

    def test_sub_neg(self):
        for n in (2, 8, 32):
            self.check(ge("SUB", n=n), ref.sub(n), f"SUB {n}")
        self.assertEqual(ge("NEG", n=1), (0, 0, 0))
        lo = 31 * (1.33 + 2.0)
        self.check(ge("NEG", n=32), (lo, lo, lo * 1.3))

    def test_one_bit_cases(self):
        self.assertEqual(ge("ADD", n=1)[:2], (2.0, 2.0))  # XOR2
        self.assertEqual(ge("MUL", n=1, m=1)[:2], (1.33, 1.33))  # AND2
        self.assertEqual(ge("MUX", k=1, w=32), (0, 0, 0))

    def test_compare(self):
        self.check(ge("EQ", n=32), ref.eq(32))
        self.check(ge("NE", n=32), ref.eq(32))
        self.check(ge("LT", n=32), ref.lt(32))
        self.check(ge("LT", n=32, signed=True), ref.lt(32, True))
        for op in ("LE", "GT", "GE"):
            self.check(ge(op, n=32), ref.lt(32))

    def test_reductions(self):
        self.check(ge("RED_AND", n=32), (31 * 0.55 * 0.85 * 0 + 31 * 0.55, 31 * 0.75 * 0.85, 31 * 1.33 * 1.15))
        self.check(ge("RED_XOR", n=8), (7 * 1.67, 7 * 2.0 * 0.85, 7 * 2.33 * 1.15))

    def test_bitwise(self):
        g = ge("AND", n=16)
        self.check(g, (16 * 1.33 * 0.85, 16 * 1.33 * 0.85, 16 * 1.33 * 1.15))
        n = ge("NOT", n=16)
        self.check(n, (0, 16 * 0.67 / 2, 16 * 0.67 * 1.15))

    def test_mux(self):
        for k in (2, 3, 4, 16):
            self.check(ge("MUX", k=k, w=32), ref.mux(k, 32), f"MUX {k}:1")

    def test_pmux_priority_decode(self):
        self.check(ge("PMUX", k=5, w=1), ref.pmux(5, 1))
        self.check(ge("PMUX", k=5, w=16), ref.pmux(5, 16))
        for s in (3, 5):
            self.check(ge("DECODE", s=s), ref.decode(s), f"DECODE {s}")
        lo, typ, hi = ge("PRIORITY", k=5, w=8)
        self.assertAlmostEqual(typ, 0.9 * 5 * 8 * 2.33 + 4 * 1.33, delta=0.01)
        self.assertAlmostEqual(hi, (5 * 8 * 2.33 + 4 * 1.33) * 1.15, delta=0.01)

    def test_shifts(self):
        for s in (5, 8):
            self.check(ge("SHL", n=32, s=s), ref.shl(32, s), f"SHL s={s}")
            self.check(ge("SHR", n=32, s=s), ref.shl(32, s), f"SHR s={s}")
        self.check(ge("ROL", n=32, s=5), ref.rot(32, 5))
        self.check(ge("ROR", n=32, s=8), ref.rot(32, 8))
        self.check(ge("SAR", n=32, s=5), ref.rot(32, 5))

    def test_mul(self):
        for n, m in ((8, 8), (16, 16), (32, 32), (24, 8), (8, 24), (12, 5), (64, 2)):
            self.check(ge("MUL", n=n, m=m), ref.mul(n, m), f"MUL {n}x{m}")
        self.check(ge("MUL", n=16, m=16, signed=True), ref.mul(16, 16, True))

    def test_mul_operand_order(self):
        self.assertEqual(ge("MUL", n=24, m=8), ge("MUL", n=8, m=24))

    def test_mul_square_and_truncated(self):
        full = ge("MUL", n=16, m=16)
        sq = ge("MUL", n=16, m=16, square=True)
        for f, s in zip(full, sq):
            self.assertAlmostEqual(s, 0.55 * f, delta=0.01)
        tri = ge("MUL", n=16, m=16, trunc=16)
        self.assertLess(tri[1], 0.7 * full[1])  # triangular array
        self.assertLess(ge("MUL", n=16, m=16, trunc=8)[1], tri[1])

    def test_div(self):
        self.check(ge("DIV", n=32, m=16), ref.div(32, 16))
        self.assertEqual(ge("DIV", n=32, m=16), ge("MOD", n=32, m=16))

    def test_registers(self):
        base = dict(n=32)
        cases = [
            ({}, {}), ({"async_reset": True}, {"async_reset": True}), ({"sync_reset": True}, {"sync_reset": True}),
            ({"enable": True}, {"enable": True}), ({"async_reset": True, "enable": True}, {"async_reset": True, "enable": True}),
            ({"sync_reset": True, "enable": True}, {"sync_reset": True, "enable": True}),
        ]
        for attrs, kw in cases:
            for scan in (False, True):
                with self.subTest(attrs=attrs, scan=scan):
                    self.check(ge("DFF", **base, **attrs, scan=scan), ref.reg(32, scan=scan, **kw))
        self.assertAlmostEqual(ge("DFF", n=32)[1], 181.44, delta=0.01)
        self.assertAlmostEqual(ge("DFFRS", n=1)[1] if False else ge("DFF", n=1, async_set_reset=True)[1], 7.67, delta=0.01)

    def test_enable_styles(self):
        for style in ("mux", "edff", "icg"):
            p = load_profile("generic-asic", {"policy": {"enable_style": style}})
            c = cost_op("DFF", {"n": 32, "enable": True}, p)
            self.assertAlmostEqual(c.typ / 1000, ref.reg(32, enable=True, style=style)[1], delta=0.01, msg=style)
        p = load_profile("generic-asic", {"policy": {"enable_style": "icg"}})
        # below icg_min_width gating is not used
        self.assertAlmostEqual(cost_op("DFF", {"n": 2, "enable": True}, p).typ / 1000, 2 * 7.33, delta=0.01)

    def test_latch(self):
        self.assertEqual(ge("LATCH", n=8)[:2], (8 * 3.33, 8 * 3.33))

    def test_mem_flop_based(self):
        self.check(ge("MEM", depth=16, width=8), ref.mem_flop(16, 8))
        self.check(ge("MEM", depth=16, width=8, rports=2, wports=2), ref.mem_flop(16, 8, 2, 2))

    def test_mem_macro_and_async_stays_flop(self):
        c = cost_op("MEM", {"depth": 1024, "width": 32, "sync_read": True}, P)
        want = 3000 + 0.6 * 1024 * 32 + 4 * 1024 + 30 * 32
        self.assertAlmostEqual(c.typ / 1000, want, delta=0.01)
        self.assertAlmostEqual(c.min / 1000, 0.5 * want, delta=0.01)
        self.assertAlmostEqual(c.max / 1000, 2 * want, delta=0.01)
        self.assertEqual((c.category, c.quality, c.arch["typ"], c.flags), ("mem", "poor", "macro", ("macro_no_table",)))
        a = cost_op("MEM", {"depth": 1024, "width": 32}, P)
        self.assertEqual(a.arch["typ"], "flop_array")
        self.assertAlmostEqual(a.typ / 1000, ref.mem_flop(1024, 32)[1], delta=0.01)
        # too small, too shallow, macros disabled
        self.assertEqual(cost_op("MEM", {"depth": 8, "width": 1024, "sync_read": True}, P).arch["typ"], "flop_array")
        self.assertEqual(cost_op("MEM", {"depth": 64, "width": 32, "sync_read": True}, P).arch["typ"], "flop_array")
        off = load_profile("generic-asic", {"memory": {"macro_enable": False}})
        self.assertEqual(cost_op("MEM", {"depth": 1024, "width": 32, "sync_read": True}, off).arch["typ"], "flop_array")

    def test_mem_macro_table_interpolation(self):
        tab = [{"d": 256, "w": 32, "ge": 9800}, {"d": 512, "w": 32, "ge": 17000},
               {"d": 256, "w": 64, "ge": 18000}, {"d": 512, "w": 64, "ge": 33000}]
        p = load_profile("generic-asic", {"memory": {"macro_table": tab}})
        c = cost_op("MEM", {"depth": 256, "width": 32, "sync_read": True}, p)
        self.assertAlmostEqual(c.typ / 1000, 9800, delta=0.01)
        self.assertEqual(c.flags, ())
        mid = cost_op("MEM", {"depth": 384, "width": 48, "sync_read": True}, p)
        self.assertAlmostEqual(mid.typ / 1000, (9800 + 17000 + 18000 + 33000) / 4, delta=0.01)

    def test_rom(self):
        # 8 x 4 table: bit0 = a0, bit1 = ~a1, bit2 = constant 1, bit3 = a2
        words = [(i & 1) | ((~(i >> 1) & 1) << 1) | (1 << 2) | (((i >> 2) & 1) << 3) for i in range(8)]
        c = cost_op("ROM", {"depth": 8, "width": 4, "contents": words}, P)
        self.assertEqual((c.min, c.typ), (670, 670))  # only the complemented address bit costs an INV
        # duplicates and complements are shared
        f = [0, 1, 1, 0, 1, 0, 0, 1]  # parity-like, not an address bit
        words = [(b | (b << 1) | ((1 - b) << 2)) for b in f]
        one = cost_op("ROM", {"depth": 8, "width": 1, "contents": f}, P)
        three = cost_op("ROM", {"depth": 8, "width": 3, "contents": words}, P)
        self.assertGreater(one.typ, 0)
        self.assertEqual(three.typ, one.typ + 670)
        self.assertEqual(three.min, one.min + 670)
        # without contents every column is random logic: typ = 2.0 D / log2 D per column
        r = cost_op("ROM", {"depth": 64, "width": 8}, P)
        self.assertAlmostEqual(r.typ / 1000, 8 * 2.0 * 64 / 6, delta=0.05)
        self.assertAlmostEqual(r.min / 1000, r.typ / 2000, delta=0.01)
        self.assertEqual(r.quality, "poor")

    def test_rom_small_uses_literal_count(self):
        # 4-input AND of address bits: one product term of 4 literals (or its complement)
        f = [1 if i == 15 else 0 for i in range(16)]
        c = cost_op("ROM", {"depth": 16, "width": 1, "contents": f}, P)
        self.assertAlmostEqual(c.typ / 1000, 4 * 0.75 * 1.0, delta=0.01)

    def test_blackbox(self):
        p = load_profile("generic-asic", {"blackbox": {"acme_pll": 4200, "sync2": {"typ": 13.5, "min": 12, "max": 15}}})
        self.assertEqual(cost_op("BLACKBOX", {"name": "acme_pll"}, p).typ, 4_200_000)
        c = cost_op("BLACKBOX", {"name": "sync2"}, p)
        self.assertEqual((c.min, c.typ, c.max), (12000, 13500, 15000))
        u = cost_op("BLACKBOX", {"name": "nope"}, p)
        self.assertEqual((u.typ, u.flags), (0, ("unknown_blackbox",)))


class Constants(unittest.TestCase):
    def test_shift_by_constant_is_free(self):
        for op in ("SHL", "SHR", "SAR", "ROL", "ROR"):
            self.assertEqual(ge(op, n=32, s=0, const_amount=True), (0, 0, 0))

    def test_one_shifted_by_x_is_a_decoder(self):
        self.assertEqual(ge("SHL", n=1, s=5, lhs_one=True), ge("DECODE", s=5))

    def test_multiply_by_power_of_two_is_wiring(self):
        for k in (1, 2, 8, 1024):
            self.assertEqual(ge("MUL", n=16, const=k), (0, 0, 0))
        self.assertEqual(ge("MUL", n=16, const=0), (0, 0, 0))

    def test_multiply_by_ten_is_one_adder(self):
        # 10 = 8 + 2: one ADD at width n + clog2(10) - dmin = 16 + 4 - 2
        c = cost_op("MUL", {"n": 16, "const": 10}, P)
        add = cost_op("ADD", {"n": 18}, P)
        self.assertEqual(c.typ, add.typ)
        self.assertEqual(c.min, round(add.typ * 0.7))
        self.assertEqual(c.max, cost_op("ADD", {"n": 20}, P).max)
        self.assertEqual(c.breakdown, add.breakdown)

    def test_multiply_by_seven_is_one_subtractor(self):
        c = cost_op("MUL", {"n": 16, "const": 7}, P)
        self.assertEqual(c.typ, cost_op("SUB", {"n": 16}, P).typ)
        self.assertEqual(csd(7), [(0, -1), (3, 1)])

    def test_multiply_by_negative_power_of_two_is_a_negation(self):
        self.assertEqual(cost_op("MUL", {"n": 16, "const": -4}, P).typ, cost_op("NEG", {"n": 16}, P).typ)

    def test_csd_digit_count(self):
        for k in (3, 5, 10, 85, 119, 255, 1000, 123456):
            digs = csd(k)
            self.assertEqual(sum(s << p for p, s in digs), k)
            self.assertTrue(all(b[0] - a[0] >= 2 for a, b in zip(digs, digs[1:])))
        self.assertEqual(len(csd(255)), 2)

    def test_more_digits_cost_more(self):
        a = cost_op("MUL", {"n": 16, "const": 5}, P).typ  # 1 adder
        b = cost_op("MUL", {"n": 16, "const": 21}, P).typ  # 3 digits
        c = cost_op("MUL", {"n": 16, "const": 85}, P).typ  # 4 digits
        self.assertLess(a, b)
        self.assertLess(b, c)

    def test_incrementer(self):
        c = ge("ADD", n=32, const=1)
        self.assertAlmostEqual(c[0], 32 * 3.33, delta=0.01)  # n half adders
        self.assertAlmostEqual(c[1], 32 * 3.33 * 1.12, delta=0.01)  # 1 + alpha * 0.3
        self.assertEqual(ge("ADD", n=32, const=0), (0, 0, 0))
        self.assertEqual(ge("ADD", n=8, const=256), (0, 0, 0))  # wraps to zero
        # trailing zeros of the constant become wires
        self.assertAlmostEqual(ge("ADD", n=32, const=4)[0], 30 * 3.33, delta=0.01)

    def test_eq_with_constant_is_a_reduction(self):
        c = ge("EQ", n=32, const=0)
        red = ge("RED_OR", n=32)
        self.assertLessEqual(c[1], red[1] / 0.85 + 0.01)  # same tree as a reduction (no glue-logic scale)
        self.assertAlmostEqual(c[0], 31 * 0.55, delta=0.01)
        self.assertAlmostEqual(c[1], 31 * 0.75, delta=0.01)
        self.assertAlmostEqual(c[2], 31 * 1.33 * 1.15, delta=0.01)
        self.assertEqual(ge("EQ", n=32, const_unknown=True), c)

    def test_lt_with_constant(self):
        c = ge("LT", n=32, const=100)
        self.assertAlmostEqual(c[0], 32.0, delta=0.01)
        self.assertAlmostEqual(c[1], 32 * 1.33, delta=0.01)
        self.assertAlmostEqual(c[2], 64.0 * 1.15, delta=0.01)

    def test_mux_with_constant_inputs_becomes_and_or(self):
        c = cost_op("MUX", {"k": 2, "w": 8, "const_inputs": 1}, P)
        self.assertEqual(c.breakdown, {"AND2": 8})
        self.assertLess(c.typ, cost_op("MUX", {"k": 2, "w": 8}, P).typ)
        both = cost_op("MUX", {"k": 2, "w": 8, "const_inputs": 2}, P)
        self.assertLessEqual(both.typ, c.typ)
        self.assertEqual(cost_op("MUX", {"k": 2, "w": 8, "same_inputs": True}, P).typ, 0)

    def test_division_by_constants(self):
        self.assertEqual(ge("DIV", n=32, const=4), (0, 0, 0))  # unsigned / 2**k
        self.assertEqual(ge("MOD", n=32, const=8), (0, 0, 0))
        self.assertEqual(ge("DIV", n=32, const=1), (0, 0, 0))
        signed = cost_op("DIV", {"n": 32, "const": 4, "signed": True}, P)
        ripple = 31 * 5.33 + 3.33
        self.assertAlmostEqual(signed.typ / 1000, ripple, delta=0.01)
        self.assertEqual(signed.quality, "poor")
        other = cost_op("DIV", {"n": 32, "const": 10}, P)
        self.assertGreater(other.typ, 0)
        self.assertGreater(cost_op("MOD", {"n": 32, "const": 10}, P).typ, other.typ)

    def test_pow(self):
        self.assertEqual(ge("POW", n=8, exp=0), (0, 0, 0))
        self.assertEqual(ge("POW", n=8, exp=1), (0, 0, 0))
        sq = cost_op("MUL", {"n": 8, "m": 8, "square": True, "trunc": 8}, P)
        self.assertEqual(cost_op("POW", {"n": 8, "exp": 2}, P).typ, sq.typ)
        # e = 5: floor(log2 5) + popcount(5) - 1 = 3 multiplications
        p5 = cost_op("POW", {"n": 8, "exp": 5}, P)
        self.assertGreater(p5.typ, 2 * sq.typ)
        self.assertEqual(cost_op("POW", {"base": 2, "s": 4}, P).typ, cost_op("DECODE", {"s": 4}, P).typ)


class Widths(unittest.TestCase):
    def test_effective_width_is_the_callers_job_and_exact(self):
        # a 32-bit add truncated to 16 bits is ADD 16; zero-extended operands give ADD 17
        self.assertEqual(ge("ADD", n=16), ref_add(16))
        self.assertEqual(ge("ADD", n=17), ref_add(17))
        self.assertGreater(ge("ADD", n=17)[1], ge("ADD", n=16)[1])

    def test_carry_only_add_is_a_comparator(self):
        self.assertEqual(ge("ADD", n=32, carry_only=True), ge("LT", n=32))

    def test_multi_operand_and_mac(self):
        three = [cost_op("ADD", {"n": 32}, P).typ * 3, cost_op("SUM", {"k": 4, "w": 32}, P).typ]
        self.assertLess(three[1], three[0])  # a+b+c+d is cheaper than 3 ADDs in typ
        mac = cost_op("MAC", {"n": 16, "m": 16, "c": 32}, P)
        sep = cost_op("MUL", {"n": 16, "m": 16}, P).typ + cost_op("ADD", {"n": 32}, P).typ
        self.assertLess(mac.typ, sep)  # a*b + c is cheaper than MUL + ADD
        self.assertGreaterEqual(mac.max, mac.typ)


def ref_add(n):
    return tuple(round(v * 1000) / 1000 for v in ref.add(n))


class Bands(unittest.TestCase):
    def test_sanity_bands(self):
        self.assertTrue(170 <= ge("ADD", n=32)[1] <= 300)
        self.assertTrue(1200 <= ge("MUL", n=16, m=16)[1] <= 2000)
        self.assertTrue(4500 <= ge("MUL", n=32, m=32)[1] <= 8000)
        self.assertTrue(180 <= ge("DFF", n=32)[1] <= 260)
        self.assertTrue(200 <= ge("MUX", k=4, w=32)[1] <= 260)
        self.assertTrue(330 <= ge("SHL", n=32, s=5)[1] <= 420)
        self.assertTrue(330 <= ge("ROL", n=32, s=5)[1] <= 420)


if __name__ == "__main__":
    unittest.main()
