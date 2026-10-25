"""Top-level parameter / generic overrides (``--param NAME=VALUE``, ``[project.params]``; CLI.md section 2.1).

A value is one of: ``int`` (decimal, ``0x``/``0b``/``0o``, underscores), :class:`Lit` (a based Verilog literal
such as ``8'h1f`` with its width), ``float``, ``bool`` (``true``/``false``) or ``str``.  The same grammar serves
the command line and the string values of the project file; quote a value (``'"123"'``) to force a string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")
_INT = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*)\Z")
_REAL = re.compile(r"[+-]?(?:[0-9][0-9_]*\.[0-9_]*(?:[eE][+-]?[0-9]+)?|\.[0-9][0-9_]*(?:[eE][+-]?[0-9]+)?|"
                   r"[0-9][0-9_]*[eE][+-]?[0-9]+)\Z")
_BASED = re.compile(r"([0-9][0-9_]*)?'([sS])?([bBoOdDhH])([0-9a-fA-F_]+)\Z")
_RADIX = {"b": 2, "o": 8, "d": 10, "h": 16}


@dataclass(frozen=True, slots=True)
class Lit:
    """A sized (or unsized, 32-bit) based Verilog literal."""

    value: int
    width: int
    signed: bool
    text: str

    def __str__(self) -> str:
        return self.text


def is_name(name: str) -> bool:
    return bool(_NAME.match(name))


def parse_value(text: str):
    """The typed value of ``text``; raises ``ValueError`` with a short reason."""
    t = text.strip()
    if not t:
        raise ValueError("empty value (use \"\" for an empty string)")
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" and not _BASED.match(t):
        return t[1:-1]
    low = t.lower()
    if low in ("true", "false"):
        return low == "true"
    if _INT.match(t):
        body = t.replace("_", "")
        try:
            return int(body, 0) if body.lstrip("+-")[:2].lower() in ("0x", "0b", "0o") else int(body, 10)
        except ValueError:
            raise ValueError(f"malformed integer {text!r}") from None
    m = _BASED.match(t)
    if m:
        size, sg, rad, digits = m.groups()
        digits = digits.replace("_", "")
        if not digits:
            raise ValueError(f"malformed literal {text!r}")
        base = _RADIX[rad.lower()]
        try:
            value = int(digits, base)
        except ValueError:
            raise ValueError(f"bad digits in literal {text!r}") from None
        width = int(size.replace("_", "")) if size else 32
        if width < 1:
            raise ValueError(f"literal {text!r} has width 0")
        return Lit(value & ((1 << width) - 1), width, bool(sg), t)
    if _REAL.match(t):
        return float(t.replace("_", ""))
    return t


def parse_assignment(item: str) -> tuple[str, object]:
    """``NAME=VALUE`` -> (name, typed value); raises ``ValueError``."""
    name, sep, value = item.partition("=")
    name = name.strip()
    if not sep or not is_name(name):
        raise ValueError(f"expects NAME=VALUE with an identifier NAME, got {item!r}")
    return name, parse_value(value)


def from_toml(name: str, value):
    """Typed value of a ``[project.params]`` entry; raises ``ValueError``."""
    if not is_name(name):
        raise ValueError(f"{name!r} is not an identifier")
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return parse_value(value) if value.strip() else ""
    raise ValueError(f"{name} must be an integer, real, boolean or string")


def to_json(value):
    """JSON form of a value (a based literal appears as its text)."""
    return value.text if isinstance(value, Lit) else value


def params_json(params: dict) -> dict:
    return {k: to_json(params[k]) for k in sorted(params)}


def canon(params: dict | None) -> list:
    """Canonical, hashable-by-JSON form for cache keys and configuration comparison."""
    out = []
    for k in sorted(params or {}):
        v = params[k]
        if isinstance(v, Lit):
            out.append([k, "lit", v.value, v.width, v.signed])
        elif isinstance(v, bool):
            out.append([k, "bool", v])
        elif isinstance(v, int):
            out.append([k, "int", v])
        elif isinstance(v, float):
            out.append([k, "real", repr(v)])
        else:
            out.append([k, "str", str(v)])
    return out


def to_text(value) -> str:
    """Text that :func:`parse_value` maps back to ``value`` (snapshots)."""
    if isinstance(value, Lit):
        return value.text
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return '"' + value + '"'
    return str(value)


def differing(a: dict, b: dict) -> list[str]:
    """Names whose value differs, or that exist on one side only (sorted)."""
    ca = {e[0]: e for e in canon(a)}
    cb = {e[0]: e for e in canon(b)}
    return sorted(k for k in set(ca) | set(cb) if ca.get(k) != cb.get(k))


def for_vhdl(params: dict) -> dict:
    """Python values handed to the VHDL elaborator (a based literal is its integer value)."""
    return {k: (v.value if isinstance(v, Lit) else v) for k, v in params.items()}
