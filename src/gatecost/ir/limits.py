"""IR limits (IR.md section 10)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Limits:
    max_nodes: int = 2_000_000
    max_nets: int = 2_000_000
    max_net_width: int = 65_536
    max_mem_depth: int = 1_048_576


DEFAULT_LIMITS = Limits()
