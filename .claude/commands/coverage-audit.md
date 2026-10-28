---
description: Cross-check HDL language coverage (support matrices vs diagnostics vs fixtures) and rank gaps
---

Audit language coverage with `repo-scout`, then `test-engineer` for the fixture check.

1. List every `UNSUPPORTED_CONSTRUCT` emission site under `src/gatecost/` (frontend, elab).
2. Extract the support matrices in `docs/FRONTEND.md` (Verilog/SV section and VHDL section, including "Still open" and "Skipped" rows).
3. Verify each matrix row has at least one fixture or test; list rows with none (IMPLEMENTATION.md stage 6 admits this is not verified row by row).
4. Compare emission sites vs matrix rows: undocumented unsupported constructs, and documented-supported constructs that still emit a diagnostic.
5. Rank the gaps by likely frequency in real RTL (use `tests/corpus` and `scripts/oss/` projects as evidence).

Return a ranked table of at most 15 gaps with a proposed owner agent and whether the `add-hdl-construct` skill applies. Make no code changes.
