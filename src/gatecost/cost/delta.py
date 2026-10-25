"""Scenario-wise delta arithmetic, error combination and confidence (COST_MODEL.md section 5).

Deltas are always computed per scenario, never ``max(after) - min(before)``. Unchanged
operations cancel exactly because their per-scenario costs are identical on both sides.
"""
from __future__ import annotations

from collections.abc import Iterable
from math import isqrt

from ..result import Confidence, Ge3
from .acc import OpCost
from .profile import Profile
from .units import pm

A_FLOOR = 20_000  # milli-GE (20 GE)


def total(items: Iterable[OpCost | Ge3]) -> Ge3:
    """Scenario-wise sum."""
    lo = ty = hi = 0
    for it in items:
        lo += it.min
        ty += it.typ
        hi += it.max
    return Ge3(lo, ty, hi)


def delta(after: Ge3, before: Ge3) -> Ge3:
    """``Delta_s = after_s - before_s`` for each scenario s."""
    return Ge3(after.min - before.min, after.typ - before.typ, after.max - before.max)


def rms_permille(quality: str, p: Profile) -> int:
    return p.F[f"rms_{quality}"]


def residual_sigma(changed: Iterable[tuple[int, str]], p: Profile) -> int:
    """``sqrt(sum sigma_i^2)`` in milli-GE for changed operations given as (typ, quality).

    ``sigma_i = typ_i * rms_c``. Pass each changed operation's typ cost on each side where
    it exists (an operation present on both sides contributes twice).
    """
    s2 = 0
    for typ, q in changed:
        sig = pm(abs(typ), rms_permille(q, p))
        s2 += sig * sig
    return isqrt(s2)


def _ceil_isqrt(x: int) -> int:
    r = isqrt(x)
    return r if r * r == x else r + 1


def delta_range(d: Ge3, sigma: int) -> Ge3:
    """Range of a scenario-wise delta: systematic half-spread and ``2 sigma`` in quadrature.

    With ``lo = min_s Delta_s``, ``hi = max_s Delta_s``, centre ``c = (lo + hi) / 2`` and half-spread
    ``h = (hi - lo) / 2``, the range is ``c -/+ sqrt(h^2 + (2 sigma)^2)`` (COST_MODEL.md 5.1): the scenario
    spread is a correlated systematic error, the residual an independent one, so they add in quadrature
    rather than linearly.  The half-width is never below ``h``, so the range always contains
    ``[lo, hi]`` (and therefore ``typ``); with ``sigma = 0`` it is exactly ``[lo, hi]`` and with
    ``lo = hi`` exactly ``lo -/+ 2 sigma``.  Integer arithmetic: the full width is rounded up and the
    bounds outwards, so the range is never narrower than the real-valued formula.

    ``typ`` stays ``Delta_typ``, so the result always satisfies ``min <= typ <= max``.
    """
    lo, hi = min(d.min, d.typ, d.max), max(d.min, d.typ, d.max)
    s = hi - lo
    width = _ceil_isqrt(s * s + 16 * sigma * sigma)  # 2 * sqrt(h^2 + (2 sigma)^2) = sqrt(s^2 + (4 sigma)^2)
    mid2 = lo + hi
    return Ge3((mid2 - width) // 2, d.typ, -((-(mid2 + width)) // 2))


def range_ratio(d: Ge3) -> float:
    """``r = (max - min) / (2 * max(|typ|, 20 GE))`` (a reporting ratio, not used in costing)."""
    return (d.max - d.min) / (2 * max(abs(d.typ), A_FLOOR))


def range_spread(d: Ge3) -> str:
    """Spread of ``d`` around ``typ`` as text: ``+/-r%`` when symmetric, ``-x%/+y%`` otherwise.

    ``x = (typ - min) / b`` and ``y = (max - typ) / b`` with ``b = max(|typ|, 20 GE)``, the base of
    ``range_ratio`` (so ``r = (x + y) / 2``).  Only the wording depends on this, never the level."""
    base = max(abs(d.typ), A_FLOOR)
    down, up = round((d.typ - d.min) * 100 / base), round((d.max - d.typ) * 100 / base)
    if down == up:
        return f"+/-{round(range_ratio(d) * 100)}%"
    return f"-{down}%/+{up}%"


def confidence(
    d: Ge3,
    contributions: Iterable[tuple[str, str, int]] = (),
    flags: Iterable[str] = (),
    unsupported: int = 0,
) -> Confidence:
    """Confidence level of a cost or delta ``d`` (COST_MODEL.md 5.2).

    ``contributions`` are (category, quality, |typ delta| in milli-GE) triples;
    ``flags`` are OpCost flags (``macro_no_table``, ``unknown_blackbox``); ``unsupported``
    counts unsupported constructs in the changed cone.
    """
    r = range_ratio(d)
    contribs = list(contributions)
    flags = set(flags)
    tot = sum(c for _, _, c in contribs)
    share = lambda q: (sum(c for _, qq, c in contribs if qq == q) / tot) if tot else 0.0  # noqa: E731
    poor, good = share("poor"), share("good")
    reasons: list[str] = []
    forced = False
    if unsupported:
        reasons.append(f"{unsupported} unsupported construct{'s' if unsupported != 1 else ''} in the changed cone")
    if "unknown_blackbox" in flags:
        forced = True
        reasons.append("unknown black box (zero cost assumed)")
    if "macro_no_table" in flags:
        forced = True
        reasons.append("memory macro cost without a profile table")
    if poor > 0.30:
        forced = True
        by_cat: dict[str, int] = {}
        for cat, q, c in contribs:
            if q == "poor":
                by_cat[cat] = by_cat.get(cat, 0) + c
        top = max(sorted(by_cat), key=lambda k: by_cat[k])
        reasons.append(f"{round(poor * 100)}% of delta in {top} class (poor model quality)")
    if unsupported or forced:
        level = "low"
    elif good >= 0.90 and r <= 0.2:
        level = "high"
    elif r <= 0.5:
        level = "medium"
    else:
        level = "low"
    if level != "high" and not reasons:
        if r > 0.2:
            reasons.append(f"delta range is {range_spread(d)} of the estimate")
        else:
            reasons.append(f"only {round(good * 100)}% of delta in good-quality classes")
    return Confidence(level, tuple(reasons), round(r, 3))
