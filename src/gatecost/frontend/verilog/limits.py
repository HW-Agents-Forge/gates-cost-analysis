"""Front-end limits (FRONTEND.md section 7, front-end subset)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Limits:
    macro_depth: int = 64
    include_depth: int = 32
    expr_depth: int = 500
