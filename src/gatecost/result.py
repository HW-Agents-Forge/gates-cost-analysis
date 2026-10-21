"""Result object model: one object per run, rendered into every output format.

All costs are integer milli-GE (COST_MODEL.md section 1); rounding happens only in the renderers.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class Ge3:
    """A cost (or delta) under the three scenarios, in milli-GE."""

    min: int
    typ: int
    max: int


@dataclass(slots=True, frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: str = "warning"
    affects_cost: bool = False
    side: str = "after"
    file: str | None = None
    line: int | None = None
    column: int | None = None


@dataclass(slots=True, frozen=True)
class Confidence:
    level: str  # high | medium | low
    reasons: tuple[str, ...] = ()
    range_ratio: float | None = None


@dataclass(slots=True, frozen=True)
class Contributor:
    file: str
    line: int
    reason: str
    description: str
    delta: Ge3
    op: str = ""
    widths: tuple[int, ...] = ()
    category: str = ""
    module: str = ""
    instances: int = 1
    per_instance: Ge3 | None = None
    location_side: str = "after"
    cause: dict | None = None  # {"kind","name","file","line","from","to"}
    children: tuple["Contributor", ...] = ()


@dataclass(slots=True, frozen=True)
class DiffResult:
    verdict: str
    confidence: Confidence
    top: str | None = None
    before: Ge3 | None = None
    after: Ge3 | None = None
    delta: Ge3 | None = None
    percent: float | None = None
    contributors: tuple[Contributor, ...] = ()
    others_count: int = 0
    others_net: Ge3 = Ge3(0, 0, 0)
    by_module: tuple[dict, ...] = ()
    by_category: dict = field(default_factory=dict)  # typ deltas, milli-GE
    structural: tuple[dict, ...] = ()
    diagnostics_new: tuple[Diagnostic, ...] = ()
    diagnostics_resolved: tuple[Diagnostic, ...] = ()
    assumed: tuple[str, ...] = ()
    # Extras for --verbose
    modules_verbose: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ModuleCost:
    module: str
    instances: int
    own: Ge3
    total: Ge3
    parameters: dict = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class CostResult:
    top: str
    total: Ge3
    confidence: Confidence
    modules: tuple[ModuleCost, ...] = ()
    by_category: dict = field(default_factory=dict)  # typ, milli-GE
    assumed: tuple[str, ...] = ()
    operations: tuple[dict, ...] | None = None


@dataclass(slots=True, frozen=True)
class EstimateResult:
    kind: str  # op | rtl
    text: str
    normalized: str
    ge: Ge3
    architecture: dict
    breakdown: dict
    confidence: Confidence
    assumed: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class CompareResult:
    alternatives: tuple[EstimateResult, ...]
    basis: str
    best_index: int
    saves: Ge3  # best versus the runner-up, typ used for display
    overlap: bool
    other_index: int = 0
    assumed: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SnapshotResult:
    id: str
    files: int
    modules: int


@dataclass(slots=True, frozen=True)
class MessageResult:
    """Plain informational result (init, clean, validate-output, help)."""

    lines: tuple[str, ...]
    data: dict = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Output:
    """The envelope of OUTPUT.md section 3.1."""

    command: str
    status: str = "ok"  # ok | partial | error
    profile: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    result: object | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    error: tuple[str, str] | None = None  # (code, message)
    meta: dict | None = None
