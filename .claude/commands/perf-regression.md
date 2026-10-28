---
description: Measure performance before/after the current changes and flag regressions
argument-hint: [base git ref, default HEAD]
---

Compare performance of the working tree against `${ARGUMENTS:-HEAD}`.

1. `bench-runner`: run `PYTHONPATH=src python3 -m tests.bench.run_bench` (startup, lines/kinds scaling, `--ir`, `--diff`) on the base ref (use a temporary `git worktree`) and on the working tree.
2. Return a compact before/after table with percentage deltas.
3. If any case regresses by more than 20% or the cold-start budget is exceeded, hand over to `perf-optimizer` with the `perf-check` skill to profile with cProfile and look for quadratic loops on widths, arrays and netlists.

Remove any temporary worktree afterwards.
