"""Multiplicity, causes, reasons, sharing, line view, verdicts, confidence (DIFF.md section 12)."""
import time
import unittest

from gatecost.diff import diff_trees
from gatecost.result import Diagnostic, Ge3
from tests.test_diff_support import check_additive, flat, mod, op, tree

G = 1000


class Multiplicity(unittest.TestCase):
    def sub(self, ops):
        return mod("alu", ops)

    def top(self, n, child="alu"):
        return mod("top", [op("AND", 100, tgt="o")], instances=[("u", child, n)])

    def test_count_only(self):
        ops = [op("ADD", 1000, tgt="s"), op("MUL", 4000, tgt="p")]
        cb = sum(o["cost"].typ for o in ops)
        b, a = tree([self.top(2), self.sub(ops)]), tree([self.top(3), self.sub(ops)])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        self.assertEqual(r.delta_scenarios.typ, cb)
        (m,) = r.by_module
        self.assertEqual((m["module"], m["instances_before"], m["instances_after"]), ("alu", 2, 3))
        self.assertEqual((m["intrinsic"]["typ"], m["multiplicity"]["typ"]), (0, cb))
        (c,) = r.all_contributors
        self.assertEqual(c.reason, "multiplicity")
        self.assertEqual(c.delta.typ, cb)  # valued at the previous per-instance cost
        self.assertIn("alu", "".join(r.modules_verbose))

    def test_intrinsic_only(self):
        b = tree([self.top(2), self.sub([op("ADD", 1000, tgt="s")])])
        a = tree([self.top(2), self.sub([op("ADD", 1000, tgt="s"), op("MUL", 4000, tgt="p", line=5)])])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        (m,) = r.by_module
        self.assertEqual(m["multiplicity"]["typ"], 0)
        self.assertEqual(m["intrinsic"]["typ"], 2 * 4000 * G)
        (c,) = r.all_contributors
        self.assertEqual((c.instances, c.per_instance.typ, c.delta.typ), (2, 4000 * G, 8000 * G))

    def test_both(self):
        cb = 1000 * G
        b = tree([self.top(2), self.sub([op("ADD", 1000, tgt="s")])])
        a = tree([self.top(3), self.sub([op("ADD", 1500, tgt="s")])])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        (m,) = r.by_module
        self.assertEqual(m["intrinsic"]["typ"], 500 * G * 3)
        self.assertEqual(m["multiplicity"]["typ"], cb * 1)
        self.assertEqual(r.delta_scenarios.typ, 3 * 1500 * G - 2 * 1000 * G)
        self.assertEqual({c.reason for c in r.all_contributors}, {"structure", "multiplicity"})

    def test_instance_array_size(self):
        b = tree([self.top(4), self.sub([op("ADD", 700, tgt="s")])])
        a = tree([self.top(8), self.sub([op("ADD", 700, tgt="s")])])
        r = diff_trees(b, a)
        self.assertEqual(r.delta_scenarios.typ, 4 * 700 * G)
        self.assertIn("instance_count", [s["kind"] for s in r.structural])

    def test_several_parameter_sets(self):
        def s(w, extra=0):
            ops = [op("ADD", 100 * w, tgt="s")] + ([op("MUL", extra, tgt="p", line=9)] if extra else [])
            return mod("alu", ops, params=(("W", w),))
        top_b = mod("top", [], instances=[("u0", "alu{W=8}", 1), ("u1", "alu{W=16}", 2)])
        top_a = mod("top", [], instances=[("u0", "alu{W=8}", 2), ("u1", "alu{W=16}", 1)])
        b = tree([top_b, s(8), s(16)])
        a = tree([top_a, s(8), s(16, 300)])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        by = {tuple(m["parameters"].items()): m for m in r.by_module}
        m8, m16 = by[(("W", 8),)], by[(("W", 16),)]
        self.assertEqual((m8["intrinsic"]["typ"], m8["multiplicity"]["typ"]), (0, 800 * G))
        self.assertEqual((m16["intrinsic"]["typ"], m16["multiplicity"]["typ"]), (300 * G, -1600 * G))

    def test_instance_moves_between_parameter_sets(self):
        def s(w):
            return mod("alu", [op("ADD", 100 * w, tgt="s", facts={"params": {"W": w}}, shape=f"add{w}")], params=(("W", w),))
        b = tree([mod("top", [], instances=[("u", "alu{W=8}", 1)]), s(8)])
        a = tree([mod("top", [], instances=[("u", "alu{W=16}", 1)]), s(16)])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        (c,) = r.all_contributors
        self.assertEqual((c.reason, c.delta.typ), ("parameter", 800 * G))
        self.assertIn("parameter_change", [s_["kind"] for s_ in r.structural])
        self.assertEqual(len(r.by_module), 1)  # matched pair, not an addition and a removal


