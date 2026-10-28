---
description: Review cost-model calibration against the frozen Yosys/Nangate45 reference and explain deviations
---

Delegate to `cost-model-dev` (analysis only unless I ask for changes):
1. Run `python3 scripts/calibration_bench.py --frozen` and capture the per-operation error table.
2. List operations whose error against `tests/reference/yosys_nangate45.csv` exceeds the tolerance documented in `docs/COST_MODEL.md`.
3. For each, check the formula in `src/gatecost/cost/` and the profile values in `src/gatecost/profiles/generic-asic.toml`; classify the deviation (formula wrong, profile value stale, reference limitation).
4. Verify that min <= typ <= max scenarios and milli-GE integer arithmetic still hold on those cases.

Report findings and proposed changes. Any change must follow the `change-cost-model` skill (docs and property tests updated together). Never run `--freeze` without explicit approval.
