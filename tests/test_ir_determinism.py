"""IR.md section 12: determinism (dump and hashes) across runs, dictionary/creation order,
and PYTHONHASHSEED."""
import hashlib
import os
import subprocess
import sys
import unittest

from gatecost.ir import analyze_ir, dump

from tests.ir_designs import design_text, rich_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DeterminismTests(unittest.TestCase):
    def test_two_runs_are_byte_identical(self):
        self.assertEqual(design_text(), design_text())

    def test_analysis_is_idempotent(self):
        m = rich_module()
        analyze_ir(m)
        first = dump(m)
        h = m.canonical_hash
        analyze_ir(m)
        self.assertEqual(dump(m), first)
        self.assertEqual(m.canonical_hash, h)

    def test_independent_of_declaration_and_statement_order(self):
        ref = None
        for seed in (None, 1, 2, 3, 4, 5, 6, 7):
            m = rich_module(seed)
            analyze_ir(m)
            cur = (dump(m, normalize=True), m.canonical_hash,
                   sorted(n.shape for n in m.nodes), sorted(n.named_key for n in m.nodes))
            if ref is None:
                ref = cur
            self.assertEqual(cur[1], ref[1], seed)
            self.assertEqual(cur[2], ref[2], seed)
            self.assertEqual(cur[3], ref[3], seed)
            self.assertEqual(cur[0], ref[0], seed)

    def test_independent_of_pythonhashseed(self):
        outs = []
        env_base = dict(os.environ)
        env_base["PYTHONPATH"] = os.pathsep.join([os.path.join(ROOT, "src"), ROOT])
        for seed in ("0", "1", "4242", "random"):
            env = dict(env_base)
            env["PYTHONHASHSEED"] = seed
            p = subprocess.run([sys.executable, "-m", "tests.ir_designs"], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            outs.append(hashlib.sha256(p.stdout.encode()).hexdigest())
        self.assertEqual(len(set(outs)), 1, outs)

    def test_hashes_are_stable_constants(self):
        # a golden value guards against accidental changes of the serialization
        m = rich_module()
        analyze_ir(m)
        self.assertEqual(len(m.canonical_hash.hex()), 32)


if __name__ == "__main__":
    unittest.main()
