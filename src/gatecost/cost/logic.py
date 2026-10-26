"""Comparators, bitwise ops, reductions, selection, decoding and shifters (COST_MODEL.md 3.2-3.4)."""
from __future__ import annotations

from .acc import Acc, OpCost, Sc, cap, finalize, zero
from .profile import Profile
from .units import ceil_div, clog2, pm, rdiv


def _all(a: Acc, arch: str, sigma: bool = False) -> Sc:
    return Sc(a, a.copy(), a.copy(), {"min": arch, "typ": arch, "max": arch}, sigma)


# ---------------------------------------------------------------------------------------
# comparators

def eq_op(n: int, p: Profile, const: bool = False) -> OpCost:
    """EQ/NE of two n-bit values, or of one n-bit value and a constant."""
    P, C = p.P, p.C
    if const:
        if n == 1:
            return zero("addcmp")
        lo = Acc().add("AND_IN", n - 1, C["eq_chain_min"])
        ty = Acc().add("AND_IN", n - 1, C["eq_chain_typ"])
        hi = Acc().add("AND_IN", n - 1, C["eqc_max"])
        return cap(finalize(Sc(lo, ty, hi, {"min": "tree", "typ": "tree", "max": "tree"}), "addcmp", "good", p), eq_op(n, p))
    lo = Acc().add("XOR2", n, P["XOR2"]).add("AND_IN", n - 1, C["eq_chain_min"])
    ty = Acc().add("XOR2", n, P["XOR2"]).add("AND_IN", n - 1, C["eq_chain_typ"])
    hi = Acc().add("EQ_BIT", n, C["eq_max_per_bit"])
    return finalize(Sc(lo, ty, hi, {"min": "xor_tree", "typ": "xor_tree", "max": "xor_tree"}), "addcmp", "good", p)


def lt_op(n: int, p: Profile, signed: bool = False, const: bool = False) -> OpCost:
    """LT/LE/GT/GE; a signed comparison adds one XOR2 (an INV against a constant)."""
    C, P = p.C, p.P
    names = {"min": "cmp", "typ": "cmp", "max": "cmp"}
    if const:
        lo, ty, hi = (Acc().add("CMP_BIT", n, C[f"ltc_{s}"]) for s in ("min", "typ", "max"))
        if signed:
            for a in (lo, ty, hi):
                a.add("INV", 1, P["INV"])
        return cap(finalize(Sc(lo, ty, hi, names), "addcmp", "good", p), lt_op(n, p, signed))
    lo, ty = (Acc().add("CMP_BIT", n, C[f"lt_{s}"]) for s in ("min", "typ"))
    hi = Acc().add("CMP_BIT", n, C["lt_max"]).scaled(p.sigma_arith)
    if signed:
        for a in (lo, ty, hi):
            a.add("XOR2", 1, P["XOR2"])
    return finalize(Sc(lo, ty, hi, names, sigma=True), "addcmp", "good", p)


# ---------------------------------------------------------------------------------------
# bitwise and reductions

_GATE = {"AND": "AND2", "OR": "OR2", "XOR": "XOR2", "NAND": "NAND2", "NOR": "NOR2", "XNOR": "XNOR2"}


def bitwise_op(kind: str, w: int, p: Profile) -> OpCost:
    if kind == "NOT":
        # An inverter is usually absorbed into neighbouring logic: 0 / half / full.
        full = Acc().add("INV", w, p.P["INV"])
        return finalize(Sc(Acc(), full.scaled(500), full, {"min": "absorbed", "typ": "absorbed", "max": "inv"}), "logic", "good", p)
    g = _GATE[kind]
    # XOR/XNOR cannot merge into AND-OR complex gates (AOI/OAI), so the glue-logic scale does not apply to them
    return finalize(_all(Acc().add(g, w, p.P[g]), "gate"), "logic", "good", p, logic=kind not in ("XOR", "XNOR"))