class Causes(unittest.TestCase):
    SRC_B = "package cfg;\nlocalparam W = 16;\nendpackage\n"
    SRC_A = "package cfg;\nlocalparam W = 32;\nendpackage\n"

    def mk(self, w, n=5, origin=True):
        ops = [op("ADD", 100 * w // 16, tgt=f"s{i}", line=10 + i, file="alu.sv", shape=f"s{i}", widths=(w, w),
                  facts={"params": {"W": w}, "param_origins": {"W": ("config.sv", 2)} if origin else {}}) for i in range(n)]
        return flat(ops, name="alu", file="alu.sv")

    def test_parameter_grouped(self):
        b, a = self.mk(16), self.mk(32)
        r = diff_trees(b, a, before_sources={"config.sv": self.SRC_B}, after_sources={"config.sv": self.SRC_A})
        check_additive(self, r, b, a)
        (c,) = r.all_contributors
        self.assertEqual(c.reason, "parameter")
        self.assertEqual((c.file, c.line), ("config.sv", 2))
        self.assertEqual(c.cause, {"kind": "parameter", "name": "W", "file": "config.sv", "line": 2, "from": "16", "to": "32"})
        self.assertEqual(len(c.children), 5)
        self.assertEqual(c.description, "W 16->32 (5 ops)")
        self.assertEqual(c.delta.typ, 5 * 100 * G)
        self.assertEqual(r.by_cause[0]["cause"]["name"], "W")
        self.assertEqual(len(r.by_line), 5)  # line view is per operation

    def test_origin_unchanged_no_group(self):
        same = self.SRC_B
        b, a = self.mk(16), self.mk(32)
        r = diff_trees(b, a, before_sources={"config.sv": same}, after_sources={"config.sv": same})
        self.assertEqual(len(r.all_contributors), 5)
        self.assertTrue(all(c.cause is None and c.reason == "parameter" for c in r.all_contributors))

    def test_no_origin_no_group(self):
        r = diff_trees(self.mk(16, origin=False), self.mk(32, origin=False))
        self.assertEqual(len(r.all_contributors), 5)

    def test_constant_cause(self):
        b = flat([op("MUL", 1000, tgt="p", shape="m1", facts={"consts": {"K": 3}, "const_origins": {"K": ("k.sv", 1)}})])
        a = flat([op("MUL", 2000, tgt="p", shape="m2", facts={"consts": {"K": 7}, "const_origins": {"K": ("k.sv", 1)}})])
        r = diff_trees(b, a, before_sources={"k.sv": "K = 3\n"}, after_sources={"k.sv": "K = 7\n"})
        (c,) = r.all_contributors
        self.assertEqual((c.reason, c.cause["kind"], c.cause["from"], c.cause["to"]), ("constant", "constant", "3", "7"))

    def test_define_toggle_via_config(self):
        b = flat([op("ADD", 100, tgt="a")], config={"defines": {}})
        a = flat([op("ADD", 100, tgt="a"),
                  op("MUL", 5000, tgt="m", line=7, facts={"defines": {"FAST": "1"}, "define_origins": {"FAST": ("a.sv", 6)}})],
                 config={"defines": {"FAST": "1"}})
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        (c,) = r.all_contributors
        self.assertEqual(c.cause["kind"], "define")
        self.assertEqual((c.cause["name"], c.cause["from"], c.cause["to"]), ("FAST", "undefined", "1"))
        self.assertTrue(any(d.code == "CONFIG_MISMATCH" for d in r.diagnostics_new))
        self.assertEqual(r.confidence.level, "low")

    def test_define_toggle_via_source(self):
        src_b = "`define FAST\nx\n`ifdef FAST\nmul\n`endif\n"
        src_a = "x\n`ifdef FAST\nmul\n`endif\n"
        b = flat([op("MUL", 5000, tgt="m", line=4, facts={"defines": {"FAST": "1"}, "define_origins": {"FAST": ("a.sv", 1)}})], hash="")
        a = flat([op("ADD", 100, tgt="z", line=1)], hash="")
        r = diff_trees(b, a, before_sources={"a.sv": src_b}, after_sources={"a.sv": src_a})
        rem = [c for c in r.all_contributors if c.cause and c.cause["kind"] == "define"]
        self.assertEqual(len(rem), 1)
        self.assertEqual(rem[0].cause["name"], "FAST")


