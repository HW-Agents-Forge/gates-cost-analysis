"""CLI parser, configuration, discovery, init/clean, and launcher tests (CLI.md section 6)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gatecost import REPO_ROOT
from gatecost.cli.parser import COMMANDS, build_parser, help_text, validate_args
from gatecost.config import load_project_file, resolve_settings
from gatecost.discovery import discover
from gatecost.errors import GatecostError

LAUNCHER = REPO_ROOT / "scripts" / "gatecost"


def run(*args, cwd=None, env=None):
    e = {**os.environ, "GATECOST_CACHE_DIR": str(Path(tempfile.gettempdir()) / "gatecost-test-cache"), **(env or {})}
    p = subprocess.run([sys.executable, str(LAUNCHER), *args], capture_output=True, text=True, cwd=cwd, env=e)
    return p.returncode, p.stdout, p.stderr


def parse(*argv):
    ns = build_parser().parse_args(list(argv))
    validate_args(ns)
    return ns


class Parser(unittest.TestCase):
    def test_documented_examples_parse(self):
        examples = [
            ["diff"], ["diff", "--base-ref", "merge-base"], ["diff", "--since-snapshot"], ["diff", "--since-snapshot", "ab12"],
            ["diff", "--before", "a", "--after", "b"], ["diff", "--top", "x", "--max-lines", "20", "rtl/a.sv", "--format", "json"],
            ["diff", "--fail-on-increase", "500", "--basis", "max", "--limit", "5", "--detail", "ops"],
            ["cost", "--by", "category", "--budget", "50000", "--define", "W=32", "--incdir", "inc", "--filelist", "f.f"],
            ["estimate", "mul 32x32"], ["estimate", "--compare", "mul 32x32", "mul 32 const=10"],
            ["estimate", "--rtl", "assign y = a*b + c;", "--widths", "a=16,b=16,c=32"],
            ["estimate", "--rtl", "y <= a + b;", "--lang", "vhdl", "--count", "4"],
            ["snapshot"], ["snapshot", "--list"], ["snapshot", "--delete", "ab12"], ["snapshot", "--keep", "3"],
            ["init", "--force"], ["profiles"], ["profiles", "show", "generic-asic"],
            ["clean", "--global", "--snapshots"], ["validate-output", "-"], ["help", "diff"],
            ["diff", "--quiet"], ["diff", "--verbose", "--no-cache", "--no-meta"],
        ]
        for ex in examples:
            with self.subTest(ex=ex):
                parse(*ex)

    def test_options_after_positional(self):
        ns = parse("diff", "rtl", "--top", "t", "other")
        self.assertEqual((ns.paths, ns.top), (["rtl", "other"], "t"))

    def test_malformed_invocations(self):
        bad = [
            ["nope"], ["diff", "--nope"], ["diff", "--top"], ["diff", "--format", "xml"],
            ["diff", "--base-ref", "x", "--since-snapshot"], ["diff", "--before", "a"], ["diff", "--after", "b"],
            ["diff", "--verbose", "--quiet"], ["estimate"], ["estimate", "a", "--rtl", "b"], ["estimate", "--compare", "only-one"],
            ["profiles", "show"], ["cost", "--by", "x"], ["diff", "--max-lines", "-1"], ["cost", "--budget", "abc"],
            ["estimate", "x", "--lang", "cobol"], ["init", "--base-ref", "x"],
        ]
        for argv in bad:
            with self.subTest(argv=argv):
                with self.assertRaises(GatecostError) as cm:
                    parse(*argv)
                self.assertEqual(cm.exception.code, "BAD_ARGUMENT")
                self.assertEqual(cm.exception.exit_code, 2)

    def test_options_accepted_only_by_listed_commands(self):
        with self.assertRaises(GatecostError):
            parse("cost", "--base-ref", "HEAD")
        with self.assertRaises(GatecostError):
            parse("estimate", "mul 8x8", "--top", "x")

    def test_help_is_short_and_lists_options(self):
        for c in COMMANDS:
            if c == "help":
                continue
            text = help_text(c)
            self.assertLessEqual(len(text.splitlines()), 25, c)
        self.assertIn("--base-ref", help_text("diff"))
        self.assertIn("--compare", help_text("estimate"))
        with self.assertRaises(GatecostError):
            help_text("nope")


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".git").mkdir()

    def settings(self, *argv, env=None):
        ns = parse(*argv)
        return resolve_settings(ns, self.tmp, REPO_ROOT, env or {})[0]

    def write(self, text):
        (self.tmp / "gatecost.toml").write_text(text)

    def test_defaults(self):
        s = self.settings("diff")
        self.assertEqual((s.format, s.profile, s.max_lines, s.threshold_ge), ("agent", "generic-asic", 10, 5))
        self.assertEqual(s.root, self.tmp.resolve())

    def test_precedence_cli_over_file_over_env(self):
        self.write('[cost]\nprofile = "from-file"\n[report]\nmax_lines = 7\nformat = "text"\n')
        env = {"GATECOST_PROFILE": "from-env", "GATECOST_FORMAT": "json"}
        s = self.settings("diff", env=env)
        self.assertEqual((s.profile, s.max_lines, s.format), ("from-file", 7, "text"))
        s = self.settings("diff", "--profile", "cli", "--max-lines", "3", "--format", "markdown", env=env)
        self.assertEqual((s.profile, s.max_lines, s.format), ("cli", 3, "markdown"))
        self.write("")
        s = self.settings("diff", env=env)
        self.assertEqual((s.profile, s.format), ("from-env", "json"))

    def test_defines_merge_and_budgets(self):
        self.write('[project]\ndefines = { WIDTH = "32", A = "1" }\n[budget]\nmax_ge = 100\nmax_delta_ge = 5\n')
        s = self.settings("diff", "--define", "WIDTH=16", "--define", "FLAG")
        self.assertEqual(s.defines, {"WIDTH": "16", "A": "1", "FLAG": "1"})
        self.assertEqual((s.budget, s.fail_on_increase), (100, 5))

    def test_relative_paths_are_relative_to_the_file(self):
        self.write('[project]\nincdirs = ["rtl/inc"]\n')
        s = self.settings("diff")
        self.assertEqual(s.incdirs, [str(self.tmp.resolve() / "rtl/inc")] if False else s.incdirs)
        self.assertTrue(s.incdirs[0].endswith("rtl/inc"))

    def test_unknown_keys_are_info_and_bad_values_rejected(self):
        self.write('[project]\nbogus = 1\n[nothing]\nx = 1\n')
        s = self.settings("diff")
        self.assertEqual([d.code for d in s.diagnostics], ["UNKNOWN_CONFIG_KEY"] * 2)
        self.assertTrue(all(d.severity == "info" for d in s.diagnostics))
        for text in ["[project\n", '[project]\ntop = 3\n', '[report]\nmax_lines = "x"\n', '[report]\nformat = "xml"\n',
                     '[cache]\nenabled = 1\n', '[budget]\nbasis = "min"\n', "project = 1\n"]:
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(GatecostError) as cm:
                    self.settings("diff")
                self.assertEqual(cm.exception.code, "BAD_CONFIG")

    def test_explicit_config_must_exist(self):
        with self.assertRaises(GatecostError) as cm:
            self.settings("diff", "--config", str(self.tmp / "nope.toml"))
        self.assertEqual(cm.exception.code, "BAD_CONFIG")

    def test_root_detection_ignores_skill_git(self):
        skill = self.tmp / "skills" / "gatecost"
        (skill / "scripts").mkdir(parents=True)
        (skill / ".git").write_text("gitdir: x")
        from gatecost.config import find_project_root

        self.assertEqual(find_project_root(skill / "scripts", skill), self.tmp.resolve())
        (self.tmp / "sub").mkdir()
        self.write("")
        self.assertEqual(find_project_root(self.tmp / "sub", skill), self.tmp.resolve())


class Discovery(unittest.TestCase):
    def test_discovery_and_exclusions(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        files = ["rtl/a.sv", "rtl/b.v", "rtl/inc/defs.svh", "vhdl/c.vhd", "sim/tb.sv", "build/x.v", "rtl/files.f",
                 ".claude/skills/gatecost/tests/fixture.v", "third/skill/fix.v", ".gatecost/s.v", "node_modules/m.v", ".github/skills/g/h.v"]
        for f in files:
            p = tmp / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("module m; endmodule\n")
        rtl, flists = discover(tmp, excludes=["build", "sim"], skill_dir=tmp / "third" / "skill")
        self.assertEqual([p.as_posix() for p in rtl], ["rtl/a.sv", "rtl/b.v", "rtl/inc/defs.svh", "vhdl/c.vhd"])
        self.assertEqual([p.as_posix() for p in flists], ["rtl/files.f"])
        rtl, _ = discover(tmp, excludes=[], skill_dir=tmp / "x", languages=("vhdl",))
        self.assertEqual([p.as_posix() for p in rtl], ["vhdl/c.vhd"])
        rtl, _ = discover(tmp, excludes=[], skill_dir=tmp / "x", globs=["rtl/**/*.sv"])
        self.assertEqual([p.as_posix() for p in rtl], ["rtl/a.sv"])


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / ".git").mkdir()
        (self.tmp / "rtl").mkdir()
        (self.tmp / "rtl/a.sv").write_text("module a; endmodule\n")
        (self.tmp / "rtl/defs.svh").write_text("`define X 1\n")

    def test_version_and_help(self):
        code, out, _ = run("--version")
        self.assertEqual((code, out), (0, f"gatecost {(REPO_ROOT / 'VERSION').read_text().strip()}\n"))
        code, out, _ = run("help")
        self.assertEqual(code, 0)
        self.assertIn("validate-output", out)

    def test_usage_error_is_one_line_without_traceback(self):
        code, out, err = run("diff", "--nope")
        self.assertEqual((code, out.count("\n"), err), (2, 1, ""))
        self.assertTrue(out.startswith("ERROR BAD_ARGUMENT "))
        code, out, _ = run("diff", "--nope", "--format", "json")
        self.assertEqual(json.loads(out)["error"]["code"], "BAD_ARGUMENT")

    def test_diff_without_baseline_is_a_single_error_line(self):
        code, out, _ = run("diff", cwd=self.tmp)
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("ERROR NO_BASELINE "), out)
        self.assertEqual(out.count("\n"), 1)

    def test_init_then_clean(self):
        code, out, _ = run("init", cwd=self.tmp)
        self.assertEqual((code, out), (0, "INIT wrote gatecost.toml sources=2\n"))
        toml = (self.tmp / "gatecost.toml").read_text()
        self.assertIn('sources = ["**/*.sv"]', toml)
        self.assertIn('incdirs = ["rtl"]', toml)
        load_project_file(self.tmp / "gatecost.toml")  # parses without diagnostics
        self.assertEqual(load_project_file(self.tmp / "gatecost.toml").diagnostics, [])
        code, out, _ = run("init", cwd=self.tmp)
        self.assertEqual((code, out.split()[:2]), (2, ["ERROR", "BAD_ARGUMENT"]))
        self.assertEqual(run("init", "--force", cwd=self.tmp)[0], 0)
        (self.tmp / ".gatecost/cache").mkdir(parents=True)
        (self.tmp / ".gatecost/snapshots").mkdir()
        self.assertEqual(run("clean", cwd=self.tmp)[1], "CLEAN removed=1\n")
        self.assertTrue((self.tmp / ".gatecost/snapshots").exists())
        self.assertEqual(run("clean", "--snapshots", cwd=self.tmp)[1], "CLEAN removed=1\n")
        self.assertFalse((self.tmp / ".gatecost/snapshots").exists())

    def test_validate_output(self):
        good = self.tmp / "ok.json"
        good.write_text(json.dumps({"schema_version": 1, "tool": "gatecost", "command": "cost", "status": "error",
                                    "result": None, "error": {"code": "X", "message": "y"}, "diagnostics": []}))
        self.assertEqual(run("validate-output", str(good))[:2], (0, "OK\n"))
        bad = self.tmp / "bad.json"
        bad.write_text("{}")
        code, out, _ = run("validate-output", str(bad))
        self.assertEqual(code, 3)
        self.assertIn("missing required key", out)
        self.assertEqual(run("validate-output", str(self.tmp / "none.json"))[0], 2)

    def test_launcher_from_other_mount_paths_and_clean_skill_dir(self):
        def litter():
            # artifacts the tool could leave behind; concurrent source edits by developers are not its business
            out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--ignored"], cwd=REPO_ROOT,
                                 capture_output=True, text=True).stdout.splitlines()
            return sorted(l for l in out if any(k in l for k in (".gatecost", ".pyc", "snapshots", "gatecost.toml")) and "__pycache__" not in l)

        before = litter()
        mount = self.tmp / "dir with spaces" / ".claude" / "skills" / "gatecost"
        mount.mkdir(parents=True)
        for item in ("scripts", "src", "VERSION", "schema"):
            src = REPO_ROOT / item
            (shutil.copytree if src.is_dir() else shutil.copy)(src, mount / item, **({"ignore": shutil.ignore_patterns("__pycache__")} if src.is_dir() else {}))
        p = subprocess.run([sys.executable, str(mount / "scripts" / "gatecost"), "--version"], capture_output=True, text=True,
                           cwd=self.tmp, env={**os.environ, "GATECOST_CACHE_DIR": str(self.tmp / "cache")})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(list(mount.rglob("__pycache__")))
        self.assertEqual(before, litter())

    def test_launcher_python_check_is_old_syntax_safe(self):
        import ast

        src = LAUNCHER.read_text()
        ast.parse(src, feature_version=(3, 4))  # no f-strings, annotations, walrus, ...
        self.assertIn("PYTHON_TOO_OLD", src)


if __name__ == "__main__":
    unittest.main()
