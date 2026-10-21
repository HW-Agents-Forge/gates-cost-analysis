"""Number and string formatting shared by the human and agent reporters."""
from __future__ import annotations

from ..result import Diagnostic, Ge3


def ge(milli: int) -> int:
    """Round milli-GE to the nearest GE, half away from zero."""
    q = (abs(milli) + 500) // 1000
    return -q if milli < 0 else q


def sge(milli: int) -> str:
    """Signed GE: explicit sign, zero is '0'."""
    v = ge(milli)
    return f"+{v}" if v > 0 else str(v)


def rng(g: Ge3, signed: bool = True) -> str:
    f = sge if signed else (lambda m: str(ge(m)))
    return f"({f(g.min)}..{f(g.max)})"


def pct(p: float | None) -> str:
    if p is None:
        return "n/a"
    s = f"{abs(p):.1f}"
    if s == "0.0":
        return "0.0%"
    return f"{'-' if p < 0 else '+'}{s}%"


def loc(file: str | None, line: int | None) -> str:
    if file is None:
        return "-"
    return f"{file}:{line}" if line is not None else file


def warn_lines(diags: tuple[Diagnostic, ...], limit: int = 5) -> list[str]:
    """WARN lines: only diagnostics that affect cost (OUTPUT.md section 2.2)."""
    sel = [d for d in diags if d.affects_cost]
    lines = [f"WARN {d.code} {loc(d.file, d.line)} {d.message}" for d in sel[:limit]]
    if len(sel) > limit:
        lines.append(f"WARN TRUNCATED - +{len(sel) - limit} more (use --format json)")
    return lines
