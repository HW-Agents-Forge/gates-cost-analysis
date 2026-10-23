# gates-cost-analysis (gatecost)

Fast, approximate gate-count estimation for RTL (Verilog, SystemVerilog, VHDL), packaged as an agent skill.
It answers "how much bigger did this edit make the design?" in gate equivalents (GE, one 2-input NAND), with
the source lines responsible and an honest confidence level. No synthesis tool, no network, no dependencies:
Python 3.11 or later and its standard library are enough.

The repository itself is the skill: `SKILL.md` is what the agent reads, `scripts/gatecost` is the launcher.

## Adopt it in a project

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

## Status and limits

Work in progress: `docs/IMPLEMENTATION.md` has a status table per stage. Commands whose stage has not landed
print `ERROR NOT_IMPLEMENTED`.

The figures are analytic estimates, good for ranking options and judging orders of magnitude, not for
sign-off: dividers, memories and ROMs are the least accurate, non-synthesizable code is ignored, and timing,
power and routing are not modeled. Every result carries a range and a confidence level; use real synthesis for
final decisions.

## More

- `SKILL.md`: what the agent does and how it words answers.
- `docs/SPEC.md`: specification; `docs/CLI.md`: commands and configuration; `docs/OUTPUT.md`: output formats;
  `docs/COST_MODEL.md`: how costs are computed; `docs/DISTRIBUTION.md`: the skill model.
- `CONTRIBUTING.md`: development setup and conventions. License: `LICENSE`.
