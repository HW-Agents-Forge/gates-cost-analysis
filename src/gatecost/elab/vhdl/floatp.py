"""ieee.float_pkg: ``float`` / ``float16`` / ``float32`` / ``float64`` / ``float128`` types and approximate arithmetic.

A ``float(e downto -f)`` value is ``sign, e exponent bits, f fraction bits`` (IEEE-754 layout, bias
``2**(e-1)-1``).  Types and widths are exact, constants (``to_float(0.5)``, constant operands) fold exactly,
and conversions to/from ``real`` and bit vectors are exact.  Arithmetic on non-constant operands is an
*approximate gate-level model* (``ctx.assume`` records it once per run):

* ``+ -``   exponent compare + swap, alignment shifter, mantissa adder with conditional inversion, leading-zero
  counter + normalising shifter, exponent adjust, round incrementer;
* ``*``     mantissa multiplier, exponent adder and bias subtract, normalise mux, round incrementer;
* ``/``     mantissa divider, exponent subtract, normalise mux, round incrementer;
* compares  sign logic plus two magnitude comparators (no NaN / +-0 special cases);
* int/fixed -> float: absolute value, leading-zero counter, normalising shifter, round incrementer;
* float -> int: barrel shifter and conditional negate; ``resize`` between formats: exponent re-bias and rounding.

Zero / infinity / NaN / denormal special cases are not modelled in these netlists (they add a few gates).
"""
from __future__ import annotations

import math

from ...frontend.vhdl import ieee
from ...ir import SigSpec
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, Unsupported, bool_val, int_val, range_int_type, real_val
from .fixed import is_fixed, is_floatv
from .values import Val

_NOTE = ("floating-point arithmetic (ieee.float_pkg) is an approximate gate-level model: adders/multipliers/"
         "dividers/converters are costed from their mantissa and exponent datapaths, special cases "
         "(zero, infinity, NaN, denormals) are not modelled")


def _rev(sig):
    w = sig.width
    return SigSpec.concat([sig.slice(w - 1 - i, 1) for i in range(w)]) if w else sig


def _guess_format(w):
    for e, f in ieee.FLOAT_FORMATS.values():
        if 1 + e + f == w:
            return e, f
    e = max(2, round(4 * math.log2(max(w, 2))) - 13)
    e = min(e, w - 2)
    return e, w - 1 - e


