---
description: Check that local checks, CONTRIBUTING.md and the CI workflows agree
---

Use `repo-scout` to compare:
- `.github/workflows/ci.yml` and `.github/workflows/python-package.yml` (OS and Python matrices, mypy blocking or not, flake8 selection, references to missing files such as `requirements.txt`).
- The definition of done and commands in `CONTRIBUTING.md` and the `run-tests` / `done-checklist` skills.

Then `test-runner`: run the exact CI commands locally and report any local-only or CI-only failure.

Return a list of discrepancies with a proposed fix for each (merge duplicate workflows, make mypy blocking if it is clean, update CONTRIBUTING). Apply nothing without my approval.
