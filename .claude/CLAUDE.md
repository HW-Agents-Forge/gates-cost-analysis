# gatecost: development with Claude Code

Conventions and definition of "done": `CONTRIBUTING.md`. Specifications: `docs/`. Development skills: `.claude/skills/` (the root `SKILL.md` is the product skill for users of the tool).

## Delegate to subagents (`.claude/agents/`)

**Mandatory rule**: the main agent is a project manager. As soon as a request touches a subsystem in the table below (reading, analysis, review, fix, test, docs, git), it launches the matching agent with the Agent tool instead of reading or modifying that code itself. It handles directly only: questions that need no code reading, a single-command lookup, and the final synthesis. Requests for analysis or proposals without modification also go through the owning agent (e.g. "is the cost multiplication formula correct?" → `cost-model-dev`).

After a delegation: do not redo the same work in parallel; wait for the agent's result, then re-read its key references. Run tasks in parallel only if they are independent (distinct subsystems or files). If the request spans several subsystems, split it and delegate each part.

Touched path → agent: `frontend/` → `frontend-dev` · `elab/` → `elaborator-dev` · `ir/` → `ir-analysis-dev` · `cost/`, `profiles/`, `estimate*.py` → `cost-model-dev` · `diff/`, `report/`, `cli/`, `schema/` → `diff-report-dev` · `tests/` → `test-engineer` · `docs/`, `SKILL.md`, `README.md` → `docs-keeper`.


| Need | Agent |
|---|---|
| Map code/tests/docs before modifying unfamiliar code | `repo-scout` (haiku) |
| Lexer, preprocessor, Verilog/SV/VHDL parsers | `frontend-dev` (opus) |
| Widths, parameters, hierarchy, lowering to the IR | `elaborator-dev` (opus) |
| IR builder, passes, canonical keys | `ir-analysis-dev` (opus) |
| Cost formulas, profiles, `estimate` | `cost-model-dev` (opus) |
| Differ, reports, JSON schema, CLI | `diff-report-dev` (sonnet) |
| Slowness, complexity, startup | `perf-optimizer` (opus), measurements by `bench-runner` (haiku) |
| Red test, fuzz, bug hunting on real RTL | `test-engineer` (sonnet) |
| Docs / `SKILL.md` / examples to resync | `docs-keeper` (sonnet) |
| Run tests, mypy, smoke (only the verdict matters) | `test-runner` (haiku) |
| Branch, atomic commits, push | `git-committer` (haiku) |

Typical flow: `repo-scout` → red test (`test-engineer`) → fix (subsystem agent) → `test-runner` → `docs-keeper` if a behavior changes → `git-committer`. Layer order: front-end → elaboration → IR → cost → diff/report → docs.

Briefs: a subagent does not see the conversation. Give the goal, the relevant files and docs, the constraints (Python 3.11 stdlib, deterministic output, diagnostics rather than exceptions, one regression test per bug, no writes to the skill directory) and what it must return.

Do not create a PR or push to the default branch without an explicit request. Before concluding: `done-checklist` skill.
