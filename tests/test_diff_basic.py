"""Zero-delta invariants, matching fixtures, reasons (DIFF.md section 12)."""
import unittest

from gatecost.diff import diff_trees
from gatecost.result import Ge3
from tests.test_diff_support import check_additive, flat, mod, op, tree


class ZeroDelta(unittest.TestCase):
    def assertNoChange(self, b, a, **kw):
        r = diff_trees(b, a, **kw)
        self.assertEqual(r.verdict, "NO_CHANGE")
        self.assertEqual((r.delta.min, r.delta.typ, r.delta.max), (0, 0, 0))
        self.assertEqual(r.all_contributors, ())
        self.assertEqual(r.confidence.level, "high")
        return r

    def base_ops(self, **kw):
        return [op("ADD", 1200, tgt="s", line=3, **kw), op("MUL", 5000, tgt="p", line=4, **kw), op("DFF", 800, tgt="q", line=6)]

    def test_identical(self):
        self.assertNoChange(flat(self.base_ops()), flat(self.base_ops()))

    def test_reformat_comment_file_move(self):
        a = flat([op("ADD", 1200, tgt="s", line=13, file="new/a.sv"), op("MUL", 5000, tgt="p", line=40, file="new/a.sv"),
                  op("DFF", 800, tgt="q", line=60, file="new/a.sv")], file="new/a.sv")
        self.assertNoChange(flat(self.base_ops()), a)

    def test_reorder(self):
        o = self.base_ops()
        self.assertNoChange(flat(o), flat(list(reversed(o))))

    def test_rename_signal_even_without_hash_shortcircuit(self):
        b = flat(self.base_ops())
        a = flat([op("ADD", 1200, tgt="sum_new", line=3, shape=b.modules["top"].ops[0].shape_key),
                  op("MUL", 5000, tgt="p", line=4), op("DFF", 800, tgt="q", line=6)], hash="")
        b2 = flat(self.base_ops(), hash="")
        r = self.assertNoChange(b2, a)
        self.assertEqual(r.by_module, ())

    def test_commuted_operands_no_hash(self):
        # shape key equal (commutative operands), named keys differ because of the role path
        b = flat([op("ADD", 1200, tgt="s", role="lhs", shape="X")], hash="")
        a = flat([op("ADD", 1200, tgt="s", role="rhs", shape="X")], hash="")
        self.assertNoChange(b, a)

    def test_module_rename(self):
        c1 = mod("alu", [op("ADD", 900, tgt="y")])
        c2 = mod("alu2", [op("ADD", 900, tgt="y")])
        top_b = mod("top", [op("AND", 100, tgt="o")], instances=[("u", "alu", 1)])
        top_a = mod("top", [op("AND", 100, tgt="o")], instances=[("u", "alu2", 1)])
        r = self.assertNoChange(tree([top_b, c1]), tree([top_a, c2]))
        self.assertIn("renamed", [s["kind"] for s in r.structural])

    def test_module_rename_hash_differs(self):
        c1 = mod("alu", [op("ADD", 900, tgt="y")], hash="h1")
        c2 = mod("alu2", [op("ADD", 900, tgt="y")], hash="h2")
        top_b = mod("top", [], instances=[("u", "alu", 1)], hash="t1")
        top_a = mod("top", [], instances=[("u", "alu2", 1)], hash="t2")
        r = diff_trees(tree([top_b, c1]), tree([top_a, c2]))
        # no pairing without equal hash: removal + addition that cancel exactly
        self.assertEqual(r.delta_scenarios.typ, 0)

    def test_param_default_overridden_everywhere(self):
        self.assertNoChange(flat(self.base_ops()), flat(self.base_ops()))


