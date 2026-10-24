"""VHDL type model for the elaborator: scalar, enum, integer, array and record types with widths."""
from __future__ import annotations

from ...ir import Const, SigSpec


class VType:
    """One type or subtype.  ``kind``: enum | int | real | array | record | other.

    enum: ``lits`` (names, character literals keep quotes), ``enc`` (value, xmask) per literal, ``width``.
    int: ``low``/``high`` (None = universal), ``width``, ``signed``.
    array: ``elem``, ``left``/``right``/``dir`` (None when unconstrained), ``length``, ``ewidth``, ``width``.
    record: ``fields`` [(name, VType)], ``offs`` {name: (offset, width)}, ``width``.
    ``cls`` is the IEEE type class of ieee.py (u, s, slv, suv, bv, sl, bit, int, nat, bool, real, str) or None.
    """

    __slots__ = ("kind", "name", "cls", "width", "signed", "lits", "enc", "onehot", "low", "high", "left", "right",
                 "dir", "elem", "fields", "offs", "length", "ewidth", "index")

    def __init__(self, kind, name="", cls=None, **kw):
        self.kind = kind
        self.name = name
        self.cls = cls
        self.width = None
        self.signed = False
        self.lits = None
        self.enc = None
        self.onehot = False
        self.low = self.high = None
        self.left = self.right = self.dir = None
        self.elem = None
        self.fields = None
        self.offs = None
        self.length = None
        self.ewidth = None
        self.index = None
        for k, v in kw.items():
            setattr(self, k, v)

    def __repr__(self):
        return "VType(%s %s w=%s)" % (self.kind, self.name, self.width)

    def copy(self, **kw):
        n = VType(self.kind, self.name, self.cls)
        for s in VType.__slots__:
            setattr(n, s, getattr(self, s))
        for k, v in kw.items():
            setattr(n, k, v)
        return n

    @property
    def is_vec(self):
        return self.kind == "array"

    @property
    def is_scalar_bit(self):
        return self.kind == "enum" and self.width == 1


def minbits(lo, hi):
    """(width, signed) of the smallest two's complement / unsigned field holding [lo, hi]."""
    if lo >= 0:
        return max(1, hi.bit_length()), False
    return max(hi.bit_length(), (-lo - 1).bit_length()) + 1, True


ENC_SL = {"'U'": (0, 1), "'X'": (0, 1), "'0'": (0, 0), "'1'": (1, 0), "'Z'": (0, 1), "'W'": (0, 1),
          "'L'": (0, 0), "'H'": (1, 0), "'-'": (0, 1)}
SL_LITS = ["'U'", "'X'", "'0'", "'1'", "'Z'", "'W'", "'L'", "'H'", "'-'"]


def enum_type(name, lits, cls=None, onehot=False):
    n = len(lits)
    if onehot and n > 2:
        enc = [(1 << i, 0) for i in range(n)]
        w = n
    else:
        onehot = False
        enc = [(i, 0) for i in range(n)]
        w = max(1, (n - 1).bit_length())
    return VType("enum", name, cls, width=w, lits=list(lits), enc=enc, onehot=onehot)


def int_type(name, low, high, cls=None):
    if low is None:
        return VType("int", name, cls or "int", width=None, signed=True)
    w, s = minbits(low, high)
    return VType("int", name, cls or ("nat" if low >= 0 else "int"), low=low, high=high, width=w, signed=s)


def array_type(name, elem, left=None, right=None, dir=None, cls=None, index=None):
    t = VType("array", name, cls, elem=elem, index=index, ewidth=elem.width)
    if left is not None:
        _set_range(t, left, right, dir)
    return t


def _set_range(t, left, right, dir):
    t.left, t.right, t.dir = left, right, dir
    t.length = max(0, (left - right + 1) if dir == "downto" else (right - left + 1))
    t.width = t.length * t.ewidth if t.ewidth is not None else None


def constrain(t, left, right, dir):
    n = t.copy()
    n.ewidth = t.elem.width
    _set_range(n, left, right, dir)
    return n


def record_type(name, fields):
    """A record; ``width`` is None while any field is an unconstrained array (see ``unconstrained_paths``)."""
    offs = {}
    off = 0
    unconstrained = False
    for fname, ft in fields:
        offs[fname] = (off, ft.width or 0)
        off += ft.width or 0
        if ft.width is None:
            unconstrained = True
    return VType("record", name, None, fields=list(fields), offs=offs, width=None if unconstrained else off)


