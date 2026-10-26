"""Expression evaluation: self/context-determined widths and signedness, references, lvalues."""
from __future__ import annotations

import math
import re

from gatecost.frontend.verilog import ast as A
from gatecost.ir import SigSpec, mask
from gatecost.ir.sigspec import Const, Slice

from .symbols import ISym, KSym, PSym, PType, TSym, V, VSym, dim_pos, vec

_MAXDYN = 1 << 16


def _bare_pattern(e) -> bool:
    """True for an assignment pattern without a type prefix, or a ternary with one in a branch."""
    if isinstance(e, A.AssignPattern):
        return e.prefix is None
    return isinstance(e, A.Ternary) and (_bare_pattern(e.then) or _bare_pattern(e.else_))


class ElabError(Exception):
    """Local, recoverable problem (reported as a diagnostic by the catching item handler)."""


class Ref:
    """Resolved reference to (part of) a signal."""

    __slots__ = ("vsym", "net", "rootsig", "mem", "addr", "elems", "eidx", "off", "w", "pt", "dyn",
                 "oob", "cache")

    def __init__(self):
        self.vsym = None
        self.net = None
        self.rootsig = None
        self.mem = None
        self.addr = None
        self.elems = None
        self.eidx = None
        self.off = 0
        self.w = 0
        self.pt = None
        self.dyn = []
        self.oob = False
        self.cache = None


_BITRUN = re.compile(r"0+|1+")


def _diff_runs(f, t, w):
    """Maximal ``(lo, hi, differs)`` runs of two ``w``-bit signals, compared chunk-wise (never bit by bit)."""
    fc, tc = f.chunks, t.chunks
    runs: list = []
    i = j = 0           # chunk indexes
    fo = to = 0         # bits already consumed inside chunks i and j
    pos = 0
    while pos < w:
        a, b = fc[i], tc[j]
        n = min(a.width - fo, b.width - to, w - pos)
        if isinstance(a, Const) and isinstance(b, Const):
            m = (1 << n) - 1
            x = (((a.xmask >> fo) ^ (b.xmask >> to)) | ((a.value >> fo) ^ (b.value >> to))) & m
            if x and x != m:     # constants differ in some bits only: split at the bit level
                bits = bin(x | (1 << n))[:2:-1]       # bit k of x at index k (the sentinel bit is cut off)
                for mt in _BITRUN.finditer(bits):
                    k0, k1 = mt.span()
                    dk = bits[k0] == "1"
                    if runs and runs[-1][2] == dk and runs[-1][1] == pos + k0:
                        runs[-1][1] = pos + k1
                    else:
                        runs.append([pos + k0, pos + k1, dk])
                d = None
            else:
                d = bool(x)
        elif isinstance(a, Slice) and isinstance(b, Slice):
            d = a.net is not b.net or a.lo + fo != b.lo + to
        else:
            d = True
        if d is None:
            pass
        elif runs and runs[-1][2] == d:
            runs[-1][1] = pos + n
        else:
            runs.append([pos, pos + n, d])
        pos += n
        fo += n
        to += n
        if fo == a.width:
            i += 1
            fo = 0
        if to == b.width:
            j += 1
            to = 0
    return runs


def mux_runs(b, c, f, t, w, **kw):
    """``c ? t : f`` where only the bit runs that differ between the arms are multiplexed (identical bits are wires)."""
    f, t = f.resize(w, False), t.resize(w, False)
    runs = _diff_runs(f, t, w) if w > 0 else []
    if not runs or all(r[2] for r in runs):
        return b.mux(c, f, t, w, **kw)
    if not any(r[2] for r in runs):
        return f
    pieces = []
    for i, j, d in runs:
        if d:
            pieces.append(b.mux(c, f.slice(i, j - i), t.slice(i, j - i), j - i, **kw))
        else:
            pieces.append(f.slice(i, j - i))
    return SigSpec.concat(pieces)


