"""scripts/validate_reference.py: CSV format, conversion to GE, statistics, and an end-to-end run."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gatecost import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "validate_reference.py"
spec = importlib.util.spec_from_file_location("validate_reference", SCRIPT)
vr = importlib.util.module_from_spec(spec)
sys.modules["validate_reference"] = vr
spec.loader.exec_module(vr)

CSV = """# reference areas from a yosys run
design,top,area_um2,ge,nand2_area_um2
a,top_a,79.8,,
b,,159.6,,
c,top_c,,50,
d,top_d,100,,1.0
"""


class ReferenceCsv(unittest.TestCase):
    def write(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "ref.csv"
        p.write_text(text)
        return p

    def test_parse(self):
        rows = vr.read_reference(self.write(CSV))
        self.assertEqual([r.design for r in rows], ["a", "b", "c", "d"])
        self.assertEqual(rows[2].ref_ge, 50.0)
        self.assertEqual(rows[3].nand2_area_um2, 1.0)

    def test_rejects_bad_files(self):
        for text in ("design,top\nx,y\n", "design,area_um2\nx,-1\n", "design,area_um2\nx,abc\n", "design,area_um2\n,5\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                vr.read_reference(self.write(text))

    def test_conversion_and_statistics(self):
        rows = vr.read_reference(self.write(CSV))
        table = {"a": (90, 100, 130), "b": (150, 200, 260), "c": (30, 40, 45), "d": (80, 120, 150)}

        def fake(row):
            if row.design == "b":
                return table["b"], 0.798
            return table[row.design], 0.798

        results, skipped = vr.compare(rows, fake, nand2_area=0.798)
        self.assertEqual(skipped, [])
        ref = {r.design: r.ref_ge for r in results}
        self.assertAlmostEqual(ref["a"], 100.0)     # 79.8 / 0.798
        self.assertAlmostEqual(ref["b"], 200.0)
        self.assertEqual(ref["c"], 50.0)            # ge column wins
        self.assertAlmostEqual(ref["d"], 100.0)     # per-row NAND2 area wins
        err = {r.design: round(r.error_pct, 1) for r in results}
        self.assertEqual(err, {"a": 0.0, "b": 0.0, "c": -20.0, "d": 20.0})
        st = vr.statistics_of(results)
        self.assertEqual(st["n"], 4)
        self.assertEqual(st["mean_abs_error_pct"], 10.0)
        self.assertEqual(st["median_abs_error_pct"], 10.0)
        self.assertEqual(st["bias_pct"], 0.0)
        self.assertEqual(st["inside_range_share"], 0.75)      # only c (50 > 45) is outside its range
        self.assertIn(st["worst_design"], ("c", "d"))

    def test_failed_design_is_skipped(self):
        rows = vr.read_reference(self.write("design,area_um2\nx,10\n"))

        def boom(row):
            raise RuntimeError("no sources")

        results, skipped = vr.compare(rows, boom)
        self.assertEqual((results, len(skipped)), ([], 1))
        self.assertEqual(vr.statistics_of(results), {"n": 0})

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "alu").mkdir()
            (root / "alu" / "alu.v").write_text(
                "module alu(input [15:0] a, input [15:0] b, output [15:0] y);\n  assign y = a + b;\nendmodule\n")
            ref = root / "ref.csv"
            ref.write_text("design,top,area_um2\nalu,alu,80.0\nmissing,,10.0\n")
            p = subprocess.run([sys.executable, str(SCRIPT), str(root), str(ref), "--format", "json"],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            doc = json.loads(p.stdout)
            self.assertEqual(doc["statistics"]["n"], 1)
            self.assertEqual(len(doc["skipped"]), 1)
            self.assertGreater(doc["designs"][0]["typ"], 0)
            q = subprocess.run([sys.executable, str(SCRIPT), str(root), str(ref), "--tolerance", "0.0001"],
                               capture_output=True, text=True)
            self.assertEqual(q.returncode, 1)
            self.assertIn("median_abs_error_pct", q.stdout)


if __name__ == "__main__":
    unittest.main()