def reduce_op(kind: str, n: int, p: Profile) -> OpCost:
    """RED_AND / RED_OR (also used for compares against zero) and RED_XOR (no glue-logic scale: see bitwise_op)."""
    if n == 1:
        return zero("logic")
    pre = "red_xor" if kind == "XOR" else "red_and"
    name = "XOR_IN" if kind == "XOR" else "AND_IN"
    lo, ty, hi = (Acc().add(name, n - 1, p.C[f"{pre}_{s}"]) for s in ("min", "typ", "max"))
    return finalize(Sc(lo, ty, hi, {"min": "tree", "typ": "tree", "max": "tree"}), "logic", "good", p, logic=kind != "XOR")


def reduce_chain_op(kind: str, n_in: int, w: int, p: Profile) -> OpCost:
    """A ``w``-input reduction that reuses the result of a nested reduction of the same kind (``^a[15:i]`` from
    ``^a[15:i+1]``): ``n_in`` inputs remain (the sub-result and ``n_in - 1`` new bits).

    A chain of such reductions is a prefix computation, the structure of a carry chain, costed as the adders of
    section 3.1 are: min is the serial chain (``n_in - 1`` gates); typ moves by ``alpha`` (``alpha_add``) towards a
    Brent-Kung prefix network, about twice the cells of the chain; max is a Kogge-Stone network, ``ceil(log2 w)``
    cells per new input.  Every scenario is capped by the reduction computed on its own (``reduce_op(kind, w)``)."""
    alone = reduce_op(kind, w, p)
    cells = max(n_in - 1, 0)
    if cells == 0:
        return zero("logic")
    pre = "red_xor" if kind == "XOR" else "red_and"
    name = "XOR_IN" if kind == "XOR" else "AND_IN"
    lo = Acc().add(name, cells, p.C[f"{pre}_min"])
    ty = Acc().add(name, cells, p.C[f"{pre}_typ"]).scaled(1000 + p.alpha_add)   # chain + alpha * (2*chain - chain)
    hi = Acc().add(name, min(cells * clog2(w), w - 1), p.C[f"{pre}_max"])
    oc = finalize(Sc(lo, ty, hi, {"min": "chain", "typ": "prefix", "max": "kogge_stone"}), "logic", "good", p,
                  logic=kind != "XOR")
    return cap(oc, alone)


# ---------------------------------------------------------------------------------------
# selection

def mux_op(k: int, w: int, p: Profile, const_inputs: int = 0, same_inputs: bool = False,
           complement: bool = False) -> OpCost:
    """k:1 multiplexer of width w with a binary select.

    For k == 2, ``const_inputs`` (1 or 2 constant data inputs) turns the mux into AND/OR
    (resp. an inverter or wire) and ``same_inputs`` into a wire.
    """
    if k <= 1 or same_inputs:
        return zero("select")
    P = p.P
    if k == 2 and complement:       # s ? ~x : x is x ^ s
        ty = Acc().add("XOR2", w, P["XOR2"])
        return finalize(Sc(ty.copy(), ty, ty.scaled(p.sigma_other), {"min": "xor", "typ": "xor", "max": "xor"}, sigma=True),
                        "select", "good", p)
    if k == 2 and const_inputs == 1:
        return finalize(_all(Acc().add("AND2", w, P["AND2"]), "and_or"), "select", "good", p)
    if k == 2 and const_inputs >= 2:
        full = Acc().add("INV", w, P["INV"])
        return finalize(Sc(Acc(), full.scaled(500), full, {"min": "wire", "typ": "inv", "max": "inv"}), "select", "good", p)
    ty = Acc().add("MUX2", (k - 1) * w, P["MUX2"])
    lo = Acc().add("MUX4_EQ", (k - 1) * w, p.C["mux_min"]) if k >= 3 else ty.copy()
    hi = ty.scaled(p.sigma_other).add("BUF", clog2(k) * ceil_div(w, 8), P["BUF"])
    return finalize(Sc(lo, ty, hi, {"min": "mux4" if k >= 3 else "mux2", "typ": "mux2", "max": "mux2_buffered"}, sigma=True), "select", "good", p)


