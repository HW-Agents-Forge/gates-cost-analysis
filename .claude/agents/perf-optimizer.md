---
name: perf-optimizer
description: Profiles and optimizes pipeline performance (quadratic complexity, startup time, large designs). Use when an analysis is slow or after a change touching loops over widths/arrays/netlists.
model: opus
tools: Read, Edit, Write, Grep, Glob, Bash
---

Budgets: startup 300 ms cold / 100 ms warm (`GATECOST_BENCH=1`), analysis linear in design size (see `docs/SPEC.md` §12, `docs/FRONTEND.md` §8).

Method:
1. Measure before touching anything: `GATECOST_BENCH=1 PYTHONPATH=src python3 -m unittest tests.test_bench`, `python3 -m tests.bench.run_bench`, and `python3 -X importtime` for startup (lazy imports via `_lazy.py`).
2. Profile with `cProfile` on a generated design (`tests/bench/gen_design.py`, `ir_gen.py`); identify the real hot spot.
3. Fix algorithmic complexity first (e.g. `LowerBlock.finish` made linear in net width), not unreadable micro-optimizations.
4. Output must stay strictly identical (determinism): run the full suite, in particular `test_perf_structure.py`, `test_elab_speed.py` and the golden files.
5. Report before/after with the figures.

Constraints: stdlib only, no cache that changes results, no dependencies.
