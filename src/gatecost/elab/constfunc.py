"""Fast path for functions called with all-constant arguments.

A Verilog function whose body only uses plain integer constructs (loops, ``if``/``case``, integer / ``reg`` /
bit-vector arithmetic, bit and part selects, concatenation, arrays of regs, calls of other such functions) is
translated once into a Python function over plain ``int`` values; a call with constant arguments then runs as
ordinary Python instead of being symbolically lowered statement by statement through ``LowerBlock``.

The translation mirrors the width / signedness rules of the general path (``ExprMixin.ty`` / ``bv``): every
expression is compiled for its context width and signedness, values are kept as non-negative integers masked to
their width, and a signed interpretation is applied where the operation needs one.

The fast path is *strictly 2-state*.  Anything that would make the general path produce an unknown (``x``) bit
-- a read of an uninitialised variable, an out-of-range select, a division by zero, a loop that exceeds the
unroll limit -- raises inside the generated code, the call is abandoned and the general path runs instead, so
both paths always agree on every value the fast path produces.  Constructs the translator does not know make
the whole function ineligible (``None`` is cached on the function symbol).

Results are memoised per function and per argument tuple; the memo and the compiled code are shared between all
functions (for instance, those of different instances of one module) whose generated source is identical.
"""
from __future__ import annotations

import hashlib
import os

from gatecost.frontend.verilog import ast as A

from .decls import _ATOMS, pack
from .expr import parse_number
from .symbols import BIT, PSym, PType, V, VSym, dim_pos, vec  # noqa: F401  (VSym: documentation of what is rejected)

_DEBUG = bool(os.environ.get("GATECOST_DEBUG_FASTFUNC"))
_MAXTOTAL = 1 << 16          # elements of one unpacked array
_MEMO_MAX = 200_000


class Unsup(Exception):
    """Compile time: the function is outside the supported subset."""


class Bail(Exception):
    """Run time: the call cannot be answered by the 2-state fast path."""


FAIL = object()
STATS = {"calls": 0, "fallbacks": 0}       # diagnostics for the tests: fast-path answers / abandoned calls
#: calls abandoned on an unexpected exception of the generated code (a translator bug: the general
#: interpreter still answers exactly; ``GATECOST_DEBUG_FASTFUNC`` re-raises it instead)
INTERNAL_ERRORS = {"count": 0}
#: run-time exceptions by which generated code gives a call back to the general interpreter, by design: an
#: explicit bail-out, 2-state arithmetic that has no answer (division by zero, negative shift, huge value), an
#: index outside a Python list, deep recursion of nested calls
_FAST_MISS: tuple[type[BaseException], ...] = (Bail, ArithmeticError, LookupError, ValueError, RecursionError,
                                               MemoryError)


class _Undef:
    """Value of an uninitialised variable: every use raises :class:`Bail`."""

    __slots__ = ()

    def _bail(self, *a):
        raise Bail("uninitialised value")

    for _n in ("add", "sub", "mul", "and", "or", "xor", "lshift", "rshift", "floordiv", "mod", "pow", "truediv"):
        locals()[f"__{_n}__"] = _bail
        locals()[f"__r{_n}__"] = _bail
    for _n in ("neg", "pos", "invert", "int", "index", "bool", "eq", "ne", "lt", "le", "gt", "ge", "len", "float"):
        locals()[f"__{_n}__"] = _bail
    del _n
    __hash__ = object.__hash__


_U = _Undef()


# ------------------------------------------------------------------------------------------ runtime helpers
def _sx(c, S, F):
    return c | F if c & S else c


def _sg(c, S, T):
    return c - T if c & S else c


def _shl(v, n, w, M):
    return (v << n) & M if n < w else 0


def _sar(v, n, w, S, T, M):
    x = v - T if v & S else v
    return (x >> (n if n < w else w)) & M


def _div(a, b, w, s):
    if b == 0:
        raise Bail("division by zero")
    if s:
        S, T = 1 << (w - 1), 1 << w
        x = a - T if a & S else a
        y = b - T if b & S else b
        q = abs(x) // abs(y)
        if (x < 0) != (y < 0):
            q = -q
        return q & (T - 1)
    return a // b


def _mod(a, b, w, s):
    if b == 0:
        raise Bail("division by zero")
    if s:
        S, T = 1 << (w - 1), 1 << w
        x = a - T if a & S else a
        y = b - T if b & S else b
        q = abs(x) // abs(y)
        if (x < 0) != (y < 0):
            q = -q
        return (x - q * y) & (T - 1)
    return a % b


def _pow(a, n, mod):
    if n < 0:
        raise Bail("negative exponent")
    return pow(a, n, mod)


def _ck(p, n):
    if 0 <= p < n:
        return p
    raise Bail("index out of range")


def _clog2(x):
    return (x - 1).bit_length() if x > 1 else 0


def _par(v):
    return bin(v).count("1") & 1


def _rd(v, k, rm):
    """Read of a partially written variable: every bit read must have been written."""
    if k & rm == rm:
        return v
    raise Bail("read of unwritten bits")


_HELPERS = {"_sx": _sx, "_sg": _sg, "_shl": _shl, "_sar": _sar, "_div": _div, "_mod": _mod, "_pow": _pow,
            "_ck": _ck, "_clog2": _clog2, "_par": _par, "_rd": _rd, "_U": _U, "_Bail": Bail}