class Reasons(unittest.TestCase):
    def one(self, ob, oa):
        b, a = flat([ob]), flat([oa])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        (c,) = r.all_contributors
        return c.reason

    def test_each(self):
        self.assertEqual(self.one(op("ADD", 1, tgt="t", facts={"params": {"W": 1}}), op("ADD", 2, tgt="t", facts={"params": {"W": 2}})), "parameter")
        self.assertEqual(self.one(op("ADD", 1, tgt="t", facts={"consts": {"k": 1}}), op("ADD", 2, tgt="t", facts={"consts": {"k": 2}})), "constant")
        self.assertEqual(self.one(op("ADD", 1, tgt="t", widths=(4, 4)), op("ADD", 2, tgt="t", widths=(8, 8))), "width")
        self.assertEqual(self.one(op("PMUX", 1, tgt="t", facts={"struct": 3}), op("PMUX", 2, tgt="t", facts={"struct": 4})), "structure")
        self.assertEqual(self.one(op("MUL", 1, tgt="t", arch={"typ": "array"}), op("MUL", 2, tgt="t", arch={"typ": "booth"})), "architecture")
        self.assertEqual(self.one(op("MUL", 1, tgt="t", tags=("shared",)), op("MUL", 2, tgt="t")), "sharing")
        self.assertEqual(self.one(op("ADD", 1, tgt="t", tags=("fused",)), op("ADD", 2, tgt="t")), "fusion")
        self.assertEqual(self.one(op("DFF", 1, tgt="t", facts={"reg": {"reset": "none"}}), op("DFF", 2, tgt="t", facts={"reg": {"reset": "async"}})), "register")
        self.assertEqual(self.one(op("BLACKBOX", 1, tgt="t"), op("BLACKBOX", 2, tgt="t")), "blackbox")

    def test_added_removed(self):
        e = flat([op("AND", 1, tgt="z")])
        r = diff_trees(e, flat([op("AND", 1, tgt="z"), op("ADD", 3, tgt="n", line=9)]))
        self.assertEqual(r.all_contributors[0].reason, "added")
        r = diff_trees(flat([op("AND", 1, tgt="z"), op("ADD", 3, tgt="n", line=9)]), e)
        self.assertEqual(r.all_contributors[0].reason, "removed")

    def test_priority_order(self):
        # parameter wins over width and constant
        ob = op("ADD", 1, tgt="t", widths=(4, 4), facts={"params": {"W": 4}, "consts": {"k": 1}})
        oa = op("ADD", 2, tgt="t", widths=(8, 8), facts={"params": {"W": 8}, "consts": {"k": 2}})
        self.assertEqual(self.one(ob, oa), "parameter")


class Sharing(unittest.TestCase):
    def test_multiplier_stops_being_shared(self):
        b = flat([op("MUL", 5000, tgt="m1", line=3, tags=("shared",), members=(1,)),
                  op("MUL", 0, tgt="m2", line=4, tags=("shared",), cost=Ge3(0, 0, 0)), op("ADD", 800, tgt="a", line=5)])
        a = flat([op("MUL", 5000, tgt="m1", line=3), op("MUL", 5000, tgt="m2", line=4), op("ADD", 800, tgt="a", line=5)])
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        (c,) = r.all_contributors
        self.assertEqual((c.reason, c.line), ("sharing", 4))
        self.assertGreater(c.delta.typ, 0)
        self.assertIn("shared", c.description)

    def test_fusion_cluster(self):
        b = flat([op("ADD", 900, tgt="x", tags=("fused",), members=(2, 3))])
        a = flat([op("ADD", 1500, tgt="x", tags=("fused",), members=(2, 3, 4))])
        (c,) = diff_trees(b, a).all_contributors
        self.assertEqual(c.reason, "fusion")


