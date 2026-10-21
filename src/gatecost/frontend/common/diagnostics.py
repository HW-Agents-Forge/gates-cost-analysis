"""Diagnostic collection shared by the front-ends (FRONTEND.md section 6)."""
from __future__ import annotations

from gatecost.result import Diagnostic

# code -> (default severity, affects_cost)
AFFECTS_COST: dict[str, tuple[str, bool]] = {
    "PARSE_ERROR": ("error", True),
    "UNSUPPORTED_CONSTRUCT": ("warning", True),
    "UNDEFINED_MACRO": ("warning", True),
    "MACRO_ERROR": ("warning", True),
    "PREPROC_ERROR": ("warning", True),
    "LIMIT_REACHED": ("warning", True),
    "INCLUDE_NOT_FOUND": ("warning", True),
    "INCLUDE_CYCLE": ("warning", True),
    "DUPLICATE_MODULE": ("warning", True),
    "SOURCE_NOT_FOUND": ("error", True),
    "IGNORED_REGION": ("info", False),
    "ENCODING_FALLBACK": ("info", False),
    "DEFAULT_NETTYPE_NONE": ("info", False),
}


class DiagSink:
    """Append-only diagnostic list; dedupes identical (code, file, line, column, message)."""

    __slots__ = ("items", "_seen")

    def __init__(self) -> None:
        self.items: list[Diagnostic] = []
        self._seen: set = set()

    def add(self, code: str, message: str, file: str | None = None, line: int | None = None,
            column: int | None = None, severity: str | None = None, affects_cost: bool | None = None) -> None:
        key = (code, file, line, column, message)
        if key in self._seen:
            return
        self._seen.add(key)
        dsev, daff = AFFECTS_COST.get(code, ("warning", False))
        self.items.append(Diagnostic(
            code=code, message=message,
            severity=severity or dsev,
            affects_cost=daff if affects_cost is None else affects_cost,
            file=file, line=line, column=column,
        ))

    def extend(self, diags) -> None:
        for d in diags:
            key = (d.code, d.file, d.line, d.column, d.message)
            if key not in self._seen:
                self._seen.add(key)
                self.items.append(d)
