"""Reporters (json, agent, text, markdown), the schema validator and the IR export/dump helpers."""
import json
import unittest

from gatecost.ir.dump import dump, fmt_const
from gatecost.ir.export import _plain, export_module, export_origin
from gatecost.ir.sigspec import Const
from gatecost.report import FORMATS, render
from gatecost.report import fmt
from gatecost.report.json_fmt import SCHEMA_VERSION, envelope, render_json
from gatecost.report.validate import validate_json
from gatecost.result import (
    CompareResult, Confidence, Contributor, CostResult, Diagnostic, DiffResult, EstimateResult, Ge3,
    MessageResult, ModuleCost, Output, SnapshotResult,
)
from tests.ir_dsl import Mod

HI = Confidence("high")
LOW = Confidence("low", ("because",), 2.5)


def ge(n):
    return Ge3(n * 1000, n * 1000, n * 1000)


def contrib(delta=5, **kw):
    d = dict(file="a.v", line=3, reason="width", description="ADD 8->16 | x", delta=ge(delta), op="ADD",
             widths=(8, 16), category="addcmp", module="top", instances=2, per_instance=ge(delta // 2))
    d.update(kw)
    return Contributor(**d)


def diff_result(**kw):
    d = dict(verdict="INCREASE", confidence=LOW, top="top", before=ge(10), after=ge(15), delta=Ge3(4000, 5000, 5600),
             percent=50.0, contributors=(contrib(), contrib(-2, reason="removed", file="b.v", line=1, instances=1,
                                                              cause={"kind": "parameter", "name": "W"})),
             others_count=1, others_net=ge(1), by_category={"addcmp": 3000}, assumed=("assume1",))
    d.update(kw)
    return DiffResult(**d)


def est(text="add 8"):
    return EstimateResult("op", text, "ADD(8,8)", Ge3(40000, 50000, 60000), {"min": "rca", "typ": "rca", "max": "rca"},
                          {"FA": 8}, HI, ("a",))


def cost_result():
    mods = (ModuleCost("top", 1, ge(3), ge(10)), ModuleCost("leaf", 3, ge(2), ge(6)))
    return CostResult("top", ge(10), HI, mods, {"addcmp": 4000}, ("as",))


def outs():
    diags = (Diagnostic("W1", "careful", affects_cost=True, file="a.v", line=2),
             Diagnostic("I1", "fyi", severity="info"))
    return {
        "diff": Output("diff", result=diff_result(), diagnostics=diags),
        "cost": Output("cost", result=cost_result(), diagnostics=diags),
        "estimate": Output("estimate", result=est()),
        "compare": Output("estimate", result=CompareResult((est("a"), est("b")), "typ", 1, ge(2), False, 0)),
        "snapshot": Output("snapshot", result=SnapshotResult("abc", 3, 4)),
        "message": Output("init", result=MessageResult(("hello",), {"k": 1})),
        "error": Output("diff", status="error", error=("BAD", "oops")),
    }


class Fmt(unittest.TestCase):
    def test_rounding_half_away_from_zero(self):
        self.assertEqual([fmt.ge(x) for x in (499, 500, 1500, -499, -500, -1500, 0)], [0, 1, 2, 0, -1, -2, 0])

    def test_sge_and_pct(self):
        self.assertEqual((fmt.sge(0), fmt.sge(1000), fmt.sge(-1000)), ("0", "+1", "-1"))
        self.assertEqual((fmt.pct(None), fmt.pct(0.04), fmt.pct(-0.04), fmt.pct(12.34), fmt.pct(-3)),
                         ("n/a", "0.0%", "0.0%", "+12.3%", "-3.0%"))

    def test_loc_and_rng(self):
        self.assertEqual((fmt.loc(None, 3), fmt.loc("a", None), fmt.loc("a", 2)), ("-", "a", "a:2"))
        self.assertEqual(fmt.rng(Ge3(-1000, 0, 2000)), "(-1..+2)")
        self.assertEqual(fmt.rng(Ge3(1000, 2000, 3000), False), "(1..3)")

    def test_warn_lines_limit_and_filter(self):
        ds = tuple(Diagnostic(f"C{i}", "m", affects_cost=True) for i in range(7)) + (Diagnostic("Q", "q"),)
        lines = fmt.warn_lines(ds)
        self.assertEqual(len(lines), 6)
        self.assertIn("+2 more", lines[-1])
        self.assertEqual(fmt.warn_lines((Diagnostic("Q", "q"),)), [])


class JsonSchema(unittest.TestCase):
    def test_every_output_validates(self):
        for name, o in outs().items():
            doc = json.loads(render_json(o))
            self.assertEqual(validate_json(doc), [], name)
            self.assertEqual(doc["schema_version"], SCHEMA_VERSION)

    def test_deterministic_and_key_order(self):
        o = outs()["diff"]
        self.assertEqual(render(o, "json"), render(o, "json"))
        keys = list(json.loads(render_json(o)))
        self.assertEqual(keys[:6], ["schema_version", "tool", "command", "status", "profile", "config"])
        self.assertEqual(keys[-2:], ["diagnostics", "meta"])

    def test_no_meta(self):
        self.assertNotIn("meta", envelope(outs()["diff"], include_meta=False))

    def test_error_envelope(self):
        doc = json.loads(render_json(outs()["error"]))
        self.assertIsNone(doc["result"])
        self.assertEqual(doc["error"], {"code": "BAD", "message": "oops"})
        self.assertEqual(validate_json(doc), [])

    def test_ge_numbers(self):
        doc = json.loads(render_json(Output("diff", result=diff_result(delta=Ge3(1, 1500, 2000)))))
        d = doc["result"]["delta"]["ge"]
        self.assertEqual(d, {"min": 0.001, "typ": 1.5, "max": 2})
        self.assertIsInstance(d["max"], int)

    def test_contributor_limit_and_rank(self):
        doc = envelope(outs()["diff"], limit=1)
        cs = doc["result"]["contributors"]
        self.assertEqual([c["rank"] for c in cs], [1])

    def test_operations_only_when_present(self):
        r = json.loads(render_json(outs()["diff"]))["result"]
        self.assertNotIn("operations", r)
        o = Output("diff", result=diff_result(operations=({"module": "x"},)))
        self.assertEqual(json.loads(render_json(o))["result"]["operations"], [{"module": "x"}])

    def test_non_ascii_preserved(self):
        o = Output("diff", result=diff_result(contributors=(contrib(description="été"),)))
        self.assertIn("été", render_json(o))

    def test_none_delta_diff_validates(self):
        r = diff_result(verdict="NOT_COMPARABLE", before=None, after=None, delta=None, percent=None, contributors=())
        self.assertEqual(validate_json(json.loads(render_json(Output("diff", result=r)))), [])

    def test_validator_rejects_bad_documents(self):
        good = json.loads(render_json(outs()["diff"]))

        def mut(f):
            d = json.loads(json.dumps(good))
            f(d)
            return validate_json(d)

        self.assertTrue(mut(lambda d: d.pop("status")))
        self.assertTrue(mut(lambda d: d.update(status="weird")))
        self.assertTrue(mut(lambda d: d.update(tool="other")))
        self.assertTrue(mut(lambda d: d.update(schema_version=0)))
        self.assertTrue(mut(lambda d: d["result"].update(verdict="MAYBE")))
        self.assertTrue(mut(lambda d: d["result"]["contributors"][0].pop("delta_ge")))
        self.assertTrue(mut(lambda d: d["result"]["contributors"][0].update(reason="nope")))
        self.assertTrue(mut(lambda d: d["result"]["confidence"].update(level="x")))
        self.assertTrue(mut(lambda d: d["diagnostics"].append({"code": "x"})))
        self.assertTrue(mut(lambda d: d.update(status="error")))     # error needs error key and null result

    def test_validator_bool_not_integer(self):
        d = json.loads(render_json(outs()["diff"]))
        d["schema_version"] = True
        self.assertTrue(validate_json(d))

    def test_validator_cost_requires_modules(self):
        d = json.loads(render_json(outs()["cost"]))
        del d["result"]["modules"]
        self.assertTrue(validate_json(d))


class Renderers(unittest.TestCase):
    def test_render_unknown_format(self):
        with self.assertRaises(ValueError):
            render(outs()["diff"], "yaml")

    def test_all_formats_all_results_nonempty_and_deterministic(self):
        for name, o in outs().items():
            for f in FORMATS:
                a = render(o, f)
                self.assertTrue(a.endswith("\n"), (name, f))
                self.assertTrue(a.strip(), (name, f))
                self.assertEqual(a, render(o, f))
                self.assertNotIn("\t", a if f != "json" else "")

    def test_agent_error(self):
        self.assertEqual(render(outs()["error"], "agent"), "ERROR BAD oops\n")
        self.assertTrue(render(outs()["error"], "text").startswith("ERROR BAD"))

    def test_agent_diff_lines(self):
        lines = render(outs()["diff"], "agent").splitlines()
        self.assertEqual(lines[0], "DELTA +5 GE (+4..+6) +50.0% verdict=INCREASE confidence=low")
        self.assertEqual(lines[1], "TOP top before=10 after=15")
        self.assertIn("a.v:3 width ADD 8->16 | x x2", lines[2])
        self.assertIn("cause=parameter:W", lines[3])
        self.assertIn("OTHERS 1 net=+1", lines)
        self.assertIn("WHY low: because", lines)
        self.assertIn("WARN W1 a.v:2 careful", lines)
        self.assertFalse(any("fyi" in x for x in lines))   # info diag not affecting cost
        self.assertIn("ASSUMED assume1", lines)

    def test_agent_max_lines_folds_into_others(self):
        lines = render(outs()["diff"], "agent", max_lines=1).splitlines()
        others = [x for x in lines if x.startswith("OTHERS")]
        self.assertEqual(others, ["OTHERS 2 net=-1"])    # (+1) + (-2)

    def test_agent_quiet(self):
        for k in ("diff", "cost", "estimate"):
            self.assertEqual(len(render(outs()[k], "agent", quiet=True).splitlines()), 1)

    def test_agent_verbose_diff(self):
        r = diff_result(structural=({"description": "new inst"},), modules_verbose=("MOD x",))
        s = render(Output("diff", result=r), "agent", verbose=True)
        self.assertIn("MOD x", s)
        self.assertIn("CATEGORIES addcmp=+3", s)
        self.assertIn("STRUCT new inst", s)

    def test_agent_diff_na(self):
        r = diff_result(verdict="NOT_COMPARABLE", before=None, after=None, delta=None, percent=None, contributors=())
        s = render(Output("diff", result=r), "agent")
        self.assertTrue(s.startswith("DELTA n/a verdict=NOT_COMPARABLE confidence=low"))
        self.assertNotIn("TOP ", s)

    def test_agent_cost(self):
        lines = render(outs()["cost"], "agent").splitlines()
        self.assertEqual(lines[0], "COST 10 GE (10..10) confidence=high")
        self.assertIn("6 leaf x3 60%", lines)
        self.assertIn("10 top 100%", lines)

    def test_agent_cost_zero_total_no_division_error(self):
        r = CostResult("top", ge(0), HI, (ModuleCost("top", 1, ge(0), ge(0)),))
        self.assertIn("0 top 0%", render(Output("cost", result=r), "agent"))

    def test_agent_estimate_and_compare(self):
        s = render(outs()["estimate"], "agent")
        self.assertIn("ESTIMATE 50 GE (40..60) confidence=high", s)
        self.assertIn("ARCH min=rca typ=rca max=rca", s)
        self.assertIn("BREAKDOWN FA=8", s)
        c = render(outs()["compare"], "agent").splitlines()
        self.assertEqual(c[0], "COMPARE 2 alternatives basis=typ")
        self.assertEqual(c[-1], "BEST B saves 2 GE vs A ranges=disjoint")

    def test_agent_snapshot_and_message(self):
        self.assertEqual(render(outs()["snapshot"], "agent"), "SNAPSHOT saved id=abc files=3 modules=4\n")
        self.assertEqual(render(outs()["message"], "agent"), "hello\n")

    def test_text_tables_width_bounded(self):
        r = diff_result(contributors=(contrib(description="x" * 300),))
        s = render(Output("diff", result=r), "text")
        self.assertTrue(all(len(x) <= 100 for x in s.splitlines()[2:] if not x.startswith("Reasons")))
        self.assertIn("Contributors", s)
        self.assertIn("Confidence low: because", s)
        self.assertIn("Assumed: assume1", s)

    def test_text_cost_and_verbose_category(self):
        s = render(outs()["cost"], "text")
        self.assertIn("Modules", s)
        self.assertIn("By category", s)
        v = render(outs()["diff"], "text", verbose=True)
        self.assertIn("By category", v)
        self.assertNotIn("By category", render(outs()["diff"], "text"))

    def test_text_more_line_count(self):
        s = render(outs()["diff"], "text", max_lines=1)
        self.assertIn("... and 2 more", s)

    def test_text_more_line_never_negative(self):
        # BUG src/gatecost/report/text.py:39: len(contributors) - max_lines + others_count goes negative
        # when fewer contributors than max_lines are shown but others_count > 0 ("... and -7 more").
        s = render(outs()["diff"], "text", max_lines=10)
        self.assertNotIn("and -", s)
        self.assertIn("... and 1 more", s)

    def test_text_quiet_and_estimate_fall_back_to_agent(self):
        self.assertEqual(render(outs()["estimate"], "text"), render(outs()["estimate"], "agent"))
        self.assertEqual(render(outs()["diff"], "text", quiet=True), render(outs()["diff"], "agent", quiet=True))

    def test_markdown_diff(self):
        s = render(outs()["diff"], "markdown")
        self.assertTrue(s.startswith("### gatecost: INCREASE +5 GE (+4..+6), +50.0%, confidence low"))
        self.assertIn("ADD 8->16 \\| x", s)          # pipe escaped
        self.assertIn("_1 more_", s)
        self.assertIn("**Confidence low:** because", s)
        self.assertIn("<details>", s)
        self.assertTrue(s.rstrip().endswith("not synthesis results._"))

    def test_markdown_non_diff_is_fenced(self):
        s = render(outs()["cost"], "markdown")
        self.assertTrue(s.startswith("```\nCOST"))
        s = render(outs()["error"], "markdown")
        self.assertIn("ERROR BAD oops", s)

    def test_markdown_truncation(self):
        many = tuple(contrib(description="d" * 200, line=i) for i in range(1000))
        s = render(Output("diff", result=diff_result(contributors=many)), "markdown", max_lines=1000)
        self.assertLessEqual(len(s), 60_000)
        self.assertIn("(truncated)", s)


class IrExportDump(unittest.TestCase):
    def mod(self):
        m = Mod("alu")
        a, b = m.inp("a", 8), m.inp("b", 8)
        m.out("y", (a + b).trunc(8) if hasattr(a + b, "trunc") else a + b)
        return m.done()

    def test_export_is_json_serializable_and_stable(self):
        a, b = export_module(self.mod()), export_module(self.mod())
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(a["name"], "alu")
        self.assertEqual([p["name"] for p in a["ports"]], ["a", "b", "y"])
        self.assertIn("ADD", [n["op"] for n in a["nodes"]])
        self.assertEqual(len(a["canonical_hash"]), 2 * len(bytes.fromhex(a["canonical_hash"])))

    def test_export_computes_hash_when_missing(self):
        m = Mod("t")
        m.out("y", m.inp("a", 4))
        mod = m.done(analyze=False)
        self.assertIsNone(mod.canonical_hash)
        self.assertIsNotNone(export_module(mod)["canonical_hash"])

    def test_export_inputs_have_known_info(self):
        n = next(n for n in export_module(self.mod())["nodes"] if n["op"] == "ADD")
        self.assertEqual([i["name"] for i in n["inputs"]], ["A", "B"])
        self.assertTrue(all("is_const" in i and "width" in i for i in n["inputs"]))
        self.assertEqual(n["merge_count"], 1)
        self.assertFalse(n["const_folded"])

    def test_plain(self):
        self.assertEqual(_plain((1, (2, "x"), [None])), [1, [2, "x"], [None]])
        self.assertEqual(_plain({1: Const(5, 4, 0)}), {"1": {"value": 5, "width": 4, "xmask": 0}})
        self.assertEqual(_plain(b"\x01\xff"), "01ff")

        class T(tuple):
            pass

        self.assertEqual(_plain(T((1, 2))), [1, 2])
        json.dumps(_plain({"a": (Const(1, 1, 0), b"z")}))

    def test_export_origin(self):
        self.assertIsNone(export_origin(None))

    def test_dump_deterministic_and_normalize(self):
        d1, d2 = dump(self.mod()), dump(self.mod())
        self.assertEqual(d1, d2)
        self.assertTrue(d1.startswith("module alu"))
        self.assertIn("ADD(", d1)
        self.assertTrue(d1.endswith("\n"))
        n = dump(self.mod(), normalize=True)
        self.assertNotIn("@", n)
        self.assertIn("known ", d1)
        self.assertNotIn("known ", dump(self.mod(), analysis=False))

    def test_fmt_const(self):
        self.assertEqual(fmt_const(Const(255, 8, 0)), "8'hff")
        self.assertEqual(fmt_const(Const(0b10, 4, 0b0100)), "4'b0x10")


if __name__ == "__main__":
    unittest.main()
