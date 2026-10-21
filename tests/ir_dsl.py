"""Test-only DSL for building IR by hand (no HDL parsing).

    m = Mod("alu")
    a = m.inp("a", 8); b = m.inp("b", 8)
    m.out("y", (a + b).trunc(8))
    ir = m.done()           # finalize + analyze
"""
from __future__ import annotations

from gatecost.ir import (
    Builder, Const, LowerBlock, Origin, SigSpec, analyze_ir, dump,
)


class E:
    """Expression wrapper: a SigSpec with its builder; Verilog-ish self-determined widths."""

    def __init__(self, mod: "Mod", sig: SigSpec, signed: bool = False) -> None:
        self.m = mod
        self.s = sig
        self.signed = signed

    @property
    def w(self) -> int:
        return self.s.width

    def _o(self, o) -> "E":
        if isinstance(o, E):
            return o
        if isinstance(o, int):
            return self.m.c(o, max(self.w, o.bit_length() or 1))
        raise TypeError(o)

    def _bin(self, op, o, width=None, **kw) -> "E":
        o = self._o(o)
        w = max(self.w, o.w) if width is None else width
        sg = self.signed and o.signed
        return E(self.m, self.m.b.op(op, self.s, o.s, width=w, sa=self.signed, sb=o.signed, **kw), sg)

    def __add__(self, o): return self._bin("ADD", o)
    def __radd__(self, o): return self._o(o)._bin("ADD", self)
    def __sub__(self, o): return self._bin("SUB", o)
    def __mul__(self, o): return self._bin("MUL", o)
    def __floordiv__(self, o): return self._bin("DIV", o)
    def __mod__(self, o): return self._bin("MOD", o)
    def __and__(self, o): return self._bin("AND", o)
    def __or__(self, o): return self._bin("OR", o)
    def __xor__(self, o): return self._bin("XOR", o)
    def __lshift__(self, o): return E(self.m, self.m.b.shl(self.s, self._o(o).s, self.w, sa=self.signed), self.signed)
    def __rshift__(self, o): return E(self.m, self.m.b.shr(self.s, self._o(o).s, self.w, sa=self.signed), self.signed)
    def __invert__(self): return E(self.m, self.m.b.not_(self.s), self.signed)
    def __neg__(self): return E(self.m, self.m.b.neg(self.s, self.w, sa=self.signed), self.signed)

    def __eq__(self, o): return self._cmp("EQ", o)       # type: ignore[override]
    def __ne__(self, o): return self._cmp("NE", o)       # type: ignore[override]
    def __lt__(self, o): return self._cmp("LT", o)
    def __le__(self, o): return self._cmp("LE", o)
    def __gt__(self, o): return self._cmp("GT", o)
    def __ge__(self, o): return self._cmp("GE", o)
    __hash__ = None  # type: ignore[assignment]

    def _cmp(self, op, o) -> "E":
        o = self._o(o)
        return E(self.m, self.m.b.op(op, self.s, o.s, width=1, sa=self.signed, sb=o.signed))

    def add(self, o, width, **kw): return E(self.m, self.m.b.add(self.s, self._o(o).s, width, sa=self.signed, sb=self._o(o).signed, **kw))
    def sub(self, o, width, **kw): return E(self.m, self.m.b.sub(self.s, self._o(o).s, width, sa=self.signed, sb=self._o(o).signed, **kw))
    def mul(self, o, width, **kw): return E(self.m, self.m.b.mul(self.s, self._o(o).s, width, sa=self.signed, sb=self._o(o).signed, **kw))

    def __getitem__(self, idx) -> "E":
        return E(self.m, self.s[idx])

    def zext(self, w) -> "E": return E(self.m, self.s.zext(w))
    def sext(self, w) -> "E": return E(self.m, self.s.sext(w), True)
    def trunc(self, w) -> "E": return E(self.m, self.s.trunc(w), self.signed)
    def sg(self, signed=True) -> "E": return E(self.m, self.s, signed)

    def red_or(self) -> "E": return E(self.m, self.m.b.red_or(self.s))
    def red_and(self) -> "E": return E(self.m, self.m.b.red_and(self.s))
    def red_xor(self) -> "E": return E(self.m, self.m.b.red_xor(self.s))
    def mux(self, a, b) -> "E":
        """self ? b : a"""
        a, b = self._o(a), self._o(b)
        w = max(a.w, b.w)
        return E(self.m, self.m.b.mux(self.s, a.s.zext(w), b.s.zext(w), width=w))


def cat(*es: E) -> E:
    """Verilog concatenation: the first argument is the most significant."""
    m = es[0].m
    return E(m, SigSpec.concat([e.s for e in reversed(es)]))


class Mod:
    def __init__(self, name: str = "t", params=None, const_ports=None, **kw) -> None:
        self.b = Builder(name, params, const_ports, **kw)
        self.ports: dict[str, E] = {}

    def inp(self, name: str, w: int, signed: bool = False) -> E:
        n = self.b.port(name, "in", w, signed)
        e = E(self, n.sig, signed)
        self.ports[name] = e
        return e

    def out(self, name: str, e: E | None = None, w: int | None = None) -> E:
        width = w or (e.w if e is not None else 1)
        n = self.b.port(name, "out", width)
        if e is not None:
            self.b.connect(n, e.s.resize(width))
        r = E(self, n.sig)
        self.ports[name] = r
        return r

    def c(self, v: int, w: int, x: int = 0) -> E:
        return E(self, SigSpec.const(v, w, x))

    def wire(self, name: str, w: int) -> E:
        return E(self, self.b.net(w, name).sig)

    def done(self, analyze: bool = True, **kw):
        m = self.b.finalize()
        if analyze:
            analyze_ir(m, **kw)
        return m


def nodes_of(m, op=None):
    return [n for n in m.nodes if op is None or n.op == op]


def ops_of(m):
    return sorted(n.op for n in m.nodes)