class LineView(unittest.TestCase):
    def test_macro_charged_to_use_site(self):
        o = op("ADD", 900, tgt="m", line=40, file="top.sv", def_file="defs.vh", def_line=3, tags=("macro",))
        b, a = flat([op("AND", 10, tgt="z")]), flat([op("AND", 10, tgt="z"), o])
        r = diff_trees(b, a)
        (e,) = r.by_line
        self.assertEqual((e["file"], e["line"]), ("top.sv", 40))
        self.assertNotIn("defs.vh", [x["file"] for x in r.by_line])

    def test_removed_keeps_before_location_without_alignment(self):
        b = flat([op("AND", 10, tgt="z"), op("MUL", 900, tgt="m", line=12)])
        a = flat([op("AND", 10, tgt="z")])
        r = diff_trees(b, a)
        (e,) = r.by_line
        self.assertEqual((e["line"], e["location_side"]), (12, "before"))
        (c,) = r.all_contributors
        self.assertEqual(c.location_side, "before")

    def test_line_view_after_coordinates(self):
        sb, sa = "a\nb\nc\nd\n", "a\nNEW\nb\nc\nd\n"
        b = flat([op("ADD", 5, tgt="s", line=4, shape="x")], hash="")
        a = flat([op("ADD", 9, tgt="q", line=5, shape="y", widths=(16, 16))], hash="")
        r = diff_trees(b, a, before_sources={"a.sv": sb}, after_sources={"a.sv": sa})
        (e,) = r.by_line
        self.assertEqual((e["line"], e["location_side"]), (5, "after"))


class Verdicts(unittest.TestCase):
    def v(self, ob, oa, **kw):
        return diff_trees(flat(ob), flat(oa), **kw)

    def test_all(self):
        base = [op("AND", 10, tgt="z")]
        self.assertEqual(self.v(base, base).verdict, "NO_CHANGE")
        self.assertEqual(self.v(base, base + [op("ADD", 2, tgt="n", line=9)]).verdict, "NEGLIGIBLE")
        self.assertEqual(self.v(base, base + [op("ADD", 100, tgt="n", line=9)]).verdict, "INCREASE")
        self.assertEqual(self.v(base + [op("ADD", 100, tgt="n", line=9)], base).verdict, "DECREASE")
        wide = op("DIV", 6, tgt="d", line=9, sigma=5000, quality="poor")
        r = self.v(base, base + [wide])
        self.assertEqual(r.verdict, "UNCERTAIN")
        self.assertLessEqual(r.delta.min, 0)

    def test_not_comparable(self):
        t = flat([op("AND", 1, tgt="z")])
        r = diff_trees(None, t)
        self.assertEqual((r.verdict, r.confidence.level, r.delta), ("NOT_COMPARABLE", "low", None))
        self.assertEqual(diff_trees(t, None).verdict, "NOT_COMPARABLE")
        t2 = flat([op("AND", 1, tgt="z")], config={"profile": "x"})
        t3 = flat([op("AND", 1, tgt="z")], config={"profile": "y"})
        self.assertEqual(diff_trees(t2, t3).verdict, "NOT_COMPARABLE")

    def test_top_changed(self):
        b, a = flat([op("AND", 100, tgt="z")], name="top_a"), flat([op("AND", 100, tgt="z")], name="top_b")
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        self.assertTrue(any(d.code == "TOP_CHANGED" for d in r.diagnostics_new))
        self.assertEqual(r.confidence.level, "low")

    def test_threshold(self):
        base = [op("AND", 10, tgt="z")]
        r = self.v(base, base + [op("ADD", 100, tgt="n", line=9)], threshold_ge=200)
        self.assertEqual(r.verdict, "NEGLIGIBLE")
        self.assertEqual((len(r.contributors), r.others_count, r.others_net.typ), (0, 1, 100 * G))

    def test_folding_and_sorting(self):
        base = [op("AND", 10, tgt="z")]
        new = [op("ADD", 20, tgt="a", line=3), op("MUL", 300, tgt="b", line=4), op("AND", 2, tgt="c", line=5)]
        b, a = flat(base), flat(base + new)
        r = diff_trees(b, a)
        check_additive(self, r, b, a)
        self.assertEqual([c.line for c in r.contributors], [4, 3])
        self.assertEqual((r.others_count, r.others_net.typ), (1, 2 * G))


