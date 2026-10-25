"""Comparators, bitwise ops, reductions, selection, decoding and shifters (COST_MODEL.md 3.2-3.4)."""
from __future__ import annotations

from .acc import Acc, OpCost, Sc, cap, finalize, zero
from .profile import Profile
from .units import ceil_div, clog2, pm


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
    return finalize(_all(Acc().add(g, w, p.P[g]), "gate"), "logic", "good", p, logic=True)


def reduce_op(kind: str, n: int, p: Profile) -> OpCost:
    """RED_AND / RED_OR (also used for compares against zero) and RED_XOR."""
    if n == 1:
        return zero("logic")
    pre = "red_xor" if kind == "XOR" else "red_and"
    name = "XOR_IN" if kind == "XOR" else "AND_IN"
    lo, ty, hi = (Acc().add(name, n - 1, p.C[f"{pre}_{s}"]) for s in ("min", "typ", "max"))
    return finalize(Sc(lo, ty, hi, {"min": "tree", "typ": "tree", "max": "tree"}), "logic", "good", p, logic=True)


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


def _pmux_accs(k: int, w: int, p: Profile) -> tuple[Acc, Acc, Acc]:
    P = p.P
    h = ceil_div(k, 2)
    lo = Acc().add("AO22", h * w, P["AO22"]).add("OR_IN", (h - 1) * w, p.C["pmux_or_min"])
    ty = Acc().add("AO22", h * w, P["AO22"]).add("OR2", (h - 1) * w, P["OR2"])
    hi = Acc().add("MUX2", k * w, P["MUX2"])
    return lo, ty, hi


def pmux_op(k: int, w: int, p: Profile) -> OpCost:
    """k data inputs with a one-hot select (AO22 tree)."""
    if k <= 1:
        return finalize(_all(Acc().add("AND2", w, p.P["AND2"]), "and"), "select", "medium", p)
    lo, ty, hi = _pmux_accs(k, w, p)
    return finalize(Sc(lo, ty, hi, {"min": "ao22", "typ": "ao22", "max": "mux_chain"}), "select", "medium", p)


def priority_op(k: int, w: int, p: Profile) -> OpCost:
    """A priority chain of k inputs (if / else-if)."""
    P = p.P
    if k <= 1:
        return pmux_op(1, w, p)
    lo = _pmux_accs(k, w, p)[0].add("AND2", k - 1, P["AND2"])
    ty = Acc().add("MUX2", k * w, P["MUX2"]).scaled(p.F["priority_typ"]).add("AND2", k - 1, P["AND2"])
    hi = Acc().add("MUX2", k * w, P["MUX2"]).add("AND2", k - 1, P["AND2"])
    return finalize(Sc(lo, ty, hi, {"min": "pmux+chain", "typ": "mux_chain", "max": "mux_chain"}), "select", "medium", p)


def decode_acc(s: int, p: Profile) -> Sc:
    """Scenarios of an s -> 2^s decoder (pre-finalization), s >= 2."""
    P, C = p.P, p.C
    outs = 1 << s
    pre = (1 << (s // 2)) + (1 << (s - s // 2))
    lo = Acc().add("NOR2", outs, P["NOR2"]).add("PREDECODE", pre, C["predecode"])
    ty = Acc().add("AND2", outs, P["AND2"]).add("PREDECODE", pre, C["predecode"])
    hi = Acc().add("AND2", outs * (s - 1), P["AND2"])
    return Sc(lo, ty, hi, {"min": "predecode_nor", "typ": "predecode_and", "max": "and_tree"})


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


def shift_op(kind: str, n: int, s: int, p: Profile, const_amount: bool = False, lhs_one: bool = False) -> OpCost:
    """Barrel shifter of n data bits and an s-bit amount.

    A constant amount is wiring (0); ``1 << x`` (``lhs_one`` on SHL) is a decoder.
    """
    if const_amount:
        return zero("shift", "medium")
    if lhs_one and kind == "SHL":
        oc = decode_op(s, p)
        return OpCost(oc.min, oc.typ, oc.max, oc.arch, oc.breakdown, "shift", "medium", oc.flags)
    L = clog2(n)
    Ls = min(s, L)
    P = p.P
    ty = Acc()
    if kind in ("SHL", "SHR"):
        for j in range(Ls):
            ty.add("MUX2", n - (1 << j), P["MUX2"]).add("AND2", 1 << j, P["AND2"])
        if s > L:
            ty.add("OR2", s - L - 1, P["OR2"]).add("AND2", n, P["AND2"])
    else:
        ty.add("MUX2", n * Ls, P["MUX2"])
        if kind == "SAR" and s > L:  # an excess amount bit forces the sign into every output bit
            ty.add("OR2", s - L - 1, P["OR2"]).add("MUX2", n, P["MUX2"])
    lo = ty.scaled(p.F["shift_min"])
    hi = ty.scaled(p.sigma_other).add("BUF", Ls * ceil_div(n, 8), P["BUF"])
    return finalize(Sc(lo, ty, hi, {"min": "barrel", "typ": "barrel", "max": "barrel_buffered"}, sigma=True), "shift", "medium", p)