def unconstrained_paths(t, prefix=""):
    """[(dotted field path, field type)] of the unconstrained array fields of record ``t`` (nested records too)."""
    out = []
    if t.kind != "record":
        return out
    for fname, ft in t.fields:
        path = prefix + fname
        if ft.kind == "record":
            out.extend(unconstrained_paths(ft, path + "."))
        elif ft.kind == "array" and ft.width is None:
            out.append((path, ft))
    return out


def field_at(t, path):
    """Type of the field ``a.b.c`` of record ``t`` (None when absent)."""
    for part in path.split("."):
        if t is None or t.kind != "record":
            return None
        t = dict(t.fields).get(part)
    return t


def constrain_fields(t, new, prefix=""):
    """Copy of record ``t`` with the fields of ``new`` ({dotted path: constrained type}) replaced."""
    fields = []
    for fname, ft in t.fields:
        path = prefix + fname
        if path in new:
            ft = new[path]
        elif ft.kind == "record":
            ft = constrain_fields(ft, new, path + ".")
        fields.append((fname, ft))
    r = record_type(t.name, fields)
    r.cls = t.cls
    return r


T_BOOLEAN = enum_type("boolean", ["false", "true"], "bool")
T_BIT = enum_type("bit", ["'0'", "'1'"], "bit")
T_STD_LOGIC = VType("enum", "std_logic", "sl", width=1, lits=list(SL_LITS), enc=[ENC_SL[l] for l in SL_LITS])
T_STD_ULOGIC = T_STD_LOGIC.copy(name="std_ulogic")
T_CHARACTER = VType("enum", "character", "str", width=8, lits=None, enc=None)
T_INTEGER = int_type("integer", -2 ** 31, 2 ** 31 - 1, "int")
T_NATURAL = int_type("natural", 0, 2 ** 31 - 1, "nat")
T_POSITIVE = int_type("positive", 1, 2 ** 31 - 1, "nat")
T_UINT = int_type("universal_integer", None, None)
T_REAL = VType("real", "real", "real")
T_TIME = int_type("time", -2 ** 63, 2 ** 63 - 1, "time")      # integer count of femtoseconds
T_SLV = array_type("std_logic_vector", T_STD_LOGIC, cls="slv")
T_SUV = array_type("std_ulogic_vector", T_STD_LOGIC, cls="suv")
T_UNSIGNED = array_type("unsigned", T_STD_LOGIC, cls="u")
T_SIGNED = array_type("signed", T_STD_LOGIC, cls="s")
T_BV = array_type("bit_vector", T_BIT, cls="bv")
T_STRING = array_type("string", T_CHARACTER, cls="str")
T_UNKNOWN = VType("other", "unknown")

BUILTIN_TYPES = {
    "boolean": T_BOOLEAN, "bit": T_BIT, "std_logic": T_STD_LOGIC, "std_ulogic": T_STD_ULOGIC,
    "x01": T_STD_LOGIC, "x01z": T_STD_LOGIC, "ux01": T_STD_LOGIC, "ux01z": T_STD_LOGIC,
    "character": T_CHARACTER, "integer": T_INTEGER, "natural": T_NATURAL, "positive": T_POSITIVE,
    "real": T_REAL, "time": T_TIME, "std_logic_vector": T_SLV, "std_ulogic_vector": T_SUV,
    "unsigned": T_UNSIGNED, "signed": T_SIGNED, "bit_vector": T_BV, "string": T_STRING,
    "severity_level": enum_type("severity_level", ["note", "warning", "error", "failure"]),
}
_BASE_BY_CLS = {"u": T_UNSIGNED, "s": T_SIGNED, "slv": T_SLV, "suv": T_SUV, "bv": T_BV}


def vec_type(cls, width, like=None):
    """Constrained (width-1 downto 0) vector of class ``cls``."""
    base = _BASE_BY_CLS.get(cls, T_SLV)
    return constrain(base, width - 1, 0, "downto")


def int_range_type(lo, hi):
    return int_type("", lo, hi)


def range_of(t):
    """(left, right, dir) of a discrete type / constrained array, or None."""
    if t.kind == "int" and t.low is not None:
        return t.low, t.high, "to"
    if t.kind == "enum" and t.lits:
        return 0, len(t.lits) - 1, "to"
    if t.kind == "enum" and t.cls == "bool":
        return 0, 1, "to"
    if t.kind == "array" and t.left is not None:
        return t.left, t.right, t.dir
    return None


def pos_to_index(t, p):
    """Index value of array element at position ``p`` counted from the rightmost element (bit 0)."""
    return t.right + p if t.dir == "downto" else t.right - p


def index_to_pos(t, i):
    return i - t.right if t.dir == "downto" else t.right - i


def const_bits(value, width):
    return SigSpec((Const(value, width),))
