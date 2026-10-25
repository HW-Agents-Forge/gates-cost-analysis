"""ieee.fixed_pkg: ``ufixed``/``sfixed`` arithmetic with the VHDL-2008 result-range rules.

A fixed-point value is an array ``(high downto low)`` (``low`` may be negative).  Its bits are an ordinary
two's complement / unsigned integer ``N`` and the value is ``N * 2**low``, so every operator aligns the binary
points, runs the integer operator at the width the package result range implies, and tags the result with
that range (``ieee.fixed_result_range``).  ``resize`` quantises (``fixed_round`` adds the dropped most
significant bit, ``fixed_truncate`` drops bits) and handles overflow (``fixed_wrap`` drops the high bits,
``fixed_saturate`` clamps with an overflow detector and a constant mux).  Constants fold to constants.
"""
from __future__ import annotations

from fractions import Fraction

from ...frontend.vhdl import ieee, nodes as N
from ...ir import SigSpec
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, Unsupported, bool_val, int_val, real_val, range_int_type
from .values import Val
from .vtypes import minbits


def is_fixed(v):
    return v.t.kind == "array" and v.t.cls in ieee.FIXED_CLASSES


def is_floatv(v):
    return v.t.kind == "array" and v.t.cls == "fl"


def _is_number(v):
    return v.t.kind in ("int", "real") or v.t.cls in ("int", "nat")


