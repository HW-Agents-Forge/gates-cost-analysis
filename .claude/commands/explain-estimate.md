---
description: Explain why gatecost produced a given cost estimate for a design, and sanity-check it
argument-hint: <HDL file or module> [top module]
---

Explain the estimate for: $ARGUMENTS

1. Run the `estimate` / `cost` commands on the input with JSON output and, if available, the IR dump.
2. Delegate to `cost-model-dev` to break the total down by operation kind, show which formula and profile value produced each part, and give the min/typ/max scenarios with confidence.
3. Sanity-check against intuition (register count, widths, multipliers, muxes). Flag suspicious contributions, such as dead logic counted, shared logic double-counted, or width inference too wide, and name the owning subsystem.

Read-only: produce the explanation and, if a bug is suspected, a ready-to-paste `/triage-bug` invocation.
