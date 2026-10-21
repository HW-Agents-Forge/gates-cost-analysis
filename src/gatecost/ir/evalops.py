"""Reference evaluation of IR operations on constants (used for constant folding).

This module defines the exact semantics of every foldable operation of IR.md
section 3.1; the builder, the known-bits pass and the tests all rely on it.
Don't-care bits (``xmask``) follow bitwise three-valued logic where that is
exact, and make the whole result don't-care otherwise.
"""
from __future__ import annotations

from .sigspec import Const, mask


def ext(c: Const, width: int, signed: bool) -> Const:
    """Extend (by sign or zero) or truncate a constant to ``width`` bits."""
    if width <= c.width:
        return Const(c.value, width, c.xmask)
    if signed and c.width > 0:
        top = (c.value >> (c.width - 1)) & 1
        topx = (c.xmask >> (c.width - 1)) & 1
        fill = mask(width) & ~mask(c.width)
        return Const(c.value | (fill if top else 0), width, c.xmask | (fill if topx else 0))
    return Const(c.value, width, c.xmask)


def to_signed(v: int, w: int) -> int:
    if w and (v >> (w - 1)) & 1:
        return v - (1 << w)
    return v


def allx(width: int) -> Const:
    return Const(0, width, mask(width))


def _isx(*cs: Const) -> bool:
    return any(c.xmask for c in cs)


def eval_const(op: str, ins: list[Const], attrs: dict, width: int) -> Const:
    """Evaluate ``op`` on constant operands, producing a ``width``-bit constant."""
    m = mask(width)
    f = _EVAL.get(op)
    if f is None:
        raise ValueError(f"cannot fold {op}")
    return f(ins, attrs, width, m)


def _add(ins, at, w, m):
    a, b = ins
    if _isx(a, b):
        return allx(w)
    a = ext(a, w, at.get("sa", False))
    b = ext(b, w, at.get("sb", False))
    return Const(a.value + b.value, w)


def _sub(ins, at, w, m):
    a, b = ins
    if _isx(a, b):
        return allx(w)
    a = ext(a, w, at.get("sa", False))
    b = ext(b, w, at.get("sb", False))
    return Const(a.value - b.value, w)


def _neg(ins, at, w, m):
    (a,) = ins
    if _isx(a):
        return allx(w)
    a = ext(a, w, at.get("sa", False))
    return Const(-a.value, w)


def _mul(ins, at, w, m):
    a, b = ins
    if _isx(a, b):
        return allx(w)
    a = ext(a, w, at.get("sa", False))
    b = ext(b, w, at.get("sb", False))
    return Const(a.value * b.value, w)