class FastFn:
    """A compiled constant function with its result memo."""

    __slots__ = ("fn", "memo", "pw", "name", "psigned", "rt", "ast", "key", "iters", "total")

    def __init__(self, fn, pw, name, iters, total):
        self.fn = fn
        self.iters = iters        # one-element list: loop iterations executed by the generated code so far
        self.total = total        # iteration budget of all calls together; beyond it every call falls back
        self.memo = {}
        self.pw = pw             # port widths, in order
        self.name = name

    def run(self, args):
        m = self.memo
        r = m.get(args, None)
        if r is None:
            if self.iters[0] > self.total:
                return FAIL
            try:
                r = self.fn(*args)
                if r is _U:
                    r = FAIL
                else:
                    r = int(r)
            except _FAST_MISS:
                r = FAIL            # by design: the general interpreter answers this call
            except Exception as ex:  # noqa: BLE001 - translator bug: the exact general path still answers
                if _DEBUG:
                    raise
                INTERNAL_ERRORS["count"] += 1
                r = FAIL
            if len(m) >= _MEMO_MAX:
                m.clear()
            m[args] = r
        return r

    def callm(self, *args):
        r = self.run(args)
        if r is FAIL:
            raise Bail("nested call failed")
        return r


def _mask(w):
    return (1 << w) - 1


def _chain(e):
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


class _Var:
    __slots__ = ("name", "pt", "udims", "total", "strides", "kn")

    def __init__(self, name, pt, udims=()):
        self.name = name
        self.kn = None          # name of the "written bits" shadow of a variable that is assigned by parts
        self.pt = pt
        self.udims = udims
        t = 1
        strides = []
        for l, r in reversed(udims):
            strides.append(t)
            t *= abs(l - r) + 1
        strides.reverse()
        self.total = t
        self.strides = strides


class _Ref:
    __slots__ = ("var", "psym", "flat", "off_c", "off_d", "w", "pt", "basew")


_ARITH = ("+", "-", "*", "/", "%")
_BITW = ("&", "|", "^", "~^", "^~")
_SHIFT = ("<<", ">>", "<<<", ">>>")
_CMP = ("<", "<=", ">", ">=")
_EQ = ("==", "!=", "===", "!==", "==?", "!=?")
_COMPOUND = frozenset(["+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=", "<<<=", ">>>="])


def _loop_ctrl(body):
    """(has break, has continue) for a loop body, not descending into nested loops."""
    brk = cnt = False
    stack = [body]
    while stack:
        n = stack.pop()
        if isinstance(n, A.Break):
            brk = True
        elif isinstance(n, A.Continue):
            cnt = True
        elif isinstance(n, (A.For, A.While, A.DoWhile, A.Repeat, A.Forever, A.Foreach)):
            continue
        elif isinstance(n, A.Node):
            stack.extend(A.children(n))
    return brk, cnt


