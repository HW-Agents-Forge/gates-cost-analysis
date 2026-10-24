"""Adders, negation, multi-operand sums, multipliers, dividers, powers (COST_MODEL.md 3.1, 3.5, 3.6)."""
from __future__ import annotations

from .acc import Acc, OpCost, Sc, cap, finalize, lerp, zero
from .profile import Profile
from .units import ceil_div, clog2, flog2, pm, rdiv


# ---------------------------------------------------------------------------------------
# adders

def add_acc(arch: str, n: int, p: Profile) -> Acc:
    """Primitive count of an n-bit adder of the named architecture (n >= 1)."""
    a = Acc()
    if n == 1:
        return a.add("XOR2", 1, p.P["XOR2"])
    L, G = clog2(n), p.C["G"]
    if arch == "ripple":
        a.add("FA", n - 1, p.P["FA"]).add("HA", 1, p.P["HA"])
    elif arch == "brent_kung":
        a.add("HA", n, p.P["HA"]).add("PREFIX", 2 * n - 2 - L, G).add("XOR2", n - 1, p.P["XOR2"])
    elif arch == "kogge_stone":
        a.add("HA", n, p.P["HA"]).add("PREFIX", n * L - n + 1, G).add("XOR2", n - 1, p.P["XOR2"])
    else:  # validated at profile load time
        raise ValueError(f"unknown adder architecture {arch!r}")
    return a


def _typ_label(p: Profile, names: tuple[str, str, str], alpha: int) -> str:
    return names[1] if alpha > 0 else names[0]


def add_sc(n: int, p: Profile, sub: bool = False) -> Sc:
    """ADD (or SUB) of two n-bit operands; min/mid/max architectures from the profile."""
    if n == 1:
        a = add_acc("ripple", 1, p)
        return Sc(a, a.copy(), a.copy(), {"min": "xor2", "typ": "xor2", "max": "xor2"})
    lo = add_acc(p.add_arch[0], n, p)
    mid = add_acc(p.add_arch[1], n, p)
    hi = add_acc(p.add_arch[2], n, p).scaled(p.sigma_arith)
    if sub:
        mid.add("SUB_EXTRA", n, p.C["sub_extra_per_bit"])
        hi.add("INV", n, p.P["INV"])
    return Sc(
        lo, lerp(lo, mid, p.alpha_add), hi,
        {"min": p.add_arch[0], "typ": _typ_label(p, p.add_arch, p.alpha_add), "max": p.add_arch[2]},
        sigma=True,
    )


def add_op(n: int, p: Profile, sub: bool = False) -> OpCost:
    return finalize(add_sc(n, p, sub), "addcmp", "good", p)


def ripple_op(n: int, p: Profile) -> OpCost:
    """A ripple adder in every scenario (used where the model prescribes one)."""
    a = add_acc("ripple", n, p)
    sc = Sc(a, a.copy(), a.copy(), {"min": "ripple", "typ": "ripple", "max": "ripple"})
    return finalize(sc, "addcmp", "good", p)


def add_const_op(n: int, k: int | None, p: Profile, sub: bool = False) -> OpCost:
    """ADD/SUB with a constant; ``k`` None means a non-zero constant of unknown value."""
    var = add_op(n, p, sub)
    tz = 0
    if k is not None:
        kk = k % (1 << n)
        if kk == 0:
            return zero("addcmp")
        tz = (kk & -kk).bit_length() - 1
    if n == 1:
        a = Acc().add("INV", 1, p.P["INV"])
        sc = Sc(a, a.copy(), a.copy(), {"min": "inv", "typ": "inv", "max": "inv"})
        return cap(finalize(sc, "addcmp", "good", p), var)
    lo = Acc().add("HA", n - tz, p.P["HA"])
    mid = lo.scaled(p.F["add_const_mid"])
    hi = lo.scaled(p.F["add_const_max"])
    arch = {"min": "const_ha", "typ": "const_ha", "max": "const_ha"}
    return cap(finalize(Sc(lo, lerp(lo, mid, p.alpha_add), hi, arch), "addcmp", "good", p), var)


