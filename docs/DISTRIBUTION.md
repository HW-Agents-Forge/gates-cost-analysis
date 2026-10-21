# gatecost — Distribution and Agent Integration

Status: Draft. Normative companion to `SPEC.md` §7, §11, §12 and §15.

`gatecost` is distributed **as an agent skill**: the repository itself is the skill, and a project adopts it by adding the repository as a git submodule inside whichever skills directory its agent toolchain reads. No installation step, package manager, virtual environment, or network access is needed on the machine that runs the agent.

## 1. Adoption Model

```bash
git submodule add <repository-url> .claude/skills/gatecost      # Claude Code
git submodule add <repository-url> .github/skills/gatecost      # GitHub Copilot
git submodule add <repository-url> .opencode/skills/gatecost    # OpenCode
```

The tool must behave identically wherever it is mounted. Consequences:

1. **The repository root is the skill directory.** `SKILL.md` sits at the repository root, with the frontmatter that the supported agents agree on (`name` and `description` only; no tool-specific keys).
2. **The mount path is unknown to the tool.** The launcher locates the tool relative to its own file; nothing in the code or the documentation hard-codes `.claude/`, `.github/`, or `.opencode/`.
3. **The skill directory is read-only.** The tool never writes inside its own directory, so the submodule stays clean and `git status` in the host project is unaffected (§5).
4. **The analyzed project is the host project**, never the skill. Project and source discovery start from the current working directory (§4) and exclude the skill directory.
5. **Updating is a submodule update.** The skill version is the submodule commit (tagged releases for stability); the tool reports it with `gatecost --version`, read from a `VERSION` file in the repository, with no call to `git`.

## 2. Repository Layout

```
SKILL.md                    # skill entry point (frontmatter + agent instructions)
scripts/gatecost            # launcher (executable, Python shebang, also run as `python3 scripts/gatecost`)
src/gatecost/               # package: frontend/, elab/, ir/, cost/, diff/, report/, cli/, profiles/
docs/                       # specification and references (developer-facing)
tests/                      # unit, golden, property, and fuzz tests, with RTL fixtures
schema/                     # JSON Schema for the json output
VERSION
LICENSE
README.md                   # adoption instructions and overview
pyproject.toml              # optional: allows `pip install .` for non-agent use
```

`SKILL.md` stays short (§6) so that loading the skill costs few tokens. Long-form documentation lives in `docs/` and is read only by developers and, on demand, by agents that need details.

## 3. Launcher

`scripts/gatecost` is the only entry point an agent needs. It:

1. Checks the Python version first. The launcher file itself uses only syntax valid on much older interpreters, so that the check can run and the message can be printed; if the version is too old, it prints `ERROR PYTHON_TOO_OLD gatecost needs Python >= 3.11 (found X.Y)` and exits with code 3.
2. Inserts `<repo>/src` at the front of `sys.path`, where `<repo>` is computed from the launcher's own resolved path (so symlinks and any mount point work).
3. Sets `sys.pycache_prefix` to a directory in the user cache (§5), so compiled bytecode is cached across invocations without creating `__pycache__` directories inside the submodule.
4. Imports `gatecost.cli` lazily and calls `main()`.

Invocation from an agent, with the path of the directory that contains `SKILL.md`:

```bash
python3 <skill-dir>/scripts/gatecost diff
```

Requirements on the code base that the launcher relies on:

- **Python 3.11 or later** and the **standard library only**. TOML configuration and profiles are read with the standard `tomllib`, so no TOML parser is shipped. The code base may use the language and library features of 3.11 (`match`, `X | Y` unions, `dataclass(slots=True)`, `zip(strict=True)`, `ExceptionGroup`), and must not use features newer than 3.11, so that behavior is identical across all supported interpreters. A machine with an older interpreter gets the `PYTHON_TOO_OLD` error above, with exit code 3.
- **Start-up time.** A cold start (no bytecode cache) prints a result for a small design in under 300 ms, and a warm start in under 100 ms. Modules are imported lazily by command, and profiles are parsed on demand.
- No dependency on the current working directory being writable.

