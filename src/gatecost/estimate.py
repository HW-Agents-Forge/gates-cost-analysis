"""``estimate``: cost of a hypothetical construct in the compact notation (CLI.md section 3).

Public API
    ``parse_expression(text) -> list[Term]``      raises ``GatecostError(BAD_ARGUMENT)``
    ``estimate_expression(text, profile, count=1) -> EstimateResult``
    ``compare_expressions(texts, profile, count=1) -> CompareResult``
    ``estimate_rtl`` / ``RTL_ESTIMATOR``          hook for the HDL front-end (stage 4)

Grammar (keywords case-insensitive, whitespace-separated; ``+`` and ``*`` may touch their
operands)::

    expr  := item ('+' item)*
    item  := [INT '*'] ( term | '(' expr ')' )      # a multiplier binds to the next item
    term  := OP args...                              # see FORMS below
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .cost import Profile, cost_op
from .cost.acc import OpCost
from .cost.delta import confidence
from .errors import GatecostError
from .result import CompareResult, EstimateResult, Ge3

FORMS = {
    "add": "add N [const[=K]]", "sub": "sub N [const[=K]]", "neg": "neg N",
    "mul": "mul NxM [signed] [square] [trunc=T]  |  mul N const[=K]",
    "div": "div N/M [signed]  |  div N const=K [signed]", "mod": "mod N/M [signed]  |  mod N const=K [signed]",
    "shl": "shl N by S|const", "shr": "shr N by S|const", "sar": "sar N by S|const",
    "rol": "rol N by S|const", "ror": "ror N by S|const",
    "and": "and N", "or": "or N", "xor": "xor N", "not": "not N", "nand": "nand N", "nor": "nor N", "xnor": "xnor N",
    "eq": "eq N [signed] [const[=K]]", "ne": "ne N [signed] [const[=K]]", "lt": "lt N [signed] [const[=K]]",
    "le": "le N [signed] [const[=K]]", "gt": "gt N [signed] [const[=K]]", "ge": "ge N [signed] [const[=K]]",
    "red_and": "red_and N", "red_or": "red_or N", "red_xor": "red_xor N",
    "mux": "mux K:1 wW", "pmux": "pmux K wW", "priority": "priority K wW",
    "decode": "decode S", "encode": "encode K [priority]",
    "reg": "reg N [async_reset] [async_set_reset] [sync_reset] [enable] [scan]", "latch": "latch N",
    "mem": "mem DxW [rports=R] [wports=P] [sync_read|async_read]", "rom": "rom DxW",
    "sum": "sum KxW", "mac": "mac NxM [c=W] [signed]", "pow": "pow N exp=E [out=O]",
}
GRAMMAR = "sum of terms: [N *] <op> <size> [modifiers] (+ ...); ops: " + " ".join(FORMS)


@dataclass(frozen=True, slots=True)
class Term:
    count: int
    op: str
    attrs: dict
    text: str  # normalized description of one instance


def _bad(msg: str) -> GatecostError:
    return GatecostError("BAD_ARGUMENT", msg)


def _form(op: str) -> str:
    return f"expected '{FORMS[op]}'"


def _uint(tok: str, op: str, what: str, lo: int = 1) -> int:
    if not re.fullmatch(r"\d+", tok) or int(tok) < lo:
        raise _bad(f"{op}: {what} must be an integer >= {lo}, got {tok!r}; {_form(op)}")
    return int(tok)


def _pair(tok: str, sep: str, op: str) -> tuple[int, int]:
    m = re.fullmatch(rf"(\d+){re.escape(sep)}(\d+)", tok)
    if not m or int(m.group(1)) < 1 or int(m.group(2)) < 1:
        raise _bad(f"{op}: bad size {tok!r}; {_form(op)}")
    return int(m.group(1)), int(m.group(2))


def _opt_const(mods: list[str], op: str, attrs: dict) -> list[str]:
    rest = []
    for m in mods:
        if m == "const":
            attrs["const_unknown"] = True
        elif m.startswith("const="):
            try:
                attrs["const"] = int(m[6:], 0)
            except ValueError:
                raise _bad(f"{op}: bad constant {m[6:]!r} in {m!r}; {_form(op)}") from None
        else:
            rest.append(m)
    return rest


def _no_extra(rest: list[str], op: str) -> None:
    if rest:
        raise _bad(f"{op}: unexpected {' '.join(repr(r) for r in rest)}; {_form(op)}")


def _parse_term(toks: list[str]) -> tuple[str, dict, str]:
    op = toks[0]
    if op not in FORMS:
        raise _bad(f"unknown operation {op!r}; expected one of: {' '.join(FORMS)}")
    args = toks[1:]
    if not args:
        raise _bad(f"{op}: missing size; {_form(op)}")
    a: dict = {}
    U = op.upper()
    if op in ("add", "sub"):
        a["n"] = _uint(args[0], op, "width")
        _no_extra(_opt_const(args[1:], op, a), op)
        norm = f"{U} {a['n']}" + (f" const={a['const']}" if "const" in a else " const" if a.get("const_unknown") else "")
    elif op == "neg":
        a["n"] = _uint(args[0], op, "width")
        _no_extra(args[1:], op)
        norm = f"NEG {a['n']}"
    elif op == "mul":
        rest = _opt_const(args[1:], op, a)
        if re.fullmatch(r"\d+x\d+", args[0]):
            a["n"], a["m"] = _pair(args[0], "x", op)
            if "const" in a or a.get("const_unknown"):
                raise _bad(f"mul: a constant multiplier takes one width; {_form(op)}")
            for m in rest:
                if m == "signed":
                    a["signed"] = True
                elif m == "square":
                    a["square"] = True
                elif m.startswith("trunc="):
                    a["trunc"] = _uint(m[6:], op, "trunc", 1)
                else:
                    raise _bad(f"mul: unexpected {m!r}; {_form(op)}")
            if a.get("square") and a["n"] != a["m"]:
                raise _bad(f"mul: square needs equal widths; {_form(op)}")
            norm = f"MUL {a['n']}x{a['m']} {'signed' if a.get('signed') else 'unsigned'}"
            norm += " square" if a.get("square") else ""
            norm += f" trunc={a['trunc']}" if "trunc" in a else ""
        else:
            a["n"] = _uint(args[0], op, "width")
            if not ("const" in a or a.get("const_unknown")):
                raise _bad(f"mul: bad size {args[0]!r}; {_form(op)}")
            _no_extra(rest, op)
            norm = f"MUL {a['n']} const" + (f"={a['const']}" if "const" in a else "")
    elif op in ("div", "mod"):
        rest = _opt_const(args[1:], op, a)
        if "/" in args[0]:
            a["n"], a["m"] = _pair(args[0], "/", op)
            if "const" in a or a.get("const_unknown"):
                raise _bad(f"{op}: use '{op} N const=K' for a constant divisor; {_form(op)}")
        else:
            a["n"] = _uint(args[0], op, "width")
            if "const" not in a:
                raise _bad(f"{op}: bad size {args[0]!r}; {_form(op)}")
            if a["const"] < 1:
                raise _bad(f"{op}: the constant divisor must be >= 1; {_form(op)}")
        for m in rest:
            if m == "signed":
                a["signed"] = True
            else:
                raise _bad(f"{op}: unexpected {m!r}; {_form(op)}")
        size = f"{a['n']}/{a['m']}" if "m" in a else f"{a['n']} const={a['const']}"
        norm = f"{U} {size} {'signed' if a.get('signed') else 'unsigned'}"
    elif op in ("shl", "shr", "sar", "rol", "ror"):
        a["n"] = _uint(args[0], op, "width")
        if len(args) < 3 or args[1] != "by":
            raise _bad(f"{op}: missing 'by'; {_form(op)}")
        if args[2] == "const":
            a["const_amount"] = True
            a["s"] = 0
            by = "const"
        else:
            a["s"] = _uint(args[2], op, "amount width", 0)
            by = str(a["s"])
        for m in args[3:]:
            if m == "lhs_one" and op == "shl":
                a["lhs_one"] = True
            else:
                raise _bad(f"{op}: unexpected {m!r}; {_form(op)}")
        norm = f"{U} {a['n']} by {by}" + (" lhs_one" if a.get("lhs_one") else "")
    elif op in ("and", "or", "xor", "not", "nand", "nor", "xnor", "red_and", "red_or", "red_xor"):
        a["n"] = _uint(args[0], op, "width")
        _no_extra(args[1:], op)
        norm = f"{U} {a['n']}"
    elif op in ("eq", "ne", "lt", "le", "gt", "ge"):
        a["n"] = _uint(args[0], op, "width")
        rest = _opt_const(args[1:], op, a)
        for m in rest:
            if m == "signed":
                a["signed"] = True
            else:
                raise _bad(f"{op}: unexpected {m!r}; {_form(op)}")
        norm = f"{U} {a['n']}" + (" signed" if a.get("signed") else "")
        norm += f" const={a['const']}" if "const" in a else " const" if a.get("const_unknown") else ""
    elif op in ("mux", "pmux", "priority"):
        if op == "mux":
            m = re.fullmatch(r"(\d+):1", args[0])
            if not m or int(m.group(1)) < 1:
                raise _bad(f"mux: bad input count {args[0]!r}; {_form(op)}")
            k = int(m.group(1))
        else:
            k = _uint(args[0], op, "input count")
        if len(args) != 2 or not re.fullmatch(r"w\d+", args[1]) or int(args[1][1:]) < 1:
            raise _bad(f"{op}: missing width 'wW'; {_form(op)}")
        a["k"], a["w"] = k, int(args[1][1:])
        norm = f"MUX {k}:1 w{a['w']}" if op == "mux" else f"{U} {k} w{a['w']}"
    elif op == "decode":
        a["s"] = _uint(args[0], op, "select bits")
        if a["s"] > 24:
            raise _bad(f"decode: at most 24 select bits; {_form(op)}")
        _no_extra(args[1:], op)
        norm = f"DECODE {a['s']}"
    elif op == "encode":
        a["k"] = _uint(args[0], op, "input count")
        for m in args[1:]:
            if m == "priority":
                a["priority"] = True
            else:
                raise _bad(f"encode: unexpected {m!r}; {_form(op)}")
        norm = f"ENCODE {a['k']}" + (" priority" if a.get("priority") else "")
    elif op == "reg":
        a["n"] = _uint(args[0], op, "width")
        flags = []
        for m in args[1:]:
            if m not in ("async_reset", "async_set_reset", "sync_reset", "enable", "scan"):
                raise _bad(f"reg: unexpected {m!r}; {_form(op)}")
            a[m] = True
            flags.append(m)
        norm = " ".join(["REG", str(a["n"]), *flags])
    elif op == "latch":
        a["n"] = _uint(args[0], op, "width")
        _no_extra(args[1:], op)
        norm = f"LATCH {a['n']}"
    elif op in ("mem", "rom"):
        a["depth"], a["width"] = _pair(args[0], "x", op)
        if op == "rom":
            _no_extra(args[1:], op)
            norm = f"ROM {a['depth']}x{a['width']}"
        else:
            for m in args[1:]:
                if m.startswith("rports="):
                    a["rports"] = _uint(m[7:], op, "rports")
                elif m.startswith("wports="):
                    a["wports"] = _uint(m[7:], op, "wports")
                elif m == "sync_read":
                    a["sync_read"] = True
                elif m == "async_read":
                    a["sync_read"] = False
                else:
                    raise _bad(f"mem: unexpected {m!r}; {_form(op)}")
            norm = f"MEM {a['depth']}x{a['width']} rports={a.get('rports', 1)} wports={a.get('wports', 1)} " + (
                "sync_read" if a.get("sync_read") else "async_read")
    elif op == "sum":
        a["k"], a["w"] = _pair(args[0], "x", op)
        _no_extra(args[1:], op)
        norm = f"SUM {a['k']}x{a['w']}"
    elif op == "mac":
        a["n"], a["m"] = _pair(args[0], "x", op)
        for m in args[1:]:
            if m == "signed":
                a["signed"] = True
            elif m.startswith("c="):
                a["c"] = _uint(m[2:], op, "addend width")
            else:
                raise _bad(f"mac: unexpected {m!r}; {_form(op)}")
        norm = f"MAC {a['n']}x{a['m']} {'signed' if a.get('signed') else 'unsigned'}" + (f" c={a['c']}" if "c" in a else "")
    else:  # pow
        a["n"] = _uint(args[0], op, "width")
        for m in args[1:]:
            if m.startswith("exp="):
                a["exp"] = _uint(m[4:], op, "exponent", 0)
            elif m.startswith("out="):
                a["out"] = _uint(m[4:], op, "out")
            else:
                raise _bad(f"pow: unexpected {m!r}; {_form(op)}")
        if "exp" not in a:
            raise _bad(f"pow: missing exp=E; {_form(op)}")
        norm = f"POW {a['n']} exp={a['exp']}" + (f" out={a['out']}" if "out" in a else "")
    return U, a, norm


def parse_expression(text: str) -> list[Term]:
    """Parse the compact notation into terms (multipliers flattened into ``Term.count``)."""
    if not text or not text.strip():
        raise _bad(f"empty expression; {GRAMMAR}")
    spaced = re.sub(r"([+*()])", r" \1 ", text.strip().lower())
    toks = spaced.split()
    pos = 0

    def parse_expr(depth: int) -> list[Term]:
        nonlocal pos
        out: list[Term] = []
        while True:
            out += parse_item(depth)
            if pos < len(toks) and toks[pos] == "+":
                pos += 1
                continue
            return out

    def parse_item(depth: int) -> list[Term]:
        nonlocal pos
        count = 1
        if pos < len(toks) and re.fullmatch(r"\d+", toks[pos]) and pos + 1 < len(toks) and toks[pos + 1] == "*":
            count = int(toks[pos])
            if count < 1:
                raise _bad(f"multiplier must be >= 1, got {toks[pos]!r}")
            pos += 2
        if pos >= len(toks):
            raise _bad(f"unexpected end of expression; {GRAMMAR}")
        if toks[pos] == "(":
            pos += 1
            inner = parse_expr(depth + 1)
            if pos >= len(toks) or toks[pos] != ")":
                raise _bad("missing ')' in expression")
            pos += 1
            return [Term(count * t.count, t.op, t.attrs, t.text) for t in inner]
        start = pos
        while pos < len(toks) and toks[pos] not in ("+", ")", "*"):
            pos += 1
        if start == pos:
            raise _bad(f"unexpected {toks[pos]!r}; {GRAMMAR}")
        op, attrs, norm = _parse_term(toks[start:pos])
        return [Term(count, op, attrs, norm)]

    terms = parse_expr(0)
    if pos != len(toks):
        raise _bad(f"unexpected {toks[pos]!r} in expression; {GRAMMAR}")
    return terms


def _merge_arch(costs: list[OpCost]) -> dict:
    out = {}
    for s in ("min", "typ", "max"):
        names = list(dict.fromkeys(c.arch.get(s, "") for c in costs if c.arch.get(s)))
        out[s] = "+".join(names) if names else "wire"
    return out


def _assumptions(terms: list[Term]) -> list[str]:
    msgs = []
    for t in terms:
        if t.attrs.get("const_unknown") and t.op in ("MUL",):
            msgs.append("constant multiplier value unknown, CSD density of 1/3 assumed")
        if t.op == "MEM" and "sync_read" not in t.attrs:
            msgs.append("memory read style not given, asynchronous read assumed (flop-based)")
    return list(dict.fromkeys(msgs))


def estimate_terms(text: str, terms: list[Term], profile: Profile, count: int = 1) -> EstimateResult:
    costs: list[OpCost] = []
    for t in terms:
        try:
            costs.append(cost_op(t.op, t.attrs, profile).scaled(t.count * count))
        except ValueError as exc:
            raise _bad(f"{t.text}: {exc}") from None
    ge = Ge3(sum(c.min for c in costs), sum(c.typ for c in costs), sum(c.max for c in costs))
    bd: dict[str, int] = {}
    for c in costs:
        for k, v in c.breakdown.items():
            bd[k] = bd.get(k, 0) + v
    bd = dict(sorted(bd.items(), key=lambda kv: (-kv[1], kv[0])))
    conf = confidence(ge, [(c.category, c.quality, abs(c.typ)) for c in costs], [f for c in costs for f in c.flags])
    parts = [(f"{t.count} * " if t.count > 1 else "") + t.text for t in terms]
    norm = " + ".join(parts)
    if count > 1:
        norm = f"{count} * " + (f"({norm})" if len(terms) > 1 else norm)
    return EstimateResult("op", text, norm, ge, _merge_arch(costs), bd, conf, tuple(_assumptions(terms)))


def estimate_expression(text: str, profile: Profile, count: int = 1) -> EstimateResult:
    """Cost of a compact-notation expression, ``count`` instances of it."""
    if count < 1:
        raise _bad("--count must be >= 1")
    return estimate_terms(text, parse_expression(text), profile, count)


def compare_results(results: list[EstimateResult]) -> CompareResult:
    order = sorted(range(len(results)), key=lambda i: (results[i].ge.typ, results[i].ge.max, i))
    b, o = order[0], order[1]
    best, other = results[b].ge, results[o].ge
    overlap = best.max >= other.min and other.max >= best.min
    assumed = tuple(dict.fromkeys(a for r in results for a in r.assumed))
    return CompareResult(
        tuple(results), "typ", b, Ge3(other.min - best.min, other.typ - best.typ, other.max - best.max),
        overlap, o, assumed,
    )


def compare_expressions(texts: list[str], profile: Profile, count: int = 1) -> CompareResult:
    if len(texts) < 2:
        raise _bad("--compare needs at least two alternatives")
    return compare_results([estimate_expression(t, profile, count) for t in texts])


# -- RTL fragments (stage 4) ----------------------------------------------------------------

RTL_ESTIMATOR: Callable[[str, dict, str, Profile], EstimateResult] | None = None
"""Set by the HDL front-end stage: ``(fragment, widths, lang, profile) -> EstimateResult``."""


def estimate_rtl(fragment: str, widths: dict[str, int], lang: str, profile: Profile) -> EstimateResult:
    if RTL_ESTIMATOR is None:
        raise GatecostError("NOT_IMPLEMENTED", "estimate --rtl needs the HDL front-end, which is not available yet")
    return RTL_ESTIMATOR(fragment, widths, lang, profile)