def add_narrow_op(n: int, m: int, p: Profile, sub: bool = False) -> OpCost:
    """ADD/SUB of an n-bit result where one operand has only ``m`` (< n - 1) significant bits.

    The low ``m`` bits are a normal adder; the upper ``n - m`` bits only propagate the carry
    (an incrementer: one HA per bit, scaled like a constant add). Capped by the full adder."""
    var = add_op(n, p, sub)
    if m < 1 or m >= n - 1:
        return var
    low = add_op(m, p, sub) if m > 1 else None
    if low is None:
        h = Acc().add("HA", 1, p.P["HA"])
        low = finalize(Sc(h, h.copy(), h.copy(), {"min": "ha", "typ": "ha", "max": "ha"}), "addcmp", "good", p)
    up = Acc().add("HA", n - m, p.P["HA"])
    arch = {"min": "carry_inc", "typ": "carry_inc", "max": "carry_inc"}
    inc = finalize(Sc(up, lerp(up, up.scaled(p.F["add_const_mid"]), p.alpha_add),
                      up.scaled(p.F["add_const_max"]), arch), "addcmp", "good", p)
    return cap(low + inc, var)


def neg_op(n: int, p: Profile) -> OpCost:
    if n == 1:
        return zero("addcmp")
    a = Acc().add("OR2", n - 1, p.P["OR2"]).add("XOR2", n - 1, p.P["XOR2"])
    arch = {"min": "neg", "typ": "neg", "max": "neg"}
    return finalize(Sc(a, a.copy(), a.scaled(p.sigma_arith), arch, sigma=True), "addcmp", "good", p)


def sum_op(k: int, w: int, p: Profile) -> OpCost:
    """k-operand fused addition: CSA tree plus one carry-propagate adder (max: unfused chain)."""
    if k <= 1:
        return zero("addcmp")
    wc = w + clog2(k)
    csa = Acc().add("FA", (k - 2) * w, p.P["FA"])
    one = add_sc(wc, p)
    unfused = add_sc(wc, p).max.times(k - 1)
    sc = Sc(
        csa.plus(one.min), csa.plus(one.typ), unfused,
        {"min": "csa+" + one.arch["min"], "typ": "csa+" + one.arch["typ"], "max": "unfused"},
        sigma=True,
    )
    return finalize(sc, "addcmp", "good", p)


def sum_terms_op(terms: tuple[int, ...] | list[int], out: int, p: Profile) -> OpCost:
    """Fused sum of operands with the given effective widths into an ``out``-bit result.

    The CSA tree removes one bit per FA until two rows of at most ``out`` bits remain:
    ``max(0, sum(terms) - 2*max(terms))`` FA (``(k-2)*w`` for k equal terms), followed by one
    ``out``-bit carry-propagate adder. max is the unfused chain of narrow-operand adders."""
    ts = [t for t in terms if t > 0]
    k = len(ts)
    if k <= 1 or out <= 0:
        return zero("addcmp")
    csa = Acc().add("FA", max(0, sum(ts) - 2 * max(ts)), p.P["FA"])
    one = add_sc(out, p)
    unfused = one.max.times(k - 1)
    narrow, full = add_narrow_op(out, min(ts), p).typ, add_op(out, p).typ
    if full and narrow < full:  # the chain adds narrow operands: scale by the incrementer saving
        unfused = unfused.scaled(rdiv(narrow * 1000, full))
    sc = Sc(
        csa.plus(one.min), csa.plus(one.typ), unfused,
        {"min": "csa+" + one.arch["min"], "typ": "csa+" + one.arch["typ"], "max": "unfused"},
        sigma=True,
    )
    return finalize(sc, "addcmp", "good", p)


# ---------------------------------------------------------------------------------------
# multipliers

def _frac(a: Acc, num: int, den: int) -> Acc:
    return Acc(rdiv(a.total * num, den), {k: rdiv(v * num, den) for k, v in a.counts.items() if rdiv(v * num, den)})


def trunc_pp_count(n: int, m: int, t: int) -> int:
    """Partial-product bits of an n x m array lying in the columns below t."""
    total = 0
    for c in range(min(t, n + m - 1)):
        lo, hi = max(0, c - (m - 1)), min(c, n - 1)
        total += hi - lo + 1
    return total


