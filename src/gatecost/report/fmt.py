"""Number and string formatting shared by the human and agent reporters."""
from __future__ import annotations

import re

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


_KIND_CUT = re.compile(r"(?::| ignored| excluded| \(| is | contains| without| with delay|').*$")
_KIND_TAIL = re.compile(r"(?: (?:statement|block|declaration|region))+$")
MAX_NOTE_KINDS = 4


def ignored_kind(message: str) -> str:
    """Short name of what an ``IGNORED_REGION`` message skipped (``initial``, ``assert``, ``class``...)."""
    k = _KIND_TAIL.sub("", _KIND_CUT.sub("", message.strip().lower()).strip()).strip()
    return k or "other"


def note_lines(diags: tuple[Diagnostic, ...]) -> list[str]:
    """One compact NOTE line summarising the ``IGNORED_REGION`` diagnostics (none: no line)."""
    kinds: dict[str, int] = {}
    seen: set = set()
    n = 0
    for d in diags:
        if d.code != "IGNORED_REGION":
            continue
        key = (d.file, d.line, d.message)
        if key in seen:
            continue
        seen.add(key)
        n += 1
        k = ignored_kind(d.message)
        kinds[k] = kinds.get(k, 0) + 1
    if not n:
        return []
    ordered = sorted(kinds, key=lambda k: (-kinds[k], k))
    shown = ", ".join(ordered[:MAX_NOTE_KINDS])
    if len(ordered) > MAX_NOTE_KINDS:
        shown += f", +{len(ordered) - MAX_NOTE_KINDS} more"
    return [f"NOTE ignored regions: {n} ({shown})"]
