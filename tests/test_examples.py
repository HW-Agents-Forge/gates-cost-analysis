"""Every example under examples/ costs without errors or warnings (and its REPORT.md exists)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "gatecost"
EXAMPLES = ROOT / "examples"

# (example directory, top, variant)
CASES = [("counter_width", "counter", "wider"), ("alu_multiplier", "alu", "with_mul"),
         ("fifo_depth", "fifo", "deeper"), ("fir_pipeline", "fir", "pipelined"), ("uart_tx", "uart_tx", None),
         ("sv_features", "top", None), ("udp_mux", "mux4w", None), ("vhdl_fsm_alu", "alu", "wider")]


class Examples(unittest.TestCase):
    def run_tool(self, cwd, *args):
        with tempfile.TemporaryDirectory() as cache:
            env = dict(os.environ, GATECOST_CACHE_DIR=cache)
            p = subprocess.run([sys.executable, str(TOOL), *args, "--root", "."], cwd=cwd, capture_output=True,
                               text=True, env=env, timeout=120)
        return p.stdout

    def test_every_example_costs_cleanly(self):
        for d, top, variant in CASES:
            with self.subTest(example=d):
                cwd = EXAMPLES / d
                out = self.run_tool(cwd, "cost", "baseline", "--top", top)
                self.assertTrue(out.startswith("COST "), out)
                self.assertNotIn("WARN ", out)
                self.assertTrue((cwd / "REPORT.md").is_file())
                if variant:
                    out = self.run_tool(cwd, "diff", "--before", "baseline", "--after", variant, "--top", top)
                    self.assertTrue(out.startswith("DELTA "), out)
                    self.assertNotIn("WARN ", out)


if __name__ == "__main__":
    unittest.main()