def mul_sc(n: int, m: int, p: Profile, signed: bool = False, square: bool = False, trunc: int | None = None) -> Sc:
    n, m = max(n, m), min(n, m)
    t = n + m if trunc is None or trunc >= n + m else trunc
    full = t >= n + m
    cnt, den = (1, 1) if full else (trunc_pp_count(n, m, t), n * m)
    P = p.P
    extra = Acc()
    if signed:
        extra.add("INV", t, P["INV"]).add("HA", 2, P["HA"])
    if m == 1:  # a single partial-product row
        a = _frac(Acc().add("AND2", n, P["AND2"]), cnt, den).plus(extra)
        sc = Sc(a, a.copy(), a.copy(), {"min": "and", "typ": "and", "max": "and"})
    else:
        array = Acc().add("AND2", n * m, P["AND2"]).add("FA", max(0, n * m - n - m), P["FA"]).add("HA", n, P["HA"])
        array = _frac(array, cnt, den)
        rows = ((m + 2) // 2) * (n + 2)
        booth = Acc().add("BOOTH_SEL", rows, p.C["booth_select"]).add("BOOTH_ENC", ceil_div(m, 2), p.C["booth_encode"])
        booth = _frac(booth.add("FA", max(0, rows - 2 * (n + m)), P["FA"]), cnt, den)
        wal = _frac(Acc().add("AND2", n * m, P["AND2"]).add("FA", max(0, n * m - 2 * (n + m)), P["FA"]), cnt, den)
        la, lm, lk = p.add_arch
        cw = t
        if cw > 1:
            cpa_lo, cpa_mid, cpa_hi = add_acc(la, cw, p), add_acc(lm, cw, p), add_acc(lk, cw, p)
        else:
            cpa_lo = cpa_mid = cpa_hi = Acc()
        booth_ok = m >= p.booth_min_width
        names = p.mul_arch
        best_lo, name_lo = array, names[0]
        b_lo = booth.plus(cpa_lo)
        if booth_ok and b_lo.total < best_lo.total:
            best_lo, name_lo = b_lo, names[1]
        best_ty, name_ty = array, names[0]
        b_ty = booth.plus(lerp(cpa_lo, cpa_mid, p.alpha_mul))
        if booth_ok and b_ty.total < best_ty.total:
            best_ty, name_ty = b_ty, names[1]
        hi = wal.scaled(p.sigma_arith).plus(cpa_hi.scaled(p.sigma_arith))
        sc = Sc(best_lo.plus(extra), best_ty.plus(extra), hi.plus(extra), {"min": name_lo, "typ": name_ty, "max": names[2]}, sigma=True)
    if square and n == m:
        f = p.F["square"]
        sc = Sc(sc.min.scaled(f), sc.typ.scaled(f), sc.max.scaled(f), sc.arch, sc.sigma)
    return sc


def mul_op(n: int, m: int, p: Profile, signed: bool = False, square: bool = False, trunc: int | None = None) -> OpCost:
    return finalize(mul_sc(n, m, p, signed, square, trunc), "mul", "medium", p)


def csd(k: int) -> list[tuple[int, int]]:
    """Canonical signed-digit form: (bit position, +1/-1) pairs, lowest position first."""
    out = []
    pos = 0
    while k != 0:
        if k & 1:
            d = 2 - (k & 3)
            out.append((pos, d))
            k -= d
        k >>= 1
        pos += 1
    return out


def _chain(n_add: int, n_sub: int, wt: int, wmax: int, p: Profile, name: str, category: str = "mul", quality: str = "medium") -> OpCost:
    """Adder chain cost: typ at width wt, min = cmul_min * typ, max at width wmax."""
    wt, wmax = max(1, wt), max(wt, wmax, 1)
    a_t, s_t = add_op(wt, p), add_op(wt, p, sub=True)
    a_m, s_m = add_op(wmax, p), add_op(wmax, p, sub=True)
    typ = a_t.typ * n_add + s_t.typ * n_sub
    bd: dict[str, int] = {}
    for oc, k in ((a_t, n_add), (s_t, n_sub)):
        if k <= 0:
            continue
        for nm, c in oc.breakdown.items():
            bd[nm] = bd.get(nm, 0) + c * k
    mx = a_m.max * n_add + s_m.max * n_sub
    lo = pm(typ, p.F["cmul_min"])
    return OpCost(lo, typ, max(mx, typ), {"min": name, "typ": name, "max": name}, bd, category, quality)


def mul_const_op(n: int, k: int | None, p: Profile, out: int | None = None) -> OpCost:
    """n-bit operand times a constant; ``k`` None = non-zero constant of unknown value."""
    if k is not None and k == 0:
        return zero("mul", "medium")
    if k is None:  # CSD density of 1/3 over an n-bit constant
        h, nsub, dmin, kb, neg = max(2, ceil_div(n + 1, 3)), 0, 2, n, False
    else:
        digs = csd(k)
        h = len(digs)
        nsub = sum(1 for _, s in digs if s < 0)
        dmin = min((b[0] - a[0] for a, b in zip(digs, digs[1:])), default=0)
        kb = clog2(abs(k)) if abs(k) > 1 else 0
        neg = all(s < 0 for _, s in digs)
        if neg:  # sum the negative digits with adders, then negate once
            nsub = 0
    if h <= 1:
        if k is not None and k < 0:
            c = neg_op(n if out is None else min(n, out), p)
            return OpCost(c.min, c.typ, c.max, c.arch, c.breakdown, "mul", "medium")
        return zero("mul", "medium")
    wt, wmax = n + kb - dmin, n + kb
    if out is not None:
        wt, wmax = min(wt, out), min(wmax, out)
    oc = _chain(h - 1 - nsub, nsub, wt, wmax, p, "csd_adders")
    if neg:
        ng = neg_op(wt, p)
        oc = oc + ng
        oc = OpCost(oc.min, oc.typ, oc.max, {"min": "csd_adders", "typ": "csd_adders", "max": "csd_adders"}, oc.breakdown, "mul", "medium", oc.flags)
    return oc


def mac_op(n: int, m: int, cw: int, p: Profile, signed: bool = False) -> OpCost:
    """a*b + c fused into the multiplier reduction tree; max (unfused) is MUL + ADD."""
    mul = mul_op(n, m, p, signed)
    fa = Acc().add("FA", cw, p.P["FA"])
    fused = pm(fa.total, p.overhead_typ)
    unfused_add = add_op(max(n + m, cw), p)
    bd = dict(mul.breakdown)
    bd["FA"] = bd.get("FA", 0) + cw
    return OpCost(
        mul.min + fa.total, mul.typ + fused, max(mul.max + unfused_add.max, mul.typ + fused),
        {"min": mul.arch["min"] + "+csa", "typ": mul.arch["typ"] + "+csa", "max": "unfused"},
        bd, "mul", "medium",
    )


def pow_op(n: int, e: int, p: Profile, out: int | None = None) -> OpCost:
    """x ** e with constant e >= 0: square-and-multiply at effective widths, truncated to ``out``."""
    if e <= 1:
        return zero("mul", "medium")
    wo = out if out is not None else n
    total = zero("mul", "medium")
    first = True
    cur = min(n, wo)
    for bit in bin(e)[3:]:  # after the leading one
        sq = mul_op(cur, cur, p, square=True, trunc=wo)
        total = sq if first else total + sq
        first = False
        cur = min(2 * cur, wo)
        if bit == "1":
            total = total + mul_op(cur, min(n, wo), p, trunc=wo)
            cur = min(cur + n, wo)
    return OpCost(total.min, total.typ, total.max, total.arch, total.breakdown, "mul", "medium")


# ---------------------------------------------------------------------------------------
# dividers

def _div_core(n: int, m: int, p: Profile) -> OpCost:
    lo = Acc().add("FA", n * (m + 1), p.P["FA"]).add("XOR2", n * (m + 1), p.P["XOR2"])
    ty = Acc().add("FA", n * (m + 1), p.P["FA"]).add("MUX2", n * m, p.P["MUX2"]).scaled(p.F["div_typ"])
    hi = ty.scaled(p.F["div_max"])
    arch = {"min": "nonrestoring", "typ": "restoring", "max": "restoring_fast"}
    return finalize(Sc(lo, ty, hi, arch, sigma=True), "div", "poor", p)


def div_op(n: int, m: int, p: Profile, signed: bool = False, k: int | None = None, mod: bool = False) -> OpCost:
    """DIV/MOD of an n-bit dividend by an m-bit divisor (or by constant ``k``)."""
    if k is not None:
        if k <= 0:
            raise ValueError("division by a constant must use a positive divisor")
        if k & (k - 1) == 0:  # power of two
            if k == 1 or not signed:
                return zero("div", "poor")
            r = ripple_op(n, p)
            return OpCost(r.min, r.typ, r.max, r.arch, r.breakdown, "div", "poor")
        h = max(2, ceil_div(n + 1, 3))
        oc = _chain(h - 1, 0, 2 * n, 2 * n, p, "reciprocal", "div", "poor")
        if mod:
            back = mul_const_op(n, k, p)
            sub = add_op(n, p, sub=True)
            oc = oc + OpCost(back.min + sub.min, back.typ + sub.typ, back.max + sub.max, back.arch, back.breakdown, "div", "poor")
        return OpCost(oc.min, oc.typ, oc.max, oc.arch, oc.breakdown, "div", "poor")
    oc = _div_core(n, m, p)
    if signed:
        ng = neg_op(n, p).scaled(2) + neg_op(m, p)
        oc = oc + OpCost(ng.min, ng.typ, ng.max, ng.arch, ng.breakdown, "div", "poor")
    return OpCost(oc.min, oc.typ, oc.max, oc.arch, oc.breakdown, "div", "poor")


def ilog2_fix(n: int) -> int:
    """Piecewise-linear log2(n) in milli (exact at powers of two); monotone, no floats."""
    if n < 1:
        raise ValueError("log2 of a non-positive number")
    f = flog2(n)
    return f * 1000 + rdiv((n - (1 << f)) * 1000, 1 << f)
