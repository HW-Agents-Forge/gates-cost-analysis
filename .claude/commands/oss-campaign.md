---
description: Run the open-source RTL campaign and summarize robustness and delta accuracy
---

Use `test-engineer` to run, in order:
1. `python3 scripts/oss_campaign.py fetch`
2. `python3 scripts/oss_campaign.py smoke --no-timing`
3. `python3 scripts/oss_campaign.py deltas --no-timing`
4. `python3 scripts/oss_campaign.py summary --no-timing`

If network access fails, say so and stop; do not fake results.
Report: projects analyzed vs failed, diagnostics by code, INTERNAL errors, deltas in `scripts/oss/deltas.json` whose direction or magnitude looks wrong. Propose follow-up `/triage-bug` runs for the worst items. Do not modify the pinned commits in `scripts/oss/projects.json`.
