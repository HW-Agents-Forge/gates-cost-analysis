"""Operators, conversions and built-in (IEEE) functions with numeric_std width rules."""
from __future__ import annotations

import math

from ...frontend.vhdl import ieee, nodes as N
from ...ir import SigSpec
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, Unsupported, bool_const, bool_val, int_val, real_val, range_int_type
from .expr import _EV
from .vtypes import minbits
from .values import Val

_LOGIC = ("and", "or", "nand", "nor", "xor", "xnor")
_CMP = ("=", "/=", "<", "<=", ">", ">=")
_SHIFT = ("sll", "srl", "sla", "sra", "rol", "ror")
_ARITH = ("+", "-", "*", "/", "mod", "rem", "**")
_ARG_NAMES = {
    "resize": ("arg", "new_size"), "to_unsigned": ("arg", "size"), "to_signed": ("arg", "size"),
    "shift_left": ("arg", "count"), "shift_right": ("arg", "count"), "rotate_left": ("arg", "count"),
    "rotate_right": ("arg", "count"), "to_integer": ("arg",), "conv_std_logic_vector": ("arg", "size"),
    "conv_unsigned": ("arg", "size"), "conv_signed": ("arg", "size"),
}


def _tdiv(a, b):
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _rem(a, b):
    return a - b * _tdiv(a, b)