class _Compiler:
    def __init__(self, ev, f):
        self.ev = ev
        self.ctx = ev.ctx
        self.f = f
        self.ast = f.ast
        self.fscope = f.scope
        self.lim = ev.ctx.limits
        self.lines: list[str] = []
        self.ind = 1
        self.scopes: list[dict] = [{}]
        self.nvar = 0
        self.ntmp = 0
        self.ns: dict = {}
        self.nested: list = []
        self._tm: dict = {}
        self._refs: dict = {}
        self._keep: list = []
        self.loops: list = []
        self.partial: set = set()
        self.retvar: _Var | None = None
        self.rt: PType | None = None

    # ------------------------------------------------------------------ plumbing
    def emit(self, line):
        self.lines.append("    " * self.ind + line)

    def tmp(self, p="_t"):
        self.ntmp += 1
        return f"{p}{self.ntmp}"

    def lookup(self, name):
        for sc in reversed(self.scopes):
            v = sc.get(name)
            if v is not None:
                return v
        return self.fscope.lookup(name)

    def new_var(self, name, pt, udims=(), shadow=False):
        self.nvar += 1
        v = _Var(f"v{self.nvar}_{''.join(ch if ch.isalnum() else '_' for ch in name)}", pt, tuple(udims))
        if v.total > _MAXTOTAL:
            raise Unsup("array too large")
        if shadow:
            v.kn = "k" + v.name[1:]
        self.scopes[-1][name] = v
        return v

    # ------------------------------------------------------------------ constants / purity
    def is_pure(self, e) -> bool:
        """True when ``e`` only depends on parameters and literals."""
        for n in A.walk(e):
            c = type(n)
            if c is A.Ident:
                if self.lookup_local(n.name) is not None:
                    return False
            elif c in (A.Call, A.Streaming, A.AssignPattern, A.Inside, A.Member):
                return False
        return True

    def lookup_local(self, name):
        for sc in reversed(self.scopes):
            v = sc.get(name)
            if v is not None:
                return v
        return None

    def const_eval(self, e):
        """``(value, w, signed)`` of a pure expression (the value signed when the expression is)."""
        if not self.is_pure(e):
            raise Unsup("expression is not constant")
        w, s = self.ty(e)
        code = self.cg(e, w, s)
        try:
            v = eval(code, dict(_HELPERS))
        except Exception as ex:                    # noqa: BLE001
            raise Unsup(f"constant evaluation failed: {ex}") from None
        if s and v >> (w - 1) & 1:
            v -= 1 << w
        return v, w, s

    def cint(self, e):
        return self.const_eval(e)[0]

    # ------------------------------------------------------------------ types
    def dims(self, dims):
        out = []
        for d in dims or ():
            if d.kind == "range":
                out.append((self.cint(d.left), self.cint(d.right)))
            elif d.kind == "size":
                n = self.cint(d.left)
                if n < 1:
                    raise Unsup("array size")
                out.append((0, n - 1))
            else:
                raise Unsup(f"dimension kind {d.kind}")
        return out

    def rtype(self, dt) -> PType:
        if dt is None:
            return BIT
        c = type(dt)
        if c is A.IntType:
            kind = dt.kind
            if kind in _ATOMS:
                w, sg = _ATOMS[kind]
                if dt.signed is not None:
                    sg = bool(dt.signed)
                base = PType(w, sg, ((w - 1, 0),), BIT)
                dims = self.dims(dt.dims)
                return pack(dims, base, sg) if dims else base
            sg = bool(dt.signed)
            dims = self.dims(dt.dims)
            return pack(dims, BIT, sg) if dims else (BIT if not sg else PType(1, True, (), BIT))
        if c is A.ImplicitType:
            sg = bool(dt.signed)
            dims = self.dims(dt.dims)
            return pack(dims, BIT, sg) if dims else (BIT if not sg else PType(1, True, (), BIT))
        raise Unsup(f"type {c.__name__}")

    def ty(self, e):
        k = id(e)
        r = self._tm.get(k)
        if r is None:
            r = self._tm[k] = self._ty(e)
            self._keep.append(e)
        return r

    def _param(self, sym):
        if sym.real is not None or sym.arr is not None:
            raise Unsup("real / array parameter")
        c = sym.val.sig.const_value()
        if c is None or c.xmask or c.width == 0:
            raise Unsup("parameter with unknown bits")
        return c

    def _ty(self, e):
        c = type(e)
        if c is A.Number:
            _v, xm, w, s = parse_number(e)
            if xm:
                raise Unsup("literal with x/z bits")
            return (w, s)
        if c is A.Ident:
            sym = self.lookup(e.name)
            if isinstance(sym, _Var):
                if sym.udims:
                    raise Unsup("whole array")
                return (sym.pt.w, sym.pt.signed)
            if isinstance(sym, PSym):
                self._param(sym)
                return (sym.val.sig.width, sym.val.signed)
            raise Unsup(f"name {e.name}")
        if c is A.Binary:
            op = e.op
            if op in _SHIFT or op == "**":
                self.ty(e.right)
                return self.ty(e.left)
            if op in _ARITH or op in _BITW:
                w1, s1 = self.ty(e.left)
                w2, s2 = self.ty(e.right)
                return (max(w1, w2), s1 and s2)
            if op in _CMP or op in _EQ or op in ("&&", "||"):
                self.ty(e.left)
                self.ty(e.right)
                return (1, False)
            raise Unsup(f"binary {op}")
        if c is A.Unary:
            op = e.op
            if op in ("+", "-", "~"):
                return self.ty(e.operand)
            if op in ("!", "&", "|", "^", "~&", "~|", "~^", "^~"):
                self.ty(e.operand)
                return (1, False)
            raise Unsup(f"unary {op}")
        if c is A.Ternary:
            self.ty(e.cond)
            w1, s1 = self.ty(e.then)
            w2, s2 = self.ty(e.else_)
            return (max(w1, w2), s1 and s2)
        if c is A.Concat:
            tot = 0
            for i in e.items:
                tot += self.ty(i)[0]
            if tot < 1:
                raise Unsup("empty concat")
            return (tot, False)
        if c is A.Replicate:
            n = self.cint(e.count)
            if n < 0:
                raise Unsup("negative replication")
            tot = sum(self.ty(i)[0] for i in e.items)
            if n * tot < 1 or n * tot > (1 << 20):
                raise Unsup("replication size")
            return (n * tot, False)
        if c is A.Index or c is A.Select:
            r = self.ref(e)
            return (r.w, bool(r.pt.signed))
        if c is A.Fill:
            if e.bit not in "01":
                raise Unsup("x/z fill")
            return (1, False)
        if c is A.SysCall:
            n = e.name
            if n in ("$signed", "$unsigned"):
                if len(e.args) != 1 or not isinstance(e.args[0], A.Expr):
                    raise Unsup("$signed arguments")
                return (self.ty(e.args[0])[0], n == "$signed")
            if n in ("$clog2", "$countones"):
                if len(e.args) != 1 or not isinstance(e.args[0], A.Expr):
                    raise Unsup(f"{n} arguments")
                self.ty(e.args[0])
                return (32, True)
            raise Unsup(f"system function {n}")
        if c is A.Call:
            fn, rt = self.callee(e)
            return (rt.w, bool(rt.signed))
        if c is A.Cast:
            t = e.target
            if isinstance(t, A.OtherType) and t.kind in ("signed", "unsigned"):
                return (self.ty(e.expr)[0], t.kind == "signed")
            if isinstance(t, A.DataType):
                if isinstance(t, A.OtherType) or isinstance(e.expr, A.AssignPattern):
                    raise Unsup("cast")
                pt = self.rtype(t)
                self.ty(e.expr)
                return (pt.w, pt.signed)
            n = self.cint(t)
            if n <= 0:
                raise Unsup("cast size")
            return (n, self.ty(e.expr)[1])
        if c is A.String:
            return (max(1, 8 * len(e.value)), False)
        raise Unsup(f"expression {c.__name__}")

    # ------------------------------------------------------------------ expression code
    @staticmethod
    def ext(code, nw, w, s):
        """``code`` (``nw`` bits) resized to ``w`` bits, sign-extended when ``s``."""
        if w == nw:
            return code
        if w < nw:
            if code.lstrip("-").isdigit():
                return str(int(code) & _mask(w))
            return f"({code} & {_mask(w)})"
        if not s:
            return code
        S, F = 1 << (nw - 1), _mask(w) & ~_mask(nw)
        if code.isdigit():
            v = int(code)
            return str(v | F if v & S else v)
        return f"_sx({code}, {S}, {F})"

    def val(self, e):
        w, s = self.ty(e)
        return self.cg(e, w, s)

    def sval(self, e):
        """Self-determined value of ``e`` as a Python integer (signed when its type is)."""
        w, s = self.ty(e)
        code = self.cg(e, w, s)
        if s:
            return f"_sg({code}, {1 << (w - 1)}, {1 << w})"
        return code

    def assign_code(self, e, lw):
        """Value of ``e`` as the right-hand side of an assignment to ``lw`` bits."""
        if isinstance(e, A.AssignPattern):
            raise Unsup("assignment pattern")
        w, s = self.ty(e)
        W = max(lw, w)
        code = self.cg(e, W, s)
        if W != lw:
            code = f"({code} & {_mask(lw)})"
        return code

    def cg(self, e, w, s):
        """Python expression for ``e`` at context width ``w`` and propagated signedness ``s``."""
        c = type(e)
        if c is A.Number:
            val, xm, nw, _sg_ = parse_number(e)
            if xm:
                raise Unsup("literal with x/z bits")
            return self.ext(str(val), nw, w, s)
        if c is A.Ident:
            sym = self.lookup(e.name)
            if isinstance(sym, _Var):
                if sym.udims:
                    raise Unsup("whole array")
                name = sym.name if sym.kn is None else f"_rd({sym.name}, {sym.kn}, {_mask(sym.pt.w)})"
                return self.ext(name, sym.pt.w, w, s)
            if isinstance(sym, PSym):
                cv = self._param(sym)
                return self.ext(str(cv.value), cv.width, w, s)
            raise Unsup(f"name {e.name}")
        if c is A.Binary:
            return self._binary(e, w, s)
        if c is A.Unary:
            return self._unary(e, w, s)
        if c is A.Ternary:
            cond = self.val(e.cond)
            t = self.cg(e.then, w, s)
            f = self.cg(e.else_, w, s)
            return f"({t} if {cond} else {f})"
        if c is A.Fill:
            return str(_mask(w)) if e.bit == "1" else "0"
        if c is A.Concat:
            parts = []
            pos = 0
            for i in reversed(e.items):
                iw, _is = self.ty(i)
                code = self.cg(i, iw, _is)
                parts.append(f"({code} << {pos})" if pos else code)
                pos += iw
            code = "(" + " | ".join(parts) + ")"
            return self.ext(code, pos, w, False)
        if c is A.Replicate:
            n = self.cint(e.count)
            parts = []
            pos = 0
            for i in reversed(e.items):
                iw, _is = self.ty(i)
                code = self.cg(i, iw, _is)
                parts.append(f"({code} << {pos})" if pos else code)
                pos += iw
            inner = "(" + " | ".join(parts) + ")"
            if n == 0:
                return "0"
            K = ((1 << (pos * n)) - 1) // ((1 << pos) - 1)
            return self.ext(f"({inner} * {K})", pos * n, w, False)
        if c is A.Index or c is A.Select:
            r = self.ref(e)
            return self.ext(self.read(r), r.w, w, s and bool(r.pt.signed))
        if c is A.SysCall:
            return self._syscall(e, w, s)
        if c is A.Call:
            fn, rt = self.callee(e)
            code = self.call_code(e)
            return self.ext(code, rt.w, w, s and bool(rt.signed))
        if c is A.Cast:
            return self._cast(e, w, s)
        if c is A.String:
            bits = 0
            for ch in e.value:
                bits = (bits << 8) | (ord(ch) & 0xFF)
            return self.ext(str(bits), max(1, 8 * len(e.value)), w, False)
        raise Unsup(f"expression {c.__name__}")

    def _cast(self, e, w, s):
        t = e.target
        iw, isg = self.ty(e.expr)
        cw, cs = self.ty(e)
        inner = self.cg(e.expr, iw, isg)
        if isinstance(t, A.OtherType) and t.kind in ("signed", "unsigned"):
            return self.ext(inner, iw, w, s and cs)
        return self.ext(self.ext(inner, iw, cw, isg), cw, w, s and cs)

    def _syscall(self, e, w, s):
        n = e.name
        a0 = e.args[0]
        if n in ("$signed", "$unsigned"):
            aw, asg = self.ty(a0)
            return self.ext(self.cg(a0, aw, asg), aw, w, s)
        if n == "$clog2":
            return self.ext(f"_clog2({self.sval(a0)})", 32, w, s)
        code = f"bin({self.val(a0)}).count('1')"            # $countones
        return self.ext(code, 32, w, s)

    def _unary(self, e, w, s):
        op = e.op
        if op == "-":
            return f"((-{self.cg(e.operand, w, s)}) & {_mask(w)})"
        if op == "+":
            return self.cg(e.operand, w, s)
        if op == "~":
            return f"({self.cg(e.operand, w, s)} ^ {_mask(w)})"
        ow, _os = self.ty(e.operand)
        v = self.cg(e.operand, ow, _os)
        if op == "!":
            r = f"(0 if {v} else 1)"
        elif op == "&":
            r = f"(1 if {v} == {_mask(ow)} else 0)"
        elif op == "~&":
            r = f"(0 if {v} == {_mask(ow)} else 1)"
        elif op == "|":
            r = f"(1 if {v} else 0)"
        elif op == "~|":
            r = f"(0 if {v} else 1)"
        elif op == "^":
            r = f"_par({v})"
        else:                                               # ~^ ^~
            r = f"(_par({v}) ^ 1)"
        return r

    def _binary(self, e, w, s):
        op = e.op
        M = _mask(w)
        if op in _ARITH:
            l = self.cg(e.left, w, s)
            r = self.cg(e.right, w, s)
            if op == "/":
                return f"_div({l}, {r}, {w}, {int(s)})"
            if op == "%":
                return f"_mod({l}, {r}, {w}, {int(s)})"
            return f"(({l} {op} {r}) & {M})"
        if op in _BITW:
            l = self.cg(e.left, w, s)
            r = self.cg(e.right, w, s)
            if op in ("~^", "^~"):
                return f"(({l} ^ {r}) ^ {M})"
            return f"({l} {op} {r})"
        if op in _SHIFT:
            l = self.cg(e.left, w, s)
            amt = self.val(e.right)
            if op in ("<<", "<<<"):
                return f"_shl({l}, {amt}, {w}, {M})"
            if op == ">>>" and s:
                return f"_sar({l}, {amt}, {w}, {1 << (w - 1)}, {1 << w}, {M})"
            return f"({l} >> {amt})"
        if op == "**":
            base = self.cg(e.left, w, s)
            return f"_pow({base}, {self.sval(e.right)}, {1 << w})"
        if op in ("&&", "||"):
            a, b = self.val(e.left), self.val(e.right)
            return f"(1 if ({a} {'and' if op == '&&' else 'or'} {b}) else 0)"
        w1, s1 = self.ty(e.left)
        w2, s2 = self.ty(e.right)
        cw, cs = max(w1, w2), s1 and s2
        l = self.cg(e.left, cw, cs)
        r = self.cg(e.right, cw, cs)
        if op in _CMP:
            if cs:
                S, T = 1 << (cw - 1), 1 << cw
                l, r = f"_sg({l}, {S}, {T})", f"_sg({r}, {S}, {T})"
            return f"(1 if {l} {op} {r} else 0)"
        neg = op in ("!=", "!==", "!=?")
        return f"(1 if {l} {'!=' if neg else '=='} {r} else 0)"

    # ------------------------------------------------------------------ references
    def ref(self, e) -> _Ref:
        r = self._refs.get(id(e))
        if r is not None:
            return r
        r = self._refs[id(e)] = self._ref(e)
        self._keep.append(e)
        return r

    def _index_value(self, x):
        """Constant int, or code of the (signed) value of an index expression."""
        if self.is_pure(x):
            return self.cint(x), None
        return None, self.sval(x)

    def _ref(self, e) -> _Ref:
        root, steps = _chain(e)
        if type(root) is not A.Ident:
            raise Unsup("select of a non-name")
        sym = self.lookup(root.name)
        r = _Ref()
        r.var = r.psym = None
        r.flat = None
        r.off_c = 0
        r.off_d = []
        n = len(steps)
        k = 0
        if isinstance(sym, _Var):
            r.var = sym
            pt = sym.pt
            ud = sym.udims
            if ud:
                use = []
                while len(use) < len(ud) and k < n and steps[k][0] == "i":
                    use.append(steps[k])
                    k += 1
                if len(use) < len(ud):
                    raise Unsup("whole-array use")
                flat_c = 0
                flat_d = []
                for (_kind, x, _nd), dim, st in zip(use, ud, sym.strides):
                    size = abs(dim[0] - dim[1]) + 1
                    lo = min(dim)
                    ci, code = self._index_value(x)
                    if ci is not None:
                        p = ci - lo
                        if not 0 <= p < size:
                            raise Unsup("constant index out of range")
                        flat_c += p * st
                    else:
                        t = f"_ck({code} - {lo}, {size})" if lo else f"_ck({code}, {size})"
                        flat_d.append(f"{t} * {st}" if st != 1 else t)
                if flat_d:
                    r.flat = " + ".join(flat_d + ([str(flat_c)] if flat_c else []))
                else:
                    r.flat = str(flat_c)
        elif isinstance(sym, PSym):
            self._param(sym)
            r.psym = sym
            pt = sym.pt if sym.pt is not None and sym.pt.w == sym.val.sig.width else vec(sym.val.sig.width,
                                                                                         sym.val.signed)
        else:
            raise Unsup(f"name {root.name}")
        r.basew = pt.w
        w = pt.w
        while k < n:
            kind, x, node = steps[k]
            k += 1
            if kind == "m":
                raise Unsup("member select")
            dim = pt.dims[0] if pt.dims else (pt.w - 1, 0)
            size = abs(dim[0] - dim[1]) + 1
            ew = pt.elem_width() if pt.dims else 1
            lft, rgt = dim
            if kind == "i":
                ept = pt.elem()
                ci, code = self._index_value(x)
                if ci is not None:
                    p = dim_pos(ci, dim)
                    if not 0 <= p < size:
                        raise Unsup("constant index out of range")
                    r.off_c += p * ew
                else:
                    pos = f"({code} - {rgt})" if lft >= rgt else f"({rgt} - {code})"
                    t = f"_ck({pos}, {size})"
                    r.off_d.append(f"{t} * {ew}" if ew != 1 else t)
                w = ew
                pt = ept
            else:
                sel = x
                if sel.mode == ":":
                    a, b = self.cint(sel.left), self.cint(sel.right)
                    p1, p2 = dim_pos(a, dim), dim_pos(b, dim)
                    if p1 < p2:
                        p1, p2 = p2, p1
                    cnt = p1 - p2 + 1
                    if p2 < 0 or p1 >= size:
                        raise Unsup("part select out of range")
                    r.off_c += p2 * ew
                else:
                    cnt = self.cint(sel.right)
                    if cnt < 1 or cnt > (1 << 20):
                        raise Unsup("indexed part-select width")
                    plus = sel.mode == "+:"
                    ci, code = self._index_value(sel.left)
                    if ci is not None:
                        i = ci
                        if lft >= rgt:
                            lowp = (i - rgt) if plus else (i - cnt + 1 - rgt)
                        else:
                            lowp = (rgt - i - cnt + 1) if plus else (rgt - i)
                        if lowp < 0 or lowp + cnt > size:
                            raise Unsup("part select out of range")
                        r.off_c += lowp * ew
                    else:
                        if lft >= rgt:
                            lowp = f"({code} - {rgt if plus else rgt + cnt - 1})"
                        else:
                            lowp = f"({rgt - cnt + 1} - {code})" if plus else f"({rgt} - {code})"
                        t = f"_ck({lowp}, {size - cnt + 1})"
                        r.off_d.append(f"{t} * {ew}" if ew != 1 else t)
                w = cnt * ew
                if pt.dims:
                    pt = PType(w, False, ((cnt - 1, 0),) + pt.dims[1:], pt.base)
                else:
                    pt = vec(w)
        r.w = w
        r.pt = pt
        return r

    def base_code(self, r):
        if r.psym is not None:
            return str(self._param(r.psym).value)
        v = r.var
        return f"{v.name}[{r.flat}]" if r.flat is not None else v.name

    def off_code(self, r):
        parts = list(r.off_d)
        if r.off_c or not parts:
            parts.append(str(r.off_c))
        return " + ".join(parts)

    def read(self, r):
        base = self.base_code(r)
        if r.var is not None and r.var.kn is not None:
            rm = _mask(r.w)
            rm = f"({rm} << ({self.off_code(r)}))" if r.off_d else str(rm << r.off_c)
            base = f"_rd({base}, {r.var.kn}, {rm})"
        if not r.off_d and r.off_c == 0 and r.w == r.basew:
            return base
        sh = f"({base} >> {self.off_code(r)})" if (r.off_d or r.off_c) else base
        return f"({sh} & {_mask(r.w)})"

    # ------------------------------------------------------------------ calls
    def callee(self, e):
        """``(FastFn, return PType)`` of a call expression."""
        k = ("callee", id(e))
        r = self._refs.get(k)
        if r is not None:
            return r
        f = e.func
        if not isinstance(f, A.Ident):
            raise Unsup("call of a non-simple name")
        from .symbols import FSym
        sym = None
        sc = self.fscope
        while sc is not None:
            sym = sc.syms.get(f.name)
            if isinstance(sym, FSym):
                break
            for pk in sc.wild:
                sym = pk.syms.get(f.name)
                if isinstance(sym, FSym):
                    break
            if isinstance(sym, FSym):
                break
            sc = sc.parent
        if not isinstance(sym, FSym):
            raise Unsup(f"function {f.name} not found")
        if sym.ast.is_task:
            raise Unsup("task call")
        if sym is self.f or id(sym) in self.ctx.cf_busy:
            raise Unsup("recursive function")
        if len(self.ctx.cf_busy) >= self.lim["call_depth"]:
            raise Unsup("call depth")
        fast = get_fast(self.ev, sym)
        if not fast:
            raise Unsup(f"callee {f.name} is not eligible")
        rt = fast.rt
        res = self._refs[k] = (fast, rt)
        self._keep.append(e)
        return res

    def call_code(self, e):
        fast, _rt = self.callee(e)
        ast = fast.ast
        bound = {}
        pos = 0
        for a in e.args:
            if isinstance(a, A.NamedArg):
                bound[a.name] = a.value
            else:
                if pos < len(ast.ports):
                    bound[ast.ports[pos].name] = a
                pos += 1
        args = []
        for p, pw, psg in zip(ast.ports, fast.pw, fast.psigned):
            a = bound.get(p.name)
            if a is None:
                a = p.default
            if a is None:
                raise Unsup("missing call argument")
            args.append(self.assign_code(a, pw))
        name = None
        for nm, fn in self.nested:
            if fn is fast:
                name = nm
        if name is None:
            name = f"_f{len(self.nested)}"
            self.nested.append((name, fast))
        return f"{name}({', '.join(args)})"

    # ------------------------------------------------------------------ statements
    def stmt(self, s):
        c = type(s)
        fn = self._DISPATCH.get(c)
        if fn is None:
            if c is A.Null:
                return
            raise Unsup(f"statement {c.__name__}")
        fn(self, s)

    def item(self, st):
        c = type(st)
        if c is A.VarDecl:
            self.declare(st)
        elif isinstance(st, A.Stmt):
            self.stmt(st)
        else:
            raise Unsup(f"declaration {c.__name__}")

    def body(self, st):
        """Emit a statement as an indented body (``pass`` when it emits nothing)."""
        self.ind += 1
        n0 = len(self.lines)
        if st is not None:
            self.stmt(st)
        if len(self.lines) == n0:
            self.emit("pass")
        self.ind -= 1

    def declare(self, vd):
        pt = self.rtype(vd.dtype)
        ud = self.dims(vd.dims)
        v = self.new_var(vd.name, pt, ud, shadow=not ud and vd.init is None and vd.name in self.partial)
        if ud:
            self.emit(f"{v.name} = [_U] * {v.total}")
        elif vd.init is not None:
            self.emit(f"{v.name} = {self.assign_code(vd.init, pt.w)}")
        elif v.kn:
            self.emit(f"{v.name} = 0")
            self.emit(f"{v.kn} = 0")
        else:
            self.emit(f"{v.name} = _U")

    def s_block(self, s):
        self.scopes.append({})
        try:
            for it in s.items:
                self.item(it)
        finally:
            self.scopes.pop()

    def lvalue(self, e):
        if not isinstance(e, (A.Ident, A.Index, A.Select)):
            raise Unsup("assignment target")
        r = self.ref(e)
        if r.var is None:
            raise Unsup("assignment to a parameter")
        return r

    def store(self, r, val):
        v = r.var
        tgt = v.name
        if r.flat is not None:
            t = self.tmp("_i")
            self.emit(f"{t} = {r.flat}")
            tgt = f"{v.name}[{t}]"
        kn = v.kn
        if not r.off_d and r.off_c == 0 and r.w == r.basew:
            self.emit(f"{tgt} = {val}")
            if kn:
                self.emit(f"{kn} = {_mask(r.basew)}")
            return
        if r.off_d:
            o = self.tmp("_o")
            self.emit(f"{o} = {self.off_code(r)}")
            self.emit(f"{tgt} = ({tgt} & ~({_mask(r.w)} << {o})) | ({val} << {o})")
            if kn:
                self.emit(f"{kn} |= {_mask(r.w)} << {o}")
        else:
            inv = ~(_mask(r.w) << r.off_c)
            sh = f" << {r.off_c}" if r.off_c else ""
            self.emit(f"{tgt} = ({tgt} & ({inv})) | (({val}){sh})")
            if kn:
                self.emit(f"{kn} |= {_mask(r.w) << r.off_c}")

    def s_assign(self, s):
        if not s.blocking:
            raise Unsup("non-blocking assignment")
        r = self.lvalue(s.lhs)
        if s.op == "=":
            rhs = s.rhs
        elif s.op in _COMPOUND:
            rhs = A.Binary(s.loc, s.op[:-1], s.lhs, s.rhs)
            self._keep.append(rhs)
        else:
            raise Unsup(f"assignment operator {s.op}")
        self.store(r, self.assign_code(rhs, r.w))

    def s_if(self, s):
        self.emit(f"if {self.val(s.cond)}:")
        self.body(s.then)
        if s.else_ is not None:
            self.emit("else:")
            self.body(s.else_)

    @staticmethod
    def _wild(lab):
        """``(value, xmask, width, signed)`` of a literal label with x/z/? bits, else None."""
        if type(lab) is A.Number:
            v, xm, nw, sg = parse_number(lab)
            if xm:
                return v, xm, nw, sg
        return None

    def label_ty(self, lab):
        wl = self._wild(lab)
        return (wl[2], wl[3]) if wl else self.ty(lab)

    def label_hit(self, sel, lab, w, sg):
        """Condition for a case label; x/z/? bits of a literal label are wildcards (as in the general path)."""
        wl = self._wild(lab)
        if wl is None:
            return f"{sel} == {self.cg(lab, w, sg)}"
        v, xm, nw, lsg = wl
        if nw < w and sg and (v | xm) >> (nw - 1) & 1:
            fill = _mask(w) & ~_mask(nw)
            v |= fill if v >> (nw - 1) & 1 else 0
            xm |= fill if xm >> (nw - 1) & 1 else 0
        km = _mask(w) & ~xm
        if km == 0:
            return "True"
        return f"(({sel} ^ {v & _mask(w)}) & {km}) == 0"

    def s_case(self, s):
        if s.inside or s.kind not in ("case", "casez", "casex"):
            raise Unsup("case variant")
        w, sg = self.ty(s.expr)
        default = None
        for it in s.items:
            if it.labels is None:
                default = it
                continue
            for lab in it.labels:
                if isinstance(lab, A.ValueRange):
                    raise Unsup("ranged case label")
                lw, ls = self.label_ty(lab)
                w, sg = max(w, lw), sg and ls
        sel = self.tmp("_s")
        self.emit(f"{sel} = {self.cg(s.expr, w, sg)}")
        first = True
        for it in s.items:
            if it.labels is None:
                continue
            conds = " or ".join(self.label_hit(sel, lab, w, sg) for lab in it.labels)
            self.emit(f"{'if' if first else 'elif'} {conds}:")
            first = False
            self.body(it.body)
        if default is not None and default.body is not None:
            if first:
                self.emit("if True:")
            else:
                self.emit("else:")
            self.body(default.body)

    # -- loops
    def _guard(self, cnt, limit):
        self.emit(f"{cnt} += 1")
        self.emit(f"if {cnt} > {limit}: raise _Bail")
        self.emit("_n += 1")
        self.emit("if _n > _BUDGET: raise _Bail")

    def ret(self, expr):
        self.emit("_C[0] += _n")
        self.emit(f"return {expr}")

    def _loop_body(self, body, ctx_kind, tail=None):
        """Emit a loop body; ``continue`` is a plain one in loops whose tail needs no code."""
        brk, cnt = _loop_ctrl(body) if body is not None else (False, False)
        wrap = cnt and tail is not None
        info = {"wrap": wrap, "flag": None}
        if wrap:
            info["flag"] = self.tmp("_b") if brk else None
            if info["flag"]:
                self.emit(f"{info['flag']} = False")
            self.emit("for _o in (0,):")
        self.loops.append(info)
        self.ind += 1 if wrap else 0
        n0 = len(self.lines)
        if body is not None:
            self.stmt(body)
        if wrap and len(self.lines) == n0:
            self.emit("pass")
        self.ind -= 1 if wrap else 0
        self.loops.pop()
        if wrap and info["flag"]:
            self.emit(f"if {info['flag']}: break")
        return wrap

    def s_for(self, s):
        self.scopes.append({})
        try:
            for st in s.init or ():
                self.item(st)
            limit = self.lim["loop_unroll"]
            cnt = self.tmp("_c")
            self.emit(f"{cnt} = 0")
            cond = self.val(s.cond) if s.cond is not None else "True"
            self.emit(f"while {cond}:")
            self.ind += 1
            self._guard(cnt, limit)
            self._loop_body(s.body, "for", tail=True)
            for st in s.step or ():
                self.item(st)
            self.ind -= 1
        finally:
            self.scopes.pop()

    def s_while(self, s):
        limit = self.lim["loop_unroll"]
        cnt = self.tmp("_c")
        self.emit(f"{cnt} = 0")
        self.emit(f"while {self.val(s.cond)}:")
        self.ind += 1
        self._guard(cnt, limit)
        self._loop_body(s.body, "while")
        self.ind -= 1

    def s_dowhile(self, s):
        limit = self.lim["loop_unroll"]
        cnt = self.tmp("_c")
        self.emit(f"{cnt} = 0")
        self.emit("while True:")
        self.ind += 1
        self._guard(cnt, limit)
        self._loop_body(s.body, "do", tail=True)
        self.emit(f"if not ({self.val(s.cond)}): break")
        self.ind -= 1

    def s_repeat(self, s):
        limit = self.lim["loop_unroll"]
        n = self.tmp("_r")
        self.emit(f"{n} = {self.sval(s.count)}")
        self.emit(f"if {n} > {limit}: raise _Bail")
        self.emit(f"for _ in range({n}):")
        self.ind += 1
        self.emit("_n += 1")
        self.emit("if _n > _BUDGET: raise _Bail")
        self._loop_body(s.body, "repeat")
        self.ind -= 1

    def s_break(self, s):
        if not self.loops:
            raise Unsup("break outside a loop")
        info = self.loops[-1]
        if info["wrap"]:
            if info["flag"]:
                self.emit(f"{info['flag']} = True")
            self.emit("break")
        else:
            self.emit("break")

    def s_continue(self, s):
        if not self.loops:
            raise Unsup("continue outside a loop")
        info = self.loops[-1]
        self.emit("break" if info["wrap"] else "continue")

    def s_return(self, s):
        if s.value is None:
            raise Unsup("return without value")
        rv = self.retvar
        self.emit(f"{rv.name} = {self.assign_code(s.value, rv.pt.w)}")
        if rv.kn:
            self.emit(f"{rv.kn} = {_mask(rv.pt.w)}")
        self.ret(rv.name)

    def s_callstmt(self, s):
        if isinstance(s.call, A.SysCall):
            return                                  # $display / $error / $finish ...: ignored, as in the general path
        raise Unsup("task / function call statement")

    _DISPATCH = {}

    # ------------------------------------------------------------------ driver
    def build(self):
        ast = self.ast
        if ast.is_task or ast.nonsynth:
            raise Unsup("task / non-synthesizable")
        self.rt = self.func_rt()
        ports = []
        pw = []
        psigned = []
        for p in ast.ports:
            if p.direction not in ("input", None) or p.dims:
                raise Unsup("non-input port")
            pt = self.rtype(p.dtype) if p.dtype is not None else BIT
            v = self.new_var(p.name, pt)
            ports.append(v.name)
            pw.append(pt.w)
            psigned.append(bool(pt.signed))
        self.partial = set()
        for it in ast.body:
            for n in A.walk(it):
                if type(n) is A.ProcAssign and type(n.lhs) in (A.Index, A.Select):
                    root, _steps = _chain(n.lhs)
                    if type(root) is A.Ident:
                        self.partial.add(root.name)
        self.retvar = rv = self.new_var(ast.name, self.rt, shadow=ast.name in self.partial)
        if rv.kn:
            self.emit(f"{rv.name} = 0")
            self.emit(f"{rv.kn} = 0")
        else:
            self.emit(f"{rv.name} = _U")
        self.emit("_n = 0")
        for it in ast.body:
            self.item(it)
        if rv.kn:
            self.ret(f"_rd({rv.name}, {rv.kn}, {_mask(rv.pt.w)})")
        else:
            self.ret(rv.name)
        head = f"def _fn({', '.join(ports)}):"
        return head, pw, psigned

    def func_rt(self):
        ast = self.ast
        if isinstance(ast.rettype, A.OtherType) and ast.rettype.kind == "void":
            raise Unsup("void function")
        if ast.rettype is None:
            return BIT
        return self.rtype(ast.rettype)


