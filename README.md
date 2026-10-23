<div align="center">

# gatecost

**How many gates does this RTL change cost?** Answered in under a second, without a synthesis tool.

![python](https://img.shields.io/badge/python-3.11%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![languages](https://img.shields.io/badge/Verilog%20%7C%20SystemVerilog%20%7C%20VHDL-supported-informational)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

</div>

Estimate the gate cost of RTL (Verilog, SystemVerilog, VHDL) and of any edit to it, in gate equivalents (GE, one 2-input NAND). No synthesis tool, no dependencies, Python 3.11+. Built to be used by an AI coding agent as a skill, and usable from the command line or CI.

```console
$ python3 scripts/gatecost diff
DELTA +159 GE (-221..+473) +21.4% verdict=UNCERTAIN confidence=low
TOP mac before=743 after=902
+496 mac.sv:16 added DFF x16 (register stage) + MUL 8x8
-496 mac.sv:21 removed MAC 8x8
+159 mac.sv:21 added ADD w24
```

Every figure comes with a range and a confidence level. Reformatting, renames and file moves give exactly `0`.

## Use

```bash
python3 scripts/gatecost diff                       # your edit against HEAD
python3 scripts/gatecost cost                       # size of the design
python3 scripts/gatecost estimate "mul 32x32"       # a hypothetical construct
python3 scripts/gatecost help
```

A full worked example, with a generated report, is in [`examples/mac`](examples/mac/REPORT.md).

## Install as an agent skill

```bash
git submodule add https://github.com/HW-Agents-Forge/gates-cost-analysis .claude/skills/gatecost
```

Use `.github/skills/` for Copilot or `.opencode/skills/` for OpenCode. To skip permission prompts, allow commands starting with `python3 .claude/skills/gatecost/scripts/gatecost`. The agent reads [`SKILL.md`](SKILL.md).

## Limits

These are analytic estimates for ranking options, not synthesis results. Dividers, memories and ROMs are the least accurate, and the cost profile has not been calibrated against a real flow yet. Use synthesis for sign-off.

## Docs

[Spec](docs/SPEC.md) · [CLI](docs/CLI.md) · [Output formats](docs/OUTPUT.md) · [Cost model](docs/COST_MODEL.md) · [Contributing](CONTRIBUTING.md) · MIT license
