"""Golden tests: the renderers reproduce the examples of OUTPUT.md section 2.3."""
import json
import unittest

from gatecost.report import render
from gatecost.report.validate import validate_json
from gatecost.result import (
    CompareResult, Confidence, Contributor, CostResult, DiffResult, EstimateResult, Ge3,
    ModuleCost, Output, Diagnostic,
)


def g(typ, lo=None, hi=None):
    return Ge3((typ if lo is None else lo) * 1000, typ * 1000, (typ if hi is None else hi) * 1000)


def contrib(file, line, reason, desc, typ, **kw):
    return Contributor(file, line, reason, desc, g(typ), **kw)


class AgentGolden(unittest.TestCase):
    def test_typical_increase(self):
        r = DiffResult(
            "INCREASE", Confidence("medium", ("delta range is +/-19% of the estimate",), 0.19), "top_core",
            g(28410), g(28752), g(342, 281, 410), 1.2,
            (contrib("alu.sv", 87, "added", "MUL 16x16", 210),
             contrib("alu.sv", 102, "width", "MUX 4:1 w16->w32", 96),
             contrib("regfile.sv", 41, "added", "DFF x36 (register stage)", 36)),
            others_count=2,
        )
        self.assertEqual(
            render(Output("diff", result=r), "agent"),
            "DELTA +342 GE (+281..+410) +1.2% verdict=INCREASE confidence=medium\n"
            "TOP top_core before=28410 after=28752\n"
            "+210 alu.sv:87 added MUL 16x16\n"
            "+96 alu.sv:102 width MUX 4:1 w16->w32\n"
            "+36 regfile.sv:41 added DFF x36 (register stage)\n"
            "OTHERS 2 net=0\n"
            "WHY medium: delta range is +/-19% of the estimate\n",
        )

    def test_parameter_group(self):
        c = contrib("config.sv", 12, "parameter", "W 16->32 (14 ops)", 1260, instances=1,
                    children=(contrib("alu.sv", 1, "width", "ADD", 1),))
        r = DiffResult("INCREASE", Confidence("medium", ("delta range is +/-23% of the estimate",)), "top_core",
                       g(28410), g(29670), g(1260, 990, 1580), 4.4, (c,))
        self.assertIn("+1260 config.sv:12 parameter W 16->32 (14 ops) x1\n", render(Output("diff", result=r), "agent"))

    def test_low_with_warning(self):
        r = DiffResult("INCREASE", Confidence("low", ("94% of delta in div class (poor model quality)",)), "top_core",
                       g(28410), g(28590), g(180, 60, 480), 0.6,
                       (contrib("div.sv", 33, "added", "DIV 32/16", 170), contrib("div.sv", 40, "added", "MUX 2:1 w32", 10)))
        d = Diagnostic("UNSUPPORTED_CONSTRUCT", "unbounded while loop (cost not included)", affects_cost=True, file="alu.sv", line=140)
        self.assertEqual(
            render(Output("diff", result=r, diagnostics=(d,)), "agent"),
            "DELTA +180 GE (+60..+480) +0.6% verdict=INCREASE confidence=low\n"
            "TOP top_core before=28410 after=28590\n"
            "+170 div.sv:33 added DIV 32/16\n"
            "+10 div.sv:40 added MUX 2:1 w32\n"
            "WHY low: 94% of delta in div class (poor model quality)\n"
            "WARN UNSUPPORTED_CONSTRUCT alu.sv:140 unbounded while loop (cost not included)\n",
        )

    def test_no_change_and_not_comparable(self):
        r = DiffResult("NO_CHANGE", Confidence("high"), "top_core", g(28410), g(28410), g(0), 0.0)
        self.assertEqual(
            render(Output("diff", result=r), "agent"),
            "DELTA 0 GE (0..0) 0.0% verdict=NO_CHANGE confidence=high\nTOP top_core before=28410 after=28410\n",
        )
        r = DiffResult("NOT_COMPARABLE", Confidence("low", ("baseline has no module named top_core (top changed?)",)))
        self.assertEqual(
            render(Output("diff", result=r), "agent"),
            "DELTA n/a verdict=NOT_COMPARABLE confidence=low\nWHY low: baseline has no module named top_core (top changed?)\n",
        )

    def test_error_and_quiet(self):
        out = Output("diff", status="error", error=("NO_SOURCES", "no RTL files found under ."))
        self.assertEqual(render(out, "agent"), "ERROR NO_SOURCES no RTL files found under .\n")
        r = DiffResult("NO_CHANGE", Confidence("high"), "t", g(1), g(1), g(0), 0.0)
        self.assertEqual(render(Output("diff", result=r), "agent", quiet=True).count("\n"), 1)

    def test_zero_and_rounding(self):
        r = DiffResult("NEGLIGIBLE", Confidence("high"), "t", g(100), g(100), Ge3(-1500, -1500, 400), -0.04,
                       (Contributor("a.v", 1, "removed", "ADD w4", Ge3(-1500, -1500, -1500)),))
        text = render(Output("diff", result=r), "agent")
        self.assertTrue(text.startswith("DELTA -2 GE (-2..0) 0.0% "), text)  # half away from zero

    def test_max_lines_folds_into_others(self):
        cs = tuple(contrib("a.v", i, "added", "ADD", 10) for i in range(1, 6))
        r = DiffResult("INCREASE", Confidence("high"), "t", g(0), g(50), g(50), None, cs)
        lines = render(Output("diff", result=r), "agent", max_lines=2).splitlines()
        self.assertEqual(lines[0], "DELTA +50 GE (+50..+50) n/a verdict=INCREASE confidence=high")
        self.assertIn("OTHERS 3 net=+30", lines)

    def test_cost_estimate_compare(self):
        conf = Confidence("medium")
        cost = CostResult("top", g(28410, 27900, 29100), conf,
                          (ModuleCost("alu", 1, g(1200), g(1400)),), {"seq": 7100_000, "mul": 4800_000})
        text = render(Output("cost", result=cost), "agent")
        self.assertTrue(text.startswith("COST 28410 GE (27900..29100) confidence=medium\nTOP top\n1400 alu 5%\n"))
        self.assertIn("CATEGORIES seq=7100 mul=4800", text)
        est = EstimateResult("op", "mul 32x32", "MUL 32x32 unsigned", g(4900, 3900, 7200),
                             {"min": "array", "typ": "booth_wallace", "max": "wallace_ks"}, {"FA": 760, "HA": 32}, conf)
        self.assertEqual(
            render(Output("estimate", result=est), "agent"),
            "ESTIMATE 4900 GE (3900..7200) confidence=medium\nOP MUL 32x32 unsigned\n"
            "ARCH min=array typ=booth_wallace max=wallace_ks\nBREAKDOWN FA=760 HA=32\n",
        )
        a, b = est, EstimateResult("op", "mul 32 const=10", "MUL 32x32 const", g(300, 200, 400), {}, {}, conf)
        cmp = CompareResult((a, b), "typ", 1, g(4600), False, 0)
        self.assertIn("BEST B saves 4600 GE vs A ranges=disjoint", render(Output("estimate", result=cmp), "agent"))


