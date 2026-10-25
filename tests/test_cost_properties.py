"""Randomized and exhaustive properties of the cost model (COST_MODEL.md section 7)."""
import math
import random
import unittest

from gatecost.cost import (
    OPS, cost_op, confidence, delta, delta_range, dump_profile, load_profile, range_ratio, residual_sigma,
    shared_leaf_cost, total,
)
from gatecost.result import Ge3

P = load_profile()
W = range(1, 129)


def c3(op, **a):
    c = cost_op(op, a, P)
    return c.min, c.typ, c.max


def random_attrs(rng):
    """A random valid (op, attrs) over widths 1..128."""
    w = lambda: rng.randint(1, 128)  # noqa: E731
    kind = rng.choice(OPS)
    if kind in ("ADD", "SUB", "NEG", "AND", "OR", "XOR", "NAND", "NOR", "XNOR", "NOT", "EQ", "NE", "LT", "LE", "GT", "GE",
                "RED_AND", "RED_OR", "RED_XOR", "LATCH", "DLAT"):
        a = {"n": w()}
        if kind in ("ADD", "SUB", "EQ", "LT") and rng.random() < 0.4:
            a["const"] = rng.randint(-300, 300)
        if kind in ("LT", "LE", "GT", "GE"):
            a["signed"] = rng.random() < 0.5
    elif kind == "MUL":
        a = {"n": w(), "m": w(), "signed": rng.random() < 0.3}
        if rng.random() < 0.3:
            a = {"n": w(), "const": rng.randint(-3000, 3000)}
    elif kind == "MAC":
        a = {"n": w(), "m": w()}
    elif kind in ("DIV", "MOD"):
        a = {"n": w(), "m": w()} if rng.random() < 0.7 else {"n": w(), "const": rng.randint(1, 5000), "signed": rng.random() < 0.5}
    elif kind == "POW":
        a = {"n": w(), "exp": rng.randint(0, 40)}
    elif kind in ("SHL", "SHR", "SAR", "ROL", "ROR"):
        a = {"n": w(), "s": rng.randint(0, 10)}
    elif kind in ("MUX", "PMUX", "PRIORITY"):
        a = {"k": rng.randint(1, 20), "w": w()}
    elif kind == "SUM":
        a = {"k": rng.randint(1, 12), "w": w()}
    elif kind == "DECODE":
        a = {"s": rng.randint(0, 10)}
    elif kind == "ENCODE":
        a = {"k": rng.randint(1, 64), "priority": rng.random() < 0.5}
    elif kind in ("DFF", "REG"):
        a = {"n": w(), "async_reset": rng.random() < 0.3, "sync_reset": rng.random() < 0.3,
             "enable": rng.random() < 0.5, "scan": rng.random() < 0.3}
    elif kind == "MEM":
        a = {"depth": rng.randint(1, 512), "width": w(), "rports": rng.randint(1, 3), "wports": rng.randint(1, 2),
             "sync_read": rng.random() < 0.5}
    elif kind == "ROM":
        a = {"depth": rng.randint(1, 512), "width": rng.randint(1, 32)}
    else:
        a = {"name": "x"}
    return kind, a


class Ordering(unittest.TestCase):
    def test_min_typ_max_ordered_for_every_operation(self):
        rng = random.Random(20240601)
        for _ in range(3000):
            op, a = random_attrs(rng)
            c = cost_op(op, a, P)
            with self.subTest(op=op, a=a):
                self.assertLessEqual(0, c.min)
                self.assertLessEqual(c.min, c.typ)
                self.assertLessEqual(c.typ, c.max)
                self.assertIn(c.quality, ("good", "medium", "poor"))
                self.assertIn(c.category, ("seq", "addcmp", "mul", "select", "logic", "shift", "div", "mem"))

    def test_other_profiles_keep_the_ordering(self):
        for ov in ({"policy": {"timing": "relaxed"}}, {"policy": {"timing": "aggressive", "enable_style": "edff", "scan": True}},
                   {"primitives": {"FA": 7.0, "XOR2": 2.67, "DFF": 4.5}}, {"policy": {"overhead": {"typ": 1.1, "max": 1.15}}}):
            p = load_profile("generic-asic", ov)
            rng = random.Random(7)
            for _ in range(600):
                op, a = random_attrs(rng)
                c = cost_op(op, a, p)
                self.assertTrue(c.min <= c.typ <= c.max, (ov, op, a, c))


