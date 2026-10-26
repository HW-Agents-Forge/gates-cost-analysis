"""scripts/yosys_area.py options (-I/-D/--param/--filelist) and the zero-area design: no Yosys needed except the skipped real runs."""
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from gatecost import REPO_ROOT


def _load(name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ya = _load("yosys_area")

# Frozen from a real run: `assign b = a;` synthesises to no cell, so stat prints no "Chip area" line.
EMPTY_STAT = """
7. Printing statistics.

=== e ===

        +----------Local Count, excluding submodules.
        | 
        2 wires
        2 wire bits
        2 public wires
        2 public wire bits
        2 ports
        2 port bits

"""


class ZeroAreaTests(unittest.TestCase):
    def test_empty_design_is_zero_with_note(self):
        area, seq, note = ya.parse_stat_or_empty(EMPTY_STAT)
        self.assertEqual((area, seq), (0.0, None))
        self.assertIn("zero area", note)

    def test_regular_stat_has_no_note(self):
        text = "=== t ===\n   Chip area for module '\\t': 12.5\n     of which used for sequential elements: 2.5 (20%)\n"
        self.assertEqual(ya.parse_stat_or_empty(text), (12.5, 2.5, None))

    def test_garbage_is_still_an_error(self):
        with self.assertRaises(ValueError):
            ya.parse_stat_or_empty("")
        with self.assertRaises(ValueError):
            ya.parse_stat_or_empty("nothing here")

    def test_strict_parse_stat_unchanged(self):
        with self.assertRaises(ValueError):
            ya.parse_stat(EMPTY_STAT)


class ScriptTests(unittest.TestCase):
    def build(self, **kw):
        return ya.build_script([Path("/x/a.v"), Path("/x/b.v")], "top", Path("/l.lib"), None, Path("/s.txt"), **kw)

    def test_default_script_is_unchanged(self):
        self.assertEqual(self.build().split("; ")[:3],
                         ["read_verilog -sv /x/a.v /x/b.v", "hierarchy -check -top top", "synth -flatten -top top"])

    def test_include_and_define_options_on_the_read_command(self):
        s = self.build(incdirs=[Path("/inc/one"), Path("/inc/two")], defines=["A", "W=8"]).split("; ")
        self.assertEqual(s[0], "read_verilog -sv -I/inc/one -I/inc/two -DA -DW=8 /x/a.v /x/b.v")

    def test_chparam_comes_after_read_and_before_hierarchy(self):
        s = self.build(params=["W=16", "NAME=\"abc\"", "M=8'hFF"]).split("; ")
        self.assertTrue(s[0].startswith("read_verilog"))
        self.assertEqual(s[1], "chparam -set W 16 top")
        self.assertEqual(s[2], "chparam -set-string NAME abc top")
        self.assertEqual(s[3], "chparam -set M '8'\"'\"'hFF' top")
        self.assertEqual(s[4], "hierarchy -check -top top")

    def test_bad_param(self):
        with self.assertRaises(ya.YosysError):
            self.build(params=["NOEQUALS"])

    def test_verilog_2005_mode_has_no_sv_flag(self):
        self.assertTrue(self.build(sv=False).startswith("read_verilog /x/a.v"))


class FilelistTests(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d).resolve()
            (d / "sub").mkdir()
            (d / "sub" / "inner.f").write_text("deeper.v\n+incdir+inc2\n")
            (d / "top.f").write_text("# comment\n// other\n+incdir+inc+inc1\n+define+X=1+Y\n-I ../up\n-f sub/inner.f\nsrc/a.v  // trailing\n-unknown\n/abs/b.v\n")
            files, incs, defs = ya.parse_filelist(d / "top.f")
        self.assertEqual(files, [d / "sub" / "deeper.v", d / "src/a.v", Path("/abs/b.v")])
        self.assertEqual(incs, [d / "inc", d / "inc1", d / "../up", d / "sub" / "inc2"])
        self.assertEqual(defs, ["X=1", "Y"])

    def test_cycle_is_ignored_and_missing_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.f").write_text("-f a.f\nx.v\n")
            files, _, _ = ya.parse_filelist(d / "a.f")
            self.assertEqual([f.name for f in files], ["x.v"])
            with self.assertRaises(ya.YosysError):
                ya.parse_filelist(d / "missing.f")

    def test_env_var_expansion(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.f").write_text("${GC_TEST_DIR}/x.v\n")
            os.environ["GC_TEST_DIR"] = "/somewhere"
            try:
                files, _, _ = ya.parse_filelist(Path(d) / "a.f")
            finally:
                del os.environ["GC_TEST_DIR"]
        self.assertEqual(files, [Path("/somewhere/x.v")])

    def test_cli_requires_a_source(self):
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ya.main(["--top", "t"]), 1)


@unittest.skipUnless(shutil.which("yosys") and ya.liberty_path().is_file(), "yosys or the Nangate liberty file is absent")
class RealRunTests(unittest.TestCase):
    def test_zero_area_design_and_param_and_define(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "wire.v").write_text("module wire_only(input a, output b); assign b = a; endmodule\n")
            (d / "add.v").write_text("`include \"w.vh\"\nmodule add #(parameter W = 4)(input [W-1:0] a, b, output [W-1:0] y); assign y = a + b; endmodule\n")
            (d / "inc").mkdir()
            (d / "inc" / "w.vh").write_text("`ifdef BIG\n`define UNUSED 1\n`endif\n")
            zero = ya.synthesize([d / "wire.v"], "wire_only", timeout=120)
            small = ya.synthesize([d / "add.v"], "add", timeout=120, incdirs=[d / "inc"], defines=["BIG"])
            wide = ya.synthesize([d / "add.v"], "add", timeout=120, incdirs=[d / "inc"], params=["W=12"])
        self.assertEqual((zero.ge, zero.area_um2), (0.0, 0.0))
        self.assertIsNotNone(zero.note)
        self.assertGreater(wide.ge, 2 * small.ge)  # chparam is effective (W 4 -> 12)
        self.assertIsNone(small.note)


if __name__ == "__main__":
    unittest.main()
