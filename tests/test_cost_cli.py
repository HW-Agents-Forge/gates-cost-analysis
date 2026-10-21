"""`estimate` and `profiles` commands (CLI.md sections 2.6, 3 and 6)."""
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from gatecost import estimate as est
from gatecost.cli import main
from gatecost.cost import cost_op, load_profile
from gatecost.errors import GatecostError
from gatecost.report.validate import validate_json

P = load_profile()


def run(*argv, cwd=None):
    out = io.StringIO()
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(argv))
    finally:
        os.chdir(old)
    return code, out.getvalue()


def headline(text):
    m = re.match(r"ESTIMATE (\d+) GE \((\d+)\.\.(\d+)\) confidence=(\w+)", text)
    assert m, text
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)


def typ_of(expr):
    return est.estimate_expression(expr, P).ge.typ


GRAMMAR = [
    ("add 32", "ADD", {"n": 32}), ("sub 16", "SUB", {"n": 16}), ("neg 8", "NEG", {"n": 8}),
    ("add 32 const=5", "ADD", {"n": 32, "const": 5}), ("add 32 const", "ADD", {"n": 32, "const_unknown": True}),
    ("mul 16x16", "MUL", {"n": 16, "m": 16}), ("mul 32x32 signed", "MUL", {"n": 32, "m": 32, "signed": True}),
    ("mul 24x8", "MUL", {"n": 24, "m": 8}), ("mul 16 const=10", "MUL", {"n": 16, "const": 10}),
    ("mul 16 const", "MUL", {"n": 16, "const_unknown": True}), ("mul 16x16 square", "MUL", {"n": 16, "m": 16, "square": True}),
    ("mul 16x16 trunc=16", "MUL", {"n": 16, "m": 16, "trunc": 16}),
    ("div 32/16", "DIV", {"n": 32, "m": 16}), ("mod 32/8", "MOD", {"n": 32, "m": 8}), ("div 32/16 signed", "DIV", {"n": 32, "m": 16, "signed": True}),
    ("div 32 const=10", "DIV", {"n": 32, "const": 10}), ("mod 16 const=4", "MOD", {"n": 16, "const": 4}),
    ("shl 32 by 5", "SHL", {"n": 32, "s": 5}), ("shr 32 by 5", "SHR", {"n": 32, "s": 5}), ("sar 64 by 6", "SAR", {"n": 64, "s": 6}),
    ("rol 32 by 5", "ROL", {"n": 32, "s": 5}), ("ror 32 by 8", "ROR", {"n": 32, "s": 8}),
    ("shl 32 by const", "SHL", {"n": 32, "s": 0, "const_amount": True}), ("shl 1 by 5 lhs_one", "SHL", {"n": 1, "s": 5, "lhs_one": True}),
    ("and 8", "AND", {"n": 8}), ("or 8", "OR", {"n": 8}), ("xor 64", "XOR", {"n": 64}), ("not 8", "NOT", {"n": 8}),
    ("eq 32", "EQ", {"n": 32}), ("ne 32", "NE", {"n": 32}), ("lt 32", "LT", {"n": 32}), ("le 32", "LE", {"n": 32}),
    ("gt 32", "GT", {"n": 32}), ("ge 32", "GE", {"n": 32}), ("lt 32 signed", "LT", {"n": 32, "signed": True}),
    ("eq 32 const", "EQ", {"n": 32, "const_unknown": True}), ("eq 32 const=0", "EQ", {"n": 32, "const": 0}),
    ("red_and 8", "RED_AND", {"n": 8}), ("red_or 8", "RED_OR", {"n": 8}), ("red_xor 32", "RED_XOR", {"n": 32}),
    ("mux 8:1 w64", "MUX", {"k": 8, "w": 64}), ("pmux 5 w16", "PMUX", {"k": 5, "w": 16}), ("priority 4 w8", "PRIORITY", {"k": 4, "w": 8}),
    ("decode 5", "DECODE", {"s": 5}), ("encode 8", "ENCODE", {"k": 8}), ("encode 8 priority", "ENCODE", {"k": 8, "priority": True}),
    ("reg 32", "DFF", {"n": 32}), ("reg 32 async_reset", "DFF", {"n": 32, "async_reset": True}),
    ("reg 128 sync_reset enable", "DFF", {"n": 128, "sync_reset": True, "enable": True}),
    ("reg 32 async_set_reset scan", "DFF", {"n": 32, "async_set_reset": True, "scan": True}),
    ("latch 8", "LATCH", {"n": 8}),
    ("mem 256x32 rports=2 wports=1 sync_read", "MEM", {"depth": 256, "width": 32, "rports": 2, "wports": 1, "sync_read": True}),
    ("mem 16x8", "MEM", {"depth": 16, "width": 8}), ("mem 16x8 async_read", "MEM", {"depth": 16, "width": 8, "sync_read": False}),
    ("rom 64x8", "ROM", {"depth": 64, "width": 8}),
    ("sum 4x32", "SUM", {"k": 4, "w": 32}), ("mac 16x16 c=32", "MAC", {"n": 16, "m": 16, "c": 32}), ("pow 8 exp=5", "POW", {"n": 8, "exp": 5}),
]


