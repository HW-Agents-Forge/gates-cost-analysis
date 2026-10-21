"""Error codes and exit codes (OUTPUT.md section 6, SPEC.md section 8.3)."""
from __future__ import annotations

EXIT_OK = 0
EXIT_BUDGET = 1
EXIT_USAGE = 2
EXIT_FAILED = 3

EXIT_CODES: dict[str, int] = {
    "BAD_ARGUMENT": EXIT_USAGE,
    "BAD_CONFIG": EXIT_USAGE,
    "NO_BASELINE": EXIT_USAGE,
    "NO_SOURCES": EXIT_FAILED,
    "TOP_NOT_FOUND": EXIT_FAILED,
    "PYTHON_TOO_OLD": EXIT_FAILED,
    "NOT_IMPLEMENTED": EXIT_FAILED,
    "INTERNAL": EXIT_FAILED,
    "BUDGET_EXCEEDED": EXIT_BUDGET,
}


class GatecostError(Exception):
    """A reportable failure; the CLI renders it as a single ERROR line."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, EXIT_FAILED)
