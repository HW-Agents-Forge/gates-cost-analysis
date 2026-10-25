"""Case-insensitive VHDL lexer.

One compiled master regular expression is iterated with ``match``; there is no per-character
Python loop.  Identifiers and keywords are folded to lower case, extended identifiers
(``\\Foo\\``) are kept verbatim.  Synthesis pragma regions (``translate_off`` / ``translate_on``)
are dropped here; the dropped line ranges are returned so the caller can report them.

Token kinds (``Token.kind``):

``id``      basic identifier, ``val`` lower-cased
``xid``     extended identifier, ``val`` verbatim including the backslashes
``kw``      reserved word, ``val`` lower-cased (interned)
``int``     decimal integer, ``val`` is a Python int
``real``    real literal, ``val`` is a float
``based``   based literal ``16#FF#`` : ``val`` is ``(base, digits_text_upper, exponent, is_real)``
            and ``num`` holds the evaluated int/float
``char``    character literal ``'x'``, ``val`` is the single character (case preserved)
``str``     string literal, ``val`` the text with ``""`` collapsed
``bitstr``  bit-string literal, ``val`` is ``BitStr(size, base, sign, digits)``
``op``      delimiter / operator, ``val`` the text
``error``   an unrecognized character or unterminated literal, ``val`` a message
``eof``     end of input
"""
from __future__ import annotations

import bisect
import re
import sys
from collections import namedtuple

BitStr = namedtuple("BitStr", "size base sign digits")

KEYWORDS = frozenset(sys.intern(k) for k in """
abs access after alias all and architecture array assert attribute begin block body buffer bus
case component configuration constant disconnect downto else elsif end entity exit file for
function generate generic group guarded if impure in inertial inout is label library linkage
literal loop map mod nand new next nor not null of on open or others out package port postponed
procedure process protected pure range record register reject rem report return rol ror select
severity shared signal sla sll sra srl subtype then to transport type unaffected units until use variable
wait when while with xnor xor
""".split())


class Token:
    __slots__ = ("kind", "val", "line", "col", "num")

    def __init__(self, kind, val, line, col, num=None):
        self.kind = kind
        self.val = val
        self.line = line
        self.col = col
        self.num = num

    def __repr__(self):
        return f"Token({self.kind},{self.val!r},{self.line}:{self.col})"


_OPS = (r"\?/=|\?<=|\?>=|\?=|\?<|\?>|\?\?|=>|<=|>=|/=|:=|\*\*|<>|<<|>>"
        r"|[&'()*+,\-./:;<=>|\[\]?@^!#~{}%$]")

_MASTER = re.compile(
    r"""
 \s*(?:
 (?P<lcom>--[^\n]*|//[^\n]*)
|(?P<bcom>/\*.*?(?:\*/|\Z))
|(?P<bits>(?:\d[\d_]*)?(?:[uUsS]?[bBoOxX]|[dD])"[^"\n]*")
|(?P<based>\d[\d_]*\#[0-9a-zA-Z_]+(?:\.[0-9a-zA-Z_]+)?\#(?:[eE][+-]?\d+)?)
|(?P<real>\d[\d_]*\.\d[\d_]*(?:[eE][+-]?\d+)?)
|(?P<int>\d[\d_]*(?:[eE]\+?\d+)?)
|(?P<id>[A-Za-z][A-Za-z0-9_]*)
|(?P<xid>\\(?:[^\\\n]|\\\\)+\\)
|(?P<str>"(?:[^"\n]|"")*")
|(?P<char>'[^\n]')
|(?P<op>""" + _OPS + r""")
|(?P<end>\Z)
|(?P<bad>.)
)
""", re.VERBOSE | re.DOTALL)

_PRAGMA = re.compile(
    r"--\s*(?:synopsys|pragma|synthesis|rtl_synthesis|xilinx|altera|synplify)\s+"
    r"(?:translate_(off|on)|(off|on)\b)", re.IGNORECASE)
_PRAGMA_BLOCK = re.compile(r"/\*\s*(?:synopsys|pragma|synthesis)\s+translate_(off|on)", re.IGNORECASE)

_BITHEAD = re.compile(r"(\d[\d_]*)?([uUsS])?([bBoOxX]|[dD])$")
_NAMELIKE_OPS = {")", "]"}


def _based_value(text: str):
    t = text.replace("_", "")
    base_s, rest = t.split("#", 1)
    body, tail = rest.split("#", 1)
    base = int(base_s)
    exp = int(tail[1:]) if tail else 0
    is_real = "." in body
    try:
        if is_real:
            ip, fp = body.split(".")
            val = int(ip or "0", base) + (int(fp, base) / base ** len(fp) if fp else 0)
            val *= base ** exp
        else:
            val = int(body, base) * base ** exp if exp >= 0 else int(body, base) / base ** -exp
            if exp < 0 and val == int(val):
                val = int(val)
    except (ValueError, OverflowError):
        return (base, body.upper(), exp, is_real), None
    return (base, body.upper(), exp, is_real), val


