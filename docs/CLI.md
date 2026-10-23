# gatecost — Command-Line and Configuration Reference

Status: Draft. Normative companion to `SPEC.md` §8 and §10.

This document defines every command, option, the `estimate` input language, and the configuration file. Output formats are defined in `OUTPUT.md`; discovery and baseline rules in `DISTRIBUTION.md` §4.

## 1. Conventions

- Invocation: `gatecost <command> [options] [arguments]` (through the launcher or the `gatecost` console command; the two are identical).
- Built with `argparse`; option names are long-form, lower case, hyphenated. Options may appear anywhere after the command name.
- A usage error prints one line in the active output format (`ERROR BAD_ARGUMENT <message>` in `agent` format) and exits with code 2. It never prints a stack trace.
- `gatecost --version` prints `gatecost <VERSION>`; `gatecost help [command]` prints a short, stable help text (at most 25 lines per command) that lists the options of the command with one line each.
- **Precedence** of settings: command line > `gatecost.toml` > environment variables > built-in defaults.
- Environment variables (all optional): `GATECOST_PROFILE`, `GATECOST_CACHE_DIR`, `GATECOST_NO_CACHE=1`, `GATECOST_FORMAT`.
- `NO_COLOR` is honored (the `text` format uses color only on a terminal; `agent`, `json`, and `markdown` never do).

## 2. Commands

| Command | Purpose |
|---|---|
| `diff` | Cost delta of a modification, with attribution. |
| `cost` | Absolute cost of a design. |
| `estimate` | Cost of a hypothetical construct, or comparison of alternatives, without any source file. |
| `snapshot` | Record the current state as a baseline (for use without git). |
| `init` | Write a `gatecost.toml` from what the tool detects. |
| `profiles` | List built-in profiles, or show one. |
| `clean` | Remove caches and snapshots. |
| `validate-output` | Validate a JSON result against the shipped schema. |

### 2.1 Options common to `diff`, `cost`, `snapshot`

| Option | Meaning |
|---|---|
| `PATH ...` | Restrict the **source set** to these files or directories (default: discovery per `DISTRIBUTION.md` §4). For `diff`, the whole source set is still used to resolve the hierarchy when the paths are a subset; only changes inside the paths are reported. |
| `--top MODULE` | Top module or entity. |
| `--define NAME[=VALUE]` | Preprocessor define (Verilog/SystemVerilog) or constant override for conditional elaboration; repeatable. |
| `--incdir DIR` | Include directory; repeatable. |
| `--filelist FILE` | Read sources, include directories and defines from a `.f` filelist; repeatable. |
| `--profile NAME\|PATH` | Cost profile (default `generic-asic`). |
| `--config FILE` | Use this project file instead of searching for `gatecost.toml`. |
| `--root DIR` | Project root (default: detected, see `DISTRIBUTION.md` §4). |
| `--format agent\|json\|text\|markdown` | Output format (default `agent`; `GATECOST_FORMAT` overrides). |
| `--max-lines N` | Maximum contributor or module lines in `agent`, `text`, `markdown` (default 10). |
| `--verbose` / `--quiet` | More detail, or only the headline (`OUTPUT.md` §2.5). |
| `--no-cache` | Neither read nor write any cache. |
| `--no-meta` | Omit the `meta` object in `json`. |
| `--budget GE` | Exit with code 1 if the resulting total (after the change, for `diff`) exceeds `GE`. |

### 2.2 `diff`

```
gatecost diff [options] [PATH ...]
```

Baseline selection (`DISTRIBUTION.md` §4.1), mutually exclusive:

| Option | Meaning |
|---|---|
| *(none)* | Working tree against `HEAD` in a git repository; otherwise the most recent snapshot. |
| `--base-ref REF` | Compare against the content of the sources at git reference `REF`; `merge-base` means the merge base with the repository's default branch. |
| `--since-snapshot [ID]` | Compare against a snapshot (the latest if `ID` is omitted). |
| `--before DIR --after DIR` | Compare two source trees. Both are required together. |

Additional options:

| Option | Meaning |
|---|---|
| `--threshold GE` | Contributors below this absolute `typ` delta are folded into `OTHERS` (default 5). |
| `--fail-on-increase GE` | Exit with code 1 if the `typ` delta exceeds `GE`. |
| `--basis typ\|max` | Which scenario `--budget` and `--fail-on-increase` test (default `typ`). |
| `--limit N` | JSON only: maximum number of contributors (default: all). |
| `--detail ops` | JSON only: include per-operation nodes for changed modules. |

### 2.3 `cost`

```
gatecost cost [options] [PATH ...]
```

Adds `--detail ops` (JSON: all operations) and `--by module|category|file` (the breakdown shown after the headline; default `module`).

### 2.4 `snapshot`

```
gatecost snapshot [options] [PATH ...]      record a snapshot; print its id
gatecost snapshot --list                    list snapshots (id, file count, module count)
gatecost snapshot --delete ID|--all         delete snapshots
```

A snapshot stores the sources (not the cache), the defines, and the profile name in `<project>/.gatecost/snapshots/`. At most 10 snapshots are kept (oldest removed first); `--keep N` changes the limit.