#: per-bit arm profile of a PMUX / PRIORITY: the kind of each data arm in priority order, then the default arm;
#: 0 / 1 = the arm is that constant at this bit, 2 = a variable signal no other arm of the bit carries,
#: 3, 4, ... = a variable signal carried by every arm of the bit with the same number (same net bit)
ArmBits = tuple[tuple[tuple[int, ...], int], ...]


def _all_var(k: int, w: int) -> ArmBits:
    return (((2,) * (k + 1), w),)


def _arm_bits(k: int, w: int, arms: ArmBits | None) -> ArmBits:
    """Validated arm profiles (``(kinds, bit count)`` pairs, ``k + 1`` kinds each, ``w`` bits in all)."""
    if arms is None:
        return _all_var(k, w)
    total = 0
    for kinds, n in arms:
        if len(kinds) != k + 1 or any(x < 0 for x in kinds) or n < 0:
            raise ValueError(f"arm profile {kinds!r} x{n} does not describe {k} arms and a default")
        total += n
    if total != w:
        raise ValueError(f"arm profiles cover {total} bits, expected {w}")
    return arms


def _same(x: int, y: int | None) -> bool:
    """Two arms that are the same signal at this bit (equal constants or the same shared variable)."""
    return y is not None and x == y and x != 2


def _pmux_accs(k: int, w: int, p: Profile, arms: ArmBits | None = None) -> tuple[Acc, Acc, Acc]:
    """k one-hot arms plus the default arm, per bit: each variable arm costs half an AOI22 (``AO_IN``: an AOI22
    serves two arms, an AOI222 three at about the same rate), and the ``h = ceil(v / 2)`` AOI outputs are merged by a
    NAND/NOR tree at the per-merged-input cost of the comparator chains (``eq_chain_min`` / ``eq_chain_typ``).

    Per bit (``arms``): an arm equal to the default is dropped (when it is selected the default is the output);
    arms that carry the same signal share one AOI input, their selects ORed once for the whole node (one merged
    input per extra arm, counted once per distinct set of arms); ``s & 0`` vanishes; ``s & 1`` is the select line
    itself, one more input of the merging tree and no AOI input; a bit with a single variable arm and nothing else
    is ``sel & x``, an AND2.  With every arm a distinct variable this is ``(k + 1)`` arm inputs and ``h - 1``
    merges.  max is the mux chain (``_chain``)."""
    P = p.P
    arm = rdiv(P["AOI22"], 2)
    ao = merge = and1 = 0
    groups: set[tuple[int, ...]] = set()
    for kinds, n in _arm_bits(k, w, arms):
        d = kinds[k]
        rem = [(j, x) for j, x in enumerate(kinds[:k]) if not _same(x, d)]
        shared: dict[int, list[int]] = {}
        for j, x in rem:
            if x >= 3:
                shared.setdefault(x, []).append(j)
        v = sum(1 for _, x in rem if x == 2) + len(shared) + (1 if d >= 2 else 0)
        ones = sum(1 for _, x in rem if x == 1) + (1 if d == 1 else 0)
        for js in shared.values():
            if len(js) > 1:
                groups.add(tuple(js))
        if v == 1 and ones == 0:      # a single variable arm, every other one 0: sel & x
            and1 += n
            continue
        ao += v * n
        merge += max(ceil_div(v, 2) + ones - 1, 0) * n
    sel = sum(len(g) - 1 for g in groups)
    lo = Acc().add("AO_IN", ao, arm).add("OR_IN", merge + sel, p.C["eq_chain_min"]).add("AND2", and1, P["AND2"])
    ty = Acc().add("AO_IN", ao, arm).add("OR_IN", merge + sel, p.C["eq_chain_typ"]).add("AND2", and1, P["AND2"])
    return lo, ty, _chain(k, w, p, arms)


