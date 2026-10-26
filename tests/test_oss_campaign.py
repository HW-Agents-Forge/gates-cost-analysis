"""scripts/oss_campaign.py: pinned list, command construction, declarative edits and summaries. No network, no Yosys, no clone."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from gatecost import REPO_ROOT


def _load(name):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


oc = _load("oss_campaign")


class PinnedListTests(unittest.TestCase):
    def test_projects_are_pinned_and_unique(self):
        projects = oc.load_projects()
        self.assertGreaterEqual(len(projects), 13)
        for p in projects:
            self.assertRegex(p["commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(p["url"].startswith("https://github.com/"))
            self.assertTrue(p["smoke"], p["name"])
        names = {p["name"] for p in projects}
        for want in ("picorv32", "verilog-axis", "ibex", "neorv32", "open-logic", "common_cells", "serv"):
            self.assertIn(want, names)

    def test_no_absolute_user_paths_in_data_files(self):
        for f in (oc.PROJECTS_FILE, oc.DELTAS_FILE):
            self.assertNotIn("/Users/", f.read_text(encoding="utf-8"))

    def test_deltas_reference_known_projects_and_are_well_formed(self):
        projects = {p["name"] for p in oc.load_projects()}
        deltas = oc.load_deltas()
        self.assertGreaterEqual(len(deltas), 30)
        for d in deltas:
            self.assertIn(d["project"], projects)
            self.assertTrue(d["edit"])
            for ops in d["edit"].values():
                for op in ops:
                    self.assertTrue("replace" in op or "insert_before_last" in op)

    def test_bad_projects_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "p.json"
            base = {"name": "x", "url": "https://h/x.git", "commit": "a" * 40, "lang": "verilog", "smoke": []}
            for patch in ({"commit": "abc"}, {"url": "git@h:x.git"}):
                f.write_text(json.dumps({"projects": [{**base, **patch}]}))
                with self.assertRaises(oc.CampaignError):
                    oc.load_projects(f)
            f.write_text(json.dumps({"projects": [base, base]}))
            with self.assertRaises(oc.CampaignError):
                oc.load_projects(f)

    def test_oss_dir_follows_environment(self):
        import os
        old = os.environ.get("GATECOST_OSS_DIR")
        os.environ["GATECOST_OSS_DIR"] = "/data/oss"
        try:
            p = {"dir": "picorv32"}
            self.assertEqual(oc.repo_dir(p), Path("/data/oss/picorv32/repo"))
        finally:
            if old is None:
                del os.environ["GATECOST_OSS_DIR"]
            else:
                os.environ["GATECOST_OSS_DIR"] = old


class CommandTests(unittest.TestCase):
    P = {"name": "p", "url": "https://github.com/o/p.git", "commit": "b" * 40, "dir": "p"}

    def test_fetch_commands_checkout_the_pinned_commit_without_hooks(self):
        cmds = oc.fetch_commands(self.P, Path("/r/p/repo"))
        self.assertEqual(len(cmds), 3)
        self.assertIn("clone", cmds[0])
        self.assertIn("--depth", cmds[0])
        self.assertEqual(cmds[1][-2:], ["origin", "b" * 40])
        self.assertEqual(cmds[2][-2:], ["--detach", "b" * 40])
        for c in cmds:
            self.assertIn("core.hooksPath=/dev/null", c)

    def test_cost_command_with_incdir_param_and_define(self):
        t = {"top": "t", "sources": ["src", "a.sv"], "incdirs": ["include"], "params": ["W=8", "N=2"], "defines": {"B": None, "V": "1"}, "root": "src"}
        cmd = oc.cost_command(self.P, t, Path("/r/p/repo"), Path("/g/gatecost"))
        self.assertEqual(cmd[1:5], ["/g/gatecost", "cost", "--format", "json"])
        self.assertIn("--no-cache", cmd)
        self.assertEqual(cmd[cmd.index("--top") + 1], "t")
        self.assertEqual(cmd[cmd.index("--root") + 1], "/r/p/repo/src")
        self.assertEqual(cmd[cmd.index("--incdir") + 1], "/r/p/repo/include")
        self.assertEqual([cmd[i + 1] for i, c in enumerate(cmd) if c == "--param"], ["W=8", "N=2"])
        self.assertEqual([cmd[i + 1] for i, c in enumerate(cmd) if c == "--define"], ["B", "V=1"])
        self.assertEqual(cmd[-2:], ["/r/p/repo/src", "/r/p/repo/a.sv"])

    def test_filelist_target_expands_sources_and_incdirs(self):
        p = {p["name"]: p for p in oc.load_projects()}["ibex"]
        sources, incdirs = oc.resolve_target(p, p["smoke"][0], Path("/r/ibex/repo"))
        self.assertIn("/r/ibex/repo/rtl/ibex_top.sv", sources)
        self.assertTrue(incdirs and all(i.startswith("/r/ibex/repo/vendor") for i in incdirs))
        self.assertTrue(all(s.startswith("/r/ibex/repo/") for s in sources))

    def test_every_smoke_target_builds_a_command(self):
        for p in oc.load_projects():
            for t in p["smoke"]:
                cmd = oc.cost_command(p, t, Path("/r") / p["dir"] / "repo")
                self.assertIn(t["top"], cmd)

    def test_diff_command(self):
        cmd = oc.diff_command({"top": "t", "params": ["A=1"]}, Path("/b"), Path("/a"), Path("/g"))
        self.assertEqual(cmd[2:5], ["diff", "--no-cache", "--format"])
        self.assertEqual(cmd[cmd.index("--before") + 1], "/b")
        self.assertEqual(cmd[cmd.index("--after") + 1], "/a")
        self.assertEqual(cmd[cmd.index("--param") + 1], "A=1")


class EditTests(unittest.TestCase):
    def test_replace_first_and_all(self):
        self.assertEqual(oc.apply_ops("a a a", [{"replace": ["a", "b"]}]), "b a a")
        self.assertEqual(oc.apply_ops("a a a", [{"replace": ["a", "b"], "count": 0}]), "b b b")

    def test_insert_before_last_marker(self):
        out = oc.apply_ops("module a;\nendmodule\nmodule b;\nendmodule\n", [{"insert_before_last": "endmodule", "code": "wire x;"}])
        self.assertEqual(out, "module a;\nendmodule\nmodule b;\nwire x;\nendmodule\n")

    def test_stale_pattern_is_loud(self):
        with self.assertRaises(oc.CampaignError):
            oc.apply_ops("abc", [{"replace": ["zzz", "y"]}])
        with self.assertRaises(oc.CampaignError):
            oc.apply_ops("abc", [{"insert_before_last": "endmodule", "code": ""}])
        with self.assertRaises(oc.CampaignError):
            oc.apply_ops("abc", [{"bogus": 1}])

    def test_build_trees_applies_pre_to_both_and_edit_to_after(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            repo = d / "repo"
            (repo / "rtl").mkdir(parents=True)
            (repo / "rtl" / "m.v").write_text("parameter W = 8;\nparameter D = 4;\n")
            delta = {"name": "x", "srcsub": "rtl", "files": ["m.v"], "pre": {"m.v": [{"replace": ["D = 4", "D = 5"]}]},
                     "edit": {"m.v": [{"replace": ["W = 8", "W = 16"]}]}}
            before, after = oc.build_trees(delta, repo, d / "work")
            self.assertEqual((before / "m.v").read_text(), "parameter W = 8;\nparameter D = 5;\n")
            self.assertEqual((after / "m.v").read_text(), "parameter W = 16;\nparameter D = 5;\n")
            delta["edit"] = {"m.v": [{"replace": ["W = 8", "W = 8"]}]}
            with self.assertRaises(oc.CampaignError):
                oc.build_trees(delta, repo, d / "work")


COST_OK = json.dumps({"status": "partial", "diagnostics": [{"code": "IGNORED_REGION"}, {"code": "PARSE_ERROR"}, {"code": "PARSE_ERROR"}],
                      "result": {"total": {"ge": {"min": 1.04, "typ": 2.0, "max": 3.0}}, "confidence": {"level": "low"}}})


def _run(stdout="", stderr="", rc=0, timeout=False):
    return {"rc": rc, "stdout": stdout, "stderr": stderr, "seconds": 1.5, "rss_mb": 80.0, "timeout": timeout}


class ClassifyTests(unittest.TestCase):
    def test_ok(self):
        r = oc.classify_cost(_run(COST_OK))
        self.assertEqual((r["status"], r["typ_ge"], r["confidence"]), ("ok", 2.0, "low"))
        self.assertEqual(r["diagnostics"], {"IGNORED_REGION": 1, "PARSE_ERROR": 2})

    def test_internal_crash_timeout_error(self):
        self.assertEqual(oc.classify_cost(_run(json.dumps({"status": "error", "error": {"code": "INTERNAL", "message": "boom"}})))["status"], "internal")
        self.assertEqual(oc.classify_cost(_run(json.dumps({"status": "error", "error": {"code": "TOP_NOT_FOUND", "message": "x"}})))["status"], "error")
        self.assertEqual(oc.classify_cost(_run(stderr="Traceback (most recent call last):\nValueError: x"))["status"], "crash")
        self.assertEqual(oc.classify_cost(_run("not json"))["status"], "crash")
        self.assertEqual(oc.classify_cost(_run(timeout=True))["status"], "timeout")
        internal_diag = json.dumps({"status": "partial", "diagnostics": [{"code": "INTERNAL"}], "result": {"total": {"ge": {}}, "confidence": {}}})
        self.assertEqual(oc.classify_cost(_run(internal_diag))["status"], "internal")

    def test_parse_diff_and_truth(self):
        out = json.dumps({"status": "ok", "result": {"verdict": "INCREASE", "delta": {"ge": {"min": 100, "typ": 200, "max": 400}},
                                                     "before": {"ge": {"typ": 10}}, "after": {"ge": {"typ": 210}}, "confidence": {"level": "medium"}}})
        row = oc.parse_diff(_run(out))
        self.assertEqual((row["status"], row["verdict"], row["typ"]), ("ok", "INCREASE", 200))
        oc.add_truth(row, 50.0, 300.0)
        self.assertEqual((row["ys_delta"], row["in_range"], row["ratio"]), (250.0, True, 0.8))
        oc.add_truth(row, 50.0, 700.0)
        self.assertFalse(row["in_range"])
        oc.add_truth(row, "FAIL x", 700.0)
        self.assertEqual(row["ys_before"], "FAIL x")


SMOKE_ROWS = [
    {"project": "b", "top": "t2", "status": "ok", "confidence": "low", "min_ge": 1.0, "typ_ge": 2.0, "max_ge": 3.0, "seconds": 1.5, "rss_mb": 80.0,
     "diagnostics": {"IGNORED_REGION": 9, "PARSE_ERROR": 1}},
    {"project": "a", "top": "t1", "status": "crash", "detail": "ValueError: x | y"},
]
DELTA_ROWS = [
    {"name": "z", "status": "ok", "verdict": "INCREASE", "min": 1.0, "typ": 2.0, "max": 3.0, "confidence": "low", "ys_delta": 2.5, "in_range": True, "ratio": 0.8},
    {"name": "y", "status": "ok", "verdict": "DECREASE", "min": -3.0, "typ": -2.0, "max": -1.0, "confidence": "low", "ys_delta": 5.0, "in_range": False, "ratio": -0.4},
]


class SummaryTests(unittest.TestCase):
    def test_smoke_markdown_and_csv_are_sorted_and_deterministic(self):
        md, cs = oc.render("smoke", SMOKE_ROWS, timing=False)
        self.assertEqual(md, oc.render("smoke", list(reversed(SMOKE_ROWS)), timing=False)[0])
        self.assertIn("Status: crash 1, ok 1", md)
        self.assertLess(md.index("| a | t1"), md.index("| b | t2"))
        self.assertNotIn("rss_mb", md)
        self.assertIn("PARSE_ERROR=1", cs)
        self.assertNotIn("IGNORED_REGION", cs)  # noise diagnostics are not listed
        self.assertEqual(cs.splitlines()[0], "project,top,status,confidence,min_ge,typ_ge,max_ge,diagnostics,detail")
        self.assertIn("ValueError: x / y", md)  # pipe escaped in Markdown

    def test_timing_columns_optional(self):
        self.assertIn("rss_mb", oc.render("smoke", SMOKE_ROWS, timing=True)[1].splitlines()[0])

    def test_deltas_summary_counts_in_range(self):
        md, cs = oc.render("deltas", DELTA_ROWS)
        self.assertIn("Truth comparison: 1/2 Yosys deltas inside the reported range", md)
        self.assertEqual(cs.splitlines()[1].split(",")[0], "y")
        self.assertIn(",yes,0.8,", cs)

    def test_write_and_regenerate(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with contextlib.redirect_stdout(io.StringIO()):
                oc.write_outputs(out, "smoke", SMOKE_ROWS, no_timing=True)
            first = (out / "smoke.md").read_text()
            (out / "smoke.md").unlink()
            args = oc.build_parser().parse_args(["summary", "--out", d, "--no-timing"])
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(oc.cmd_summary(args), 0)
            self.assertEqual((out / "smoke.md").read_text(), first)


class MeasuredRunTests(unittest.TestCase):
    def test_run_measured_collects_output_and_timeout(self):
        r = oc.run_measured([sys.executable, "-c", "print('hi')"], 30)
        self.assertEqual((r["rc"], r["stdout"].strip(), r["timeout"]), (0, "hi", False))
        self.assertGreater(r["rss_mb"], 0)
        t = oc.run_measured([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
        self.assertTrue(t["timeout"])


if __name__ == "__main__":
    unittest.main()
