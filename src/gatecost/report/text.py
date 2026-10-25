"""The human-readable `text` format (OUTPUT.md section 4)."""
from __future__ import annotations

from ..result import CostResult, DiffResult, Output
from .agent import render_agent
from .fmt import ge, loc, sge

WIDTH = 100
LEGEND = (
    "Reasons: added removed width constant parameter structure architecture "
    "sharing fusion register multiplicity blackbox"
)


def _table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> list[str]:
    cols = list(zip(headers, *rows)) if rows else [(h,) for h in headers]
    widths = [max(len(c) for c in col) for col in cols]
    out = []
    for r in [headers, *rows]:
        line = "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()
        out.append(line[:WIDTH])
    return out


def render_text(out: Output, *, max_lines: int = 10, verbose: bool = False, quiet: bool = False) -> str:
    r = out.result
    if out.status == "error" or quiet or not isinstance(r, (DiffResult, CostResult)):
        return render_agent(out, max_lines=max_lines, verbose=verbose, quiet=quiet)
    head = render_agent(out, max_lines=0, quiet=True).rstrip("\n")
    lines = [head, ""]
    if isinstance(r, DiffResult):
        rows = [
            (sge(c.delta.typ), loc(c.file, c.line), c.reason, c.description + (f" x{c.instances}" if c.instances > 1 else ""))
            for c in r.contributors[:max_lines]
        ]
        lines.append("Contributors")
        lines += _table(rows, ("DELTA", "LOCATION", "REASON", "WHAT"))
        hidden = max(len(r.contributors) - max_lines, 0) + r.others_count
        if hidden:
            lines.append(f"... and {hidden} more")
        if verbose and r.by_category:
            lines += ["", "By category"]
            lines += _table([(k, sge(v)) for k, v in r.by_category.items()], ("CATEGORY", "DELTA"))
        lines += ["", LEGEND]
        for c in r.confidence.reasons:
            lines.append(f"Confidence {r.confidence.level}: {c}")
        for a in r.assumed:
            lines.append(f"Assumed: {a}")
    else:
        rows = [(str(ge(m.total.typ)), m.module, f"x{m.instances}") for m in r.modules[:max_lines]]
        lines.append("Modules")
        lines += _table(rows, ("GE", "MODULE", "COUNT"))
        if r.by_category:
            lines += ["", "By category"]
            lines += _table([(k, str(ge(v))) for k, v in r.by_category.items()], ("CATEGORY", "GE"))
    return "\n".join(lines) + "\n"
