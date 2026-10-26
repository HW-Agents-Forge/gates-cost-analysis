"""Reliability notes of the reports: implicit discovery mixing variants, and ignored regions."""
from __future__ import annotations

import json
import unittest

from gatecost.cli.notes import explain_confidence
from gatecost.report.fmt import ignored_kind, note_lines
from gatecost.result import Confidence, Diagnostic
from tests.agent_grammar import parse
from tests.e2e_support import GitCase, Project

MAC_A = "module mac(input clk, input [7:0] a, input [7:0] b, output reg [15:0] y);\n" \
        "  always @(posedge clk) y <= a * b;\nendmodule\n"
MAC_B = "module mac(input clk, input [7:0] a, input [7:0] b, output reg [15:0] y);\n" \
        "  reg [15:0] p;\n  always @(posedge clk) begin p <= a * b; y <= p; end\nendmodule\n"
TWO_VARIANTS = {"baseline/mac.sv": MAC_A, "pipelined/mac.sv": MAC_B}
IGN = ("module a(input clk, input [7:0] x, output reg [7:0] y);\n  initial y = 0;\n"
       "  always @(posedge clk) begin\n    y <= x + 1;\n    assert (x != 8'hff);\n  end\nendmodule\n")


class DiscoveryDuplicates(unittest.TestCase):
    def setUp(self) -> None:
        self.p = Project(TWO_VARIANTS, git=False)
        self.addCleanup(self.p.close)

    def test_agent_warns_and_reason_names_duplicate_module(self):
        code, text = self.p.run("cost")
        self.assertEqual(code, 0)
        rec = parse(text)
        warns = [r.groups[0] for r in rec.of("WARN")]
        self.assertEqual(warns[0], "DISCOVERY_DUPLICATES")
        self.assertIn("DUPLICATE_MODULE", warns)
        line = next(r.line for r in rec.of("WARN") if r.groups[0] == "DISCOVERY_DUPLICATES")
        for needle in ("1 duplicate", "module 'mac'", "baseline/mac.sv", "pipelined/mac.sv", "kept pipelined/mac.sv",
                       "--top", "--filelist"):
            self.assertIn(needle, line)
        why = next(r.line for r in rec.of("WHY"))
        self.assertIn("DUPLICATE_MODULE", why)
        self.assertNotIn("unsupported construct", why)

    def test_text_and_markdown_show_it(self):
        for fmt in ("text", "markdown"):
            with self.subTest(fmt=fmt):
                _, text = self.p.run("cost", "--format", fmt)
                self.assertIn("WARN DISCOVERY_DUPLICATES", text)
                self.assertIn("DUPLICATE_MODULE", text)

    def test_json_has_the_diagnostic(self):
        _, text = self.p.run("cost", "--format", "json", "--no-meta")
        doc = json.loads(text)
        d = [x for x in doc["diagnostics"] if x["code"] == "DISCOVERY_DUPLICATES"]
        self.assertEqual(len(d), 1)
        self.assertTrue(d[0]["affects_cost"])
        self.assertIn("DUPLICATE_MODULE", doc["result"]["confidence"]["reasons"][0])

    def test_single_variant_and_explicit_path_have_no_discovery_warning(self):
        for argv in (["cost", "pipelined"], ["cost", "baseline"]):
            with self.subTest(argv=argv):
                _, text = self.p.run(*argv)
                self.assertNotIn("DISCOVERY_DUPLICATES", text)
                self.assertNotIn("DUPLICATE_MODULE", text)

    def test_explicit_paths_with_duplicates_name_duplicate_module_but_no_discovery_warning(self):
        _, text = self.p.run("cost", "baseline/mac.sv", "pipelined/mac.sv")
        self.assertNotIn("DISCOVERY_DUPLICATES", text)
        self.assertIn("WHY low: 1 DUPLICATE_MODULE", text)


class DiffDiscoveryDuplicates(GitCase):
    FILES = TWO_VARIANTS

    def test_diff_warns(self):
        self.p.edit("pipelined/mac.sv", "p <= a * b;", "p <= a * b + 1;")
        code, parsed, text = self.p.diff()
        self.assertEqual(code, 0)
        self.assertEqual(parsed.of("WARN")[0].groups[0], "DISCOVERY_DUPLICATES")
        why = [r.line for r in parsed.of("WHY")]
        self.assertTrue(not why or "unsupported construct" not in why[0], text)


class IgnoredRegions(unittest.TestCase):
    def test_cost_note_in_every_human_format(self):
        p = Project({"a.sv": IGN}, git=False)
        self.addCleanup(p.close)
        code, text = p.run("cost")
        self.assertEqual(code, 0)
        rec = parse(text)
        self.assertEqual(rec.records[-1].kind, "NOTE")
        self.assertEqual(rec.records[-1].line, "NOTE ignored regions: 2 (assert, initial)")
        for fmt in ("text", "markdown"):
            with self.subTest(fmt=fmt):
                self.assertIn("NOTE ignored regions: 2 (assert, initial)", p.run("cost", "--format", fmt)[1])
        self.assertNotIn("NOTE", p.run("cost", "--quiet")[1])

    def test_no_note_without_ignored_region(self):
        p = Project({"a.sv": MAC_A}, git=False)
        self.addCleanup(p.close)
        self.assertNotIn("NOTE", p.run("cost")[1])

    def test_diff_note(self):
        p = Project({"a.sv": IGN})
        self.addCleanup(p.close)
        p.edit("a.sv", "x + 1", "x + 2")
        _, parsed, _ = p.diff()
        self.assertEqual(parsed.records[-1].line, "NOTE ignored regions: 2 (assert, initial)")


class Units(unittest.TestCase):
    def test_ignored_kind(self):
        for msg, kind in (("initial block: only the contents of memories are used", "initial"),
                          ("assert statement ignored", "assert"), ("class declaration ignored", "class"),
                          ("translate_off region (lines 1-2) excluded", "translate_off"),
                          ("always block without an event control is not synthesizable; skipped", "always"),
                          ("", "other")):
            self.assertEqual(ignored_kind(msg), kind)

    def test_note_lines_caps_kinds(self):
        ds = tuple(Diagnostic("IGNORED_REGION", f"k{i} ignored", "info", False, line=i) for i in range(6))
        self.assertEqual(note_lines(ds), ["NOTE ignored regions: 6 (k0, k1, k2, k3, +2 more)"])
        self.assertEqual(note_lines(()), [])

    def test_explain_confidence_mixed(self):
        dup = Diagnostic("DUPLICATE_MODULE", "module 'm' redefined", "warning", True, line=1)
        c = explain_confidence(Confidence("low", ("3 unsupported constructs in the changed cone",)), [dup])
        self.assertEqual(c.reasons, ("3 unsupported constructs in the changed cone, including 1 DUPLICATE_MODULE",))
        self.assertEqual(explain_confidence(Confidence("low", ("2 unsupported constructs in the changed cone",)), []).reasons,
                         ("2 unsupported constructs in the changed cone",))


if __name__ == "__main__":
    unittest.main()
