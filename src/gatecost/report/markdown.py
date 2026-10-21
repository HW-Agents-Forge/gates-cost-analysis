"""The `markdown` format for pull-request comments (OUTPUT.md section 4)."""
from __future__ import annotations

from ..result import DiffResult, Output
from .agent import render_agent
from .fmt import ge, loc, pct, sge

MAX_CHARS = 60_000
FOOTNOTE = "_Figures are estimates for ranking options and judging orders of magnitude, not synthesis results._"


def _esc(s: str) -> str:
    return s.replace("|", "\\|")


def render_markdown(out: Output, *, max_lines: int = 10) -> str:
    r = out.result
    if out.status == "error" or not isinstance(r, DiffResult) or r.delta is None:
        body = render_agent(out, max_lines=max_lines).rstrip("\n")
        return f"```\n{body}\n```\n\n{FOOTNOTE}\n"
    lines = [
        f"### gatecost: {r.verdict} {sge(r.delta.typ)} GE ({sge(r.delta.min)}..{sge(r.delta.max)}), "
        f"{pct(r.percent)}, confidence {r.confidence.level}",
        "",
        f"Top `{r.top}`: {ge(r.before.typ)} -> {ge(r.after.typ)} GE",
        "",
        "| Δ GE | location | reason | description |",
        "|---:|---|---|---|",
    ]
    for c in r.contributors[:max_lines]:
        lines.append(f"| {sge(c.delta.typ)} | `{loc(c.file, c.line)}` | {c.reason} | {_esc(c.description)} |")
    hidden = len(r.contributors) - max_lines
    if hidden > 0 or r.others_count:
        lines.append(f"| | | | _{max(hidden, 0) + r.others_count} more_ |")
    for reason in r.confidence.reasons:
        lines += ["", f"**Confidence {r.confidence.level}:** {_esc(reason)}"]
    if r.by_module or r.by_category:
        lines += ["", "<details><summary>Details</summary>", ""]
        if r.by_category:
            lines += ["| category | Δ GE |", "|---|---:|"]
            lines += [f"| {k} | {sge(v)} |" for k, v in r.by_category.items()]
        lines += ["", "</details>"]
    lines += ["", FOOTNOTE]
    text = "\n".join(lines) + "\n"
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 40] + "\n\n_(truncated)_\n"
