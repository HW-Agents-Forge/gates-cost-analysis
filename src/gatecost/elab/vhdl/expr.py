"""Expression evaluation: names, literals, aggregates, attributes, indexing, slicing, types."""
from __future__ import annotations

import math

from ...frontend.vhdl import ieee, nodes as N
from ...ir import Const, Origin, SigSpec
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, StopLimit, Unsupported, bool_const, int_val, real_val, range_int_type
from .vtypes import VType, minbits
from .values import Val, Sym

_HEXVAL = {c: i for i, c in enumerate("0123456789ABCDEF")}
_SLBIT = {"0": (0, 0), "1": (1, 0), "L": (0, 0), "H": (1, 0), "U": (0, 1), "X": (0, 1), "W": (0, 1), "-": (0, 1), "Z": (0, 1)}


class ExprMixin:
    # ------------------------------------------------------------------ basics
    def org(self, node):
        if node is None:
            return self.b.origin
        if isinstance(node, N.EXPR_NODES) and self.b.origin is not None:
            return self.b.origin          # operations inherit the origin of their statement (IR.md 3)
        return Origin(self.ctx.disp(self.file) if self.file else "<unknown>", getattr(node, "line", 0) or 0, getattr(node, "col", 0) or 0)

    def diag(self, code, msg, node=None, **kw):
        self.ctx.diag(code, msg, self.file, getattr(node, "line", None), getattr(node, "col", None), **kw)

    def unknown(self, hint=None, why=None):
        if hint is not None and hint.kind in ("array", "record", "enum", "int") and hint.width:
            return Val(hint, SigSpec.x(hint.width))
        return Val(T.T_STD_LOGIC, SigSpec.x(1))

    def sig_of(self, v, width=None, signed=None):
        s = v.sig
        if s is None:
            t = v.t
            if t.kind == "int":
                c = int(v.c)
                w = width or t.width or minbits(c, c)[0]
                s = SigSpec.const(c & mask(w), w)
            elif t.kind == "enum":
                if t.enc is None:
                    s = SigSpec.const(int(v.c), 8)
                else:
                    val, xm = t.enc[v.c]
                    s = SigSpec.const(val, t.width, xm)
            elif t.kind == "real":
                raise Unsupported("real value used as hardware")
            else:
                raise Unsupported("value without bits")
        if width is not None and s.width != width:
            s = s.resize(width, self.is_signed(v) if signed is None else signed)
        return s

    @staticmethod
    def is_signed(v):
        t = v.t
        if t.kind == "int":
            return bool(t.signed) or (v.c is not None and v.c < 0)
        return t.cls == "s"

    def num(self, v):
        """(sig, signed) numeric view."""
        t = v.t
        if t.kind == "int":
            if v.sig is None:
                c = int(v.c)
                w, s = minbits(c, c)
                return SigSpec.const(c & mask(w), w), s
            return v.sig, bool(t.signed)
        s = self.sig_of(v)
        return s, t.cls == "s"

    def const_int(self, v):
        if v.c is not None:
            if isinstance(v.c, float):
                return int(v.c)
            return int(v.c)
        if v.sig is not None:
            c = v.sig.const_value()
            if c is not None and not c.xmask:
                return c.as_signed() if (v.t.kind == "int" and v.t.signed) or v.t.cls == "s" else c.value
        raise NotConst()

    def const_val(self, v):
        """Python int/float constant of a value or NotConst."""
        if isinstance(v.c, float):
            return v.c
        return self.const_int(v)

    def cond_bit(self, v):
        if v.c is not None and v.sig is None:
            return SigSpec.const(1 if v.c else 0, 1)
        s = self.sig_of(v)
        if v.t.kind == "enum" and v.t.width == 1 or s.width == 1:
            return s
        return self.b.red_or(s)

    def fit(self, v, t, node=None):
        """Coerce ``v`` to type ``t`` (assignment / port / generic association)."""
        if t.kind == "real":
            if v.c is not None:
                return Val(t, None, float(v.c))
            return v
        if t.kind == "int":
            if t.width is None:
                return v
            if v.c is not None and v.sig is None:
                c = int(v.c)
                if t.low is not None and not (t.low <= c <= t.high):
                    self.ctx.assume("constant %d outside range %d..%d of %s; wrapped" % (c, t.low, t.high, t.name or "subtype"))
                return Val(t, SigSpec.const(c & mask(t.width), t.width), c)
            if v.t.kind == "enum" and v.t.cls == "bool":
                return Val(t, self.sig_of(v).zext(t.width))
            s = self.sig_of(v, t.width, self.is_signed(v))
            return Val(t, s, v.c)
        if t.kind == "enum":
            if v.c is not None and v.t is not t and v.t.lits and t.lits:
                nm = v.t.lits[v.c]
                if nm in t.lits:
                    i = t.lits.index(nm)
                    return Val(t, None, i, v.z)
            if v.t.kind == "enum" and v.t.width == t.width and v.sig is None and v.c is not None and t.lits is None:
                return Val(t, None, v.c)
            if v.c is not None and v.sig is None and v.t is t:
                return v
            if v.t.kind == "enum" and v.t.onehot != t.onehot and v.t.lits and t.lits and v.t.lits == t.lits:
                return Val(t, self._reencode(v, t))
            s = self.sig_of(v, t.width, False)
            return Val(t, s, v.c if (v.t is t) else None, v.z)
        if t.kind in ("array", "record"):
            if t.width is None:
                return v
            s = self.sig_of(v) if v.sig is not None or v.t.kind != "int" else self.sig_of(v, t.width)
            if s.width != t.width:
                self.ctx.assume("width mismatch (%d bits assigned to %d) at %s:%s; value %s" % (
                    s.width, t.width, self.file, getattr(node, "line", "?"), "truncated" if s.width > t.width else "zero-extended"))
                s = s.resize(t.width, False)
            return Val(t, s, None, v.z)
        return v

    def _reencode(self, v, t):
        s = self.sig_of(v)
        # onehot <-> binary on hardware values: rebuild via decode/encode is out of scope; resize
        return s.resize(t.width, False)

    # ------------------------------------------------------------------ dispatch
    def eval(self, e, sc, hint=None):
        h = _EV.get(type(e))
        if h is None:
            self.diag("UNSUPPORTED_CONSTRUCT", "expression %s" % type(e).__name__, e)
            return self.unknown(hint)
        try:
            return h(self, e, sc, hint)
        except (StopLimit, RecursionError):
            raise
        except Exception as ex:  # degrade: never fail globally
            if getattr(self.ctx, "debug", False):
                raise
            self.diag("UNSUPPORTED_CONSTRUCT", "cannot evaluate expression (%s: %s)" % (type(ex).__name__, ex), e)
            return self.unknown(hint)

    def eval_const_int(self, e, sc, hint=None):
        return self.const_int(self.eval(e, sc, hint))

    # ------------------------------------------------------------------ names
    def read_sym_sig(self, sym):
        s = sym.sig
        o = sym.owner
        if o is not None:
            return o.blk.read(s)
        p = self.proc
        if p is not None:
            return p.blk.read(s)
        return s

    def ev_ident(self, e, sc, hint):
        name = e.name
        syms = sc.lookup(name)
        if not syms:
            return self.ev_builtin_name(e, name, sc, hint)
        return self.val_of_syms(syms, e, sc, hint)

    def val_of_syms(self, syms, e, sc, hint):
        s = syms[0]
        k = s.kind
        if k == "const":
            if s.val is None:
                raise Unsupported("deferred constant '%s' has no value" % s.name)
            return s.val
        if k == "obj":
            return Val(s.t, self.read_sym_sig(s))
        if k == "enumlit":
            return self.pick_enum(syms, hint)
        if k == "alias":
            return self.eval_alias(s)
        if k == "func":
            return self.call_user(syms, [], e, sc, hint)
        if k == "mem":
            raise Unsupported("whole memory used as a value")
        if k == "type":
            raise Unsupported("type name '%s' used as a value" % s.name)
        raise Unsupported("name '%s' cannot be used as a value" % s.name)

    def pick_enum(self, syms, hint):
        if hint is not None:
            for s in syms:
                if s.t is hint or (hint.kind == "enum" and hint.lits and s.t.lits is hint.lits):
                    return Val(hint, None, s.pos, 1 if s.name == "'Z'" else 0)
            for s in syms:
                if hint.kind == "enum" and hint.lits and s.name in hint.lits:
                    return Val(hint, None, hint.lits.index(s.name))
        s = syms[-1]
        return Val(s.t, None, s.pos)

    def eval_alias(self, s):
        node, sc2, t = s.node, s.scope, s.t
        v = self.eval(node, sc2, t)
        if t is not None and t.kind == "array" and t.width is not None and v.sig is not None and v.sig.width == t.width:
            return Val(t, v.sig, None, v.z)
        return v

    def ev_builtin_name(self, e, name, sc, hint):
        if name in ieee.MATH_REAL_CONSTANTS:
            return real_val(ieee.MATH_REAL_CONSTANTS[name])
        if name in ("true", "false"):
            return bool_const(name == "true")
        if name in ("note", "warning", "error", "failure"):
            return int_val(0)
        if ieee.lookup(name):
            return self.builtin_call(name, [], e, sc, hint)
        if name == "now":
            return int_val(0)
        self.diag("UNSUPPORTED_CONSTRUCT", "unknown name '%s'" % name, e)
        return self.unknown(hint)

    def ev_literal(self, e, sc, hint):
        k = e.kind
        if k == "int" or k == "based":
            v = e.value
            if isinstance(v, float):
                return real_val(v)
            return int_val(v)
        if k == "real":
            return real_val(e.value)
        if k == "char":
            return self.char_val(e.value, sc, hint)
        if k == "str":
            return self.string_val(e.value, hint)
        if k == "null":
            raise Unsupported("null")
        raise Unsupported("literal kind %s" % k)

    def char_val(self, ch, sc, hint):
        name = "'%s'" % ch
        if hint is not None and hint.kind == "enum" and hint.lits and name in hint.lits:
            i = hint.lits.index(name)
            return Val(hint, None, i, 1 if name == "'Z'" else 0)
        if hint is not None and hint.kind == "enum" and hint.lits is None:
            return Val(hint, None, ord(ch))
        syms = [s for s in sc.lookup(name) if s.kind == "enumlit"]
        if syms:
            return self.pick_enum(syms, hint)
        up = "'%s'" % ch.upper()
        if up in T.SL_LITS:
            if hint is not None and hint.cls == "bit" and up in ("'0'", "'1'"):
                return Val(T.T_BIT, None, ("'0'", "'1'").index(up))
            return Val(T.T_STD_LOGIC, None, T.SL_LITS.index(up), 1 if up == "'Z'" else 0)
        return Val(T.T_CHARACTER, None, ord(ch))

    def string_val(self, text, hint):
        et = None
        if hint is not None and hint.kind == "array":
            et = hint.elem
        chars = text
        if et is None:
            if all(c.upper() in "01UXZWLH-" for c in chars) and chars:
                et, base = T.T_STD_LOGIC, T.T_SLV
            else:
                et, base = T.T_CHARACTER, T.T_STRING
        else:
            base = hint
        value = xm = zm = 0
        n = len(chars)
        for i, c in enumerate(chars):
            pos = n - 1 - i
            if et.kind == "enum" and et.lits is None:
                value |= ord(c) << (8 * pos)
                continue
            if et.kind == "enum" and et.lits:
                nm = "'%s'" % (c if "'%s'" % c in et.lits else c.upper())
                if nm not in et.lits:
                    nm = "'%s'" % c.upper()
                if nm in et.lits:
                    ev, ex = et.enc[et.lits.index(nm)]
                else:
                    ev, ex = 0, 1
                value |= ev << pos
                xm |= ex << pos
                if nm == "'Z'":
                    zm |= 1 << pos
            else:
                raise Unsupported("string literal for element type %s" % et.name)
        ew = et.width
        if ew != 1 and et.kind == "enum" and et.lits:
            value = xm = zm = 0
            for i, c in enumerate(chars):
                nm = "'%s'" % c.upper()
                ev, ex = et.enc[et.lits.index(nm)] if nm in et.lits else (0, 1)
                value |= ev << ((n - 1 - i) * ew)
                xm |= (mask(ew) if ex else 0) << ((n - 1 - i) * ew)
        t = T.constrain(base if base.kind == "array" else T.T_SLV, n - 1, 0, "downto") if n else T.constrain(base, -1, 0, "downto")
        t = t.copy(elem=et, ewidth=ew) if t.elem is not et else t
        t.width = n * ew
        return Val(t, SigSpec.const(value, n * ew, xm), None, zm)

    def ev_bitstr(self, e, sc, hint):
        base, digits = e.base, e.digits
        per = {"b": 1, "o": 3, "x": 4}.get(base)
        bits = []   # (value, xmask, z) MSB first
        if base == "d":
            n = int(digits)
            w = max(1, n.bit_length())
            bits = [((n >> (w - 1 - i)) & 1, 0, 0) for i in range(w)]
        else:
            for ch in digits:
                if ch in _HEXVAL and _HEXVAL[ch] < (1 << per):
                    d = _HEXVAL[ch]
                    bits.extend(((d >> (per - 1 - i)) & 1, 0, 0) for i in range(per))
                else:
                    v, x = _SLBIT.get(ch, (0, 1))
                    bits.extend((v, x, 1 if ch == "Z" else 0) for _ in range(per))
        size = e.size
        if size is not None:
            size = int(size)
            if len(bits) < size:
                fill = bits[0] if (e.sign == "s" and bits) else (0, 0, 0)
                bits = [fill] * (size - len(bits)) + bits
            elif len(bits) > size:
                bits = bits[len(bits) - size:]
        n = len(bits)
        value = xm = zm = 0
        for i, (v, x, z) in enumerate(bits):
            p = n - 1 - i
            value |= v << p
            xm |= x << p
            zm |= z << p
        if hint is not None and hint.kind == "array" and hint.ewidth == 1:
            base_t = hint
        else:
            base_t = T.T_SLV
        t = T.constrain(base_t, n - 1, 0, "downto") if n else T.constrain(base_t, -1, 0, "downto")
        return Val(t, SigSpec.const(value, n, xm), None, zm)

    def ev_physical(self, e, sc, hint):
        return int_val(0)

    def ev_others(self, e, sc, hint):
        raise Unsupported("'others' outside an aggregate")

    def ev_open(self, e, sc, hint):
        raise Unsupported("open")

    def ev_qualified(self, e, sc, hint):
        t = self.type_of_mark(e.type_mark, sc)
        v = self.eval(e.operand, sc, t)
        if t.kind == "array" and t.width is None:
            return v
        return self.fit(v, t, e)

    # ------------------------------------------------------------------ qualified names
    def flat_name(self, e):
        out = []
        while True:
            if isinstance(e, N.Ident):
                out.append(e.name)
                break
            if isinstance(e, N.Select):
                out.append(e.suffix)
                e = e.prefix
                continue
            return None
        out.reverse()
        return out

    def qualified_syms(self, e, sc):
        """Symbols named by a dotted path through libraries/packages, or None if not such a path."""
        path = self.flat_name(e)
        if not path or len(path) < 2:
            return None
        i = 0
        lib = None
        if path[0] in self.libs_known(sc) and not sc.lookup(path[0]):
            lib = "work" if path[0] == "work" else path[0]
            if path[0] == "work":
                lib = sc.lib
            i = 1
        if i >= len(path) - 0:
            return None
        pname = path[i]
        rest = path[i + 1:]
        if not rest:
            return None
        pk = None
        if lib is not None:
            pk = self.get_package(lib, pname)
            if pk is None and lib != "work":
                if (lib, pname) in ieee.KNOWN_PACKAGES:
                    return [Sym("builtin", rest[0], extra=(lib, pname))]
            if pk is None and path[0] == "work":
                pk = self.get_package(None, pname)
        else:
            s = sc.lookup1(pname)
            if s is not None and s.kind == "pkg":
                pk = s.val
            else:
                return None
        if pk is None:
            return [Sym("builtin", rest[0], extra=(lib, pname))] if rest else None
        if pk.scope is None:
            return []
        r = pk.scope.names.get(rest[0])
        if r is None:
            return []
        if len(rest) > 1:
            return None
        return r

    def libs_known(self, sc):
        s = set(("work", "ieee", "std"))
        for (lib, _n) in self.ctx.parsed.entities:
            s.add(lib)
        for (lib, _n) in self.ctx.parsed.packages:
            s.add(lib)
        return s

    def name_syms(self, p, sc):
        if isinstance(p, N.Ident):
            return sc.lookup(p.name)
        if isinstance(p, N.Select):
            q = self.qualified_syms(p, sc)
            if q is not None:
                return q
        return None

    def ev_select(self, e, sc, hint):
        q = self.qualified_syms(e, sc)
        if q is not None:
            if q and q[0].kind == "builtin":
                return self.ev_builtin_name(e, q[0].name, sc, hint)
            if not q:
                raise Unsupported("unknown name in package: %s" % e.suffix)
            return self.val_of_syms(q, e, sc, hint)
        if e.suffix == "all":
            return self.eval(e.prefix, sc, hint)
        base = self.eval(e.prefix, sc, None)
        return self.field_of(base, e.suffix, e)

    def field_of(self, base, name, node=None):
        t = base.t
        if t.kind != "record" or name not in t.offs:
            raise Unsupported("no record field '%s'" % name)
        off, w = t.offs[name]
        ft = dict(t.fields)[name]
        s = self.sig_of(base)
        return Val(ft, s.slice(off, w), None, (base.z >> off) & mask(w))

    # ------------------------------------------------------------------ types
    def type_of_mark(self, mark, sc):
        if isinstance(mark, N.SubtypeInd):
            return self.resolve_subtype(mark, sc)
        if isinstance(mark, (N.Ident, N.Select)):
            syms = self.name_syms(mark, sc)
            if syms:
                for s in syms:
                    if s.kind == "type":
                        return s.t
                if syms[0].kind == "builtin":
                    nm = syms[0].name
                    if nm in T.BUILTIN_TYPES:
                        return T.BUILTIN_TYPES[nm]
            elif isinstance(mark, N.Ident) and mark.name in T.BUILTIN_TYPES:
                return T.BUILTIN_TYPES[mark.name]
            if isinstance(mark, N.Select) and mark.suffix in T.BUILTIN_TYPES:
                return T.BUILTIN_TYPES[mark.suffix]
            self.diag("UNSUPPORTED_CONSTRUCT", "unknown type '%s'" % (self.flat_name(mark) or "?"), mark)
            return T.T_STD_LOGIC
        if isinstance(mark, N.Attr):
            nm = mark.name
            if nm in ("subtype", "base", "element"):
                pre = mark.prefix
                t = self.try_type(pre, sc)
                if t is None:
                    t = self.eval(pre, sc).t
                if nm == "element":
                    return t.elem
                return t
        raise Unsupported("type mark")

    def try_type(self, e, sc):
        if isinstance(e, (N.Ident, N.Select)):
            syms = self.name_syms(e, sc)
            if syms:
                if syms[0].kind == "type":
                    return syms[0].t
                if syms[0].kind == "builtin" and syms[0].name in T.BUILTIN_TYPES:
                    return T.BUILTIN_TYPES[syms[0].name]
                return None
            if isinstance(e, N.Ident) and e.name in T.BUILTIN_TYPES:
                return T.BUILTIN_TYPES[e.name]
        elif isinstance(e, N.SubtypeInd):
            return self.resolve_subtype(e, sc)
        return None

    def resolve_subtype(self, ind, sc):
        if ind is None:
            return T.T_STD_LOGIC
        if not isinstance(ind, N.SubtypeInd):
            return self.type_of_mark(ind, sc)
        base = self.type_of_mark(ind.mark, sc)
        ics = ind.index_constraints
        if ics and base.kind == "array":
            return self.constrain_array(base, ics, sc)
        if ics and base.kind == "record":
            return base
        if ind.range is not None and base.kind in ("int", "real"):
            if base.kind == "real":
                return base
            l, r, d = self.eval_range(ind.range, sc)
            lo, hi = (l, r) if d == "to" else (r, l)
            if lo > hi:
                lo, hi = hi, lo
            return T.int_type(base.name, lo, hi)
        if ind.range is not None and base.kind == "enum":
            try:
                l, r, d = self.eval_range(ind.range, sc)
                lits = base.lits
                if lits and 0 <= min(l, r) and max(l, r) < len(lits):
                    return base
            except Exception:
                pass
        return base

    def constrain_array(self, base, ics, sc):
        first = ics[0]
        try:
            l, r, d = self.eval_range(first, sc)
        except NotConst:
            self.diag("NON_CONSTANT_BOUND", "non-constant array bound; one element assumed", first)
            self.ctx.assume("non-constant array bound at %s:%s; one element assumed" % (self.file, getattr(first, "line", "?")))
            l, r, d = 0, 0, "to"
        if base.left is not None:
            # already constrained: constrain element (array of arrays)
            return base
        t = T.constrain(base, l, r, d)
        if len(ics) > 1 and base.elem.kind == "array":
            el = self.constrain_array(base.elem, ics[1:], sc)
            t = t.copy(elem=el, ewidth=el.width)
            t.width = t.length * el.width if el.width is not None else None
        return t

    def eval_range(self, r, sc):
        """(left, right, dir) of a range-valued expression."""
        if isinstance(r, N.Range):
            l = self.const_int(self.eval(r.left, sc))
            rr = self.const_int(self.eval(r.right, sc))
            return l, rr, r.direction
        if isinstance(r, N.SubtypeInd):
            if r.range is not None and not r.index_constraints:
                base = self.type_of_mark(r.mark, sc) if r.mark is not None else None
                return self.eval_range(r.range, sc)
            t = self.resolve_subtype(r, sc)
            rg = T.range_of(t)
            if rg is None:
                raise Unsupported("range of type")
            return rg
        if isinstance(r, N.Attr) and r.name in ("range", "reverse_range"):
            t = self.try_type(r.prefix, sc)
            if t is None:
                t = self.eval(r.prefix, sc).t
            if r.arg is not None and t.kind == "array":
                k = self.const_int(self.eval(r.arg, sc))
                while k > 1 and t.elem is not None:
                    t = t.elem
                    k -= 1
            rg = T.range_of(t)
            if rg is None:
                raise Unsupported("'range of unconstrained object")
            l, rr, d = rg
            if r.name == "reverse_range":
                return rr, l, "to" if d == "downto" else "downto"
            return l, rr, d
        t = self.try_type(r, sc)
        if t is not None:
            rg = T.range_of(t)
            if rg is not None:
                return rg
        raise Unsupported("range expression")

    def is_range_expr(self, a, sc):
        if isinstance(a, N.Range):
            return True
        if isinstance(a, N.Attr) and a.name in ("range", "reverse_range"):
            return True
        if isinstance(a, N.SubtypeInd):
            return True
        if isinstance(a, (N.Ident, N.Select)):
            t = self.try_type(a, sc)
            return t is not None and t.kind in ("int", "enum")
        return False

    # ------------------------------------------------------------------ attributes
    def ev_attr(self, e, sc, hint):
        nm = e.name
        pre = e.prefix
        if nm in ("event", "stable", "last_value", "delayed", "quiet", "transaction", "active", "last_event"):
            self.diag("UNSUPPORTED_CONSTRUCT", "attribute '%s' outside an edge condition" % nm, e)
            return bool_const(False)
        if nm in ("range", "reverse_range"):
            raise Unsupported("'%s used as a value" % nm)
        if nm in ("image", "path_name", "instance_name", "simple_name"):
            return self.string_val("", None)
        t = self.try_type(pre, sc)
        v = None
        if t is None:
            v = self.eval(pre, sc)
            t = v.t
        if nm == "base":
            return v
        k = 1
        if e.arg is not None and nm in ("length", "high", "low", "left", "right", "ascending"):
            k = self.const_int(self.eval(e.arg, sc))
        tt = t
        while k > 1 and tt.elem is not None:
            tt = tt.elem
            k -= 1
        if nm in ("length", "high", "low", "left", "right", "ascending"):
            rg = T.range_of(tt)
            if rg is None:
                if nm == "length" and tt.kind == "array" and v is not None and v.sig is not None and tt.ewidth:
                    return int_val(v.sig.width // tt.ewidth)
                raise Unsupported("'%s of an unconstrained type" % nm)
            l, r, d = rg
            if tt.kind == "enum" and nm in ("left", "right", "high", "low"):
                p = {"left": l, "right": r, "high": max(l, r), "low": min(l, r)}[nm]
                return Val(tt, None, p)
            if nm == "length":
                return int_val(max(0, (l - r + 1) if d == "downto" else (r - l + 1)))
            if nm == "left":
                return int_val(l)
            if nm == "right":
                return int_val(r)
            if nm == "high":
                return int_val(max(l, r))
            if nm == "low":
                return int_val(min(l, r))
            return bool_const(d == "to")
        if nm in ("pos", "val", "succ", "pred", "leftof", "rightof"):
            return self.attr_scalar(nm, t, e, sc)
        if nm == "element":
            return Val(t.elem, None)
        raise Unsupported("attribute '%s" % nm)

    def attr_scalar(self, nm, t, e, sc):
        a = self.eval(e.arg, sc, t if nm != "pos" else None)
        if nm == "pos":
            if a.c is not None:
                return int_val(a.c)
            s = self.sig_of(a)
            return Val(range_int_type(0, mask(s.width)), s)
        if nm == "val":
            if a.c is not None:
                if t.kind == "enum":
                    return Val(t, None, int(a.c))
                return int_val(a.c)
            s = self.sig_of(a)
            if t.kind == "enum":
                return Val(t, s.resize(t.width, False))
            return Val(t, s)
        step = {"succ": 1, "pred": -1, "rightof": 1, "leftof": -1}[nm]
        if a.c is not None:
            return Val(t, None, a.c + step) if t.kind == "enum" else int_val(a.c + step)
        s, sg = self.num(a)
        r = self.b.add(s, SigSpec.const(1 if step > 0 else mask(s.width), s.width), s.width, origin=self.org(e))
        return Val(a.t, r)

    # ------------------------------------------------------------------ indexing / calls
    def ev_apply(self, e, sc, hint):
        p = e.prefix
        args = e.args
        syms = None
        if isinstance(p, (N.Ident, N.Select)):
            syms = self.name_syms(p, sc)
        if isinstance(p, N.Attr) and p.name in ("val", "pos", "succ", "pred") and p.arg is None:
            # t'val(x) parsed as Apply(Attr)
            t = self.try_type(p.prefix, sc)
            if t is not None and args:
                tmp = N.Attr(p.prefix, p.name, args[0].actual, line=p.line, col=p.col)
                return self.ev_attr(tmp, sc, hint)
        if syms:
            k = syms[0].kind
            if k == "type":
                return self.type_convert(syms[0].t, args, sc, e)
            if k == "func":
                return self.call_user(syms, args, e, sc, hint)
            if k == "mem":
                return self.mem_read(syms[0], args, sc, e)
            if k == "builtin":
                nm = syms[0].name
                if nm in T.BUILTIN_TYPES:
                    return self.type_convert(T.BUILTIN_TYPES[nm], args, sc, e)
                return self.builtin_call(nm, args, e, sc, hint)
            if k in ("obj", "const", "alias"):
                return self.index_into(self.val_of_syms(syms, p, sc, None), args, sc, e)
            if k == "enumlit":
                raise Unsupported("enumeration literal applied")
            raise Unsupported("cannot apply '%s'" % syms[0].name)
        if isinstance(p, N.Ident):
            nm = p.name
            if nm in T.BUILTIN_TYPES:
                return self.type_convert(T.BUILTIN_TYPES[nm], args, sc, e)
            return self.builtin_call(nm, args, e, sc, hint)
        if isinstance(p, N.Select) and p.suffix in T.BUILTIN_TYPES and self.qualified_syms(p, sc) is not None:
            return self.type_convert(T.BUILTIN_TYPES[p.suffix], args, sc, e)
        if isinstance(p, N.Attr) and p.name in ("subtype", "base"):
            return self.type_convert(self.type_of_mark(p, sc), args, sc, e)
        base = self.eval(p, sc)
        return self.index_into(base, args, sc, e)

    def index_into(self, base, args, sc, e):
        for a in args:
            ex = a.actual
            if self.is_range_expr(ex, sc):
                l, r, d = self.eval_range(ex, sc)
                base = self.slice_val(base, l, r, d, e)
            else:
                if base.t.kind != "array":
                    raise Unsupported("indexing a non-array value")
                base = self.index_val(base, ex, sc, e)
        return base

    def ev_slice(self, e, sc, hint):
        base = self.eval(e.prefix, sc)
        try:
            l, r, d = self.eval_range(e.range, sc)
        except NotConst:
            return self.dyn_slice_val(base, e, sc)
        return self.slice_val(base, l, r, d, e)

    def split_offset(self, x, sc):
        """``(base expression, constant offset)`` of ``x`` written as ``base +/- constant`` (else ``(x, 0)``)."""
        off = 0
        while isinstance(x, N.Binary) and x.op in ("+", "-"):
            try:
                c = self.const_int(self.eval(x.right, sc))
                x, off = x.left, off + (c if x.op == "+" else -c)
                continue
            except NotConst:
                pass
            if x.op == "+":
                try:
                    c = self.const_int(self.eval(x.left, sc))
                    x, off = x.right, off + c
                    continue
                except NotConst:
                    pass
            break
        return x, off

    def dyn_slice_bounds(self, rng, sc):
        """For ``a(i+3 downto i)``: ``(width in elements, low-index expression, direction)`` or raises Unsupported."""
        if not isinstance(rng, N.Range):
            raise Unsupported("slice with a non-constant range")
        lb, lo_ = self.split_offset(rng.left, sc)
        rb, ro = self.split_offset(rng.right, sc)
        if repr(lb) != repr(rb):
            raise Unsupported("slice bounds with a non-constant width")
        n = abs(lo_ - ro) + 1
        if rng.direction == "downto":
            if lo_ < ro:
                raise Unsupported("null slice with non-constant bounds")
            return n, rng.right, rng.direction
        if ro < lo_:
            raise Unsupported("null slice with non-constant bounds")
        return n, rng.left, rng.direction

    def dyn_slice_val(self, base, e, sc):
        """Slice whose position is a variable but whose width is constant: ``a(i+3 downto i)``."""
        n, low, d = self.dyn_slice_bounds(e.range, sc)
        t = base.t
        if t.kind != "array":
            raise Unsupported("slice of a non-array")
        ew = t.ewidth or 1
        s = self.sig_of(base)
        if t.left is None:
            t = T.constrain(t, s.width // ew - 1, 0, "downto")
        iv = self.eval(low, sc, t.index)
        x, _cnt, lo = self.index_pos_sig(t, iv, e)
        count = t.length - n + 1
        nt = T.constrain(t, n - 1, 0, "downto") if d == "downto" else T.constrain(t, 0, n - 1, "to")
        if count < 1:
            self.ctx.assume("variable slice wider than the array at %s:%s" % (self.file, getattr(e, "line", "?")))
            return Val(nt, SigSpec.x(n * ew))
        if count > 65536:
            self.diag("LIMIT_REACHED", "variable slice with %d positions exceeds the expansion limit" % count, e)
            return Val(nt, SigSpec.x(n * ew))
        arms = []
        for a in range(count):
            i = lo + a
            if t.dir == "downto":
                p_lo = T.index_to_pos(t, i)
            else:
                p_lo = T.index_to_pos(t, i + n - 1)
            arms.append(s.slice(p_lo * ew, n * ew))
        cw = max(1, (count - 1).bit_length())
        xi = x.slice(0, min(cw, x.width)) if x.width > cw else x
        r = self.b.index_mux(arms, xi, origin=self.org(e)) if count > 1 else arms[0]
        self.ctx.assume("slice at a variable position (width %d) costed as a %d-way multiplexer" % (n, count))
        return Val(nt, r.slice(0, n * ew) if r.width >= n * ew else r.zext(n * ew))

    def slice_val(self, base, l, r, d, node=None):
        t = base.t
        if t.kind != "array":
            raise Unsupported("slice of a non-array")
        ew = t.ewidth or 1
        s = self.sig_of(base)
        if t.left is None:
            # unconstrained value with positional bits: assume (n-1 downto 0)
            n = s.width // ew
            t = T.constrain(t, n - 1, 0, "downto")
        lo_i, hi_i = min(l, r), max(l, r)
        tl, tr = t.left, t.right
        t_lo, t_hi = min(tl, tr), max(tl, tr)
        if hi_i < lo_i:
            return Val(T.constrain(t, l, r, d), SigSpec(()), None, 0)
        if lo_i < t_lo or hi_i > t_hi:
            self.ctx.assume("slice %d..%d outside %d..%d at %s:%s; clamped" % (lo_i, hi_i, t_lo, t_hi, self.file, getattr(node, "line", "?")))
            lo_c, hi_c = max(lo_i, t_lo), min(hi_i, t_hi)
            if hi_c < lo_c:
                return Val(T.constrain(t, l, r, d), SigSpec.x((hi_i - lo_i + 1) * ew), None, 0)
        else:
            lo_c, hi_c = lo_i, hi_i
        if t.dir == "downto":
            p_lo, p_hi = lo_c - t.right, hi_c - t.right
        else:
            p_lo, p_hi = t.right - hi_c, t.right - lo_c
        sub = s.slice(p_lo * ew, (p_hi - p_lo + 1) * ew)
        if (lo_c, hi_c) != (lo_i, hi_i):
            pad_lo = (lo_c - lo_i) * ew
            pad_hi = (hi_i - hi_c) * ew
            sub = SigSpec.concat([SigSpec.x(pad_lo), sub, SigSpec.x(pad_hi)]) if (t.dir == "downto") else sub
        nt = T.constrain(t, l, r, d)
        return Val(nt, sub, None, (base.z >> (p_lo * ew)) & mask(sub.width))

    def index_val(self, base, ex, sc, node):
        t = base.t
        iv = self.eval(ex, sc, t.index)
        ew = t.ewidth or 1
        s = self.sig_of(base)
        if t.left is None:
            n = s.width // ew
            t = T.constrain(t, n - 1, 0, "downto")
        et = t.elem
        try:
            i = self.const_int(iv)
        except NotConst:
            return self.dyn_index_val(base, t, s, iv, node)
        p = T.index_to_pos(t, i)
        if not (0 <= p < t.length):
            self.ctx.assume("index %d outside range at %s:%s; value unknown" % (i, self.file, getattr(node, "line", "?")))
            return Val(et, SigSpec.x(ew))
        sub = s.slice(p * ew, ew)
        return self.mk_elem(et, sub, (base.z >> (p * ew)) & mask(ew))

    def mk_elem(self, et, sub, z=0):
        c = None
        if et.kind == "enum" and et.lits and sub.const_value() is not None and not z:
            cv = sub.const_value()
            if et.enc:
                for i, (ev, ex) in enumerate(et.enc):
                    if ev == cv.value and ex == cv.xmask:
                        c = i
                        break
        return Val(et, sub, None if c is None else c, z) if c is None else Val(et, sub, c, z)

    def index_pos_sig(self, t, iv, node):
        """Position (from the rightmost element) of a dynamic index, as an unsigned SigSpec, and element count."""
        isg, sg = self.num(iv)
        lo = min(t.left, t.right)
        n = t.length
        if sg:
            w = max(isg.width, (max(abs(t.left), abs(t.right)) + 1).bit_length() + 1)
        else:
            w = max(isg.width, max(t.left, t.right, 1).bit_length())
        w = max(w, 1)
        x = isg.resize(w, sg)
        if lo != 0:
            x = self.b.sub(x, SigSpec.const(lo & mask(w), w), w, origin=self.org(node))
        return x, n, lo

    def dyn_index_val(self, base, t, s, iv, node):
        """Variable-index read: MUX tree (arms ascending by index)."""
        ew = t.ewidth or 1
        n = t.length
        if n > 65536:
            self.diag("LIMIT_REACHED", "variable-index read of %d elements exceeds the expansion limit; value unknown" % n, node)
            return Val(t.elem, SigSpec.x(ew))
        x, _n, lo = self.index_pos_sig(t, iv, node)
        arms = []
        for a in range(n):
            i = lo + a
            p = T.index_to_pos(t, i)
            arms.append(s.slice(p * ew, ew))
        cw = max(1, (n - 1).bit_length())
        xi = x.slice(0, min(cw, x.width)) if x.width > cw else x
        cv = s.const_value()
        if cv is not None and n >= 2:
            contents = [a_.const_value().value for a_ in arms]
            r = self.b.rom(xi, contents, ew, origin=self.org(node))
            return Val(t.elem, r)
        r = self.b.index_mux(arms, xi, origin=self.org(node))
        return Val(t.elem, r.slice(0, ew) if r.width >= ew else r.zext(ew))

    # ------------------------------------------------------------------ conversions
    def type_convert(self, t, args, sc, e):
        if len(args) != 1:
            raise Unsupported("type conversion needs one argument")
        v = self.eval(args[0].actual, sc, t if t.kind != "array" else None)
        return self.convert_to(t, v, e)

    def convert_to(self, t, v, e=None):
        vt = v.t
        if t.kind == "array":
            if vt.kind == "array":
                s = self.sig_of(v)
                ew = t.elem.width or 1
                if vt.left is not None and t.width is None:
                    n = s.width // ew
                    nt = t.copy()
                    nt.ewidth = ew
                    nt.left, nt.right, nt.dir = vt.left, vt.right, vt.dir
                    if (vt.ewidth or 1) != ew:
                        nt.left, nt.right, nt.dir = n - 1, 0, "downto"
                    nt.length = n
                    nt.width = n * ew
                    return Val(nt, s, None, v.z)
                if t.width is None:
                    n = s.width // ew
                    nt = T.constrain(t, n - 1, 0, "downto")
                    return Val(nt, s, None, v.z)
                return Val(t, s.resize(t.width, False), None, v.z)
            raise Unsupported("conversion of a scalar to an array type")
        if t.kind == "int":
            if vt.kind == "real":
                c = v.c
                if c is None:
                    raise Unsupported("real value is not constant")
                from ...frontend.vhdl.ieee import _round_away
                return int_val(int(_round_away(c)))
            if vt.kind == "int":
                return Val(t if t.width is None else t, v.sig, v.c) if t.width is None else self.fit(v, t, e)
            raise Unsupported("conversion to integer")
        if t.kind == "real":
            if v.c is not None:
                return real_val(v.c)
            raise Unsupported("real conversion of hardware value")
        if t.kind == "enum":
            return self.fit(v, t, e)
        return self.fit(v, t, e)

    # ------------------------------------------------------------------ aggregates
    def ev_aggregate(self, e, sc, hint):
        t = hint
        if t is None or t.kind not in ("array", "record"):
            return self.agg_unhinted(e, sc)
        if t.kind == "record":
            return self.agg_record(e, sc, t)
        return self.agg_array(e, sc, t)

    def agg_unhinted(self, e, sc):
        items = e.items
        if items and all(it.choices is None for it in items):
            vals = [self.eval(it.value, sc) for it in items]
            if vals and all(v.t.kind == "enum" and v.t.width == 1 for v in vals):
                s = SigSpec.concat([self.sig_of(v) for v in reversed(vals)])
                return Val(T.constrain(T.T_SLV, len(vals) - 1, 0, "downto"), s, None, sum((1 << i) for i, v in enumerate(reversed(vals)) if v.z))
        raise Unsupported("aggregate without a type context")

    def agg_record(self, e, sc, t):
        parts = {}
        pos = 0
        names = [n for n, _ in t.fields]
        others = None
        for it in e.items:
            if it.choices is None:
                parts[names[pos]] = it.value
                pos += 1
            else:
                for ch in it.choices:
                    if isinstance(ch, N.Others):
                        others = it.value
                    elif isinstance(ch, N.Ident):
                        parts[ch.name] = it.value
                    else:
                        raise Unsupported("record aggregate choice")
        out = []
        z = 0
        off = 0
        for fname, ft in t.fields:
            ex = parts.get(fname, others)
            if ex is None:
                v = Val(ft, SigSpec.x(ft.width))
            else:
                v = self.fit(self.eval(ex, sc, ft), ft, e)
            s = self.sig_of(v)
            out.append(s)
            z |= v.z << off
            off += s.width
        return Val(t, SigSpec.concat(out), None, z)

    def agg_array(self, e, sc, t):
        et = t.elem
        ew = et.width or 1
        slots = {}      # index -> expr
        others = None
        nxt = None
        n_pos = 0
        rngs = []
        # first pass to know length when unconstrained
        for it in e.items:
            if it.choices is None:
                n_pos += 1
        left, right, d = t.left, t.right, t.dir
        for it in e.items:
            if it.choices is None:
                continue
            for ch in it.choices:
                if isinstance(ch, N.Others):
                    others = it.value
        # element values
        def elem(ex):
            v = self.eval(ex, sc, et)
            if et.kind == "array" and et.width is None:
                return v
            return self.fit(v, et, e)
        # a positional/named item may also be an array piece (concatenation-like): only for matching types
        if left is None:
            # unconstrained: infer from the choices
            if n_pos and not any(it.choices for it in e.items):
                left, right, d = n_pos - 1, 0, "downto"
            else:
                idxs = []
                for it in e.items:
                    for ch in (it.choices or []):
                        if isinstance(ch, N.Others):
                            continue
                        if isinstance(ch, N.Range):
                            l, r, _d = self.eval_range(ch, sc)
                            idxs += [l, r]
                        else:
                            idxs.append(self.const_int(self.eval(ch, sc)))
                if not idxs:
                    raise Unsupported("aggregate length unknown")
                left, right, d = max(idxs), min(idxs), "downto"
            t = T.constrain(t, left, right, d)
        length = t.length
        tmp = {}
        pos_i = 0
        for it in e.items:
            if it.choices is None:
                idx = left + pos_i if d == "to" else left - pos_i
                tmp[idx] = ("e", it.value)
                pos_i += 1
                continue
            for ch in it.choices:
                if isinstance(ch, N.Others):
                    continue
                if isinstance(ch, N.Range) or (isinstance(ch, N.Attr) and ch.name in ("range", "reverse_range")):
                    l, r, _d = self.eval_range(ch, sc)
                    for i in range(min(l, r), max(l, r) + 1):
                        tmp[i] = ("e", it.value)
                else:
                    tmp[self.const_int(self.eval(ch, sc))] = ("e", it.value)
        sigs = []
        z = 0
        cache = {}
        for p in range(length):
            idx = T.pos_to_index(t, p)
            ex = tmp.get(idx)
            if ex is None:
                if others is None:
                    sigs.append(SigSpec.x(ew))
                    continue
                ex = ("e", others)
            key = id(ex[1])
            if key in cache:
                v = cache[key]
            else:
                v = elem(ex[1])
                cache[key] = v
            s = self.sig_of(v)
            if s.width != ew:
                s = s.resize(ew, False)
            sigs.append(s)
            z |= v.z << (p * ew)
        return Val(t, SigSpec.concat(sigs), None, z)

    def ev_range_as_val(self, e, sc, hint):
        raise Unsupported("range used as a value")

    def ev_skipped(self, e, sc, hint):
        raise Unsupported("skipped construct (%s)" % getattr(e, "kind", ""))


_EV = {
    N.Ident: ExprMixin.ev_ident, N.Literal: ExprMixin.ev_literal, N.BitStringLit: ExprMixin.ev_bitstr,
    N.PhysicalLit: ExprMixin.ev_physical, N.Select: ExprMixin.ev_select, N.Apply: ExprMixin.ev_apply,
    N.Slice: ExprMixin.ev_slice, N.Attr: ExprMixin.ev_attr, N.Qualified: ExprMixin.ev_qualified,
    N.Aggregate: ExprMixin.ev_aggregate, N.Others: ExprMixin.ev_others, N.Open: ExprMixin.ev_open,
    N.Range: ExprMixin.ev_range_as_val, N.Skipped: ExprMixin.ev_skipped,
}
