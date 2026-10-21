# gates-cost-analysis (gatecost)

Fast, approximate gate-count estimation for RTL (Verilog, SystemVerilog, VHDL), packaged as an agent skill.

Status: implementation in progress, see `docs/IMPLEMENTATION.md`. Stage 1 (skeleton, launcher, command line,
output formats) is done; analysis commands return `ERROR NOT_IMPLEMENTED` until their stage lands.

```bash
python3 scripts/gatecost help
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

Specification: `docs/SPEC.md`. Agent entry point: `SKILL.md`.
