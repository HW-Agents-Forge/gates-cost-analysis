"""Recursive-descent / Pratt parser for the synthesizable Verilog/SystemVerilog subset
(FRONTEND.md section 2.4) with error recovery by synchronization.

The parser works on the output of :mod:`.preproc` and :mod:`.lexer` for one file.  It never raises:
errors become ``PARSE_ERROR`` diagnostics and the affected statement/item/module is skipped (see
``sync_stmt``/``sync_item``); a module with any recovered error is marked ``incomplete``.
"""
from __future__ import annotations

from bisect import bisect_left

from gatecost.frontend.common.diagnostics import DiagSink
from . import ast as A
from .lexer import Tokens, parse_number
from .limits import Limits

INT_VEC = frozenset(("logic", "bit", "reg"))
INT_ATOM = frozenset(("byte", "shortint", "int", "longint", "integer", "time"))
OTHER_TYPES = frozenset(("real", "shortreal", "realtime", "string", "event", "chandle", "void"))
NET_TYPES = frozenset(("wire", "tri", "tri0", "tri1", "wand", "wor", "triand", "trior", "trireg",
                       "supply0", "supply1", "uwire"))
TYPE_KW = INT_VEC | INT_ATOM | OTHER_TYPES | frozenset(("enum", "struct", "union"))
DECL_PREFIX = frozenset(("const", "var", "static", "automatic"))
DIRS = frozenset(("input", "output", "inout", "ref"))
STRENGTH = frozenset(("supply0", "supply1", "strong0", "strong1", "pull0", "pull1", "weak0", "weak1",
                      "highz0", "highz1"))
GATES = frozenset(("and", "nand", "or", "nor", "xor", "xnor", "buf", "not", "bufif0", "bufif1", "notif0",
                   "notif1", "nmos", "pmos", "rnmos", "rpmos", "cmos", "rcmos", "tran", "rtran", "tranif0",
                   "tranif1", "rtranif0", "rtranif1", "pullup", "pulldown"))
ELAB_TASKS = frozenset(("$error", "$warning", "$info", "$fatal"))     # IEEE 1800 20.11, legal as generate items
ASSIGN_OPS = frozenset(("=", "<=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=", "<<<=", ">>>="))
UNARY = frozenset(("+", "-", "!", "~", "&", "|", "^", "~&", "~|", "~^", "^~"))
BINPREC = {
    "||": 3, "&&": 4, "|": 5, "^": 6, "~^": 6, "^~": 6, "&": 7,
    "==": 8, "!=": 8, "===": 8, "!==": 8, "==?": 8, "!=?": 8,
    "<": 9, "<=": 9, ">": 9, ">=": 9, "<<": 10, ">>": 10, "<<<": 10, ">>>": 10,
    "+": 11, "-": 11, "*": 12, "/": 12, "%": 12, "**": 13,
}
CALLABLE = (A.Ident, A.ScopedName, A.Member)
ITEM_STOP = frozenset(("EOF", "endmodule", "endinterface", "endpackage", "endprogram", "module", "macromodule",
                       "package", "primitive", "endgenerate"))
STRAY_CLOSERS = frozenset(("endgenerate", "end", "endcase", "endfunction", "endtask", "join", "join_any",
                           "join_none", "endclass", "endgroup", "endproperty", "endsequence", "endclocking"))
HARD_END = frozenset(("EOF", "endmodule", "endinterface", "endpackage", "endprogram", "module", "macromodule",
                      "package", "primitive", "endfunction", "endtask", "endgenerate"))
STMT_BARRIER = frozenset(("end", "endcase", "join", "join_any", "join_none")) | HARD_END
STMT_KW = frozenset(("if", "for", "while", "case", "casez", "casex", "repeat", "forever", "foreach", "do",
                     "return", "unique", "unique0", "priority", "always", "always_ff", "always_comb",
                     "always_latch", "assign"))
ITEM_START = frozenset(("assign", "always", "always_comb", "always_ff", "always_latch", "initial", "final",
                        "function", "task", "generate", "genvar", "defparam", "parameter", "localparam",
                        "typedef", "specify", "input", "output", "inout", "wire", "reg", "logic"))
OPENERS = frozenset(("begin", "case", "casez", "casex", "fork", "function", "task", "generate"))
CLOSER_OF = {"begin": "end", "case": "endcase", "casez": "endcase", "casex": "endcase", "fork": "join",
             "function": "endfunction", "task": "endtask", "generate": "endgenerate"}
NEST_CLOSERS = frozenset(("end", "endcase", "join", "join_any", "join_none", "endfunction", "endtask",
                          "endgenerate"))
SKIP_BALANCED = {
    "class": "endclass", "program": "endprogram", "covergroup": "endgroup", "property": "endproperty",
    "sequence": "endsequence", "clocking": "endclocking", "checker": "endchecker", "specify": "endspecify",
    "primitive": "endprimitive",
}
ASSERT_KW = frozenset(("assert", "assume", "cover", "restrict", "expect"))


class ParseError(Exception):
    def __init__(self, idx: int, msg: str):
        Exception.__init__(self, msg)
        self.idx = idx
        self.msg = msg


class ParsedUnit:
    __slots__ = ("modules", "packages", "interfaces", "unit_items", "udps")

    def __init__(self):
        self.modules: list = []
        self.packages: list = []
        self.interfaces: list = []
        self.unit_items: list = []
        self.udps: list = []


