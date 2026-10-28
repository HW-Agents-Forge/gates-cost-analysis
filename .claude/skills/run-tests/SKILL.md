---
name: run-tests
description: Runs the gatecost tests (unittest, stdlib) at the right level: one module while iterating, the full suite before concluding, plus optional benchmarks, smoke, and mypy. Use whenever a code change needs verifying.
---

# run-tests

Everything runs from the repository root with `PYTHONPATH=src`. There is nothing to install.

## Choose the level

| Need | Command |
|---|---|
| One module (iteration) | `PYTHONPATH=src python3 -m unittest tests.test_cost_formulas` |
| A single test | `PYTHONPATH=src python3 -m unittest tests.test_cli -k launcher` |
| Full suite (before concluding) | `PYTHONPATH=src python3 -m unittest discover -s tests -t .` |
| Docs / SKILL.md / stdlib lint | `PYTHONPATH=src python3 -m unittest tests.test_docs_sync tests.test_agent_grammar` |
| Performance budgets | `GATECOST_BENCH=1 PYTHONPATH=src python3 -m unittest tests.test_bench` |
| Type check (optional) | `mypy --strict src` |
| Real RTL (optional) | `python3 tests/smoke/run_smoke.py CORPUS_DIR --json` |

## Which module for which subsystem

- front-end: `test_verilog_fuzz`, `test_udp`, `test_elab_vhdl_*`, corpus `tests/corpus/`
- elaboration: `test_elab_*` (`regressions`, `widths`, `hierarchy`, `params`, `speed`, `invariance`)
- IR: `test_ir_*`
- cost: `test_cost_*`
- diff / reports / CLI: `test_diff_*`, `test_report`, `test_cli`, `test_e2e_*`, `test_snapshot`, `test_baseline`
- docs: `test_docs_sync`, `test_agent_grammar`

## Rules

- The project must pass on Python 3.11 (oldest supported) and on the latest version; if only one interpreter is available, say so in the report.
- Some modules (fuzz, speed, campaign) are slow: rerun them in full only once, at the end of the work.
- If a test fails, report it with its output; never ignore, disable, or skip a test to get a green result.
- Golden files are regenerated only by an explicit script, never automatically.
