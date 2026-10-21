---
name: gatecost
description: Estimate the logic-gate cost (gate equivalents, GE, NAND2-equivalents) of RTL, and the cost delta of a modification, for Verilog, SystemVerilog and VHDL. Use when asked how many gates, how much area, or how big or expensive an RTL/HDL change is, to compare hardware design alternatives before writing them, or to check that an edit did not grow a design. Fast approximation without a synthesis tool; it does not do synthesis, timing, power or simulation.
---

# gatecost

Fast, approximate gate-count estimation for RTL (Verilog, SystemVerilog, VHDL). It reports the cost change of an edit, which source lines cause it, and how far the figure can be trusted. No synthesis tool is needed.

Run the launcher that sits next to this file (use the directory of this `SKILL.md` as `<skill-dir>`):

```bash
python3 <skill-dir>/scripts/gatecost <command>
```

## Which command

| Situation | Command |
|---|---|
| You edited RTL in a git repository and want the cost of the edit | `diff` |
| Work is already committed on a branch | `diff --base-ref merge-base` |
| Compare design options before writing code | `estimate "mul 32x32"` or `estimate --rtl "assign y = a*b + c;" --widths a=16,b=16,c=32` |
| No git: record the state, edit, compare | `snapshot`, then `diff --since-snapshot` |
| Total size of the design and largest modules | `cost` |

Run from the project directory. Sources, the top module, and includes are found automatically; add `--top <module>` only if the tool asks for it. Add `--max-lines N` for more detail.

## Reading the output

The first line is the answer:

```
DELTA +342 GE (+281..+410) +1.2% verdict=INCREASE confidence=medium
```

- `GE` = gate equivalents (one 2-input NAND). The parentheses give the plausible range.
- Following lines: `<delta> <file>:<line> <reason> <what>` sorted by impact. `OTHERS` is the sum of small items.
- `WHY` explains a confidence below `high`. `WARN` lists code that could not be costed. `ASSUMED` lists assumptions made, for example a parameter taken from its default.
- Verdicts: `INCREASE`, `DECREASE`, `NEGLIGIBLE`, `NO_CHANGE` (the edit does not change the hardware as modeled: formatting, comments, renames), `UNCERTAIN` (the sign is not established), `NOT_COMPARABLE`.

## How to answer the user

1. Give the figure with its range, never alone: "about +340 GE, between +280 and +410".
2. State the confidence. If it is `low`, repeat the `WHY` line and avoid precise conclusions.
3. Mention any `WARN` or `ASSUMED` line: part of the change was not costed, or was costed under an assumption.
4. Explain the main contributors in the user's terms (file, line, what changed), not tool terms.
5. Do not present `NO_CHANGE` as a saving, and do not present `UNCERTAIN` as an increase or a decrease.
6. These are estimates for ranking options and judging orders of magnitude. For sign-off decisions, recommend real synthesis.

## When something goes wrong

- `ERROR NO_BASELINE`: not a git repository and no snapshot. Use `snapshot` before editing, or pass two directories: `diff <before_dir> <after_dir>`.
- `ERROR NO_SOURCES`: no RTL found from the current directory. Run from the project root or pass paths.
- `ERROR TOP_NOT_FOUND`, or several candidate tops: pass `--top <module>`.
- `WARN MISSING_MODULE` / `UNRESOLVED_PARAM`: a unit or parameter is unknown to the tool; the result is partial. Pass `--incdir`, `--define NAME=VALUE`, or the missing files.
- `ERROR PYTHON_TOO_OLD`: Python 3.9 or later is required.

## Limits

Estimation is analytic, not synthesis. Dividers, memories and ROMs are the least accurate; non-synthesizable code (testbenches, assertions) is ignored. Timing, power and routing are not modeled.

For details (configuration file, profiles, output schema), see `docs/` in this directory: `OUTPUT.md` for the formats, `COST_MODEL.md` for how costs are computed. Read them only if needed.