class Matching(unittest.TestCase):
    def test_add_adder(self):
        b = flat([op("MUL", 5000, tgt="p", line=4)])
        a = flat([op("MUL", 5000, tgt="p", line=4), op("ADD", 1200, tgt="s", line=5)])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        self.assertEqual(r.delta_scenarios.typ, 1200000)
        (c,) = r.all_contributors
        self.assertEqual((c.reason, c.line, c.op), ("added", 5, "ADD"))
        self.assertEqual(r.verdict, "INCREASE")
        self.assertEqual(r.by_category, {"addcmp": 1200000})

    def test_widen_bus(self):
        b = flat([op("ADD", 800, tgt="s", widths=(8, 8))])
        a = flat([op("ADD", 1200, tgt="s", widths=(12, 12))])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        (c,) = r.all_contributors
        self.assertEqual(c.reason, "width")
        self.assertEqual(c.delta.typ, 400000)
        self.assertIn("w8->w12", c.description)

    def test_change_constant(self):
        b = flat([op("MUL", 800, tgt="s", facts={"consts": {"k": 3}})])
        a = flat([op("MUL", 1400, tgt="s", facts={"consts": {"k": 255}}, shape="other")])
        r = diff_trees(b, a)
        (c,) = r.all_contributors
        self.assertEqual(c.reason, "constant")

    def test_case_branch(self):
        b = flat([op("PMUX", 800, tgt="y", facts={"struct": {"inputs": 3}})])
        a = flat([op("PMUX", 1000, tgt="y", facts={"struct": {"inputs": 4}})])
        r = diff_trees(b, a)
        self.assertEqual(r.all_contributors[0].reason, "structure")

    def test_pipeline_stage(self):
        b = flat([op("MUL", 5000, tgt="p", line=4)])
        a = flat([op("MUL", 5000, tgt="p", line=4), op("DFF", 6000, tgt="st", line=7, widths=(36,))])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        self.assertEqual(r.all_contributors[0].category, "seq")
        self.assertEqual(r.all_contributors[0].reason, "added")

    def test_wrap_in_new_module_moved_hint(self):
        b = tree([mod("top", [op("MUL", 5000, tgt="p", shape="M")])])
        w = mod("wrap", [op("MUL", 5000, tgt="p", shape="M")])
        top = mod("top", [], instances=[("u_w", "wrap", 1)])
        a = tree([top, w])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        self.assertEqual(r.delta_scenarios.typ, 0)
        self.assertIn("moved", [s["kind"] for s in r.structural])
        self.assertIn("module_added", [s["kind"] for s in r.structural])
        self.assertEqual(sorted(c.reason for c in r.all_contributors), ["added", "removed"])

    def test_split_module(self):
        b = tree([mod("top", [op("MUL", 5000, tgt="p", shape="M"), op("ADD", 900, tgt="s", shape="A")])])
        a = tree([mod("top", [op("ADD", 900, tgt="s", shape="A")], instances=[("u", "mulpart", 1)]),
                  mod("mulpart", [op("MUL", 5000, tgt="p", shape="M")])])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        self.assertEqual(r.delta_scenarios.typ, 0)
        self.assertEqual(len(r.all_contributors), 2)

    def test_leftover_line_alignment(self):
        src_b = "module top;\nassign y = a + b;\nassign z = c * d;\nendmodule\n"
        src_a = "module top;\n// new comment\nassign y = a + b + e;\nassign z = c * d;\nendmodule\n"
        b = flat([op("ADD", 800, tgt="y", line=2, shape="s1", role="x"), op("MUL", 5000, tgt="z", line=3, shape="s2")])
        a = flat([op("ADD", 1700, tgt="yy", line=3, shape="s1b", role="y", widths=(16, 16)),
                  op("MUL", 5000, tgt="z", line=4, shape="s2")], hash="")
        b = flat([op("ADD", 800, tgt="y", line=2, shape="s1", role="x"), op("MUL", 5000, tgt="z", line=3, shape="s2")], hash="")
        r = diff_trees(b, a, before_sources={"a.sv": src_b}, after_sources={"a.sv": src_a})
        check_additive(self, r, b, a)
        (c,) = r.all_contributors
        self.assertEqual((c.reason, c.line, c.delta.typ), ("width", 3, 900000))

    def test_leftover_without_alignment_is_add_remove(self):
        b = flat([op("ADD", 800, tgt="y", line=2, shape="s1")], hash="")
        a = flat([op("ADD", 800, tgt="yy", line=5, shape="s9")], hash="")
        r = diff_trees(b, a, before_sources={"a.sv": "keep1\nold\nkeep2\nkeep3\n"},
                       after_sources={"a.sv": "keep1\nkeep2\nkeep3\nnew\n"})
        self.assertEqual(sorted(c.reason for c in r.all_contributors), ["added", "removed"])
        rem = [c for c in r.all_contributors if c.reason == "removed"][0]
        self.assertEqual((rem.location_side, rem.file, rem.line), ("after", "a.sv", 1))  # translated to nearest survivor
        check_additive(self, r, b, a)


def exact(typ_ge: int) -> Ge3:
    """A cost without scenario spread, so that the delta range is ``typ -/+ 2 sigma`` whatever the range rule."""
    return Ge3(typ_ge * 1000, typ_ge * 1000, typ_ge * 1000)