class Parser:
    def __init__(self, text: str, toks: Tokens, files: list, lines: list, nettype: list,
                 sink: DiagSink, limits: Limits | None = None, default_file: str = "<string>"):
        self.text = text
        self.kinds = toks.kinds
        self.texts = toks.texts
        self.offs = toks.offs
        self.attrs = toks.attrs
        self.nl = [i for i, c in enumerate(text) if c == "\n"] if "\n" in text else []
        if self.nl:
            # faster than a Python loop for large inputs
            import re
            self.nl = [m.start() for m in re.finditer("\n", text)]
        self.lf = files
        self.ll = lines
        self.nettype = nettype
        self.sink = sink
        self.limits = limits or Limits()
        self.default_file = default_file
        self.i = 0
        self.k = self.kinds[0]
        self.last_err = -1
        self.nerr = 0
        self.ns = 0              # counter of non-synthesizable statements seen
        self.depth = 0
        self.unit = ParsedUnit()
        self.lex_err = set()
        for off, msg in toks.errors:
            if msg == "unterminated block comment":
                ol = bisect_left(self.nl, off)
                col = off - self.nl[ol - 1] if ol else off + 1
                sink.add("PREPROC_ERROR", msg, self.lf[ol], self.ll[ol], col)
                continue
            idx = self._tok_at_offset(off)
            self.lex_err.add(idx)
            self._diag("PARSE_ERROR", msg, idx)
            self.nerr += 1

    # ------------------------------------------------------------------ utilities

    def _tok_at_offset(self, off):
        from bisect import bisect_left as bl
        return min(bl(self.offs, off), len(self.offs) - 1)

    def loc(self, i):
        off = self.offs[i]
        nl = self.nl
        ol = bisect_left(nl, off)
        col = off - nl[ol - 1] if ol else off + 1
        return (self.lf[ol], self.ll[ol], col)

    def _diag(self, code, msg, idx, severity=None):
        f, l, c = self.loc(idx)
        self.sink.add(code, msg, f, l, c, severity)

    def info(self, idx, msg):
        self._diag("IGNORED_REGION", msg, idx)

    def unsupported(self, idx, msg):
        self._diag("UNSUPPORTED_CONSTRUCT", msg, idx)

    def report(self, e: ParseError):
        self.nerr += 1
        if e.idx != self.last_err and e.idx not in self.lex_err:
            self.last_err = e.idx
            self._diag("PARSE_ERROR", e.msg, e.idx)

    def desc(self, i=None):
        i = self.i if i is None else i
        return "end of file" if self.kinds[i] == "EOF" else f"'{self.texts[i]}'"

    def adv(self):
        i = self.i + 1
        self.i = i
        self.k = self.kinds[i]

    def err(self, msg, idx=None):
        return ParseError(self.i if idx is None else idx, msg)

    def expect(self, kind):
        if self.k == kind:
            i = self.i + 1
            self.i = i
            self.k = self.kinds[i]
            return
        raise ParseError(self.i, f"expected '{kind}', found {self.desc()}")

    def accept(self, kind):
        if self.k == kind:
            i = self.i + 1
            self.i = i
            self.k = self.kinds[i]
            return True
        return False

    def ident(self):
        if self.k == "ID":
            s = self.texts[self.i]
            i = self.i + 1
            self.i = i
            self.k = self.kinds[i]
            return s
        raise ParseError(self.i, f"expected identifier, found {self.desc()}")

    def expect_end_label(self):
        if self.k == ":":
            self.adv()
            if self.k == "ID":
                self.adv()

    # ------------------------------------------------------------------ recovery

    def _open_parens(self, start):
        """Number of '(' left unclosed in the tokens consumed since ``start``."""
        if start is None:
            return 0
        d = 0
        kinds = self.kinds
        for j in range(start, self.i):
            kk = kinds[j]
            if kk == "(" or kk == "'(":
                d += 1
            elif kk == ")":
                d -= 1
        return d if d > 0 else 0

    def sync_stmt(self, start=None):
        """Skip to the end of the current statement (see FRONTEND.md section 2.4)."""
        i0 = self.i
        depth = self._open_parens(start)
        nest = 0
        while True:
            k = self.k
            if depth > 0 and (k == "begin" or k in STMT_BARRIER):
                depth = 0
            if k in NEST_CLOSERS and nest > 0 and k in ("end", "endcase", "join", "join_any", "join_none"):
                nest -= 1
                self.adv()
                if nest == 0 and depth <= 0:
                    if self.k == "else":
                        self.adv()
                        i0 = self.i
                        continue
                    return
                continue
            if k in STMT_BARRIER:
                return
            if k == "begin" or k == "fork" or k == "case" or k == "casez" or k == "casex":
                nest += 1
            elif k == "(" or k == "'(":
                depth += 1
            elif k == ")":
                depth -= 1
            elif k == ";" and depth <= 0 and nest == 0:
                self.adv()
                if self.k == "else":
                    self.adv()
                    continue
                return
            elif k in STMT_KW and nest == 0 and depth <= 0 and self.i > i0:
                return
            self.adv()

    def sync_item(self, start):
        kinds = self.kinds
        closer = CLOSER_OF.get(kinds[start])
        nest = 0
        opener = kinds[start]
        depth = self._open_parens(start) if closer is None else 0
        while True:
            k = self.k
            if k in ITEM_STOP and k != "endgenerate":
                return
            if depth > 0 and (k == "begin" or k == "end" or k == "endgenerate" or (self.i > start and k in ITEM_START)):
                depth = 0
            if closer is not None:
                # started with a block-structured item: skip to its matching closer
                if k == opener or (opener in ("case", "casez", "casex") and k in ("case", "casez", "casex")):
                    nest += 1
                elif k == closer:
                    if nest == 0:
                        self.adv()
                        return
                    nest -= 1
                elif k == "endgenerate" and closer != "endgenerate":
                    return
                self.adv()
                continue
            if k == "begin" or k == "fork" or k == "case" or k == "casez" or k == "casex":
                nest += 1
            elif k in ("end", "endcase", "join", "join_any", "join_none"):
                if nest == 0:
                    return
                nest -= 1
                self.adv()
                if nest == 0:
                    return
                continue
            elif k == "endgenerate":
                if nest == 0:
                    return
            elif k == "(":
                depth += 1
            elif k == ")":
                depth -= 1
            elif k == ";" and nest == 0 and depth <= 0:
                self.adv()
                return
            elif nest == 0 and depth <= 0 and self.i > start and k in ITEM_START:
                return
            self.adv()

    def skip_statement(self):
        depth = 0
        nest = 0
        while True:
            k = self.k
            if k in HARD_END:
                return
            if k == "begin" or k == "fork" or k == "case" or k == "casez" or k == "casex":
                nest += 1
            elif k in ("end", "endcase", "join", "join_any", "join_none"):
                nest -= 1
                if nest <= 0 and depth <= 0:
                    self.adv()
                    if self.k == "else":
                        self.adv()
                        self.skip_statement()
                    return
            elif k in ("(", "[", "{", "'{", "'("):
                depth += 1
            elif k in (")", "]", "}"):
                depth -= 1
            elif k == ";" and depth <= 0 and nest <= 0:
                self.adv()
                if self.k == "else":
                    self.adv()
                    self.skip_statement()
                return
            self.adv()

    def skip_to_semi(self):
        depth = 0
        while True:
            k = self.k
            if k in HARD_END:
                return
            if k in ("(", "[", "{", "'{", "'("):
                depth += 1
            elif k in (")", "]", "}"):
                depth -= 1
            elif k == ";" and depth <= 0:
                self.adv()
                return
            self.adv()

    def skip_balanced(self, opener, closer):
        nest = 0
        while True:
            k = self.k
            if k == opener:
                nest += 1
            elif k == closer:
                nest -= 1
                if nest <= 0:
                    self.adv()
                    self.expect_end_label()
                    return
            elif k == "EOF" or (k in HARD_END and k not in ("endfunction", "endtask", "endgenerate")):
                return
            self.adv()

    def skip_parens(self):
        """At '(' : skip the balanced parenthesised group."""
        depth = 0
        while True:
            k = self.k
            if k == "EOF":
                return
            if k == "(":
                depth += 1
            elif k == ")":
                depth -= 1
                if depth == 0:
                    self.adv()
                    return
            elif k == ";" and depth <= 0:
                return
            self.adv()

    def skip_delay(self):
        """At '#': skip a delay value."""
        self.adv()
        k = self.k
        if k == "(":
            self.skip_parens()
        elif k in ("NUM", "REAL", "TIME", "ID"):
            self.adv()
            if self.k == "::" and self.kinds[self.i + 1] == "ID":
                self.adv()
                self.adv()

    # ------------------------------------------------------------------ expressions

    def parse_expr(self, minp=0):
        left = self.parse_unary()
        kinds = self.kinds
        while True:
            k = self.k
            p = BINPREC.get(k)
            if p is not None:
                if p < minp:
                    break
                self.adv()
                right = self.parse_expr(p + 1)
                left = A.Binary(left.loc, k, left, right)
                continue
            if k == "?":
                if minp > 2:
                    break
                self.adv()
                then = self.parse_expr(0)
                self.expect(":")
                els = self.parse_expr(2)
                left = A.Ternary(left.loc, left, then, els)
                continue
            if k == "inside":
                if minp > 9:
                    break
                self.adv()
                self.expect("{")
                items = self.parse_range_list("}")
                self.expect("}")
                left = A.Inside(left.loc, left, tuple(items))
                continue
            break
        return left

    def parse_range_list(self, closer):
        items = []
        if self.k == closer:
            return items
        while True:
            if self.k == "[":
                i = self.i
                self.adv()
                lo = self.parse_expr()
                self.expect(":")
                hi = self.parse_expr()
                self.expect("]")
                items.append(A.ValueRange(self.loc(i), lo, hi))
            else:
                items.append(self.parse_expr())
            if not self.accept(","):
                break
        return items

    def parse_unary(self):
        k = self.k
        if k in UNARY:
            ops = []
            while self.k in UNARY:
                ops.append((self.k, self.i))
                self.adv()
                if len(ops) > self.limits.expr_depth:
                    raise ParseError(self.i, "expression nesting too deep")
            e = self.parse_primary()
            for op, i in reversed(ops):
                e = A.Unary(self.loc(i), op, e)
            return e
        return self.parse_primary()

    def parse_primary(self):
        k = self.k
        i = self.i
        texts = self.texts
        if k == "ID":
            self.adv()
            e = A.Ident(self.loc(i), texts[i])
            if self.k == "::" and self.kinds[self.i + 1] == "ID":
                scope = texts[i]
                while self.k == "::" and self.kinds[self.i + 1] == "ID":
                    self.adv()
                    name = texts[self.i]
                    self.adv()
                    if self.k == "::" and self.kinds[self.i + 1] == "ID":
                        scope = scope + "::" + name
                    else:
                        e = A.ScopedName(e.loc, scope, name)
                        break
        elif k == "NUM":
            self.adv()
            size, base, signed, digits, value, based = parse_number(texts[i])
            e = A.Number(self.loc(i), size, base, signed, digits, value, based)
        elif k == "(":
            self.depth += 1
            if self.depth > self.limits.expr_depth:
                self.depth = 0
                raise ParseError(i, "expression nesting too deep")
            self.adv()
            e = self.parse_expr()
            if self.k == ":":   # (min:typ:max) -> typ
                self.adv()
                e = self.parse_expr()
                if self.k == ":":
                    self.adv()
                    self.parse_expr()
            self.expect(")")
            self.depth -= 1
            if self.k == "'(":
                return self.parse_cast_tail(e)
            return e
        elif k == "{":
            return self.parse_concat()
        elif k == "FILL":
            self.adv()
            return A.Fill(self.loc(i), texts[i][1].lower())
        elif k == "SYSID":
            self.adv()
            args = ()
            if self.k == "(":
                self.adv()
                args = self.parse_call_args(sysc=True)
                self.expect(")")
            return A.SysCall(self.loc(i), texts[i], args)
        elif k == "STR":
            self.adv()
            t = texts[i]
            return A.String(self.loc(i), t[1:-1] if t.endswith('"') and len(t) > 1 else t[1:])
        elif k == "REAL" or k == "TIME":
            self.adv()
            t = texts[i].replace("_", "")
            while t and t[-1].isalpha() and t[-1] not in "eE0123456789":
                t = t[:-1]
            try:
                v = float(t)
            except ValueError:
                v = 0.0
            return A.Real(self.loc(i), v)
        elif k == "'{":
            return self.parse_pattern(None)
        elif k in TYPE_KW or k == "signed" or k == "unsigned":
            if k == "signed" or k == "unsigned":
                self.adv()
                dt = A.OtherType(self.loc(i), k)
            else:
                dt = self.parse_data_type()
            if self.k == "'(":
                return self.parse_cast_tail(dt)
            return dt
        elif k == "type":
            self.adv()
            self.expect("(")
            args = self.parse_call_args(sysc=True)
            self.expect(")")
            return A.SysCall(self.loc(i), "type", args)
        elif k == "$":
            self.adv()
            return A.Ident(self.loc(i), "$")
        else:
            raise ParseError(i, f"expected expression, found {self.desc()}")
        return self.parse_postfix(e)

    def parse_cast_tail(self, target):
        i = self.i
        self.adv()   # consumes "'("
        e = self.parse_expr()
        self.expect(")")
        loc = target.loc if target.loc is not None else self.loc(i)
        return A.Cast(loc, target, e)

    def parse_postfix(self, e):
        kinds = self.kinds
        while True:
            k = self.k
            if k == "[":
                self.adv()
                a = self.parse_expr()
                k2 = self.k
                if k2 == ":" or k2 == "+:" or k2 == "-:":
                    self.adv()
                    b = self.parse_expr()
                    self.expect("]")
                    e = A.Select(e.loc, e, k2, a, b)
                else:
                    self.expect("]")
                    e = A.Index(e.loc, e, a)
            elif k == "." and kinds[self.i + 1] == "ID":
                self.adv()
                name = self.texts[self.i]
                self.adv()
                e = A.Member(e.loc, e, name)
            elif k == "(" and e.__class__ in CALLABLE:
                self.adv()
                args = self.parse_call_args()
                self.expect(")")
                e = A.Call(e.loc, e, args)
            elif k == "'(" and e.__class__ in (A.Ident, A.ScopedName, A.Number):
                return self.parse_cast_tail(e)
            elif k == "'{" and e.__class__ in (A.Ident, A.ScopedName):
                return self.parse_pattern(e)
            else:
                return e

    def parse_call_args(self, sysc=False):
        args = []
        if self.k == ")":
            return ()
        while True:
            k = self.k
            if k == ",":
                args.append(None)
            elif k == "." and self.kinds[self.i + 1] == "ID":
                i = self.i
                self.adv()
                name = self.texts[self.i]
                self.adv()
                v = None
                if self.accept("("):
                    if self.k != ")":
                        v = self.parse_expr()
                    self.expect(")")
                args.append(A.NamedArg(self.loc(i), name, v))
            else:
                args.append(self.parse_expr())
            if not self.accept(","):
                break
            if self.k == ")":
                args.append(None)
                break
        return tuple(args)

    def parse_concat(self):
        i = self.i
        loc = self.loc(i)
        self.adv()
        self.depth += 1
        if self.depth > self.limits.expr_depth:
            self.depth = 0
            raise ParseError(i, "expression nesting too deep")
        k = self.k
        if k == "<<" or k == ">>":
            op = k
            self.adv()
            size = None
            if self.k != "{":
                if self.k in TYPE_KW:
                    size = self.parse_data_type()
                else:
                    size = self.parse_expr()
            self.expect("{")
            items = []
            while True:
                e = self.parse_expr()
                if self.k == "with":
                    j = self.i
                    self.adv()
                    self.expect("[")
                    a = self.parse_expr()
                    k2 = self.k
                    if k2 in (":", "+:", "-:"):
                        self.adv()
                        b = self.parse_expr()
                        sel = A.Select(self.loc(j), e, k2, a, b)
                    else:
                        sel = A.Index(self.loc(j), e, a)
                    self.expect("]")
                    e = A.StreamWith(self.loc(j), e, sel)
                items.append(e)
                if not self.accept(","):
                    break
            self.expect("}")
            self.expect("}")
            self.depth -= 1
            return A.Streaming(loc, op, size, tuple(items))
        if k == "}":
            self.adv()
            self.depth -= 1
            return A.Concat(loc, ())
        first = self.parse_expr()
        if self.k == "{":
            self.adv()
            items = [self.parse_expr()]
            while self.accept(","):
                items.append(self.parse_expr())
            self.expect("}")
            self.expect("}")
            self.depth -= 1
            return A.Replicate(loc, first, tuple(items))
        items = [first]
        while self.accept(","):
            items.append(self.parse_expr())
        self.expect("}")
        self.depth -= 1
        return A.Concat(loc, tuple(items))

    def parse_pattern(self, prefix):
        i = self.i
        loc = prefix.loc if prefix is not None else self.loc(i)
        self.adv()   # "'{"
        items = []
        if self.k != "}":
            while True:
                if self.k == "default":
                    j = self.i
                    self.adv()
                    self.expect(":")
                    items.append(A.PatternItem(self.loc(j), "default", self.parse_expr()))
                else:
                    e = self.parse_expr()
                    if self.k == ":":
                        self.adv()
                        v = self.parse_expr()
                        items.append(A.PatternItem(e.loc, e, v))
                    elif self.k == "{":
                        self.adv()
                        sub = [self.parse_expr()]
                        while self.accept(","):
                            sub.append(self.parse_expr())
                        self.expect("}")
                        items.append(A.Replicate(e.loc, e, tuple(sub)))
                    else:
                        items.append(e)
                if not self.accept(","):
                    break
        self.expect("}")
        return A.AssignPattern(loc, prefix, tuple(items))

    # ------------------------------------------------------------------ types and declarations

    def parse_dims(self):
        dims = []
        while self.k == "[":
            i = self.i
            self.adv()
            k = self.k
            if k == "]":
                self.adv()
                dims.append(A.Dim(self.loc(i), "unsized", None, None))
                continue
            if k == "$":
                self.adv()
                self.expect("]")
                dims.append(A.Dim(self.loc(i), "queue", None, None))
                continue
            if k == "*":
                self.adv()
                self.expect("]")
                dims.append(A.Dim(self.loc(i), "assoc", None, None))
                continue
            a = self.parse_expr()
            if self.k == ":":
                self.adv()
                b = self.parse_expr()
                dims.append(A.Dim(self.loc(i), "range", a, b))
            elif isinstance(a, A.DataType):
                dims.append(A.Dim(self.loc(i), "assoc", a, None))
            else:
                dims.append(A.Dim(self.loc(i), "size", a, None))
            self.expect("]")
        return tuple(dims)

    def _signing(self):
        if self.k == "signed":
            self.adv()
            return True
        if self.k == "unsigned":
            self.adv()
            return False
        return None

    def parse_data_type(self, implicit_ok=False):
        k = self.k
        i = self.i
        loc = self.loc(i)
        if k in INT_VEC:
            self.adv()
            sg = self._signing()
            return A.IntType(loc, k, sg, self.parse_dims())
        if k in INT_ATOM:
            self.adv()
            sg = self._signing()
            return A.IntType(loc, k, sg, self.parse_dims() if self.k == "[" else ())
        if k in OTHER_TYPES:
            self.adv()
            return A.OtherType(loc, k)
        if k == "enum":
            return self.parse_enum()
        if k == "struct" or k == "union":
            return self.parse_struct()
        if k == "ID":
            self.adv()
            name = A.Ident(loc, self.texts[i])
            while self.k == "::" and self.kinds[self.i + 1] == "ID":
                self.adv()
                nm = self.texts[self.i]
                self.adv()
                name = A.ScopedName(loc, name.name if isinstance(name, A.Ident) else name.scope + "::" + name.name, nm)
            return A.NamedType(loc, name, self.parse_dims())
        if k == "signed" or k == "unsigned":
            sg = self._signing()
            return A.ImplicitType(loc, sg, self.parse_dims())
        if k == "[":
            return A.ImplicitType(loc, None, self.parse_dims())
        if k == "virtual":
            raise ParseError(i, "virtual interface types are not supported")
        raise ParseError(i, f"expected data type, found {self.desc()}")

    def parse_enum(self):
        i = self.i
        self.adv()
        base = None
        if self.k != "{":
            base = self.parse_data_type()
        self.expect("{")
        members = []
        while True:
            j = self.i
            name = self.ident()
            count = None
            if self.k == "[":
                self.adv()
                count = self.parse_expr()
                if self.accept(":"):
                    self.parse_expr()
                self.expect("]")
            init = None
            if self.accept("="):
                init = self.parse_expr()
            members.append(A.EnumMember(self.loc(j), name, init, count))
            if not self.accept(","):
                break
            if self.k == "}":
                break
        self.expect("}")
        return A.EnumType(self.loc(i), base, tuple(members), self.parse_dims())

    def parse_struct(self):
        i = self.i
        is_union = self.k == "union"
        self.adv()
        if self.k == "tagged":
            self.adv()
        packed = False
        sg = None
        if self.k == "packed":
            packed = True
            self.adv()
            sg = self._signing()
        self.expect("{")
        members = []
        while self.k != "}":
            dt = self.parse_data_type()
            while True:
                j = self.i
                name = self.ident()
                dims = self.parse_dims()
                init = None
                if self.accept("="):
                    init = self.parse_expr()
                members.append(A.StructMember(self.loc(j), dt, name, dims, init))
                if not self.accept(","):
                    break
            self.expect(";")
        self.expect("}")
        return A.StructType(self.loc(i), is_union, packed, sg, tuple(members), self.parse_dims())

    def _scan_dims_end(self, j):
        kinds = self.kinds
        while kinds[j] == "[":
            depth = 0
            while True:
                kk = kinds[j]
                if kk == "[":
                    depth += 1
                elif kk == "]":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                elif kk == "EOF" or kk == ";":
                    return j
                j += 1
        return j

    def _named_type_ahead(self):
        """At an ID: True if tokens look like ``type_name name`` (a declaration)."""
        kinds = self.kinds
        j = self.i + 1
        while kinds[j] == "::" and kinds[j + 1] == "ID":
            j += 2
        if kinds[j] == "ID":
            return True
        if kinds[j] == "[":
            return kinds[self._scan_dims_end(j)] == "ID"
        return False

    def is_decl_start(self):
        k = self.k
        if k in TYPE_KW or k in DECL_PREFIX:
            return True
        if k == "ID":
            return self._named_type_ahead()
        return False

    def parse_declarators(self, allow_init=True):
        res = []
        while True:
            i = self.i
            name = self.ident()
            dims = self.parse_dims()
            init = None
            if allow_init and self.k == "=":
                self.adv()
                init = self.parse_expr()
            res.append((self.loc(i), name, dims, init))
            if not self.accept(","):
                break
        return res

    def parse_var_decl(self, out, terminator=True):
        const = static = False
        while self.k in DECL_PREFIX:
            if self.k == "const":
                const = True
            elif self.k == "static":
                static = True
            self.adv()
        if self.k in TYPE_KW or self.k == "ID" or self.k == "[" or self.k == "signed" or self.k == "unsigned":
            dt = self.parse_data_type()
        else:
            dt = A.ImplicitType(self.loc(self.i), None, ())
        for loc, name, dims, init in self.parse_declarators():
            out.append(A.VarDecl(loc, dt, name, dims, init, const, static))
        if terminator:
            self.expect(";")

    def parse_net_decl(self, out):
        net = self.k
        self.adv()
        if self.k == "(" and self.kinds[self.i + 1] in STRENGTH:
            self.skip_parens()
        if self.k in ("vectored", "scalared"):
            self.adv()
        if self.k in TYPE_KW or (self.k == "ID" and self._named_type_ahead() and
                                 self.kinds[self.i + 1] in ("ID", "::")):
            dt = self.parse_data_type()
        else:
            dt = self.parse_data_type(True) if self.k in ("[", "signed", "unsigned") else \
                A.ImplicitType(self.loc(self.i), None, ())
        if self.k == "#":
            self.skip_delay()
        for loc, name, dims, init in self.parse_declarators():
            out.append(A.NetDecl(loc, net, dt, name, dims, init))
        self.expect(";")

    def parse_port_head(self):
        """At a direction keyword. Returns (direction, net_type, dtype)."""
        direction = self.k
        self.adv()
        net = None
        if self.k in NET_TYPES:
            net = self.k
            self.adv()
        elif self.k == "var":
            self.adv()
        dt = None
        k = self.k
        if k in TYPE_KW or k == "[" or k == "signed" or k == "unsigned":
            dt = self.parse_data_type()
        elif k == "ID" and (self.kinds[self.i + 1] in ("ID", "::") or
                            (self.kinds[self.i + 1] == "[" and self._named_type_ahead())):
            dt = self.parse_data_type()
        elif net is not None:
            dt = A.ImplicitType(self.loc(self.i), None, ())
        return direction, net, dt

    def parse_port_decl_item(self, out):
        direction, net, dt = self.parse_port_head()
        for loc, name, dims, init in self.parse_declarators(allow_init=False):
            out.append(A.Port(loc, direction, net, dt, name, dims, init, None, None))
        self.expect(";")

    def parse_ansi_ports(self, default_dir=None, ports=None):
        """After '(' ; stops at ')' (not consumed)."""
        if ports is None:
            ports = []
        if self.k == ")":
            return ports
        cur = None   # (direction, net, dtype, iface, modport)
        while True:
            k = self.k
            i = self.i
            if k in DIRS:
                d, n, dt = self.parse_port_head()
                cur = (d, n, dt, None, None)
            elif k == "interface":
                self.adv()
                mp = None
                if self.accept("."):
                    mp = self.ident()
                cur = (None, None, None, "interface", mp)
            elif k == "ID" and self.kinds[i + 1] == "." and self.kinds[i + 2] == "ID" and self.kinds[i + 3] == "ID":
                ifc = self.texts[i]
                self.adv()
                self.adv()
                mp = self.ident()
                cur = (None, None, None, ifc, mp)
            elif k == "ID" and self.kinds[i + 1] == "ID" and default_dir is None and (cur is None or cur[0] is None):
                ifc = self.texts[i]
                self.adv()
                cur = (None, None, None, ifc, None)
            elif (k in TYPE_KW or k == "[" or k == "signed" or k == "unsigned" or
                  (k == "ID" and self.kinds[i + 1] in ("ID", "::") and (cur is None or cur[3] is None))):
                # a data type starts a new port; without a direction it inherits the previous one
                dt = self.parse_data_type()
                cur = ((cur[0] if cur is not None and cur[0] else None) or default_dir or "input", None, dt, None, None)
            elif cur is None:
                cur = (default_dir, None, None, None, None)
            j = self.i
            name = self.ident()
            dims = self.parse_dims()
            default = None
            if self.accept("="):
                default = self.parse_expr()
            ports.append(A.Port(self.loc(j), cur[0], cur[1], cur[2], name, dims, default, cur[3], cur[4]))
            if not self.accept(","):
                break
            if self.k == ")":
                break
        return ports

    def parse_param_list(self, header):
        out = []
        local = False
        dtype = None
        is_type = False
        started = False
        while True:
            k = self.k
            if k == "parameter" or k == "localparam":
                local = k == "localparam"
                self.adv()
                dtype = None
                is_type = False
                started = True
            elif not started:
                # header parameter without the keyword
                started = True
            if self.k == "type":
                self.adv()
                is_type = True
                dtype = None
            elif not is_type:
                k = self.k
                if k in TYPE_KW or k == "[" or k == "signed" or k == "unsigned":
                    dtype = self.parse_data_type()
                elif k == "ID" and (self.kinds[self.i + 1] in ("ID", "::") or
                                    (self.kinds[self.i + 1] == "[" and self._named_type_ahead())):
                    dtype = self.parse_data_type()
            i = self.i
            name = self.ident()
            udims = ()
            if self.k == "[":
                udims = tuple(self.parse_dims())
            init = None
            if self.accept("="):
                if is_type:
                    init = self.parse_data_type()
                else:
                    init = self.parse_expr()
            out.append(A.ParamDecl(self.loc(i), local, dtype, name, init, is_type, udims))
            if self.k != ",":
                break
            self.adv()
            if header and self.k == ")":
                break
            if not header and self.k == ";":
                break
            if self.k in ("parameter", "localparam", "type"):
                started = False
                continue
            kk = self.k
            if kk in TYPE_KW or kk == "[" or (kk == "ID" and self.kinds[self.i + 1] in ("ID", "::")):
                started = True
                is_type = False
                dtype = None
                continue
            started = True
        return out

    # ------------------------------------------------------------------ statements

    def parse_event_control(self):
        i = self.i
        self.expect("@")
        loc = self.loc(i)
        if self.k == "*":
            self.adv()
            return A.EventControl(loc, True, ())
        if self.k == "ID":
            j = self.i
            e = self.parse_primary()
            return A.EventControl(loc, False, (A.EventItem(self.loc(j), None, e),))
        self.expect("(")
        if self.k == "*" and self.kinds[self.i + 1] == ")":
            self.adv()
            self.adv()
            return A.EventControl(loc, True, ())
        items = []
        while True:
            j = self.i
            edge = None
            if self.k in ("posedge", "negedge", "edge"):
                edge = self.k
                self.adv()
            e = self.parse_expr()
            if self.k == "iff":
                self.adv()
                self.parse_expr()
            items.append(A.EventItem(self.loc(j), edge, e))
            if self.k == "or" or self.k == ",":
                self.adv()
                continue
            break
        self.expect(")")
        return A.EventControl(loc, False, tuple(items))

    def parse_stmt(self):
        k = self.k
        h = _STMT.get(k)
        if h is not None:
            return h(self)
        i = self.i
        if k == "ID" and self.kinds[i + 1] == ":" and self.kinds[i + 2] != ":":
            self.adv()
            self.adv()
            return self.parse_stmt()
        return self.parse_simple_stmt(True)

    def parse_simple_stmt(self, semi):
        """Assignment, increment/decrement, or call statement."""
        k = self.k
        i = self.i
        loc = self.loc(i)
        if k == "++" or k == "--":
            self.adv()
            t = self.parse_primary()
            if semi:
                self.expect(";")
            return A.ProcAssign(loc, t, "+=" if k == "++" else "-=", A.Number(loc, None, "d", True, "1", 1, False), True)
        if k not in ("ID", "{", "SYSID", "(", "void") and k not in TYPE_KW:
            raise ParseError(i, f"expected statement, found {self.desc()}")
        lhs = self.parse_primary()
        op = self.k
        if op in ASSIGN_OPS:
            self.adv()
            if self.k == "#":
                self.skip_delay()
            elif self.k == "@":
                self.parse_event_control()
            elif self.k == "repeat":
                self.adv()
                self.skip_parens()
                if self.k == "@":
                    self.parse_event_control()
            rhs = self.parse_expr()
            if semi:
                self.expect(";")
            return A.ProcAssign(loc, lhs, op, rhs, op != "<=")
        if op == "++" or op == "--":
            self.adv()
            if semi:
                self.expect(";")
            return A.ProcAssign(loc, lhs, "+=" if op == "++" else "-=", A.Number(loc, None, "d", True, "1", 1, False), True)
        if isinstance(lhs, A.SysCall):
            if semi:
                self.expect(";")
            self.ns += 1
            self.info(i, f"system task {lhs.name} ignored")
            return A.Null(loc)
        if isinstance(lhs, A.Cast) and isinstance(lhs.expr, A.Call):
            lhs = lhs.expr
        if isinstance(lhs, A.Call):
            if semi:
                self.expect(";")
            return A.CallStmt(loc, lhs)
        if isinstance(lhs, (A.Ident, A.ScopedName, A.Member)) and op == ";":
            self.adv()
            return A.CallStmt(loc, A.Call(loc, lhs, ()))
        raise ParseError(self.i, f"expected assignment operator, found {self.desc()}" if op != ";" else "expected assignment")

    def parse_block_item(self, out):
        k = self.k
        if k == ";":
            self.adv()
            return
        if k == "parameter" or k == "localparam":
            local = k == "localparam"
            self.adv()
            for p in self.parse_param_list(False):
                if local and not p.local:
                    p.local = True
                out.append(p)
            self.expect(";")
            return
        if k == "typedef":
            self.parse_typedef(out)
            return
        if self.is_decl_start():
            self.parse_var_decl(out)
            return
        if k == "import":
            self.parse_import(out)
            return
        s = self.parse_stmt()
        if s.__class__ is not A.Null:
            out.append(s)

    def parse_body_items(self, stop):
        items = []
        while True:
            k = self.k
            if k == stop or k in HARD_END or k in ("endcase",):
                break
            start = self.i
            try:
                self.parse_block_item(items)
            except ParseError as e:
                self.report(e)
                self.sync_stmt(start)
            except RecursionError:
                self.report(ParseError(self.i, "nesting too deep"))
                self.sync_stmt(start)
            if self.i == start:
                if self.k == "EOF":
                    break
                self.adv()
        return items

    def s_begin(self):
        i = self.i
        self.adv()
        label = None
        if self.accept(":"):
            label = self.ident()
        items = self.parse_body_items("end")
        if self.k == "end":
            self.adv()
            self.expect_end_label()
        else:
            self.report(ParseError(self.i, f"expected 'end', found {self.desc()}"))
        return A.Block(self.loc(i), label, tuple(items))

    def s_if(self):
        # iterative over else-if chains
        chain = []
        qual = None
        if self.k in ("unique", "unique0", "priority"):
            qual = self.k
            self.adv()
            if self.k in ("case", "casez", "casex"):
                return self.s_case(qual)
        first = True
        while True:
            i = self.i
            self.expect("if")
            self.expect("(")
            cond = self.parse_expr()
            if self.k == "matches":
                raise ParseError(self.i, "pattern matching is not supported")
            self.expect(")")
            then = self.parse_stmt()
            chain.append((self.loc(i), qual if first else None, cond, then))
            first = False
            if self.k == "else":
                self.adv()
                if self.k == "if":
                    continue
                els = self.parse_stmt()
                break
            els = None
            break
        for loc, q, cond, then in reversed(chain):
            els = A.If(loc, q, cond, then, els)
        return els

    def s_case(self, qual=None):
        if qual is None and self.k in ("unique", "unique0", "priority"):
            qual = self.k
            self.adv()
        i = self.i
        kind = self.k
        full = parallel = False
        a = self.attrs.get(i) or self.attrs.get(i - 1)
        self.adv()
        self.expect("(")
        sel = self.parse_expr()
        self.expect(")")
        inside = False
        if self.k == "inside":
            inside = True
            self.adv()
        a2 = self.attrs.get(self.i)
        for aa in (a, a2):
            if aa:
                full = full or "full_case" in aa
                parallel = parallel or "parallel_case" in aa
        items = []
        while self.k != "endcase":
            if self.k in HARD_END or self.k in ("end",):
                self.report(ParseError(self.i, f"expected 'endcase', found {self.desc()}"))
                return A.Case(self.loc(i), kind, qual, inside, sel, tuple(items), full, parallel)
            start = self.i
            try:
                items.append(self.parse_case_item(inside))
            except ParseError as e:
                self.report(e)
                self.sync_stmt(start)
            if self.i == start:
                self.adv()
        self.adv()
        return A.Case(self.loc(i), kind, qual, inside, sel, tuple(items), full, parallel)

    def parse_case_item(self, inside):
        i = self.i
        loc = self.loc(i)
        if self.k == "default":
            self.adv()
            self.accept(":")
            return A.CaseItem(loc, None, self.parse_stmt())
        labels = []
        while True:
            if self.k == "[":
                j = self.i
                self.adv()
                lo = self.parse_expr()
                self.expect(":")
                hi = self.parse_expr()
                self.expect("]")
                labels.append(A.ValueRange(self.loc(j), lo, hi))
            else:
                labels.append(self.parse_expr())
            if not self.accept(","):
                break
        self.expect(":")
        return A.CaseItem(loc, tuple(labels), self.parse_stmt())

    def parse_for_header(self):
        self.expect("(")
        init = []
        if self.k != ";":
            if self.k in TYPE_KW or self.k in DECL_PREFIX or self.k == "genvar" or \
                    (self.k == "ID" and self.kinds[self.i + 1] == "ID"):
                if self.k == "genvar":
                    self.adv()
                    while True:
                        j = self.i
                        init.append(A.Genvar(self.loc(j), self.ident()))
                        if self.k == "=":
                            self.adv()
                            nm = init[-1].name
                            init.append(A.ProcAssign(self.loc(j), A.Ident(self.loc(j), nm), "=", self.parse_expr(), True))
                        if not self.accept(","):
                            break
                else:
                    self.parse_var_decl(init, terminator=False)
            else:
                while True:
                    init.append(self.parse_simple_stmt(False))
                    if not self.accept(","):
                        break
        self.expect(";")
        cond = None
        if self.k != ";":
            cond = self.parse_expr()
        self.expect(";")
        step = []
        if self.k != ")":
            while True:
                step.append(self.parse_simple_stmt(False))
                if not self.accept(","):
                    break
        self.expect(")")
        return tuple(init), cond, tuple(step)

    def s_for(self):
        i = self.i
        self.adv()
        init, cond, step = self.parse_for_header()
        return A.For(self.loc(i), init, cond, step, self.parse_stmt())

    def s_while(self):
        i = self.i
        self.adv()
        self.expect("(")
        c = self.parse_expr()
        self.expect(")")
        return A.While(self.loc(i), c, self.parse_stmt())

    def s_do(self):
        i = self.i
        self.adv()
        body = self.parse_stmt()
        self.expect("while")
        self.expect("(")
        c = self.parse_expr()
        self.expect(")")
        self.expect(";")
        return A.DoWhile(self.loc(i), body, c)

    def s_repeat(self):
        i = self.i
        self.adv()
        self.expect("(")
        c = self.parse_expr()
        self.expect(")")
        return A.Repeat(self.loc(i), c, self.parse_stmt())

    def s_forever(self):
        i = self.i
        self.adv()
        return A.Forever(self.loc(i), self.parse_stmt())

    def s_foreach(self):
        i = self.i
        self.adv()
        self.expect("(")
        arr = self.parse_primary_noindex()
        names = []
        self.expect("[")
        while True:
            names.append(self.ident() if self.k == "ID" else None)
            if not self.accept(","):
                break
        self.expect("]")
        self.expect(")")
        return A.Foreach(self.loc(i), arr, tuple(names), self.parse_stmt())

    def parse_primary_noindex(self):
        i = self.i
        name = self.ident()
        e = A.Ident(self.loc(i), name)
        while self.k == "." and self.kinds[self.i + 1] == "ID":
            self.adv()
            e = A.Member(e.loc, e, self.texts[self.i])
            self.adv()
        return e

    def s_return(self):
        i = self.i
        self.adv()
        v = None
        if self.k != ";":
            v = self.parse_expr()
        self.expect(";")
        return A.Return(self.loc(i), v)

    def s_break(self):
        i = self.i
        self.adv()
        self.expect(";")
        return A.Break(self.loc(i))

    def s_continue(self):
        i = self.i
        self.adv()
        self.expect(";")
        return A.Continue(self.loc(i))

    def s_null(self):
        i = self.i
        self.adv()
        return A.Null(self.loc(i))

    def s_event(self):
        i = self.i
        ev = self.parse_event_control()
        self.ns += 1
        if self.k == ";":
            self.adv()
            return A.EventStmt(self.loc(i), ev, None)
        return A.EventStmt(self.loc(i), ev, self.parse_stmt())

    def s_delay(self):
        i = self.i
        self.skip_delay()
        self.ns += 1
        if self.k == ";":
            self.adv()
            return A.Null(self.loc(i))
        return self.parse_stmt()

    def s_skip(self):
        i = self.i
        name = self.k
        if name in ASSERT_KW:
            self.info(i, f"{name} statement ignored")
        elif name == "fork":
            self.ns += 1
            self.info(i, "fork/join block ignored")
        elif name == "wait":
            self.ns += 1
            self.info(i, "wait statement ignored")
        else:
            self.info(i, f"{name} statement ignored")
        loc = self.loc(i)
        self.skip_statement()
        return A.Null(loc)

    def s_skip_semi(self):
        i = self.i
        self.info(i, f"{self.k} statement ignored")
        loc = self.loc(i)
        self.skip_to_semi()
        return A.Null(loc)

    # ------------------------------------------------------------------ module items

    def parse_typedef(self, out):
        i = self.i
        self.adv()
        k = self.k
        if k in ("class", "struct", "union", "enum", "interface") and self.kinds[self.i + 1] == "ID" and \
                self.kinds[self.i + 2] == ";":
            self.skip_to_semi()
            return
        if k == "ID" and self.kinds[self.i + 1] == ";":
            self.skip_to_semi()
            return
        dt = self.parse_data_type()
        j = self.i
        name = self.ident()
        dims = self.parse_dims()
        self.expect(";")
        out.append(A.Typedef(self.loc(j), name, dt, dims))

    def parse_import(self, out):
        i = self.i
        self.adv()
        if self.k == "STR":
            self.info(i, "DPI import ignored")
            self.skip_to_semi()
            return
        while True:
            j = self.i
            pkg = self.ident()
            self.expect("::")
            if self.k == "*":
                self.adv()
                name = "*"
            else:
                name = self.ident()
            out.append(A.Import(self.loc(j), pkg, name))
            if not self.accept(","):
                break
        self.expect(";")

    def parse_function(self, out):
        i = self.i
        is_task = self.k == "task"
        self.adv()
        automatic = False
        if self.k in ("automatic", "static"):
            automatic = self.k == "automatic"
            self.adv()
        rettype = None
        if not is_task:
            k = self.k
            if k in TYPE_KW or k == "[" or k == "signed" or k == "unsigned":
                rettype = self.parse_data_type()
            elif k == "ID":
                j = self.i + 1
                kinds = self.kinds
                while kinds[j] == "::" and kinds[j + 1] == "ID":
                    j += 2
                if kinds[j] == "ID" or kinds[j] == "[":
                    rettype = self.parse_data_type()
        ns0 = self.ns
        j = self.i
        name = self.ident()
        while self.k == "::" and self.kinds[self.i + 1] == "ID":
            self.adv()
            name = self.ident()
        ports = []
        if self.k == "(":
            self.adv()
            ports = self.parse_ansi_ports("input")
            self.expect(")")
        self.expect(";")
        body = []
        end = "endtask" if is_task else "endfunction"
        while True:
            k = self.k
            if k == end or k in HARD_END:
                break
            start = self.i
            try:
                if k in DIRS:
                    self.parse_port_decl_item(ports)
                else:
                    self.parse_block_item(body)
            except ParseError as e:
                self.report(e)
                self.sync_stmt(start)
            except RecursionError:
                self.report(ParseError(self.i, "nesting too deep"))
                self.sync_stmt(start)
            if self.i == start:
                if self.k == "EOF":
                    break
                self.adv()
        if self.k == end:
            self.adv()
            self.expect_end_label()
        else:
            self.report(ParseError(self.i, f"expected '{end}', found {self.desc()}"))
        nonsynth = self.ns != ns0
        if nonsynth:
            self.info(i, f"{'task' if is_task else 'function'} '{name}' contains non-synthesizable constructs; ignored")
            body = []
        out.append(A.Function(self.loc(j), name, is_task, rettype, automatic, tuple(ports), tuple(body), nonsynth))

    def parse_gen_body(self):
        i = self.i
        if self.k == "begin":
            self.adv()
            label = None
            if self.accept(":"):
                label = self.ident()
            items = []
            self.parse_items(items, "end")
            if self.k == "end":
                self.adv()
                self.expect_end_label()
            else:
                self.report(ParseError(self.i, f"expected 'end', found {self.desc()}"))
            return A.GenBlock(self.loc(i), label, tuple(items))
        items = []
        self.parse_item(items)
        return A.GenBlock(self.loc(i), None, tuple(items))

    def parse_gen_for(self, out):
        i = self.i
        self.adv()
        init, cond, step = self.parse_for_header()
        body = self.parse_gen_body()
        out.append(A.GenFor(self.loc(i), init, cond, step, body))

    def parse_gen_if(self, out):
        i = self.i
        self.adv()
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        then = self.parse_gen_body()
        els = None
        if self.k == "else":
            self.adv()
            if self.k == "if":
                tmp = []
                self.parse_gen_if(tmp)
                els = tmp[0]
            else:
                els = self.parse_gen_body()
        out.append(A.GenIf(self.loc(i), cond, then, els))

    def parse_gen_case(self, out):
        i = self.i
        self.adv()
        self.expect("(")
        sel = self.parse_expr()
        self.expect(")")
        items = []
        while self.k != "endcase":
            if self.k in HARD_END:
                self.report(ParseError(self.i, f"expected 'endcase', found {self.desc()}"))
                break
            j = self.i
            try:
                if self.k == "default":
                    self.adv()
                    self.accept(":")
                    items.append(A.GenCaseItem(self.loc(j), None, self.parse_gen_body()))
                else:
                    labels = [self.parse_expr()]
                    while self.accept(","):
                        labels.append(self.parse_expr())
                    self.expect(":")
                    items.append(A.GenCaseItem(self.loc(j), tuple(labels), self.parse_gen_body()))
            except ParseError as e:
                self.report(e)
                self.sync_item(j)
            if self.i == j:
                self.adv()
        if self.k == "endcase":
            self.adv()
        out.append(A.GenCase(self.loc(i), sel, tuple(items)))

    def parse_always(self, out):
        i = self.i
        kind = self.k
        self.adv()
        ev = None
        if self.k == "@":
            ev = self.parse_event_control()
        elif self.k == "#":
            self.info(i, f"{kind} with delay ignored")
            self.skip_delay()
            self.skip_statement()
            return
        ns0 = self.ns
        body = self.parse_stmt()
        out.append(A.Always(self.loc(i), kind, ev, body))

    def parse_cont_assign(self, out):
        self.adv()
        if self.k == "(" and self.kinds[self.i + 1] in STRENGTH:
            self.skip_parens()
        if self.k == "#":
            self.skip_delay()
        while True:
            i = self.i
            lhs = self.parse_primary()
            self.expect("=")
            rhs = self.parse_expr()
            out.append(A.ContAssign(self.loc(i), lhs, rhs))
            if not self.accept(","):
                break
        self.expect(";")

    def parse_defparam(self, out):
        self.adv()
        while True:
            i = self.i
            path = self.parse_primary()
            self.expect("=")
            val = self.parse_expr()
            out.append(A.Defparam(self.loc(i), path, val))
            if not self.accept(","):
                break
        self.expect(";")

    def parse_gate(self, out):
        gate = self.k
        self.adv()
        if self.k == "(" and self.kinds[self.i + 1] in STRENGTH:
            self.skip_parens()
        if self.k == "#":
            self.skip_delay()
        while True:
            i = self.i
            name = None
            dims = ()
            if self.k == "ID":
                name = self.ident()
                dims = self.parse_dims()
            self.expect("(")
            terms = [self.parse_expr()]
            while self.accept(","):
                terms.append(self.parse_expr())
            self.expect(")")
            out.append(A.GateInst(self.loc(i), gate, name, dims, tuple(terms)))
            if not self.accept(","):
                break
        self.expect(";")

    def parse_instance(self, out):
        i = self.i
        modname = self.texts[i]
        self.adv()
        params = ()
        if self.k == "#":
            if self.kinds[self.i + 1] in ("NUM", "REAL", "TIME"):
                self.skip_delay()            # '#1': delay of a user-defined primitive instance
            else:
                self.adv()
                self.expect("(")
                params = self.parse_param_assigns()
                self.expect(")")
        while True:
            j = self.i
            if self.k == "(":
                # unnamed instance (user-defined primitives may omit the instance name)
                lc = self.loc(j)
                name = f"{modname}${lc[1]}_{lc[2]}"
                dims = ()
            else:
                name = self.ident()
                dims = self.parse_dims()
            self.expect("(")
            conns = self.parse_conns()
            self.expect(")")
            out.append(A.Instance(self.loc(j), modname, params, name, dims, conns))
            if not self.accept(","):
                break
        self.expect(";")

    def parse_param_assigns(self):
        res = []
        if self.k == ")":
            return ()
        while True:
            j = self.i
            if self.k == "." and self.kinds[self.i + 1] == "ID":
                self.adv()
                name = self.ident()
                self.expect("(")
                v = None
                if self.k != ")":
                    v = self.parse_param_value()
                self.expect(")")
                res.append(A.ParamAssign(self.loc(j), name, v))
            else:
                res.append(A.ParamAssign(self.loc(j), None, self.parse_param_value()))
            if not self.accept(","):
                break
        return tuple(res)

    def parse_param_value(self):
        if self.k in TYPE_KW and self.k not in ("struct", "union", "enum") or self.k in ("struct", "union", "enum"):
            return self.parse_data_type()
        return self.parse_expr()

    def parse_conns(self):
        res = []
        if self.k == ")":
            return ()
        while True:
            j = self.i
            k = self.k
            if k == ".*":
                self.adv()
                res.append(A.PortConn(self.loc(j), "wildcard", "*", None))
            elif k == ".":
                self.adv()
                name = self.ident()
                if self.k == "(":
                    self.adv()
                    e = None
                    if self.k != ")":
                        e = self.parse_expr()
                    self.expect(")")
                    res.append(A.PortConn(self.loc(j), "named", name, e))
                else:
                    res.append(A.PortConn(self.loc(j), "implicit", name, None))
            elif k == "," or k == ")":
                res.append(A.PortConn(self.loc(j), "positional", None, None))
            else:
                res.append(A.PortConn(self.loc(j), "positional", None, self.parse_expr()))
            if not self.accept(","):
                break
            if self.k == ")":
                res.append(A.PortConn(self.loc(self.i), "positional", None, None))
                break
        return tuple(res)

    def parse_modport(self, out):
        self.adv()
        while True:
            i = self.i
            name = self.ident()
            self.expect("(")
            ports = []
            d = None
            while self.k != ")":
                if self.k in ("input", "output", "inout", "ref"):
                    d = self.k
                    self.adv()
                elif self.k == "import" or self.k == "export":
                    self.adv()
                    d = "import"
                    if self.k in ("function", "task"):
                        self.adv()
                    # skip prototype up to ',' or ')'
                    depth = 0
                    while not (depth == 0 and self.k in (",", ")")) and self.k not in HARD_END:
                        if self.k == "(":
                            depth += 1
                        elif self.k == ")":
                            depth -= 1
                        self.adv()
                    if self.k == ",":
                        self.adv()
                    continue
                j = self.i
                if self.k == ".":
                    self.adv()
                    nm = self.ident()
                    self.expect("(")
                    if self.k != ")":
                        self.parse_expr()
                    self.expect(")")
                else:
                    nm = self.ident()
                ports.append(A.ModportPort(self.loc(j), d, nm))
                if not self.accept(","):
                    break
            self.expect(")")
            out.append(A.Modport(self.loc(i), name, tuple(ports)))
            if not self.accept(","):
                break
        self.expect(";")

    def parse_id_item(self, out):
        kinds = self.kinds
        i = self.i
        j = i + 1
        while kinds[j] == "::" and kinds[j + 1] == "ID":
            j += 2
        k1 = kinds[j]
        if k1 == "#":
            return self.parse_instance(out)
        if k1 == "(" and j == i + 1:
            return self.parse_instance(out)      # unnamed instance (user-defined primitive)
        if k1 == "ID":
            k2 = kinds[j + 1]
            if k2 == "(":
                return self.parse_instance(out)
            if k2 == "[":
                e = self._scan_dims_end(j + 1)
                if kinds[e] == "(":
                    return self.parse_instance(out)
            return self.parse_var_decl(out)
        if k1 == "[":
            e = self._scan_dims_end(j)
            if kinds[e] == "ID":
                return self.parse_var_decl(out)
        if k1 == ":" and kinds[i + 2] in ASSERT_KW | frozenset(("begin", "generate", "for", "if", "case", "casez", "casex")):
            label = self.texts[i]
            self.adv()
            self.adv()
            n0 = len(out)
            self.parse_item(out)
            if len(out) > n0:
                x = out[n0]
                b = x.body if isinstance(x, A.GenFor) else x.then if isinstance(x, A.GenIf) else \
                    x if isinstance(x, A.GenBlock) else None
                if b is not None and b.label is None:
                    b.label = label
            return None
        if k1 == "." and kinds[j + 1] == "ID" and kinds[j + 2] == "ID":
            self.unsupported(i, "interface-typed declaration is not supported")
            self.skip_to_semi()
            return None
        raise ParseError(self.i, f"unexpected {self.desc()} at module item level" if k1 != "ID" else "bad item")

    def parse_item(self, out):
        k = self.k
        h = _ITEM.get(k)
        if h is not None:
            return h(self, out)
        if k == "ID":
            return self.parse_id_item(out)
        if k in TYPE_KW or k in DECL_PREFIX or k == "signed" or k == "unsigned":
            return self.parse_var_decl(out)
        if k in NET_TYPES:
            return self.parse_net_decl(out)
        if k in GATES:
            return self.parse_gate(out)
        if k in SKIP_BALANCED:
            i = self.i
            if k == "specify":
                self.info(i, "specify block ignored")
            else:
                self.info(i, f"{k} declaration ignored")
            self.skip_balanced(k, SKIP_BALANCED[k])
            return None
        if k in ASSERT_KW:
            self.info(self.i, f"{k} ignored")
            self.skip_to_semi()
            return None
        if k == "SYSID" and self.texts[self.i] in ELAB_TASKS:
            self.info(self.i, f"elaboration system task {self.texts[self.i]} ignored")
            self.skip_to_semi()
            return None
        raise ParseError(self.i, f"unexpected {self.desc()} at module item level")

    # item handlers
    def i_semi(self, out):
        self.adv()

    def i_initial(self, out):
        i = self.i
        self.info(i, f"{self.k} block ignored")
        self.adv()
        self.skip_statement()

    def i_generate(self, out):
        self.adv()
        self.parse_items(out, "endgenerate")
        if self.k == "endgenerate":
            self.adv()
        else:
            self.report(ParseError(self.i, f"expected 'endgenerate', found {self.desc()}"))

    def i_genvar(self, out):
        self.adv()
        while True:
            i = self.i
            out.append(A.Genvar(self.loc(i), self.ident()))
            if not self.accept(","):
                break
        self.expect(";")

    def i_param(self, out):
        local = self.k == "localparam"
        self.adv()
        for p in self.parse_param_list(False):
            if local:
                p.local = True
            out.append(p)
        self.expect(";")

    def i_typedef(self, out):
        self.parse_typedef(out)

    def i_import(self, out):
        self.parse_import(out)

    def i_export(self, out):
        self.info(self.i, "export ignored")
        self.skip_to_semi()

    def i_port(self, out):
        self.parse_port_decl_item(out)

    def i_skip_semi(self, out):
        self.info(self.i, f"{self.k} ignored")
        self.skip_to_semi()

    def i_unsupported_semi(self, out):
        self.unsupported(self.i, f"'{self.k}' is not supported")
        self.skip_to_semi()

    def i_virtual(self, out):
        if self.kinds[self.i + 1] == "class":
            self.adv()
            self.info(self.i, "class declaration ignored")
            self.skip_balanced("class", "endclass")
            return
        self.unsupported(self.i, "virtual interface is not supported")
        self.skip_to_semi()

    def i_default(self, out):
        self.info(self.i, "default clocking/disable ignored")
        self.skip_to_semi()

    def i_interface_kw(self, out):
        raise ParseError(self.i, "unexpected 'interface' inside a module")

    def i_extern(self, out):
        self.info(self.i, "extern declaration ignored")
        self.skip_to_semi()

    # ------------------------------------------------------------------ item loop

    def parse_items(self, out, term):
        """Parse items until ``term`` (not consumed) or a hard stop.  Returns True if ``term`` seen."""
        while True:
            k = self.k
            if k == term:
                return True
            if k in ITEM_STOP:
                return False
            start = self.i
            try:
                self.parse_item(out)
            except ParseError as e:
                self.report(e)
                self.sync_item(start)
            except RecursionError:
                self.report(ParseError(self.i, "nesting too deep"))
                self.sync_item(start)
            if self.i == start:
                if self.k == "EOF":
                    return False
                self.adv()

    def parse_container_body(self, out, term, name):
        """Items of a module/interface/package up to its terminator.  Returns True if properly closed."""
        while True:
            if self.parse_items(out, term):
                self.adv()
                self.expect_end_label()
                return True
            k = self.k
            if k in STRAY_CLOSERS:
                self.report(ParseError(self.i, f"unexpected {self.desc()}"))
                self.adv()
                continue
            if k == "EOF":
                self.report(ParseError(self.i, f"expected '{term}', found end of file"))
            else:
                self.report(ParseError(self.i, f"expected '{term}', found {self.desc()}"))
            return False

    def _nettype_at(self, idx):
        if not self.nettype:
            return "wire"
        ol = bisect_left(self.nl, self.offs[idx]) + 1
        val = "wire"
        for line, v in self.nettype:
            if line <= ol:
                val = v
            else:
                break
        return val

    def parse_module(self, interface=False):
        i = self.i
        kind = self.k
        end_kw = "endinterface" if interface else "endmodule"
        nerr0 = self.nerr
        self.adv()
        if self.k in ("automatic", "static"):
            self.adv()
        header_items: list = []
        params: list = []
        ports: list = []
        ansi = True
        name = "<error>"
        items: list = []
        closed = False
        header_ok = True
        try:
            name = self.ident()
            while self.k == "import":
                self.parse_import(header_items)
            if self.k == "#":
                self.adv()
                self.expect("(")
                if self.k != ")":
                    params = self.parse_param_list(True)
                self.expect(")")
            if self.k == "(":
                self.adv()
                if self.k == ")":
                    pass
                elif self.k == "ID" and self.kinds[self.i + 1] in (",", ")"):
                    ansi = False
                    while True:
                        j = self.i
                        ports.append(A.Port(self.loc(j), None, None, None, self.ident(), (), None, None, None))
                        if not self.accept(","):
                            break
                elif self.k == ".":
                    raise ParseError(self.i, "explicit port expressions in the module header are not supported")
                else:
                    self.parse_ansi_ports(None, ports)
                self.expect(")")
            self.expect(";")
        except ParseError as e:
            self.report(e)
            header_ok = False
            # skip to the end keyword
            while self.k not in (end_kw, "EOF", "module", "macromodule", "package", "primitive"):
                self.adv()
            if self.k == end_kw:
                self.adv()
                self.expect_end_label()
                closed = True
        if header_ok:
            items = header_items
            closed = self.parse_container_body(items, end_kw, name)
        end_line = self.loc(max(self.i - 1, 0))[1]
        mod_cls = A.Interface if interface else A.Module
        m = mod_cls(self.loc(i), name, kind, tuple(params), tuple(ports), ansi, tuple(items),
                    (not closed) or self.nerr != nerr0, self._nettype_at(i), self.loc(i)[0], end_line, None)
        return m

    def parse_package(self):
        i = self.i
        nerr0 = self.nerr
        self.adv()
        if self.k in ("automatic", "static"):
            self.adv()
        name = "<error>"
        items: list = []
        closed = False
        try:
            name = self.ident()
            self.expect(";")
            closed = self.parse_container_body(items, "endpackage", name)
        except ParseError as e:
            self.report(e)
            while self.k not in ("endpackage", "EOF", "module", "macromodule", "package"):
                self.adv()
            if self.k == "endpackage":
                self.adv()
        end_line = self.loc(max(self.i - 1, 0))[1]
        return A.Package(self.loc(i), name, tuple(items), (not closed) or self.nerr != nerr0,
                         self.loc(i)[0], end_line, None)

    # ------------------------------------------------------------------ top level

    def parse_unit(self):
        u = self.unit
        while self.k != "EOF":
            k = self.k
            start = self.i
            try:
                if k == "module" or k == "macromodule":
                    u.modules.append(self.parse_module(False))
                elif k == "interface":
                    if self.kinds[self.i + 1] == "class":
                        self.adv()
                        self.info(self.i, "class declaration ignored")
                        self.skip_balanced("class", "endclass")
                    else:
                        u.interfaces.append(self.parse_module(True))
                elif k == "package":
                    u.packages.append(self.parse_package())
                elif k == "primitive":
                    nm = self.texts[self.i + 1] if self.kinds[self.i + 1] == "ID" else "<unnamed>"
                    from .udp import lower_udp
                    mod, why = lower_udp(self, self.i)
                    if mod is not None:
                        u.modules.append(mod)
                    else:
                        self.unsupported(self.i, f"user-defined primitive '{nm}' not lowered ({why}); "
                                         "instances are treated as black boxes")
                        u.udps.append(nm)
                    self.skip_balanced("primitive", "endprimitive")
                elif k == "extern":
                    self.i_extern(u.unit_items)
                else:
                    self.parse_item(u.unit_items)
            except ParseError as e:
                self.report(e)
                self.sync_item(start)
            except RecursionError:
                self.report(ParseError(self.i, "nesting too deep"))
                self.sync_item(start)
            if self.i == start:
                if self.k in STRAY_CLOSERS or self.k in ITEM_STOP:
                    self.report(ParseError(self.i, f"unexpected {self.desc()}"))
                self.adv()
        return u


