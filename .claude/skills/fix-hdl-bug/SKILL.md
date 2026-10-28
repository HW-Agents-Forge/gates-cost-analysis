---
name: fix-hdl-bug
description: Workflow to fix a gatecost bug revealed by real RTL (INTERNAL crash, wrong width, unsupported construct, absurd estimate, slowness): reduce, red test, targeted fix, regression, full suite. Use for any bug report on Verilog/SystemVerilog/VHDL input.
---

# fix-hdl-bug

## 1. Reproduce

```bash
python3 scripts/gatecost cost --no-cache --format json   # or diff, from the faulty RTL's directory
```

Identify the symptom: `ERROR INTERNAL` / traceback (always a bug), frequent `UNSUPPORTED_CONSTRUCT`, absurd result, abnormal delay.

## 2. Reduce

Reduce the RTL to the smallest fragment that triggers the symptom (remove modules, signals, branches as long as the symptom persists). An inline fragment is often enough: `estimate --rtl "assign y = a*b + c;" --widths a=16,b=16,c=32`.

## 3. Locate the responsible layer

| Symptom | Layer | Agent |
|---|---|---|
| Syntax error, token, preprocessor | `frontend/` | `frontend-dev` |
| Wrong width/sign, parameters, hierarchy, combinational loop, violated IR invariant | `elab/` | `elaborator-dev` |
| Wrong reduction, known-bits, sharing | `ir/` | `ir-analysis-dev` |
| Absurd figure for a correct operation | `cost/` | `cost-model-dev` |
| Wrong delta or verdict, format | `diff/`, `report/` | `diff-report-dev` |
| Compute time | whole pipeline | `perf-optimizer` |

## 4. Red test first

Add the reduced fragment as a test in the subsystem's module (model: `tests/test_elab_regressions.py`: `run(text)` helper + assertion on the diagnostic codes or the figure). Run it and confirm it fails for the right reason.

## 5. Fix

- Root cause, not a patch on the symptom.
- No exceptions raised across stages: produce a diagnostic.
- Deterministic output; no dependency added.

## 6. Verify

1. The regression test passes.
2. The subsystem's module passes, then the full suite (skill `run-tests`).
3. If the fix changes documented behavior, update `docs/` in the same change (skill `docs-sync`).
4. Report: root cause, file:line, test added, what could not be verified.
