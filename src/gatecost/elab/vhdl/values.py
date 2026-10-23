"""Values, symbols and scopes of the VHDL elaborator."""
from __future__ import annotations

from ...ir import Const, SigSpec
from .vtypes import VType, minbits


class Val:
    """An elaborated expression value.

    ``t`` type; ``sig`` bits (LSB first) or None for a pure Python constant; ``c`` Python constant
    (int / float / enum position) when known; ``z`` mask of high-impedance bits.
    """

    __slots__ = ("t", "sig", "c", "z")

    def __init__(self, t, sig=None, c=None, z=0):
        self.t = t
        self.sig = sig
        self.c = c
        self.z = z

    def __repr__(self):
        return "Val(%r, c=%r, sig=%r)" % (self.t, self.c, self.sig)

    @property
    def is_const(self):
        return self.c is not None or (self.sig is not None and self.sig.const_value() is not None)


class Sym:
    """Symbol: kind in obj, const, type, func, comp, enumlit, alias, pkg, lib, mem, unit."""

    __slots__ = ("kind", "name", "t", "val", "sig", "node", "scope", "mode", "owner", "extra", "pos")

    def __init__(self, kind, name, t=None, **kw):
        self.kind = kind
        self.name = name
        self.t = t
        self.val = self.sig = self.node = self.scope = self.mode = self.owner = self.extra = self.pos = None
        for k, v in kw.items():
            setattr(self, k, v)


class Scope:
    __slots__ = ("names", "parent", "uses", "pkgs", "file", "lib", "label")

    def __init__(self, parent=None, file=None, lib=None, label=""):
        self.names = {}
        self.parent = parent
        self.uses = []          # PkgEnv imported by use clauses
        self.pkgs = parent.pkgs if parent is not None else ["standard"]   # ieee package names in scope
        self.file = file if file is not None else (parent.file if parent else None)
        self.lib = lib if lib is not None else (parent.lib if parent else "work")
        self.label = label

    def declare(self, sym):
        self.names.setdefault(sym.name, []).append(sym)
        return sym

    def declare_unique(self, sym):
        self.names[sym.name] = [sym]
        return sym

    def lookup(self, name):
        """Candidate symbols for ``name`` (overloadable kinds accumulate)."""
        acc = []
        s = self
        while s is not None:
            d = s.names.get(name)
            if d:
                if all(x.kind in ("func", "enumlit") for x in d):
                    acc.extend(d)
                elif not acc:
                    return d
                else:
                    return acc
            s = s.parent
        s = self
        while s is not None:
            for p in s.uses:
                d = p.scope.names.get(name)
                if d:
                    if all(x.kind in ("func", "enumlit") for x in d):
                        acc.extend(d)
                    elif not acc:
                        return d
            s = s.parent
        return acc

    def lookup1(self, name):
        r = self.lookup(name)
        return r[0] if r else None


class FuncDef:
    """A user subprogram with its defining scope."""

    __slots__ = ("node", "scope", "body_node")

    def __init__(self, node, scope):
        self.node = node
        self.scope = scope
        self.body_node = node if node.has_body else None


class PkgEnv:
    __slots__ = ("name", "lib", "scope", "unit", "body", "loading", "loaded", "generics")

    def __init__(self, name, lib, unit):
        self.name = name
        self.lib = lib
        self.unit = unit
        self.scope = None
        self.body = None
        self.loading = False
        self.loaded = False
        self.generics = None


class LV:
    """Assignable target: ``sig`` (net-slice SigSpec) with optional dynamic index levels, or a memory word."""

    __slots__ = ("t", "sig", "dyn", "mem", "addr", "parts", "base", "off", "width", "sym")

    def __init__(self, t, sig=None, dyn=None, mem=None, addr=None, parts=None):
        self.t = t
        self.sig = sig
        self.dyn = dyn or []     # list of (pos_sig, stride_bits, count)
        self.mem = mem
        self.addr = addr
        self.parts = parts        # concatenation targets (MSB first)
        self.sym = None


def sig_const(v, w):
    return SigSpec.const(v, w)
