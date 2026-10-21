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


def delta_range(d: Ge3, sigma: int) -> Ge3:
    """Range of a scenario-wise delta: ``min_s Delta_s - 2 sigma`` .. ``max_s Delta_s + 2 sigma``.

    ``typ`` stays ``Delta_typ``, so the result always satisfies ``min <= typ <= max``.
    """
    lo, hi = min(d.min, d.typ, d.max), max(d.min, d.typ, d.max)
    return Ge3(lo - 2 * sigma, d.typ, hi + 2 * sigma)


def range_ratio(d: Ge3) -> float:
    """``r = (max - min) / (2 * max(|typ|, 20 GE))`` (a reporting ratio, not used in costing)."""
    return (d.max - d.min) / (2 * max(abs(d.typ), A_FLOOR))


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
            reasons.append(f"delta range is +/-{round(r * 100)}% of the estimate")
        else:
            reasons.append(f"only {round(good * 100)}% of delta in good-quality classes")
    return Confidence(level, tuple(reasons), round(r, 3))