_ARITH = {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod"}
_BITW = {"&": "and_", "|": "or_", "^": "xor", "~^": "xnor", "^~": "xnor"}
_SHIFT = ("<<", ">>", "<<<", ">>>")
_CMP = ("<", "<=", ">", ">=")
_EQ = ("==", "!=", "===", "!==", "==?", "!=?")
_REAL_ARITH = ("+", "-", "*", "/", "**")
_REAL_TYPES = ("real", "shortreal", "realtime")
_REAL_FUNCS = {
    "$ln": math.log, "$log10": math.log10, "$exp": math.exp, "$sqrt": math.sqrt, "$floor": math.floor,
    "$ceil": math.ceil, "$sin": math.sin, "$cos": math.cos, "$tan": math.tan, "$asin": math.asin,
    "$acos": math.acos, "$atan": math.atan, "$sinh": math.sinh, "$cosh": math.cosh, "$tanh": math.tanh,
    "$asinh": math.asinh, "$acosh": math.acosh, "$atanh": math.atanh, "$itor": float,
    "$pow": math.pow, "$atan2": math.atan2, "$hypot": math.hypot,
}


def round_real(f: float) -> int:
    """SystemVerilog real -> integer conversion: round half away from zero."""
    n = int(math.floor(abs(f) + 0.5))
    return -n if f < 0 else n


def is_z_expr(e) -> bool:
    if isinstance(e, A.Fill):
        return e.bit == "z"
    if isinstance(e, A.Number):
        return e.value is None and bool(e.digits) and all(c == "z" for c in e.digits)
    return False


_PN: dict = {}        # literal node id -> (node, result); cleared per project (see ``elaborate_project``)


def parse_number(n: A.Number):
    """``(value, xmask, width, signed)`` of a literal at its natural self-determined width."""
    e = _PN.get(id(n))
    if e is not None and e[0] is n:
        return e[1]
    r = _parse_number(n)
    _PN[id(n)] = (n, r)
    return r


def _parse_number(n: A.Number):
    base = n.base
    digits = n.digits
    signed = bool(n.signed)
    bits = {"b": 1, "o": 3, "h": 4}.get(base)
    if bits is not None:
        val = xm = 0
        dm = (1 << bits) - 1
        for ch in digits:
            val <<= bits
            xm <<= bits
            if ch in "xz?":
                xm |= dm
            else:
                try:
                    val |= int(ch, 16)
                except ValueError:
                    pass
        nat = len(digits) * bits
        top_x = bool(digits) and digits[0] in "xz?"
    else:
        xm = 0
        top_x = False
        if n.value is not None:
            val = n.value
            nat = max(1, val.bit_length())
        else:
            val = 0
            nat = 32
            xm = mask(n.size or 32)
            top_x = True
    if n.size is None:
        w = max(32, nat)
    else:
        w = min(max(1, n.size), 1 << 20)
    if w > nat:
        if top_x and bits is not None and digits:
            # x/z extension of the leftmost digit
            ext = mask(w) & ~mask(nat)
            xm |= ext
    val &= mask(w)
    xm &= mask(w)
    return val & ~xm, xm, w, signed


class ExprMixin:
    # ------------------------------------------------------------------ small helpers
    def rd(self, sig: SigSpec) -> SigSpec:
        blk = self.blk
        return blk.read(sig) if blk is not None else sig

    def lookup(self, name):
        return self.scope.lookup(name)

    def cond(self, e) -> SigSpec:
        """1-bit truth value of an expression (self-determined operand)."""
        if self._isreal(e):
            f = self.rval(e)
            return SigSpec.const(0 if f is None or f == 0 else 1, 1)
        v = self.val(e)
        return self.b.to_bool(v.sig)

    def val(self, e) -> V:
        old = self._tm
        self._tm = {}
        try:
            w, s = self.ty(e)
            sig = self.bv(e, w, s)
            if self._isreal(e):
                return V(sig, s, self.rval(e))
            return V(sig, s)
        finally:
            self._tm = old

    def assign_val(self, e, lw: int, pt: PType | None = None) -> SigSpec:
        """Value of ``e`` as the right-hand side of an assignment to ``lw`` bits."""
        if self._isreal(e):
            self.diag("UNSUPPORTED_CONSTRUCT", "real value used as a signal value; rounded to an integer", e, True)
        if isinstance(e, A.AssignPattern) and (pt is not None or e.prefix is not None):
            if e.prefix is not None:
                pt = self.resolve_dtype(e.prefix) if isinstance(e.prefix, A.DataType) else self.type_of_expr(e.prefix)
            sig = self.pattern_sig(e, pt, lw)
            return sig.resize(lw, False)
        if pt is not None and isinstance(e, A.Ternary) and _bare_pattern(e):
            # ``c ? CONST : '{field: v, ...}``: the untyped pattern takes the type of the assignment target
            return mux_runs(self.b, self.cond(e.cond), self.assign_val(e.else_, lw, pt),
                            self.assign_val(e.then, lw, pt), lw, origin=self.org(e.loc))
        old = self._tm
        self._tm = {}
        try:
            w, s = self.ty(e)
            W = max(lw, w)
            sig = self.bv(e, W, s)
            return sig if W == lw else sig.slice(0, lw)
        finally:
            self._tm = old

    def cint(self, e, default=None):
        """Python integer value of a constant expression, or ``default``."""
        try:
            v = self.val(e)
        except ElabError:
            return default
        c = v.sig.const_value()
        if c is None or c.xmask or c.width == 0:
            return default
        return c.as_signed() if v.signed else c.value

    def cval(self, e):
        v = self.val(e)
        c = v.sig.const_value()
        if c is None:
            return None
        return v

    def const_v(self, value: int, w: int = 32, signed: bool = True) -> V:
        return V(SigSpec.const(value & mask(w), w), signed)

    # ------------------------------------------------------------------ types
    def ty(self, e):
        k = id(e)
        r = self._tm.get(k)
        if r is None:
            r = self._tm[k] = self._ty(e)
        return r

    def _ty(self, e):
        c = type(e)
        if c is A.Number:
            _v, _x, w, s = parse_number(e)
            return (w, s)
        if self._isreal(e):
            return (64, True)
        if c is A.Ident:
            return self._ty_ident(e)
        if c is A.Binary:
            op = e.op
            if op in _SHIFT or op == "**":
                return self.ty(e.left)
            if op in _ARITH or op in _BITW:
                w1, s1 = self.ty(e.left)
                w2, s2 = self.ty(e.right)
                return (max(w1, w2), s1 and s2)
            self.ty(e.left)
            self.ty(e.right)
            return (1, False)
        if c is A.Unary:
            op = e.op
            if op in ("+", "-", "~"):
                return self.ty(e.operand)
            self.ty(e.operand)
            return (1, False)
        if c is A.Ternary:
            self.ty(e.cond)
            w1, s1 = self.ty(e.then)
            w2, s2 = self.ty(e.else_)
            return (max(w1, w2), s1 and s2)
        if c is A.Concat:
            return (sum(self.ty(i)[0] for i in e.items), False)
        if c is A.Replicate:
            n = self.cint(e.count, 0)
            if n is None or n < 0:
                n = 0
            return (min(max(0, n) * sum(self.ty(i)[0] for i in e.items), 1 << 20), False)
        if c is A.Index and (ap := self._arr_param(e)) is not None:
            return (ap.pt.w, ap.pt.signed)
        if c is A.Index or c is A.Select or c is A.Member:
            r = self.resolve_ref(e, False)
            if r is None:
                return (1, False)
            return (r.w, self._ref_signed(r))
        if c is A.Fill:
            return (1, False)
        if c is A.SysCall:
            return self._ty_syscall(e)
        if c is A.Call:
            return self._ty_call(e)
        if c is A.Cast:
            return self._ty_cast(e)
        if c is A.ScopedName:
            sym = self.lookup_scoped(e)
            if isinstance(sym, PSym):
                return (sym.val.sig.width, sym.val.signed)
            if isinstance(sym, VSym):
                return (sym.pt.w, sym.pt.signed)
            return (32, False)
        if c is A.Inside:
            return (1, False)
        if c is A.Streaming:
            tot = 0
            for it in e.items:
                x = it.expr if isinstance(it, A.StreamWith) else it
                tot += self.ty(x)[0]
            return (tot, False)
        if c is A.AssignPattern:
            if e.prefix is not None:
                pt = self.resolve_dtype(e.prefix) if isinstance(e.prefix, A.DataType) else self.type_of_expr(e.prefix)
                return (pt.w, pt.signed)
            return (1, False)
        if c is A.String:
            return (max(1, 8 * len(e.value)), False)
        return (1, False)

    def _ref_signed(self, r: Ref) -> bool:
        pt = r.pt
        return bool(pt is not None and pt.signed)

    def _ty_ident(self, e):
        sym = self.lookup(e.name)
        if sym is None:
            self.unresolved(e.name, e)
            return (32, False)
        k = sym.kind
        if k == "param":
            return (sym.val.sig.width, sym.val.signed)
        if k == "var":
            if sym.udims:
                return (sym.pt.w * self._array_count(sym), False)
            return (sym.pt.w, sym.pt.signed)
        if k == "type":
            return (sym.pt.w, sym.pt.signed)
        return (32, False)

    def unresolved(self, name, node):
        if name not in self._unres:
            self._unres.add(name)
            self.diag("UNRESOLVED_NAME", f"identifier '{name}' is not declared; assumed 32 bits of "
                      "unknown value", node, True)
            self.assume(f"undeclared identifier '{name}' assumed to be 32 bits wide, unknown value")

    def lookup_scoped(self, e):
        scope = e.scope
        sc = None
        if isinstance(scope, str):
            names = scope.split("::")
            sc = self.ctx.pkg_scope(names[0], self)
            sym = sc.syms.get(e.name) if sc is not None else None
            if sym is None:
                t = self.lookup(names[0])
                if isinstance(t, TSym) and t.pt.enum and e.name in t.pt.enum:
                    return PSym(self.const_v(t.pt.enum[e.name], t.pt.w, t.pt.signed), t.pt)
                if sc is None:
                    self.diag("MISSING_UNIT", f"package '{names[0]}' not found", e, True)
                else:
                    self.diag("UNRESOLVED_NAME", f"'{e.name}' not found in package '{names[0]}'", e, True)
            return sym
        return None

    def type_of_expr(self, e) -> PType:
        """PType named by an expression (Ident/ScopedName typedef)."""
        sym = None
        if isinstance(e, A.Ident):
            sym = self.lookup(e.name)
        elif isinstance(e, A.ScopedName):
            sym = self.lookup_scoped(e)
        if isinstance(sym, TSym):
            return sym.pt
        w, s = self.ty(e)
        return vec(w, s)

    def _ty_syscall(self, e):
        n = e.name
        if n in ("$signed", "$unsigned"):
            w = self.ty(e.args[0])[0] if e.args and isinstance(e.args[0], A.Expr) else 1
            return (w, n == "$signed")
        if n in ("$onehot", "$onehot0", "$isunknown"):
            return (1, False)
        if n in ("$min", "$max"):
            ts = [self.ty(a) for a in e.args if isinstance(a, A.Expr)]
            if ts:
                return (max(t[0] for t in ts), all(t[1] for t in ts))
        return (32, True)

    def _ty_call(self, e):
        f = self.find_func(e)
        if f is None:
            return (32, False)
        pt = self.func_rettype(f)
        if pt is None:
            return (1, False)
        return (pt.w, pt.signed)

    def _real_to_int_cast(self, e):
        """(width, signed, value) of ``int'(real expr)``-style casts of a real operand, else None."""
        t = e.target
        if not self._isreal(e.expr) or (isinstance(t, A.OtherType) and t.kind in ("signed", "unsigned")):
            return None
        if isinstance(t, A.DataType):
            pt = self.resolve_dtype(t)
        else:
            sym = None
            if isinstance(t, A.Ident):
                sym = self.lookup(t.name)
            elif isinstance(t, A.ScopedName):
                sym = self.lookup_scoped(t)
            if isinstance(sym, TSym):
                pt = sym.pt
            else:
                n = self.cint(t, None)
                if n is None or n <= 0:
                    n = 32
                pt = vec(n, True)
        f = self.rval(e.expr)
        return pt.w, pt.signed, (None if f is None else round_real(f))

    def _ty_cast(self, e):
        t = e.target
        r = self._real_to_int_cast(e)
        if r is not None:
            return (r[0], r[1])
        if isinstance(t, A.OtherType) and t.kind in ("signed", "unsigned"):
            w = self.ty(e.expr)[0]
            return (w, t.kind == "signed")
        if isinstance(t, A.DataType):
            pt = self.resolve_dtype(t)
            return (pt.w, pt.signed)
        # expression target: typedef name or a size
        sym = None
        if isinstance(t, A.Ident):
            sym = self.lookup(t.name)
        elif isinstance(t, A.ScopedName):
            sym = self.lookup_scoped(t)
        if isinstance(sym, TSym):
            return (sym.pt.w, sym.pt.signed)
        n = self.cint(t, None)
        if n is None or n <= 0:
            self.diag("UNRESOLVED_PARAM", "cast size is not constant; assumed 32", e, True)
            n = 32
        return (n, self.ty(e.expr)[1])

    # ------------------------------------------------------------------ building
    def bv(self, e, w: int, s: bool) -> SigSpec:
        """Signal for ``e`` at context width ``w`` with propagated signedness ``s``."""
        c = type(e)
        b = self.b
        if c is A.Number:
            val, xm, nw, sg = parse_number(e)
            sig = SigSpec.const(val, nw, xm)
            return sig.resize(w, s) if nw != w else sig
        if self._isreal(e):
            f = self.rval(e)
            if f is None:
                self.diag("UNRESOLVED_PARAM", "real expression is not constant; assumed unknown", e, True)
                return SigSpec.x(w)
            return SigSpec.const(round_real(f) & mask(max(w, 1)), w)
        if c is A.Ident:
            return self._bv_ident(e, w, s)
        if c is A.Binary:
            return self._bv_binary(e, w, s)
        if c is A.Unary:
            return self._bv_unary(e, w, s)
        if c is A.Ternary:
            return self._bv_ternary(e, w, s)
        if c is A.Fill:
            if e.bit == "1":
                return SigSpec.const(mask(w), w)
            if e.bit == "0":
                return SigSpec.const(0, w)
            return SigSpec.x(w)
        if c is A.Concat:
            parts = [self.bv(i, *self.ty(i)) for i in e.items]
            parts.reverse()
            sig = SigSpec.concat(parts)
            return sig.resize(w, False)
        if c is A.Replicate:
            n = self.cint(e.count, 0)
            if n is None or n < 0:
                self.diag("UNRESOLVED_PARAM", "replication count is not constant; assumed 1", e, True)
                n = 1
            parts = [self.bv(i, *self.ty(i)) for i in e.items]
            parts.reverse()
            inner = SigSpec.concat(parts)
            if n * inner.width > (1 << 20):
                self.diag("LIMIT_REACHED", "replication wider than 2**20 bits truncated", e, True)
                n = max(0, (1 << 20) // max(1, inner.width))
            return inner.repeat(n).resize(w, False)
        if c is A.Index and (ap := self._arr_param(e)) is not None:
            return self._arr_read(ap, e).resize(w, s and ap.pt.signed)
        if c is A.Index or c is A.Select or c is A.Member:
            r = self.resolve_ref(e, True)
            if r is None:
                return SigSpec.x(w)
            sig = self.read_ref(r)
            return sig.resize(w, s and self._ref_signed(r))
        if c is A.SysCall:
            return self._syscall(e).sig.resize(w, s)
        if c is A.Call:
            v = self.call_expr(e)
            return v.sig.resize(w, s and v.signed)
        if c is A.Cast:
            return self._bv_cast(e, w, s)
        if c is A.ScopedName:
            sym = self.lookup_scoped(e)
            if isinstance(sym, PSym):
                return sym.val.sig.resize(w, s)
            if isinstance(sym, VSym) and sym.net is not None:
                return self.read_sym(sym).resize(w, s)
            return SigSpec.x(w)
        if c is A.Inside:
            return self._bv_inside(e).zext(w)
        if c is A.Streaming:
            return self._bv_stream(e).resize(w, False)
        if c is A.AssignPattern:
            pt = None
            if e.prefix is not None:
                pt = self.resolve_dtype(e.prefix) if isinstance(e.prefix, A.DataType) else self.type_of_expr(e.prefix)
            else:
                pt = vec(w)
            return self.pattern_sig(e, pt, w).resize(w, False)
        if c is A.String:
            bits = 0
            for ch in e.value:
                bits = (bits << 8) | (ord(ch) & 0xFF)
            return SigSpec.const(bits, max(1, 8 * len(e.value))).resize(w, False)
        return SigSpec.x(w)

    def _bv_ident(self, e, w, s):
        sym = self.lookup(e.name)
        if sym is None:
            self.unresolved(e.name, e)
            return SigSpec.x(w)
        k = sym.kind
        if k == "param":
            return sym.val.sig.resize(w, s)
        if k == "var":
            if sym.udims:
                return self._read_array(sym, e).resize(w, False)
            if sym.net is None and sym.mem is None and sym.elems is None:
                return SigSpec.x(w)
            return self.read_sym(sym).resize(w, s)
        return SigSpec.x(w)

    # ---- whole unpacked arrays, element-wise (first element in the most significant bits, the order
    # ---- an unpacked-array port is flattened to)
    @staticmethod
    def _array_count(sym) -> int:
        n = 1
        for l, r in sym.udims:
            n *= abs(l - r) + 1
        return n

    def _array_order(self, sym, node):
        """Flat element indices (row-major storage order) from the most significant element down."""
        total = self._array_count(sym)
        if total > 4096:
            self.diag("LIMIT_REACHED", f"whole-array use of '{sym.name}' with {total} elements is not supported",
                      node, True)
            return None
        order = [0]
        for l, r in sym.udims:
            n = abs(l - r) + 1
            # flat index contribution of the position-th element from the left of this dimension
            pos = list(range(n)) if l <= r else list(range(n - 1, -1, -1))
            order = [o * n + q for o in order for q in pos]
        return order

    def _array_elem_ref(self, sym, flat: int, total: int):
        r = Ref()
        r.vsym = sym
        r.pt = sym.pt
        r.w = sym.pt.w
        if sym.mem is not None:
            r.mem = sym.mem
            r.addr = SigSpec.const(flat, max(1, (total - 1).bit_length()))
        else:
            r.net = self.elem_net(sym, flat)
        return r

    def _array_refs(self, sym, node):
        order = self._array_order(sym, node)
        if order is None:
            return None
        total = self._array_count(sym)
        return [self._array_elem_ref(sym, f, total) for f in order]

    def _read_array(self, sym, node) -> SigSpec:
        refs = self._array_refs(sym, node)
        if refs is None:
            return SigSpec.x(sym.pt.w * self._array_count(sym))
        parts = [self.read_ref(r) for r in refs]
        parts.reverse()
        return SigSpec.concat(parts)

    def read_sym(self, sym: VSym) -> SigSpec:
        if sym.const_sig is not None:
            return sym.const_sig
        net = sym.net
        if net is None:
            if sym.elems is None:
                self.diag("UNSUPPORTED_CONSTRUCT", f"whole-array use of memory '{sym.name}'", None, True)
                return SigSpec.x(sym.pt.w)
            net = self.elem_net(sym, 0)
        return self.rd_var(sym, net)

    def rd_var(self, sym, net) -> SigSpec:
        """Value of a variable now; an uninitialised procedural local reads as unknown."""
        blk = self.blk
        if blk is not None:
            if sym is not None:
                if sym.local and net.id not in blk.env.vis and net.id not in blk.env.val:
                    return SigSpec.x(net.width)
                if self.tmp_cands is not None and sym in self.tmp_cands and net.id not in blk.env.assigned:
                    self.rbw.add(net.id)
            return blk.read(net.sig)
        return net.sig

    def _bv_unary(self, e, w, s):
        b = self.b
        op = e.op
        o = self.org(e.loc)
        if op == "-":
            return b.neg(self.bv(e.operand, w, s), w, sa=s, origin=o)
        if op == "+":
            return self.bv(e.operand, w, s)
        if op == "~":
            return b.not_(self.bv(e.operand, w, s), w, origin=o)
        v = self.val(e.operand)
        sig = v.sig
        if op == "!":
            r = b.lnot(sig, origin=o)
        elif op == "&":
            r = b.red_and(sig, origin=o)
        elif op == "|":
            r = b.red_or(sig, origin=o)
        elif op == "^":
            r = b.red_xor(sig, origin=o)
        elif op == "~&":
            r = b.not_(b.red_and(sig, origin=o), 1, origin=o)
        elif op == "~|":
            r = b.not_(b.red_or(sig, origin=o), 1, origin=o)
        elif op in ("~^", "^~"):
            r = b.not_(b.red_xor(sig, origin=o), 1, origin=o)
        else:
            self.diag("UNSUPPORTED_CONSTRUCT", f"unary operator '{op}'", e)
            return SigSpec.x(w)
        return r.zext(w)

    def _bv_binary(self, e, w, s):
        b = self.b
        op = e.op
        o = self.org(e.loc)
        if (op in _CMP or op in _EQ or op in ("&&", "||")) and (self._isreal(e.left) or self._isreal(e.right)):
            r = self._real_cmp(e)
            if r is None:
                self.diag("UNRESOLVED_PARAM", "real comparison is not constant; assumed unknown", e, True)
                return SigSpec.x(w)
            return SigSpec.const(r, w)
        if op in _ARITH:
            l = self.bv(e.left, w, s)
            r = self.bv(e.right, w, s)
            return getattr(b, _ARITH[op])(l, r, w, sa=s, sb=s, origin=o)
        if op in _BITW:
            l = self.bv(e.left, w, s)
            r = self.bv(e.right, w, s)
            return getattr(b, _BITW[op])(l, r, w, origin=o)
        if op in _SHIFT:
            l = self.bv(e.left, w, s)
            amt = self.val(e.right).sig
            if op in ("<<", "<<<"):
                return b.shl(l, amt, w, sa=s, origin=o)
            if op == ">>>" and s:
                return b.sar(l, amt, w, sa=s, origin=o)
            return b.shr(l, amt, w, origin=o)
        if op == "**":
            return self._bv_pow(e, w, s)
        if op in ("&&", "||"):
            l = self.cond(e.left)
            r = self.cond(e.right)
            res = b.land(l, r, origin=o) if op == "&&" else b.lor(l, r, origin=o)
            return res.zext(w)
        w1, s1 = self.ty(e.left)
        w2, s2 = self.ty(e.right)
        cw = max(w1, w2)
        cs = s1 and s2
        if op in _CMP:
            l = self.bv(e.left, cw, cs)
            r = self.bv(e.right, cw, cs)
            fn = {"<": b.lt, "<=": b.le, ">": b.gt, ">=": b.ge}[op]
            return fn(l, r, sa=cs, sb=cs, origin=o).zext(w)
        if op in _EQ:
            l = self.bv(e.left, cw, cs)
            r = self.bv(e.right, cw, cs)
            neg = op in ("!=", "!==", "!=?")
            res = self.eq_wild(l, r, o, neg)
            return res.zext(w)
        self.diag("UNSUPPORTED_CONSTRUCT", f"binary operator '{op}'", e)
        return SigSpec.x(w)

    def eq_wild(self, l: SigSpec, r: SigSpec, o, negate: bool = False) -> SigSpec:
        """Equality (``negate``: inequality, a NE node) where don't-care constant bits (x/z/?) are not compared."""
        b = self.b
        cl, cr = l.const_value(), r.const_value()
        km = None
        if cr is not None and cr.xmask:
            km = cr.kmask
        if cl is not None and cl.xmask:
            km = cl.kmask if km is None else km & cl.kmask
        cmp = b.ne if negate else b.eq
        if km is None:
            return cmp(l, r, origin=o)
        if km == 0:
            return SigSpec.const(0 if negate else 1, 1)
        ps, pr = [], []
        i, w = 0, l.width
        while i < w:
            if not (km >> i) & 1:
                i += 1
                continue
            j = i
            while j < w and (km >> j) & 1:
                j += 1
            ps.append(l.slice(i, j - i))
            pr.append(r.slice(i, j - i))
            i = j
        return cmp(SigSpec.concat(ps), SigSpec.concat(pr), origin=o)

    def _bv_pow(self, e, w, s):
        b = self.b
        o = self.org(e.loc)
        n = self.cint(e.right, None)
        base = self.bv(e.left, w, s)
        if n is not None and n >= 0:
            return b.pow(base, n, w, sa=s, origin=o)
        bc = base.const_value()
        if bc is not None and not bc.xmask and bc.value == 2:
            amt = self.val(e.right).sig
            return b.shl(SigSpec.const(1, w), amt, w, origin=o)
        if n is not None:           # negative constant exponent
            self.diag("UNSUPPORTED_CONSTRUCT", "power with a negative exponent is not supported", e)
            return SigSpec.x(w)
        # square-and-multiply ladder over the exponent bits (bounded)
        ew, es = self.ty(e.right)
        ex = self.bv(e.right, ew, es)
        k = ex.width
        if k > 6:
            self.diag("LIMIT_REACHED", f"power with a {k}-bit exponent: only the low 6 bits are costed", e, True)
            k = 6
        self.assume("power with a non-constant exponent costed as a square-and-multiply ladder")
        acc = SigSpec.const(1, w)
        sq = base
        for i in range(k):
            prod = b.mul(acc, sq, w, sa=s, sb=s, origin=o)
            acc = b.mux(ex.slice(i, 1), acc, prod, origin=o)
            if i + 1 < k:
                sq = b.mul(sq, sq, w, sa=s, sb=s, origin=o)
        return acc

    def _bv_ternary(self, e, w, s):
        b = self.b
        o = self.org(e.loc)
        c = self.cond(e.cond)
        zt, ze = is_z_expr(e.then), is_z_expr(e.else_)
        if zt or ze:
            data = self.bv(e.else_ if zt else e.then, w, s)
            en = b.not_(c, 1, origin=o) if zt else c
            self.assume("tri-state driver ('z') costed as a multiplexer with enable")
            self.diag("TRISTATE", "tri-state driver costed as a mux with enable", e, True, "info")
            return b.mux(en, SigSpec.x(w), data, w, tristate=True, origin=o)
        t = self.bv(e.then, w, s)
        f = self.bv(e.else_, w, s)
        return mux_runs(b, c, f, t, w, origin=o)

    def _bv_cast(self, e, w, s):
        t = e.target
        r = self._real_to_int_cast(e)
        if r is not None:
            cw, cs, n = r
            if n is None:
                self.diag("UNRESOLVED_PARAM", "real expression is not constant; assumed unknown", e, True)
                return SigSpec.x(w)
            return SigSpec.const(n & mask(cw), cw).resize(w, s and cs)
        cw, cs = self.ty(e)
        inner_w, inner_s = self.ty(e.expr)
        if isinstance(t, A.OtherType) and t.kind in ("signed", "unsigned"):
            sig = self.bv(e.expr, inner_w, inner_s)
            return sig.resize(w, s and cs)
        if isinstance(e.expr, A.AssignPattern):
            pt = self.resolve_dtype(t) if isinstance(t, A.DataType) else self.type_of_expr(t)
            return self.pattern_sig(e.expr, pt, pt.w).resize(w, False)
        sig = self.bv(e.expr, inner_w, inner_s).resize(cw, inner_s)
        return sig.resize(w, s and cs)

    # ------------------------------------------------------------------ inside / streaming / patterns
    def _bv_inside(self, e) -> SigSpec:
        b = self.b
        o = self.org(e.loc)
        w0, s0 = self.ty(e.expr)
        w, s = w0, s0
        for it in e.items:
            for x in ((it.lo, it.hi) if isinstance(it, A.ValueRange) else (it,)):
                w1, s1 = self.ty(x)
                w, s = max(w, w1), s and s1
        x = self.bv(e.expr, w, s)
        hits = []
        for it in e.items:
            if isinstance(it, A.ValueRange):
                lo = self.bv(it.lo, w, s)
                hi = self.bv(it.hi, w, s)
                hits.append(b.land(b.ge(x, lo, sa=s, sb=s, origin=o), b.le(x, hi, sa=s, sb=s, origin=o), origin=o))
            else:
                hits.append(self.eq_wild(x, self.bv(it, w, s), o))
        if not hits:
            return SigSpec.const(0, 1)
        r = hits[0]
        for h in hits[1:]:
            r = b.or_(r, h, 1, origin=o)
        return r

    def _arr_param(self, e):
        """The array parameter an ``Index`` expression selects an element of, or None."""
        if type(e.base) is A.Ident:
            sym = self.lookup(e.base.name)
            if isinstance(sym, PSym) and sym.arr is not None:
                return sym
        return None

    def _arr_read(self, sym, e) -> SigSpec:
        """Element of an array parameter: constant index -> constant, otherwise a ROM."""
        arr, ew = sym.arr, sym.pt.w
        v = self.val(e.index)
        c = v.sig.const_value()
        if c is not None and not c.xmask:
            i = c.as_signed() if v.signed else c.value
            if 0 <= i < len(arr):
                return SigSpec.const(arr[i], ew)
            return SigSpec.x(ew)
        n = len(arr)
        ab = max(1, (n - 1).bit_length())
        if n == 1:
            return SigSpec.const(arr[0], ew)
        iw, isg = self.ty(e.index)
        addr = self.bv(e.index, max(iw, ab), isg).slice(0, ab)
        pad = list(arr) + [0] * ((1 << ab) - n) if (1 << ab) <= 4096 else list(arr)
        return self.b.rom(addr, pad, ew, origin=self.b.origin)

    def stream_size(self, e) -> int:
        """Slice size (bits) of a ``{<< size {...}}`` streaming concatenation."""
        size = 1
        if e.size is not None:
            if isinstance(e.size, A.DataType):
                size = self.resolve_dtype(e.size).w
            elif isinstance(e.size, (A.Ident, A.ScopedName)) and isinstance(
                    self.lookup(e.size.name) if isinstance(e.size, A.Ident) else None, TSym):
                size = self.lookup(e.size.name).pt.w
            else:
                size = self.cint(e.size, 1) or 1
        return max(1, size)

    def _stream_lvalue(self, e):
        """Parts of ``{>> {a, b}} = x`` / ``{<< n {a, b}} = x`` (most significant first): ``<<`` reverses
        the order of the n-bit slices taken from the right."""
        parts = []
        for it in e.items:
            x = it.expr if isinstance(it, A.StreamWith) else it
            parts.extend(self.lvalue_parts(x))
        if e.op == ">>" or not parts or any(p is None for p in parts):
            return parts
        size = self.stream_size(e)
        if any(p.dyn or p.elems is not None for p in parts):
            self.diag("UNSUPPORTED_CONSTRUCT", "left-to-right streaming target with variable selects; "
                      "slice order not reversed", e, True)
            return parts
        chunks = [[]]              # chunks[0] is the right-most slice; each holds segments most significant first
        room = size
        for p in reversed(parts):  # least significant part first
            lo = 0
            while lo < p.w:
                n = min(room, p.w - lo)
                q = Ref()
                for f in Ref.__slots__:
                    setattr(q, f, getattr(p, f))
                q.off = p.off + lo
                q.w = n
                q.pt = None
                chunks[-1].insert(0, q)
                lo += n
                room -= n
                if room == 0:
                    chunks.append([])
                    room = size
        if not chunks[-1]:
            chunks.pop()
        return [q for ch in chunks for q in ch]

    def _bv_stream(self, e) -> SigSpec:
        parts = []
        for it in e.items:
            x = it.expr if isinstance(it, A.StreamWith) else it
            parts.append(self.bv(x, *self.ty(x)))
        parts.reverse()
        sig = SigSpec.concat(parts)
        if e.op == ">>" or sig.width == 0:
            return sig
        size = self.stream_size(e)
        chunks = []
        pos = 0
        while pos < sig.width:
            n = min(size, sig.width - pos)
            chunks.append(sig.slice(pos, n))
            pos += n
        # chunks[0] is the first slice from the right; it goes to the leftmost position
        return SigSpec.concat(list(reversed(chunks)))

    def pattern_sig(self, e: A.AssignPattern, pt: PType, w: int) -> SigSpec:
        """Lower an assignment pattern for a packed struct / array type to a vector."""
        total = pt.w
        items = e.items
        keyed = [i for i in items if isinstance(i, A.PatternItem)]
        pieces: dict = {}      # (offset, width) -> sig  ;  default handled separately
        default = None
        if pt.fields:
            names = list(pt.fields.keys())
            if keyed:
                for it in keyed:
                    if it.key == "default":
                        default = it.value
                    elif isinstance(it.key, A.Ident) and it.key.name in pt.fields:
                        off, fpt = pt.fields[it.key.name]
                        pieces[it.key.name] = self._sub_value(it.value, fpt)
                    elif isinstance(it.key, A.DataType):
                        default = it.value
                    else:
                        self.diag("UNSUPPORTED_CONSTRUCT", "assignment pattern key", it)
            else:
                for nm, it in zip(names, items):
                    pieces[nm] = self._sub_value(it, pt.fields[nm][1])
            out = SigSpec.const(0, total)
            parts = sorted(pt.fields.items(), key=lambda kv: kv[1][0])
            sigs = []
            for nm, (off, fpt) in parts:
                if nm in pieces:
                    sigs.append(pieces[nm])
                elif default is not None:
                    sigs.append(self._sub_value(default, fpt))
                else:
                    sigs.append(SigSpec.x(fpt.w))
            # fields may overlap (unions): last wins for the low bits
            if len({o for _n, (o, _p) in parts}) != len(parts):
                return sigs[0].resize(total, False) if sigs else out
            return SigSpec.concat(sigs).resize(total, False)
        # array / vector
        if not pt.dims:
            pt = vec(total)
        n = pt.size0()
        ept = pt.elem()
        ew = pt.elem_width()
        if n * ew > self.ctx.ir_limits.max_net_width:
            # the IR rejects a value this wide anyway (unresolved parameters default to 32 and multiply):
            # do not build it element by element
            self.diag("LIMIT_REACHED", "assignment pattern of %d bits exceeds the net width limit (%d); its value "
                      "is unknown" % (n * ew, self.ctx.ir_limits.max_net_width), e, True)
            return SigSpec.x(total)
        slots = [None] * n          # index by position (0 = least significant)
        d0 = pt.dims[0]
        pos_i = 0
        for it in items:
            if isinstance(it, A.PatternItem):
                if it.key == "default":
                    default = it.value
                    continue
                if isinstance(it.key, A.DataType):
                    default = it.value
                    continue
                k = self.cint(it.key, None)
                if k is None:
                    self.diag("UNSUPPORTED_CONSTRUCT", "non-constant assignment pattern index", it)
                    continue
                p = dim_pos(k, d0)
                if 0 <= p < n:
                    slots[p] = self._sub_value(it.value, ept)
            elif isinstance(it, A.Replicate):
                cnt = self.cint(it.count, 0) or 0
                vals = [self._sub_value(x, ept) for x in it.items]
                for _ in range(cnt):
                    for v in vals:
                        # positional: first item is the leftmost element
                        p = n - 1 - pos_i
                        if 0 <= p < n:
                            slots[p] = v
                        pos_i += 1
            else:
                p = n - 1 - pos_i
                if 0 <= p < n:
                    slots[p] = self._sub_value(it, ept)
                pos_i += 1
        dflt = self._sub_value(default, ept) if default is not None else None
        parts = []
        for p in range(n):
            if slots[p] is not None:
                parts.append(slots[p])
            elif dflt is not None:
                parts.append(dflt)
            else:
                parts.append(SigSpec.x(ew))
        return SigSpec.concat(parts).resize(total, False)

    def _sub_value(self, e, pt: PType) -> SigSpec:
        if isinstance(e, A.AssignPattern):
            sub = e
            ppt = pt
            if e.prefix is not None:
                ppt = self.resolve_dtype(e.prefix) if isinstance(e.prefix, A.DataType) else self.type_of_expr(e.prefix)
            return self.pattern_sig(sub, ppt, ppt.w).resize(pt.w, False)
        return self.assign_val(e, pt.w)

    # ------------------------------------------------------------------ real constants
    def _isreal(self, e) -> bool:
        """True when ``e`` has type real (elaboration-time floating-point constants only)."""
        c = type(e)
        if c is A.Real:
            return True
        if c is A.Ident:
            sym = self.lookup(e.name)
            return isinstance(sym, PSym) and sym.real is not None
        if c is A.ScopedName:
            sym = self.lookup_scoped(e)
            return isinstance(sym, PSym) and sym.real is not None
        if c not in (A.Binary, A.Unary, A.Ternary, A.Cast, A.SysCall):
            return False
        k = ("r", id(e))
        r = self._tm.get(k)
        if r is None:
            r = self._tm[k] = self._isreal2(e, c)
        return r

    def _isreal2(self, e, c) -> bool:
        if c is A.Binary:
            return e.op in _REAL_ARITH and (self._isreal(e.left) or self._isreal(e.right))
        if c is A.Unary:
            return e.op in ("+", "-") and self._isreal(e.operand)
        if c is A.Ternary:
            return self._isreal(e.then) or self._isreal(e.else_)
        if c is A.Cast:
            t = e.target
            return isinstance(t, A.OtherType) and t.kind in _REAL_TYPES
        n = e.name
        if n in ("$pow", "$abs"):
            return any(self._isreal(a) for a in e.args if isinstance(a, A.Expr))
        return n in _REAL_FUNCS

    def _rv(self, e):
        """Floating-point value of any constant expression, or None."""
        if self._isreal(e):
            return self.rval(e)
        x = self.cint(e, None)
        return None if x is None else float(x)

    def _real_cmp(self, e):
        a, b = self._rv(e.left), self._rv(e.right)
        if a is None or b is None:
            return None
        op = e.op
        if op == "&&":
            return int(a != 0 and b != 0)
        if op == "||":
            return int(a != 0 or b != 0)
        if op in ("==", "===", "==?"):
            return int(a == b)
        if op in ("!=", "!==", "!=?"):
            return int(a != b)
        return int({"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op])

    def rval(self, e):
        """Value of a real-typed constant expression as a Python float, or None when not constant."""
        try:
            return self._rval(e)
        except (ValueError, OverflowError, ZeroDivisionError, ElabError):
            return None

    def _rval(self, e):
        c = type(e)
        if c is A.Real:
            return float(e.value)
        if c is A.Ident or c is A.ScopedName:
            sym = self.lookup(e.name) if c is A.Ident else self.lookup_scoped(e)
            if isinstance(sym, PSym) and sym.real is not None:
                return sym.real
            return self._rv(e) if not self._isreal(e) else None
        if c is A.Binary:
            op = e.op
            a, b = self._rv(e.left), self._rv(e.right)
            if a is None or b is None:
                return None
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                return a / b
            return math.pow(a, b)
        if c is A.Unary:
            a = self._rv(e.operand)
            if a is None:
                return None
            return -a if e.op == "-" else a
        if c is A.Ternary:
            cv = self._rv(e.cond)
            if cv is None:
                return None
            return self._rv(e.then if cv != 0 else e.else_)
        if c is A.Cast:
            return self._rv(e.expr)
        if c is A.SysCall:
            n = e.name
            args = [a for a in e.args if isinstance(a, A.Expr)]
            if n == "$abs":
                a = self._rv(args[0])
                return None if a is None else abs(a)
            fn = _REAL_FUNCS.get(n)
            vs = [self._rv(a) for a in args]
            if fn is None or None in vs:
                return None
            r = fn(*vs)
            return float(r)
        return None

    # ------------------------------------------------------------------ system functions
    def _syscall(self, e: A.SysCall) -> V:
        n = e.name
        args = e.args
        b = self.b
        o = self.org(e.loc)
        a0 = args[0] if args else None
        if n == "$clog2":
            x = self.cint(a0, None) if a0 is not None else None
            if x is None:
                self.diag("UNRESOLVED_PARAM", "$clog2 argument is not constant; assumed 1", e, True)
                x = 2
            return self.const_v((x - 1).bit_length() if x > 1 else 0)
        if n in ("$bits", "$size", "$dimensions", "$left", "$right", "$low", "$high", "$increment"):
            return self._sys_bits(e)
        if n == "$signed" or n == "$unsigned":
            v = self.val(a0)
            return V(v.sig, n == "$signed")
        if n in ("$min", "$max"):
            vs = [self.val(a) for a in args if isinstance(a, A.Expr)]
            if not vs:
                return self.const_v(0)
            sg = all(v.signed for v in vs)
            w = max(v.w for v in vs)
            acc = vs[0].sig.resize(w, sg and vs[0].signed)
            for v in vs[1:]:
                x = v.sig.resize(w, sg and v.signed)
                lt = b.lt(x, acc, sa=sg, sb=sg, origin=o)
                acc = b.mux(lt, acc, x, w, origin=o) if n == "$min" else b.mux(lt, x, acc, w, origin=o)
            return V(acc, sg)
        if n == "$countones":
            v = self.val(a0)
            return V(self.popcount(v.sig, o).zext(32), True)
        if n in ("$onehot", "$onehot0"):
            v = self.val(a0)
            x = v.sig
            w = x.width
            m1 = b.sub(x, SigSpec.const(1, w), w, origin=o)
            z = b.eq(b.and_(x, m1, w, origin=o), SigSpec.const(0, w), origin=o)
            if n == "$onehot0":
                return V(z, False)
            return V(b.land(z, b.red_or(x, origin=o), origin=o), False)
        if n == "$isunknown":
            return V(SigSpec.const(0, 1), False)
        if n == "$rtoi" and a0 is not None and self._isreal(a0):
            f = self.rval(a0)
            if f is None:
                self.diag("UNRESOLVED_PARAM", "$rtoi argument is not constant; assumed unknown", e, True)
                return V(SigSpec.x(32), True)
            return self.const_v(int(f), 64)
        if n in ("$rtoi", "$itor", "$cast"):
            return self.val(a0) if a0 is not None else self.const_v(0)
        if n == "$pow":
            x, y = self.cint(args[0], None), self.cint(args[1], None)
            if x is not None and y is not None and y >= 0:
                return self.const_v(x ** y)
        if n == "$abs":
            x = self.cint(a0, None)
            if x is not None:
                return self.const_v(abs(x))
        self.diag("UNSUPPORTED_CONSTRUCT", f"system function {n} is not supported", e)
        return V(SigSpec.x(32), False)

    def popcount(self, sig: SigSpec, o) -> SigSpec:
        b = self.b
        rw = max(1, sig.width.bit_length())
        items = [sig.slice(i, 1).zext(rw) for i in range(sig.width)]
        while len(items) > 1:
            nxt = []
            for i in range(0, len(items) - 1, 2):
                nxt.append(b.add(items[i], items[i + 1], rw, origin=o))
            if len(items) % 2:
                nxt.append(items[-1])
            items = nxt
        return items[0]

    def _sys_bits(self, e: A.SysCall) -> V:
        n = e.name
        a0 = e.args[0] if e.args else None
        if a0 is None:
            return self.const_v(0)
        udims = ()
        if isinstance(a0, A.DataType):
            pt = self.resolve_dtype(a0)
        else:
            sym = None
            if isinstance(a0, A.Ident):
                sym = self.lookup(a0.name)
            elif isinstance(a0, A.ScopedName):
                sym = self.lookup_scoped(a0)
            if isinstance(sym, TSym):
                pt = sym.pt
            elif isinstance(sym, VSym) and sym.pt is not None:
                pt, udims = sym.pt, sym.udims
            else:
                r = self.resolve_ref(a0, False) if isinstance(a0, (A.Index, A.Select, A.Member)) else None
                if r is not None:
                    pt = r.pt
                else:
                    w, s = self.ty(a0)
                    pt = vec(w, s)
        if n == "$bits":
            tot = pt.w
            for d in udims:
                tot *= abs(d[0] - d[1]) + 1
            return self.const_v(tot)
        dims = list(udims) + list(pt.dims or ((pt.w - 1, 0),))
        k = 1
        if len(e.args) > 1:
            k = self.cint(e.args[1], 1) or 1
        d = dims[min(max(k, 1), len(dims)) - 1]
        if n == "$dimensions":
            return self.const_v(len(dims))
        if n == "$size":
            return self.const_v(abs(d[0] - d[1]) + 1)
        if n == "$left":
            return self.const_v(d[0])
        if n == "$right":
            return self.const_v(d[1])
        if n == "$low":
            return self.const_v(min(d))
        if n == "$high":
            return self.const_v(max(d))
        return self.const_v(1 if d[0] >= d[1] else -1)

    # ------------------------------------------------------------------ references
    def _chain(self, e):
        steps = []
        while True:
            c = type(e)
            if c is A.Index:
                steps.append(("i", e.index, e))
                e = e.base
            elif c is A.Select:
                steps.append(("s", e, e))
                e = e.base
            elif c is A.Member:
                steps.append(("m", e.name, e))
                e = e.base
            else:
                break
        steps.reverse()
        return e, steps

    def norm_idx(self, sig: SigSpec, dim, o=None) -> SigSpec:
        """0-based element position of an index signal for a dimension."""
        l, r = dim
        n = abs(l - r) + 1
        bits = max(1, (n - 1).bit_length())
        W = max(sig.width, bits)
        sig = sig.zext(W) if sig.width < W else sig
        if l >= r:
            if r == 0:
                return sig
            return self.b.sub(sig, SigSpec.const(r & mask(W), W), W, origin=o)
        return self.b.sub(SigSpec.const(r & mask(W), W), sig, W, origin=o)

    def resolve_ref(self, e, need: bool):
        """Resolve a select chain rooted at a signal. ``need`` False skips dynamic index logic."""
        root, steps = self._chain(e)
        r = Ref()
        o = self.org(e.loc)
        k = 0
        n = len(steps)
        sym = None
        if isinstance(root, A.Ident):
            sym = self.lookup(root.name)
            if sym is None:
                if n and steps[0][0] == "m":
                    self.diag("UNSUPPORTED_CONSTRUCT", f"hierarchical reference '{root.name}.{steps[0][1]}' "
                              "is not supported", e, True)
                else:
                    self.unresolved(root.name, root)
                return None
        elif isinstance(root, A.ScopedName):
            sym = self.lookup_scoped(root)
            if sym is None:
                return None
        else:
            v = self.val(root)
            r.rootsig = v.sig
            r.pt = vec(v.w, v.signed)
            r.w = v.w
            sym = None
        # generate-block / interface scoping
        while sym is not None and k < n:
            if isinstance(sym, KSym):
                if sym.arr is not None:
                    if steps[k][0] != "i":
                        break
                    idx = self.cint(steps[k][1], None)
                    sc = sym.arr.get(idx)
                    if sc is None or k + 1 >= n or steps[k + 1][0] != "m":
                        self.diag("UNSUPPORTED_CONSTRUCT", "hierarchical reference into generate block", e, True)
                        return None
                    sym = sc.syms.get(steps[k + 1][1])
                    k += 2
                else:
                    if steps[k][0] != "m":
                        break
                    sym = sym.scope.syms.get(steps[k][1])
                    k += 1
                if sym is None:
                    self.diag("UNRESOLVED_NAME", "name not found in generate block", e, True)
                    return None
            elif isinstance(sym, ISym):
                if steps[k][0] != "m":
                    break
                sym = sym.members.get(steps[k][1])
                k += 1
                if sym is None:
                    self.diag("UNRESOLVED_NAME", "interface member not found", e, True)
                    return None
            else:
                break
        if sym is not None:
            if isinstance(sym, PSym) and sym.arr is not None and k < n and steps[k][0] == "i" and sym.pt.fields:
                # element of an array parameter of structs (``Cfg[i].field``): the element, then the packed steps
                r.rootsig = self._arr_read(sym, steps[k][2])
                r.pt = sym.pt
                r.w = sym.pt.w
                k += 1
            elif isinstance(sym, PSym):
                r.rootsig = sym.val.sig
                r.pt = sym.pt if sym.pt is not None and sym.pt.w == sym.val.w else vec(sym.val.w, sym.val.signed)
                r.w = r.pt.w
            elif isinstance(sym, VSym):
                r.vsym = sym
                r.pt = sym.pt
                r.w = sym.pt.w
                ud = sym.udims
                if ud:
                    # consume unpacked dimensions
                    use = []
                    while len(use) < len(ud) and k < n and steps[k][0] == "i":
                        use.append(steps[k])
                        k += 1
                    if len(use) < len(ud):
                        if not use and not need:
                            r.net = sym.net
                            return r
                        self.diag("UNSUPPORTED_CONSTRUCT", f"whole-array use of '{sym.name}'", e, True)
                        return None
                    self._unpacked_index(r, sym, use, o, need)
                else:
                    r.net = sym.net
                    if r.net is None and need:      # type-only queries (``$bits(port.field)``) need no storage
                        self.diag("UNSUPPORTED_CONSTRUCT", f"'{sym.name}' has no storage here", e, True)
                        return None
            else:
                self.diag("UNSUPPORTED_CONSTRUCT", f"'{root.name if hasattr(root, 'name') else '?'}' "
                          "cannot be selected", e, True)
                return None
        # packed steps
        pt = r.pt
        off = 0
        dyn = []
        w = r.w
        while k < n:
            kind, x, node = steps[k]
            k += 1
            if kind == "m":
                if pt.fields and x in pt.fields:
                    foff, fpt = pt.fields[x]
                    off += foff
                    pt = fpt
                    w = fpt.w
                else:
                    if pt.enum is not None or x in ("size", "num", "first", "last"):
                        self.diag("UNSUPPORTED_CONSTRUCT", f"method/member '{x}'", node, True)
                    else:
                        self.diag("UNRESOLVED_NAME", f"unknown member '{x}'", node, True)
                    return None
            elif kind == "i":
                dim = pt.dims[0] if pt.dims else (pt.w - 1, 0)
                size = abs(dim[0] - dim[1]) + 1
                ew = pt.elem_width() if pt.dims else 1
                ept = pt.elem()
                v = self.val(x)
                c = v.sig.const_value()
                if c is not None and not c.xmask:
                    i = c.as_signed() if v.signed else c.value
                    p = dim_pos(i, dim)
                    if p < 0 or p >= size:
                        r.oob = True
                        p = 0
                    off += p * ew
                else:
                    dyn.append((self.norm_idx(v.sig, dim, o) if need else None, ew, size))
                w = ew
                pt = ept
            else:
                sel = x
                dim = pt.dims[0] if pt.dims else (pt.w - 1, 0)
                size = abs(dim[0] - dim[1]) + 1
                ew = pt.elem_width() if pt.dims else 1
                lft, rgt = dim
                if sel.mode == ":":
                    a = self.cint(sel.left, None)
                    bb = self.cint(sel.right, None)
                    if a is None or bb is None:
                        self.diag("UNRESOLVED_PARAM", "part-select bounds are not constant", node, True)
                        return None
                    p1, p2 = dim_pos(a, dim), dim_pos(bb, dim)
                    rev = p1 < p2
                    if rev:
                        # reversed part-select (``f[31:32]``, typically ``[N*W-1:W]`` with N = 1): illegal in
                        # IEEE 1800, taken swapped like Yosys; never a null range in Verilog
                        p1, p2 = p2, p1
                    cnt = p1 - p2 + 1
                    if rev or p2 < 0 or p1 >= size:
                        ln = e.loc[1] if isinstance(e.loc, tuple) and len(e.loc) > 1 else "?"
                        self.assume(f"part-select [{a}:{bb}] of declared range [{lft}:{rgt}] at line {ln}"
                                    + ("; reversed, taken swapped" if rev else "")
                                    + ("; out of range: read as unknown, write clamped"
                                       if (p2 < 0 or p1 >= size) else ""))
                    if p2 < 0 or p1 >= size:
                        r.oob = True
                        p2 = max(0, min(p2, size - 1))
                        cnt = max(1, min(cnt, size - p2))
                    off += p2 * ew
                else:
                    cnt = self.cint(sel.right, None)
                    if cnt is not None and cnt > (1 << 20):
                        cnt = 1 << 20
                    if cnt is None or cnt < 1:
                        self.diag("UNRESOLVED_PARAM", "indexed part-select width is not constant", node, True)
                        return None
                    v = self.val(sel.left)
                    c = v.sig.const_value()
                    plus = sel.mode == "+:"
                    if c is not None and not c.xmask:
                        i = c.as_signed() if v.signed else c.value
                        if lft >= rgt:
                            lowp = (i - rgt) if plus else (i - cnt + 1 - rgt)
                        else:
                            lowp = (rgt - i - cnt + 1) if plus else (rgt - i)
                        if lowp < 0 or lowp + cnt > size:
                            r.oob = True
                            lowp = max(0, min(lowp, size - cnt)) if size >= cnt else 0
                        off += lowp * ew
                    elif need:
                        W = max(v.w, max(1, size.bit_length())) + 1
                        sg = v.sig.zext(W)
                        bld = self.b
                        if lft >= rgt:
                            adj = -rgt if plus else -(cnt - 1) - rgt
                            low = bld.add(sg, SigSpec.const(adj & mask(W), W), W, origin=o) if adj else sg
                        else:
                            adj = rgt - (cnt - 1) if plus else rgt
                            low = bld.sub(SigSpec.const(adj & mask(W), W), sg, W, origin=o)
                        dyn.append((low, ew, max(1, size - cnt + 1)))
                    else:
                        dyn.append((None, ew, max(1, size - cnt + 1)))
                w = cnt * ew
                if pt.dims:
                    pt = PType(w, False, ((cnt - 1, 0),) + pt.dims[1:], pt.base)
                else:
                    pt = vec(w)
        r.off = off
        r.w = w
        r.pt = pt
        r.dyn = dyn
        return r

    def _unpacked_index(self, r: Ref, sym: VSym, use, o, need: bool):
        """Resolve the unpacked-dimension part of a select chain."""
        ud = sym.udims
        sizes = [abs(d[0] - d[1]) + 1 for d in ud]
        flat = 0
        dynterms = []
        stride = 1
        strides = []
        for sz in reversed(sizes):
            strides.append(stride)
            stride *= sz
        strides.reverse()
        total = stride
        for (kind, x, node), dim, st in zip(use, ud, strides):
            v = self.val(x)
            c = v.sig.const_value()
            lo = min(dim)
            if c is not None and not c.xmask:
                i = c.as_signed() if v.signed else c.value
                p = i - lo
                if p < 0 or p >= abs(dim[0] - dim[1]) + 1:
                    r.oob = True
                    p = 0
                flat += p * st
            else:
                dynterms.append((self.norm_idx(v.sig, (max(dim), lo), o) if need else None, st))
        if sym.mem is not None:
            r.mem = sym.mem
            if need:
                if not dynterms:
                    bits = max(1, (total - 1).bit_length())
                    r.addr = SigSpec.const(flat, bits)
                else:
                    bits = max(1, (total - 1).bit_length())
                    acc = None
                    for sg, st in dynterms:
                        t = sg.zext(bits) if sg.width < bits else sg.slice(0, bits)
                        if st != 1:
                            t = self.b.mul(t, SigSpec.const(st, bits), bits, origin=o)
                        acc = t if acc is None else self.b.add(acc, t, bits, origin=o)
                    if flat:
                        acc = self.b.add(acc, SigSpec.const(flat, bits), bits, origin=o)
                    r.addr = acc
        else:
            if not dynterms:
                r.net = self.elem_net(sym, flat)
            else:
                if total > 4096:
                    self.diag("LIMIT_REACHED", f"dynamic index into array '{sym.name}' with {total} elements "
                              "is not supported", use[0][2], True)
                    r.net = self.elem_net(sym, 0)
                    return
                r.elems = [self.elem_net(sym, i) for i in range(total)]
                if need:
                    bits = max(1, (total - 1).bit_length())
                    acc = None
                    for sg, st in dynterms:
                        t = sg.zext(bits) if sg.width < bits else sg.slice(0, bits)
                        if st != 1:
                            t = self.b.mul(t, SigSpec.const(st, bits), bits, origin=o)
                        acc = t if acc is None else self.b.add(acc, t, bits, origin=o)
                    if flat:
                        acc = self.b.add(acc, SigSpec.const(flat, bits), bits, origin=o)
                    r.eidx = acc

    def net_array_rom(self, sym: VSym) -> list | None:
        """Contents when a variable-index read of net array ``sym`` is a ROM (module elaborator only)."""
        return None

    def read_ref(self, r: Ref) -> SigSpec:
        b = self.b
        if r.oob and not (r.dyn):
            return SigSpec.x(r.w)
        if r.mem is not None:
            if r.cache is None:
                k = (id(r.mem), r.addr.key())
                rp = self.mem_ports.get(k)
                if rp is None:
                    rp = self.mem_ports[k] = r.mem.read(r.addr, origin=self.b.origin)
                r.cache = rp
            base = r.cache
        elif r.elems is not None:
            rom = self.net_array_rom(r.vsym) if r.vsym is not None else None
            if rom is not None:
                ab = r.eidx.width
                pad = rom + [0] * ((1 << ab) - len(rom)) if len(rom) < (1 << ab) else rom
                base = b.rom(r.eidx, pad, r.vsym.pt.w, origin=b.origin)
            else:
                arms = [self.rd_var(r.vsym, n) for n in r.elems]
                base = b.index_mux(arms, r.eidx)
        elif r.net is not None:
            vs = r.vsym
            if vs is not None and vs.const_sig is not None and r.net is vs.net:
                base = vs.const_sig
            else:
                base = self.rd_var(vs, r.net)
        else:
            base = r.rootsig
        if r.w == 0:
            return SigSpec(())
        return self._dyn_read(base, r.off, r.w, r.dyn)

    def _dyn_read(self, base: SigSpec, off: int, w: int, dyn) -> SigSpec:
        if w < 1 or off < 0 or off >= base.width:
            # only reachable when a type was clamped at the net-width limit (offsets then exceed the net)
            return SigSpec.x(max(w, 0))
        if not dyn:
            if off + w > base.width:
                return SigSpec.x(w)
            return base.slice(off, w)
        b = self.b
        idx, stride, count = dyn[0]
        if idx is None:
            return SigSpec.x(w)
        rest = dyn[1:]
        if count > _MAXDYN:
            count = _MAXDYN
        if stride == 1 and not rest and w > 1 and count > 2 and base.width - off >= w:
            span = min(base.width - off, w + count - 1)
            win = base.slice(off, span)
            sh = b.shr(win, idx, span)
            return sh.slice(0, w)
        arms = [self._dyn_read(base, off + j * stride, w, rest) for j in range(count)
                if off + j * stride + w <= base.width]
        if not arms:
            return SigSpec.x(w)
        return b.index_mux(arms, idx)

    # ------------------------------------------------------------------ lvalues / stores
    def lvalue_parts(self, e):
        """Refs of an lvalue expression, most significant part first."""
        if isinstance(e, A.Concat):
            out = []
            for it in e.items:
                out.extend(self.lvalue_parts(it))
            return out
        if isinstance(e, A.Streaming):
            return self._stream_lvalue(e)
        if isinstance(e, A.Ident):
            sym = self.lookup(e.name)
            if sym is None:
                sym = self.implicit_net(e)
            if sym is None:
                self.diag("UNRESOLVED_NAME", f"assignment to undeclared identifier '{e.name}' skipped", e, True)
                return [None]
            if isinstance(sym, VSym) and sym.udims:
                refs = self._array_refs(sym, e)
                return refs if refs is not None else [None]
            if not isinstance(sym, VSym) or sym.net is None:
                self.diag("UNSUPPORTED_CONSTRUCT", f"'{e.name}' is not assignable", e, True)
                return [None]
            r = Ref()
            r.vsym = sym
            r.net = self.store_net(sym)
            r.pt = sym.pt
            r.w = sym.pt.w
            return [r]
        if isinstance(e, (A.Index, A.Select, A.Member)):
            root, _ = self._chain(e)
            if isinstance(root, A.Ident) and self.lookup(root.name) is None:
                self.implicit_net(root)
            r = self.resolve_ref(e, True)
            if r is None:
                return [None]
            if r.vsym is not None and r.net is r.vsym.net:
                r.net = self.store_net(r.vsym)
            return [r]
        self.diag("UNSUPPORTED_CONSTRUCT", "unsupported assignment target", e, True)
        return [None]

    def store_net(self, sym: VSym):
        return sym.wired if sym.wired is not None else sym.net

    def implicit_net(self, e: A.Ident):
        return None

    def part_width(self, parts) -> int:
        return sum((p.w if p is not None else 0) for p in parts)
