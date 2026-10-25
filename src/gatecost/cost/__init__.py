"""Cost model: primitive table, profiles, per-operation formulas, deltas and confidence.

Everything here is pure (no I/O besides reading profile files) and integer-valued: costs
are milli-GE (1 GE = 1000), ratios are permille. ``COST_MODEL.md`` is the normative
reference; this docstring documents the Python API used by the later stages.

Profiles
    ``load_profile(spec="generic-asic", overrides=None, base_dir=None) -> Profile``
        built-in name or TOML path; ``extends`` and a ``[cost.override]`` dict are applied.
    ``list_profiles()``, ``dump_profile(profile) -> str`` (resolved TOML),
    ``Profile.scaled(num, den)`` (scale every GE value), ``Profile.blackbox_cost(name)``.

Operations
    ``cost_op(op, attrs, profile) -> OpCost``
        ``op`` (case-insensitive) and ``attrs`` (widths are *effective* widths already
        reduced by demanded-bits / known-bits analysis):

        ADD, SUB        n [const=K | const_unknown=True] [carry_only=True]
        NEG             n
        SUM             k, w            k-operand fused addition (CSA tree + one CPA)
        MUL             n, m [signed] [square] [trunc=T]       T = low bits demanded
                        n, const=K | const_unknown=True [out=O]   constant multiplier (CSD)
        MAC             n, m [c=addend width] [signed]         a*b + c fused
        DIV, MOD        n, m [signed] | n, const=K [signed]
        POW             n, exp=E [out=O]  |  base=2, s=exponent bits
        SHL SHR SAR ROL ROR   n, s [const_amount] [lhs_one] [wa=data width] [tz=known-zero amount LSBs]
                        (lhs_one: ``1 << x``; wa > n: SHR/SAR selecting n bits of a wider word, ``d[s*W +: W]``)
        AND OR XOR NAND NOR XNOR NOT     n (or w)
        EQ NE LT LE GT GE    n [signed] [const=K | const_unknown=True]
        RED_AND RED_OR RED_XOR   n
        MUX             k, w [const_inputs=0|1|2] [same_inputs] [complement]   (k == 2 only: constant arms, x/~x arms)
        PMUX PRIORITY   k, w
        DECODE          s               s -> 2**s
        ENCODE          k [priority]
        DFF (REG)       n [async_reset] [async_set_reset] [sync_reset] [enable] [scan]
        LATCH           n
        MEM             depth, width [rports=1] [wports=1] [sync_read] [const_reads=0]
        ROM             depth, width [contents=list of ints]
        BLACKBOX        name            looked up in the profile's ``[blackbox]``

    ``OpCost(min, typ, max, arch, breakdown, category, quality, flags)``: milli-GE per
    scenario; ``arch`` is ``{"min","typ","max"}`` architecture names; ``breakdown`` maps a
    primitive (or pseudo-primitive) to its count in the typ architecture, before sizing and
    overhead factors; ``category`` in seq addcmp mul select logic shift div mem; ``quality``
    in good medium poor; ``flags`` in {"macro_no_table", "unknown_blackbox"}.
    ``OpCost.ge3``, ``OpCost.scaled(k)`` (k instances), ``a + b`` (scenario-wise sum).
    Invalid attributes raise ``ValueError``.

Composition helpers
    ``shared_leaf_cost(leaves, w_a, w_b, w_o, profile)``  resource sharing (4.5)
    ``total(items)``, ``delta(after, before)``              scenario-wise sums / deltas
    ``residual_sigma(changed, profile)``, ``delta_range(d, sigma)``   error combination (5.1)
    ``range_ratio(d)``, ``confidence(d, contributions, flags, unsupported)``   (5.2)
"""
from __future__ import annotations

from .acc import OpCost
from .delta import confidence, delta, delta_range, range_ratio, residual_sigma, total
from .ops import OPS, cost_op
from .profile import DEFAULT_PROFILE, Profile, builtin_names, dump_profile, list_profiles, load_profile
from .sharing import shared_leaf_cost

_LAZY = {"build_cost_tree": "build", "category_totals": "build", "module_totals": "build",
         "tree_confidence": "build", "CostOp": "tree", "DesignCostTree": "tree",
         "InstanceEdge": "tree", "ModuleCost": "tree"}


def __getattr__(name: str):
    """Cost-tree names are imported on first use (``estimate`` does not need them: start-up time)."""
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module("." + mod, __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "CostOp", "DesignCostTree", "InstanceEdge", "ModuleCost", "build_cost_tree", "category_totals",
    "module_totals", "tree_confidence",
    "DEFAULT_PROFILE", "OPS", "OpCost", "Profile", "builtin_names", "confidence", "cost_op", "delta",
    "delta_range", "dump_profile", "list_profiles", "load_profile", "range_ratio", "residual_sigma",
    "shared_leaf_cost", "total",
]