### 2.5 `init`

```
gatecost init [--force]
```

Writes `gatecost.toml` at the project root with the detected top module, source globs, include directories, and a commented list of every available key. The command is the only one that creates a file in the project other than under `.gatecost/`. It refuses to overwrite an existing file without `--force`.

### 2.6 `profiles`

```
gatecost profiles                list profiles (name, description)
gatecost profiles show NAME      print the resolved profile (after `extends`) as TOML
```

### 2.7 `clean`

```
gatecost clean [--global] [--snapshots]
```

Removes `<project>/.gatecost/cache` (and `.gatecost/snapshots` with `--snapshots`); with `--global`, also the user cache directory.

### 2.8 `validate-output`

```
gatecost validate-output FILE|-
```

Validates a JSON result against the shipped schema; prints `OK` or the list of violations; exit code 0 or 3.

## 3. `estimate`

`estimate` returns the cost of a construct described either in a compact notation or as an RTL fragment. It needs no project and no source file; it can be run from any directory.

```
gatecost estimate "<expression>" [options]
gatecost estimate --rtl "<fragment>" [--widths a=16,b=8] [--lang verilog|vhdl]
gatecost estimate --rtl-file FILE [--widths ...]
gatecost estimate --compare "<expr A>" "<expr B>" [...]
```

Options: `--profile`, `--format`, `--count N` (multiply the result by `N` instances), `--widths`, `--lang`.

### 3.1 Compact notation

An expression is a sum of terms: `term (+ term)*`, optionally prefixed by a multiplier `N *`. Each term is `<op> <size> [modifiers]`. Keywords are case-insensitive; separators are single spaces.

| Operation | Form | Examples |
|---|---|---|
| Add, subtract, negate | `add N`, `sub N`, `neg N` | `add 32`, `sub 16`, `add 32 const=5` |
| Multiply | `mul NxM` | `mul 16x16`, `mul 32x32 signed`, `mul 24x8`, `mul 16 const=10` |
| Divide, modulo | `div N/M`, `mod N/M` | `div 32/16`, `mod 32/8` |
| Shift, rotate | `shl N by S`, `shr`, `sar`, `rol`, `ror`; `by const` for a constant amount | `shl 32 by 5`, `sar 64 by 6`, `shl 32 by const` |
| Bitwise | `and N`, `or N`, `xor N`, `not N` | `xor 64` |
| Compare | `eq N`, `ne N`, `lt N`, `le N`, `gt N`, `ge N`; `signed` | `eq 32`, `lt 32 signed`, `eq 32 const` |
| Reduce | `red_and N`, `red_or N`, `red_xor N` | `red_xor 32` |
| Multiplexers | `mux K:1 wW`, `pmux K wW`, `priority K wW` | `mux 8:1 w64`, `pmux 5 w16` |
| Decode | `decode S` (S select bits) | `decode 5` |
| Registers | `reg N [async_reset] [sync_reset] [enable] [scan]` | `reg 128 sync_reset enable`, `reg 32 async_reset` |
| Latch | `latch N` | `latch 8` |
| Memory | `mem DxW [rports=R] [wports=P] [sync_read\|async_read]` | `mem 256x32 rports=2 wports=1 sync_read` |
| ROM | `rom DxW` | `rom 64x8` |

Examples of sums and multipliers:

```
gatecost estimate "mul 16x16 + add 32 + reg 32"
gatecost estimate "4 * mul 16x16"
gatecost estimate "mux 8:1 w64 + reg 64 enable"
```

Constants: `const` marks an operand as a non-zero constant of unknown value; `const=K` gives the value (used for CSD analysis of multipliers and for incrementers). A constant that is a power of two reduces the operation to wiring.

Notation details (implemented in stage 2): a multiplier binds to the next term, so `3 * add 8 + 2 * reg 8` is `3·add + 2·reg`; parentheses group (`2 * (add 8 + reg 8)`); `--count N` multiplies the whole expression. Extra forms: `sum KxW` (k-operand fused addition), `mac NxM [c=W] [signed]` (`a*b + c`), `pow N exp=E [out=O]`, `encode K [priority]`; extra modifiers: `mul ... square`, `mul ... trunc=T` (only the low `T` product bits demanded), `shl N by S lhs_one` (`1 << x`), `reg ... async_set_reset`; `div|mod N const=K` for a constant divisor. A `mem` without `sync_read`/`async_read` is asynchronous (flop-based) and prints an `ASSUMED` line. `--rtl` / `--rtl-file` are implemented on top of the HDL front-end and elaborator; `--count N` combined with `--rtl` is not available yet and reports `NOT_IMPLEMENTED`.

### 3.2 RTL fragment

`--rtl` accepts one or more statements of Verilog/SystemVerilog (default) or VHDL (`--lang vhdl`). The tool wraps the fragment in an implicit module, then costs it with the normal front-end:

- identifiers used but not declared are **inputs**, with their width taken from `--widths`; an undeclared width defaults to 32 and an `ASSUMED` line is printed;
- identifiers assigned and not declared are **outputs**, with the width of the assigned expression;
- a statement inside `always @(posedge clk)` (or a VHDL clocked `process`) makes its targets registers; `clk` and an optional `rst` are implicit inputs;
- declarations are allowed in the fragment and take precedence.