class Notation(unittest.TestCase):
    def test_every_term_matches_cost_op(self):
        for text, op, attrs in GRAMMAR:
            with self.subTest(text=text):
                r = est.estimate_expression(text, P)
                want = cost_op(op, attrs, P)
                self.assertEqual((r.ge.min, r.ge.typ, r.ge.max), (want.min, want.typ, want.max))
                self.assertEqual(r.breakdown, dict(sorted(want.breakdown.items(), key=lambda kv: (-kv[1], kv[0]))))

    def test_case_and_spacing_are_flexible(self):
        self.assertEqual(typ_of("MUL 16X16 SIGNED"), typ_of("mul 16x16 signed"))
        self.assertEqual(typ_of("  mul   16x16 +add 32"), typ_of("mul 16x16 + add 32"))
        self.assertEqual(typ_of("4*mul 16x16"), typ_of("4 * mul 16x16"))

    def test_sums(self):
        s = typ_of("mul 16x16 + add 32 + reg 32")
        self.assertEqual(s, typ_of("mul 16x16") + typ_of("add 32") + typ_of("reg 32"))
        r = est.estimate_expression("mul 16x16 + add 32 + reg 32", P)
        parts = [est.estimate_expression(t, P).ge for t in ("mul 16x16", "add 32", "reg 32")]
        self.assertEqual((r.ge.min, r.ge.max), (sum(g.min for g in parts), sum(g.max for g in parts)))  # scenario-wise
        self.assertEqual(r.normalized, "MUL 16x16 unsigned + ADD 32 + REG 32")

    def test_multipliers(self):
        self.assertEqual(typ_of("4 * mul 16x16"), 4 * typ_of("mul 16x16"))
        self.assertEqual(typ_of("3 * add 8 + 2 * reg 8"), 3 * typ_of("add 8") + 2 * typ_of("reg 8"))
        self.assertEqual(typ_of("2 * (add 8 + reg 8)"), 2 * typ_of("add 8") + 2 * typ_of("reg 8"))
        self.assertEqual(typ_of("2 * (3 * add 8)"), 6 * typ_of("add 8"))
        r = est.estimate_expression("4 * mul 16x16", P)
        self.assertEqual(r.normalized, "4 * MUL 16x16 unsigned")
        self.assertEqual(r.breakdown, {k: 4 * v for k, v in est.estimate_expression("mul 16x16", P).breakdown.items()})

    def test_count(self):
        one = est.estimate_expression("mul 16x16 + add 8", P)
        four = est.estimate_expression("mul 16x16 + add 8", P, count=4)
        self.assertEqual((four.ge.min, four.ge.typ, four.ge.max), (4 * one.ge.min, 4 * one.ge.typ, 4 * one.ge.max))
        self.assertEqual(four.normalized, "4 * (MUL 16x16 unsigned + ADD 8)")

    def test_invalid_forms_name_the_expected_form(self):
        cases = {
            "mul 16": "mul NxM", "mux 8": "mux K:1 wW", "add x": "add N", "add": "add N", "frob 3": "unknown operation",
            "mux 8:1": "mux K:1 wW", "mux 8:1 64": "mux K:1 wW", "shl 32": "shl N by", "shl 32 by": "shl N by",
            "div 32": "div N/M", "div 32/0": "div N/M", "mul 0x4": "mul NxM", "mem 16": "mem DxW", "rom 8": "rom DxW",
            "reg 8 bogus": "reg N", "reg 0": "reg N", "eq 8 signd": "eq N", "add 8 +": "unexpected end", "": "empty",
            "2 *": "unexpected end", "(add 8": "missing ')'", "add 8)": "unexpected", "mul 16x8 square": "square",
            "pmux 5": "pmux K wW", "decode": "decode S", "decode 30": "at most 24", "mul 16x16 const=3": "constant",
            "add 8 const=abc": "add N", "sum 4": "sum KxW", "pow 8": "exp=E", "mem 16x8 rports=0": "mem DxW", "0 * add 8": "multiplier",
            "div 32 const=0": "div N/M",
        }
        for text, frag in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(GatecostError) as cm:
                    est.estimate_expression(text, P)
                self.assertEqual(cm.exception.code, "BAD_ARGUMENT")
                self.assertIn(frag, cm.exception.message)

    def test_assumptions(self):
        self.assertIn("constant", est.estimate_expression("mul 16 const", P).assumed[0])
        self.assertEqual(est.estimate_expression("mul 16 const=10", P).assumed, ())
        self.assertIn("asynchronous", est.estimate_expression("mem 16x8", P).assumed[0])
        self.assertEqual(est.estimate_expression("mem 16x8 sync_read", P).assumed, ())


