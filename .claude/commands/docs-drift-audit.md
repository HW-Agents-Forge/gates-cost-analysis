---
description: Audit documentation for stale numbers, contradictions and outdated status (read-only report)
---

Audit documentation drift. Use `docs-keeper` (read-only: report, do not edit).

Check at least:
- Counts quoted in `CONTRIBUTING.md`, `README.md`, `docs/COST_MODEL.md` (micro-benchmarks, frozen cases) vs the actual contents of `tests/reference/` and the calibration CSV.
- Startup budgets quoted in `docs/SPEC.md`, `docs/DISTRIBUTION.md`, `tests/test_bench.py`.
- CI description in `CONTRIBUTING.md` vs `.github/workflows/*.yml` (OS, Python versions, mypy blocking or not).
- License and version statements (`docs/SPEC.md`, `LICENSE`, `VERSION`).
- `docs/IMPLEMENTATION.md` status table vs what the code and tests actually cover.
- CLI examples in `SKILL.md`, `README.md`, `docs/CLI.md` vs `gatecost --help` output.

Output a table: location, claim, reality, suggested fix. Then ask whether to apply the fixes via `docs-keeper` and the `docs-sync` skill.