_STMT = {
    "begin": Parser.s_begin, "if": Parser.s_if, "unique": Parser.s_if, "unique0": Parser.s_if,
    "priority": Parser.s_if, "case": Parser.s_case, "casez": Parser.s_case, "casex": Parser.s_case,
    "for": Parser.s_for, "while": Parser.s_while, "do": Parser.s_do, "repeat": Parser.s_repeat,
    "forever": Parser.s_forever, "foreach": Parser.s_foreach, "return": Parser.s_return,
    "break": Parser.s_break, "continue": Parser.s_continue, ";": Parser.s_null, "@": Parser.s_event,
    "#": Parser.s_delay, "fork": Parser.s_skip, "wait": Parser.s_skip, "assert": Parser.s_skip,
    "assume": Parser.s_skip, "cover": Parser.s_skip, "restrict": Parser.s_skip, "expect": Parser.s_skip,
    "disable": Parser.s_skip_semi, "assign": Parser.s_skip_semi, "deassign": Parser.s_skip_semi,
    "force": Parser.s_skip_semi, "release": Parser.s_skip_semi, "->": Parser.s_skip_semi,
    "->>": Parser.s_skip_semi,
}

_ITEM = {
    ";": Parser.i_semi, "initial": Parser.i_initial, "final": Parser.i_initial,
    "assign": Parser.parse_cont_assign, "always": Parser.parse_always, "always_comb": Parser.parse_always,
    "always_ff": Parser.parse_always, "always_latch": Parser.parse_always,
    "generate": Parser.i_generate, "genvar": Parser.i_genvar, "parameter": Parser.i_param,
    "localparam": Parser.i_param, "typedef": Parser.i_typedef, "import": Parser.i_import,
    "export": Parser.i_export, "input": Parser.i_port, "output": Parser.i_port, "inout": Parser.i_port,
    "ref": Parser.i_port, "defparam": Parser.parse_defparam,
    "function": Parser.parse_function, "task": Parser.parse_function, "modport": Parser.parse_modport,
    "for": Parser.parse_gen_for, "if": Parser.parse_gen_if, "case": Parser.parse_gen_case,
    "casez": Parser.parse_gen_case, "casex": Parser.parse_gen_case,
    "begin": lambda self, out: out.append(self.parse_gen_body()),
    "bind": Parser.i_skip_semi, "specparam": Parser.i_skip_semi, "timeunit": Parser.i_skip_semi,
    "timeprecision": Parser.i_skip_semi, "alias": Parser.i_unsupported_semi,
    "virtual": Parser.i_virtual, "default": Parser.i_default, "interface": Parser.i_interface_kw,
    "extern": Parser.i_extern,
}