class OtherFormats(unittest.TestCase):
    def setUp(self):
        self.r = DiffResult("INCREASE", Confidence("medium", ("range",), 0.19), "top_core", g(100), g(150), g(50, 40, 60), 50.0,
                            (contrib("alu.sv", 87, "added", "MUL 16x16 | x", 50),), by_category={"mul": 50_000})
        self.out = Output("diff", result=self.r)

    def test_json_validates_and_roundtrips(self):
        doc = json.loads(render(self.out, "json"))
        self.assertEqual(validate_json(doc), [])
        self.assertEqual(doc["result"]["delta"]["ge"], {"min": 40, "typ": 50, "max": 60})
        self.assertEqual(doc["result"]["contributors"][0]["rank"], 1)
        self.assertEqual(doc["schema_version"], 1)

    def test_json_error_envelope(self):
        doc = json.loads(render(Output("diff", status="error", error=("NO_BASELINE", "x")), "json"))
        self.assertIsNone(doc["result"])
        self.assertEqual(validate_json(doc), [])
        doc.pop("error")
        self.assertTrue(validate_json(doc))

    def test_schema_rejects_bad_verdict(self):
        doc = json.loads(render(self.out, "json"))
        doc["result"]["verdict"] = "MAYBE"
        self.assertTrue(validate_json(doc))

    def test_text_and_markdown_agree_on_headline(self):
        text = render(self.out, "text")
        md = render(self.out, "markdown")
        self.assertTrue(text.startswith("DELTA +50 GE (+40..+60) +50.0% verdict=INCREASE"))
        self.assertIn("INCREASE +50 GE (+40..+60)", md)
        self.assertIn("MUL 16x16 \\| x", md)  # pipes escaped in tables

    def test_agent_is_ascii(self):
        render(self.out, "agent").encode("ascii")

    def test_determinism(self):
        self.assertEqual(render(self.out, "json"), render(self.out, "json"))


if __name__ == "__main__":
    unittest.main()
