"""``cost_op``: the single entry point that maps an operation and its attributes to an ``OpCost``."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from . import arith, logic, seq
from .acc import OpCost, cap, zero
from .profile import Profile
from .units import clog2

CMP_OPS = ("EQ", "NE", "LT", "LE", "GT", "GE")
BITWISE_OPS = ("AND", "OR", "XOR", "NAND", "NOR", "XNOR", "NOT")
REDUCE_OPS = {"RED_AND": "AND", "RED_OR": "AND", "RED_XOR": "XOR"}


def _int(a: Mapping[str, Any], *names: str, default: int | None = None, lo: int = 1) -> int:
    for nm in names:
        if nm in a and a[nm] is not None:
            v = a[nm]
            if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                raise ValueError(f"attribute {nm} must be an integer >= {lo}, got {v!r}")
            return v
    if default is None:
        raise ValueError(f"missing attribute {names[0]}")
    return default


def _const(a: Mapping[str, Any]) -> tuple[bool, int | None]:
    """(has a constant operand, its value or None when unknown)."""
    k = a.get("const")
    if k is not None and not isinstance(k, bool):
        if not isinstance(k, int):
            raise ValueError(f"attribute const must be an integer, got {k!r}")
        return True, k
    return bool(a.get("const_unknown") or k is True), None


def _addsub(op: str, a: Mapping[str, Any], p: Profile) -> OpCost:
    n = _int(a, "n", "w", "width")
    if a.get("carry_only"):
        return logic.lt_op(n, p)
    has, k = _const(a)
    if has:
        return arith.add_const_op(n, k, p, sub=op == "SUB")
    return arith.add_op(n, p, sub=op == "SUB")


def _mul(a: Mapping[str, Any], p: Profile) -> OpCost:
    n = _int(a, "n", "w", "width")
    has, k = _const(a)
    out = _int(a, "out", default=0, lo=0) or None
    if has:
        kb = n if k is None else max(1, abs(k).bit_length())
        oc = arith.mul_const_op(n, k, p, out)
        return cap(oc, arith.mul_op(n, kb, p, bool(a.get("signed"))))
    m = _int(a, "m")
    trunc = _int(a, "trunc", default=0, lo=0) or None
    return arith.mul_op(n, m, p, bool(a.get("signed")), bool(a.get("square")), trunc)


def _div(op: str, a: Mapping[str, Any], p: Profile) -> OpCost:
    n = _int(a, "n")
    has, k = _const(a)
    if has and k is not None:
        return arith.div_op(n, max(1, k.bit_length()), p, bool(a.get("signed")), k, op == "MOD")
    m = _int(a, "m")
    return arith.div_op(n, m, p, bool(a.get("signed")))


def _pow(a: Mapping[str, Any], p: Profile) -> OpCost:
    if a.get("base") == 2:  # 2 ** x is a decoder of the exponent
        oc = logic.decode_op(_int(a, "s", "n"), p)
        return OpCost(oc.min, oc.typ, oc.max, oc.arch, oc.breakdown, "mul", "medium", oc.flags)
    return arith.pow_op(_int(a, "n"), _int(a, "exp", lo=0), p, _int(a, "out", default=0, lo=0) or None)


def _cmp(op: str, a: Mapping[str, Any], p: Profile) -> OpCost:
    n = _int(a, "n", "w", "width")
    has, _ = _const(a)
    if op in ("EQ", "NE"):
        return logic.eq_op(n, p, has)
    return logic.lt_op(n, p, bool(a.get("signed")), has)


def _blackbox(a: Mapping[str, Any], p: Profile) -> OpCost:
    name = str(a.get("name", ""))
    c = p.blackbox_cost(name)
    if c is None:
        return OpCost(0, 0, 0, {"min": "unknown", "typ": "unknown", "max": "unknown"}, {}, "logic", "poor", ("unknown_blackbox",))
    return OpCost(c[0], c[1], c[2], {"min": "blackbox", "typ": "blackbox", "max": "blackbox"}, {}, "logic", "medium")


_H: dict[str, Callable[[str, Mapping[str, Any], Profile], OpCost]] = {}


def _reg(names, fn) -> None:
    for nm in names:
        _H[nm] = fn


_reg(("ADD", "SUB"), lambda op, a, p: _addsub(op, a, p))
_reg(("NEG",), lambda op, a, p: arith.neg_op(_int(a, "n", "w", "width"), p))
_reg(("SUM",), lambda op, a, p: arith.sum_op(_int(a, "k"), _int(a, "w", "n"), p))
_reg(("MUL",), lambda op, a, p: _mul(a, p))
_reg(("MAC",), lambda op, a, p: arith.mac_op(_int(a, "n"), _int(a, "m"), _int(a, "c", default=_int(a, "n") + _int(a, "m")), p, bool(a.get("signed"))))
_reg(("DIV", "MOD"), lambda op, a, p: _div(op, a, p))
_reg(("POW",), lambda op, a, p: _pow(a, p))
_reg(logic.SHIFTS, lambda op, a, p: logic.shift_op(op, _int(a, "n"), _int(a, "s", default=1, lo=0), p, bool(a.get("const_amount")), bool(a.get("lhs_one"))))
_reg(BITWISE_OPS, lambda op, a, p: logic.bitwise_op(op, _int(a, "n", "w", "width"), p))
_reg(CMP_OPS, lambda op, a, p: _cmp(op, a, p))
_reg(tuple(REDUCE_OPS), lambda op, a, p: logic.reduce_op(REDUCE_OPS[op], _int(a, "n", "w", "width"), p))
_reg(("MUX",), lambda op, a, p: logic.mux_op(_int(a, "k"), _int(a, "w", "n"), p, _int(a, "const_inputs", default=0, lo=0), bool(a.get("same_inputs"))))
_reg(("PMUX",), lambda op, a, p: logic.pmux_op(_int(a, "k"), _int(a, "w", "n"), p))
_reg(("PRIORITY",), lambda op, a, p: logic.priority_op(_int(a, "k"), _int(a, "w", "n"), p))
_reg(("DECODE",), lambda op, a, p: logic.decode_op(_int(a, "s", lo=0), p))
_reg(("ENCODE",), lambda op, a, p: logic.encode_op(_int(a, "k"), p, bool(a.get("priority"))))
_reg(("DFF", "REG"), lambda op, a, p: seq.reg_op(
    _int(a, "n", "w", "width"), p, bool(a.get("async_reset")), bool(a.get("async_set_reset")),
    bool(a.get("sync_reset")), bool(a.get("enable")), a.get("scan")))
_reg(("LATCH", "DLAT"), lambda op, a, p: seq.latch_op(_int(a, "n", "w", "width"), p))
_reg(("MEM",), lambda op, a, p: seq.mem_op(_int(a, "depth"), _int(a, "width", "w"), p, _int(a, "rports", default=1), _int(a, "wports", default=1), bool(a.get("sync_read"))))
_reg(("ROM",), lambda op, a, p: seq.rom_op(_int(a, "depth"), _int(a, "width", "w"), p, a.get("contents")))
_reg(("BLACKBOX",), lambda op, a, p: _blackbox(a, p))

OPS = tuple(sorted(_H))


def cost_op(op: str, attrs: Mapping[str, Any], profile: Profile) -> OpCost:
    """Cost of one operation. See the package docstring for the attribute names of each op.

    Raises ``ValueError`` for an unknown operation or an invalid attribute.
    """
    h = _H.get(op.upper())
    if h is None:
        raise ValueError(f"unknown operation {op!r}")
    return h(op.upper(), attrs, profile)