class Compare(unittest.TestCase):
    def test_orders_and_reports_savings(self):
        r = est.compare_expressions(["mul 32x32", "mul 32 const=10"], P)
        self.assertEqual((r.best_index, r.other_index, r.overlap), (1, 0, False))
        a, b = (x.ge for x in r.alternatives)
        self.assertEqual((r.saves.min, r.saves.typ, r.saves.max), (a.min - b.min, a.typ - b.typ, a.max - b.max))
        self.assertGreater(r.saves.typ, 0)

    def test_overlapping_ranges_are_flagged_uncertain(self):
        r = est.compare_expressions(["mux 8:1 w32", "pmux 8 w32"], P)
        self.assertTrue(r.overlap)
        self.assertEqual(r.best_index, 1)

    def test_three_alternatives_best_versus_runner_up(self):
        r = est.compare_expressions(["add 32", "add 8", "add 16"], P)
        self.assertEqual((r.best_index, r.other_index), (1, 2))

    def test_ties_keep_the_first(self):
        r = est.compare_expressions(["add 8", "add 8"], P)
        self.assertEqual((r.best_index, r.saves.typ), (0, 0))

    def test_agent_output(self):
        code, out = run("estimate", "--compare", "mul 32x32", "mul 32 const=10")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "COMPARE 2 alternatives basis=typ")
        self.assertTrue(re.fullmatch(r"A \d+ GE \(\d+\.\.\d+\) mul 32x32", lines[1]), lines[1])
        self.assertTrue(re.fullmatch(r"B \d+ GE \(\d+\.\.\d+\) mul 32 const=10", lines[2]), lines[2])
        self.assertRegex(lines[3], r"^BEST B saves \d+ GE vs A ranges=disjoint$")
        _, out = run("estimate", "--compare", "mux 8:1 w32", "pmux 8 w32")
        self.assertIn("ranges=overlap", out)