def _chain(k: int, w: int, p: Profile, arms: ArmBits | None = None) -> Acc:
    """Per bit, a chain of ``k`` 2:1 muxes ``s_j ? d_j : rest`` ending on the default arm.  An arm equal to the rest
    of the chain (``s ? x : x``) is a wire; an arm equal to the arm of the mux just below it merges into that mux
    (``s1 ? x : (s2 ? x : r)`` is ``(s1 | s2) ? x : r``, the OR is on the 1-bit selects).  A mux with one constant
    input is an AND2/OR2, with two different constants the select or its complement (half an INV, as for MUX)."""
    P = p.P
    mux = andg = inv = 0
    for kinds, n in _arm_bits(k, w, arms):
        rest: int | None = kinds[k]
        head: int | None = None
        for d in reversed(kinds[:k]):
            if _same(d, rest) or _same(d, head):
                continue
            if d < 2 and rest is not None and rest < 2:
                inv += n
            elif d < 2 or (rest is not None and rest < 2):
                andg += n
            else:
                mux += n
            rest, head = None, d
    return Acc().add("MUX2", mux, P["MUX2"]).add("AND2", andg, P["AND2"]).add("INV", inv, rdiv(P["INV"], 2))


def pmux_op(k: int, w: int, p: Profile, arms: ArmBits | None = None) -> OpCost:
    """k one-hot data inputs and a default (AOI22 pairs and a NAND tree); ``arms``: per-bit constant and shared arms."""
    if k <= 1:
        return finalize(_all(Acc().add("AND2", w, p.P["AND2"]), "and"), "select", "medium", p)
    lo, ty, hi = _pmux_accs(k, w, p, arms)
    return finalize(Sc(lo, ty, hi, {"min": "aoi22", "typ": "aoi22", "max": "mux_chain"}), "select", "medium", p)


def priority_op(k: int, w: int, p: Profile, arms: ArmBits | None = None) -> OpCost:
    """A priority chain of k inputs (if / else-if); ``arms``: per-bit constant and shared arms."""
    P = p.P
    if k <= 1:
        return pmux_op(1, w, p)
    chain = _chain(k, w, p, arms)
    lo = _pmux_accs(k, w, p, arms)[0].add("AND2", k - 1, P["AND2"])
    ty = chain.scaled(p.F["priority_typ"]).add("AND2", k - 1, P["AND2"])
    hi = chain.copy().add("AND2", k - 1, P["AND2"])
    return finalize(Sc(lo, ty, hi, {"min": "pmux+chain", "typ": "mux_chain", "max": "mux_chain"}), "select", "medium", p)


#: per-bit leaf profile of a flattened select tree: ``(V, c0, c1, dflt, bit count)``: ``V`` distinct variable leaves,
#: ``c0`` / ``c1`` (0 or 1) whether the constants 0 / 1 are leaves, ``dflt`` the default leaf (2 variable, 0 / 1 that
#: constant); see ``gatecost.ir.muxtree``
TreeBits = tuple[tuple[int, int, int, int, int], ...]


def _tree_arms(bits: TreeBits) -> tuple[int, int, ArmBits]:
    """(arms k, width w, arm profiles) of a flattened tree seen as one select over its distinct leaves per bit: the
    default leaf innermost, then the other variable leaves, then the constant leaves; bits with fewer leaves than
    the widest are padded with copies of their default (a wire)."""
    rows = []
    k = 0
    for v, c0, c1, d, n in bits:
        if min(v, c0, c1, n) < 0 or max(c0, c1) > 1 or d not in (0, 1, 2) or (d == 2 and v < 1) \
                or (d == 0 and not c0) or (d == 1 and not c1):
            raise ValueError(f"tree leaf profile {(v, c0, c1, d, n)!r} is inconsistent")
        if n == 0:
            continue
        dk = 3 if d == 2 else d                       # the default variable: a class of its own (padding = wire)
        consts = [c for c, has in ((0, c0), (1, c1)) if has and c != d]
        arms = consts + [2] * (v - (1 if d == 2 else 0))
        rows.append((arms, dk, n))
        k = max(k, len(arms))
    w = sum(n for _, _, n in rows)
    prof = tuple((tuple(arms + [dk] * (k - len(arms)) + [dk]), n) for arms, dk, n in rows)
    return k, w, prof


