"""Integer helpers: costs are integer milli-GE, ratios are integer permille.

No floating point is used in any cost evaluation; the only conversions from float are
at profile load time (``to_milli``).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def clog2(n: int) -> int:
    """ceil(log2(n)) for n >= 1, via ``int.bit_length`` (clog2(1) == 0)."""
    if n < 1:
        raise ValueError("clog2 needs n >= 1")
    return (n - 1).bit_length()


def flog2(n: int) -> int:
    """floor(log2(n)) for n >= 1."""
    if n < 1:
        raise ValueError("flog2 needs n >= 1")
    return n.bit_length() - 1


def rdiv(a: int, b: int) -> int:
    """Integer division rounding half away from zero (b > 0)."""
    if b <= 0:
        raise ValueError("rdiv needs b > 0")
    q = (abs(a) * 2 + b) // (2 * b)
    return -q if a < 0 else q


def pm(x: int, permille: int) -> int:
    """Scale ``x`` by ``permille / 1000``, rounding half away from zero."""
    return rdiv(x * permille, 1000)


def to_milli(x: float | int | str) -> int:
    """GE (or any unit-less ratio) to milli, decimal-exact for short literals."""
    return int((Decimal(str(x)) * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)
