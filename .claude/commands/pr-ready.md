---
description: Final gate before committing or opening a PR (done-checklist, atomic commit plan)
---

1. `repo-scout`: summarize `git status` and the diff by subsystem.
2. Run the `done-checklist` skill and `test-runner` (full suite, docs sync, mypy, startup budget).
3. Confirm each fixed bug has a regression test and that no file was written outside the documented zones (never the skill directory).
4. Propose an atomic commit plan (one commit per logical change, imperative messages).

Wait for my approval, then use `git-committer` to commit on a feature branch and push. Never push to the default branch and never create a PR unless I ask explicitly.