def mux_tree_op(bits: TreeBits, sel_and: int, sel_or: int, p: Profile) -> OpCost:
    """A tree of nested 2:1 / one-hot / priority muxes flattened per bit (``c1 ? (c2 ? a : h) : h`` is
    ``(c1 & c2) ? a : h``): per bit a select of its *distinct* leaves, whose conditions are exclusive and built once
    for every bit by ``sel_and`` AND2 and ``sel_or`` OR2 on 1-bit signals.

    typ: the mux chain of the distinct leaves (``_chain``: ``V - 1`` MUX2 per bit, an AND2 per constant leaf);
    min: the AND-OR form of a one-hot select (``_pmux_accs``: the leaf conditions are exclusive); max: the chain with
    the sizing factor and select buffering of ``mux_op``.  The caller caps every scenario by the unflattened cost."""
    P = p.P
    k, w, arms = _tree_arms(bits)
    if k == 0 or w == 0:
        lo, ty = Acc(), Acc()
    else:
        lo = _pmux_accs(k, w, p, arms)[0]
        ty = _chain(k, w, p, arms)
    for a in (lo, ty):
        a.add("AND2", sel_and, P["AND2"]).add("OR2", sel_or, P["OR2"])
    hi = ty.scaled(p.sigma_other).add("BUF", clog2(k + 1) * ceil_div(w, 8) if k else 0, P["BUF"])
    return finalize(Sc(lo, ty, hi, {"min": "tree_aoi", "typ": "tree_mux_chain", "max": "tree_mux_chain"}, sigma=True),
                    "select", "medium", p)