class FloatMixin:
    # ------------------------------------------------------------------ views
    def fl_view(self, v):
        """``(sig, exponent bits, fraction bits)`` of a float value."""
        s = self.sig_of(v)
        t = v.t
        w = s.width
        if t.left is not None and t.length == w and t.left >= 1 and t.right <= 0 and 1 + t.left - t.right == w:
            return s, t.left, -t.right
        e, f = _guess_format(w)
        return s, e, f

    def fl_val(self, sig, e, f):
        return Val(T.constrain(T.T_FLOAT, e, -f, "downto"), sig)

    def fl_const(self, v):
        """Python float of a constant float value, or None."""
        s, e, f = self.fl_view(v)
        c = s.const_value()
        if c is None or c.xmask:
            return None
        return ieee.float_decode(c.value, e, f)

    def fl_operand(self, v, other):
        if is_floatv(v):
            return self.fl_view(v)
        _s, e, f = self.fl_view(other)
        if v.t.kind == "real" or isinstance(v.c, float) or (v.c is not None and v.sig is None):
            x = float(self.const_val(v))
            return SigSpec.const(ieee.float_encode(x, e, f), 1 + e + f), e, f
        raise Unsupported("floating-point operand of type %s" % (v.t.name or v.t.kind))

    @staticmethod
    def fl_fields(s, e, f):
        return s.slice(e + f, 1), s.slice(f, e), s.slice(0, f)

    def fl_note(self):
        self.ctx.assume(_NOTE)

    # ------------------------------------------------------------------ operators
    def fl_unary(self, op, v, node):
        s, e, f = self.fl_view(v)
        o = self.org(node)
        if s.width == 0:
            return v
        mag = s.slice(0, e + f)
        sign = s.slice(e + f, 1)
        if op == "-":
            return self.fl_val(SigSpec.concat([mag, self.b.not_(sign, 1, origin=o)]), e, f)
        return self.fl_val(SigSpec.concat([mag, SigSpec.const(0, 1)]), e, f)

    def fl_binary(self, op, l, r, node, sc):
        o = self.org(node)
        A = self.fl_operand(l, r)
        B = self.fl_operand(r, l)
        (a, ea, fa), (c, eb, fb) = A, B
        E, F = max(ea, eb), max(fa, fb)
        if (ea, fa) != (E, F):
            a = self.fl_convert(a, ea, fa, E, F, o)
        if (eb, fb) != (E, F):
            c = self.fl_convert(c, eb, fb, E, F, o)
        ca, cc = a.const_value(), c.const_value()
        if ca is not None and cc is not None and not ca.xmask and not cc.xmask:
            x, y = ieee.float_decode(ca.value, E, F), ieee.float_decode(cc.value, E, F)
            if op in ("=", "/=", "<", "<=", ">", ">="):
                from .core import bool_const
                return bool_const({"=": x == y, "/=": x != y, "<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op])
            try:
                z = {"+": x + y, "-": x - y, "*": x * y, "/": (x / y if y else math.copysign(math.inf, x) if x else math.nan),
                     "mod": math.fmod(x, y) if y else math.nan, "rem": math.fmod(x, y) if y else math.nan}[op]
            except (KeyError, OverflowError, ValueError):
                z = None
            if z is not None:
                return self.fl_val(SigSpec.const(ieee.float_encode(z, E, F), 1 + E + F), E, F)
        if op in ("=", "/=", "<", "<=", ">", ">="):
            return self.fl_compare(op, a, c, E, F, o)
        if op in ("?=", "?/=", "?<", "?<=", "?>", "?>="):
            return Val(T.T_STD_LOGIC, self.fl_compare(op[1:], a, c, E, F, o).sig)
        self.fl_note()
        if op in ("+", "-"):
            return self.fl_addsub(a, c, E, F, op == "-", o)
        if op == "*":
            return self.fl_mul(a, c, E, F, o)
        if op in ("/", "mod", "rem"):
            return self.fl_div(a, c, E, F, o)
        raise Unsupported("floating-point operator %s" % op)

    def fl_compare(self, op, a, c, E, F, o):
        b = self.b
        if op == "=":
            return bool_val(b.eq(a, c, origin=o))
        if op == "/=":
            return bool_val(b.ne(a, c, origin=o))
        def lt(x, y):
            sx, sy = x.slice(E + F, 1), y.slice(E + F, 1)
            mx, my = x.slice(0, E + F), y.slice(0, E + F)
            lt_mag = b.lt(mx, my, origin=o)
            gt_mag = b.gt(mx, my, origin=o)
            nsx, nsy = b.not_(sx, 1, origin=o), b.not_(sy, 1, origin=o)
            t1 = b.and_(sx, nsy, 1, origin=o)
            t2 = b.and_(b.and_(nsx, nsy, 1, origin=o), lt_mag, 1, origin=o)
            t3 = b.and_(b.and_(sx, sy, 1, origin=o), gt_mag, 1, origin=o)
            return b.or_(b.or_(t1, t2, 1, origin=o), t3, 1, origin=o)
        if op == "<":
            return bool_val(lt(a, c))
        if op == ">":
            return bool_val(lt(c, a))
        if op == "<=":                      # not (c < a)
            return bool_val(b.not_(lt(c, a), 1, origin=o))
        return bool_val(b.not_(lt(a, c), 1, origin=o))      # >=

    def fl_mantissa(self, exp, frac, o):
        """Significand with the hidden bit (``frac`` then ``exp /= 0``): ``f + 1`` bits."""
        return SigSpec.concat([frac, self.b.red_or(exp, origin=o)])

    def fl_pack(self, sign, exp, frac):
        return SigSpec.concat([frac, exp, sign])

    def fl_addsub(self, a, c, E, F, sub, o):
        b = self.b
        sa, ea, fa = self.fl_fields(a, E, F)
        sb, eb, fb = self.fl_fields(c, E, F)
        if sub:
            sb = b.not_(sb, 1, origin=o)
        ma, mb = self.fl_mantissa(ea, fa, o), self.fl_mantissa(eb, fb, o)
        swap = b.lt(a.slice(0, E + F), c.slice(0, E + F), origin=o)
        big_m, small_m = b.mux(swap, ma, mb, F + 1, origin=o), b.mux(swap, mb, ma, F + 1, origin=o)
        big_e, small_e = b.mux(swap, ea, eb, E, origin=o), b.mux(swap, eb, ea, E, origin=o)
        big_s = b.mux(swap, sa, sb, 1, origin=o)
        d = b.sub(big_e, small_e, E, origin=o)
        W = F + 5
        bm = SigSpec.concat([SigSpec.const(0, 3), big_m]).zext(W)
        sm = SigSpec.concat([SigSpec.const(0, 3), small_m]).zext(W)
        sm = b.shr(sm, d, W, sa=False, origin=o)
        eff_sub = b.xor(sa, sb, 1, origin=o)
        smx = b.xor(sm, eff_sub.repeat(W), W, origin=o)
        total = b.add(b.add(bm, smx, W, origin=o), eff_sub.zext(W), W, origin=o)
        lz = b.encode(_rev(total), kind="priority", origin=o)
        norm = b.shl(total, lz, W, sa=False, origin=o)
        ew = E + 1
        e_adj = b.sub(b.add(big_e.zext(ew), SigSpec.const(1, ew), ew, origin=o), lz.zext(ew), ew, origin=o)
        mant = norm.slice(W - 1 - F, F + 1)
        rbit = norm.slice(W - 2 - F, 1)
        rounded = b.add(mant, rbit.zext(F + 1), F + 2, origin=o)
        carry = rounded.slice(F + 1, 1)
        e_out = b.add(e_adj, carry.zext(ew), ew, origin=o)
        return self.fl_val(self.fl_pack(big_s, e_out.slice(0, E), rounded.slice(0, F)), E, F)

    def fl_mul(self, a, c, E, F, o):
        b = self.b
        sa, ea, fa = self.fl_fields(a, E, F)
        sb, eb, fb = self.fl_fields(c, E, F)
        ma, mb = self.fl_mantissa(ea, fa, o), self.fl_mantissa(eb, fb, o)
        sign = b.xor(sa, sb, 1, origin=o)
        prod = b.mul(ma, mb, 2 * F + 2, origin=o)
        top = prod.slice(2 * F + 1, 1)
        mant = b.mux(top, prod.slice(F, F + 1), prod.slice(F + 1, F + 1), F + 1, origin=o)
        rbit = b.mux(top, prod.slice(F - 1, 1) if F else SigSpec.const(0, 1), prod.slice(F, 1), 1, origin=o)
        rounded = b.add(mant, rbit.zext(F + 1), F + 2, origin=o)
        carry = rounded.slice(F + 1, 1)
        ew = E + 2
        bias = (1 << (E - 1)) - 1
        e_sum = b.add(ea.zext(ew), eb.zext(ew), ew, origin=o)
        e_adj = b.sub(e_sum, SigSpec.const(bias, ew), ew, origin=o)
        e_adj = b.add(e_adj, top.zext(ew), ew, origin=o)
        e_out = b.add(e_adj, carry.zext(ew), ew, origin=o)
        return self.fl_val(self.fl_pack(sign, e_out.slice(0, E), rounded.slice(0, F)), E, F)

    def fl_div(self, a, c, E, F, o):
        b = self.b
        sa, ea, fa = self.fl_fields(a, E, F)
        sb, eb, fb = self.fl_fields(c, E, F)
        ma, mb = self.fl_mantissa(ea, fa, o), self.fl_mantissa(eb, fb, o)
        sign = b.xor(sa, sb, 1, origin=o)
        QW = 2 * F + 3
        num = SigSpec.concat([SigSpec.const(0, F + 2), ma]).zext(QW)
        q = b.div(num, mb.zext(QW), QW, origin=o)
        top = q.slice(F + 2, 1)
        mant = b.mux(top, q.slice(1, F + 1), q.slice(2, F + 1), F + 1, origin=o)
        rbit = b.mux(top, q.slice(0, 1), q.slice(1, 1), 1, origin=o)
        rounded = b.add(mant, rbit.zext(F + 1), F + 2, origin=o)
        carry = rounded.slice(F + 1, 1)
        ew = E + 2
        bias = (1 << (E - 1)) - 1
        e_diff = b.sub(ea.zext(ew), eb.zext(ew), ew, origin=o)
        e_adj = b.add(e_diff, SigSpec.const(bias, ew), ew, origin=o)
        e_adj = b.sub(e_adj, b.not_(top, 1, origin=o).zext(ew), ew, origin=o)
        e_out = b.add(e_adj, carry.zext(ew), ew, origin=o)
        return self.fl_val(self.fl_pack(sign, e_out.slice(0, E), rounded.slice(0, F)), E, F)

    # ------------------------------------------------------------------ conversions
    def fl_convert(self, s, e, f, e2, f2, o):
        """Bits of float ``(e, f)`` as float ``(e2, f2)``."""
        if (e, f) == (e2, f2):
            return s
        c = s.const_value()
        if c is not None and not c.xmask:
            return SigSpec.const(ieee.float_encode(ieee.float_decode(c.value, e, f), e2, f2), 1 + e2 + f2)
        b = self.b
        sign, exp, frac = self.fl_fields(s, e, f)
        if f2 > f:
            frac2 = SigSpec.concat([SigSpec.const(0, f2 - f), frac])
        elif f2 < f:
            keep = frac.slice(f - f2, f2)
            rbit = frac.slice(f - f2 - 1, 1)
            frac2 = b.add(keep, rbit.zext(f2), f2, origin=o) if f2 else keep
        else:
            frac2 = frac
        if e2 != e:
            w = max(e, e2) + 1
            delta = ((1 << (e2 - 1)) - 1) - ((1 << (e - 1)) - 1)
            exp2w = b.add(exp.zext(w), SigSpec.const(delta & mask(w), w), w, sa=True, sb=True, origin=o)
            exp2 = exp2w.slice(0, e2) if e2 <= w else exp2w.zext(e2)
            if e2 < e:                      # narrowing: clamp to infinity / zero on range overflow
                ovf = b.red_or(exp2w.slice(e2, w - e2), origin=o)
                exp2 = b.mux(ovf, exp2, SigSpec.const(mask(e2), e2), e2, origin=o)
        else:
            exp2 = exp
        return SigSpec.concat([frac2, exp2, sign])

    def fl_format_args(self, vals, sc):
        """(e, f) from ``(x, exponent_width, fraction_width)`` or ``(x, size_res)`` style arguments."""
        if len(vals) >= 2 and vals[1] is not None and is_floatv(vals[1]):
            _s, e, f = self.fl_view(vals[1])
            return e, f
        if len(vals) >= 3 and vals[1] is not None and vals[2] is not None:
            return self.const_int(vals[1]), self.const_int(vals[2])
        m = self.fmodel_of(sc)
        return m.get("float_exponent_width", 8), m.get("float_fraction_width", 23)

    _TOFLOAT_FORMALS = (("arg",), ("exponent_width", "size_res"), ("fraction_width",), ("round_style",),
                        ("denormalize",))

    def bi_to_float(self, name, args, e, sc, hint):
        o = self.org(e)
        vals = self.fx_args(args, sc, self._TOFLOAT_FORMALS)
        x = vals[0]
        if x is None:
            raise Unsupported("%s without argument" % name)
        if name in ieee.FLOAT_FORMATS:
            E, F = ieee.FLOAT_FORMATS[name]
        elif name.startswith("to_float") and name[8:] in ("16", "32", "64", "128"):
            E, F = ieee.FLOAT_FORMATS["float" + name[8:]]
        else:
            E, F = self.fl_format_args(vals, sc)
        W = 1 + E + F
        t = x.t
        if is_floatv(x):
            s, e0, f0 = self.fl_view(x)
            return self.fl_val(self.fl_convert(s, e0, f0, E, F, o), E, F)
        if t.kind == "real" or isinstance(x.c, float) or (x.c is not None and x.sig is None and t.kind == "int"):
            return self.fl_val(SigSpec.const(ieee.float_encode(float(self.const_val(x)), E, F), W), E, F)
        if is_fixed(x):
            s, sg, hi, lo = self.fx_view(x)
            c = self.fx_value_of(x)
            if c is not None:
                return self.fl_val(SigSpec.const(ieee.float_encode(float(c), E, F), W), E, F)
            return self.fl_val(self.fl_from_int(s, sg, lo, E, F, o), E, F)
        if t.kind == "array" and t.cls in ("slv", "suv", "bv"):
            s = self.sig_of(x)
            return self.fl_val(s.resize(W, False), E, F)
        if t.kind == "array" and t.cls in ("u", "s") or t.kind == "int" or t.cls in ("int", "nat"):
            s, sg = self.num(x) if t.kind == "int" else (self.sig_of(x), t.cls == "s")
            c = s.const_value()
            if c is not None and not c.xmask:
                n = c.as_signed() if sg else c.value
                return self.fl_val(SigSpec.const(ieee.float_encode(float(n), E, F), W), E, F)
            return self.fl_val(self.fl_from_int(s, sg, 0, E, F, o), E, F)
        raise Unsupported("%s of %s" % (name, t.name or t.kind))

    def fl_from_int(self, s, signed, lo, E, F, o):
        """Integer (two's complement if ``signed``) ``* 2**lo`` as float ``(E, F)``."""
        self.fl_note()
        b = self.b
        w0 = s.width
        if w0 == 0:
            return SigSpec.const(0, 1 + E + F)
        if signed:
            sign = s.slice(w0 - 1, 1)
            neg = b.neg(s, w0, sa=True, origin=o)
            mag = b.mux(sign, s, neg, w0, origin=o)
        else:
            sign = SigSpec.const(0, 1)
            mag = s
        W = max(w0, F + 3)
        mag = mag.zext(W)
        lz = b.encode(_rev(mag), kind="priority", origin=o)
        norm = b.shl(mag, lz, W, sa=False, origin=o)
        mant = norm.slice(W - 1 - F, F + 1)
        rbit = norm.slice(W - 2 - F, 1)
        rounded = b.add(mant, rbit.zext(F + 1), F + 2, origin=o)
        carry = rounded.slice(F + 1, 1)
        ew = E + 2
        bias = (1 << (E - 1)) - 1
        e0 = b.sub(SigSpec.const((bias + W - 1 + lo) & mask(ew), ew), lz.zext(ew), ew, origin=o)
        e_out = b.add(e0, carry.zext(ew), ew, origin=o)
        packed = self.fl_pack(sign, e_out.slice(0, E), rounded.slice(0, F))
        nonzero = b.red_or(s, origin=o)
        return b.mux(nonzero, SigSpec.const(0, 1 + E + F), packed, 1 + E + F, origin=o)

    def fl_to_int_bits(self, x, width, o):
        """(bits, signed) of the float's truncated integer value in ``width`` bits (two's complement)."""
        self.fl_note()
        b = self.b
        s, E, F = self.fl_view(x)
        sign, exp, frac = self.fl_fields(s, E, F)
        mant = self.fl_mantissa(exp, frac, o)
        bias = (1 << (E - 1)) - 1
        ew = E + 1
        k = b.sub(exp.zext(ew), SigSpec.const(bias, ew), ew, sa=False, sb=False, origin=o)
        W = max(width, F + 1) + F + 1
        big = b.shl(mant.zext(W), k.slice(0, max(1, min(k.width, W.bit_length() + 1))), W, sa=False, origin=o)
        val = b.shr(big, SigSpec.const(F, max(1, F.bit_length())), W, sa=False, origin=o).slice(0, width)
        neg = b.neg(val, width, sa=True, origin=o)
        return b.mux(sign, val, neg, width, origin=o)

    def fl_to_integer(self, x, e):
        o = self.org(e)
        c = self.fl_const(x)
        if c is not None and math.isfinite(c):
            return int_val(int(c))
        bits = self.fl_to_int_bits(x, 32, o)
        return Val(range_int_type(-2 ** 31, 2 ** 31 - 1), bits)

    def fl_to_vec(self, name, args, e, sc):
        o = self.org(e)
        vals = self.call_args(name, args, sc)
        x = vals[0]
        n = vals[1]
        w = self.sig_of(n).width if n.t.kind == "array" else self.const_int(n)
        c = self.fl_const(x)
        cls = "u" if name == "to_unsigned" else "s"
        if c is not None and math.isfinite(c):
            return Val(T.vec_type(cls, w), SigSpec.const(int(c) & mask(w), w))
        return Val(T.vec_type(cls, w), self.fl_to_int_bits(x, w, o))

    def fl_to_real(self, x):
        c = self.fl_const(x)
        if c is None:
            raise Unsupported("to_real of a non-constant floating-point value")
        return real_val(c)

    def fl_resize(self, args, e, sc):
        o = self.org(e)
        vals = self.fx_args(args, sc, self._TOFLOAT_FORMALS)
        x = vals[0]
        E, F = self.fl_format_args(vals, sc)
        s, e0, f0 = self.fl_view(x)
        return self.fl_val(self.fl_convert(s, e0, f0, E, F, o), E, F)

    def fl_scalb(self, y, n, e):
        o = self.org(e)
        b = self.b
        s, E, F = self.fl_view(y)
        sign, exp, frac = self.fl_fields(s, E, F)
        k, kg = (self.num(n) if n.t.kind == "int" else (self.sig_of(n), n.t.cls == "s"))
        if n.t.kind == "int" and n.c is not None and n.sig is None:
            k, kg = SigSpec.const(int(n.c) & mask(E), E), True
        e2 = b.add(exp, k.resize(E, kg), E, origin=o)
        return self.fl_val(self.fl_pack(sign, e2, frac), E, F)

    # ------------------------------------------------------------------ classification
    def bi_fl_class(self, name, args, e, sc, hint):
        (x,) = self.call_args(name, args, sc)[:1]
        o = self.org(e)
        b = self.b
        s, E, F = self.fl_view(x)
        sign, exp, frac = self.fl_fields(s, E, F)
        n = name.replace("_", "")
        if n in ("isnan", "isnan"):
            return bool_val(b.and_(b.red_and(exp, origin=o), b.red_or(frac, origin=o), 1, origin=o))
        if n in ("isinfinity", "isinf"):
            return bool_val(b.and_(b.red_and(exp, origin=o), b.not_(b.red_or(frac, origin=o), 1, origin=o), 1, origin=o))
        if n in ("iszero",):
            return bool_val(b.not_(b.red_or(s.slice(0, E + F), origin=o), 1, origin=o))
        raise Unsupported(name)


FLOAT_BUILTINS = {
    "to_float": FloatMixin.bi_to_float, "to_float16": FloatMixin.bi_to_float, "to_float32": FloatMixin.bi_to_float,
    "to_float64": FloatMixin.bi_to_float, "to_float128": FloatMixin.bi_to_float,
    "isnan": FloatMixin.bi_fl_class, "is_nan": FloatMixin.bi_fl_class, "isinfinity": FloatMixin.bi_fl_class,
    "is_infinity": FloatMixin.bi_fl_class, "iszero": FloatMixin.bi_fl_class, "is_zero": FloatMixin.bi_fl_class,
}
