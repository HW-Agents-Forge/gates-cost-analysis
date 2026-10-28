---
name: add-hdl-construct
description: Adds support for a Verilog/SystemVerilog/VHDL construct to gatecost, from lexer/parser to cost: tokens, AST, elaboration, IR, Verilog/VHDL tests, coverage table. Use when a construct produces UNSUPPORTED_CONSTRUCT or is wrongly ignored.
---

# add-hdl-construct

A construct goes through the `frontend → elab → ir → cost` pipeline. Proceed in that order and move to the next step only once the previous one is tested.

## 0. Scope

- Read the matching row of the coverage table in `docs/FRONTEND.md` (current state, expected semantics).
- Decide on the hardware model: what does this construct synthesize (combinational logic, register, memory, nothing)? If it is not synthesizable (testbench, assertions), it must be ignored silently, without a noisy diagnostic.

## 1. Front-end (`src/gatecost/frontend/`)

- Lexer/preprocessor if there is a new token or directive.
- AST (`verilog/ast.py`, `vhdl/nodes.py`): frozen/slotted dataclasses.
- Parser with error recovery: a malformed fragment must not prevent analysis of the surrounding code.

## 2. Elaboration (`src/gatecost/elab/`)

- Width and sign rules for the construct, constant evaluation if needed.
- Lower to the IR using the existing operations of the catalog (`docs/IR.md` §2–3); add an IR operation only as a last resort, with its entry in `IR.md`, its cost in `COST_MODEL.md`, and its cases in `tests/ir_sim.py`.
- Unresolved value: documented default + diagnostic (`ASSUMED` / `UNRESOLVED_PARAM`).

## 3. Tests

- Parser/elaborator for the language concerned (`tests/test_elab_*.py`, `tests/test_elab_vhdl_*.py`).
- If the construct exists in both languages, an equivalence test (`test_elab_vhdl_equiv.py`): same costs for the same hardware.
- Invariance: reformatting or renaming does not change the result.
- Fuzz/mutation of the module concerned; speed on a large instance (no quadratic behavior).

## 4. Documentation

Switch the `docs/FRONTEND.md` row to "supported", and update `docs/IMPLEMENTATION.md` if a step changes state (skill `docs-sync`).

## 5. Close

Full suite (skill `run-tests`), then `done-checklist`.