## 4. Project and Source Discovery

The analyzed project is the host project, determined as follows:

1. **Project root:** the nearest ancestor of the current working directory that contains `gatecost.toml`; otherwise the nearest ancestor containing `.git` that is **not inside the skill directory**; otherwise the current directory. Using the working directory (not the launcher location) as the starting point keeps the result correct when the skill is a submodule, which has its own `.git` entry.
2. **Excluded from discovery**, always: the skill directory itself; `.git`; `.gatecost`; skills directories (`.claude`, `.github/skills`, `.opencode`); `node_modules`; build, simulation, and vendor directories listed in `exclude` in `gatecost.toml` (default: `build`, `sim`, `work`, `obj_dir`, `xcelium.d`, `.venv`). RTL fixtures that ship with the tool can therefore never be mistaken for project sources.
3. **Source set:** `gatecost.toml` if present; otherwise every supported RTL file under the project root after exclusions, plus filelists (`*.f`).
4. **Changed set (for `diff`):** the files that differ from the baseline (§4.1). They determine which modules are re-analyzed (`FRONTEND.md` §5); the rest of the source set provides the hierarchy and the cost of unchanged modules.
5. **Top module:** auto-detected as the module (or entity) not instantiated by any other. If several qualify, the tool selects the one whose hierarchy contains the changed modules; if that is still ambiguous it reports each root (`FRONTEND.md` §4). The resolution is recorded in `gatecost.toml` only on explicit request (`gatecost init`).

### 4.1 Baseline for agent workflows

An agent normally edits files in a git working tree and asks for the cost of that edit. Baseline resolution for `diff`, in order of precedence:

1. `--base-ref <ref>`: the content of the changed files at `<ref>`.
2. `--since-snapshot [id]`: the state recorded by `gatecost snapshot`.
3. `--before <dir> --after <dir>`: two explicit source trees.
4. **Default: `HEAD`** when the project is a git repository. Uncommitted edits (staged or not) are compared with the last commit. For work already committed, the agent passes `--base-ref <branch-point>`; `--base-ref merge-base` is accepted as shorthand for the merge base with the repository's default branch.
5. Otherwise, the most recent snapshot; otherwise `NO_BASELINE`.

Reading the baseline from git uses the `git` executable (`git show`, `git ls-tree`, `git diff --name-only`) when it is present; it is the only external program the tool may invoke and it is never required (snapshots and directory comparison work without it). Untracked new RTL files are treated as added.

## 5. Files the Tool Writes

The tool writes only to locations outside the skill directory, and only when needed:

| Location | Content | Cleanup |
|---|---|---|
| `<project>/.gatecost/` | Snapshots, and the analysis cache when the project is writable. On creation the tool also writes `.gatecost/.gitignore` containing `*`, so the directory never appears in `git status` and no change to the host project's `.gitignore` is required. | `gatecost clean` removes it. |
| User cache directory (`$XDG_CACHE_HOME/gatecost`, `~/Library/Caches/gatecost`, or `%LOCALAPPDATA%\gatecost`) | Bytecode cache (`sys.pycache_prefix`) and, if the project is not writable, the analysis cache. | `gatecost clean --global`. |

`--no-cache` disables all cache reads and writes; results are identical with or without it. If no cache location is writable, the tool still runs, with a performance penalty and a single `info` diagnostic on standard error.

## 6. `SKILL.md` Contract

`SKILL.md` is the single document an agent loads. Requirements:

