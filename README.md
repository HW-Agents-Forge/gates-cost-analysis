<div align="center">

# gatecost

**How many gates does this RTL change cost?** Answered in seconds, without a synthesis tool.

![python](https://img.shields.io/badge/python-3.11%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![languages](https://img.shields.io/badge/Verilog%20%7C%20SystemVerilog%20%7C%20VHDL-supported-informational)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

</div>

Estimate the gate cost of RTL (Verilog, SystemVerilog, VHDL) and of any edit to it, in gate equivalents (GE, one 2-input NAND). No synthesis tool, no dependencies, Python 3.11+. Built to be used by an AI coding agent as a skill, and usable from the command line or CI.

```console
$ python3 scripts/gatecost diff --before baseline/sv --after pipelined/sv --root . --top mac
DELTA +134 GE (+82..+238) +16.3% verdict=INCREASE confidence=low
TOP mac before=819 after=953
+128 mac.sv:16 added DFF x16 (register stage)
+6 mac.sv:16 fusion MUL no longer fused
WHY low: delta range is -38%/+78% of the estimate
```

Every figure comes with a range and a confidence level. Reformatting, renames and file moves give exactly `0`.

## Use

```bash
python3 scripts/gatecost diff                       # your edit against HEAD
python3 scripts/gatecost cost                       # size of the design
python3 scripts/gatecost estimate "mul 32x32"       # a hypothetical construct
python3 scripts/gatecost help
```

A full worked example, with a generated report, is in [`examples/mac`](examples/mac/REPORT.md). More, each with a generated `REPORT.md` (`python3 examples/make_reports.py` regenerates them): [`counter_width`](examples/counter_width/REPORT.md), [`alu_multiplier`](examples/alu_multiplier/REPORT.md), [`fifo_depth`](examples/fifo_depth/REPORT.md), [`fir_pipeline`](examples/fir_pipeline/REPORT.md), [`uart_tx`](examples/uart_tx/REPORT.md), [`sv_features`](examples/sv_features/REPORT.md), [`udp_mux`](examples/udp_mux/REPORT.md), [`vhdl_fsm_alu`](examples/vhdl_fsm_alu/REPORT.md).

## Install as an agent skill

```bash
git submodule add https://github.com/HW-Agents-Forge/gates-cost-analysis .claude/skills/gatecost
```

Use `.github/skills/` for Copilot or `.opencode/skills/` for OpenCode. To skip permission prompts, allow commands starting with `python3 .claude/skills/gatecost/scripts/gatecost`. The agent reads [`SKILL.md`](SKILL.md).

## Limits

These are analytic estimates for ranking options, not synthesis results. Dividers, memories and ROMs are the least accurate. Use synthesis for sign-off.

Calibration status: the default profile (`generic-asic`) is calibrated on one point, Yosys 0.70 with the Nangate 45 nm library (X1 cells), without a timing constraint. Over the 164 frozen cases the typ estimate has a MAPE of 8.4 % and a bias of +1.1 %, and the `[min, max]` range contains the Yosys area in 84 % of the cases. On the 11 complete designs of that set the MAPE is 14.5 % with a bias of +13.9 %: the tool over-estimates because the IR does not recognise some patterns (counter enable not merged, rotation). Details and how to rerun: [`docs/COST_MODEL.md`](docs/COST_MODEL.md) section 5.3, [`CONTRIBUTING.md`](CONTRIBUTING.md).

Validation on open-source code (maintainers' campaign, `scripts/oss_campaign.py`, pinned commits): 13 projects, about 400 tops, 0 crash, 0 `INTERNAL`, 0 timeout. On 31 atomic modifications checked against Yosys, the order of magnitude holds for small changes (FIFOs, memories, registers, counters, CPU): ratios of 0.8 to 1.4 on the large deltas.

Honest limits:

- Control logic and FSMs are still over-estimated, by about 30 %.
- ROMs and memories are good to about 50 %.
- A verdict can have a range that crosses 0: the sign is reliable, the magnitude is not.
- There is no ground truth for SystemVerilog with packages, and VHDL is checked through GHDL only.
- Not covered: other technology nodes and commercial flows, the confidence thresholds, and the constants `sigma` and `alpha`.

Speed (CPython 3.15, laptop): start-up 0.1 s, a small design 0.4 s cold, 10 000 lines about 1.5 s, 100 000 lines about 12 s cold (about 0.9 GB), and a warm `diff` well under a second. These are comfort budgets for agent use, not guarantees.

## Docs

[Spec](docs/SPEC.md) · [CLI](docs/CLI.md) · [Output formats](docs/OUTPUT.md) · [Cost model](docs/COST_MODEL.md) · [Contributing](CONTRIBUTING.md) · MIT license
