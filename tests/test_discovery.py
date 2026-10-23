"""Source discovery: the built-in and configured exclusions also apply to explicit ``sources`` globs."""
import tempfile
import unittest
from pathlib import Path

from gatecost.discovery import discover

SKILL = Path("/nonexistent-skill-dir")


class GlobExclusions(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        for rel in ("rtl/a.sv", "rtl/b.v", ".gatecost/snapshots/1/files/rtl/a.sv", ".git/x.v",
                    "build/gen.v", "node_modules/p/q.v"):
            f = self.root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("module m; endmodule\n")

    def tearDown(self):
        self._td.cleanup()

    def names(self, **kw):
        rtl, _ = discover(self.root, skill_dir=SKILL, **kw)
        return sorted(p.as_posix() for p in rtl)

    def test_builtin_directories_are_skipped_with_globs(self):
        got = self.names(excludes=[], globs=["**/*.sv", "**/*.v"])
        self.assertEqual(got, ["build/gen.v", "rtl/a.sv", "rtl/b.v"])

    def test_configured_excludes_apply_to_globs(self):
        got = self.names(excludes=["build"], globs=["**/*.sv", "**/*.v"])
        self.assertEqual(got, ["rtl/a.sv", "rtl/b.v"])

    def test_walk_and_glob_agree(self):
        walked = self.names(excludes=["build"])
        globbed = self.names(excludes=["build"], globs=["**/*.sv", "**/*.v"])
        self.assertEqual(walked, globbed)


class MessagePaths(unittest.TestCase):
    def test_absolute_root_is_stripped_from_diagnostic_messages(self):
        from gatecost.pipeline import _as_diag
        from gatecost.result import Diagnostic

        d = Diagnostic("DUPLICATE_MODULE", "redefined (previous definition at /proj/a/m.v:1)", "warning", True, "after")
        self.assertEqual(_as_diag(d, Path("/proj")).message, "redefined (previous definition at a/m.v:1)")


if __name__ == "__main__":
    unittest.main()
