"""Cost accumulators and the ``OpCost`` result type.

``Acc`` accumulates one scenario (milli-GE total plus primitive counts). ``Sc`` bundles the
three scenarios of an operation. ``finalize`` applies the profile-wide factors (typ
overhead, logic scale, max overhead), enforces ``min <= typ <= max`` and produces the
immutable ``OpCost``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..result import Ge3
from .profile import Profile
from .units import pm, rdiv


class Acc:
    """One scenario: milli-GE total and primitive/coefficient counts."""

    __slots__ = ("total", "counts")

    def __init__(self, total: int = 0, counts: dict[str, int] | None = None) -> None:
        self.total = total
        self.counts: dict[str, int] = counts if counts is not None else {}

    def add(self, name: str, count: int, unit: int) -> "Acc":
        """Add ``count`` instances of a unit costing ``unit`` milli-GE."""
        if count > 0:
            self.total += count * unit
            self.counts[name] = self.counts.get(name, 0) + count
        return self

    def add_milli(self, name: str, milli: int) -> "Acc":
        """Add a lump (not a count of a unit); recorded as one pseudo-primitive."""
        if milli:
            self.total += milli
            self.counts[name] = self.counts.get(name, 0) + 1
        return self

    def plus(self, other: "Acc") -> "Acc":
        out = Acc(self.total + other.total, dict(self.counts))
        for k, v in other.counts.items():
            out.counts[k] = out.counts.get(k, 0) + v
        return out

    def scaled(self, permille: int) -> "Acc":
        """Scale the cost (counts are unscaled: they describe the architecture)."""
        return Acc(pm(self.total, permille), dict(self.counts))

    def times(self, k: int) -> "Acc":
        return Acc(self.total * k, {n: c * k for n, c in self.counts.items()})

    def copy(self) -> "Acc":
        return Acc(self.total, dict(self.counts))


def lerp(lo: Acc, hi: Acc, alpha_permille: int) -> Acc:
    """``lo + alpha * (hi - lo)``, counts included."""
    names = list(lo.counts) + [k for k in hi.counts if k not in lo.counts]
    counts = {}
    for n in names:
        c = lo.counts.get(n, 0)
        v = c + rdiv((hi.counts.get(n, 0) - c) * alpha_permille, 1000)
        if v:
            counts[n] = v
    return Acc(lo.total + rdiv((hi.total - lo.total) * alpha_permille, 1000), counts)


@dataclass(slots=True)
class Sc:
    """The three scenarios of one operation, before finalization."""

    min: Acc
    typ: Acc
    max: Acc
    arch: dict[str, str]
    sigma: bool = False  # True when the max scenario already includes a sizing factor
    flags: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class OpCost:
    """Cost of one operation under the three scenarios, in integer milli-GE.

    ``arch`` names the architecture assumed per scenario; ``breakdown`` counts the
    primitives of the typ architecture (before sizing and overhead factors);
    ``category`` is one of seq addcmp mul select logic shift div mem; ``quality`` is
    good, medium or poor; ``flags`` carries markers that limit confidence
    (``macro_no_table``, ``unknown_blackbox``).
    """

    min: int
    typ: int
    max: int
    arch: dict = field(default_factory=dict, compare=False)
    breakdown: dict = field(default_factory=dict, compare=False)
    category: str = "logic"
    quality: str = "good"
    flags: tuple[str, ...] = ()

    @property
    def ge3(self) -> Ge3:
        return Ge3(self.min, self.typ, self.max)

    def scaled(self, k: int) -> "OpCost":
        """``k`` instances of this operation."""
        return OpCost(
            self.min * k, self.typ * k, self.max * k, self.arch,
            {n: c * k for n, c in self.breakdown.items()}, self.category, self.quality, self.flags,
        )

    def __add__(self, other: "OpCost") -> "OpCost":
        """Scenario-wise sum (used to compose operations of the same category)."""
        bd = dict(self.breakdown)
        for n, c in other.breakdown.items():
            bd[n] = bd.get(n, 0) + c
        arch = {s: self.arch.get(s) if self.arch.get(s) == other.arch.get(s) else
                "+".join(dict.fromkeys(filter(None, (self.arch.get(s), other.arch.get(s)))))
                for s in ("min", "typ", "max")}
        return OpCost(
            self.min + other.min, self.typ + other.typ, self.max + other.max, arch, bd,
            self.category, _worse(self.quality, other.quality), tuple(dict.fromkeys(self.flags + other.flags)),
        )


ZERO_ARCH = {"min": "wire", "typ": "wire", "max": "wire"}
_Q = {"good": 0, "medium": 1, "poor": 2}


def _worse(a: str, b: str) -> str:
    return a if _Q[a] >= _Q[b] else b


def zero(category: str, quality: str = "good") -> OpCost:
    return OpCost(0, 0, 0, dict(ZERO_ARCH), {}, category, quality)


def finalize(sc: Sc, category: str, quality: str, p: Profile, *, logic: bool = False) -> OpCost:
    """Apply typ overhead, glue-logic scale and max overhead; enforce ``min <= typ <= max``."""
    lo, ty, hi = sc.min.total, sc.typ.total, sc.max.total
    ty = pm(ty, p.overhead_typ)
    if logic:
        ty = pm(ty, p.F["logic_scale"])
    if not sc.sigma:
        hi = pm(hi, p.overhead_max)
    lo = min(lo, ty)
    hi = max(hi, ty)
    return OpCost(lo, ty, hi, dict(sc.arch), dict(sc.typ.counts), category, quality, sc.flags)


def cap(const: OpCost, var: OpCost) -> OpCost:
    """An operation with a constant operand never costs more than its variable counterpart."""
    return OpCost(
        min(const.min, var.min), min(const.typ, var.typ), min(const.max, var.max),
        const.arch, const.breakdown, const.category, const.quality, const.flags,
    )
