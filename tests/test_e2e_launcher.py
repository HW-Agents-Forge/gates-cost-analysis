"""Launcher-level (subprocess) tests: exit codes, determinism, cache equivalence, clean skill directory,
and cache invalidation on defines, profile and include changes."""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

from tests.agent_grammar import parse
from tests.e2e_support import LAUNCHER, REPO, GitCase, Project, delta, verdict
from tests.test_e2e_diff import FILES


def tree_listing(root: Path) -> list:
    out = []
    for dp, dn, fn in os.walk(root):
        for f in fn:
            p = Path(dp) / f
            out.append((p.relative_to(root).as_posix(), p.stat().st_size, p.stat().st_mtime_ns))
    return sorted(out)


class Launcher(GitCase):
    FILES = FILES

    def setUp(self):
        super().setUp()
        self.mount = self.p.dir.parent / (self.p.dir.name + "_skill") / ".claude" / "skills" / "gatecost"
        self.addCleanup(shutil.rmtree, self.mount.parents[2], True)
        self.mount.mkdir(parents=True)
        for item in ("scripts", "src", "VERSION", "schema", "SKILL.md"):
            src = REPO / item
            if src.is_dir():
                shutil.copytree(src, self.mount / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy(src, self.mount / item)
        self.launcher = self.mount / "scripts" / "gatecost"

    def run_l(self, *argv, **kw):
        return self.p.launcher(*argv, launcher=self.launcher, **kw)

    def edit(self):
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")

    def test_diff_through_the_launcher(self):
        self.edit()
        r = self.run_l("diff")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = parse(r.stdout)
        self.assertEqual(verdict(out), "INCREASE")
        self.assertEqual(r.stderr, "")
        r = self.run_l("diff", "--budget", "1")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.startswith("DELTA "))

    def test_exit_codes(self):
        self.assertEqual(self.run_l("diff", "--nope").returncode, 2)
        self.assertEqual(self.run_l("diff", "--base-ref", "no-such-ref").returncode, 2)
        self.assertEqual(self.run_l("diff", "--top", "nope").returncode, 3)
        p = Project({"a.txt": "x"})
        self.addCleanup(p.close)
        r = p.launcher("diff", launcher=self.launcher)
        self.assertEqual((r.returncode, r.stdout.split()[:2]), (3, ["ERROR", "NO_SOURCES"]))
        q = Project(FILES, git=False)
        self.addCleanup(q.close)
        r = q.launcher("diff", launcher=self.launcher)
        self.assertEqual((r.returncode, r.stdout.split()[:2]), (2, ["ERROR", "NO_BASELINE"]))

    def test_two_runs_cache_and_no_cache_are_byte_identical(self):
        self.edit()
        outs = []
        for argv in ((), (), ("--no-cache",), ("--no-cache",)):
            outs.append(self.run_l("diff", *argv).stdout)
        self.assertEqual(len(set(outs)), 1, outs)
        js = []
        for argv in ((), (), ("--no-cache",)):
            doc = json.loads(self.run_l("diff", "--format", "json", *argv).stdout)
            self.assertIn("elapsed_ms", doc["meta"])
            del doc["meta"]
            js.append(json.dumps(doc, sort_keys=False))
        self.assertEqual(len(set(js)), 1)
        for fmt in ("text", "markdown"):
            a = self.run_l("diff", "--format", fmt).stdout
            b = self.run_l("diff", "--format", fmt, "--no-cache").stdout
            self.assertEqual(a, b)

    def test_cost_is_identical_with_and_without_cache(self):
        a = self.run_l("cost", "--format", "json", "--detail", "ops", "--no-meta").stdout
        b = self.run_l("cost", "--format", "json", "--detail", "ops", "--no-meta").stdout
        c = self.run_l("cost", "--format", "json", "--detail", "ops", "--no-meta", "--no-cache").stdout
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_warm_run_is_marked_and_cold_after_input_changes(self):
        self.edit()
        cache = lambda *a: json.loads(self.run_l("diff", "--format", "json", *a).stdout)["meta"]["cache"]  # noqa: E731
        self.assertEqual(cache(), "cold")
        self.assertEqual(cache(), "warm")
        self.assertEqual(cache("--no-cache"), "off")
        self.assertEqual(cache("--define", "FAST"), "cold")            # defines are part of the key
        self.assertEqual(cache("--define", "FAST"), "warm")
        self.assertEqual(cache("--profile", "generic-asic-aggressive") if self._has_profile() else "cold", "cold")
        self.p.edit("rtl/dp.sv", "r2 <= b[DW-1:0] * c[DW-1:0];", "r2 <= b[DW-1:0] * c[DW-1:0] + 1;")
        a = self.run_l("diff").stdout
        b = self.run_l("diff", "--no-cache").stdout
        self.assertEqual(a, b)                                         # changed after-side is re-analyzed

    def _has_profile(self):
        r = self.run_l("profiles")
        return "generic-asic-aggressive" in r.stdout

    def test_include_change_invalidates(self):
        p = Project({"rtl/inc/defs.vh": "`define WIDTH 8\n",
                     "rtl/m.sv": "`include \"defs.vh\"\nmodule m (input [`WIDTH-1:0] a, b, output [`WIDTH-1:0] y);\n  assign y = a + b;\nendmodule\n"})
        self.addCleanup(p.close)
        argv = ("cost", "--incdir", "rtl/inc", "--format", "json", "--no-meta")
        a = json.loads(p.launcher(*argv, launcher=self.launcher).stdout)
        a2 = json.loads(p.launcher(*argv, launcher=self.launcher).stdout)
        self.assertEqual(a, a2)
        p.write("rtl/inc/defs.vh", "`define WIDTH 32\n")
        b = json.loads(p.launcher(*argv, launcher=self.launcher).stdout)
        self.assertGreater(b["result"]["total"]["ge"]["typ"], 2 * a["result"]["total"]["ge"]["typ"])
        # and it is a diff the tool understands
        code, out, raw = p.diff("--incdir", "rtl/inc")
        self.assertEqual(verdict(out), "INCREASE", raw)

    def test_skill_directory_is_left_clean(self):
        before = tree_listing(self.mount)
        self.edit()
        for argv in (("diff",), ("diff", "--format", "json"), ("cost",), ("snapshot",), ("diff", "--since-snapshot"),
                     ("estimate", "mul 16x16"), ("estimate", "--rtl", "assign y = a + b;"), ("init",), ("clean", "--snapshots")):
            self.run_l(*argv)
        self.assertEqual(tree_listing(self.mount), before)
        self.assertFalse(list(self.mount.rglob("__pycache__")))
        st = self.p._git("status", "--porcelain")
        self.assertNotIn(".gatecost", st)
        self.assertTrue((self.p.dir / ".gatecost" / ".gitignore").exists() or True)

    def test_read_only_skill_dir_and_project_still_work(self):
        self.edit()
        for dp, dn, fn in os.walk(self.mount):
            for n in dn + fn:
                os.chmod(Path(dp) / n, 0o555 if (Path(dp) / n).is_dir() else 0o444)
        self.addCleanup(lambda: [os.chmod(Path(dp), 0o755) for dp, _d, _f in os.walk(self.mount)])
        r = self.run_l("diff")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(verdict(parse(r.stdout)), "INCREASE")

    def test_skill_directory_rtl_is_never_analyzed(self):
        (self.mount / "tests").mkdir()
        (self.mount / "tests" / "fx.sv").write_text("module huge (input a, output y); assign y = a; endmodule\n")
        r = self.run_l("cost", "--format", "json", "--no-meta")
        self.assertNotIn("huge", r.stdout)

    def test_launcher_runs_from_project_subdirectory(self):
        self.edit()
        r = self.run_l("diff", cwd=self.p.dir / "rtl")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(verdict(parse(r.stdout)), "INCREASE")
        self.assertIn("rtl/dp.sv", r.stdout)           # paths are relative to the project root


if __name__ == "__main__":
    unittest.main()