class Monotonic(unittest.TestCase):
    def assertMonotone(self, fn, rng=W, label=""):
        prev = None
        for x in rng:
            cur = fn(x)
            if prev is not None:
                for s, (a, b) in zip(("min", "typ", "max"), zip(prev[1], cur)):
                    self.assertLessEqual(a, b, f"{label} {s} decreases from {prev[0]} to {x}: {prev[1]} -> {cur}")
            prev = (x, cur)

    def test_single_width_operations(self):
        for op in ("ADD", "SUB", "NEG", "AND", "OR", "XOR", "NOT", "EQ", "NE", "LT", "LE", "RED_AND", "RED_XOR", "LATCH", "DFF"):
            self.assertMonotone(lambda n, op=op: c3(op, n=n), label=op)
        self.assertMonotone(lambda n: c3("LT", n=n, signed=True), label="LT signed")
        self.assertMonotone(lambda n: c3("EQ", n=n, const=0), label="EQ const")
        self.assertMonotone(lambda n: c3("LT", n=n, const=5), label="LT const")
        self.assertMonotone(lambda n: c3("ADD", n=n, const=1), label="ADD const 1")
        self.assertMonotone(lambda n: c3("ADD", n=n, const_unknown=True), label="ADD const")
        self.assertMonotone(lambda n: c3("DFF", n=n, enable=True, sync_reset=True), label="DFF enable")
        self.assertMonotone(lambda n: c3("DFF", n=n, async_reset=True, enable=True, scan=True), label="DFF en scan")

    def test_two_width_operations(self):
        for op in ("MUL", "DIV", "MOD"):
            for fixed in (1, 2, 3, 7, 12, 16, 33, 64):
                self.assertMonotone(lambda n: c3(op, n=n, m=fixed), label=f"{op} n (m={fixed})")
                self.assertMonotone(lambda m: c3(op, n=fixed, m=m), label=f"{op} m (n={fixed})")
        for fixed in (1, 5, 16):
            self.assertMonotone(lambda n: c3("MUL", n=n, m=fixed, signed=True), label="MUL signed")
            self.assertMonotone(lambda t: c3("MUL", n=24, m=16, trunc=t), range(1, 41), label="MUL trunc")
            self.assertMonotone(lambda n: c3("MAC", n=n, m=fixed), label="MAC")

    def test_selection_and_decoding(self):
        for k in (1, 2, 3, 4, 5, 8, 17):
            for op in ("MUX", "PMUX", "PRIORITY"):
                self.assertMonotone(lambda w: c3(op, k=k, w=w), label=f"{op} w (k={k})")
        for w in (1, 7, 32):
            for op in ("MUX", "PMUX", "PRIORITY"):
                self.assertMonotone(lambda k: c3(op, k=k, w=w), range(1, 65), label=f"{op} k (w={w})")
        self.assertMonotone(lambda s: c3("DECODE", s=s), range(0, 14), label="DECODE")
        self.assertMonotone(lambda k: c3("ENCODE", k=k), range(1, 129), label="ENCODE")
        self.assertMonotone(lambda k: c3("SUM", k=k, w=16), range(1, 40), label="SUM k")
        self.assertMonotone(lambda w: c3("SUM", k=5, w=w), label="SUM w")

    def test_shifts(self):
        for op in ("SHL", "SHR", "SAR", "ROL", "ROR"):
            for s in (0, 1, 3, 5, 8):
                self.assertMonotone(lambda n: c3(op, n=n, s=s), label=f"{op} n (s={s})")
            for n in (1, 5, 32, 100):
                self.assertMonotone(lambda s: c3(op, n=n, s=s), range(0, 13), label=f"{op} s (n={n})")

    def test_flop_memories_and_rom(self):
        for d in (1, 2, 16):
            self.assertMonotone(lambda w: c3("MEM", depth=d, width=w), label="MEM width")
        self.assertMonotone(lambda d: c3("MEM", depth=d, width=8), range(1, 300), label="MEM depth")
        self.assertMonotone(lambda d: c3("ROM", depth=d, width=8), range(1, 300), label="ROM depth")
        self.assertMonotone(lambda w: c3("ROM", depth=64, width=w), label="ROM width")

    def test_constant_operations(self):
        self.assertMonotone(lambda n: c3("MUL", n=n, const=10), label="MUL const 10")
        self.assertMonotone(lambda n: c3("MUL", n=n, const=-7), label="MUL const -7")
        self.assertMonotone(lambda n: c3("MUL", n=n, const_unknown=True), label="MUL const")
        self.assertMonotone(lambda n: c3("POW", n=n, exp=5), label="POW")
        self.assertMonotone(lambda n: c3("DIV", n=n, const=10), label="DIV const")


