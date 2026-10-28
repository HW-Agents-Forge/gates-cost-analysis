---
name: done-checklist
description: gatecost "definition of done" checklist to run before concluding a change or opening a PR (spec, tests, two interpreters, benchmark, docs, no dependency, no writes outside allowed areas). Use at the end of a task.
---

# done-checklist

Source: `CONTRIBUTING.md` § Definition of done and `docs/IMPLEMENTATION.md` §5. Go through each item and answer honestly; an unverified item is reported as such.

1. **Spec**: the behavior follows the cited sections of `docs/`, or the spec is updated in the same change with the reason.
2. **Tests**: tests for the sections concerned + a regression test for each fixed defect.
3. **Full suite**: `PYTHONPATH=src python3 -m unittest discover -s tests -t .` is green. Python 3.11 and the latest version if available (CI: Linux + macOS × 3.11 / 3.x). Startup benchmark within its budget (`GATECOST_BENCH=1 … tests.test_bench`) if the startup path or hot loops changed.
4. **Docs**: `SKILL.md`, `README.md`, `docs/` match the tool; `tests.test_docs_sync` passes.
5. **Hygiene**:
   - no new dependency, stdlib only, syntax ≤ 3.11;
   - no writes to the skill directory (only `<project>/.gatecost/` or the user cache);
   - deterministic output (no `hash()`, dict ordering, timestamps);
   - errors as diagnostics, no exceptions between stages;
   - golden files not regenerated automatically.
6. **Status**: table in `docs/IMPLEMENTATION.md` updated if a step changed state.
7. **Diff review**: reread `git diff` looking for what CI would reject (types, stdlib lint, temporary files, `__pycache__`).

Optional: `mypy --strict src`, smoke run on an RTL corpus.

Final report: what is done and verified, what is not, with the test output in case of failure.