_Compiler._DISPATCH = {
    A.Block: _Compiler.s_block, A.ProcAssign: _Compiler.s_assign, A.If: _Compiler.s_if, A.Case: _Compiler.s_case,
    A.For: _Compiler.s_for, A.While: _Compiler.s_while, A.DoWhile: _Compiler.s_dowhile,
    A.Repeat: _Compiler.s_repeat, A.Break: _Compiler.s_break, A.Continue: _Compiler.s_continue,
    A.Return: _Compiler.s_return, A.CallStmt: _Compiler.s_callstmt,
}


def get_fast(ev, f):
    """The :class:`FastFn` of function symbol ``f``, or False when it is outside the supported subset."""
    fast = f.fast
    if fast is None:
        fast = f.fast = _compile(ev, f) or False
    return fast


def _compile(ev, f):
    ctx = ev.ctx
    cache = ctx.cf_cache
    ctx.cf_busy.add(id(f))
    try:
        comp = _Compiler(ev, f)
        try:
            head, pw, psigned = comp.build()
        except Unsup as ex:
            if _DEBUG:
                print(f"[fastfunc] {f.ast.name}: unsupported: {ex}")
            return None
        except RecursionError:
            return None
        nested_names = []
        for nm, fn in comp.nested:
            nested_names.append(f"# {nm} = {fn.key}")
        src = "\n".join([head, *comp.lines])
        src = "\n".join(nested_names + [src])
        key = (f"{ctx.limits['loop_unroll']}/{ctx.limits['const_func_fast_steps']}/"
               f"{ctx.limits['const_func_fast_total_steps']}\n{src}")
        hit = cache.get(key)
        if hit is not None:
            return hit
        ns = dict(_HELPERS)
        ns["_BUDGET"] = ctx.limits["const_func_fast_steps"]
        iters = ns["_C"] = [0]
        for nm, fn in comp.nested:
            ns[nm] = fn.callm
        try:
            exec(compile(src, f"<constfunc {f.ast.name}>", "exec"), ns)
        except SyntaxError as ex:                  # translator bug: never fatal, the general path takes over
            if _DEBUG:
                print(f"[fastfunc] {f.ast.name}: generated code does not compile: {ex}\n{src}")
            return None
        fast = FastFn(ns["_fn"], tuple(pw), f.ast.name, iters, ctx.limits["const_func_fast_total_steps"])
        fast.psigned = tuple(psigned)
        fast.rt = comp.rt
        fast.ast = f.ast
        fast.key = hashlib.sha1(key.encode()).hexdigest()
        cache[key] = fast
        if _DEBUG:
            print(f"[fastfunc] {f.ast.name}: compiled ({len(comp.lines)} lines)")
        return fast
    except Exception as ex:                        # noqa: BLE001 - never let a translator bug break elaboration
        if _DEBUG:
            import traceback
            traceback.print_exc()
            print(f"[fastfunc] {f.ast.name}: internal error {ex!r}")
        return None
    finally:
        ctx.cf_busy.discard(id(f))


def fast_value(fast, vals):
    """``int`` result of a call with constant argument values, or None."""
    r = fast.run(vals)
    STATS["fallbacks" if r is FAIL else "calls"] += 1
    return None if r is FAIL else r


__all__ = ["FAIL", "FastFn", "Bail", "Unsup", "get_fast", "fast_value", "V"]
