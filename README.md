<div align="center">

# gatecost

**How many gates does this RTL change cost?** Answered in under a second, without a synthesis tool.

![python](https://img.shields.io/badge/python-3.11%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![languages](https://img.shields.io/badge/Verilog%20%7C%20SystemVerilog%20%7C%20VHDL-supported-informational)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

</div>

`gatecost` estimates the logic cost of RTL in **gate equivalents** (GE, one 2-input NAND) and, above all, the
**cost delta of a modification**, with the source lines responsible and an honest confidence level. It is
built to be called by an AI coding agent as a *skill*, and works just as well from the command line or CI.

- **Fast**: no daemon, no EDA tool, no network, **standard library only** (Python 3.11+).
- **Explainable**: every figure comes with a range (`min..max`), a confidence level and the lines that caused it.
- **Stable**: reformatting, comments, renames and file moves give exactly `0`.
- **Multi-language**: Verilog, SystemVerilog and VHDL, including mixed designs.
- **Agent-friendly**: a compact ~400-token text format, plus a stable JSON schema.

## Example

Widen a parameter, ask what it cost:

```console
$ python3 scripts/gatecost diff
DELTA +1608 GE (-5..+4644) +104.9% verdict=UNCERTAIN confidence=low
TOP top before=1533 after=3141
+1608 a.sv:1 parameter W 16->24 (2 ops) x1 cause=parameter:W
WHY low: delta range is +/-145% of the estimate
```

One edit, one explained contributor. The first line is the answer: the delta, its plausible range, a verdict
and a confidence. Compare options *before* writing code:

```console
$ python3 scripts/gatecost estimate --compare "mul 32x32" "mul 32 const=10"
$ python3 scripts/gatecost estimate --rtl "assign y = a*b + c;" --widths a=16,b=16,c=32
$ python3 scripts/gatecost estimate "mul 16x16 + add 32"
ESTIMATE 1566 GE (1474..2882) confidence=medium
OP MUL 16x16 unsigned + ADD 32
ARCH min=booth_wallace+ripple typ=booth_wallace+brent_kung max=wallace_ks+kogge_stone
WHY medium: delta range is +/-45% of the estimate
```

## Commands

| Command | What it does |
|---|---|
| `diff` | Cost delta of your edit (against `HEAD`, a git ref, a snapshot, or two directories), with attribution. |
| `cost` | Absolute size of the design and its largest modules. |
| `estimate` | Cost of a hypothetical construct, an RTL fragment, or a comparison of alternatives. |
| `snapshot` | Record a baseline when there is no git. |
| `init`, `profiles`, `clean`, `validate-output` | Config file, cost profiles, caches, JSON schema check. |

Formats: `--format agent` (default), `json`, `text`, `markdown` (for pull-request comments).
Budgets for CI: `--budget GE` and `--fail-on-increase GE` exit with code 1 when exceeded.

## How it works

```
sources -> lexer/parser -> elaborator -> word-level IR -> cost model -> cost tree
                                                                           |
baseline ------------------------- (same pipeline) ---------------------> differ -> reporters
```

- A hand-written, error-tolerant front-end per language: a broken construct produces a diagnostic, never a crash.
- Procedural code becomes dataflow; registers, resets, enables, latches and memories are recognised.
- Costs are built from library primitives (NAND2, XOR2, FA, DFF, ...) under three consistent scenarios
  (`min`, `typ`, `max`), after reductions that mimic synthesis (constant propagation, unused bits, sharing, fusion).
- The differ matches operations across versions and explains each change with a reason
  (`added`, `width`, `parameter`, `sharing`, ...); contributors add up exactly to the total.

Details: [`docs/SPEC.md`](docs/SPEC.md), [`docs/COST_MODEL.md`](docs/COST_MODEL.md), [`docs/DIFF.md`](docs/DIFF.md).

## Install as an agent skill

Add the repository as a git submodule inside the skills directory that your agent toolchain reads:

```bash
git submodule add <repository-url> .claude/skills/gatecost      # Claude Code
git submodule add <repository-url> .github/skills/gatecost      # GitHub Copilot
git submodule add <repository-url> .opencode/skills/gatecost    # OpenCode
```

Use the line that matches your toolchain (or several). The tool behaves identically wherever it is mounted and
never writes inside its own directory, so the submodule stays clean. Pin it to a reviewed commit or tag: the
skill runs code from the repository inside your project.

### Permission rule

One rule covers every command, because they all go through the launcher. Without it the agent asks before each
call. Substitute the mount path you chose.

Claude Code, `.claude/settings.json`:

```json
{ "permissions": { "allow": ["Bash(python3 .claude/skills/gatecost/scripts/gatecost *)"] } }
```

GitHub Copilot, in VS Code `settings.json` (terminal auto-approve; the Copilot CLI equivalent is
`--allow-tool 'shell(python3)'`):

```json
{ "chat.tools.terminal.autoApprove": { "/^python3 \\.github\\/skills\\/gatecost\\/scripts\\/gatecost\\b/": true } }
```

OpenCode, `opencode.json`:

```json
{ "permission": { "bash": { "python3 .opencode/skills/gatecost/scripts/gatecost *": "allow" } } }
```

The agent toolchains evolve their permission syntax; if a rule is not honored, check your toolchain's current
documentation. The pattern to allow is always "commands starting with `python3 <skill-dir>/scripts/gatecost`".

### Minimal `gatecost.toml`

Optional, at the root of the host project. Without it the sources and the top module are discovered
automatically. `gatecost init` writes a commented file from what it detects.

```toml
[project]
top = "top_core"
sources = ["rtl/**/*.sv", "rtl/**/*.vhd"]
incdirs = ["rtl/include"]
exclude = ["rtl/legacy/**"]

[cost]
profile = "generic-asic"
```

All keys are listed in `docs/CLI.md` section 4.

### Update

```bash
git submodule update --remote .claude/skills/gatecost     # or the path you chose
git add .claude/skills/gatecost && git commit -m "Update gatecost"
```

The version is the submodule commit; `python3 <skill-dir>/scripts/gatecost --version` prints the `VERSION` file.

## Use it without an agent

```bash
python3 scripts/gatecost estimate "mul 32x32"
python3 scripts/gatecost estimate --compare "mul 32x32" "mul 32 const=10"
python3 scripts/gatecost cost          # run from your project directory
python3 scripts/gatecost diff          # working tree against HEAD
python3 scripts/gatecost help
```

`pip install .` also provides a `gatecost` console command with the same behavior (needs setuptools; not
needed for the skill).

## Accuracy and limits

The figures are analytic **estimates**, good for ranking options and judging orders of magnitude, not for
sign-off. Dividers, memories and ROMs are the least accurate; non-synthesizable code is ignored; timing,
power and routing are not modeled. Every result carries a range and a confidence level. The built-in
`generic-asic` profile has not yet been calibrated against real synthesis reports
(`scripts/validate_reference.py` is provided for that): use real synthesis for final decisions.

Known gaps are listed in [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) (status per stage).

## Documentation

| | |
|---|---|
| [`SKILL.md`](SKILL.md) | What the agent does and how it words answers |
| [`docs/SPEC.md`](docs/SPEC.md) | Specification |
| [`docs/CLI.md`](docs/CLI.md) | Commands, options, `estimate` notation, `gatecost.toml` |
| [`docs/OUTPUT.md`](docs/OUTPUT.md) | Output formats and JSON schema |
| [`docs/COST_MODEL.md`](docs/COST_MODEL.md) | Primitives, formulas, uncertainty |
| [`docs/DIFF.md`](docs/DIFF.md) | Matching and attribution |
| [`docs/FRONTEND.md`](docs/FRONTEND.md), [`docs/IR.md`](docs/IR.md) | Front-ends and intermediate representation |
| [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) | The skill model and launcher |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development setup and conventions |

## License

MIT, see [`LICENSE`](LICENSE).
