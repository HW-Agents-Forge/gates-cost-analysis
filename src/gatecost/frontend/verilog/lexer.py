"""Verilog/SystemVerilog lexer (FRONTEND.md section 2.3).

One compiled master regular expression iterated with ``finditer``; no per-character Python loop.

Token representation: three parallel lists (``Tokens.kinds``, ``Tokens.texts``, ``Tokens.offs``)
indexed by token number.  ``kinds[i]`` is

* the keyword text for keywords (``"module"``, ``"begin"`` ...),
* the operator/punctuation text for operators (``"<="``, ``"'{"`` ...),
* one of ``ID`` (identifiers, escaped identifiers with the backslash removed), ``SYSID``
  (``$clog2``), ``NUM`` (integer literal, any form), ``FILL`` (``'0 '1 'x 'z``), ``REAL``,
  ``TIME``, ``STR``, ``BAD`` (illegal character), ``EOF`` (sentinel, repeated 4 times).

Attributes ``(* ... *)`` are not tokens; the names from a small recorded set (``full_case``,
``parallel_case``, ``keep`` ...) are attached to the next token in ``Tokens.attrs``
(``{token index: frozenset(names)}``).  Comments are skipped.
"""
from __future__ import annotations

import re
import sys

KEYWORDS = frozenset("""
alias always always_comb always_ff always_latch and assert assign assume automatic begin bind bit break buf
bufif0 bufif1 byte case casex casez chandle checker class clocking cmos const continue cover covergroup
deassign default defparam disable do edge else end endcase endchecker endclass endclocking endfunction
endgenerate endgroup endinterface endmodule endpackage endprimitive endprogram endproperty endspecify
endsequence endtable endtask enum event expect export extern final for force foreach forever fork forkjoin
function generate genvar highz0 highz1 if iff ifnone import initial inout input inside int integer interface
join join_any join_none localparam logic longint macromodule module nand negedge nmos nor not
notif0 notif1 null or output package packed parameter pmos posedge primitive priority program property
pull0 pull1 pulldown pullup rcmos real realtime ref reg release repeat restrict return rnmos rpmos rtran
rtranif0 rtranif1 scalared sequence shortint shortreal signed specify specparam static string strong0
strong1 struct supply0 supply1 table tagged task time timeprecision timeunit tran tranif0 tranif1 tri tri0
tri1 triand trior trireg type typedef union unique unique0 unsigned uwire var vectored virtual void wait
wand weak0 weak1 while wire wor xnor xor modport with
""".split())

# SystemVerilog-only keywords that plain Verilog designs commonly use as identifiers (``reg [3:0] bit;``,
# ``.ref(x)``).  ``lex`` demotes one of them to an identifier when the context cannot be a keyword use.
SOFT_KEYWORDS = frozenset("""
bit byte shortint int longint string void chandle alias bind checker class cover covergroup do expect export
extern final inside interface modport priority program property ref restrict sequence static tagged type
unique unique0 var virtual with
""".split())
_SOFT_TYPES = frozenset("bit byte shortint int longint string void chandle".split())
# tokens that may follow an identifier but never a keyword of SOFT_KEYWORDS
_AFTER_IDENT = frozenset("""
<= = ; ) ] , . + - * / % == != === !== & | ^ ~^ ^~ && || ? : } < > >= << >> <<< >>> +: -: += -= *= /= ++ --
""".split())

RECORDED_ATTRS = frozenset(("full_case", "parallel_case", "keep", "fsm_encoding", "enum_encoding",
                            "ram_style", "rom_style", "syn_keep", "keep_hierarchy"))

_OPS = [
    "<<<=", ">>>=", "===", "!==", "==?", "!=?", "<<<", ">>>", "<<=", ">>=", "->>", "<->", "|->", "|=>",
    "+:", "-:", "::", "**", "&&", "||", "==", "!=", "<=", ">=", "<<", ">>", "~&", "~|", "~^", "^~",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "++", "--", "->", "=>", "*>", "##", ".*", ":=", ":/",
    "'{", "'(", "@@",
    "+", "-", "*", "/", "%", "&", "|", "^", "~", "!", "<", ">", "=", "?", ":", ";", ",", ".", "(", ")",
    "[", "]", "{", "}", "#", "@", "'", "$",
]
_OPS.sort(key=len, reverse=True)
OPERATORS = frozenset(_OPS)

