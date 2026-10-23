# Real-world smoke harness (optional)

Not part of the unit-test suite and not run by CI: it needs RTL that is not shipped with the repository.

```bash
python3 tests/smoke/run_smoke.py CORPUS_DIR [--timeout 120] [--json]
```

`CORPUS_DIR` holds one sub-directory per project (for example shallow clones of open-source cores). For each
project the harness runs `gatecost cost --format json --no-cache` and reports: exit code, `INTERNAL` errors
(always a bug), run time, the confidence level, and a coverage summary counting the diagnostic codes raised
(`UNSUPPORTED_CONSTRUCT`, `MISSING_MODULE`, ...), so gaps in the front-ends are measurable (IMPLEMENTATION.md
stage 9). The exit code is 1 when any project crashed (`INTERNAL`, a traceback, or a timeout).
