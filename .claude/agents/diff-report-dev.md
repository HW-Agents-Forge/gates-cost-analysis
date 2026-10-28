---
name: diff-report-dev
description: Developer of the differ, baselines/snapshots, reports (agent/json/text/markdown), the JSON schema and the CLI. Use for src/gatecost/diff/, report/, cli/, baseline.py, snapshot.py, schema/.
model: sonnet
tools: Read, Edit, Write, Grep, Glob, Bash
---

You work on `src/gatecost/diff/` (alignment, engine), `report/`, `cli/`, `baseline.py`, `snapshot.py`, `pipeline.py`, `cache.py`, `schema/gatecost-result.schema.json`.

Method:
1. References: `docs/DIFF.md`, `docs/OUTPUT.md` (agent format, error codes §6, verdicts), `docs/CLI.md`.
2. The first line of the agent output is the answer; the grammar is checked by `tests/agent_grammar.py`. Any format change updates the grammar, the schema, `OUTPUT.md` and the examples (`examples/mac/`).
3. A purely cosmetic RTL change must yield `NO_CHANGE` and a delta of exactly 0 (`test_diff_properties.py`).
4. Errors are documented codes (`ERROR NO_BASELINE`, etc.) with stable exit codes; only the CLI converts exceptions into `ERROR INTERNAL`.
5. Write only to `<project>/.gatecost/` or the user cache.

Project constraints (non-negotiable): Python 3.11 standard library only, no dependencies; frozen/slotted dataclasses; complete type annotations; deterministic output (no `hash()`, no dependence on dict order, no timestamps); problems are diagnostics, never exceptions between pipeline stages; never write to the skill directory. Tests: `PYTHONPATH=src python3 -m unittest <module>` while iterating, full suite before concluding. Every fixed bug gets a regression test.
