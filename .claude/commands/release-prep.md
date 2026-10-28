---
description: Prepare a release (version bump, changelog draft, packaging check, checklist) without tagging
argument-hint: <new version, e.g. 0.2.0>
---

Prepare release $ARGUMENTS. Do not tag, push or publish.

1. `repo-scout`: list commits since the last tag (or all commits if none) and group them by subsystem.
2. `docs-keeper`: draft a CHANGELOG entry (create `CHANGELOG.md` if missing), update `VERSION`, and check that the status table in `docs/IMPLEMENTATION.md` and the license statement in `docs/SPEC.md` are accurate.
3. `test-runner`: run the full suite, mypy, startup budget, and a packaging build check (`setup.py` must copy `VERSION` and `schema/` into `gatecost/_data`; verify the JSON schema ships and `--version` prints $ARGUMENTS).
4. Run the `done-checklist` skill.

Return a go/no-go with blockers and the exact commands to tag once I approve.
