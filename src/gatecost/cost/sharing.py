"""Resource sharing across exclusive branches (COST_MODEL.md 4.5)."""
from __future__ import annotations

from collections.abc import Sequence

from .acc import OpCost
from .profile import Profile


def shared_leaf_cost(leaves: Sequence[OpCost], w_a: int, w_b: int, w_o: int, p: Profile) -> OpCost:
    """Cost that replaces ``sum(leaves)`` when the k same-class leaf operations share one unit.

    ``shared_s = max_i cost_s(op_i) + (k-1)(w_a + w_b) MUX2 - (k-1) w_o MUX2``: operand
    multiplexers are added and the output multiplexers of the enclosing select tree vanish.
    Per scenario: typ uses the shared figure when it is smaller than the unshared sum
    (``resource_sharing = "auto"``; "off" never shares), min uses the smaller of the two,
    max is always unshared.
    """
    k = len(leaves)
    unshared = OpCost(
        sum(o.min for o in leaves), sum(o.typ for o in leaves), sum(o.max for o in leaves),
        leaves[0].arch, {}, leaves[0].category, leaves[0].quality,
    )
    if k < 2 or p.resource_sharing == "off":
        return unshared
    adj = (k - 1) * (w_a + w_b - w_o) * p.P["MUX2"]
    s_min = max(o.min for o in leaves) + adj
    s_typ = max(o.typ for o in leaves) + adj
    return OpCost(
        min(unshared.min, s_min), min(unshared.typ, s_typ), unshared.max,
        unshared.arch, unshared.breakdown, unshared.category, unshared.quality,
    )
