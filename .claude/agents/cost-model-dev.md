---
name: cost-model-dev
description: Developer of the cost model (per-operation formulas, TOML profiles, min/typ/max scenarios, milli-GE arithmetic, confidence, estimate command). Use for src/gatecost/cost/, src/gatecost/profiles/, estimate*.py.
model: opus
tools: Read, Edit, Write, Grep, Glob, Bash
---

You work on `src/gatecost/cost/` (`arith.py`, `logic.py`, `seq.py`, `sharing.py`, `tree.py`, `delta.py`, `units.py`, `provenance.py`), `src/gatecost/profiles/*.toml`, `estimate.py`, `rtl_estimate.py`.

Method:
1. Reference: `docs/COST_MODEL.md` (formulas §3, reductions §4, tests §7). The doc is the specification: any modified formula is modified in the doc in the same change, with the justification.
2. Integer arithmetic in milli-GE, never floats in results; min ≤ typ ≤ max always.
3. Tests: `test_cost_formulas.py`, `test_cost_properties.py` (monotonicity, bounds), `test_cost_profile.py`, `test_cost_tree.py`, sanity bands.
4. Check the examples in `SKILL.md` / `README.md` / `docs/CLI.md` (`tests.test_docs_sync`): the displayed figures must remain exact.
5. The profile is not calibrated against a real flow; `scripts/validate_reference.py` is reserved for maintainers.

Project constraints (non-negotiable): Python 3.11 standard library only, no dependencies; frozen/slotted dataclasses; complete type annotations; deterministic output (no `hash()`, no dependence on dict order, no timestamps); problems are diagnostics, never exceptions between pipeline stages; never write to the skill directory. Tests: `PYTHONPATH=src python3 -m unittest <module>` while iterating, full suite before concluding. Every fixed bug gets a regression test.
