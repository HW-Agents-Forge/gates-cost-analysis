"""Calibration bench (scripts/yosys_area.py, scripts/calibration_bench.py): stat parsing, frozen Yosys reference, statistics.

A guard keeps a representative subset of the recalibrated micro-benchmarks within 12% of Yosys (no global threshold)."""
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
vr = _load("validate_reference")
cb = _load("calibration_bench")

FLAT_STAT = """
=== t ===

      366  479.864 cells
       16    85.12   DFFR_X1

   Chip area for module '\\t': 479.864000
     of which used for sequential elements: 85.120000 (17.74%)
"""

HIER_STAT = """
=== sub ===
   Chip area for module '\\sub': 10.500000

=== top ===
   Chip area for module '\\top': 5.000000

=== design hierarchy ===
   Chip area for top module '\\top': 15.500000
     of which used for sequential elements: 2.000000 (12.90%)
"""


class ParseStatTests(unittest.TestCase):
    def test_flat(self):
        self.assertEqual(ya.parse_stat(FLAT_STAT), (479.864, 85.12))

    def test_hierarchy_prefers_top_module_line(self):
        self.assertEqual(ya.parse_stat(HIER_STAT), (15.5, 2.0))

    def test_no_area_line(self):
        with self.assertRaises(ValueError):
            ya.parse_stat("nothing here")

    def test_liberty_cell_area(self):
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "x.lib"
            lib.write_text("cell (INV_X1) {\n area : 0.532;\n}\ncell (NAND2_X1) {\n drive_strength : 1;\n area : 0.798000;\n}\n")
            self.assertEqual(ya.read_cell_area(lib), 0.798)
            with self.assertRaises(ya.YosysError):
                ya.read_cell_area(lib, "NOPE")

    def test_missing_liberty_is_a_diagnostic(self):
        with self.assertRaises(ya.YosysError):
            ya.synthesize([Path("x.v")], "t", Path("/nonexistent/lib.lib"))