class Relations(unittest.TestCase):
    def test_mul_is_symmetric(self):
        rng = random.Random(5)
        for _ in range(300):
            n, m = rng.randint(1, 128), rng.randint(1, 128)
            self.assertEqual(c3("MUL", n=n, m=m), c3("MUL", n=m, m=n))
            self.assertEqual(c3("MUL", n=n, m=m, signed=True), c3("MUL", n=m, m=n, signed=True))

    def test_constant_operand_never_costs_more_than_variable(self):
        rng = random.Random(11)
        for _ in range(800):
            n = rng.randint(1, 128)
            k = rng.randint(-5000, 5000)
            pairs = [
                (c3("ADD", n=n, const=k), c3("ADD", n=n)), (c3("SUB", n=n, const=k), c3("SUB", n=n)),
                (c3("EQ", n=n, const=k), c3("EQ", n=n)), (c3("LT", n=n, const=k), c3("LT", n=n)),
                (c3("LT", n=n, const=k, signed=True), c3("LT", n=n, signed=True)),
                (c3("MUL", n=n, const=k), c3("MUL", n=n, m=max(1, abs(k).bit_length()))),
                (c3("MUL", n=n, const_unknown=True), c3("MUL", n=n, m=n)),
                (c3("ADD", n=n, const_unknown=True), c3("ADD", n=n)),
            ]
            for const, var in pairs:
                for a, b in zip(const, var):
                    self.assertLessEqual(a, b, (n, k, const, var))

    def test_scaling_every_primitive_scales_the_total(self):
        rng = random.Random(3)
        for factor in (2, 3):
            q = P.scaled(factor)
            for _ in range(400):
                op, a = random_attrs(rng)
                if op == "ROM" or op == "MEM":
                    a = {**a, "depth": min(a["depth"], 64)}
                base, sc = cost_op(op, a, P), cost_op(op, a, q)
                for x, y in zip((base.min, base.typ, base.max), (sc.min, sc.typ, sc.max)):
                    self.assertAlmostEqual(y, x * factor, delta=max(40, 2e-4 * y), msg=f"{op} {a} x{factor}")

    def test_determinism(self):
        rng = random.Random(9)
        items = [random_attrs(rng) for _ in range(300)]
        first = [(c.min, c.typ, c.max, c.arch, c.breakdown, c.category, c.quality) for c in (cost_op(o, a, P) for o, a in items)]
        q = load_profile()
        second = [(c.min, c.typ, c.max, c.arch, c.breakdown, c.category, c.quality) for c in (cost_op(o, a, q) for o, a in items)]
        self.assertEqual(first, second)

    def test_extends_without_overrides_is_identical(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "child.toml"
            f.write_text('extends = "generic-asic"\n[meta]\nname = "child"\n', encoding="utf-8")
            child = load_profile(str(f))
        self.assertEqual(dump_profile(child).replace('name = "child"', 'name = "generic-asic"'), dump_profile(P))
        rng = random.Random(1)
        for _ in range(300):
            op, a = random_attrs(rng)
            x, y = cost_op(op, a, P), cost_op(op, a, child)
            self.assertEqual((x.min, x.typ, x.max, x.breakdown, x.arch), (y.min, y.typ, y.max, y.breakdown, y.arch))

    def test_timing_policy_moves_typ_between_min_and_mid(self):
        relaxed = load_profile("generic-asic", {"policy": {"timing": "relaxed"}})
        aggressive = load_profile("generic-asic", {"policy": {"timing": "aggressive"}})
        a, r, g = cost_op("ADD", {"n": 32}, P), cost_op("ADD", {"n": 32}, relaxed), cost_op("ADD", {"n": 32}, aggressive)
        self.assertEqual(r.typ, r.min)
        self.assertLess(r.typ, a.typ)
        self.assertLess(a.typ, g.typ)
        self.assertEqual(a.min, g.min)


class Deltas(unittest.TestCase):
    def ops(self, rng, n=12):
        return [cost_op(*random_attrs(rng), P) for _ in range(n)]

    def test_identical_states_give_zero_delta_and_range(self):
        rng = random.Random(4)
        ops = self.ops(rng)
        d = delta(total(ops), total(ops))
        self.assertEqual(d, Ge3(0, 0, 0))
        self.assertEqual(delta_range(d, residual_sigma([], P)), Ge3(0, 0, 0))

    def test_unchanged_operations_cancel_exactly(self):
        rng = random.Random(8)
        common, before, after = self.ops(rng), self.ops(rng, 3), self.ops(rng, 4)
        d_all = delta(total(common + after), total(common + before))
        d_changed = delta(total(after), total(before))
        self.assertEqual(d_all, d_changed)

    def test_delta_is_scenario_wise_not_interval_subtraction(self):
        before = cost_op("MUL", {"n": 16, "m": 16}, P)
        after = cost_op("MUL", {"n": 17, "m": 16}, P)
        d = delta(total([after]), total([before]))
        self.assertEqual((d.min, d.typ, d.max), (after.min - before.min, after.typ - before.typ, after.max - before.max))
        self.assertLess(d.max, after.max - before.min)  # interval subtraction would be wider

    def test_range_orders_and_is_widened_by_the_residual(self):
        rng = random.Random(21)
        for _ in range(200):
            b, a = self.ops(rng, 3), self.ops(rng, 3)
            d = delta(total(a), total(b))
            sig = residual_sigma([(o.typ, o.quality) for o in a + b], P)
            r = delta_range(d, sig)
            self.assertLessEqual(r.min, r.typ)
            self.assertLessEqual(r.typ, r.max)
            # COST_MODEL.md 5.1: centre of the scenarios -/+ sqrt(half-spread^2 + (2 sigma)^2)
            lo, hi = min(d.min, d.typ, d.max), max(d.min, d.typ, d.max)
            half = math.sqrt(((hi - lo) / 2) ** 2 + (2 * sig) ** 2)
            self.assertLessEqual(abs(r.min - ((lo + hi) / 2 - half)), 1)  # integer rounding, outwards
            self.assertLessEqual(abs(r.max - ((lo + hi) / 2 + half)), 1)
            self.assertLessEqual(r.min, lo)  # always contains every scenario
            self.assertGreaterEqual(r.max, hi)
            self.assertGreaterEqual(r.min, lo - 2 * sig)  # never wider than the former linear sum
            self.assertLessEqual(r.max, hi + 2 * sig)
            self.assertEqual(delta_range(d, 0), Ge3(lo, d.typ, hi))
            self.assertEqual(delta_range(d, 0).typ, d.typ)

    def test_range_is_a_quadrature_sum(self):
        # regression (confidence review): the scenario spread and the 2-sigma residual used to add linearly
        self.assertEqual(delta_range(Ge3(100_000, 100_000, 100_000), 5_000), Ge3(90_000, 100_000, 110_000))
        self.assertEqual(delta_range(Ge3(0, 30_000, 60_000), 0), Ge3(0, 30_000, 60_000))
        # half-spread 30, 2 sigma 40 -> half-width 50 around the centre 30 (linear: 70)
        self.assertEqual(delta_range(Ge3(0, 30_000, 60_000), 20_000), Ge3(-20_000, 30_000, 80_000))
        # asymmetric scenarios: the range is centred on the scenarios, not on typ, and still contains them
        r = delta_range(Ge3(232_080, 313_430, 468_924), 49_715)
        self.assertLessEqual(r.min, 232_080)
        self.assertGreaterEqual(r.max, 468_924)
        self.assertLess(range_ratio(r), 0.5)  # counter_width example: r 0.69 -> 0.49 (medium)
        self.assertEqual(confidence(r, [("seq", "good", 223_920), ("addcmp", "good", 89_510)]).level, "medium")
        # odd sums round outwards
        self.assertEqual(delta_range(Ge3(0, 0, 1), 0), Ge3(0, 0, 1))
        self.assertEqual(delta_range(Ge3(-3, 0, 0), 1), Ge3(-4, 0, 1))  # width ceil(sqrt(9 + 16)) = 5

    def test_residual_uses_quality_classes(self):
        self.assertEqual(residual_sigma([(100_000, "good")], P), 15_000)
        self.assertEqual(residual_sigma([(100_000, "medium")], P), 25_000)
        self.assertEqual(residual_sigma([(100_000, "poor")], P), 50_000)
        self.assertEqual(residual_sigma([(30_000, "good"), (40_000, "good")], P), 7_500)  # sqrt(4.5^2 + 6^2) = 7.5

    def test_confidence_levels(self):
        tight = Ge3(95_000, 100_000, 110_000)  # r = 0.075
        good = [("addcmp", "good", 100_000)]
        self.assertEqual(confidence(tight, good).level, "high")
        wide = Ge3(60_000, 100_000, 140_000)  # r = 0.4
        self.assertEqual(confidence(wide, good).level, "medium")
        self.assertEqual(confidence(Ge3(0, 100_000, 400_000), good).level, "low")
        self.assertEqual(confidence(tight, good, unsupported=1).level, "low")
        self.assertEqual(confidence(tight, good, flags=["macro_no_table"]).level, "low")
        self.assertEqual(confidence(tight, good, flags=["unknown_blackbox"]).level, "low")
        mixed = [("addcmp", "good", 60_000), ("div", "poor", 40_000)]
        c = confidence(tight, mixed)
        self.assertEqual(c.level, "low")
        self.assertIn("40% of delta in div class (poor model quality)", c.reasons)
        medq = [("mul", "medium", 100_000)]
        self.assertEqual(confidence(tight, medq).level, "medium")  # < 90% good: not high
        self.assertAlmostEqual(range_ratio(Ge3(0, 10_000, 40_000)), 40_000 / 40_000)  # floor of 20 GE
        self.assertAlmostEqual(confidence(wide, good).range_ratio, 0.4)
        self.assertIn("delta range is +/-40% of the estimate", confidence(wide, good).reasons)

    def test_asymmetric_range_reason(self):
        # regression: an asymmetric range is reported as -x%/+y%, the level still follows r
        skew = Ge3(70_000, 100_000, 160_000)  # r = 0.45: -30% / +60%
        c = confidence(skew, [("addcmp", "good", 100_000)])
        self.assertEqual(c.level, "medium")
        self.assertEqual(c.reasons, ("delta range is -30%/+60% of the estimate",))
        self.assertAlmostEqual(c.range_ratio, 0.45)
        dec = confidence(Ge3(-160_000, -100_000, -70_000), [("addcmp", "good", 100_000)])
        self.assertEqual(dec.reasons, ("delta range is -60%/+30% of the estimate",))
        self.assertEqual(confidence(Ge3(0, 100_000, 400_000), [("addcmp", "good", 100_000)]).level, "low")

    def test_sharing_a_case_of_four_multipliers(self):
        m = cost_op("MUL", {"n": 16, "m": 16}, P)
        shared = shared_leaf_cost([m] * 4, 16, 16, 32, P)
        two = cost_op("MUL", {"n": 16, "m": 16}, P).scaled(2)
        self.assertLess(shared.typ, two.typ)  # a case of 4 MULs costs less than 2 MULs
        self.assertEqual(shared.max, 4 * m.max)  # max is never shared
        self.assertLessEqual(shared.min, shared.typ)
        off = load_profile("generic-asic", {"policy": {"resource_sharing": "off"}})
        self.assertEqual(shared_leaf_cost([m] * 4, 16, 16, 32, off).typ, 4 * m.typ)
        small = cost_op("ADD", {"n": 4}, P)  # sharing never makes a cheap operation dearer
        self.assertLessEqual(shared_leaf_cost([small] * 3, 4, 4, 5, P).typ, 3 * small.typ)


if __name__ == "__main__":
    unittest.main()
