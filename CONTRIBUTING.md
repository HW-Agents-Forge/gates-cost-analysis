# Contributing

## Setup

Python 3.11 or later and the standard library; there is nothing to install. Run the tool from a checkout:

```bash
python3 scripts/gatecost help
```

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
PYTHONPATH=src python3 -m unittest tests.test_docs_sync     # one module
```

The suite uses `unittest` only. It must pass on Python 3.11 (the oldest supported interpreter) and on the
latest release; CI runs both on Linux and macOS. Some modules are slow (fuzz, speed); run one module while
iterating and the whole suite before you finish.

Optional, not part of CI: `mypy --strict src` (type check), `tests/smoke/` (real-world RTL smoke run, see its
README), `scripts/validate_reference.py` (maintainers only: compares estimates with reference synthesis areas
that you supply), and the calibration bench below.

## Calibration against Yosys (maintainers only, not in CI)

The built-in `generic-asic` profile is calibrated against Yosys 0.70 and the Nangate 45 nm library (results and
limits: `docs/COST_MODEL.md` section 5.3). The bench needs Yosys and the liberty file only to refresh the
reference; the suite itself reads the frozen reference and needs neither.

```bash
python3 scripts/calibration_bench.py --frozen                  # estimates vs the frozen areas, no Yosys
python3 scripts/calibration_bench.py --frozen --only add,mul   # categories or name prefixes
python3 scripts/calibration_bench.py                           # also runs Yosys on every case
python3 scripts/calibration_bench.py --freeze                  # rewrite tests/reference/yosys_nangate45.csv
```

- It generates 131 micro-benchmarks in `tests/reference/designs/<name>/t.v` (identical on every run), adds the
  repository examples and corpus designs, runs `scripts/yosys_area.py` and gatecost on each, and writes
  `results.csv` and a summary (MAPE, bias, coverage of `[min, max]`, per category, ten worst cases) to `--out`
  (default: a temporary directory). `--profile NAME` evaluates another profile.
- `scripts/yosys_area.py` runs `read_verilog -sv`, `hierarchy`, `synth -flatten`, `dfflibmap -liberty`,
  `abc -liberty` (no `-D`, so no timing constraint), `opt_clean -purge` and `stat -liberty`. GE is the area divided
  by the `NAND2_X1` area read from the liberty file. The liberty path comes from `--liberty` or `GATECOST_LIBERTY`;
  the default is `~/gatecost-refs/nangate45/NangateOpenCellLibrary_typical.lib`, outside the repository.
- `scripts/yosys_area.py` also takes `-I/--incdir DIR`, `-D/--define NAME[=VAL]`, `--param NAME=VALUE` (a `chparam` applied before `hierarchy -top`), `--top`, several source files, and `--filelist/-f FILE` (one source per line, `+incdir+`, `+define+`, `-I`, nested `-f`, `//` and `#` comments, `$VAR` expansion). A design with no cell at all gives 0 GE with a note.
- `tests/reference/yosys_nangate45.csv` holds 164 frozen cases (header: Yosys 0.70+post sha `0c11ea09`,
  2026-10-26). Only `--freeze` writes it, never by hand; refreeze when Yosys, the library or the RTL of a case
  changes, and say so in the commit.
- `tests/test_calibration_reference.py` checks the parsing, the frozen file and a representative subset of the
  micro-benchmarks (within 12% of Yosys). After a change of the cost model, run the bench with `--frozen` and
  update `docs/COST_MODEL.md` section 5.3 and `README.md` with the new figures.

## Open-source validation campaign (maintainers only, not in CI)

`scripts/oss_campaign.py` replays gatecost on pinned open-source projects. It needs `git`, network access for `fetch`, and optionally Yosys with the Nangate 45 nm liberty file (and GHDL for VHDL) for ground truth. The suite itself does not need any of it.

```bash
python3 scripts/oss_campaign.py fetch                 # git clone --depth 1 at the pinned commit
python3 scripts/oss_campaign.py smoke --no-timing     # cost on every listed top: crashes, INTERNAL, timeouts
python3 scripts/oss_campaign.py deltas --no-timing    # 31 atomic modifications, gatecost diff vs Yosys
python3 scripts/oss_campaign.py summary --no-timing   # regenerate the .md and .csv from the .json files
```

- `fetch` clones into `$GATECOST_OSS_DIR` (default `~/gatecost-refs/oss`), one `<dir>/repo` per project, at the commit pinned in `scripts/oss/projects.json` (13 projects). It only runs `git`: no hook, and no code of the projects is ever executed.
- `deltas` applies the declarative edits of `scripts/oss/deltas.json` (literal substitutions, no code), runs `gatecost diff`, and, when Yosys is available, the same edit through `scripts/yosys_area.py` as ground truth. `--no-truth` skips Yosys.
- Results go to `--out` (default `$GATECOST_OSS_DIR/_out`). `--no-timing` drops the time and memory columns, so two runs give byte-identical summaries.
- Latest result: 0 crash, 0 `INTERNAL`, 0 timeout on about 400 tops (`docs/IMPLEMENTATION.md` section 6).

## Conventions

The full list is `docs/IMPLEMENTATION.md` section 1. In short:

- Standard library only, Python 3.11 syntax and library features at most (a lint test enforces both).
- Pipeline stages are pure functions of immutable inputs; I/O stays in `cli` and the small I/O layer.
- Frozen, slotted dataclasses for IR and AST nodes; full type annotations.
- Deterministic output: no dictionary-order dependence, no `hash()`, no timestamps in results.
- Problems are diagnostics, not exceptions across stage boundaries; only the CLI maps unexpected exceptions
  to `ERROR INTERNAL`.
- Never write inside the skill directory; writes go to `<project>/.gatecost/` or the user cache.
- Golden files are regenerated by an explicit script, never automatically.
- `SKILL.md` stays under 120 lines and contains no mount path.

## Definition of done

1. The behavior matches the specification sections it refers to, or the specification is updated in the same
   change with the reason.
2. The tests of those sections pass, plus a regression test for every defect fixed.
3. The whole suite passes on both interpreters; the start-up benchmark still stays within its budget (a comfort budget for agent use, `docs/SPEC.md` section 12).
4. `SKILL.md`, `README.md` and `docs/` match the tool (`tests/test_docs_sync.py` passes).
5. No new dependency and no write outside the documented locations.
6. `docs/IMPLEMENTATION.md` status table updated if a stage changed state.
