"""Registers, latches, memories and ROMs (COST_MODEL.md 3.7, 3.8)."""
from __future__ import annotations

from fractions import Fraction

from .acc import Acc, OpCost, Sc, finalize, zero
from .arith import ilog2_fix
from .logic import decode_acc
from .profile import Profile
from .units import ceil_div, clog2, pm, rdiv


# ---------------------------------------------------------------------------------------
# registers

def _bank(base: str, n: int, style: str, p: Profile) -> Acc:
    """n flops of the ``base`` kind with the given enable implementation."""
    P = p.P
    a = Acc()
    if style == "none":
        return a.add(base, n, P[base])
    if style == "mux":
        return a.add(base, n, P[base]).add("MUX2", n, P["MUX2"])
    if style == "edff":
        a.add("EDFF", n, P["EDFF"])
        return a.add("ASYNC_EXTRA", n, P[base] - P["DFF"])
    if style == "icg":
        return a.add(base, n, P[base]).add("ICG", 1, P["ICG"])
    raise ValueError(style)


def reg_op(n: int, p: Profile, async_reset: bool = False, async_set_reset: bool = False,
           sync_reset: bool = False, enable: bool = False, scan: bool | None = None) -> OpCost:
    """n flip-flops. ``async_reset`` covers asynchronous reset or set; ``async_set_reset`` both."""
    P = p.P
    base = "DFFRS" if async_set_reset else "DFFR" if async_reset else "DFF"
    scan = p.scan if scan is None else scan
    if not enable:
        lo = ty = hi = _bank(base, n, "none", p)
    else:
        mux = _bank(base, n, "mux", p)
        options = [_bank(base, n, "edff", p)]
        if n >= p.icg_min_width:
            options.append(_bank(base, n, "icg", p))
        options.append(mux)
        lo = min(options, key=lambda a: a.total)
        style = p.enable_style
        if style == "mux":
            ty = mux
        elif style == "edff":
            ty = options[0]
        else:  # min-style gating
            ty = min(options[:-1], key=lambda a: a.total)
        hi = mux
    lo, ty, hi = lo.copy(), ty.copy(), hi.copy()
    if sync_reset:
        lo.add("SYNC_RST", n, p.C["sync_reset_min"])
        ty.add("AND2", n, P["AND2"])
        hi.add("MUX2", n, P["MUX2"])
    if scan:
        for a in (lo, ty, hi):
            a.add("SDFF_ADD", n, P["SDFF_ADD"])
    return finalize(Sc(lo, ty, hi, {"min": "dff", "typ": "dff", "max": "dff"}), "seq", "good", p)


def latch_op(n: int, p: Profile) -> OpCost:
    a = Acc().add("DLAT", n, p.P["DLAT"])
    return finalize(Sc(a, a.copy(), a.copy(), {"min": "latch", "typ": "latch", "max": "latch"}), "seq", "good", p)


# ---------------------------------------------------------------------------------------
# memories

def _interp(p: Profile, d: int, w: int) -> int | None:
    """Bilinear interpolation of ``macro_table`` (milli-GE), or None when it is not a full grid."""
    tab = {(a, b): g for a, b, g in p.macro_table}
    if not tab:
        return None
    ds, ws = sorted({a for a, _ in tab}), sorted({b for _, b in tab})
    if len(tab) != len(ds) * len(ws):
        return None

    def bracket(vals: list[int], x: int) -> tuple[int, int]:
        if len(vals) == 1:
            return vals[0], vals[0]
        i = max(0, min(len(vals) - 2, sum(1 for v in vals if v <= x) - 1))
        return vals[i], vals[i + 1]

    d0, d1 = bracket(ds, d)
    w0, w1 = bracket(ws, w)
    td = Fraction(d - d0, d1 - d0) if d1 != d0 else Fraction(0)
    tw = Fraction(w - w0, w1 - w0) if w1 != w0 else Fraction(0)
    v = (
        tab[(d0, w0)] * (1 - td) * (1 - tw) + tab[(d1, w0)] * td * (1 - tw)
        + tab[(d0, w1)] * (1 - td) * tw + tab[(d1, w1)] * td * tw
    )
    return max(0, round(v))


def mem_op(depth: int, width: int, p: Profile, rports: int = 1, wports: int = 1, sync_read: bool = False) -> OpCost:
    """D x W memory: flop-based, or a macro when synchronous and large enough."""
    D, W, R, Pw = depth, width, rports, wports
    P = p.P
    if p.macro_enable and sync_read and D * W >= p.macro_min_bits and D >= 16:
        table = _interp(p, D, W)
        flags: tuple[str, ...] = ()
        if table is not None:
            cost, fmin, fmax = table, p.F["macro_table_min"], p.F["macro_table_max"]
        else:
            m = p.macro
            cost = m["c0"] + pm(m["c_bit"] * D * W, 1000) + m["c_row"] * D + m["c_col"] * W
            fmin, fmax = p.F["macro_min"], p.F["macro_max"]
            flags = ("macro_no_table",)
        typ = Acc().add_milli("MACRO", cost)
        sc = Sc(Acc(pm(cost, fmin), dict(typ.counts)), typ, Acc(pm(cost, fmax), dict(typ.counts)),
                {"min": "macro", "typ": "macro", "max": "macro"}, sigma=True, flags=flags)
        return finalize(sc, "mem", "poor", p)
    s = clog2(D)
    dec = decode_acc(s, p) if s >= 2 else None

    def decode(which: str, scale: int) -> Acc:
        if s == 0:
            return Acc()
        if s == 1:
            return Acc().add("INV", scale, P["INV"])
        return getattr(dec, which).times(scale)

    ty = Acc().add("DFF", D * W, P["DFF"]).add("MUX2", D * W, P["MUX2"])
    ty = ty.add("MUX2", R * (D - 1) * W, P["MUX2"]).plus(decode("typ", Pw)).add("MUX2", (Pw - 1) * D * W, P["MUX2"])
    lo = Acc().add("DLAT", D * W, P["DLAT"]).add("ICG", D, P["ICG"])
    lo = lo.add("MUX4_EQ", R * (D - 1) * W, p.C["mux_min"]).plus(decode("min", Pw))
    hi = ty.scaled(p.sigma_other)
    return finalize(Sc(lo, ty, hi, {"min": "latch_array", "typ": "flop_array", "max": "flop_array"}, sigma=True), "mem", "poor", p)