@unittest.skipUnless(shutil.which("yosys") and ya.liberty_path().is_file(), "yosys or the Nangate liberty file is absent")
class YosysRunTests(unittest.TestCase):
    def test_single_nand2_is_one_ge(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "n.v"
            src.write_text("module n(input a, b, output y); assign y = ~(a & b); endmodule\n")
            res = ya.synthesize([src], "n", timeout=120)
        self.assertAlmostEqual(res.area_um2, res.nand2_area_um2, places=6)
        self.assertAlmostEqual(res.ge, 1.0, places=6)

    def test_syntax_error_reports_yosys_message(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "bad.v"
            src.write_text("module bad(input a; endmodule\n")
            with self.assertRaises(ya.YosysError) as cm:
                ya.synthesize([src], "bad", timeout=120)
        self.assertIn("ERROR", str(cm.exception))


class FrozenReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = cb.FROZEN.read_text(encoding="utf-8")
        cls.header = [ln for ln in text.splitlines() if ln.startswith("#")]
        cls.ref = cb.read_frozen()

    def test_header_documents_provenance(self):
        joined = "\n".join(self.header)
        for key in ("yosys:", "liberty:", "command:", "date:"):
            self.assertIn(key, joined)

    def test_values_are_positive_and_names_unique(self):
        import csv
        lines = [ln for ln in cb.FROZEN.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
        names = [r["name"] for r in csv.DictReader(lines)]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(v > 0 for v in self.ref.values()))
        self.assertGreater(len(names), 100)

    def test_every_frozen_case_matches_a_known_case_and_its_files_exist(self):
        cases = {c.name: c for c in cb.all_cases(write=False)}
        for name in self.ref:
            self.assertIn(name, cases)
            self.assertTrue(all(p.is_file() for p in cb.case_files(cases[name])), name)

    def test_committed_microbench_rtl_matches_generator(self):
        for c in cb.all_cases(write=False):
            if c.source is not None:
                self.assertEqual((REPO_ROOT / c.design / "t.v").read_text(encoding="utf-8"), c.source, c.name)

    def test_reference_csv_is_readable_by_validate_reference(self):
        rows = vr.read_reference(cb.FROZEN)
        self.assertEqual(len(rows), len(self.ref))
        self.assertTrue(all(r.area_um2 and r.top for r in rows))


class StatisticsTests(unittest.TestCase):
    def _rows(self, factor):
        cases = [c for c in cb.all_cases(write=False) if c.name in self.ref_names]
        return [cb.Row(c, 100.0, (80.0 * factor, 100.0 * factor, 150.0 * factor)) for c in cases]

    ref_names = ("add_w16", "mul_u8", "dff_w8")

    def test_statistics_on_synthetic_estimates(self):
        s = cb.stats(self._rows(1.1))
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["mape_pct"], 10.0, places=1)
        self.assertAlmostEqual(s["bias_pct"], 10.0, places=1)
        self.assertAlmostEqual(s["geomean_ratio"], 1.1, places=3)
        self.assertEqual(s["coverage_pct"], 100.0)
        s = cb.stats(self._rows(0.5))
        self.assertAlmostEqual(s["bias_pct"], -50.0, places=1)
        self.assertEqual(s["coverage_pct"], 0.0)

    def test_summary_and_csv_render(self):
        rows = self._rows(1.0)
        text = cb.summary(rows)
        self.assertIn("| ALL | 3 |", text)
        with tempfile.TemporaryDirectory() as d:
            cb.write_results(rows, Path(d) / "r.csv")
            self.assertTrue((Path(d) / "r.csv").read_text().startswith(",".join(cb.CSV_HEADER)))

    # One or two micro-benchmarks per rule recalibrated against the frozen Yosys reference (COST_MODEL.md section 3):
    # name -> whether the Yosys value must lie inside [min, max] as well.  Design-level cases are deliberately absent
    # (they are the validation set, never used to tune the model).
    GUARDED = {"mul_u16": True, "mul_s16": True, "mul_u32": True, "mac_comb_w16": True, "sub_w32": True,
               "add_w32": True, "inc_w16": True, "div_u16": True, "mux8_w32": True, "mux16_w8": True,
               "mux8_slice_w32": True, "pmux_case5_w16": True, "rom_64x8": True, "decode_s3": True,
               "red_xor_w32": True, "sum4_w16": False}

    def test_recalibrated_rules_stay_close_to_yosys(self):
        from concurrent.futures import ThreadPoolExecutor
        ref = cb.read_frozen()
        cases = [c for c in cb.all_cases(write=False) if c.name in self.GUARDED]
        self.assertEqual(len(cases), len(self.GUARDED))

        def est(c):
            return c, vr.run_gatecost(cb.gatecost_path(c), c.top, None)[0]

        with ThreadPoolExecutor(max_workers=8) as pool:
            res = list(pool.map(est, cases))
        bad = []
        for c, (lo, typ, hi) in res:
            y = ref[c.name]
            if abs(typ - y) > 0.12 * y:
                bad.append(f"{c.name}: typ {typ:.1f} vs Yosys {y:.1f} ({100 * (typ - y) / y:+.1f}%)")
            if self.GUARDED[c.name] and not lo <= y <= hi:
                bad.append(f"{c.name}: Yosys {y:.1f} outside [{lo:.1f}, {hi:.1f}]")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_end_to_end_on_frozen_subset_without_threshold(self):
        ref = cb.read_frozen()
        rows = []
        for c in cb.all_cases(write=False):
            if c.name in self.ref_names:
                (lo, typ, hi), _ = vr.run_gatecost(cb.gatecost_path(c), c.top, None)
                rows.append(cb.Row(c, ref[c.name], (lo, typ, hi)))
        s = cb.stats(rows)
        self.assertEqual(s["n"], len(self.ref_names))
        self.assertIn("mape_pct", s)   # informational only: no accuracy threshold before re-calibration


if __name__ == "__main__":
    unittest.main()