- **Frontmatter** with exactly `name: gatecost` and a `description`. The description is written to trigger on the user's intents, in the user's vocabulary: area/gate cost, gate count, gate equivalents, GE, NAND2 equivalents, "how big is this change", RTL/HDL/Verilog/SystemVerilog/VHDL modification, or comparing design alternatives. It states what the skill does *not* do (no synthesis, timing, or power), to avoid false triggers. It stays under 1,024 characters. `name` equals the recommended mount directory name.
- **Body under about 120 lines**, containing, in order: when to use; the invocation command with the `<skill-dir>` convention; which command fits which situation; how to read the output; the response guidelines of `OUTPUT.md` §5; what to do on common errors; limits of the estimate.
- **No hard-coded mount path.** The body instructs the agent to run the launcher located next to `SKILL.md`, using that file's directory.
- **Self-contained for the common case.** An agent should never need to open `docs/` to answer the user's question.
- **Pointers.** For rare needs (profiles, configuration), `SKILL.md` names the relevant `docs/` file so the agent reads it only on demand.

The repository ships a trigger test set (`tests/skill/prompts.md`) of prompts that should and should not activate the skill, used when the description is revised.

## 7. Agent Workflows

1. **Check an edit just made** (most frequent). After editing RTL in a git working tree:
   ```bash
   python3 <skill-dir>/scripts/gatecost diff
   ```
2. **Check committed work.** For a branch or recent commits:
   ```bash
   python3 <skill-dir>/scripts/gatecost diff --base-ref merge-base
   ```
3. **Decide before coding.** To compare alternatives:
   ```bash
   python3 <skill-dir>/scripts/gatecost estimate "mul 32x32"
   python3 <skill-dir>/scripts/gatecost estimate --rtl "assign y = a*b + c;" --widths a=16,b=16,c=32
   ```
4. **Outside git.** Snapshot before editing, diff afterwards:
   ```bash
   python3 <skill-dir>/scripts/gatecost snapshot
   python3 <skill-dir>/scripts/gatecost diff --since-snapshot
   ```
5. **Absolute size.** `gatecost cost` gives the total and the largest modules.

Default output is the `agent` format; when invoked by an agent the `--format` option is therefore normally omitted.

## 8. Permissions and Safety

- The tool only reads files and writes to the locations in §5. It does not use the network, execute analyzed sources, or follow includes outside the project and configured include directories (`FRONTEND.md` §1).
- A single permission rule covers every command (for example, allowing shell commands that start with `python3 <skill-dir>/scripts/gatecost`). The documentation for adopters provides this rule for each of the three supported agent toolchains.
- Because the skill executes code from a repository mounted in the host project, adopters pin the submodule to a reviewed commit or tag; the tool's `VERSION` is printed by `--version` and included in `meta` of the JSON output.

## 9. Documentation for Adopters

`README.md` contains: the one-line adoption commands (§1), the permission rule, a minimal `gatecost.toml` example, update instructions (`git submodule update --remote`), and a pointer to `SKILL.md` for what the agent will do.

## 10. Tests

- **Mount independence:** the launcher is run from copies mounted at `.claude/skills/gatecost`, `.github/skills/gatecost`, `.opencode/skills/gatecost`, and a path containing spaces, with the working directory set to a different project; results are identical.
- **Clean submodule:** after a full workflow, `git status` in the skill directory is empty (no bytecode, cache, or snapshot files), and `git status` in the host project shows no change.
- **Discovery:** RTL fixtures inside the skill directory are never analyzed; the host project root is chosen correctly when the skill is a submodule (with its own `.git` entry).
- **Baselines:** each rule of §4.1, including untracked files, staged and unstaged edits, and a repository without commits; behavior when `git` is absent (simulated by an empty `PATH`).
- **Python floor:** the test suite runs on 3.11 (the oldest supported interpreter) and on the latest release, with identical results; a lint test rejects syntax and standard-library calls newer than 3.11; the launcher is syntax-checked against an old grammar so that `PYTHON_TOO_OLD` is reachable.
- **Start-up time:** the cold and warm start budgets of §3 are enforced by a benchmark test.
- **Read-only install:** the tool works when the skill directory and the project are read-only (it falls back to the user cache, then to no cache).
- **Skill file:** `SKILL.md` frontmatter is valid, `description` is under the limit, the body is under its size budget, and every command shown in it is accepted by the CLI parser (so the documentation cannot drift from the tool).
