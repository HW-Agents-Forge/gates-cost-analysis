---
description: Triage and fix a bug reported on real RTL (crash, wrong width, unsupported construct, absurd estimate, slowness)
argument-hint: <path to HDL file | pasted snippet | symptom description>
---

Bug report: $ARGUMENTS

Follow the `fix-hdl-bug` skill:
1. `repo-scout`: locate the code path, existing tests and doc sections involved.
2. `test-engineer`: reduce the input to a minimal case and write a RED regression test (one test per bug).
3. Owning-subsystem agent (frontend-dev, elaborator-dev, ir-analysis-dev, cost-model-dev, diff-report-dev): targeted fix. Constraints: stdlib only, Python 3.11, deterministic output, diagnostics instead of exceptions.
4. `test-runner`: full suite must be green.
5. `docs-keeper` only if behavior or coverage tables changed.

End with: root cause, files changed, test added, and the `done-checklist` result. Do not commit unless asked.