class Confidence(unittest.TestCase):
    def test_range_reason(self):
        r = diff_trees(flat([op("AND", 10, tgt="z")]), flat([op("AND", 10, tgt="z"), op("ADD", 100, tgt="n", line=9)]))
        self.assertIn(r.confidence.level, ("medium", "low"))
        self.assertTrue(any("delta range is +/-" in x for x in r.confidence.reasons))

    def test_poor_class(self):
        b = flat([op("AND", 10, tgt="z")])
        a = flat([op("AND", 10, tgt="z"), op("DIV", 200, tgt="d", line=9, quality="poor"), op("ADD", 10, tgt="x", line=3)])
        r = diff_trees(b, a)
        self.assertEqual(r.confidence.level, "low")
        self.assertTrue(any("% of delta in div class (poor model quality)" in x for x in r.confidence.reasons))

    def test_unsupported_in_changed_cone(self):
        b = flat([op("AND", 10, tgt="z")])
        a = flat([op("AND", 10, tgt="z"), op("ADD", 100, tgt="n", line=9)], unsupported_in=frozenset({"top"}))
        r = diff_trees(b, a)
        self.assertEqual(r.confidence.level, "low")
        self.assertTrue(any("unsupported construct" in x for x in r.confidence.reasons))

    def test_unsupported_outside_changed_cone(self):
        a_mods = [mod("top", [op("AND", 10, tgt="z")], instances=[("u", "other", 1)]), mod("other", [op("ADD", 5, tgt="y")])]
        b = tree(a_mods)
        a = tree([mod("top", [op("AND", 10, tgt="z"), op("ADD", 100, tgt="n", line=9)], instances=[("u", "other", 1)]), a_mods[1]],
                 unsupported_in=frozenset({"other"}))
        r = diff_trees(b, a)
        self.assertFalse(any("unsupported" in x for x in r.confidence.reasons))

    def test_diagnostics_delta(self):
        d_old = Diagnostic("MISSING_MODULE", "m", "warning", True, "before", "a.sv", 3)
        d_new = Diagnostic("UNSUPPORTED_CONSTRUCT", "w", "warning", True, "after", "a.sv", 9)
        b = flat([op("AND", 10, tgt="z")], diagnostics=(d_old,))
        a = flat([op("AND", 10, tgt="z")], diagnostics=(d_new,))
        r = diff_trees(b, a)
        self.assertEqual([d.code for d in r.diagnostics_new], ["UNSUPPORTED_CONSTRUCT"])
        self.assertEqual([d.code for d in r.diagnostics_resolved], ["MISSING_MODULE"])

    def test_assumed(self):
        a = flat([op("AND", 10, tgt="z")], assumptions=("parameter DEPTH unresolved, default 32 used",))
        r = diff_trees(flat([op("AND", 10, tgt="z")]), a)
        self.assertEqual(r.assumed, ("parameter DEPTH unresolved, default 32 used",))


class Speed(unittest.TestCase):
    def test_single_module_edit(self):
        def build(bump):
            mods = []
            for i in range(300):
                ops = [op("ADD", 10 + j, tgt=f"t{j}", line=j + 1, shape=f"s{i}_{j}") for j in range(20)]
                if bump and i == 150:
                    ops.append(op("MUL", 50, tgt="new", line=99))
                mods.append(mod(f"m{i}", ops, instances=[] if i else [(f"u{k}", f"m{k}", 1) for k in range(1, 300)]))
            return tree(mods)
        b, a = build(False), build(True)
        t = time.perf_counter()
        r = diff_trees(b, a)
        dt = time.perf_counter() - t
        self.assertEqual(len(r.all_contributors), 1)
        self.assertLess(dt, 0.25)


class TopConfig(unittest.TestCase):
    def test_auto_top_against_pinned_top_is_not_a_mismatch(self):
        b = flat([op("ADD", 100, tgt="a")], config={"top": None})
        a = flat([op("ADD", 100, tgt="a")], config={"top": "top"})
        r = diff_trees(b, a)
        self.assertFalse(any(d.code == "CONFIG_MISMATCH" for d in r.diagnostics_new))

    def test_two_different_pinned_tops_still_mismatch(self):
        b = flat([op("ADD", 100, tgt="a")], config={"top": "x"})
        a = flat([op("ADD", 100, tgt="a")], config={"top": "y"})
        r = diff_trees(b, a)
        self.assertTrue(any(d.code == "CONFIG_MISMATCH" for d in r.diagnostics_new))


if __name__ == "__main__":
    unittest.main()
