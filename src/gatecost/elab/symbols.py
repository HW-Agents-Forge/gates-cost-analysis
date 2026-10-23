"""Types, values, symbols and scopes of the Verilog/SystemVerilog elaborator."""
from __future__ import annotations

from gatecost.ir import SigSpec


class PType:
    """Packed type.

    ``w`` total width, ``signed``, ``dims`` packed dimensions ``((left, right), ...)`` outermost
    first, ``base`` the element type of the innermost dimension (a 1-bit scalar for plain vectors),
    ``fields`` ``{name: (offset, PType)}`` for packed structs/unions, ``enum`` ``{name: int}``.
    """

    __slots__ = ("w", "signed", "dims", "base", "fields", "enum")

    def __init__(self, w, signed=False, dims=(), base=None, fields=None, enum=None):
        self.w = w
        self.signed = signed
        self.dims = dims
        self.base = base if base is not None else self
        self.fields = fields
        self.enum = enum

    def elem(self) -> "PType":
        """Type of ``x[i]`` for a packed dimension."""
        if len(self.dims) > 1:
            n = abs(self.dims[0][0] - self.dims[0][1]) + 1
            return PType(self.w // n, self.signed, self.dims[1:], self.base, self.fields)
        return self.base if self.dims else BIT

    def elem_width(self) -> int:
        if self.dims:
            n = abs(self.dims[0][0] - self.dims[0][1]) + 1
            return max(1, self.w // n)
        return 1

    def size0(self) -> int:
        if self.dims:
            return abs(self.dims[0][0] - self.dims[0][1]) + 1
        return self.w

    def describe(self) -> str:
        return f"{'s' if self.signed else 'u'}{self.w}{list(self.dims)}"


BIT = PType(1)


def vec(w: int, signed: bool = False) -> PType:
    return PType(w, signed, ((w - 1, 0),), BIT)


def dim_pos(i: int, dim) -> int:
    """Element position (0 = least significant element) of index ``i`` in dimension ``dim``."""
    l, r = dim
    return i - r if l >= r else r - i


class V:
    """A value: signal specification plus signedness."""

    __slots__ = ("sig", "signed")

    def __init__(self, sig: SigSpec, signed: bool = False):
        self.sig = sig
        self.signed = signed

    @property
    def w(self) -> int:
        return self.sig.width

    def __repr__(self):
        return f"V({self.sig!r}, signed={self.signed})"


class PSym:
    """Parameter / localparam / enum constant / genvar value."""

    __slots__ = ("val", "pt", "arr")
    kind = "param"

    def __init__(self, val: V, pt: PType | None = None, arr=None):
        self.val = val
        self.pt = pt
        self.arr = arr          # array parameter: list of constant element values (ints), else None


class TSym:
    __slots__ = ("pt",)
    kind = "type"

    def __init__(self, pt: PType):
        self.pt = pt


class VSym:
    """Signal (net / variable / port / array of them)."""

    __slots__ = ("name", "pt", "udims", "net", "elems", "mem", "direction", "is_port", "local",
                 "wired", "const_sig", "hier")
    kind = "var"

    def __init__(self, name, pt, udims=(), net=None):
        self.name = name
        self.pt = pt
        self.udims = udims
        self.net = net
        self.elems = None
        self.mem = None
        self.direction = None
        self.is_port = False
        self.local = False
        self.wired = None
        self.const_sig = None
        self.hier = ""


class FSym:
    __slots__ = ("ast", "scope", "rt")
    kind = "func"

    def __init__(self, ast, scope):
        self.ast = ast
        self.scope = scope
        self.rt = None


class KSym:
    """Generate block scope(s): ``scope`` for a plain block, ``arr`` ``{index: Scope}`` for loops."""

    __slots__ = ("scope", "arr")
    kind = "block"

    def __init__(self, scope=None, arr=None):
        self.scope = scope
        self.arr = arr


class ISym:
    """Interface instance flattened into member signals."""

    __slots__ = ("name", "members", "order", "iface", "scope", "pkeys")
    kind = "iface"

    def __init__(self, name, iface, scope):
        self.name = name
        self.iface = iface
        self.scope = scope
        self.members = {}
        self.order = []
        self.pkeys = {}


class Scope:
    __slots__ = ("parent", "syms", "wild", "path", "name")

    def __init__(self, parent=None, path: str = "", name: str = ""):
        self.parent = parent
        self.syms = {}
        self.wild = []
        self.path = path
        self.name = name

    def lookup(self, name):
        s = self
        while s is not None:
            r = s.syms.get(name)
            if r is not None:
                return r
            for pk in s.wild:
                r = pk.syms.get(name)
                if r is not None:
                    return r
            s = s.parent
        return None

    def lookup_local(self, name):
        return self.syms.get(name)
