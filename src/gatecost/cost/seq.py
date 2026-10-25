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


def _macro_op(D: int, W: int, p: Profile) -> OpCost:
    """Cost of one memory macro of D x W (from the profile table when it is a full grid)."""
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


def mem_op(depth: int, width: int, p: Profile, rports: int = 1, wports: int = 1, sync_read: bool = False,
           const_reads: int = 0) -> OpCost:
    """D x W memory: flop-based, or ``min(flops, macro)`` per scenario when synchronous.

    A synchronous-read memory may be implemented by a memory compiler.  The smallest macro
    has ``macro_min_bits`` bits (and 16 words), so a smaller memory is costed as that
    smallest macro (``D' = max(D, 16, ceil(macro_min_bits / W))``) and the result is the
    cheaper of the flop array and the macro, scenario by scenario.  Both are non-decreasing
    in D, hence so is their minimum: there is no cliff at the macro threshold.
    """
    flops = _flop_mem_op(depth, width, p, rports, wports, sync_read, const_reads)
    if not (p.macro_enable and sync_read):
        return flops
    D, W = depth, width
    macro = _macro_op(max(D, 16, ceil_div(p.macro_min_bits, W)), W, p)
    pick = {s: ("macro" if getattr(macro, s) < getattr(flops, s) else "flops") for s in ("min", "typ", "max")}
    if all(v == "flops" for v in pick.values()):
        return flops
    src = macro if pick["typ"] == "macro" else flops
    return OpCost(
        min(macro.min, flops.min), min(macro.typ, flops.typ), min(macro.max, flops.max),
        {s: (macro if pick[s] == "macro" else flops).arch[s] for s in pick},
        src.breakdown, "mem", "poor", src.flags,
    )


def _flop_mem_op(depth: int, width: int, p: Profile, rports: int, wports: int, sync_read: bool,
                 const_reads: int = 0) -> OpCost:
    D, W, R, Pw = depth, width, rports, wports
    Rv = max(R - const_reads, 0)          # read ports with a constant address are plain word taps: no mux tree
    P = p.P
    s = clog2(D)
    dec = decode_acc(s, p) if s >= 2 else None

    def decode(which: str, scale: int) -> Acc:
        if s == 0:
            return Acc()
        if s == 1:
            return Acc().add("INV", scale, P["INV"])
        return getattr(dec, which).times(scale)

    ty = Acc().add("DFF", D * W, P["DFF"]).add("MUX2", D * W, P["MUX2"])
    ty = ty.add("MUX2", Rv * (D - 1) * W, P["MUX2"]).plus(decode("typ", Pw)).add("MUX2", (Pw - 1) * D * W, P["MUX2"])
    lo = Acc().add("DLAT", D * W, P["DLAT"]).add("ICG", D, P["ICG"])
    lo = lo.add("MUX4_EQ", Rv * (D - 1) * W, p.C["mux_min"]).plus(decode("min", Pw))
    if sync_read:  # the registered read data: one W-bit flop bank per read port
        ty = ty.add("DFF", R * W, P["DFF"])
        lo = lo.add("DFF", R * W, P["DFF"])
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
    """D x W constant table.  Without ``contents`` every column is assumed to be random logic.

    typ is the cheaper of two textbook implementations:

    * decoder + OR planes: one shared ``D``-output decoder, then each remaining column ORs the
      words in which it is set (or, through an inverter, clear), ``min(ones, zeros)`` inputs at
      ``rom_plane`` GE per merged input (NAND4 first level plus the OR tree);
    * two-level logic per column (``rom_lit`` GE per literal of a minimal sum of products; for
      random data ``D·log2(D)/6`` literals), which wins for few columns or very small tables.

    min is ``rom_min`` times typ, max the all-columns MUX2 tree (or typ if larger).
    """
    D, W = depth, width
    if D <= 1:
        return zero("mem", "poor")
    s = clog2(D)
    P = p.P
    mux_hi = ceil_div(D, 2) * P["MUX2"]
    plane_unit = p.C["rom_plane"]
    lit_unit = p.C["rom_lit"]

    inv = 0
    cols = 0
    plane = 0            # milli-GE of the OR-plane inputs of all remaining columns
    sop = 0              # milli-GE of the per-column two-level implementation
    if contents is None:
        cols = W
        plane = W * rdiv(D * plane_unit, 2)
        sop = W * rdiv(D * ilog2_fix(D) * lit_unit, 6000)
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
            ones = sum(col)
            plane += min(ones, care - ones) * plane_unit
            if s <= 6:
                on = {i for i, b in enumerate(col) if b}
                off = {i for i, b in enumerate(col) if not b}
                sop += min(sop_literals(on, dc, s), sop_literals(off, dc, s)) * lit_unit
            else:
                sop += rdiv(D * ilog2_fix(D) * lit_unit, 6000)
    ty = 0
    if cols:
        dec = decode_acc(s, p).typ.total if s >= 2 else P["INV"]
        textbook = dec + plane
        ty = min(textbook, sop)
    lo = pm(ty, p.F["rom_min"])
    hi = max(cols * mux_hi, ty)
    acc_lo, acc_ty, acc_hi = Acc(lo, {}), Acc(ty, {}), Acc(hi, {})
    for a in (acc_lo, acc_ty, acc_hi):
        a.add("INV", inv, P["INV"])
    acc_ty.counts["ROM_COL"] = cols
    return finalize(Sc(acc_lo, acc_ty, acc_hi, {"min": "rom", "typ": "rom", "max": "mux_tree"}), "mem", "poor", p)
