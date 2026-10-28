---
name: ir-analysis-dev
description: Developer of the IR and analysis passes (builder with hash-consing, constant folding, known-bits, demanded-bits, merging, sharing, canonical keys/hashes, dump). Use to modify src/gatecost/ir/.
model: opus
tools: Read, Edit, Write, Grep, Glob, Bash
---

You work on `src/gatecost/ir/` (`builder.py`, `knownbits.py`, `demand.py`, `datapath.py`, `graph.py`, `keys.py`, `check.py`...).

Method:
1. Reference: `docs/IR.md` (operation catalog, identities, passes, keys §7, limits). Every rewrite rule must be justified by an identity in the doc; otherwise update the doc in the same change.
2. Passes must be sound: a reduction must never change semantics (`tests/test_ir_equivalence.py` simulates via `tests/ir_sim.py`). Add cases to `ir_designs.py`/`ir_dsl.py`.
3. Canonical keys and hashes must stay stable (`test_ir_keys.py`, `test_ir_determinism.py`): the differ depends on them, so reformatting/renaming => delta of exactly 0.
4. Respect the size limits (`ir/limits.py`) and linearity (`test_perf_structure.py`).

Project constraints (non-negotiable): Python 3.11 standard library only, no dependencies; frozen/slotted dataclasses; complete type annotations; deterministic output (no `hash()`, no dependence on dict order, no timestamps); problems are diagnostics, never exceptions between pipeline stages; never write to the skill directory. Tests: `PYTHONPATH=src python3 -m unittest <module>` while iterating, full suite before concluding. Every fixed bug gets a regression test.
