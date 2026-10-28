---
name: perf-check
description: Measure and diagnose gatecost performance (startup, large designs, quadratic complexity) with the repository benchmarks and cProfile. Use when an analysis is slow or before/after a change to loops over widths, arrays, or netlists.
---

# perf-check

Budgets (`docs/DISTRIBUTION.md` §3, `docs/SPEC.md` §12): startup ≤ 300 ms cold, ≤ 100 ms warm; analysis linear in design size.

## Measure

```bash
GATECOST_BENCH=1 PYTHONPATH=src python3 -m unittest tests.test_bench   # enforces the budgets
PYTHONPATH=src python3 -m tests.bench.run_bench                        # full table
PYTHONPATH=src python3 -X importtime scripts/gatecost --version 2>&1 | sort -t'|' -k2 -n | tail   # startup
```

Synthetic design generators: `tests/bench/gen_design.py`, `tests/bench/ir_gen.py`.

## Profile

```bash
PYTHONPATH=src python3 -c "import cProfile,pstats,sys; sys.argv=['gatecost','cost','--no-cache']; \
from gatecost.cli import main; cProfile.run('main(sys.argv[1:])','/tmp/p.out')"
python3 -c "import pstats; pstats.Stats('/tmp/p.out').sort_stats('cumtime').print_stats(25)"
```

(Write the profile output to the session scratchpad rather than into the repository.)

## Interpret

- Does the time grow faster than linearly when the size doubles? Look for nested loops over the bits of a net or the elements of an array, repeated list concatenations, linear searches in lists.
- Slow startup: an expensive import outside the critical path; use lazy imports (`_lazy.py`).
- Precedents: `LowerBlock.finish` made linear in net width (44 s → 2.5 s), quadratic dynamic write to a packed vector.

## Rules

- Measure before and after; report the figures.
- Output must remain byte-for-byte identical (determinism): full suite + `test_perf_structure`, `test_elab_speed`.
- No cache that alters results, no dependency.

Recommended agent: `perf-optimizer`.
