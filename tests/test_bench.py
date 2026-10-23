"""Performance checks (IMPLEMENTATION.md stage 8; SPEC.md section 12; FRONTEND.md section 8).

* The start-up test always runs, with loose bounds (a loaded CI machine must not make it flaky); with
  ``GATECOST_BENCH=1`` the documented budgets of DISTRIBUTION.md section 3 are enforced (300 ms cold,
  100 ms warm).
* The benchmarks on generated designs run only with ``GATECOST_BENCH=1`` and print their timings.
  ``python -m tests.bench.run_bench`` prints the full table.
"""
import os
import unittest

BENCH = os.environ.get("GATECOST_BENCH") == "1"


class StartUp(unittest.TestCase):
    def test_startup_time(self):
        from tests.bench.run_bench import startup

        res = startup()
        cold_budget, warm_budget = (0.300, 0.100) if BENCH else (1.5, 0.5)
        for label, (cold, warm) in res.items():
            print(f"\nstart-up {label}: cold {cold * 1000:.0f} ms, warm {warm * 1000:.0f} ms", end="")
            self.assertLess(cold, cold_budget, label)
            self.assertLess(warm, warm_budget, label)


@unittest.skipUnless(BENCH, "set GATECOST_BENCH=1 to run the benchmarks")
class Benchmarks(unittest.TestCase):
    def _check(self, kind, lines, total_limit):
        from tests.bench.run_bench import pipeline

        r = pipeline(kind, lines)
        print(f"\n{kind} {r['lines']} lines: parse {r['parse_s']:.2f}s ({r['parse_MBps']:.2f} MB/s), "
              f"elab+analyze {r['elab_s']:.2f}s, cost-tree {r['cost_tree_s']:.2f}s, {r['nodes']} IR nodes", end="")
        self.assertGreaterEqual(r["parse_MBps"], 1.0, "FRONTEND.md section 8: at least 1 MB/s")
        self.assertLess(r["elab_s"] + r["cost_tree_s"] + r["parse_s"], total_limit)

    def test_sv_10k(self):
        self._check("sv", 10_000, 6.0)

    def test_vhdl_10k(self):
        self._check("vhdl", 10_000, 6.0)

    def test_sv_100k(self):
        self._check("sv", 100_000, 40.0)

    def test_vhdl_100k(self):
        self._check("vhdl", 100_000, 40.0)

    def test_ir_100k_nodes(self):
        from tests.bench.run_bench import synthetic_ir

        r = synthetic_ir(100_000)
        print(f"\nIR {r['nodes']} nodes: build {r['build_s']:.2f}s, analyze {r['analyze_s']:.2f}s", end="")
        self.assertLess(r["build_s"] + r["analyze_s"], 8.0)


if __name__ == "__main__":
    unittest.main()
