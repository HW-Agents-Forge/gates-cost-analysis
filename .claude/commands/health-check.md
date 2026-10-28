---
description: Full project health check (tests, mypy, docs sync, startup budget) with a short verdict
---

Run a complete health check of gatecost and report a compact verdict.

Delegate to the `test-runner` agent (it only returns verdicts):
1. Full suite: `PYTHONPATH=src python3 -m unittest discover -s tests -t .`
2. Docs consistency: `PYTHONPATH=src python3 -m unittest tests.test_docs_sync`
3. Types: `python3 -m mypy --strict src/gatecost`
4. Startup budget: `GATECOST_BENCH=1 PYTHONPATH=src python3 -m unittest tests.test_bench`
5. Launcher sanity: `python3 scripts/gatecost help`

Return a table: check, status (green/red), first useful error line. For every red item, name the owning agent (see `.claude/CLAUDE.md`) and propose, but do not apply, the next step.
