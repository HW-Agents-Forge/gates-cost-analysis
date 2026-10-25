"""Model of ``ieee.fixed_pkg`` / ``ieee.float_pkg`` (VHDL-2008) as data and pure functions.

Re-exported by :mod:`gatecost.frontend.vhdl.ieee`.  Nothing here touches the IR.

``ufixed``/``sfixed`` are arrays indexed ``high downto low``; ``low`` may be negative, the bit at index ``i``
weighs ``2**i`` (the sign bit of ``sfixed`` is index ``high``).  Ranges are ``(high, low)`` tuples.
``float(e downto -f)`` holds a sign bit (index ``e``), ``e`` exponent bits and ``f`` fraction bits.
"""
from __future__ import annotations

import math
from fractions import Fraction

FIXED_CLASSES = ("uf", "sf")
FIXED_OVERFLOW_STYLES = ("fixed_wrap", "fixed_saturate")
FIXED_ROUND_STYLES = ("fixed_round", "fixed_truncate")
#: package defaults: ``fixed_overflow_style`` / ``fixed_round_style``
DEFAULT_OVERFLOW = "fixed_saturate"
DEFAULT_ROUND = "fixed_round"


def fixed_result_range(op, a, b=None, signed=False):
    """``(high, low)`` of the fixed_pkg result of ``op`` on ranges ``a`` (and ``b``).

    ``+ -``: ``max(high)+1 downto min(low)``; ``*``: ``ha+hb+1 downto la+lb``; ``/``: ``ha-lb downto la-hb-1``
    (signed: ``ha-lb+1 downto la-hb``); ``rem``: ``min(high) downto min(low)``; ``mod``: ``hb downto min(low)``;
    ``neg``/``abs`` (signed) and ``to_signed`` of a ufixed: ``ha+1 downto la``.
    """
    ah, al = a
    if op in ("neg", "abs", "to_signed"):
        return ah + 1, al
    bh, bl = b
    if op in ("+", "-"):
        return max(ah, bh) + 1, min(al, bl)
    if op == "*":
        return ah + bh + 1, al + bl
    if op == "/":
        return (ah - bl + 1, al - bh) if signed else (ah - bl, al - bh - 1)
    if op == "rem":
        return min(ah, bh), min(al, bl)
    if op == "mod":
        return bh, min(al, bl)
    raise ValueError("no fixed_pkg range rule for %r" % (op,))


def fixed_quantize(x, high, low, signed, saturate=True, rounding=True):
    """Bit pattern (non-negative int of ``high-low+1`` bits) of the real/integer ``x`` in ``high downto low``.

    ``rounding`` rounds to nearest (ties up, ``fixed_round``), else truncates towards minus infinity
    (``fixed_truncate``); ``saturate`` clamps (``fixed_saturate``), else wraps (``fixed_wrap``).  None when ``x``
    is not finite.
    """
    try:
        f = Fraction(x) * (Fraction(2) ** (-low))
    except (ValueError, OverflowError, ZeroDivisionError, TypeError):
        return None
    n = math.floor(f + Fraction(1, 2)) if rounding else math.floor(f)
    w = high - low + 1
    if w <= 0:
        return 0
    lo, hi = (-(1 << (w - 1)), (1 << (w - 1)) - 1) if signed else (0, (1 << w) - 1)
    if saturate:
        n = max(lo, min(hi, n))
    return n & ((1 << w) - 1)


def fixed_to_real(pattern, high, low, signed):
    """Value of a ``high downto low`` fixed-point bit pattern (a Python float)."""
    w = high - low + 1
    if w <= 0:
        return 0.0
    n = pattern & ((1 << w) - 1)
    if signed and n >> (w - 1):
        n -= 1 << w
    try:
        return math.ldexp(float(n), low)
    except OverflowError:
        return math.inf if n > 0 else -math.inf


#: ``float_pkg`` subtypes: name -> (exponent bits, fraction bits); ``float(e downto -f)``
FLOAT_FORMATS = {"float16": (5, 10), "float32": (8, 23), "float64": (11, 52), "float128": (15, 112)}
FLOAT_BASE_NAMES = ("float", "unresolved_float", "u_float")


def _half():
    return Fraction(1, 2)


def _rne(q):
    n = math.floor(q)
    r = q - n
    if r > _half() or (r == _half() and (n & 1)):
        n += 1
    return n


def float_encode(x, e, f):
    """Bit pattern (sign, ``e`` exponent bits, ``f`` fraction bits) of ``x``; round to nearest even."""
    sign = 1 if math.copysign(1.0, x) < 0 else 0
    top = sign << (e + f)
    allones = ((1 << e) - 1) << f
    if math.isnan(x):
        return allones | ((1 << (f - 1)) if f else 0) | top
    if math.isinf(x):
        return allones | top
    ax = Fraction(abs(x))
    if ax == 0:
        return top
    bias = (1 << (e - 1)) - 1
    ex = ax.numerator.bit_length() - ax.denominator.bit_length()
    if ax < Fraction(2) ** ex:
        ex -= 1
    # ax = m * 2**ex with 1 <= m < 2
    if ex + bias <= 0:                       # subnormal
        n = _rne(ax / (Fraction(2) ** (1 - bias - f)))
        if n >> f:
            return top | (1 << f)            # rounded up to the smallest normal number
        return top | n
    n = _rne((ax / (Fraction(2) ** ex) - 1) * (1 << f))
    if n >> f:
        n = 0
        ex += 1
    if ex + bias >= (1 << e) - 1:
        return allones | top
    return top | ((ex + bias) << f) | n


def float_decode(bits, e, f):
    """Python float of an ``e``/``f`` floating-point bit pattern."""
    sign = -1.0 if (bits >> (e + f)) & 1 else 1.0
    ex = (bits >> f) & ((1 << e) - 1)
    fr = bits & ((1 << f) - 1)
    bias = (1 << (e - 1)) - 1
    if ex == (1 << e) - 1:
        return sign * math.inf if fr == 0 else math.nan
    if ex == 0:
        return sign * math.ldexp(float(fr), 1 - bias - f)
    return sign * math.ldexp(float((1 << f) | fr), ex - bias - f)