class CorrelatedResidual(unittest.TestCase):
    """A matched operation resized: its formula error is correlated, sigma = rms * |delta typ| (not sqrt(sb^2+sa^2))."""

    def test_matched_resize_is_rms_times_delta(self):
        b = flat([op("ADD", 1000, tgt="s", widths=(8, 8), cost=exact(1000))])
        a = flat([op("ADD", 4000, tgt="s", widths=(32, 32), cost=exact(4000))])
        r = diff_trees(b, a)
        # rms 0.15 (good) * 3000 GE = 450 GE; the old independent rule gave 0.15*sqrt(1000^2+4000^2) = 618 GE
        self.assertEqual((r.delta.min, r.delta.typ, r.delta.max), (3000000 - 900000, 3000000, 3000000 + 900000))

    def test_pessimistic_quality_of_the_two_sides(self):
        b = flat([op("MEM", 1000, tgt="m", widths=(16, 8), quality="good", cost=exact(1000), sigma=150000)])
        a = flat([op("MEM", 5000, tgt="m", widths=(64, 8), quality="poor", cost=exact(5000), sigma=2500000)])
        r = diff_trees(b, a)
        # rms 0.5 (poor, the worse of good/poor) * 4000 GE = 2000 GE
        self.assertEqual((r.delta.min, r.delta.max), (4000000 - 4000000, 4000000 + 4000000))

    def test_shrink_is_symmetric(self):
        a = flat([op("ADD", 1000, tgt="s", widths=(8, 8), cost=exact(1000))])
        b = flat([op("ADD", 4000, tgt="s", widths=(32, 32), cost=exact(4000))])
        r = diff_trees(b, a)
        self.assertEqual((r.delta.min, r.delta.typ, r.delta.max), (-3000000 - 900000, -3000000, -3000000 + 900000))

    def test_unchanged_op_has_no_residual(self):
        ops = [op("ADD", 1000, tgt="s", cost=exact(1000)), op("MUL", 4000, tgt="p", cost=exact(4000))]
        r = diff_trees(flat(ops), flat(ops))
        self.assertEqual(r.verdict, "NO_CHANGE")

    def test_instance_count_change_is_correlated(self):
        ops = [op("ADD", 1000, tgt="s", cost=exact(1000))]
        top = lambda n: mod("top", [op("AND", 100, tgt="o", cost=exact(100))], instances=[("u", "alu", n)])
        b = tree([top(2), mod("alu", ops)])
        a = tree([top(3), mod("alu", ops)])
        r = diff_trees(b, a)
        # x2 -> x3 of the same op: sigma = 0.15 * 1000 GE * |3 - 2| = 150 GE (independent errors gave 0.15*sqrt(13) = 541)
        self.assertEqual((r.delta.min, r.delta.typ, r.delta.max), (1000000 - 300000, 1000000, 1000000 + 300000))


class MacUnfused(unittest.TestCase):
    """A fused multiply-accumulate that is split into MUL + ADD: only the part that changes carries a residual."""

    @staticmethod
    def mac(typ, widths=(24,)):
        return op("SUM", typ, tgt="acc", role="acc", widths=widths, quality="medium", cost=exact(typ), line=21,
                  sigma=round(typ * 1000 * 0.25), facts={"struct": {"k": 2, "mul": [8, 8]}}, tags=("fused",),
                  desc="MAC 8x8 +1")

    def test_mac_split_pairs_mul_and_adder(self):
        b = flat([self.mac(496)])
        a = flat([op("MUL", 368, tgt="p", role="p", widths=(8, 8), quality="medium", cost=exact(368), line=16,
                     sigma=round(368 * 1000 * 0.25)),
                  op("ADD", 134, tgt="acc", role="acc2", widths=(24,), cost=exact(134), line=21),
                  op("DFF", 128, tgt="q", role="q", widths=(16,), cost=exact(128), line=16)])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        self.assertEqual(r.delta.typ, 134000)
        by = {c.op: c for c in r.all_contributors}
        self.assertNotIn("ADD", by)                       # folded in the MUL pair, not an "added" item
        self.assertEqual(by["MUL"].reason, "fusion")
        self.assertEqual(by["MUL"].delta.typ, 6000)       # (368 + 134) - 496
        # residual: DFF 0.15*128 and MUL pair 0.25*6 in quadrature (the old rule: 0.25*496 + 0.25*368 + 0.15*134 ...)
        self.assertEqual(r.verdict, "INCREASE")
        self.assertAlmostEqual(r.delta.min, 134000 - 2 * 19260, delta=100)    # sqrt(19200^2 + 1500^2) = 19259
        self.assertAlmostEqual(r.delta.max, 134000 + 2 * 19260, delta=100)

    def test_mul_alone_is_paired_without_adder(self):
        b = flat([self.mac(496)])
        a = flat([op("MUL", 368, tgt="p", role="p", widths=(8, 8), quality="medium", cost=exact(368), line=16,
                     sigma=round(368 * 1000 * 0.25))])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        (c,) = r.all_contributors
        self.assertEqual((c.reason, c.delta.typ), ("fusion", -128000))
        # rms 0.25 * 128 GE, not sqrt(124^2 + 92^2)
        self.assertEqual((r.delta.min, r.delta.max), (-128000 - 64000, -128000 + 64000))

    def test_plain_sum_is_not_paired_with_a_mul(self):
        sm = op("SUM", 300, tgt="acc", role="acc", widths=(24,), cost=exact(300), line=21, facts={"struct": {"k": 3}})
        r = diff_trees(flat([sm]), flat([op("MUL", 368, tgt="p", role="p", widths=(8, 8), cost=exact(368), line=16)]))
        self.assertEqual({c.reason for c in r.all_contributors}, {"added", "removed"})


if __name__ == "__main__":
    unittest.main()