def _divmod(kind):
    def f(ins, at, w, m):
        a, b = ins
        if _isx(a, b):
            return allx(w)
        sa, sb = at.get("sa", False), at.get("sb", False)
        cw = max(a.width, b.width)
        a = ext(a, cw, sa)
        b = ext(b, cw, sb)
        if b.value == 0:
            return allx(w)
        if sa and sb:
            x, y = to_signed(a.value, cw), to_signed(b.value, cw)
            q = abs(x) // abs(y)
            if (x < 0) != (y < 0):
                q = -q
            r = x - q * y
            return Const(q if kind == "div" else r, w)
        return Const(a.value // b.value if kind == "div" else a.value % b.value, w)
    return f


def _pow(ins, at, w, m):
    (a,) = ins
    if _isx(a):
        return allx(w)
    a = ext(a, w, at.get("sa", False))
    return Const(pow(a.value, int(at["exp"]), 1 << w), w)


def _shift(kind):
    def f(ins, at, w, m):
        a, b = ins
        if b.xmask:
            return allx(w)
        a = ext(a, w, at.get("sa", False))
        n = b.value
        if kind == "shl":
            return Const(a.value << n if n < w else 0, w, (a.xmask << n) if n < w else 0)
        if kind == "shr":
            return Const(a.value >> n, w, a.xmask >> n)
        if kind == "sar":
            sv = to_signed(a.value | 0, w) >> min(n, w)
            sx = to_signed(a.xmask, w) >> min(n, w)
            return Const(sv, w, sx)
        if w == 0:
            return Const(0, 0)
        n %= w
        if kind == "ror":
            n = (w - n) % w
        # rol by n
        def rot(v):
            return ((v << n) | (v >> (w - n))) & m if n else v
        return Const(rot(a.value), w, rot(a.xmask))
    return f


def _bitwise(kind):
    def f(ins, at, w, m):
        a, b = ins
        a = Const(a.value, w, a.xmask) if a.width == w else ext(a, w, False)
        b = Const(b.value, w, b.xmask) if b.width == w else ext(b, w, False)
        ak, bk = a.kmask, b.kmask
        av, bv = a.value, b.value
        if kind == "and":
            zero = (ak & ~av) | (bk & ~bv)
            one = ak & bk & av & bv
            known = (zero | one) & m
            return Const(one & known, w, m & ~known)
        if kind == "or":
            one = (ak & av) | (bk & bv)
            zero = ak & bk & ~av & ~bv
            known = (zero | one) & m
            return Const(one & known, w, m & ~known)
        known = ak & bk
        v = (av ^ bv) & known
        if kind == "xnor":
            v = ~v & known
        return Const(v, w, m & ~known)
    return f


def _not(ins, at, w, m):
    (a,) = ins
    a = ext(a, w, False) if a.width != w else a
    return Const(~a.value & a.kmask, w, a.xmask)


def _red(kind):
    def f(ins, at, w, m):
        (a,) = ins
        k = a.kmask
        if kind == "and":
            if k & ~a.value:
                return Const(0, 1)
            if a.xmask:
                return allx(1)
            return Const(1, 1)
        if kind == "or":
            if a.value:
                return Const(1, 1)
            if a.xmask:
                return allx(1)
            return Const(0, 1)
        if a.xmask:
            return allx(1)
        return Const(bin(a.value).count("1") & 1, 1)
    return f


def _eqne(kind):
    def f(ins, at, w, m):
        a, b = ins
        cw = max(a.width, b.width)
        a = ext(a, cw, False)
        b = ext(b, cw, False)
        both = a.kmask & b.kmask
        if (a.value ^ b.value) & both:
            r = 0
        elif a.xmask or b.xmask:
            return allx(1)
        else:
            r = 1
        return Const(r if kind == "eq" else 1 - r, 1)
    return f


def _cmp(kind):
    def f(ins, at, w, m):
        a, b = ins
        if _isx(a, b):
            return allx(1)
        sa, sb = at.get("sa", False), at.get("sb", False)
        cw = max(a.width, b.width)
        a = ext(a, cw, sa)
        b = ext(b, cw, sb)
        if sa and sb:
            x, y = to_signed(a.value, cw), to_signed(b.value, cw)
        else:
            x, y = a.value, b.value
        r = {"lt": x < y, "le": x <= y, "gt": x > y, "ge": x >= y}[kind]
        return Const(int(r), 1)
    return f


def _mux(ins, at, w, m):
    a, b, s = ins
    if s.xmask:
        known = a.kmask & b.kmask & ~(a.value ^ b.value)
        return Const(a.value & known, w, m & ~known)
    return b if s.value else a


def _pmux(prio: bool):
    def f(ins, at, w, m):
        a, b, s = ins
        k = s.width
        if s.xmask:
            return allx(w)
        hits = [i for i in range(k) if (s.value >> i) & 1]
        if not hits:
            return a
        i = hits[0]
        if len(hits) > 1 and not prio:
            # undefined for one-hot select with several hits: fold as priority
            pass
        return Const(b.value >> (i * w), w, b.xmask >> (i * w))
    return f


def _decode(ins, at, w, m):
    (a,) = ins
    if a.xmask:
        return allx(w)
    return Const(1 << a.value if a.value < w else 0, w)


def _encode(ins, at, w, m):
    (a,) = ins
    if a.xmask:
        return allx(w)
    if a.value == 0:
        return Const(0, w)
    return Const((a.value & -a.value).bit_length() - 1, w)


def _rom(ins, at, w, m):
    (a,) = ins
    if a.xmask:
        return allx(w)
    contents = at["contents"]
    if a.value >= len(contents):
        return allx(w)
    return Const(contents[a.value], w)


_EVAL = {
    "ADD": _add, "SUB": _sub, "NEG": _neg, "MUL": _mul,
    "DIV": _divmod("div"), "MOD": _divmod("mod"), "POW": _pow,
    "SHL": _shift("shl"), "SHR": _shift("shr"), "SAR": _shift("sar"),
    "ROL": _shift("rol"), "ROR": _shift("ror"),
    "AND": _bitwise("and"), "OR": _bitwise("or"), "XOR": _bitwise("xor"),
    "XNOR": _bitwise("xnor"), "NOT": _not,
    "RED_AND": _red("and"), "RED_OR": _red("or"), "RED_XOR": _red("xor"),
    "EQ": _eqne("eq"), "NE": _eqne("ne"),
    "LT": _cmp("lt"), "LE": _cmp("le"), "GT": _cmp("gt"), "GE": _cmp("ge"),
    "MUX": _mux, "PMUX": _pmux(False), "PRIORITY": _pmux(True),
    "DECODE": _decode, "ENCODE": _encode, "ROM": _rom,
}

FOLDABLE = frozenset(_EVAL)