class Command(unittest.TestCase):
    def test_agent_estimate_lines(self):
        code, out = run("estimate", "mul 32x32")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        typ, lo, hi, conf = headline(lines[0])
        want = est.estimate_expression("mul 32x32", P)
        self.assertEqual(typ, round(want.ge.typ / 1000))
        self.assertLessEqual(lo, typ)
        self.assertLessEqual(typ, hi)
        self.assertEqual(lines[1], "OP MUL 32x32 unsigned")
        self.assertRegex(lines[2], r"^ARCH min=\w+ typ=\w+ max=\w+$")
        self.assertRegex(lines[3], r"^BREAKDOWN (\w+=\d+ ?)+$")
        out.encode("ascii")

    def test_documented_examples(self):
        for expr in ("mul 16x16 + add 32 + reg 32", "4 * mul 16x16", "mux 8:1 w64 + reg 64 enable", "reg 128 sync_reset enable",
                     "mem 256x32 rports=2 wports=1 sync_read", "shl 32 by const", "sar 64 by 6", "decode 5"):
            with self.subTest(expr=expr):
                code, out = run("estimate", expr)
                self.assertEqual(code, 0)
                self.assertTrue(out.startswith("ESTIMATE "))

    def test_matches_python_api(self):
        _, out = run("estimate", "mul 16x16 + add 32 + reg 32")
        typ, _, _, _ = headline(out)
        self.assertEqual(typ, round(typ_of("mul 16x16 + add 32 + reg 32") / 1000))

    def test_count_option(self):
        _, a = run("estimate", "mul 16x16", "--count", "4")
        _, b = run("estimate", "4 * mul 16x16")
        self.assertEqual(a, b)
        code, out = run("estimate", "mul 16x16", "--count", "0")
        self.assertEqual((code, out.split()[:2]), (2, ["ERROR", "BAD_ARGUMENT"]))

    def test_why_line_for_poor_classes(self):
        _, out = run("estimate", "div 32/16")
        self.assertIn("confidence=low", out)
        self.assertIn("WHY low: 100% of delta in div class (poor model quality)", out)
        _, out = run("estimate", "mem 256x32 sync_read")
        self.assertIn("WHY low", out)
        self.assertIn("macro", out)

    def test_quiet_and_verbose(self):
        _, q = run("estimate", "mul 8x8", "--quiet")
        self.assertEqual(len(q.splitlines()), 1)

    def test_json_validates_and_is_stable(self):
        for argv in (("estimate", "mul 32x32"), ("estimate", "--compare", "add 32", "sub 32"), ("estimate", "4 * reg 8 enable")):
            code, out = run(*argv, "--format", "json")
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertEqual(validate_json(doc), [], argv)
            self.assertEqual(doc["command"], "estimate")
            self.assertEqual(doc["profile"]["name"], "generic-asic")
            self.assertEqual(run(*argv, "--format", "json")[1], out)
        doc = json.loads(run("estimate", "mul 32x32", "--format", "json")[1])
        r = doc["result"]
        self.assertEqual(r["input"], {"kind": "op", "text": "mul 32x32"})
        self.assertEqual(r["normalized"], "MUL 32x32 unsigned")
        self.assertLessEqual(r["ge"]["min"], r["ge"]["typ"])
        self.assertIn("range_ratio", r["confidence"])
        self.assertEqual(set(r["architecture"]), {"min", "typ", "max"})
        _, nometa = run("estimate", "mul 32x32", "--format", "json", "--no-meta")
        self.assertNotIn("meta", json.loads(nometa))

    def test_text_and_markdown_formats(self):
        for fmt in ("text", "markdown"):
            code, out = run("estimate", "add 32", "--format", fmt)
            self.assertEqual(code, 0)
            self.assertIn("ESTIMATE", out)

    def test_errors_are_single_lines_with_exit_code_2(self):
        for expr in ("mul 16", "mux 8", "add x"):
            code, out = run("estimate", expr)
            self.assertEqual(code, 2)
            self.assertEqual(len(out.splitlines()), 1)
            self.assertTrue(out.startswith("ERROR BAD_ARGUMENT "))
            self.assertNotIn("Traceback", out)
        code, out = run("estimate", "mul 16", "--format", "json")
        self.assertEqual(json.loads(out)["status"], "error")

    def test_rtl_fragments_are_not_implemented_yet(self):
        for argv in (("--rtl", "assign y = a*b + c;", "--widths", "a=16,b=16,c=32"), ("--rtl", "y <= a + b;", "--lang", "vhdl")):
            code, out = run("estimate", *argv)
            self.assertEqual(code, 3)
            self.assertTrue(out.startswith("ERROR NOT_IMPLEMENTED "), out)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "frag.v"
            f.write_text("assign y = a & b;", encoding="utf-8")
            code, out = run("estimate", "--rtl-file", str(f))
            self.assertEqual((code, out.split()[1]), (3, "NOT_IMPLEMENTED"))
            code, out = run("estimate", "--rtl-file", str(Path(d) / "missing.v"))
            self.assertEqual((code, out.split()[1]), (2, "BAD_ARGUMENT"))
        code, out = run("estimate", "--rtl", "assign y = a;", "--widths", "a")
        self.assertEqual((code, out.split()[1]), (2, "BAD_ARGUMENT"))

    def test_rtl_hook(self):
        from gatecost.result import Confidence, EstimateResult, Ge3

        def fake(fragment, widths, lang, profile):
            return EstimateResult("rtl", fragment, "FRAGMENT", Ge3(1000, 2000, 3000), {"min": "x", "typ": "x", "max": "x"}, {"AND2": 1},
                                  Confidence("high"))

        est.RTL_ESTIMATOR = fake
        try:
            code, out = run("estimate", "--rtl", "assign y = a & b;")
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith("ESTIMATE 2 GE (1..3)"))
        finally:
            est.RTL_ESTIMATOR = None

    def test_works_from_any_directory_without_a_project(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run("estimate", "add 32", cwd=d)
            self.assertEqual(code, 0)
            self.assertEqual(out, run("estimate", "add 32")[1])

    def test_deterministic(self):
        a = run("estimate", "mul 16x16 + add 32 + reg 32 + mem 64x8 + rom 32x4")
        self.assertEqual(a, run("estimate", "mul 16x16 + add 32 + reg 32 + mem 64x8 + rom 32x4"))


class ProfileSelection(unittest.TestCase):
    def test_profile_option_and_environment(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "big-fa.toml"
            f.write_text('extends = "generic-asic"\n[meta]\nname = "big-fa"\n[primitives]\nFA = 10.0\n', encoding="utf-8")
            base = headline(run("estimate", "add 32")[1])[0]
            via_opt = headline(run("estimate", "add 32", "--profile", str(f))[1])[0]
            self.assertGreater(via_opt, base)
            os.environ["GATECOST_PROFILE"] = str(f)
            try:
                self.assertEqual(headline(run("estimate", "add 32")[1])[0], via_opt)
            finally:
                del os.environ["GATECOST_PROFILE"]
            doc = json.loads(run("estimate", "add 32", "--profile", str(f), "--format", "json")[1])
            self.assertEqual(doc["profile"]["name"], "big-fa")
            code, out = run("estimate", "add 32", "--profile", "nope")
            self.assertEqual((code, out.split()[:2]), (2, ["ERROR", "BAD_CONFIG"]))

    def test_cost_override_from_the_project_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "gatecost.toml").write_text('[cost.override.primitives]\nDFF = 10.0\n', encoding="utf-8")
            plain = headline(run("estimate", "reg 10", cwd=d, )[1])[0]
            self.assertEqual(plain, 100)
            (Path(d) / "sub").mkdir()
            self.assertEqual(headline(run("estimate", "reg 10", cwd=Path(d) / "sub")[1])[0], 100)  # found upwards
        self.assertEqual(headline(run("estimate", "reg 10")[1])[0], 57)


class Profiles(unittest.TestCase):
    def test_list(self):
        code, out = run("profiles")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^PROFILE generic-asic ")
        doc = json.loads(run("profiles", "--format", "json")[1])
        self.assertEqual(validate_json(doc), [])
        self.assertEqual(doc["result"]["profiles"][0]["name"], "generic-asic")

    def test_show_prints_resolved_toml(self):
        code, out = run("profiles", "show", "generic-asic")
        self.assertEqual(code, 0)
        doc = __import__("tomllib").loads(out)
        self.assertEqual(doc["meta"]["name"], "generic-asic")
        self.assertEqual(doc["primitives"]["FA"], 5.33)
        self.assertIn("coefficients", doc)

    def test_show_applies_extends(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.toml"
            f.write_text('extends = "generic-asic"\n[meta]\nname = "x"\n[primitives]\nFA = 6.5\n', encoding="utf-8")
            code, out = run("profiles", "show", str(f))
        doc = __import__("tomllib").loads(out)
        self.assertEqual((code, doc["primitives"]["FA"], doc["primitives"]["HA"], "extends" in doc), (0, 6.5, 3.33, False))

    def test_unknown_profile(self):
        code, out = run("profiles", "show", "nope")
        self.assertEqual((code, out.split()[:2]), (2, ["ERROR", "BAD_CONFIG"]))
        self.assertEqual(run("profiles", "show")[0], 2)


if __name__ == "__main__":
    unittest.main()