# ---------------------------------------------------------------------------------------
# ROM

def _prime_implicants(care_on: set[int], dc: set[int], nv: int) -> list[tuple[int, int]]:
    """Quine-McCluskey primes as (value, dash-mask) pairs."""
    cur = {(m, 0) for m in care_on | dc}
    primes: set[tuple[int, int]] = set()
    while cur:
        nxt: set[tuple[int, int]] = set()
        used: set[tuple[int, int]] = set()
        items = sorted(cur)
        by_mask: dict[int, list[int]] = {}
        for v, mk in items:
            by_mask.setdefault(mk, []).append(v)
        for mk, vals in by_mask.items():
            vs = set(vals)
            for v in vals:
                for b in range(nv):
                    bit = 1 << b
                    if not mk & bit and not v & bit and (v | bit) in vs:
                        nxt.add((v, mk | bit))
                        used.add((v, mk))
                        used.add((v | bit, mk))
        primes |= cur - used
        cur = nxt
    return sorted(primes)


def sop_literals(on: set[int], dc: set[int], nv: int) -> int:
    """Literal count of a greedy minimal sum-of-products cover (nv <= 6)."""
    if not on:
        return 0
    primes = _prime_implicants(on, dc, nv)

    def covers(pi: tuple[int, int], m: int) -> bool:
        return (m & ~pi[1]) == pi[0]

    remaining = set(on)
    chosen: list[tuple[int, int]] = []
    while remaining:
        # essential primes first, then the prime covering the most remaining minterms
        pick = None
        for m in sorted(remaining):
            cov = [pi for pi in primes if covers(pi, m)]
            if len(cov) == 1:
                pick = cov[0]
                break
        if pick is None:
            pick = max(primes, key=lambda pi: (sum(covers(pi, m) for m in remaining), -bin(pi[1]).count("1"), pi))
        chosen.append(pick)
        remaining = {m for m in remaining if not covers(pick, m)}
    return sum(nv - bin(mk).count("1") for _, mk in chosen)


def rom_op(depth: int, width: int, p: Profile, contents: list[int] | tuple[int, ...] | None = None) -> OpCost:
    """D x W constant table. Without ``contents`` every column is assumed to be random logic."""
    D, W = depth, width
    if D <= 1:
        return zero("mem", "poor")
    s = clog2(D)
    P = p.P
    mux_hi = ceil_div(D, 2) * P["MUX2"]
    generic = rdiv(p.C["rom_col"] * D * 1000, ilog2_fix(D))

    def col_cost(lits: int | None) -> tuple[int, int, int]:
        ty = generic if lits is None else lits * p.C["rom_lit"]
        return pm(ty, p.F["rom_min"]), ty, max(mux_hi, ty)

    lo = ty = hi = 0
    inv = 0
    cols = 0
    if contents is None:
        lo, ty, hi = (v * W for v in col_cost(None))
        cols = W
    else:
        words = list(contents)[:D]
        care = len(words)
        dc = set(range(care, 1 << s))
        seen: list[tuple[int, ...]] = []
        for j in range(W):
            col = tuple((w >> j) & 1 for w in words)
            comp = tuple(1 - b for b in col)
            if all(b == col[0] for b in col):
                continue
            if any(col == tuple((i >> a) & 1 for i in range(care)) for a in range(s)):
                continue
            if any(comp == tuple((i >> a) & 1 for i in range(care)) for a in range(s)):
                inv += 1
                continue
            if col in seen:
                continue
            if comp in seen:
                inv += 1
                continue
            seen.append(col)
            cols += 1
            lits = None
            if s <= 6:
                on = {i for i, b in enumerate(col) if b}
                off = {i for i, b in enumerate(col) if not b}
                lits = min(sop_literals(on, dc, s), sop_literals(off, dc, s))
            a, b, c = col_cost(lits)
            lo, ty, hi = lo + a, ty + b, hi + c
    acc_lo, acc_ty, acc_hi = Acc(lo, {}), Acc(ty, {}), Acc(hi, {})
    for a in (acc_lo, acc_ty, acc_hi):
        a.add("INV", inv, P["INV"])
    acc_ty.counts["ROM_COL"] = cols
    return finalize(Sc(acc_lo, acc_ty, acc_hi, {"min": "rom", "typ": "rom", "max": "mux_tree"}), "mem", "poor", p)
