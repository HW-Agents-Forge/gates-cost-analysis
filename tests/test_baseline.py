"""Baseline resolution (DISTRIBUTION.md section 4.1)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from gatecost.baseline import resolve_baseline
from gatecost.config import Settings
from gatecost.discovery import discover
from gatecost.errors import GatecostError
from gatecost import REPO_ROOT

HAVE_GIT = shutil.which("git") is not None
MOD = "module {n}(input a, output y);\n  assign y = a;\nendmodule\n"


def args(**kw):
    d = dict(base_ref=None, since_snapshot=None, before=None, after=None, paths=[])
    d.update(kw)
    return Namespace(**d)


def git(root, *a):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-c", "commit.gpgsign=false", *a], cwd=root, check=True, capture_output=True, env=env)


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        p = mock.patch.dict(os.environ, {"GATECOST_CACHE_DIR": self.cache.name})
        p.start()
        self.addCleanup(p.stop)
        self.root = Path(self.tmp.name).resolve() / "proj dir"
        self.root.mkdir()

    def settings(self, **kw):
        return Settings(root=self.root, exclude=["build", "sim"], **kw)

    def resolve(self, **kw):
        bl = resolve_baseline(self.settings(), args(**kw), self.root)
        self.addCleanup(bl.cleanup)
        return bl


@unittest.skipUnless(HAVE_GIT, "git not installed")
class GitBaseline(Base):
    def init(self):
        git(self.root, "init", "-q", "-b", "main")
        write(self.root, "rtl/a.v", MOD.format(n="a"))
        write(self.root, "rtl/b.v", MOD.format(n="b"))
        write(self.root, "rtl/inc/defs.vh", "`define W 8\n")
        write(self.root, "gatecost.toml", "[project]\nincdirs=['rtl/inc']\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

    def test_clean_tree_no_change(self):
        self.init()
        bl = self.resolve()
        self.assertEqual(bl.changed_files, [])
        self.assertEqual(bl.kind, "git")
        self.assertEqual(bl.after_root, self.root)
        self.assertTrue((bl.before_root / "rtl" / "inc" / "defs.vh").is_file())

    def test_unstaged_and_staged(self):
        self.init()
        write(self.root, "rtl/a.v", MOD.format(n="a2"))
        write(self.root, "rtl/b.v", MOD.format(n="b2"))
        git(self.root, "add", "rtl/b.v")
        bl = self.resolve()
        self.assertEqual(bl.modified, ["rtl/a.v", "rtl/b.v"])
        self.assertIn("module a(", (bl.before_root / "rtl/a.v").read_text())

    def test_untracked_is_added(self):
        self.init()
        write(self.root, "rtl/new.sv", MOD.format(n="n"))
        bl = self.resolve()
        self.assertEqual(bl.added, ["rtl/new.sv"])
        self.assertFalse((bl.before_root / "rtl/new.sv").exists())

    def test_deleted_and_staged_delete(self):
        self.init()
        (self.root / "rtl/a.v").unlink()
        git(self.root, "rm", "-q", "--cached", "rtl/b.v")
        (self.root / "rtl/b.v").unlink()
        bl = self.resolve()
        self.assertEqual(bl.removed, ["rtl/a.v", "rtl/b.v"])
        self.assertTrue((bl.before_root / "rtl/a.v").is_file())

    def test_moved_file(self):
        self.init()
        git(self.root, "mv", "rtl/a.v", "rtl/moved a.v")
        bl = self.resolve()
        self.assertEqual((bl.added, bl.removed), (["rtl/moved a.v"], ["rtl/a.v"]))

    def test_spaces_in_paths(self):
        self.init()
        write(self.root, "my rtl/with space.v", MOD.format(n="s"))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "s")
        write(self.root, "my rtl/with space.v", MOD.format(n="s2"))
        bl = self.resolve()
        self.assertEqual(bl.changed_files, ["my rtl/with space.v"])
        self.assertIn("module s(", (bl.before_root / "my rtl/with space.v").read_text())

    def test_no_commits(self):
        git(self.root, "init", "-q", "-b", "main")
        write(self.root, "a.v", MOD.format(n="a"))
        bl = self.resolve()
        self.assertEqual(bl.added, ["a.v"])
        self.assertEqual(bl.removed, [])

    def test_base_ref_and_merge_base(self):
        self.init()
        git(self.root, "checkout", "-q", "-b", "feat")
        write(self.root, "rtl/a.v", MOD.format(n="feat"))
        git(self.root, "commit", "-q", "-am", "feat")
        git(self.root, "checkout", "-q", "main")
        write(self.root, "rtl/b.v", MOD.format(n="mainchange"))
        git(self.root, "commit", "-q", "-am", "main")
        git(self.root, "checkout", "-q", "feat")
        self.assertEqual(self.resolve().changed_files, [])  # HEAD default
        bl = self.resolve(base_ref="merge-base")
        self.assertEqual(bl.changed_files, ["rtl/a.v"])  # b.v change is on main only
        self.assertIn("module a(", (bl.before_root / "rtl/a.v").read_text())
        bl = self.resolve(base_ref="main")
        self.assertEqual(bl.changed_files, ["rtl/a.v", "rtl/b.v"])
        bl = self.resolve(base_ref="HEAD~1")
        self.assertEqual(bl.changed_files, ["rtl/a.v"])

    def test_merge_base_master_fallback(self):
        git(self.root, "init", "-q", "-b", "master")
        write(self.root, "a.v", MOD.format(n="a"))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "x")
        write(self.root, "a.v", MOD.format(n="z"))
        self.assertEqual(self.resolve(base_ref="merge-base").modified, ["a.v"])

    def test_bad_ref(self):
        self.init()
        for ref in ("nope", "--output=x"):
            with self.assertRaises(GatecostError) as cm:
                self.resolve(base_ref=ref)
            self.assertEqual(cm.exception.code, "NO_BASELINE")

    def test_path_restriction(self):
        self.init()
        write(self.root, "rtl/a.v", MOD.format(n="a2"))
        write(self.root, "rtl/b.v", MOD.format(n="b2"))
        bl = self.resolve(paths=[str(self.root / "rtl/a.v")])
        self.assertEqual(bl.changed_files, ["rtl/a.v"])
        self.assertEqual(bl.all_changed_files, ["rtl/a.v", "rtl/b.v"])

    def test_excluded_and_ignored(self):
        self.init()
        write(self.root, "build/gen.v", MOD.format(n="g"))
        write(self.root, ".gitignore", "out/\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-q", "-m", "ig")
        write(self.root, "out/gen.v", MOD.format(n="g"))
        bl = self.resolve()
        self.assertEqual(bl.changed_files, [])
        self.assertTrue((bl.before_root / "out/gen.v").is_file())  # ignored file: same on both sides

    def test_matches_discovery_on_committed_tree(self):
        self.init()
        write(self.root, "sim/x.v", MOD.format(n="x"))
        write(self.root, "node_modules/y.v", MOD.format(n="y"))
        write(self.root, "f.f", "rtl/a.v\n")
        git(self.root, "add", "-A", "-f")
        git(self.root, "commit", "-q", "-m", "more")
        bl = self.resolve()
        rtl, fl = discover(self.root, excludes=["build", "sim"], skill_dir=REPO_ROOT)
        want = sorted(p.as_posix() for p in [*rtl, *fl]) + ["gatecost.toml", "rtl/inc/defs.vh"]
        got = sorted(str(p.relative_to(bl.before_root).as_posix()) for p in bl.before_root.rglob("*") if p.is_file())
        self.assertEqual(got, sorted(set(want)))

    def test_cleanup_removes_copy(self):
        self.init()
        bl = resolve_baseline(self.settings(), args(), self.root)
        d = bl.before_root
        bl.cleanup()
        self.assertFalse(d.exists())

    def test_git_absent_falls_back(self):
        self.init()
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaises(GatecostError) as cm:
                resolve_baseline(self.settings(), args(), self.root)
            self.assertEqual(cm.exception.code, "NO_BASELINE")
            with self.assertRaises(GatecostError):
                resolve_baseline(self.settings(), args(base_ref="HEAD"), self.root)


class NonGit(Base):
    def test_no_baseline(self):
        write(self.root, "a.v", MOD.format(n="a"))
        with self.assertRaises(GatecostError) as cm:
            resolve_baseline(self.settings(), args(), self.root)
        self.assertEqual(cm.exception.code, "NO_BASELINE")

    def test_before_after_dirs(self):
        b, a = self.root / "b", self.root / "a"
        write(b, "x.v", MOD.format(n="x"))
        write(b, "gone.v", MOD.format(n="g"))
        write(a, "x.v", MOD.format(n="x2"))
        write(a, "new.v", MOD.format(n="n"))
        bl = resolve_baseline(self.settings(), args(before=str(b), after=str(a)), self.root)
        self.assertEqual((bl.added, bl.removed, bl.modified), (["new.v"], ["gone.v"], ["x.v"]))
        self.assertEqual((bl.before_root, bl.after_root), (b, a))

    def test_before_after_missing(self):
        with self.assertRaises(GatecostError):
            resolve_baseline(self.settings(), args(before=str(self.root / "no"), after=str(self.root)), self.root)

    def test_unknown_snapshot(self):
        with self.assertRaises(GatecostError) as cm:
            resolve_baseline(self.settings(), args(since_snapshot="deadbeef"), self.root)
        self.assertEqual(cm.exception.code, "NO_BASELINE")


if __name__ == "__main__":
    unittest.main()