```
gatecost estimate --rtl "assign y = a*b + c;" --widths a=16,b=16,c=32
gatecost estimate --rtl "always @(posedge clk) q <= en ? d : q;" --widths d=32
gatecost estimate --rtl "y <= a + b;" --lang vhdl --widths a=16,b=16
```

The reported figure is the cost of the fragment as elaborated, including the effects of `COST_MODEL.md` §4 (for example, demanded bits): `assign y = a*b;` with a 16-bit `y` and 16-bit inputs costs a truncated multiplier.

### 3.3 Comparing alternatives

`--compare` takes two or more expressions or fragments (use `--rtl` once per alternative) and prints each estimate, then the difference of the others relative to the first:

```
gatecost estimate --compare "mul 32x32" "mul 32 const=10"
gatecost estimate --compare "mux 8:1 w32" "pmux 8 w32"
```

The result states which alternative has the lowest `typ` cost and whether the ranges overlap (in which case the preference is flagged as `uncertain`).

## 4. Configuration File: `gatecost.toml`

The file is optional and located at the project root (or given with `--config`). Unknown keys are ignored with an `info` diagnostic; invalid values produce `BAD_CONFIG`. Every key is optional.

```toml
[project]
top = "top_core"                          # module or entity; auto-detected if absent
sources = ["rtl/**/*.sv", "rtl/**/*.vhd"] # globs relative to the project root
filelists = ["rtl/files.f"]
incdirs = ["rtl/include"]
exclude = ["rtl/legacy/**", "build", "sim"]   # added to the built-in exclusions
defines = { SYNTHESIS = "1", WIDTH = "32" }
languages = ["verilog", "vhdl"]           # restrict front-ends (default: both)

[project.libraries]                       # VHDL library mapping (default: all in "work")
core = ["rtl/core/*.vhd"]

[project.architectures]                   # VHDL: architecture per entity when several exist
cpu = "rtl"

[cost]
profile = "generic-asic"                  # built-in name or path to a TOML profile
# Per-key profile overrides can be placed in a [cost.override] table (same keys as a profile file).

[cost.override.policy]
enable_style = "mux"
fsm_encoding = "binary"

[budget]
max_ge = 50000                            # equivalent to --budget
max_delta_ge = 500                        # equivalent to --fail-on-increase
basis = "typ"

[report]
max_lines = 10
threshold_ge = 5
format = "agent"

[limits]                                  # front-end limits (FRONTEND.md §7)
generate_iterations = 4096
loop_unroll = 1024
inline_depth = 32
max_instances = 100000

[cache]
enabled = true
dir = ""                                  # empty: default locations (DISTRIBUTION.md §5)
```

Rules:

- `sources` globs support `**`. Paths use forward slashes on all platforms.
- A relative path in the file is relative to the file's directory.
- `[cost.override]` accepts the same tables as a profile file (`COST_MODEL.md` §6) and is applied on top of the selected profile.
- The file contains no executable content; the tool never evaluates expressions from it.

## 5. Exit Codes

| Code | Meaning |
|---|---|
| 0 | Success (including partial results). |
| 1 | Budget exceeded (`--budget`, `--fail-on-increase`); the result is printed normally. |
| 2 | Usage or configuration error (`BAD_ARGUMENT`, `BAD_CONFIG`, `NO_BASELINE`). |
| 3 | Analysis could not produce a result (`NO_SOURCES`, `TOP_NOT_FOUND`, `PYTHON_TOO_OLD`, `INTERNAL`). |

## 6. Tests

- **Parser:** every example of this document is accepted by the argument parser, and every documented option is accepted by exactly the commands that list it (a table-driven test generated from §2).
- **Errors:** each malformed invocation (unknown command, unknown option, missing value, mutually exclusive options such as `--base-ref` with `--since-snapshot`, `--before` without `--after`) yields one `ERROR BAD_ARGUMENT` line, exit code 2, and no stack trace.
- **Estimate notation:** a grammar test table for §3.1 covers every operation, modifier, multiplier, and sum; invalid forms (`mul 16`, `mux 8`, `add x`) give `BAD_ARGUMENT` with a message naming the expected form.
- **Estimate fragments:** inputs, outputs, and registers are inferred as documented; undeclared widths trigger `ASSUMED`; the Verilog and VHDL forms of the same fragment give the same cost.
- **Comparison:** `--compare` orders alternatives correctly, flags overlapping ranges as uncertain, and reports differences consistent with the individual estimates.
- **Configuration:** a fixture per section of §4; precedence between command line, file, environment, and defaults; relative paths; unknown keys; strict rejection of malformed TOML with `BAD_CONFIG`.
- **`init`:** produces a file that parses and yields the same analysis as no file; refuses to overwrite without `--force`.
- **Snapshot:** creation, listing, deletion, rotation at the limit, and use as a baseline.
- **Documentation sync:** the options in `SKILL.md` and `DISTRIBUTION.md` are all accepted by the parser.
