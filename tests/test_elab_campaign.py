"""Seeded mutation campaigns over the hardening corpus (tests/corpus), both languages.

The committed run is small and fast.  Larger local campaigns::

    GATECOST_FUZZ_ITERS=300 PYTHONPATH=src python3 -m unittest tests.test_elab_campaign
    GATECOST_FUZZ_ITERS=300 GATECOST_FUZZ_SEED=7 PYTHONPATH=src python3 -m tests.test_elab_campaign

Every mutated design must elaborate without an uncaught exception, without an ``INTERNAL`` diagnostic, within a
bounded time, and return well-formed diagnostics.  ``python -m tests.test_elab_campaign`` prints a report
(designs, mutations, worst time).
"""
import os
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

from gatecost.elab import elaborate_project
from gatecost.errors import GatecostError
from gatecost.result import Diagnostic

from tests.test_elab_corpus import CORPUS, all_designs
from tests.test_verilog_fuzz import mutate as vmutate, spans as vspans
from tests.test_vhdl_fuzz import mutate as hmutate

ITERS = int(os.environ.get("GATECOST_FUZZ_ITERS", "2"))
SEED = int(os.environ.get("GATECOST_FUZZ_SEED", "20260101"))
TIME_LIMIT = 15.0


def sources():
    out = []
    for label, root, rel, _top in all_designs():
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            out.append((label, os.path.splitext(rel)[1], fh.read()))
    return out


def mutate_text(rng, ext, text):
    if not text.strip():
        return text
    if ext == ".vhd":
        return hmutate(text, rng)
    return vmutate(rng, text, vspans(text))


SLOW = 5.0
FAIL_DIR = os.environ.get("GATECOST_FUZZ_DUMP", os.path.join(tempfile.gettempdir(), "gatecost_fuzz_failures"))


def save_failure(label, ext, text, stats, why):
    os.makedirs(FAIL_DIR, exist_ok=True)
    name = "%s_%s_%d%s" % (label.replace("/", "_"), why, stats.runs, ext)
    Path(FAIL_DIR, name).write_text(text)


class Stats:
    def __init__(self):
        self.runs = 0
        self.worst = 0.0
        self.worst_label = ""
        self.errors = 0


def run_one(testcase, label, ext, text, stats):
    with tempfile.TemporaryDirectory() as td:
        name = "fuzz" + ext
        Path(td, name).write_text(text)
        t0 = time.perf_counter()
        try:
            res = elaborate_project([Path(name)], root=Path(td))
        except GatecostError as exc:
            if testcase is not None:
                testcase.assertIn(exc.code, ("NO_SOURCES", "TOP_NOT_FOUND"), label)
            stats.runs += 1
            return
        dt = time.perf_counter() - t0
    stats.runs += 1
    if dt > stats.worst:
        stats.worst, stats.worst_label = dt, label
    if dt > SLOW and testcase is None:
        save_failure(label, ext, text, stats, "slow%.0fs" % dt)
    for d in res.diagnostics:
        ok = isinstance(d, Diagnostic) and d.code and d.message
        bad = d.code == "INTERNAL"
        if not ok or bad:
            stats.errors += 1
            if testcase is None:
                print("INTERNAL", label, d.message)
                save_failure(label, ext, text, stats, "internal")
        if testcase is not None:
            testcase.assertTrue(ok, label)
            testcase.assertFalse(bad, (label, d.message))
    if testcase is not None:
        testcase.assertLess(dt, TIME_LIMIT, label)
        for m in res.design.modules.values():
            testcase.assertTrue(m.analyzed, label)


class Campaign(unittest.TestCase):
    def test_mutated_corpus_designs(self):
        rng = random.Random(SEED)
        stats = Stats()
        for label, ext, text in sources():
            for k in range(ITERS):
                mt = mutate_text(rng, ext, text)
                with self.subTest(design=label, k=k):
                    run_one(self, label, ext, mt, stats)

    def test_double_mutations(self):
        rng = random.Random(SEED + 1)
        stats = Stats()
        for label, ext, text in sources():
            for k in range(max(1, ITERS // 2)):
                mt = mutate_text(rng, ext, mutate_text(rng, ext, text))
                with self.subTest(design=label, k=k):
                    run_one(self, label, ext, mt, stats)

    def test_truncations(self):
        rng = random.Random(SEED + 2)
        stats = Stats()
        for label, ext, text in sources():
            for _ in range(max(1, ITERS // 2)):
                cut = rng.randrange(1, len(text))
                with self.subTest(design=label, cut=cut):
                    run_one(self, label, ext, text[:cut], stats)


def campaign_report(iters, seed):
    global ITERS, SEED
    ITERS, SEED = iters, seed
    rng = random.Random(seed)
    stats = Stats()
    t0 = time.perf_counter()
    for label, ext, text in sources():
        for _ in range(iters):
            r = rng.random()
            if r < 0.15:
                mt = text[:rng.randrange(1, len(text))]
            elif r < 0.4:
                mt = mutate_text(rng, ext, mutate_text(rng, ext, text))
            else:
                mt = mutate_text(rng, ext, text)
            try:
                run_one(None, label, ext, mt, stats)
            except Exception as exc:                                # an uncaught exception is the failure we hunt
                stats.errors += 1
                print("UNCAUGHT", label, type(exc).__name__, exc)
                save_failure(label, ext, mt, stats, "uncaught")
    print("campaign: %d mutated designs from %d corpus files, %d errors, worst %.2fs (%s), total %.1fs"
          % (stats.runs, len(sources()), stats.errors, stats.worst, stats.worst_label, time.perf_counter() - t0))
    return stats


if __name__ == "__main__":
    n = int(os.environ.get("GATECOST_FUZZ_ITERS", "100"))
    st = campaign_report(n, SEED)
    sys.exit(1 if st.errors else 0)