def lex(text: str):
    """Tokenize ``text``.

    Returns ``(tokens, ignored, errors)``: ``tokens`` ends with an ``eof`` token; ``ignored`` is a
    list of ``(first_line, last_line)`` pragma-excluded regions; ``errors`` a list of
    ``(line, col, message)`` lexical errors (also present as ``error`` tokens).
    """
    tokens: list[Token] = []
    append = tokens.append
    ignored: list[tuple[int, int]] = []
    errors: list[tuple[int, int, str]] = []
    match = _MASTER.match
    n = len(text)
    pos = 0
    line = 1
    line_start = 0
    off_line = 0  # >0: inside a translate_off region starting at that line
    prev_kind = ""
    prev_val = ""
    intern = sys.intern
    while pos < n:
        m = match(text, pos)
        kind = m.lastgroup
        end = m.end()
        ts = m.start(m.lastindex)          # the leading white space is part of the match
        if ts != pos:
            nl = text.count("\n", pos, ts)
            if nl:
                line += nl
                line_start = text.rfind("\n", pos, ts) + 1
            pos = ts
        if kind == "end":
            break
        col = pos - line_start + 1
        if kind == "lcom":
            pm = _PRAGMA.match(text, pos)
            if pm:
                which = (pm.group(1) or pm.group(2)).lower()
                if which == "off" and not off_line:
                    off_line = line
                elif which == "on" and off_line:
                    ignored.append((off_line, line))
                    off_line = 0
            pos = end
            continue
        if kind == "bcom":
            if end - pos < 4 or text[end - 2:end] != "*/":
                errors.append((line, col, "unterminated block comment"))
            pm = _PRAGMA_BLOCK.match(text, pos)
            if pm:
                which = pm.group(1).lower()
                if which == "off" and not off_line:
                    off_line = line
                elif which == "on" and off_line:
                    ignored.append((off_line, line))
                    off_line = 0
            nl = text.count("\n", pos, end)
            if nl:
                line += nl
                line_start = text.rfind("\n", pos, end) + 1
            pos = end
            continue
        tline = line
        if off_line:
            pos = end
            continue
        raw = text[pos:end]
        if kind == "id":
            low = raw.lower()
            if low in KEYWORDS:
                tok = Token("kw", intern(low), tline, col)
                prev_kind, prev_val = "kw", low
            else:
                tok = Token("id", intern(low), tline, col)
                prev_kind, prev_val = "id", low
        elif kind == "op":
            tok = Token("op", intern(raw), tline, col)
            prev_kind, prev_val = "op", raw
        elif kind == "int":
            t = raw.replace("_", "")
            try:
                if "e" in t or "E" in t:
                    mant, ex = re.split("[eE]", t)
                    v = int(mant) * 10 ** int(ex)
                else:
                    v = int(t)
            except (ValueError, OverflowError):
                v = 0
            tok = Token("int", v, tline, col)
            prev_kind, prev_val = "int", ""
        elif kind == "real":
            try:
                v = float(raw.replace("_", ""))
            except ValueError:
                v = 0.0
            tok = Token("real", v, tline, col)
            prev_kind, prev_val = "real", ""
        elif kind == "char":
            if (prev_kind in ("id", "xid", "str") or (prev_kind == "op" and prev_val in _NAMELIKE_OPS)
                    or (prev_kind == "kw" and prev_val == "all")):
                # attribute tick / qualified expression tick, not a character literal
                tok = Token("op", "'", tline, col)
                end = pos + 1
                prev_kind, prev_val = "op", "'"
            else:
                tok = Token("char", raw[1], tline, col)
                prev_kind, prev_val = "char", ""
        elif kind == "str":
            tok = Token("str", raw[1:-1].replace('""', '"'), tline, col)
            prev_kind, prev_val = "str", ""
        elif kind == "bits":
            q = raw.index('"')
            body = raw[q + 1:-1]
            hm = _BITHEAD.match(raw[:q])
            size_s, sign, spec = hm.group(1), hm.group(2), hm.group(3).lower()
            sign = sign.lower() if sign else None
            size = int(size_s.replace("_", "")) if size_s else None
            digits = body.replace("_", "").upper()
            tok = Token("bitstr", BitStr(size, spec, sign, digits), tline, col)
            prev_kind, prev_val = "bitstr", ""
        elif kind == "based":
            val, num = _based_value(raw)
            tok = Token("based", val, tline, col, num)
            prev_kind, prev_val = "based", ""
        elif kind == "xid":
            tok = Token("xid", raw, tline, col)
            prev_kind, prev_val = "xid", ""
        else:  # bad
            msg = "unexpected character %r" % raw
            if raw == '"':
                msg = "unterminated string literal"
            errors.append((tline, col, msg))
            tok = Token("error", msg, tline, col)
            prev_kind, prev_val = "error", ""
        append(tok)
        pos = end
    if off_line:
        ignored.append((off_line, line))
    append(Token("eof", "<eof>", line, pos - line_start + 1))
    return tokens, ignored, errors
