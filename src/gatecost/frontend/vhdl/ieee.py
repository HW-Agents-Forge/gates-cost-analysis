"""Built-in model of the standard packages, as data (FRONTEND.md 10.3), plus a width-rule table.

Nothing here parses VHDL: the future elaborator looks operators and functions up by name and
operand *type classes*, and obtains the result type class and a *width rule* that is evaluated with
:func:`result_width`.

Type classes (strings): ``u`` unsigned, ``s`` signed, ``slv`` std_logic_vector, ``suv``
std_ulogic_vector, ``bv`` bit_vector, ``sl`` std_(u)logic scalar, ``bit``, ``int`` integer,
``nat`` natural/positive, ``bool``, ``real``, ``str`` string.  ``slv`` is accepted where ``suv`` is
expected (VHDL-2008 subtype relation); ``nat``/``int`` accept each other (a literal is universal).

Width rules (tuples, evaluated by :func:`result_width` against operand widths ``w`` and known
constant argument values ``c``):

    ("arg", i)            w[i]
    ("max", i, j)         max(w[i], w[j])
    ("sum", i, j)         w[i] + w[j]
    ("twice", i)          2 * w[i]
    ("max_adj", i, a, j, b)  max(w[i] + a, w[j] + b)      (std_logic_arith mixed signedness)
    ("param", i)          c[i]  : the value of constant argument i (resize/to_unsigned size)
    ("const", n)          n     (scalar results are 1)
    ("none",)             not a vector / width not derivable from operands
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FuncSig:
    package: str          # lower-case package name ("numeric_std")
    name: str             # lower-case function name or operator symbol ("+", "and", "resize")
    params: tuple         # operand type classes
    result: str           # result type class
    width: tuple          # width rule
    sem: str | None = None  # operand interpretation for slv ops: "u" or "s" (legacy packages)
    note: str = ""


# ------------------------------------------------------------------ type table
TYPES = {
    # std.standard
    "boolean": dict(cls="bool", kind="enum", width=1, signed=False, package="standard"),
    "bit": dict(cls="bit", kind="enum", width=1, signed=False, package="standard"),
    "character": dict(cls="str", kind="enum", width=8, signed=False, package="standard"),
    "severity_level": dict(cls="str", kind="enum", width=2, signed=False, package="standard"),
    "integer": dict(cls="int", kind="int", width=32, signed=True, low=-2 ** 31, high=2 ** 31 - 1, package="standard"),
    "natural": dict(cls="nat", kind="int", width=31, signed=False, low=0, high=2 ** 31 - 1, package="standard"),
    "positive": dict(cls="nat", kind="int", width=31, signed=False, low=1, high=2 ** 31 - 1, package="standard"),
    "real": dict(cls="real", kind="real", width=None, signed=True, package="standard"),
    "time": dict(cls="time", kind="physical", width=None, signed=True, package="standard"),
    "string": dict(cls="str", kind="array", element="character", package="standard"),
    "bit_vector": dict(cls="bv", kind="array", element="bit", package="standard"),
    # ieee.std_logic_1164
    "std_ulogic": dict(cls="sl", kind="enum", width=1, signed=False, package="std_logic_1164"),
    "std_logic": dict(cls="sl", kind="enum", width=1, signed=False, package="std_logic_1164"),
    "x01": dict(cls="sl", kind="enum", width=1, signed=False, package="std_logic_1164"),
    "x01z": dict(cls="sl", kind="enum", width=1, signed=False, package="std_logic_1164"),
    "ux01": dict(cls="sl", kind="enum", width=1, signed=False, package="std_logic_1164"),
    "ux01z": dict(cls="sl", kind="enum", width=1, signed=False, package="std_logic_1164"),
    "std_ulogic_vector": dict(cls="suv", kind="array", element="std_ulogic", package="std_logic_1164"),
    "std_logic_vector": dict(cls="slv", kind="array", element="std_logic", package="std_logic_1164"),
    # numeric_std / std_logic_arith
    "unsigned": dict(cls="u", kind="array", element="std_logic", package="numeric_std"),
    "signed": dict(cls="s", kind="array", element="std_logic", package="numeric_std"),
}

STD_LOGIC_VALUES = "UX01ZWLH-"
#: values folded to 0 / 1 / don't-care / tri-state when costing literals (FRONTEND.md 10.4)
STD_LOGIC_ZERO = "0L"
STD_LOGIC_ONE = "1H"
STD_LOGIC_DONT_CARE = "UXW-"
STD_LOGIC_TRISTATE = "Z"


def type_class(type_name: str) -> str | None:
    t = TYPES.get(type_name.lower())
    return t["cls"] if t else None


_ACCEPTS = {
    "suv": {"suv", "slv"}, "slv": {"slv"}, "int": {"int", "nat"}, "nat": {"nat", "int"},
    "sl": {"sl", "bit"}, "u": {"u"}, "s": {"s"}, "bool": {"bool"}, "bv": {"bv"}, "bit": {"bit", "sl"},
    "real": {"real", "int", "nat"}, "any": None,
}


def compatible(param: str, actual: str) -> bool:
    acc = _ACCEPTS.get(param)
    return True if param == "any" else (actual in acc if acc is not None else actual == param)


# ------------------------------------------------------------------ signature tables
_SIGS: list[FuncSig] = []


def _add(pkg, names, params, result, width, sem=None, note=""):
    for n in (names if isinstance(names, (tuple, list)) else (names,)):
        _SIGS.append(FuncSig(pkg, n, tuple(params), result, tuple(width), sem, note))


_CMP = ("=", "/=", "<", "<=", ">", ">=")
_MATCH = ("?=", "?/=", "?<", "?<=", "?>", "?>=")
_LOGIC = ("and", "or", "nand", "nor", "xor", "xnor")


def _numeric_family(pkg, vec, scal_u, scal_s, signed_cls=None, sem=None):
    """Operators of numeric_std-style packages for vector class ``vec`` with scalar class ``scal``."""
    T = vec
    N = scal_s if vec in ("s",) else scal_u
    _add(pkg, ("+", "-"), (T, T), T, ("max", 0, 1), sem)
    _add(pkg, ("+", "-"), (T, N), T, ("arg", 0), sem)
    _add(pkg, ("+", "-"), (N, T), T, ("arg", 1), sem)
    _add(pkg, "*", (T, T), T, ("sum", 0, 1), sem)
    _add(pkg, "*", (T, N), T, ("twice", 0), sem)
    _add(pkg, "*", (N, T), T, ("twice", 1), sem)
    _add(pkg, "/", (T, T), T, ("arg", 0), sem)
    _add(pkg, "/", (T, N), T, ("arg", 0), sem)
    _add(pkg, "/", (N, T), T, ("arg", 1), sem)
    _add(pkg, ("rem", "mod"), (T, T), T, ("arg", 1), sem)
    _add(pkg, ("rem", "mod"), (T, N), T, ("arg", 0), sem)
    _add(pkg, ("rem", "mod"), (N, T), T, ("arg", 1), sem)
    _add(pkg, _CMP, (T, T), "bool", ("const", 1), sem)
    _add(pkg, _CMP, (T, N), "bool", ("const", 1), sem)
    _add(pkg, _CMP, (N, T), "bool", ("const", 1), sem)
    _add(pkg, _MATCH, (T, T), "sl", ("const", 1), sem)
    _add(pkg, _MATCH, (T, N), "sl", ("const", 1), sem)
    _add(pkg, _MATCH, (N, T), "sl", ("const", 1), sem)


def _numeric_std_extras(pkg, T, N):
    _add(pkg, _LOGIC, (T, T), T, ("arg", 0))
    _add(pkg, _LOGIC, (T, "sl"), T, ("arg", 0), note="VHDL-2008 array/scalar")
    _add(pkg, _LOGIC, ("sl", T), T, ("arg", 1), note="VHDL-2008 array/scalar")
    _add(pkg, _LOGIC, (T,), "sl", ("const", 1), note="VHDL-2008 reduction")
    _add(pkg, "not", (T,), T, ("arg", 0))
    _add(pkg, ("shift_left", "shift_right", "rotate_left", "rotate_right"), (T, "nat"), T, ("arg", 0))
    _add(pkg, ("sll", "srl", "rol", "ror", "sla", "sra"), (T, "int"), T, ("arg", 0))
    _add(pkg, "resize", (T, "nat"), T, ("param", 1))
    _add(pkg, "std_match", (T, T), "bool", ("const", 1))
    _add(pkg, "is_x", (T,), "bool", ("const", 1))
    _add(pkg, "to_01", (T,), T, ("arg", 0))
    _add(pkg, "to_01", (T, "sl"), T, ("arg", 0))
    _add(pkg, ("minimum", "maximum"), (T, T), T, ("max", 0, 1), note="VHDL-2008")
    _add(pkg, ("minimum", "maximum"), (T, N), T, ("arg", 0), note="VHDL-2008")
    _add(pkg, ("minimum", "maximum"), (N, T), T, ("arg", 1), note="VHDL-2008")
    _add(pkg, ("find_leftmost", "find_rightmost"), (T, "sl"), "int", ("none",), note="VHDL-2008")


# numeric_std
_numeric_family("numeric_std", "u", "nat", "int")
_numeric_family("numeric_std", "s", "nat", "int")
_add("numeric_std", "-", ("s",), "s", ("arg", 0))
_add("numeric_std", "abs", ("s",), "s", ("arg", 0))
_numeric_std_extras("numeric_std", "u", "nat")
_numeric_std_extras("numeric_std", "s", "int")
_add("numeric_std", "to_integer", ("u",), "nat", ("none",))
_add("numeric_std", "to_integer", ("s",), "int", ("none",))
_add("numeric_std", "to_unsigned", ("nat", "nat"), "u", ("param", 1))
_add("numeric_std", "to_signed", ("int", "nat"), "s", ("param", 1))
_add("numeric_std", "to_unsigned", ("nat", "u"), "u", ("arg", 1), note="VHDL-2008 size_res form")
_add("numeric_std", "to_signed", ("int", "s"), "s", ("arg", 1), note="VHDL-2008 size_res form")
_add("numeric_std", "resize", ("u", "u"), "u", ("arg", 1), note="VHDL-2008 size_res form")
_add("numeric_std", "resize", ("s", "s"), "s", ("arg", 1), note="VHDL-2008 size_res form")

# numeric_std_unsigned: std_ulogic_vector with unsigned semantics
_numeric_family("numeric_std_unsigned", "suv", "nat", "nat", sem="u")
_add("numeric_std_unsigned", "to_integer", ("suv",), "nat", ("none",), "u")
_add("numeric_std_unsigned", ("to_stdulogicvector", "to_stdlogicvector"), ("nat", "nat"), "suv", ("param", 1), "u")
_add("numeric_std_unsigned", ("to_stdulogicvector", "to_stdlogicvector"), ("nat", "suv"), "suv", ("arg", 1), "u")
_add("numeric_std_unsigned", "resize", ("suv", "nat"), "suv", ("param", 1), "u")
_add("numeric_std_unsigned", ("shift_left", "shift_right", "rotate_left", "rotate_right"), ("suv", "nat"), "suv",
     ("arg", 0), "u")

# std_logic_1164
_add("std_logic_1164", _LOGIC, ("sl", "sl"), "sl", ("const", 1))
_add("std_logic_1164", _LOGIC, ("suv", "suv"), "suv", ("arg", 0))
_add("std_logic_1164", _LOGIC, ("suv", "sl"), "suv", ("arg", 0), note="VHDL-2008")
_add("std_logic_1164", _LOGIC, ("sl", "suv"), "suv", ("arg", 1), note="VHDL-2008")
_add("std_logic_1164", _LOGIC, ("suv",), "sl", ("const", 1), note="VHDL-2008 reduction")
_add("std_logic_1164", "not", ("sl",), "sl", ("const", 1))
_add("std_logic_1164", "not", ("suv",), "suv", ("arg", 0))
_add("std_logic_1164", ("rising_edge", "falling_edge"), ("sl",), "bool", ("const", 1))
_add("std_logic_1164", ("to_bit", "to_x01", "to_x01z", "to_ux01", "to_01"), ("sl",), "sl", ("const", 1))
_add("std_logic_1164", ("to_bitvector",), ("suv",), "bv", ("arg", 0))
_add("std_logic_1164", ("to_stdlogicvector", "to_stdulogicvector"), ("bv",), "slv", ("arg", 0))
_add("std_logic_1164", ("to_stdlogicvector", "to_stdulogicvector"), ("suv",), "slv", ("arg", 0))
_add("std_logic_1164", ("to_x01", "to_x01z", "to_ux01", "to_01"), ("suv",), "suv", ("arg", 0))
_add("std_logic_1164", "to_stdulogic", ("bit",), "sl", ("const", 1))
_add("std_logic_1164", "is_x", ("sl",), "bool", ("const", 1))
_add("std_logic_1164", "is_x", ("suv",), "bool", ("const", 1))
_add("std_logic_1164", "??", ("sl",), "bool", ("const", 1), note="VHDL-2008 condition operator")

# std_logic_arith (Synopsys): unsigned/signed with mixed-signedness rules
P = "std_logic_arith"
_add(P, ("+", "-"), ("u", "u"), "u", ("max", 0, 1))
_add(P, ("+", "-"), ("s", "s"), "s", ("max", 0, 1))
_add(P, ("+", "-"), ("u", "s"), "s", ("max_adj", 0, 1, 1, 0), note="unsigned operand gains a sign bit")
_add(P, ("+", "-"), ("s", "u"), "s", ("max_adj", 0, 0, 1, 1), note="unsigned operand gains a sign bit")
for _T in ("u", "s"):
    _add(P, ("+", "-"), (_T, "int"), _T, ("arg", 0))
    _add(P, ("+", "-"), ("int", _T), _T, ("arg", 1))
    _add(P, ("+", "-"), (_T, "sl"), _T, ("arg", 0))
    _add(P, ("+", "-"), ("sl", _T), _T, ("arg", 1))
_add(P, "+", ("u",), "u", ("arg", 0))
_add(P, ("+", "-", "abs"), ("s",), "s", ("arg", 0))
_add(P, "*", ("u", "u"), "u", ("sum", 0, 1))
_add(P, "*", ("s", "s"), "s", ("sum", 0, 1))
_add(P, "*", ("u", "s"), "s", ("sum", 0, 1))
_add(P, "*", ("s", "u"), "s", ("sum", 0, 1))
for _a, _b in (("u", "u"), ("s", "s"), ("u", "s"), ("s", "u"), ("u", "int"), ("int", "u"), ("s", "int"), ("int", "s")):
    _add(P, _CMP, (_a, _b), "bool", ("const", 1))
_add(P, "conv_integer", ("u",), "int", ("none",))
_add(P, "conv_integer", ("s",), "int", ("none",))
_add(P, "conv_integer", ("int",), "int", ("none",))
_add(P, "conv_integer", ("sl",), "int", ("none",))
for _src in ("int", "u", "s", "sl"):
    _add(P, "conv_unsigned", (_src, "nat"), "u", ("param", 1))
    _add(P, "conv_signed", (_src, "nat"), "s", ("param", 1))
    _add(P, "conv_std_logic_vector", (_src, "nat"), "slv", ("param", 1))
_add(P, ("shl", "shr"), ("u", "u"), "u", ("arg", 0))
_add(P, ("shl", "shr"), ("s", "u"), "s", ("arg", 0))
_add(P, ("ext", "sxt"), ("slv", "nat"), "slv", ("param", 1))

# std_logic_unsigned / std_logic_signed: std_logic_vector operands
for _P, _sem in (("std_logic_unsigned", "u"), ("std_logic_signed", "s")):
    _add(_P, ("+", "-"), ("slv", "slv"), "slv", ("max", 0, 1), _sem)
    _add(_P, ("+", "-"), ("slv", "int"), "slv", ("arg", 0), _sem)
    _add(_P, ("+", "-"), ("int", "slv"), "slv", ("arg", 1), _sem)
    _add(_P, ("+", "-"), ("slv", "sl"), "slv", ("arg", 0), _sem)
    _add(_P, ("+", "-"), ("sl", "slv"), "slv", ("arg", 1), _sem)
    _add(_P, "+", ("slv",), "slv", ("arg", 0), _sem)
    _add(_P, "*", ("slv", "slv"), "slv", ("sum", 0, 1), _sem)
    _add(_P, _CMP, ("slv", "slv"), "bool", ("const", 1), _sem)
    _add(_P, _CMP, ("slv", "int"), "bool", ("const", 1), _sem)
    _add(_P, _CMP, ("int", "slv"), "bool", ("const", 1), _sem)
    _add(_P, "conv_integer", ("slv",), "int", ("none",), _sem)
    _add(_P, ("shl", "shr"), ("slv", "slv"), "slv", ("arg", 0), _sem)
_add("std_logic_signed", ("-", "abs"), ("slv",), "slv", ("arg", 0), "s")

SIGNATURES: tuple = tuple(_SIGS)
del P, _T, _P, _sem, _a, _b, _src

_BY_NAME: dict = {}
for _s in SIGNATURES:
    _BY_NAME.setdefault(_s.name, []).append(_s)

#: packages the model knows, as (library, package)
KNOWN_PACKAGES = frozenset(
    [("std", "standard"), ("std", "textio"), ("std", "env"),
     ("ieee", "std_logic_1164"), ("ieee", "numeric_std"), ("ieee", "numeric_std_unsigned"),
     ("ieee", "math_real"), ("ieee", "math_complex"), ("ieee", "std_logic_arith"),
     ("ieee", "std_logic_unsigned"), ("ieee", "std_logic_signed"), ("ieee", "std_logic_textio"),
     ("ieee", "std_logic_misc")])
#: recognized and ignored (no effect on the estimate)
IGNORED_PACKAGES = frozenset(["std.textio", "std.env", "ieee.std_logic_textio"])


def lookup(name: str, packages=None):
    """Signatures named ``name`` (lower case), optionally restricted to ``packages`` (names in scope order)."""
    sigs = _BY_NAME.get(name.lower(), [])
    if packages is None:
        return list(sigs)
    order = {p: i for i, p in enumerate(packages)}
    return sorted((s for s in sigs if s.package in order), key=lambda s: order[s.package])


def resolve(name: str, arg_classes, packages=None):
    """First signature matching the operand type classes, preferring exact class matches."""
    cands = [s for s in lookup(name, packages) if len(s.params) == len(arg_classes)
             and all(compatible(p, a) for p, a in zip(s.params, arg_classes))]
    if not cands:
        return None
    for s in cands:
        if tuple(s.params) == tuple(arg_classes):
            return s
    return cands[0]


def result_width(rule, widths, consts=()):
    """Evaluate a width rule. ``widths[i]`` is the width of operand i (None if unknown or scalar-not-vector);
    ``consts[i]`` the constant value of operand i when known. Returns an int or None if not derivable."""
    kind = rule[0]

    def w(i):
        return widths[i] if i < len(widths) else None

    if kind == "const":
        return rule[1]
    if kind == "none":
        return None
    if kind == "arg":
        return w(rule[1])
    if kind == "param":
        i = rule[1]
        return consts[i] if i < len(consts) else None
    if kind == "twice":
        x = w(rule[1])
        return None if x is None else 2 * x
    if kind in ("max", "sum"):
        a, b = w(rule[1]), w(rule[2])
        if a is None or b is None:
            return None
        return max(a, b) if kind == "max" else a + b
    if kind == "max_adj":
        a, b = w(rule[1]), w(rule[3])
        if a is None or b is None:
            return None
        return max(a + rule[2], b + rule[4])
    raise ValueError("unknown width rule %r" % (rule,))


def scope_packages(use_paths, default=("standard",)):
    """Package names made visible by ``use`` clauses (``ieee.numeric_std.all`` -> ``numeric_std``)."""
    out = list(default)
    for p in use_paths:
        if len(p) >= 2 and (p[0], p[1]) in KNOWN_PACKAGES and p[1] not in out:
            out.append(p[1])
    return out


# ------------------------------------------------------------------ math_real
MATH_REAL_CONSTANTS = {
    "math_e": math.e, "math_1_over_e": 1 / math.e, "math_pi": math.pi, "math_2_pi": 2 * math.pi,
    "math_1_over_pi": 1 / math.pi, "math_pi_over_2": math.pi / 2, "math_pi_over_3": math.pi / 3,
    "math_pi_over_4": math.pi / 4, "math_3_pi_over_2": 3 * math.pi / 2, "math_log_of_2": math.log(2),
    "math_log_of_10": math.log(10), "math_log2_of_e": 1 / math.log(2), "math_log10_of_e": 1 / math.log(10),
    "math_sqrt_2": math.sqrt(2), "math_1_over_sqrt_2": 1 / math.sqrt(2), "math_sqrt_pi": math.sqrt(math.pi),
    "math_deg_to_rad": math.pi / 180, "math_rad_to_deg": 180 / math.pi,
}


def _round_away(x):
    return float(math.floor(x + 0.5)) if x >= 0 else -float(math.floor(-x + 0.5))


def _log(x, base=None):
    return math.log(x) if base is None else math.log(x) / math.log(base)


def _arctan(y, x=None):
    return math.atan(y) if x is None else math.atan2(y, x)


def _pow(a, b):
    return float(a) ** float(b)


#: constant-evaluable math_real functions (name -> callable); ``uniform`` and friends are not constant
MATH_REAL_FUNCTIONS = {
    "sign": lambda x: float((x > 0) - (x < 0)), "ceil": lambda x: float(math.ceil(x)),
    "floor": lambda x: float(math.floor(x)), "round": _round_away, "trunc": lambda x: float(math.trunc(x)),
    "realmax": max, "realmin": min, "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "exp": math.exp, "log": _log, "log2": math.log2, "log10": math.log10, "**": _pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "arcsin": math.asin, "arccos": math.acos,
    "arctan": _arctan, "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "arcsinh": math.asinh, "arccosh": math.acosh, "arctanh": math.atanh,
}


def eval_math_real(name, *args):
    """Evaluate a math_real function on constant arguments; None when unknown or out of domain."""
    f = MATH_REAL_FUNCTIONS.get(name.lower())
    if f is None:
        return None
    try:
        return f(*[float(a) for a in args])
    except (ValueError, OverflowError, TypeError, ZeroDivisionError):
        return None


# ------------------------------------------------------------------ width-rule table (numeric_std)
#: (package, operator or function, operand classes, widths of operands, constant args, expected result class, expected width)
#: The same rows are asserted by tests/test_vhdl_ieee.py.
WIDTH_RULE_TABLE = (
    ("numeric_std", "+", ("u", "u"), (8, 4), (), "u", 8),
    ("numeric_std", "-", ("u", "u"), (4, 8), (), "u", 8),
    ("numeric_std", "+", ("u", "nat"), (8, None), (), "u", 8),
    ("numeric_std", "+", ("nat", "u"), (None, 6), (), "u", 6),
    ("numeric_std", "+", ("s", "s"), (5, 9), (), "s", 9),
    ("numeric_std", "+", ("s", "int"), (12, None), (), "s", 12),
    ("numeric_std", "*", ("u", "u"), (8, 4), (), "u", 12),
    ("numeric_std", "*", ("s", "s"), (8, 8), (), "s", 16),
    ("numeric_std", "*", ("u", "nat"), (8, None), (), "u", 16),
    ("numeric_std", "*", ("nat", "u"), (None, 5), (), "u", 10),
    ("numeric_std", "/", ("u", "u"), (8, 4), (), "u", 8),
    ("numeric_std", "/", ("u", "nat"), (8, None), (), "u", 8),
    ("numeric_std", "/", ("nat", "u"), (None, 4), (), "u", 4),
    ("numeric_std", "mod", ("u", "u"), (8, 4), (), "u", 4),
    ("numeric_std", "rem", ("s", "s"), (8, 4), (), "s", 4),
    ("numeric_std", "mod", ("u", "nat"), (8, None), (), "u", 8),
    ("numeric_std", "-", ("s",), (7,), (), "s", 7),
    ("numeric_std", "abs", ("s",), (7,), (), "s", 7),
    ("numeric_std", "and", ("u", "u"), (8, 8), (), "u", 8),
    ("numeric_std", "not", ("s",), (6,), (), "s", 6),
    ("numeric_std", "shift_left", ("u", "nat"), (8, None), (), "u", 8),
    ("numeric_std", "rotate_right", ("s", "nat"), (8, None), (), "s", 8),
    ("numeric_std", "sll", ("u", "int"), (8, None), (), "u", 8),
    ("numeric_std", "resize", ("u", "nat"), (8, None), (12, 16), "u", 16),
    ("numeric_std", "resize", ("s", "nat"), (8, None), (None, 4), "s", 4),
    ("numeric_std", "to_unsigned", ("nat", "nat"), (None, None), (5, 10), "u", 10),
    ("numeric_std", "to_signed", ("int", "nat"), (None, None), (-5, 9), "s", 9),
    ("numeric_std", "to_integer", ("u",), (8,), (), "nat", None),
    ("numeric_std", "to_integer", ("s",), (8,), (), "int", None),
    ("numeric_std", "<", ("u", "u"), (8, 4), (), "bool", 1),
    ("numeric_std", "=", ("s", "int"), (8, None), (), "bool", 1),
    ("numeric_std", "?=", ("u", "u"), (8, 8), (), "sl", 1),
    ("numeric_std", "maximum", ("u", "u"), (4, 9), (), "u", 9),
    ("numeric_std_unsigned", "+", ("slv", "slv"), (8, 12), (), "suv", 12),
    ("numeric_std_unsigned", "+", ("slv", "nat"), (8, None), (), "suv", 8),
    ("numeric_std_unsigned", "*", ("slv", "slv"), (4, 4), (), "suv", 8),
    ("numeric_std_unsigned", "to_integer", ("slv",), (8,), (), "nat", None),
    ("std_logic_arith", "+", ("u", "u"), (8, 8), (), "u", 8),
    ("std_logic_arith", "+", ("u", "s"), (8, 8), (), "s", 9),
    ("std_logic_arith", "+", ("s", "u"), (8, 4), (), "s", 8),
    ("std_logic_arith", "*", ("u", "s"), (8, 4), (), "s", 12),
    ("std_logic_arith", "conv_std_logic_vector", ("int", "nat"), (None, None), (3, 12), "slv", 12),
    ("std_logic_unsigned", "+", ("slv", "slv"), (8, 4), (), "slv", 8),
    ("std_logic_unsigned", "+", ("slv", "int"), (8, None), (), "slv", 8),
    ("std_logic_unsigned", "*", ("slv", "slv"), (8, 4), (), "slv", 12),
    ("std_logic_signed", "conv_integer", ("slv",), (8,), (), "int", None),
    ("std_logic_1164", "and", ("slv", "slv"), (8, 8), (), "suv", 8),
)