def decode_acc(s: int, p: Profile) -> Sc:
    """Scenarios of an s -> 2^s decoder (pre-finalization), s >= 2."""
    P, C = p.P, p.C
    outs = 1 << s
    pre = (1 << (s // 2)) + (1 << (s - s // 2))
    lo = Acc().add("NOR2", outs, P["NOR2"]).add("PREDECODE", pre, C["predecode"])
    ty = Acc().add("AND2", outs, P["AND2"]).add("PREDECODE", pre, C["predecode"])
    hi = Acc().add("AND2", outs * (s - 1), P["AND2"])
    arch = {"min": "predecode_nor", "typ": "predecode_and", "max": "and_tree"}
    if s <= 4:
        # direct form: one s-input NOR per output on true/complemented address bits (s inverters), no predecoder
        g = ("NOR2", "NOR3", "NOR4")[s - 2]
        direct = Acc().add(g, outs, P[g]).add("INV", s, P["INV"])
        if direct.total < lo.total:
            lo, arch["min"] = direct, "direct_nor"
        if direct.total < ty.total:
            ty, arch["typ"] = direct.copy(), "direct_nor"
    return Sc(lo, ty, hi, arch)


def decode_op(s: int, p: Profile) -> OpCost:
    if s <= 0:
        return zero("select", "medium")
    if s == 1:
        return finalize(_all(Acc().add("INV", 1, p.P["INV"]), "inv"), "select", "medium", p)
    return finalize(decode_acc(s, p), "select", "medium", p)


def encode_op(k: int, p: Profile, priority: bool = False) -> OpCost:
    """k one-hot (or priority) inputs to a binary code of clog2(k) bits."""
    if k <= 1:
        return zero("logic", "medium")
    b = clog2(k)
    n_or = b * (ceil_div(k, 2) - 1)
    lo = Acc().add("OR_IN", n_or, p.C["pmux_or_min"])
    ty = Acc().add("OR2", n_or, p.P["OR2"])
    hi = Acc().add("OR2", n_or, p.P["OR2"])
    if priority:
        for a in (lo, ty, hi):
            a.add("AND2", k - 1, p.P["AND2"])
    return finalize(Sc(lo, ty, hi, {"min": "or_tree", "typ": "or_tree", "max": "or_tree"}), "select", "medium", p)


# ---------------------------------------------------------------------------------------
# shifters

SHIFTS = ("SHL", "SHR", "SAR", "ROL", "ROR")


def _wide_shr_levels(n: int, wa: int, tz: int, L: int) -> tuple[int, int]:
    """(MUX2, AND2) of a right shift of a ``wa``-bit word to ``n`` output bits by multiples of ``2^tz``.

    Levels ``j = tz .. L-1`` (shift by ``2^j``), the smallest shift next to the output: the output level needs ``n``
    bits and every level towards the input ``2^j`` more (at most ``wa``).  A bit whose shifted-in source lies above
    the word is an AND2 (zero fill), every other bit a MUX2.  With ``wa == n`` and ``tz == 0`` this is the usual
    ``sum_j (n - 2^j) MUX2 + 2^j AND2``; ``d[s*W +: W]`` (``wa = k*W``, ``tz = log2 W``) is ``(k-1)*W`` MUX2."""
    mux = andg = 0
    need = min(n, wa)
    for j in range(tz, L):
        sh = 1 << j
        a = max(0, need - max(0, wa - sh))
        mux += need - a
        andg += a
        need = min(wa, need + sh)
    return mux, andg


def shift_op(kind: str, n: int, s: int, p: Profile, const_amount: bool = False, lhs_one: bool = False,
             wa: int | None = None, tz: int = 0) -> OpCost:
    """Barrel shifter of n output bits and an s-bit amount.

    A constant amount is wiring (0); ``1 << x`` (``lhs_one`` on SHL) is a decoder.  ``wa`` is the data width when
    a right shift selects ``n`` bits out of a wider word (default ``n``); ``tz`` known-zero low amount bits remove
    the corresponding levels (the shift is by multiples of ``2^tz``).
    """
    if const_amount:
        return zero("shift", "medium")
    if lhs_one and kind == "SHL":
        oc = decode_op(s, p)
        return OpCost(oc.min, oc.typ, oc.max, oc.arch, oc.breakdown, "shift", "medium", oc.flags)
    wa = n if wa is None or wa < n or kind not in ("SHR", "SAR") else wa
    L = clog2(wa)
    Ls = min(s, L)
    tz = max(0, min(tz, Ls))
    P = p.P
    ty = Acc()
    if kind in ("SHL", "SHR"):
        if kind == "SHR":
            mux, andg = _wide_shr_levels(n, wa, tz, Ls)
        else:
            mux = sum(n - (1 << j) for j in range(tz, Ls))
            andg = sum(1 << j for j in range(tz, Ls))
        ty.add("MUX2", mux, P["MUX2"]).add("AND2", andg, P["AND2"])
        if s > L:
            ty.add("OR2", s - L - 1, P["OR2"]).add("AND2", n, P["AND2"])
    else:
        if kind == "SAR" and wa > n:  # sign fill: every selected bit is a MUX2
            mux, andg = _wide_shr_levels(n, wa, tz, Ls)
            ty.add("MUX2", mux + andg, P["MUX2"])
        else:
            ty.add("MUX2", n * (Ls - tz), P["MUX2"])
        if kind == "SAR" and s > L:  # an excess amount bit forces the sign into every output bit
            ty.add("OR2", s - L - 1, P["OR2"]).add("MUX2", n, P["MUX2"])
    Ls = Ls - tz
    lo = ty.scaled(p.F["shift_min"])
    hi = ty.scaled(p.sigma_other).add("BUF", Ls * ceil_div(n, 8), P["BUF"])
    return finalize(Sc(lo, ty, hi, {"min": "barrel", "typ": "barrel", "max": "barrel_buffered"}, sigma=True), "shift", "medium", p)