_SKIP = r"(?:[ \t\r\n\f\v]+|//[^\n]*|/\*(?:.*?\*/|.*\Z))*+"
_CMT_SCAN = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/|(?P<open>/\*)', re.S)
_BASED = r"(?:\d[\d_]*[ \t]*)?'[sS]?[bBoOdDhH][ \t]*[0-9a-fA-FxXzZ?_]+"
_REAL = r"\d[\d_]*(?:\.\d[\d_]*(?:[eE][+-]?\d+)?|[eE][+-]?\d+)(?![\w.])"
_TIME = r"\d[\d_]*(?:\.\d[\d_]*)?(?:fs|ps|ns|us|ms|s)(?![\w])"
_MASTER = re.compile(
    _SKIP + r"(?:"
    r"(?P<ID>[A-Za-z_][A-Za-z0-9_$]*)"
    r"|(?P<NUM>" + _BASED + r")"
    r"|(?P<TIME>" + _TIME + r")"
    r"|(?P<REAL>" + _REAL + r")"
    r"|(?P<NUM2>\d[\d_]*)"
    r"|(?P<FILL>'[01xXzZ](?![\w']))"
    r"|(?P<SYSID>\$[A-Za-z0-9_$]+)"
    r"|(?P<ATTR>\(\*(?!\))(?:.*?\*\)|.*\Z))"
    r'|(?P<STR>"(?:[^"\\\n]|\\.)*(?:"|(?=\n)|\Z))'
    r"|(?P<ESC>\\\S+)"
    r"|(?P<OP>" + "|".join(re.escape(o) for o in _OPS) + r")"
    r"|(?P<END>\Z)"
    r"|(?P<BAD>\S)"
    r")",
    re.S,
)

_ATTR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


class Tokens:
    __slots__ = ("kinds", "texts", "offs", "attrs", "errors")

    def __init__(self):
        self.kinds: list[str] = []
        self.texts: list[str] = []
        self.offs: list[int] = []
        self.attrs: dict[int, frozenset] = {}
        self.errors: list[tuple[int, str]] = []   # (offset, message)

    def __len__(self):
        return len(self.kinds)

    def stream(self, n: int | None = None):
        """Debug/test helper: list of ``(kind, text)`` without the EOF sentinels."""
        k = self.kinds
        t = self.texts
        end = len(k)
        while end and k[end - 1] == "EOF":
            end -= 1
        return [(k[i], t[i]) for i in range(end)]


def lex(text: str) -> Tokens:
    toks = Tokens()
    kinds = toks.kinds
    texts = toks.texts
    offs = toks.offs
    ka = kinds.append
    ta = texts.append
    oa = offs.append
    kw = KEYWORDS
    attrs = toks.attrs
    pending_attr: set | None = None
    soft: list[int] = []
    for m in _MASTER.finditer(text):
        g = m.lastgroup
        s = m.group(g)
        if g == "ID":
            if s in kw:
                ka(s)
                if s in SOFT_KEYWORDS:
                    soft.append(len(kinds) - 1)
            else:
                ka("ID")
        elif g == "OP":
            ka(s)
        elif g == "NUM2":
            ka("NUM")
        elif g == "ATTR":
            names = {x for x in _ATTR_NAME.findall(s) if x in RECORDED_ATTRS}
            if names:
                pending_attr = (pending_attr or set()) | names
            continue
        elif g == "ESC":
            s = s[1:]
            ka("ID")
        elif g == "END":
            continue
        elif g == "BAD":
            toks.errors.append((m.end() - 1, f"illegal character {s!r}"))
            ka("BAD")
        else:
            ka(g)
        if pending_attr is not None:
            attrs[len(kinds) - 1] = frozenset(pending_attr)
            pending_attr = None
        ta(s)
        oa(m.end() - len(s))
    for i in soft:
        prev = kinds[i - 1] if i else ""
        nxt = kinds[i + 1] if i + 1 < len(kinds) else "EOF"
        k = kinds[i]
        if prev == "." or nxt in _AFTER_IDENT or (prev == "]" and k in _SOFT_TYPES):
            kinds[i] = "ID"
    n = len(text)
    i = text.rfind("/*")
    if i >= 0 and text.find("*/", i + 2) < 0:
        for m in _CMT_SCAN.finditer(text):
            if m.lastgroup == "open":
                toks.errors.append((m.start(), "unterminated block comment"))
                break
    for _ in range(4):
        ka("EOF")
        ta("")
        oa(n)
    return toks


def parse_number(text: str):
    """Decode an integer literal.

    Returns ``(size, base, signed, digits, value, based)``: ``size`` int or None; ``base`` one of
    ``b o d h`` (plain decimals are ``d`` with ``based=False``); ``signed`` (plain decimals and
    ``'s`` literals); ``digits`` lower-case without underscores; ``value`` the integer, or ``None``
    when the digits contain ``x``/``z``/``?``.  The value is *not* truncated to ``size``.
    """
    t = text.replace("_", "")
    q = t.find("'")
    if q < 0:
        digits = t
        try:
            return None, "d", True, digits, int(digits), False
        except ValueError:
            return None, "d", True, digits, None, False
    size_s = t[:q].strip()
    rest = t[q + 1:].lstrip()
    signed = False
    if rest[:1] in ("s", "S"):
        signed = True
        rest = rest[1:]
    base = rest[0].lower()
    digits = rest[1:].strip().lower()
    size = int(size_s) if size_s else None
    value = None
    if not any(c in "xz?" for c in digits):
        try:
            value = int(digits, {"b": 2, "o": 8, "d": 10, "h": 16}[base]) if digits else 0
        except ValueError:
            value = None
    return size, base, signed, digits, value, True