class FixedMixin:
    # ------------------------------------------------------------------ views
    def fx_view(self, v):
        """``(sig, signed, high, low)`` of a fixed-point value."""
        t = v.t
        s = self.sig_of(v)
        w = s.width
        if t.left is not None and t.length == w:
            lo = min(t.left, t.right)
            return s, t.cls == "sf", lo + w - 1, lo
        return s, t.cls == "sf", w - 1, 0

    def fx_val(self, sig, signed, hi, lo):
        w = hi - lo + 1
        if w < 0:
            w = 0
            hi = lo - 1
        if sig.width != w:
            sig = sig.resize(w, signed)
        return Val(T.constrain(T.T_SFIXED if signed else T.T_UFIXED, hi, lo, "downto"), sig)

    def fx_value_of(self, v):
        """Exact value (Fraction) of a constant fixed-point Val, or None."""
        s, sg, hi, lo = self.fx_view(v)
        c = s.const_value()
        if c is None or c.xmask:
            return None
        n = c.as_signed() if sg and s.width else c.value
        return Fraction(n) * (Fraction(2) ** lo)

    def fx_operand(self, v, other):
        """``(sig, signed, high, low)`` of an operand of a fixed-point operator (``other`` is the fixed side)."""
        if is_fixed(v):
            return self.fx_view(v)
        oh, ol, osg = None, None, False
        if other is not None and is_fixed(other):
            _s, osg, oh, ol = self.fx_view(other)
        t = v.t
        if t.kind == "real" or isinstance(v.c, float):
            if oh is None:
                raise Unsupported("real operand without a fixed-point partner")
            c = self.const_val(v)
            pat = ieee.fixed_quantize(c, oh, ol, osg)
            if pat is None:
                raise Unsupported("non-finite real operand")
            return SigSpec.const(pat, oh - ol + 1), osg, oh, ol
        if t.kind == "int" or t.cls in ("int", "nat"):
            if v.c is not None and v.sig is None:
                c = int(v.c)
                w, _sg = minbits(c, c)
                hi = max(oh if oh is not None else 0, 0, w - 1 + (1 if osg and c >= 0 and not _sg else 0))
                return SigSpec.const(c & mask(hi + 1), hi + 1), osg, hi, 0
            s, sg = self.num(v)
            return s, sg, s.width - 1, 0
        if t.kind == "enum" and t.width == 1:
            return self.sig_of(v), False, 0, 0
        s = self.sig_of(v)
        return s, t.cls in T.SIGNED_CLASSES, s.width - 1, 0

    # ------------------------------------------------------------------ alignment helpers
    @staticmethod
    def fx_pad_low(sig, n):
        return SigSpec.concat([SigSpec.const(0, n), sig]) if n > 0 else sig

    def fx_align(self, sig, signed, hi, lo, thi, tlo):
        """``sig`` (range ``hi downto lo``) re-expressed on ``thi downto tlo`` (``thi >= hi``, ``tlo <= lo``)."""
        s = self.fx_pad_low(sig, lo - tlo)
        return s.resize(max(0, thi - tlo + 1), signed)

    # ------------------------------------------------------------------ operators
    def fx_binary(self, op, l, r, node, sc):
        """Fixed-point ``+ - * / rem mod`` and comparisons; None when neither operand is fixed-point."""
        if not (is_fixed(l) or is_fixed(r)):
            return None
        if is_fixed(l) and is_fixed(r) and l.t.cls != r.t.cls:
            self.ctx.assume("ufixed/sfixed operands mixed in '%s'; treated as signed" % op)
        o = self.org(node)
        A = self.fx_operand(l, r)
        B = self.fx_operand(r, l)
        if op in ("=", "/=", "<", "<=", ">", ">="):
            return self.fx_compare(op, A, B, o)
        if op in ("?=", "?/=", "?<", "?<=", "?>", "?>="):
            rv = self.fx_compare(op[1:], A, B, o)
            return Val(T.T_STD_LOGIC, rv.sig)
        if op in ("+", "-"):
            return self.fx_addsub(op, A, B, o)
        if op == "*":
            return self.fx_mul(A, B, o)
        if op == "/":
            return self.fx_div(A, B, o)
        if op in ("rem", "mod"):
            return self.fx_remmod(op, A, B, o)
        raise Unsupported("fixed-point operator %s" % op)

    def fx_addsub(self, op, A, B, o):
        b = self.b
        (a, sa, ah, al), (c, sb, bh, bl) = A, B
        S = sa or sb
        hi, lo = ieee.fixed_result_range(op, (ah, al), (bh, bl))
        if a.width == 0 or c.width == 0:
            return self.fx_val(SigSpec(()), S, hi, lo)
        M = max(al, bl)
        k = M - lo
        fn = b.add if op == "+" else b.sub
        if k > 0 and (op == "+" or al <= bl):
            # the low bits exist in one operand only: they pass through, the adder is narrower
            src = a if al < bl else c
            low = src.slice(0, min(k, src.width))
            low = low if low.width == k else low.resize(k, False)

            def cut(sig, low_idx, signed):
                n = M - low_idx
                if n <= 0:
                    return sig
                if n >= sig.width:
                    return sig.slice(sig.width - 1, 1) if signed else SigSpec.const(0, 1)
                return sig.slice(n)
            wu = hi - M + 1
            up = fn(cut(a, al, sa), cut(c, bl, sb), wu, sa=sa, sb=sb, origin=o)
            return self.fx_val(SigSpec.concat([low, up]), S, hi, lo)
        w = hi - lo + 1
        a2 = self.fx_pad_low(a, al - lo)
        c2 = self.fx_pad_low(c, bl - lo)
        return self.fx_val(fn(a2, c2, w, sa=sa, sb=sb, origin=o), S, hi, lo)

    def fx_mul(self, A, B, o):
        (a, sa, ah, al), (c, sb, bh, bl) = A, B
        S = sa or sb
        hi, lo = ieee.fixed_result_range("*", (ah, al), (bh, bl))
        w = hi - lo + 1
        if a.width == 0 or c.width == 0 or w <= 0:
            return self.fx_val(SigSpec(()), S, hi, lo)
        return self.fx_val(self.b.mul(a, c, w, sa=sa, sb=sb, origin=o), S, hi, lo)

    def fx_div(self, A, B, o):
        (a, sa, ah, al), (c, sb, bh, bl) = A, B
        S = sa or sb
        hi, lo = ieee.fixed_result_range("/", (ah, al), (bh, bl), S)
        w = hi - lo + 1
        if a.width == 0 or c.width == 0 or w <= 0:
            return self.fx_val(SigSpec(()), S, hi, lo)
        shift = (bh - bl + 1) - (1 if S else 0)
        num = self.fx_pad_low(a, shift)
        return self.fx_val(self.b.div(num, c, w, sa=sa, sb=sb, origin=o), S, hi, lo)

    def fx_remmod(self, op, A, B, o):
        (a, sa, ah, al), (c, sb, bh, bl) = A, B
        S = sa or sb
        hi, lo = ieee.fixed_result_range(op, (ah, al), (bh, bl))
        H, L = max(ah, bh), min(al, bl)
        W = H - L + 1
        w = hi - lo + 1
        if a.width == 0 or c.width == 0 or w <= 0 or W <= 0:
            return self.fx_val(SigSpec(()), S, hi, lo)
        x = self.fx_align(a, sa, ah, al, H, L)
        y = self.fx_align(c, sb, bh, bl, H, L)
        res = self.b.mod(x, y, W, sa=sa, sb=sb, origin=o)
        return self.fx_val(res.slice(0, min(w, W)), S, hi, lo)

    def fx_compare(self, op, A, B, o):
        b = self.b
        (a, sa, ah, al), (c, sb, bh, bl) = A, B
        S = sa or sb
        H, L = max(ah, bh), min(al, bl)
        W = max(1, H - L + 1) + (1 if S and not (sa and sb) else 0)
        x = self.fx_align(a, sa, ah, al, H, L).resize(W, sa)
        y = self.fx_align(c, sb, bh, bl, H, L).resize(W, sb)
        if op == "=":
            return bool_val(b.eq(x, y, origin=o))
        if op == "/=":
            return bool_val(b.ne(x, y, origin=o))
        fn = {"<": b.lt, "<=": b.le, ">": b.gt, ">=": b.ge}[op]
        return bool_val(fn(x, y, sa=S, sb=S, origin=o))

    def fx_unary(self, op, v, node):
        b = self.b
        o = self.org(node)
        s, sg, hi, lo = self.fx_view(v)
        if op == "-" or (op == "abs" and sg):
            if not sg:
                raise Unsupported("unary '-' on ufixed")
            w = s.width + 1
            x = s.resize(w, True)
            neg = b.neg(x, w, sa=True, origin=o)
            if op == "-":
                return self.fx_val(neg, True, hi + 1, lo)
            return self.fx_val(b.mux(s.slice(s.width - 1, 1), x, neg, w, origin=o), True, hi + 1, lo)
        return v

    # ------------------------------------------------------------------ quantisation / resize
    def fx_requant(self, s, signed, hi, lo, thi, tlo, sat=True, rnd=True, o=None):
        """Bits of ``s`` (range ``hi downto lo``) on the range ``thi downto tlo`` (fixed_pkg ``resize``)."""
        b = self.b
        tw = thi - tlo + 1
        if tw <= 0:
            return SigSpec(())
        if s.width == 0:
            return SigSpec.const(0, tw)
        c = s.const_value()
        if c is not None and not c.xmask:
            n = c.as_signed() if signed else c.value
            pat = ieee.fixed_quantize(Fraction(n) * (Fraction(2) ** lo), thi, tlo, signed, sat, rnd)
            return SigSpec.const(pat, tw)
        if tlo < lo:
            s = self.fx_pad_low(s, lo - tlo)
            lo = tlo
        elif tlo > lo:
            k = tlo - lo
            ext = s.resize(max(s.width, k + 1), signed)
            kept = ext.slice(k)
            rbit = ext.slice(k - 1, 1)
            hi = max(hi, tlo)
            lo = tlo
            if rnd:
                kept = kept.resize(kept.width + 1, signed)
                kept = b.add(kept, rbit, kept.width, sa=signed, sb=False, origin=o)
                hi += 1
            s = kept
        w = s.width
        if thi >= hi:
            return s.resize(tw, signed)
        d = hi - thi
        kept = s.slice(0, tw)
        if not sat:
            return kept
        if not signed:
            ovf = b.red_or(s.slice(tw, d), origin=o)
            return b.mux(ovf, kept, SigSpec.const(mask(tw), tw), tw, origin=o)
        top = s.slice(tw - 1, d + 1)
        ovf = b.and_(b.red_or(top, origin=o), b.not_(b.red_and(top, origin=o), 1, origin=o), 1, origin=o)
        sat_val = b.mux(s.slice(w - 1, 1), SigSpec.const(mask(tw - 1), tw), SigSpec.const(1 << (tw - 1), tw), tw, origin=o)
        return b.mux(ovf, kept, sat_val, tw, origin=o)

    def fmodel_of(self, sc):
        """Defaults of the ``fixed_generic_pkg`` / ``float_generic_pkg`` instances visible from ``sc``."""
        out = {}
        s = sc
        while s is not None:
            for env in s.uses:
                m = env.scope.fmodel if env.scope is not None else None
                if m:
                    for k, v in m.items():
                        out.setdefault(k, v)
            if s.fmodel:
                for k, v in s.fmodel.items():
                    out.setdefault(k, v)
            s = s.parent
        return out

    def fx_model_styles(self, sc):
        """(saturate?, round?) defaults of the visible ``fixed_pkg`` (the package's own: saturate, round)."""
        m = self.fmodel_of(sc)
        return (str(m.get("fixed_overflow_style", "fixed_saturate")).lower() != "fixed_wrap",
                str(m.get("fixed_round_style", "fixed_round")).lower() != "fixed_truncate")

    def fx_styles(self, vals, default_sat=True, default_rnd=True, sc=None):
        sat, rnd = default_sat, default_rnd
        if sc is not None:
            msat, mrnd = self.fx_model_styles(sc)
            sat, rnd = (msat if default_sat else sat), (mrnd if default_rnd else rnd)
        for v in vals:
            if v is None or v.c is None:
                continue
            if v.t is T.T_FIXED_OVF:
                sat = bool(v.c)
            elif v.t is T.T_FIXED_RND:
                rnd = not bool(v.c)
        return sat, rnd

    def fx_args(self, args, sc, formals):
        """Evaluate call arguments by position and by formal name (``formals``: tuple of tuples of accepted names)."""
        slots = [None] * len(formals)
        pos = 0
        for a in args:
            idx = None
            if a.formal is not None and isinstance(a.formal, N.Ident):
                for i, names in enumerate(formals):
                    if a.formal.name in names:
                        idx = i
                        break
            if idx is None:
                while pos < len(slots) and slots[pos] is not None:
                    pos += 1
                idx = pos
                pos += 1
            if idx < len(slots):
                slots[idx] = self.eval(a.actual, sc, None)
        return slots

    _RESIZE_FORMALS = (("arg",), ("left_index", "size_res"), ("right_index", "overflow_style"),
                       ("overflow_style", "round_style"), ("round_style",))

    def bi_fx_resize(self, name, args, e, sc, hint):
        o = self.org(e)
        vals = self.fx_args(args, sc, self._RESIZE_FORMALS)
        x = vals[0]
        s, sg, hi, lo = self.fx_view(x)
        if vals[1] is not None and is_fixed(vals[1]):                       # resize(arg, size_res, [styles])
            _s2, _g2, thi, tlo = self.fx_view(vals[1])
            styles = vals[2:]
        else:                                                             # resize(arg, left, right, [styles])
            thi, tlo = self.const_int(vals[1]), self.const_int(vals[2])
            styles = vals[3:]
        sat, rnd = self.fx_styles(styles, sc=sc)
        return self.fx_val(self.fx_requant(s, sg, hi, lo, thi, tlo, sat, rnd, o), sg, thi, tlo)

    _TOFIXED_FORMALS = (("arg",), ("left_index", "size_res"), ("right_index", "overflow_style"),
                        ("overflow_style", "round_style"), ("round_style",))

    def bi_to_fixed(self, name, args, e, sc, hint):
        o = self.org(e)
        signed = name == "to_sfixed"
        vals = self.fx_args(args, sc, self._TOFIXED_FORMALS)
        x = vals[0]
        if x is None:
            raise Unsupported("%s without argument" % name)
        if vals[1] is None:                                    # to_sfixed(ufixed): one more bit for the sign
            if not is_fixed(x):
                if x.t.kind == "array" and x.t.cls in ("u", "s", "slv", "suv"):
                    # to_ufixed(unsigned) / to_sfixed(signed): the bits as an integer, range (w-1 downto 0)
                    s = self.sig_of(x)
                    return self.fx_val(s, signed, s.width - 1, 0)
                raise Unsupported("%s needs a range" % name)
            s, sg, hi, lo = self.fx_view(x)
            return self.fx_val(s.resize(s.width + 1, False), True, hi + 1, lo)
        if is_fixed(vals[1]):
            _s2, _g2, thi, tlo = self.fx_view(vals[1])
            styles = vals[2:]
        else:
            thi, tlo = self.const_int(vals[1]), self.const_int(vals[2])
            styles = vals[3:]
        sat, rnd = self.fx_styles(styles, sc=sc)
        tw = thi - tlo + 1
        t = x.t
        if t.kind == "real" or isinstance(x.c, float) or (x.c is not None and x.sig is None and t.kind == "int"):
            pat = ieee.fixed_quantize(self.const_val(x), thi, tlo, signed, sat, rnd)
            if pat is None:
                raise Unsupported("non-finite constant")
            return self.fx_val(SigSpec.const(pat, max(tw, 0)), signed, thi, tlo)
        if is_fixed(x):
            s, sg, hi, lo = self.fx_view(x)
            return self.fx_val(self.fx_requant(s, sg, hi, lo, thi, tlo, sat, rnd, o), signed, thi, tlo)
        if t.kind == "array" and t.cls in ("slv", "suv", "bv"):         # bit reinterpretation
            s = self.sig_of(x)
            return self.fx_val(s.resize(max(tw, 0), False), signed, thi, tlo)
        if t.kind == "array" and t.cls in ("u", "s") or t.kind == "int" or t.cls in ("int", "nat"):
            s, sg = self.num(x) if t.kind == "int" else (self.sig_of(x), t.cls == "s")
            return self.fx_val(self.fx_requant(s, sg, s.width - 1, 0, thi, tlo, sat, rnd, o), signed, thi, tlo)
        raise Unsupported("%s of %s" % (name, t.name or t.kind))

    # ------------------------------------------------------------------ conversions out of fixed
    def fx_to_integer(self, x, e):
        """Integer part (truncated) of a fixed-point value as an integer-typed Val."""
        s, sg, hi, lo = self.fx_view(x)
        o = self.org(e)
        c = self.fx_value_of(x)
        if c is not None:
            return int_val(c.__floor__())
        thi = max(hi, 0)
        r = self.fx_requant(s, sg, hi, lo, thi, 0, sat=True, rnd=False, o=o)
        w = r.width
        lo_v, hi_v = (-(1 << (w - 1)), (1 << (w - 1)) - 1) if sg else (0, mask(w))
        return Val(range_int_type(lo_v, hi_v) if w <= 32 else range_int_type(-2 ** 31, 2 ** 31 - 1), r)

    def bi_fx_to_vec(self, name, args, e, sc, hint):
        """``to_unsigned`` / ``to_signed`` of a fixed-point value (integer part, saturating, truncating)."""
        o = self.org(e)
        vals = self.fx_args(args, sc, (("arg",), ("size", "size_res"), ("overflow_style",), ("round_style",)))
        x, n = vals[0], vals[1]
        s, sg, hi, lo = self.fx_view(x)
        if n.t.kind == "array":
            n = Val(T.T_UINT, None, self.sig_of(n).width)
        w = self.const_int(n)
        sat, rnd = self.fx_styles(vals[2:], True, False)
        cls = "u" if name == "to_unsigned" else "s"
        r = self.fx_requant(s, sg, hi, lo, w - 1, 0, sat, rnd, o)
        return Val(T.vec_type(cls, w), r) if w > 0 else Val(T.constrain(T.vec_type(cls, 1), -1, 0, "downto"), SigSpec(()))

    def bi_fx_to_real(self, name, args, e, sc, hint):
        if len(args) != 1:
            raise Unsupported("to_real with %d arguments" % len(args))
        (x,) = self.call_args(name, args, sc)[:1]
        if x.t.kind == "real":
            return x
        if is_fixed(x):
            c = self.fx_value_of(x)
            if c is None:
                raise Unsupported("to_real of a non-constant fixed-point value")
            return real_val(float(c))
        if is_floatv(x):
            return self.fl_to_real(x)
        return real_val(float(self.const_int(x)))

    def bi_to_slv(self, name, args, e, sc, hint):
        (x,) = self.call_args(name, args, sc)[:1]
        s = self.sig_of(x)
        if s.width == 0:
            return Val(T.constrain(T.T_SUV if name in ("to_sulv", "to_suv") else T.T_SLV, -1, 0, "downto"), s)
        base = T.T_SUV if name in ("to_suv", "to_sulv") else T.T_SLV
        return Val(T.constrain(base, s.width - 1, 0, "downto"), s, None, x.z)

    # ------------------------------------------------------------------ misc functions
    def bi_fx_divide(self, name, args, e, sc, hint):
        vals = self.fx_args(args, sc, (("l",), ("r",), ("round_style",), ("guard_bits",)))
        o = self.org(e)
        A, B = self.fx_operand(vals[0], vals[1]), self.fx_operand(vals[1], vals[0])
        if name == "divide":
            v = self.fx_div(A, B, o)
            s, sg, hi, lo = self.fx_view(v)
            # divide() drops the extra guard bit of "/"
            return self.fx_val(self.fx_requant(s, sg, hi, lo, A[2] - B[3] + (1 if sg else 0), lo + 1, True, False, o),
                               sg, A[2] - B[3] + (1 if sg else 0), lo + 1)
        if name in ("remainder", "modulo"):
            return self.fx_remmod("rem" if name == "remainder" else "mod", A, B, o)
        raise Unsupported(name)

    def bi_fx_reciprocal(self, name, args, e, sc, hint):
        (x,) = self.call_args(name, args, sc)[:1]
        _s, sg, _h, _l = self.fx_view(x)
        one = (SigSpec.const(1, 2 if sg else 1), sg, 1 if sg else 0, 0)
        return self.fx_div(one, self.fx_view(x), self.org(e))

    def bi_fx_scalb(self, name, args, e, sc, hint):
        y, n = self.call_args(name, args, sc)[:2]
        if is_floatv(y):
            return self.fl_scalb(y, n, e)
        s, sg, hi, lo = self.fx_view(y)
        o = self.org(e)
        b = self.b
        w = s.width
        try:
            k = self.const_int(n)
        except NotConst:
            k = None
        if k is not None:
            if k == 0 or w == 0:
                return y
            amt = SigSpec.const(abs(k), max(1, abs(k).bit_length()))
            if k > 0:
                r = b.shl(s, amt, w, sa=False, origin=o)
            else:
                r = b.sar(s, amt, w, origin=o) if sg else b.shr(s, amt, w, sa=False, origin=o)
            return Val(y.t, r)
        amt, asg = self.numv(n, sc)
        left = b.shl(s, amt, w, sa=False, origin=o)
        right = b.sar(s, b.neg(amt, amt.width, sa=asg, origin=o), w, origin=o) if sg else \
            b.shr(s, b.neg(amt, amt.width, sa=asg, origin=o), w, sa=False, origin=o)
        return Val(y.t, b.mux(amt.slice(amt.width - 1, 1) if asg else SigSpec.const(0, 1), left, right, w, origin=o))

    def bi_fx_minmax(self, name, a, c, e, sc):
        """minimum / maximum of two fixed-point values."""
        o = self.org(e)
        A, B = self.fx_operand(a, c), self.fx_operand(c, a)
        (x, sa, ah, al), (y, sb, bh, bl) = A, B
        S = sa or sb
        H, L = max(ah, bh), min(al, bl)
        W = max(1, H - L + 1) + (1 if S and not (sa and sb) else 0)
        xs = self.fx_align(x, sa, ah, al, H, L).resize(W, sa)
        ys = self.fx_align(y, sb, bh, bl, H, L).resize(W, sb)
        lt = self.b.lt(xs, ys, sa=S, sb=S, origin=o)
        pick_x = lt if name == "minimum" else self.b.not_(lt, 1, origin=o)
        return self.fx_val(self.b.mux(pick_x, ys, xs, W, origin=o), S, H + (1 if W > H - L + 1 else 0), L)

    def bi_find_extreme(self, name, args, e, sc, hint):
        """``find_leftmost`` / ``find_rightmost`` (arg, y): index of the first element equal to y, else arg'low-1."""
        x, y = self.call_args(name, args, sc)[:2]
        o = self.org(e)
        b = self.b
        s = self.sig_of(x)
        w = s.width
        t = x.t
        low = (t.right if t.dir == "downto" else t.left) if t.left is not None and t.length == w else 0
        yc = self.sig_of(y)
        yv = yc.const_value()
        want_one = not (yv is not None and yv.value == 0)
        bits = s if want_one else b.not_(s, w, origin=o)
        if w == 0:
            return int_val(low - 1)
        rev = bits if name == "find_rightmost" else SigSpec.concat([bits.slice(w - 1 - i, 1) for i in range(w)])
        any_ = b.red_or(rev, origin=o)
        pos = b.encode(rev, kind="priority", origin=o)
        k = pos.width
        if name == "find_leftmost":
            pos = b.sub(SigSpec.const(w - 1, k + 1), pos.zext(k + 1), k + 1, sa=False, sb=False, origin=o)
        rw = max(k, (abs(low) + w + 1).bit_length()) + 2
        idx = b.add(pos.zext(rw), SigSpec.const(low & mask(rw), rw), rw, sa=True, sb=True, origin=o)
        res = b.mux(any_, SigSpec.const((low - 1) & mask(rw), rw), idx, rw, origin=o)
        return Val(range_int_type(-(1 << (rw - 1)), (1 << (rw - 1)) - 1), res)


FIXED_BUILTINS = {
    "to_sfixed": FixedMixin.bi_to_fixed, "to_ufixed": FixedMixin.bi_to_fixed,
    "to_slv": FixedMixin.bi_to_slv, "to_sulv": FixedMixin.bi_to_slv, "to_suv": FixedMixin.bi_to_slv,
    "to_real": FixedMixin.bi_fx_to_real,
    "scalb": FixedMixin.bi_fx_scalb, "reciprocal": FixedMixin.bi_fx_reciprocal,
    "divide": FixedMixin.bi_fx_divide, "remainder": FixedMixin.bi_fx_divide, "modulo": FixedMixin.bi_fx_divide,
    "find_leftmost": FixedMixin.bi_find_extreme, "find_rightmost": FixedMixin.bi_find_extreme,
}