def _mod(a, b):
    return a - b * (a // b)


class OpsMixin:
    # ------------------------------------------------------------------ helpers
    def legacy_sem(self, sc):
        p = sc.pkgs
        if "std_logic_signed" in p and "std_logic_unsigned" not in p:
            return "s"
        return "u"

    def numv(self, v, sc):
        t = v.t
        if t.kind == "int":
            return self.num(v)
        s = self.sig_of(v)
        if t.cls == "s":
            return s, True
        if t.cls in ("slv", "suv") and self.legacy_sem(sc) == "s":
            return s, True
        return s, False

    def common_numeric(self, l, r, sc):
        a, sa = self.numv(l, sc)
        c, sb = self.numv(r, sc)
        S = sa or sb
        wa = a.width + (1 if S and not sa else 0)
        wb = c.width + (1 if S and not sb else 0)
        W = max(wa, wb, 1)
        return a.resize(W, sa), c.resize(W, sb), S

    def vec_of(self, cls, sig, like=None):
        n = sig.width
        t = T.vec_type(cls, n)
        return Val(t, sig)

    def int_to_vec(self, v, w, sc, signed_target=None):
        """Integer-like value as a ``w``-bit vector (to_unsigned / to_signed semantics)."""
        if v.c is not None and v.sig is None:
            return SigSpec.const(int(v.c) & mask(w), w)
        s, sg = self.numv(v, sc)
        return s.resize(w, sg)

    # ------------------------------------------------------------------ unary / binary
    def ev_unary(self, e, sc, hint):
        op = e.op
        v = self.eval(e.operand, sc, hint if op in ("not",) else None)
        return self.unary_op(op, v, e, sc)

    def unary_op(self, op, v, e, sc):
        b = self.b
        o = self.org(e)
        t = v.t
        if op == "+":
            return v
        if op == "-":
            if v.c is not None and v.sig is None:
                return Val(v.t, None, -v.c) if v.t.kind == "real" else int_val(-v.c)
            if t.kind == "int":
                lo, hi = (t.low, t.high) if t.low is not None else (-2 ** 31, 2 ** 31 - 1)
                nlo, nhi = -hi, -lo
                w, sg = self.cap_width(nlo, nhi)
                s, sa = self.num(v)
                r = b.neg(s, w, sa=sa, origin=o)
                return Val(self.int_t(nlo, nhi, w, sg), r)
            s, sa = self.numv(v, sc)
            r = b.neg(s, s.width, sa=sa, origin=o)
            return Val(t, r)
        if op == "abs":
            if v.c is not None and v.sig is None:
                return Val(v.t, None, abs(v.c)) if v.t.kind == "real" else int_val(abs(v.c))
            if t.kind == "int":
                lo, hi = (t.low, t.high) if t.low is not None else (-2 ** 31, 2 ** 31 - 1)
                if lo >= 0:
                    return v
                nlo, nhi = 0, max(abs(lo), abs(hi))
                w, sg = self.cap_width(nlo, nhi)
                s, sa = self.num(v)
                x = s.resize(w, True)
                neg = b.neg(x, w, sa=True, origin=o)
                r = b.mux(s.slice(s.width - 1, 1), x, neg, w, origin=o)
                return Val(self.int_t(nlo, nhi, w, sg), r)
            s, sa = self.numv(v, sc)
            if not sa:
                return v
            neg = b.neg(s, s.width, sa=True, origin=o)
            return Val(t, b.mux(s.slice(s.width - 1, 1), s, neg, s.width, origin=o))
        if op == "not":
            s = self.sig_of(v)
            r = b.not_(s, s.width, origin=o)
            if t.kind == "enum" and t.cls == "bool":
                return bool_val(r)
            return Val(t, r, None, v.z)
        if op == "??":
            return bool_val(self.cond_bit(v))
        if op in ("and", "or", "nand", "nor", "xor", "xnor"):
            s = self.sig_of(v)
            r = {"and": b.red_and, "nand": b.red_and, "or": b.red_or, "nor": b.red_or, "xor": b.red_xor,
                 "xnor": b.red_xor}[op](s, origin=o)
            if op in ("nand", "nor", "xnor"):
                r = b.not_(r, 1, origin=o)
            return Val(T.T_STD_LOGIC, r)
        raise Unsupported("unary operator %s" % op)

    def cap_width(self, lo, hi):
        w, sg = minbits(lo, hi)
        if w > 32:
            return 32, lo < 0
        return w, sg

    def int_t(self, lo, hi, w, sg):
        if minbits(lo, hi)[0] > w:
            lo, hi = (-2 ** 31, 2 ** 31 - 1) if sg else (0, 2 ** 32 - 1)
        return range_int_type(lo, hi)

    def ev_binary(self, e, sc, hint):
        chain = []
        n = e
        while isinstance(n, N.Binary):
            chain.append(n)
            n = n.left
        lead = chain[-1]
        h0 = hint if (lead.op in _LOGIC and hint is not None and hint.kind == "array") else None
        acc = self.eval(n, sc, h0)
        for node in reversed(chain):
            op = node.op
            ht = acc.t if (acc.t.kind in ("array", "enum") and op not in ("&", "**") and op not in _SHIFT) else None
            if op == "&" and acc.t.kind == "array":
                ht = None
            r = self.eval(node.right, sc, ht)
            try:
                acc = self.binary_op(op, acc, r, node, sc, hint)
            except NotConst:
                raise
            except Exception as ex:
                if getattr(self.ctx, "debug", False):
                    raise
                self.diag("UNRESOLVED_OVERLOAD" if isinstance(ex, Unsupported) else "UNSUPPORTED_CONSTRUCT",
                          "operator '%s' on %s and %s cannot be resolved (%s)" % (op, acc.t.name or acc.t.kind, r.t.name or r.t.kind, ex), node)
                acc = self.unknown(hint if hint is not None else (acc.t if acc.t.kind == "array" else None))
        return acc

    def user_op(self, op, l, r, node, sc, hint):
        syms = [s for s in sc.lookup('"%s"' % op) if s.kind == "func"]
        if not syms:
            return None
        cand = []
        for s in syms:
            fd = s.val
            if fd is not None and len(fd.node.params) == 2:
                cand.append(s)
        if not cand:
            return None
        for s in cand:
            if self.user_params_match(s.val, [l, r]):
                return self.call_user([s], [N.Assoc(None, _Lit(l)), N.Assoc(None, _Lit(r))], node, sc, hint)
        return None

    def binary_op(self, op, l, r, node, sc, hint):
        u = self.user_op(op, l, r, node, sc, hint) if sc.lookup('"%s"' % op) else None
        if u is not None:
            return u
        if op in _LOGIC:
            return self.op_logic(op, l, r, node)
        if op in ("=", "/="):
            return self.op_eq(op, l, r, node, sc)
        if op in ("<", "<=", ">", ">="):
            return self.op_rel(op, l, r, node, sc)
        if op in ("?=", "?/=", "?<", "?<=", "?>", "?>="):
            return self.op_match(op, l, r, node, sc)
        if op in _SHIFT:
            return self.op_shift(op, l, r, node, sc)
        if op == "&":
            return self.op_concat(l, r, node, hint)
        if op in _ARITH:
            return self.op_arith(op, l, r, node, sc)
        raise Unsupported("binary operator %s" % op)

    # ------------------------------------------------------------------ logic
    def op_logic(self, op, l, r, node):
        b = self.b
        o = self.org(node)
        lt, rt = l.t, r.t
        a, c = self.sig_of(l), self.sig_of(r)
        if lt.kind == "enum" and lt.cls == "bool" or (rt.kind == "enum" and rt.cls == "bool"):
            a, c = self.cond_bit(l), self.cond_bit(r)
            res_t = T.T_BOOLEAN
        else:
            if a.width != c.width:
                if a.width == 1 and lt.kind == "enum":
                    a = a.repeat(c.width)
                elif c.width == 1 and rt.kind == "enum":
                    c = c.repeat(a.width)
                else:
                    self.ctx.assume("logic operands of different widths (%d, %d) at %s:%s; widest used" % (
                        a.width, c.width, self.file, node.line))
            res_t = lt if lt.kind == "array" else (rt if rt.kind == "array" else lt)
        w = max(a.width, c.width)
        if op == "and":
            s = b.and_(a, c, w, origin=o)
        elif op == "or":
            s = b.or_(a, c, w, origin=o)
        elif op == "xor":
            s = b.xor(a, c, w, origin=o)
        elif op == "xnor":
            s = b.xnor(a, c, w, origin=o)
        elif op == "nand":
            s = b.not_(b.and_(a, c, w, origin=o), w, origin=o)
        else:
            s = b.not_(b.or_(a, c, w, origin=o), w, origin=o)
        if res_t is T.T_BOOLEAN:
            return bool_val(s)
        if res_t.kind == "array" and res_t.length is not None and res_t.length * (res_t.ewidth or 1) != w:
            res_t = T.constrain(res_t, w // (res_t.ewidth or 1) - 1, 0, "downto")
        return Val(res_t, s, None, l.z | r.z if False else 0)

    # ------------------------------------------------------------------ comparison
    def is_numeric(self, v):
        t = v.t
        return t.kind == "int" or t.cls in ("u", "s", "int", "nat")

    def enum_unify(self, l, r):
        if l.t.kind == "enum" and r.t.kind == "enum" and l.t is not r.t and l.t.lits and l.t.lits is r.t.lits:
            if r.c is not None:
                r = Val(l.t, None, r.c)
            elif l.c is not None:
                l = Val(r.t, None, l.c)
        return l, r

    def op_eq(self, op, l, r, node, sc):
        b = self.b
        o = self.org(node)
        l, r = self.enum_unify(l, r)
        if l.t.kind == "real" or r.t.kind == "real":
            x = l.c if l.c is not None else self.const_val(l)
            y = r.c if r.c is not None else self.const_val(r)
            return bool_const((x == y) == (op == "="))
        if l.c is not None and r.c is not None and l.sig is None and r.sig is None and l.t.kind != "array":
            return bool_const((l.c == r.c) == (op == "="))
        numeric = self.is_numeric(l) or self.is_numeric(r)
        if not numeric and l.t.cls in ("slv", "suv") and r.t.kind == "int":
            numeric = True
        if not numeric and r.t.cls in ("slv", "suv") and l.t.kind == "int":
            numeric = True
        if numeric:
            a, c, _s = self.common_numeric(l, r, sc)
        else:
            a, c = self.sig_of(l), self.sig_of(r)
            if a.width != c.width:
                return bool_const(op == "/=")
            for k in (a, c):
                kc = k.const_value()
                if kc is not None and kc.xmask and (a.const_value() is None or c.const_value() is None):
                    return bool_const(op == "/=")
        if l.t.kind == "enum" and l.t.onehot and l.t.width > 1:
            i = None
            for x, y in ((a, c), (c, a)):
                cv = y.const_value()
                if cv is not None and cv.value and cv.value & (cv.value - 1) == 0:
                    i = cv.value.bit_length() - 1
                    bit = x.slice(i, 1)
                    return bool_val(bit if op == "=" else b.not_(bit, 1, origin=o))
        s = b.eq(a, c, origin=o) if op == "=" else b.ne(a, c, origin=o)
        return bool_val(s)

    def op_rel(self, op, l, r, node, sc):
        b = self.b
        o = self.org(node)
        if l.t.kind == "real" or r.t.kind == "real":
            x, y = self.const_val(l), self.const_val(r)
            return bool_const({"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op])
        if l.c is not None and r.c is not None and l.sig is None and r.sig is None:
            x, y = l.c, r.c
            return bool_const({"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op])
        a, c, S = self.common_numeric(l, r, sc)
        fn = {"<": b.lt, "<=": b.le, ">": b.gt, ">=": b.ge}[op]
        return bool_val(fn(a, c, sa=S, sb=S, origin=o))

    def op_match(self, op, l, r, node, sc):
        b = self.b
        o = self.org(node)
        if op in ("?=", "?/="):
            a, c = self.sig_of(l), self.sig_of(r)
            if a.width != c.width:
                w = max(a.width, c.width)
                a, c = a.zext(w), c.zext(w)
            s = self.match_eq(a, c, o)
            if op == "?/=":
                s = b.not_(s, 1, origin=o)
            return Val(T.T_STD_LOGIC, s)
        rv = self.op_rel(op[1:], l, r, node, sc)
        return Val(T.T_STD_LOGIC, rv.sig)

    def match_eq(self, a, c, o):
        b = self.b
        ca, cc = a.const_value(), c.const_value()
        for x, y, cy in ((a, c, cc), (c, a, ca)):
            if cy is not None and cy.xmask:
                km = cy.kmask
                if km == 0:
                    return SigSpec.const(1, 1)
                xs, ys = [], []
                for i in range(a.width):
                    if (km >> i) & 1:
                        xs.append(x.slice(i, 1))
                        ys.append(SigSpec.const((cy.value >> i) & 1, 1))
                return b.eq(SigSpec.concat(xs), SigSpec.concat(ys), origin=o)
        return b.eq(a, c, origin=o)

    # ------------------------------------------------------------------ shifts
    def op_shift(self, op, l, r, node, sc):
        b = self.b
        o = self.org(node)
        if l.t.kind != "array":
            raise Unsupported("shift of a non-array")
        s = self.sig_of(l)
        w = s.width
        amt_neg = False
        try:
            k = self.const_int(r)
            if k < 0:
                k, amt_neg = -k, True
            amt = SigSpec.const(k, max(1, k.bit_length()))
        except NotConst:
            amt, sg = self.numv(r, sc)
        if amt_neg:
            op = {"sll": "srl", "srl": "sll", "sla": "sra", "sra": "sla", "rol": "ror", "ror": "rol"}[op]
        fn = {"sll": b.shl, "sla": b.shl, "srl": b.shr, "sra": b.sar, "rol": b.rol, "ror": b.ror}[op]
        res = fn(s, amt, w, origin=o) if op in ("rol", "ror") else fn(s, amt, w, sa=False, origin=o)
        return Val(l.t, res, None, 0)

    # ------------------------------------------------------------------ concatenation
    def op_concat(self, l, r, node, hint):
        lt, rt = l.t, r.t
        la, ra = lt.kind == "array", rt.kind == "array"
        if la or ra:
            at = lt if la else rt
            et = at.elem
            ew = et.width or 1
            if la and not (lt.elem is rt.elem or not ra or True):
                pass
            if la:
                ls = self.sig_of(l)
            else:
                ls = self.sig_of(self.fit(l, et, node))
            if ra:
                rs = self.sig_of(r)
            else:
                rs = self.sig_of(self.fit(r, et, node))
            lz = l.z if la else l.z
            res = SigSpec.concat([rs, ls])
            n = res.width // ew
            nt = T.constrain(at, n - 1, 0, "downto") if n > 0 else T.constrain(at, -1, 0, "downto")
            return Val(nt, res, None, l.z << rs.width | r.z)
        et = lt
        base = hint if (hint is not None and hint.kind == "array") else {"bit": T.T_BV, "str": T.T_STRING}.get(lt.cls, T.T_SLV)
        if hint is None and lt.kind != "enum":
            raise Unsupported("concatenation of scalars")
        ls, rs = self.sig_of(l), self.sig_of(r)
        res = SigSpec.concat([rs, ls])
        n = 2
        nt = T.constrain(base, n - 1, 0, "downto")
        return Val(nt, res, None, l.z << rs.width | r.z)

    # ------------------------------------------------------------------ arithmetic
    def int_range(self, v):
        t = v.t
        if v.c is not None and v.sig is None:
            return int(v.c), int(v.c)
        if t.low is not None:
            return t.low, t.high
        return -2 ** 31, 2 ** 31 - 1

    def op_arith(self, op, l, r, node, sc):
        # constants
        lc = l.c is not None and l.sig is None and l.t.kind in ("int", "real")
        rc = r.c is not None and r.sig is None and r.t.kind in ("int", "real")
        if lc and rc:
            return self.arith_py(op, l, r, node)
        if l.t.kind == "real" or r.t.kind == "real":
            x, y = self.const_val(l), self.const_val(r)
            return self.arith_py(op, real_val(x), real_val(y), node)
        li = l.t.kind == "int" or l.t.cls in ("int", "nat")
        ri = r.t.kind == "int" or r.t.cls in ("int", "nat")
        if li and ri:
            return self.arith_int(op, l, r, node)
        return self.arith_vec(op, l, r, node, sc)

    def arith_py(self, op, l, r, node):
        x, y = l.c, r.c
        isreal = l.t.kind == "real" or r.t.kind == "real"
        try:
            if op == "+":
                v = x + y
            elif op == "-":
                v = x - y
            elif op == "*":
                v = x * y
            elif op == "/":
                v = x / y if isreal else _tdiv(x, y)
            elif op == "mod":
                v = _mod(x, y)
            elif op == "rem":
                v = _rem(x, y)
            elif op == "**":
                v = float(x) ** y if isreal else (x ** y if y >= 0 else float(x) ** y)
        except ZeroDivisionError:
            self.diag("UNSUPPORTED_CONSTRUCT", "constant division by zero", node)
            v = 0
        except OverflowError:
            v = 0
        if isreal or isinstance(v, float):
            return real_val(v)
        return int_val(v)

    def arith_int(self, op, l, r, node):
        b = self.b
        o = self.org(node)
        la, ha = self.int_range(l)
        lb, hb = self.int_range(r)
        if op == "**":
            try:
                k = self.const_int(r)
            except NotConst:
                k = None
            if k is not None and k >= 0:
                cand = [la ** k, ha ** k]
                lo, hi = min(cand + ([0] if la < 0 < ha and k % 2 == 0 else [])), max(cand)
                w, sg = self.cap_width(lo, hi)
                s, sa = self.num(l)
                return Val(self.int_t(lo, hi, w, sg), b.pow(s, k, w, sa=sa, origin=o))
            try:
                bv = self.const_int(l)
            except NotConst:
                bv = None
            if bv == 2:
                s, _ = self.num(r)
                hi = 2 ** min(hb, 32)
                w, sg = self.cap_width(1, hi)
                return Val(self.int_t(1, hi, w, sg), b.shl(SigSpec.const(1, w), s, w, origin=o))
            raise Unsupported("variable exponent")
        if op == "+":
            lo, hi = la + lb, ha + hb
        elif op == "-":
            lo, hi = la - hb, ha - lb
        elif op == "*":
            c4 = [la * lb, la * hb, ha * lb, ha * hb]
            lo, hi = min(c4), max(c4)
        elif op == "/":
            m = max(abs(la), abs(ha))
            lo, hi = (-m, m) if la < 0 else (0, m)
        else:
            m = max(abs(lb), abs(hb))
            if la >= 0 and lb > 0:
                lo, hi = 0, max(m - 1, 0)
            else:
                lo, hi = -max(m - 1, 0), max(m - 1, 0)
            if op == "rem" and la >= 0:
                lo = 0
            if la >= 0 and ha < m:
                hi = min(hi, ha)
        w, sg = self.cap_width(lo, hi)
        a, sa = self.num(l)
        c, sc_ = self.num(r)
        fn = {"+": b.add, "-": b.sub, "*": b.mul, "/": b.div, "mod": b.mod, "rem": b.mod}[op]
        res = fn(a, c, w, sa=sa, sb=sc_, origin=o)
        return Val(self.int_t(lo, hi, w, sg), res)

    def arith_vec(self, op, l, r, node, sc):
        b = self.b
        o = self.org(node)
        cl, cr = l.t.cls, r.t.cls
        if l.t.kind == "enum" and l.t.cls in ("sl", "bit"):
            cl = "sl"
        if r.t.kind == "enum" and r.t.cls in ("sl", "bit"):
            cr = "sl"
        if l.t.kind == "int":
            cl = "int"
        if r.t.kind == "int":
            cr = "int"
        fs = ieee.resolve(op, [cl, cr], sc.pkgs) or ieee.resolve(op, [cl, cr])
        if fs is None:
            self.diag("UNRESOLVED_OVERLOAD", "no '%s' for %s and %s" % (op, cl, cr), node)
            return self.fallback_arith(op, l, r, node, sc)
        wl = self.vec_width(l, cl)
        wr = self.vec_width(r, cr)
        rw = ieee.result_width(fs.width, [wl, wr])
        sem = fs.sem
        # operand conversion
        def conv(v, cls, other_w, other_sig):
            if cls in ("int", "nat"):
                w = other_w
                if fs.name in ("*",) and other_w is not None:
                    w = other_w
                return self.int_to_vec(v, w or 1, sc), self.is_signed(v) if v.c is not None else self.num(v)[1]
            s = self.sig_of(v)
            if cls == "s":
                return s, True
            if cls in ("slv", "suv") and sem:
                return s, sem == "s"
            return s, False
        if wl is not None and wr is None:
            a, sa = conv(l, cl, None, None)
            c, sb = conv(r, cr, wl, None)
        elif wr is not None and wl is None:
            c, sb = conv(r, cr, None, None)
            a, sa = conv(l, cl, wr, None)
        else:
            a, sa = conv(l, cl, None, None)
            c, sb = conv(r, cr, None, None)
        if rw is None:
            rw = max(a.width, c.width)
        if fs.name in ("+", "-") and len([1 for _ in (a, c)]) == 2:
            fn = b.add if op == "+" else b.sub
            res = fn(a, c, rw, sa=sa, sb=sb, origin=o)
        elif op == "*":
            res = b.mul(a, c, rw, sa=sa, sb=sb, origin=o)
        elif op == "/":
            res = b.div(a, c, rw, sa=sa, sb=sb, origin=o)
        elif op in ("mod", "rem"):
            res = b.mod(a, c, rw, sa=sa, sb=sb, origin=o)
        else:
            raise Unsupported("operator %s on vectors" % op)
        rcls = fs.result
        return Val(T.vec_type(rcls if rcls in ("u", "s", "slv", "suv") else "slv", rw), res)

    def vec_width(self, v, cls):
        if cls in ("int", "nat"):
            return None
        if cls == "sl":
            return 1
        s = v.sig if v.sig is not None else None
        return s.width if s is not None else None

    def fallback_arith(self, op, l, r, node, sc):
        b = self.b
        o = self.org(node)
        a, sa = self.numv(l, sc)
        c, sb = self.numv(r, sc)
        w = max(a.width, c.width)
        if op == "*":
            w = a.width + c.width
        fn = {"+": b.add, "-": b.sub, "*": b.mul, "/": b.div, "mod": b.mod, "rem": b.mod}.get(op)
        if fn is None:
            raise Unsupported("operator %s" % op)
        res = fn(a, c, w, sa=sa, sb=sb, origin=o)
        return Val(T.vec_type("s" if (sa and sb) else "u", w), res)

    # ------------------------------------------------------------------ built-in calls
    def call_args(self, name, args, sc, hints=()):
        """Evaluate call arguments positionally, honoring named formals of known functions."""
        names = _ARG_NAMES.get(name, ())
        pos = []
        named = {}
        for a in args:
            if a.formal is not None and isinstance(a.formal, N.Ident) and a.formal.name in names:
                named[a.formal.name] = a.actual
            else:
                pos.append(a.actual)
        exprs = list(pos)
        if named:
            exprs = [None] * len(names)
            it = iter(pos)
            for i, nm in enumerate(names):
                if nm in named:
                    exprs[i] = named[nm]
                else:
                    exprs[i] = next(it, None)
            exprs = [x for x in exprs if x is not None]
        out = []
        for i, ex in enumerate(exprs):
            out.append(self.eval(ex, sc, hints[i] if i < len(hints) else None))
        return out

    def builtin_call(self, name, args, e, sc, hint):
        b = self.b
        o = self.org(e)
        h = _BUILTIN.get(name)
        if h is not None:
            try:
                return h(self, name, args, e, sc, hint)
            except (IndexError, ValueError) as ex:
                raise Unsupported("bad arguments for %s() (%s)" % (name, ex))
        if name in ieee.MATH_REAL_FUNCTIONS:
            vals = self.call_args(name, args, sc)
            try:
                cs = [self.const_val(v) for v in vals]
            except NotConst:
                raise Unsupported("math_real function '%s' on a non-constant" % name)
            r = ieee.eval_math_real(name, *cs)
            if r is None:
                raise Unsupported("math_real %s out of domain" % name)
            return real_val(r)
        self.diag("UNRESOLVED_OVERLOAD", "function '%s' is neither user-defined nor in the built-in IEEE model" % name, e)
        return self.unknown(hint)

    # individual built-ins -------------------------------------------------------
    def bi_to_integer(self, name, args, e, sc, hint):
        (x,) = self.call_args(name, args, sc)[:1]
        t = x.t
        if t.kind == "int":
            return x
        s, sg = self.numv(x, sc)
        c = s.const_value()
        if c is not None and not c.xmask:
            return int_val(c.as_signed() if sg else c.value)
        w = s.width
        lo, hi = (-(1 << (w - 1)), (1 << (w - 1)) - 1) if sg else (0, mask(w))
        return Val(self.int_t(lo, hi, w, sg) if w <= 32 else range_int_type(lo, hi), s)

    def bi_to_vec(self, name, args, e, sc, hint):
        vals = self.call_args(name, args, sc)
        x = vals[0]
        if len(vals) < 2:
            raise Unsupported("%s needs a size" % name)
        sz = vals[1]
        if sz.t.kind == "array":
            n = self.sig_of(sz).width
        else:
            n = self.const_int(sz)
        cls = {"to_unsigned": "u", "to_signed": "s", "conv_unsigned": "u", "conv_signed": "s",
               "conv_std_logic_vector": "slv", "to_stdlogicvector": "slv", "to_stdulogicvector": "suv"}[name]
        if n <= 0:
            return Val(T.constrain(T.vec_type(cls, 1), -1, 0, "downto"), SigSpec(()))
        if x.t.kind == "array" or (x.t.kind == "enum" and x.t.width == 1 and x.t.cls in ("sl", "bit")):
            s, sg = self.numv(x, sc)
            if x.t.kind == "array" and x.t.cls == "s" or (name == "conv_std_logic_vector" and sg):
                res = s.resize(n, True)
            else:
                res = s.resize(n, False)
        else:
            res = self.int_to_vec(x, n, sc)
            sgn = self.is_signed(x)
            if x.sig is not None and x.c is None:
                s, sg = self.num(x)
                res = s.resize(n, sg)
        return Val(T.vec_type(cls, n), res)

    def bi_resize(self, name, args, e, sc, hint):
        vals = self.call_args(name, args, sc)
        x, n = vals[0], vals[1]
        if x.t.kind == "int":
            raise Unsupported("resize of an integer")
        nn = self.sig_of(n).width if n.t.kind == "array" else self.const_int(n)
        s = self.sig_of(x)
        w = s.width
        cls = x.t.cls
        if name == "sxt":
            res = s.resize(nn, True)
        elif cls == "s" or (cls in ("slv", "suv") and self.legacy_sem(sc) == "s" and name == "resize" and False):
            if nn >= w:
                res = s.sext(nn)
            elif nn > 0:
                res = SigSpec.concat([s.slice(0, nn - 1), s.slice(w - 1, 1)])
            else:
                res = SigSpec(())
        else:
            res = s.resize(nn, False)
        t = T.vec_type(cls if cls in ("u", "s", "slv", "suv") else "slv", nn) if nn > 0 else x.t
        return Val(t, res)

    def bi_shift(self, name, args, e, sc, hint):
        vals = self.call_args(name, args, sc)
        x, n = vals[0], vals[1]
        s = self.sig_of(x)
        w = s.width
        try:
            k = self.const_int(n)
            amt = SigSpec.const(k, max(1, k.bit_length()))
        except NotConst:
            amt, _ = self.numv(n, sc)
        b = self.b
        o = self.org(e)
        cls = x.t.cls
        signed = cls == "s" or (cls in ("slv", "suv") and self.legacy_sem(sc) == "s" and name in ("shr",))
        if name in ("shift_left", "shl"):
            res = b.shl(s, amt, w, sa=False, origin=o)
        elif name in ("shift_right", "shr"):
            res = b.sar(s, amt, w, origin=o) if signed else b.shr(s, amt, w, sa=False, origin=o)
        elif name == "rotate_left":
            res = b.rol(s, amt, w, origin=o)
        else:
            res = b.ror(s, amt, w, origin=o)
        return Val(x.t, res)

    def bi_retype(self, name, args, e, sc, hint):
        (x,) = self.call_args(name, args, sc)[:1]
        t = x.t
        if name in ("to_bitvector",):
            return self.convert_to(T.T_BV, x, e)
        if name in ("to_stdlogicvector", "to_stdulogicvector") and t.kind == "array":
            return self.convert_to(T.T_SLV if name == "to_stdlogicvector" else T.T_SUV, x, e)
        if name == "to_bit":
            return Val(T.T_BIT, self.sig_of(x))
        if name == "to_stdulogic":
            return Val(T.T_STD_LOGIC, self.sig_of(x))
        return x

    def bi_edge(self, name, args, e, sc, hint):
        self.diag("UNSUPPORTED_CONSTRUCT", "%s() outside a recognized clocked-process condition" % name, e)
        return bool_const(False)

    def bi_is_x(self, name, args, e, sc, hint):
        self.call_args(name, args, sc)
        return bool_const(False)

    def bi_std_match(self, name, args, e, sc, hint):
        a, c = self.call_args(name, args, sc)
        sa, sc_ = self.sig_of(a), self.sig_of(c)
        if sa.width != sc_.width:
            return bool_const(False)
        return bool_val(self.match_eq(sa, sc_, self.org(e)))

    def bi_minmax(self, name, args, e, sc, hint):
        a, c = self.call_args(name, args, sc)
        if a.c is not None and c.c is not None and a.sig is None and c.sig is None:
            return int_val(min(a.c, c.c) if name == "minimum" else max(a.c, c.c))
        lt = self.op_rel("<", a, c, e, sc)
        x, sx = self.numv(a, sc)
        y, sy = self.numv(c, sc)
        w = max(x.width, y.width)
        x, y = x.resize(w, sx), y.resize(w, sy)
        pick_a = lt.sig if name == "minimum" else self.b.not_(lt.sig, 1)
        res = self.b.mux(pick_a, y, x, w, origin=self.org(e))
        t = a.t if a.t.kind == "array" else (c.t if c.t.kind == "array" else range_int_type(*self._mm_range(a, c)))
        return Val(t, res)

    def _mm_range(self, a, c):
        la, ha = self.int_range(a)
        lb, hb = self.int_range(c)
        return min(la, lb), max(ha, hb)

    def bi_conv_integer(self, name, args, e, sc, hint):
        return self.bi_to_integer("to_integer", args, e, sc, hint)

    def bi_string(self, name, args, e, sc, hint):
        self.call_args(name, args, sc)
        return self.string_val("", None)


class _Lit:
    """Pre-evaluated value wrapped as an expression node (used to call user operators)."""

    __slots__ = ("val", "line", "col")

    def __init__(self, val):
        self.val = val
        self.line = self.col = 0


_BUILTIN = {
    "to_integer": OpsMixin.bi_to_integer, "conv_integer": OpsMixin.bi_conv_integer,
    "to_unsigned": OpsMixin.bi_to_vec, "to_signed": OpsMixin.bi_to_vec, "conv_unsigned": OpsMixin.bi_to_vec,
    "conv_signed": OpsMixin.bi_to_vec, "conv_std_logic_vector": OpsMixin.bi_to_vec,
    "resize": OpsMixin.bi_resize, "ext": OpsMixin.bi_resize, "sxt": OpsMixin.bi_resize,
    "shift_left": OpsMixin.bi_shift, "shift_right": OpsMixin.bi_shift, "rotate_left": OpsMixin.bi_shift,
    "rotate_right": OpsMixin.bi_shift, "shl": OpsMixin.bi_shift, "shr": OpsMixin.bi_shift,
    "to_stdlogicvector": OpsMixin.bi_retype, "to_stdulogicvector": OpsMixin.bi_retype, "to_bitvector": OpsMixin.bi_retype,
    "to_bit": OpsMixin.bi_retype, "to_stdulogic": OpsMixin.bi_retype, "to_x01": OpsMixin.bi_retype,
    "to_x01z": OpsMixin.bi_retype, "to_ux01": OpsMixin.bi_retype, "to_01": OpsMixin.bi_retype,
    "rising_edge": OpsMixin.bi_edge, "falling_edge": OpsMixin.bi_edge, "is_x": OpsMixin.bi_is_x,
    "std_match": OpsMixin.bi_std_match, "minimum": OpsMixin.bi_minmax, "maximum": OpsMixin.bi_minmax,
    "to_string": OpsMixin.bi_string, "to_hstring": OpsMixin.bi_string, "to_ostring": OpsMixin.bi_string,
}

_EV[N.Unary] = OpsMixin.ev_unary
_EV[N.Binary] = OpsMixin.ev_binary


def _ev_lit(self, e, sc, hint):
    return e.val


_EV[_Lit] = _ev_lit
