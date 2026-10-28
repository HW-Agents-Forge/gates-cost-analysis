---
name: change-cost-model
description: Modify or add a cost formula, primitive, or profile in gatecost (GE, min/typ/max scenarios, milli-GE, confidence) while keeping COST_MODEL.md, property tests, and examples consistent. Use for any change to the figures the tool produces.
---

# change-cost-model

Cost is defined by `docs/COST_MODEL.md`; the code implements it. Every change touches both.

## Files

- Formulas: `src/gatecost/cost/{arith,logic,seq,sharing}.py`
- Units and rounding: `cost/units.py` (integers in milli-GE)
- Tree, delta, provenance: `cost/{tree,delta,provenance}.py`
- Profiles: `src/gatecost/profiles/*.toml` (`generic-asic`, inheritance via `extends`)
- `estimate` notation: `estimate.py`, `rtl_estimate.py`

## Procedure

1. **Specify**: write/modify the formula in `docs/COST_MODEL.md` §3 (all three min/typ/max scenarios) with the rationale (assumed architecture: ripple vs carry-lookahead adder, Booth multiplier, etc.).
2. **Implement** in integer milli-GE, with no floats in the results, and with min ≤ typ ≤ max.
3. **Tests**:
   - exact values for a few widths (`tests/test_cost_formulas.py`);
   - properties: monotonic in width, constants cheaper than variables, bounds (`test_cost_properties.py`);
   - profile (`test_cost_profile.py`), tree (`test_cost_tree.py`).
4. **Quality/confidence**: a poorly modeled operation (dividers, memories, ROM) keeps a low quality class; do not claim higher confidence than reality.
5. **Propagate**: the figures quoted in `SKILL.md`, `README.md`, `docs/CLI.md`, `examples/mac/REPORT.md` must remain true. Rerun `tests.test_docs_sync` and the e2e tests, then regenerate the example if an explicit script exists for that.
6. **Do not** present the profile as calibrated: it is not (`scripts/validate_reference.py` compares against reference areas, maintainer use).

## Verify

Recommended agent: `cost-model-dev`. Then skill `run-tests` (full suite) and `done-checklist`.
