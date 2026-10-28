---
description: Hunt for new bugs by running fuzz tests and the smoke harness on a corpus of real RTL
argument-hint: [corpus directory]
---

Bug hunt on: ${ARGUMENTS:-tests/corpus}

Delegate to `test-engineer`:
1. Run the fuzz suites (`tests/test_verilog_fuzz.py`, `tests/test_vhdl_fuzz.py`) with more seeds if supported.
2. Run `python3 tests/smoke/run_smoke.py <corpus> --timeout 120 --json` and collect INTERNAL errors, timeouts, UNSUPPORTED_CONSTRUCT clusters and absurd estimates (zero or huge GE).
3. Group findings by root cause, reduce each group to a minimal reproducer, and report.

Return the top 5 findings with reproducer, suspected subsystem and owner agent. Do not fix anything here; suggest running `/triage-bug` for each.
