---
name: docs-sync
description: Synchronize gatecost's SKILL.md, README.md, docs/, and examples with the code, and pass the documentation consistency tests (test_docs_sync, agent output grammar). Use after any change to the CLI, output format, cost model, or language coverage.
---

# docs-sync

## What to check depending on the change

| Change | Documents |
|---|---|
| CLI option/command | `docs/CLI.md`, `SKILL.md` ("Which command" table), `README.md`, `gatecost help` output |
| Output format, verdict, error code | `docs/OUTPUT.md`, `schema/gatecost-result.schema.json`, `tests/agent_grammar.py`, `SKILL.md` |
| Cost formula | `docs/COST_MODEL.md`, worked examples (`README.md`, `examples/mac/`) |
| Supported HDL construct | coverage table in `docs/FRONTEND.md` |
| IR rule / pass | `docs/IR.md` |
| Differ / alignment | `docs/DIFF.md` |
| Completed step | status table in `docs/IMPLEMENTATION.md` |
| Conventions / process | `CONTRIBUTING.md` |

## `SKILL.md` constraints (the product skill, at the root)

- Frontmatter = exactly `name` and `description`; description bounded and using a controlled vocabulary.
- Fewer than 120 lines; no hardcoded mount path; references only existing `docs/` files.
- Every command quoted must be accepted by the parser; every `estimate` example must run.

## Verify

```bash
PYTHONPATH=src python3 -m unittest tests.test_docs_sync tests.test_agent_grammar
python3 scripts/gatecost help
```

## Principle

The documents are the specification. If code and docs diverge, decide which one is wrong, and say so explicitly in the report: never align the docs to a bug without flagging it.

Do not confuse `SKILL.md` (product skill for agents using the tool) with `.claude/skills/` (development skills for this repository).

Recommended agent: `docs-keeper`.
