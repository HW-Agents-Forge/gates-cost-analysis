"""Snapshots, snapshot baselines and the clean-skill-dir property."""
import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from gatecost import REPO_ROOT, snapshot as snap
from gatecost.baseline import resolve_baseline
from gatecost.cli import main
from gatecost.config import Settings
from tests.test_baseline import MOD, Base, args, write


def cli(*argv, cwd):
    old = os.getcwd()
    os.chdir(cwd)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()
    finally:
        os.chdir(old)


class SnapshotTests(Base):
    def setUp(self):
        super().setUp()
        write(self.root, "rtl/a.v", "// module no\nmodule a(); endmodule\nmodule b(); endmodule\n")
        write(self.root, "rtl/c.vhd", "entity c is end entity;\n-- entity z is\n")
        write(self.root, "gatecost.toml", "")

    def run_cli(self, *a):
        return cli(*a, "--root", str(self.root), cwd=self.root)

    def test_create_output_and_layout(self):
        code, out = self.run_cli("snapshot")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^SNAPSHOT saved id=[0-9a-f]{8} files=3 modules=3\n$")
        self.assertEqual((self.root / ".gatecost/.gitignore").read_text(), "*\n")
        sid = out.split("id=")[1].split()[0]
        self.assertTrue((self.root / ".gatecost/snapshots" / sid / "files/rtl/a.v").is_file())

    def test_same_content_same_id_and_different_differs(self):
        a = self.run_cli("snapshot")[1]
        b = self.run_cli("snapshot")[1]
        self.assertEqual(a, b)
        self.assertEqual(len(snap.list_snapshots(self.root)), 1)
        write(self.root, "rtl/a.v", MOD.format(n="q"))
        c = self.run_cli("snapshot")[1]
        self.assertNotEqual(a, c)
        self.assertEqual(len(snap.list_snapshots(self.root)), 2)

    def test_list_delete(self):
        ids = []
        for i in range(3):
            write(self.root, "rtl/a.v", MOD.format(n=f"m{i}"))
            ids.append(self.run_cli("snapshot")[1].split("id=")[1].split()[0])
        out = self.run_cli("snapshot", "--list")[1]
        lines = out.strip().splitlines()
        self.assertEqual([l.split()[1] for l in lines], ids[::-1])
        self.assertEqual(self.run_cli("snapshot", "--delete", ids[0])[0], 0)
        self.assertEqual(len(snap.list_snapshots(self.root)), 2)
        code, out = self.run_cli("snapshot", "--delete", "ffffffff")
        self.assertEqual(code, 2)
        self.assertIn("ERROR BAD_ARGUMENT", out)
        self.run_cli("snapshot", "--all")
        self.assertEqual(snap.list_snapshots(self.root), [])
        self.assertIn("none", self.run_cli("snapshot", "--list")[1])

    def test_rotation_and_keep(self):
        for i in range(12):
            write(self.root, "rtl/a.v", MOD.format(n=f"m{i}"))
            self.run_cli("snapshot")
        snaps = snap.list_snapshots(self.root)
        self.assertEqual(len(snaps), 10)
        write(self.root, "rtl/a.v", MOD.format(n="last"))
        self.run_cli("snapshot", "--keep", "3")
        self.assertEqual(len(snap.list_snapshots(self.root)), 3)

    def test_use_as_baseline(self):
        sid = self.run_cli("snapshot")[1].split("id=")[1].split()[0]
        write(self.root, "rtl/a.v", MOD.format(n="changed"))
        write(self.root, "rtl/new.v", MOD.format(n="n"))
        (self.root / "rtl/c.vhd").unlink()
        for which in ("latest", sid, sid[:4]):
            bl = resolve_baseline(Settings(root=self.root), args(since_snapshot=which), self.root)
            self.assertEqual((bl.added, bl.removed, bl.modified), (["rtl/new.v"], ["rtl/c.vhd"], ["rtl/a.v"]))
            self.assertEqual(bl.kind, "snapshot")
        # outside git, the default is the latest snapshot
        bl = resolve_baseline(Settings(root=self.root), args(), self.root)
        self.assertEqual(bl.label, f"snapshot {sid}")

    def test_paths_restrict(self):
        out = self.run_cli("snapshot", "rtl/c.vhd")[1]
        self.assertIn("files=2", out)

    def test_defines_profile_recorded(self):
        self.run_cli("snapshot", "--define", "X=2", "--profile", "generic-asic")
        info = snap.find(self.root)
        self.assertEqual(info.defines, {"X": "2"})
        self.assertEqual(info.profile, "generic-asic")

    def test_json_format(self):
        code, out = self.run_cli("snapshot", "--format", "json")
        self.assertEqual(code, 0)
        self.assertIn('"files": 3', out.replace('":3', '": 3'))

    def test_no_sources(self):
        empty = self.root / "empty"
        empty.mkdir()
        code, out = cli("snapshot", "--root", str(empty), cwd=empty)
        self.assertIn("NO_SOURCES", out)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs permissions")
    def test_read_only_project_falls_back(self):
        os.chmod(self.root, stat.S_IREAD | stat.S_IEXEC)
        self.addCleanup(os.chmod, self.root, 0o755)
        code, out = self.run_cli("snapshot")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.root / ".gatecost").exists())
        self.assertEqual(len(snap.list_snapshots(self.root)), 1)
        self.assertTrue(str(snap.list_snapshots(self.root)[0].dir).startswith(self.cache.name))


class SkillDirClean(Base):
    def test_project_inside_skill_dir_writes_nothing_there(self):
        proj = REPO_ROOT / "tests" / "_tmp_skill_proj_check"
        before = {p for p in REPO_ROOT.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts}
        try:
            write(proj, "a.v", MOD.format(n="a"))
            code, out = cli("snapshot", "--root", str(proj), cwd=proj)
            self.assertEqual(code, 0, out)
            self.assertFalse((proj / ".gatecost").exists())
            self.assertEqual(len(snap.list_snapshots(proj)), 1)
        finally:
            import shutil

            shutil.rmtree(proj, ignore_errors=True)
        after = {p for p in REPO_ROOT.rglob("*") if ".git" not in p.parts and "__pycache__" not in p.parts}
        self.assertEqual(after - before, set())


class CountModules(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(snap.count_modules("module a; endmodule /* module x */ module b(); endmodule", "verilog"), 2)
        self.assertEqual(snap.count_modules("entity e is end; -- entity f is\nentity g is end;", "vhdl"), 2)


if __name__ == "__main__":
    unittest.main()
