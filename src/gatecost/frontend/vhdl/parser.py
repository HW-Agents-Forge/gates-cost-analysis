"""Error-tolerant VHDL parser: recursive descent for declarations/statements, Pratt for expressions.

Error recovery follows FRONTEND.md 10.2: statement and declaration errors synchronize on ``;`` or on
the ``end`` of an enclosing open construct, and on design-unit keywords.  A broken unit keeps what was
parsed before the error and is marked ``incomplete``.
"""
from __future__ import annotations

from ...result import Diagnostic
from . import nodes as N

UNIT_KWS = frozenset(("entity", "architecture", "package", "configuration"))
DECL_START = frozenset(("type", "subtype", "constant", "signal", "variable", "shared", "file", "alias",
                        "component", "function", "procedure", "pure", "impure", "attribute", "use",
                        "disconnect", "group", "package"))
PSL_WORDS = frozenset(("property", "sequence", "cover", "assume", "restrict", "vunit", "vprop", "vmode",
                       "assume_guarantee", "restrict_guarantee", "fairness", "strong"))
TIME_UNITS = frozenset(("fs", "ps", "ns", "us", "ms", "sec", "min", "hr"))
CONSTRUCT_KWS = frozenset(("if", "case", "loop", "process", "generate", "block", "architecture", "entity",
                           "package", "function", "procedure", "record", "units", "protected",
                           "configuration", "component", "context", "for", "postponed"))
SYNTH_ATTRS = frozenset(("enum_encoding", "fsm_encoding", "syn_encoding", "keep", "dont_touch", "ram_style",
                         "rom_style", "syn_ramstyle", "syn_romstyle", "async_reg", "shreg_extract", "use_dsp",
                         "use_dsp48", "syn_keep", "max_fanout", "preserve", "noprune", "resource_sharing",
                         "syn_preserve", "equivalent_register_removal", "buffer_type", "fsm_state"))
FILE_IO_CALLS = frozenset(("readline", "writeline", "file_open", "file_close", "endfile", "hread", "hwrite",
                           "oread", "owrite", "finish", "stop", "flush", "deallocate"))

BIN_BP = {
    "and": 1, "or": 1, "nand": 1, "nor": 1, "xor": 1, "xnor": 1,
    "=": 2, "/=": 2, "<": 2, "<=": 2, ">": 2, ">=": 2, "?=": 2, "?/=": 2, "?<": 2, "?<=": 2, "?>": 2, "?>=": 2,
    "sll": 3, "srl": 3, "sla": 3, "sra": 3, "rol": 3, "ror": 3,
    "+": 4, "-": 4, "&": 4,
    "*": 6, "/": 6, "mod": 6, "rem": 6,
    "**": 7,
}
LOGICAL = frozenset(("and", "or", "nand", "nor", "xor", "xnor"))
MODES = frozenset(("in", "out", "inout", "buffer", "linkage"))
OPERATOR_SYMBOLS = frozenset(('"and"', '"or"', '"nand"', '"nor"', '"xor"', '"xnor"', '"="', '"/="', '"<"', '"<="',
                              '">"', '">="', '"+"', '"-"', '"&"', '"*"', '"/"', '"mod"', '"rem"', '"**"',
                              '"abs"', '"not"', '"sll"', '"srl"', '"sla"', '"sra"', '"rol"', '"ror"',
                              '"?="', '"?/="', '"??"'))


class ParseError(Exception):
    def __init__(self, msg, tok, code="PARSE_ERROR"):
        Exception.__init__(self, msg)
        self.msg = msg
        self.tok = tok
        self.code = code


class Parser:
    def __init__(self, tokens, file, lex_errors=(), max_depth=500, max_diags=300, side="after"):
        self.toks = tokens
        self.n = len(tokens)
        self.i = 0
        self.tok = tokens[0]
        self.file = file
        self.side = side
        self.diags: list[Diagnostic] = []
        self.max_depth = max_depth
        self.max_diags = max_diags
        self.depth = 0
        self.open: list[str] = []
        self.units: list = []
        self.cur_unit = None
        self._last_err = None
        self._pf = None
        self._overflow = False
        self.protected_types: set = set()
        for (ln, col, msg) in lex_errors:
            self._add("PARSE_ERROR", msg, "error", True, ln, col)

    # ------------------------------------------------------------------ token helpers
    def adv(self):
        t = self.tok
        if self.i < self.n - 1:
            self.i += 1
            self.tok = self.toks[self.i]
        return t

    def peek(self, k=1):
        j = self.i + k
        return self.toks[j] if j < self.n else self.toks[-1]

    def kw(self, *vals):
        t = self.tok
        return t.kind == "kw" and t.val in vals

    def op(self, *vals):
        t = self.tok
        return t.kind == "op" and t.val in vals

    def is_id(self, val=None):
        t = self.tok
        return (t.kind == "id" and (val is None or t.val == val)) or (val is None and t.kind == "xid")

    def accept_kw(self, v):
        t = self.tok
        if t.kind == "kw" and t.val == v:
            self.adv()
            return True
        return False

    def accept_op(self, v):
        t = self.tok
        if t.kind == "op" and t.val == v:
            self.adv()
            return True
        return False

    def semi(self):
        if not self.accept_op(";"):
            self.soft_err("expected ';' but found " + self.describe(self.tok))

    def err(self, msg, tok=None, code="PARSE_ERROR"):
        raise ParseError(msg, tok or self.tok, code)

    def expect_kw(self, v):
        t = self.tok
        if t.kind == "kw" and t.val == v:
            return self.adv()
        self.err("expected '%s' but found %s" % (v, self.describe(t)))

    def expect_op(self, v):
        t = self.tok
        if t.kind == "op" and t.val == v:
            return self.adv()
        self.err("expected '%s' but found %s" % (v, self.describe(t)))

    def expect_ident(self):
        t = self.tok
        if t.kind == "id" or t.kind == "xid":
            self.adv()
            return t.val
        self.err("expected identifier but found %s" % self.describe(t))

    @staticmethod
    def describe(t):
        if t.kind == "eof":
            return "end of file"
        return "'%s'" % (t.val if t.kind in ("kw", "op", "id", "xid") else t.kind)

    # ------------------------------------------------------------------ diagnostics
    def _add(self, code, msg, sev, affects, line, col):
        if len(self.diags) >= self.max_diags:
            if not self._overflow:
                self._overflow = True
                self.diags.append(Diagnostic("LIMIT_REACHED", "too many diagnostics in this file; further ones suppressed",
                                             "warning", False, self.side, self.file, line, col))
            return
        self.diags.append(Diagnostic(code, msg, sev, affects, self.side, self.file, line, col))

    def record(self, e: ParseError):
        t = e.tok
        if t.kind == "error":
            return
        key = (t.line, t.col)
        if key == self._last_err:
            return
        self._last_err = key
        if self.cur_unit is not None:
            self.cur_unit.incomplete = True
        if e.code == "PARSE_ERROR":
            self._add("PARSE_ERROR", e.msg, "error", True, t.line, t.col)
        else:
            self._add(e.code, e.msg, "warning", True, t.line, t.col)

    def soft_err(self, msg):
        self.record(ParseError(msg, self.tok))
        if self.cur_unit is not None:
            self.cur_unit.incomplete = True

    def info_skip(self, what, tok=None):
        t = tok or self.tok
        self._add("IGNORED_REGION", "%s ignored" % what, "info", False, t.line, t.col)

    def unsupported(self, what, tok=None):
        t = tok or self.tok
        self._add("UNSUPPORTED_CONSTRUCT", "%s is not supported (skipped)" % what, "warning", True, t.line, t.col)

    # ------------------------------------------------------------------ recovery
    def at_unit_start(self):
        t = self.tok
        if t.kind != "kw" or t.val not in UNIT_KWS:
            if t.kind == "kw" and t.val == "library":
                return True
            return False
        p = self.toks[self.i - 1] if self.i else None
        if p is not None and ((p.kind == "kw" and p.val in ("end", "use")) or (p.kind == "op" and p.val == ":")):
            return False
        if t.val == "package":
            # nested package instantiation: "package p is new ..."
            n1, n2, n3 = self.peek(1), self.peek(2), self.peek(3)
            if n2.kind == "kw" and n2.val == "is" and n3.kind == "kw" and n3.val == "new":
                return False
        return True

    def at_term(self, terms=()):
        t = self.tok
        k = t.kind
        if k == "eof":
            return True
        if k != "kw":
            return False
        v = t.val
        if v in terms:
            return True
        if v == "end":
            nx = self.peek(1)
            if nx.kind == "kw":
                if nx.val == "postponed":
                    return "process" in self.open
                return nx.val in self.open
            return bool(self.open)
        if v == "library":
            return not (self.open and self.open[-1] == "context")
        return v in UNIT_KWS and self.at_unit_start()

    def sync(self, terms=(), stop_paren=False):
        """Skip to after the next ';' (depth 0) or to a terminator, never past a unit start."""
        depth = 0
        while True:
            t = self.tok
            k = t.kind
            if k == "eof":
                return
            if k == "op":
                v = t.val
                if v == "(" or v == "{":
                    depth += 1
                elif v == ")" or v == "}":
                    if depth == 0:
                        if stop_paren:
                            return
                    else:
                        depth -= 1
                elif v == ";" and depth == 0:
                    self.adv()
                    return
            elif k == "kw" and depth == 0 and self.at_term(terms):
                return
            self.adv()

    def parse_list(self, item_fn, terms, out=None):
        """Repeat ``item_fn`` until a terminator; errors are recorded and resynchronized."""
        out = [] if out is None else out
        while not self.at_term(terms):
            start = self.i
            try:
                r = item_fn()
                if r is not None:
                    if isinstance(r, list):
                        out.extend(r)
                    else:
                        out.append(r)
            except ParseError as e:
                self.record(e)
                if self.cur_unit is not None:
                    self.cur_unit.incomplete = True
                self.sync(terms)
            if self.i == start and self.tok.kind != "eof" and not self.at_term(terms):
                self.adv()
            elif self.i == start:
                break
        return out

    def finish(self, what, name=None):
        """Consume ``end [what] [name] ;``; report (without raising) when it does not fit."""
        t = self.tok
        if not (t.kind == "kw" and t.val == "end"):
            self.soft_err("missing 'end %s'" % what)
            return False
        nx = self.peek(1)
        if nx.kind == "kw" and nx.val in CONSTRUCT_KWS and nx.val != what.split()[0] and nx.val != "postponed":
            self.soft_err("missing 'end %s' (found 'end %s')" % (what, nx.val))
            return False
        self.adv()
        if what == "package body":
            if self.kw("package"):
                self.adv()
                self.accept_kw("body")
        else:
            if self.kw(what) or (what == "context" and self.is_id("context")):
                self.adv()
                if what == "case" and self.op("?"):
                    self.adv()
        if self.tok.kind in ("id", "xid") or (self.tok.kind == "str" and what in ("function", "procedure")):
            self.adv()
        if not self.accept_op(";"):
            self.soft_err("expected ';' after 'end %s'" % what)
            return False
        return True

    def enter(self):
        self.depth += 1
        if self.depth > self.max_depth:
            self.depth = 0
            self.err("nesting too deep (limit %d)" % self.max_depth, code="LIMIT_REACHED")

    def leave(self):
        if self.depth > 0:
            self.depth -= 1

    # ------------------------------------------------------------------ design file
    def parse_design_file(self):
        ctx: list = []
        while self.tok.kind != "eof":
            start = self.i
            self.cur_unit = None
            try:
                t = self.tok
                if t.kind == "kw" and t.val == "library":
                    ctx.append(self.parse_library())
                elif t.kind == "kw" and t.val == "use":
                    ctx.append(self.parse_use())
                elif t.kind == "id" and t.val == "context":
                    r = self.parse_context(ctx)
                    if isinstance(r, N.ContextRef):
                        ctx.append(r)
                    else:
                        self.units.append(r)
                        ctx = []
                else:
                    u = None
                    if t.kind == "kw" and t.val == "entity":
                        u = self.parse_entity(ctx)
                    elif t.kind == "kw" and t.val == "architecture":
                        u = self.parse_architecture(ctx)
                    elif t.kind == "kw" and t.val == "package":
                        u = self.parse_package_unit(ctx)
                    elif t.kind == "kw" and t.val == "configuration":
                        u = self.parse_configuration(ctx)
                    else:
                        self.err("expected a design unit (entity, architecture, package, configuration) but found "
                                 + self.describe(t))
                    ctx = []
                    if u is not None:
                        self.units.append(u)
            except ParseError as e:
                self.record(e)
                if self.cur_unit is not None:
                    self.cur_unit.incomplete = True
                    if self.cur_unit not in self.units:
                        self.units.append(self.cur_unit)
                self.open.clear()
                self.depth = 0
                self.sync_unit()
                ctx = []
            if self.i == start and self.tok.kind != "eof":
                self.adv()
        return self.units

    def sync_unit(self):
        while self.tok.kind != "eof":
            if self.at_unit_start() or (self.tok.kind == "id" and self.tok.val == "context"
                                        and self.peek(1).kind == "id"
                                        and self.peek(2).kind == "kw" and self.peek(2).val == "is"):
                if self.i > 0 or True:
                    return
            self.adv()

    def parse_library(self):
        t = self.expect_kw("library")
        names = [self.expect_ident()]
        while self.accept_op(","):
            names.append(self.expect_ident())
        self.semi()
        return N.LibraryClause(names, line=t.line, col=t.col)

    def parse_selected_path(self):
        parts = []
        while True:
            t = self.tok
            if t.kind in ("id", "xid", "str", "char"):
                parts.append(t.val)
                self.adv()
            elif t.kind == "kw" and t.val == "all":
                parts.append("all")
                self.adv()
            else:
                self.err("expected a name but found " + self.describe(t))
            if not self.accept_op("."):
                break
        return tuple(parts)

    def parse_use(self):
        t = self.expect_kw("use")
        paths = [self.parse_selected_path()]
        while self.accept_op(","):
            paths.append(self.parse_selected_path())
        self.semi()
        for p in paths:
            if len(p) >= 2 and p[0] == "std" and p[1] in ("textio", "env"):
                self.info_skip("use of std.%s" % p[1], t)
        return N.UseClause(paths, line=t.line, col=t.col)

    def parse_context(self, ctx):
        t = self.adv()  # 'context'
        if self.peek(0).kind in ("id", "xid") and self.peek(1).kind == "kw" and self.peek(1).val == "is":
            name = self.expect_ident()
            u = N.ContextDecl(name, list(ctx), [], line=t.line, col=t.col)
            self.cur_unit = u
            self.expect_kw("is")
            self.open.append("context")
            u.items = self.parse_list(self.parse_context_item, ())
            self.open.pop()
            self.finish("context", name)
            u.end_line = self.toks[self.i - 1].line
            return u
        paths = [self.parse_selected_path()]
        while self.accept_op(","):
            paths.append(self.parse_selected_path())
        self.semi()
        return N.ContextRef(paths, line=t.line, col=t.col)

    def parse_context_item(self):
        t = self.tok
        if t.kind == "kw" and t.val == "library":
            return self.parse_library()
        if t.kind == "kw" and t.val == "use":
            return self.parse_use()
        if t.kind == "id" and t.val == "context":
            return self.parse_context([])
        self.err("expected library, use or context clause")

    # ------------------------------------------------------------------ design units
    def parse_entity(self, ctx):
        t = self.adv()
        name = self.expect_ident()
        u = N.Entity(name, list(ctx), [], [], [], [], line=t.line, col=t.col)
        self.cur_unit = u
        if not self.accept_kw("is"):
            self.soft_err("expected 'is' but found " + self.describe(self.tok))
        self.open.append("entity")
        try:
            if self.kw("generic"):
                u.generics = self.parse_generic_clause()
            if self.kw("port"):
                u.ports = self.parse_port_clause()
            self.parse_list(self.parse_decl, ("begin",), u.decls)
            if self.accept_kw("begin"):
                self.parse_list(self.parse_concurrent_stmt, (), u.stmts)
        finally:
            self.open.pop()
        self.finish("entity", name)
        u.end_line = self.toks[self.i - 1].line
        return u

    def parse_architecture(self, ctx):
        t = self.adv()
        name = self.expect_ident()
        u = N.Architecture(name, list(ctx), "?", [], [], line=t.line, col=t.col)
        self.cur_unit = u
        try:
            self.expect_kw("of")
            u.entity = self.parse_type_mark_name()
            self.expect_kw("is")
        except ParseError as e:
            self.record(e)
            while self.tok.kind != "eof" and not self.kw("is", "begin") and not self.at_unit_start():
                self.adv()
            self.accept_kw("is")
        self.open.append("architecture")
        try:
            self.parse_list(self.parse_decl, ("begin",), u.decls)
            if self.accept_kw("begin"):
                self.parse_list(self.parse_concurrent_stmt, (), u.stmts)
            else:
                self.soft_err("expected 'begin' in architecture '%s'" % name)
        finally:
            self.open.pop()
        self.finish("architecture", name)
        u.end_line = self.toks[self.i - 1].line
        return u

    def parse_type_mark_name(self):
        """Entity name in an architecture header: last component of a possibly selected name."""
        name = self.expect_ident()
        while self.accept_op("."):
            name = self.expect_ident()
        return name

    def parse_package_unit(self, ctx):
        t = self.adv()
        if self.accept_kw("body"):
            name = self.expect_ident()
            u = N.PackageBody(name, list(ctx), [], line=t.line, col=t.col)
            self.cur_unit = u
            self.expect_kw("is")
            self.open.append("package")
            try:
                self.parse_list(self.parse_decl, (), u.decls)
            finally:
                self.open.pop()
            self.finish("package body", name)
            u.end_line = self.toks[self.i - 1].line
            return u
        name = self.expect_ident()
        u = N.Package(name, list(ctx), [], [], line=t.line, col=t.col)
        self.cur_unit = u
        self.expect_kw("is")
        if self.kw("new"):  # package instantiation as a design unit
            self.adv()
            self.unsupported("package instantiation unit")
            self.sync(())
            u.incomplete = True
            return u
        self.open.append("package")
        try:
            if self.kw("generic"):
                u.generics = self.parse_generic_clause()
                if self.kw("generic"):  # generic map
                    self.adv()
                    self.expect_kw("map")
                    self.parse_map_list()
                    self.semi()
            self.parse_list(self.parse_decl, (), u.decls)
        finally:
            self.open.pop()
        self.finish("package", name)
        u.end_line = self.toks[self.i - 1].line
        return u

    def parse_configuration(self, ctx):
        t = self.adv()
        name = self.expect_ident()
        self.expect_kw("of")
        ent = self.parse_type_mark_name()
        u = N.Configuration(name, list(ctx), ent, None, line=t.line, col=t.col)
        self.cur_unit = u
        self.expect_kw("is")
        self.info_skip("configuration '%s' body (default binding by name is used)" % name, t)
        depth = 0
        first = True
        while self.tok.kind != "eof":
            tk = self.tok
            if tk.kind == "kw" and tk.val == "for":
                self.adv()
                if first:
                    first = False
                    if self.tok.kind == "id":
                        u.architecture = self.tok.val
                depth += 1
                continue
            if tk.kind == "kw" and tk.val == "end":
                nx = self.peek(1)
                if nx.kind == "kw" and nx.val == "for":
                    self.adv()
                    self.adv()
                    depth -= 1
                    continue
                if depth <= 0:
                    break
                # tolerate a missing "end for"
                self.adv()
                continue
            if self.at_unit_start() and depth <= 0:
                break
            self.adv()
        self.open.append("configuration")
        self.finish("configuration", name)
        self.open.pop()
        u.end_line = self.toks[self.i - 1].line
        return u

    # ------------------------------------------------------------------ interface lists
    def parse_generic_clause(self):
        self.expect_kw("generic")
        return self.parse_interface_list("generic")

    def parse_port_clause(self):
        self.expect_kw("port")
        r = self.parse_interface_list("port")
        self.semi()
        return r

    def parse_interface_list(self, kind):
        self.expect_op("(")
        out = []
        if self.op(")"):
            self.adv()
            if kind == "generic":
                self.semi()
            return out
        while True:
            start = self.i
            try:
                out.extend(self.parse_interface_element(kind))
            except ParseError as e:
                self.record(e)
                if self.cur_unit is not None:
                    self.cur_unit.incomplete = True
                self.sync_elem()
            if self.accept_op(";"):
                if self.op(")"):
                    self.adv()
                    break
                continue
            if self.accept_op(")"):
                break
            if self.tok.kind in ("eof", "kw") or self.at_term(()):
                self.soft_err("unterminated interface list")
                return out
            self.soft_err("expected ';' or ')' in interface list but found " + self.describe(self.tok))
            self.sync_elem()
            if self.accept_op(";"):
                continue
            if self.accept_op(")"):
                break
            if self.tok.kind in ("eof", "kw"):
                return out
            if self.i == start:
                self.adv()
        if kind == "generic":
            self.semi()
        return out

    def sync_elem(self):
        depth = 0
        while True:
            t = self.tok
            if t.kind == "eof":
                return
            if t.kind == "op":
                if t.val == "(":
                    depth += 1
                elif t.val == ")":
                    if depth == 0:
                        return
                    depth -= 1
                elif t.val == ";" and depth == 0:
                    return
            elif t.kind == "kw" and depth == 0 and t.val in ("begin", "end", "is", "return", "port", "generic"):
                return
            elif self.at_unit_start():
                return
            self.adv()

    def parse_interface_element(self, kind):
        t0 = self.tok
        cls = "constant" if kind == "generic" else ("signal" if kind == "port" else None)
        if t0.kind == "kw" and t0.val in ("constant", "signal", "variable", "file"):
            cls = t0.val
            self.adv()
        elif kind == "generic" and t0.kind == "kw" and t0.val == "type":
            self.adv()
            name = self.expect_ident()
            return [N.Generic(name, "type", None, None, line=t0.line, col=t0.col)]
        elif kind == "generic" and t0.kind == "kw" and t0.val in ("function", "procedure", "pure", "impure", "package"):
            self.unsupported("generic %s" % t0.val)
            self.sync_elem()
            return [N.Generic("<subprogram>", "subprogram", None, None, line=t0.line, col=t0.col)]
        names = [(self.tok.line, self.tok.col, self.expect_ident())]
        while self.accept_op(","):
            names.append((self.tok.line, self.tok.col, self.expect_ident()))
        self.expect_op(":")
        mode = None
        if self.tok.kind == "kw" and self.tok.val in MODES:
            mode = self.adv().val
        elif kind == "port":
            mode = "in"
        elif kind == "param":
            mode = "in"
        st = self.parse_subtype_ind()
        self.accept_kw("bus")
        default = None
        if self.accept_op(":="):
            default = self.parse_expr()
        out = []
        for (ln, col, nm) in names:
            if kind == "generic":
                out.append(N.Generic(nm, "constant", st, default, line=ln, col=col))
            elif kind == "port":
                out.append(N.Port(nm, mode, st, default, line=ln, col=col))
            else:
                out.append(N.Param(nm, cls or ("variable" if mode in ("out", "inout") else "constant"), mode, st,
                                   default, line=ln, col=col))
        return out

    # ------------------------------------------------------------------ subtype indications
    def parse_type_mark(self):
        t = self.tok
        if t.kind not in ("id", "xid"):
            self.err("expected a type name but found " + self.describe(t))
        self.adv()
        e = N.Ident(t.val, line=t.line, col=t.col)
        while self.op("."):
            self.adv()
            s = self.tok
            if s.kind not in ("id", "xid"):
                self.err("expected a name after '.'")
            self.adv()
            e = N.Select(e, s.val, line=s.line, col=s.col)
        if self.op("'") and self.peek(1).kind in ("id", "kw") and self.peek(1).val in ("subtype", "base", "element"):
            self.adv()
            a = self.adv()
            e = N.Attr(e, a.val, None, line=a.line, col=a.col)
        return e

    def parse_subtype_ind(self):
        t = self.tok
        resolution = None
        mark = self.parse_type_mark()
        if self.tok.kind in ("id", "xid"):
            resolution = mark
            mark = self.parse_type_mark()
        elif self.op("(") and mark is not None and self.peek(1).kind == "id" and self.peek(2).kind == "op" \
                and self.peek(2).val == ")" and self.peek(3).kind in ("id", "xid"):
            self.adv()
            resolution = self.parse_type_mark()
            self.expect_op(")")
            mark = self.parse_type_mark()
        index = None
        rng = None
        box = False
        if self.op("("):
            index = self.parse_index_constraint()
            while self.op("("):  # element constraints (VHDL-2008): further dimensions of the array-of-arrays
                more = self.parse_index_constraint()
                if index:
                    index = index + more
        elif self.kw("range"):
            self.adv()
            if self.op("<>"):
                self.adv()
                box = True
            else:
                rng = self.parse_range()
        return N.SubtypeInd(mark, resolution, index, rng, box, line=t.line, col=t.col)

    def parse_index_constraint(self):
        self.expect_op("(")
        self.enter()
        try:
            items = []
            if self.kw("open"):
                self.adv()
            else:
                while True:
                    items.append(self.parse_discrete_range())
                    if not self.accept_op(","):
                        break
            self.expect_op(")")
        finally:
            self.leave()
        return items

    def parse_range(self):
        left = self.parse_expr()
        if self.kw("to", "downto"):
            d = self.adv()
            right = self.parse_expr()
            return N.Range(left, d.val, right, line=d.line, col=d.col)
        return left

    def parse_discrete_range(self):
        t = self.tok
        e = self.parse_expr()
        if self.kw("to", "downto"):
            d = self.adv()
            return N.Range(e, d.val, self.parse_expr(), line=d.line, col=d.col)
        if self.kw("range"):
            self.adv()
            if self.op("<>"):
                self.adv()
                return N.SubtypeInd(e, None, None, None, True, line=t.line, col=t.col)
            return N.SubtypeInd(e, None, None, self.parse_range(), False, line=t.line, col=t.col)
        return e

    # ------------------------------------------------------------------ expressions
    def parse_expr(self, min_bp=0):
        self.enter()
        try:
            left = self.parse_unary()
            while True:
                t = self.tok
                k = t.kind
                if k != "op" and k != "kw":
                    break
                bp = BIN_BP.get(t.val)
                if bp is None or bp <= min_bp:
                    break
                self.adv()
                right = self.parse_expr(bp)
                left = N.Binary(t.val, left, right, line=t.line, col=t.col)
            return left
        finally:
            self.leave()

    def parse_unary(self):
        t = self.tok
        k = t.kind
        if k == "kw":
            v = t.val
            if v == "not" or v == "abs":
                self.adv()
                return N.Unary(v, self.parse_expr(7), line=t.line, col=t.col)
            if v in LOGICAL:
                self.adv()
                return N.Unary(v, self.parse_expr(7), line=t.line, col=t.col)
        elif k == "op":
            v = t.val
            if v == "-" or v == "+":
                self.adv()
                return N.Unary(v, self.parse_expr(5), line=t.line, col=t.col)
            if v == "??":
                self.adv()
                return N.Unary(v, self.parse_expr(7), line=t.line, col=t.col)
        return self.parse_primary()

    def parse_primary(self):
        t = self.tok
        k = t.kind
        if k == "id" or k == "xid":
            self.adv()
            return self.parse_name_suffix(N.Ident(t.val, line=t.line, col=t.col))
        if k == "int" or k == "real" or k == "based":
            self.adv()
            lit = N.Literal(k, t.num if k == "based" else t.val, None, line=t.line, col=t.col)
            if k == "based":
                lit.text = "%d#%s#" % (t.val[0], t.val[1])
            nt = self.tok
            if nt.kind == "id":          # a unit of TIME or of a user-defined physical type
                self.adv()
                return N.PhysicalLit(lit, nt.val, line=t.line, col=t.col)
            return lit
        if k == "op":
            if t.val == "(":
                return self.parse_paren()
            if t.val == "<<":
                self.unsupported("external name")
                depth = 0
                while self.tok.kind != "eof":
                    if self.op(">>"):
                        self.adv()
                        break
                    if self.op(";"):
                        break
                    self.adv()
                return N.Skipped("external_name", None, line=t.line, col=t.col)
        elif k == "char":
            self.adv()
            return N.Literal("char", t.val, None, line=t.line, col=t.col)
        elif k == "str":
            self.adv()
            if self.op("(") and ('"%s"' % t.val.lower()) in OPERATOR_SYMBOLS:
                return self.parse_name_suffix(N.Ident('"%s"' % t.val.lower(), line=t.line, col=t.col))
            lit = N.Literal("str", t.val, None, line=t.line, col=t.col)
            if self.op("'"):
                return self.parse_name_suffix(lit)
            return lit
        elif k == "bitstr":
            self.adv()
            b = t.val
            return N.BitStringLit(b.size, b.base, b.sign, b.digits, line=t.line, col=t.col)
        elif k == "kw":
            v = t.val
            if v == "null":
                self.adv()
                return N.Literal("null", None, None, line=t.line, col=t.col)
            if v == "new":
                self.adv()
                self.info_skip("allocator ('new')", t)
                depth = 0
                while self.tok.kind != "eof":
                    x = self.tok
                    if x.kind == "op":
                        if x.val == "(":
                            depth += 1
                        elif x.val == ")":
                            if depth == 0:
                                break
                            depth -= 1
                        elif x.val in (";", ",") and depth == 0:
                            break
                    self.adv()
                return N.Skipped("access", None, line=t.line, col=t.col)
            if v == "others":
                self.adv()
                return N.Others(line=t.line, col=t.col)
            if v == "open":
                self.adv()
                return N.Open(line=t.line, col=t.col)
            if v == "unaffected":
                self.adv()
                return N.Unaffected(line=t.line, col=t.col)
        self.err("unexpected %s in expression" % self.describe(t))

    def parse_name_suffix(self, e):
        while True:
            t = self.tok
            if t.kind != "op":
                return e
            v = t.val
            if v == ".":
                self.adv()
                s = self.tok
                if s.kind in ("id", "xid", "str", "char"):
                    self.adv()
                    e = N.Select(e, s.val, line=s.line, col=s.col)
                elif s.kind == "kw" and s.val == "all":
                    self.adv()
                    e = N.Select(e, "all", line=s.line, col=s.col)
                else:
                    self.err("expected a name after '.' but found " + self.describe(s))
            elif v == "(":
                e = self.parse_apply(e)
            elif v == "'":
                self.adv()
                s = self.tok
                if s.kind == "op" and s.val == "(":
                    inner = self.parse_paren()
                    e = N.Qualified(e, inner, line=t.line, col=t.col)
                elif s.kind in ("id", "kw"):
                    self.adv()
                    arg = None
                    if self.op("("):
                        self.adv()
                        arg = self.parse_expr()
                        self.expect_op(")")
                    e = N.Attr(e, s.val, arg, line=s.line, col=s.col)
                else:
                    self.err("expected an attribute name after \"'\"")
            else:
                return e

    def parse_apply(self, prefix):
        t = self.expect_op("(")
        self.enter()
        try:
            items = []
            while True:
                items.append(self.parse_assoc_item())
                if self.accept_op(","):
                    continue
                self.expect_op(")")
                break
        finally:
            self.leave()
        if len(items) == 1 and items[0].formal is None and isinstance(items[0].actual, N.Range):
            return N.Slice(prefix, items[0].actual, line=t.line, col=t.col)
        return N.Apply(prefix, items, line=t.line, col=t.col)

    def parse_assoc_expr(self):
        if self.kw("open"):
            t = self.adv()
            return N.Open(line=t.line, col=t.col)
        if self.kw("inertial"):
            self.adv()
        t = self.tok
        e = self.parse_expr()
        if self.kw("to", "downto"):
            d = self.adv()
            return N.Range(e, d.val, self.parse_expr(), line=d.line, col=d.col)
        if self.kw("range") and isinstance(e, (N.Ident, N.Select, N.Attr)):
            self.adv()
            return N.SubtypeInd(e, None, None, self.parse_range(), False, line=t.line, col=t.col)
        return e

    def parse_assoc_item(self):
        t = self.tok
        first = self.parse_assoc_expr()
        if self.op("=>"):
            self.adv()
            actual = self.parse_assoc_expr()
            return N.Assoc(first, actual, line=t.line, col=t.col)
        return N.Assoc(None, first, line=t.line, col=t.col)

    def parse_choice(self):
        t = self.tok
        if t.kind == "kw" and t.val == "others":
            self.adv()
            return N.Others(line=t.line, col=t.col)
        e = self.parse_expr()
        if self.kw("to", "downto"):
            d = self.adv()
            return N.Range(e, d.val, self.parse_expr(), line=d.line, col=d.col)
        if self.kw("range") and isinstance(e, (N.Ident, N.Select)):
            self.adv()
            return N.SubtypeInd(e, None, None, self.parse_range(), False, line=t.line, col=t.col)
        return e

    def parse_choices(self):
        out = [self.parse_choice()]
        while self.op("|") or self.op("!"):
            self.adv()
            out.append(self.parse_choice())
        return out

    def parse_paren(self):
        """'(' ... ')' : parenthesized expression (returns the inner expression) or an aggregate."""
        t = self.expect_op("(")
        self.enter()
        try:
            items = []
            while True:
                st = self.tok
                choices = self.parse_choices()
                if self.op("=>"):
                    self.adv()
                    value = self.parse_assoc_expr()
                    items.append(N.ElemAssoc(choices, value, line=st.line, col=st.col))
                else:
                    if len(choices) > 1:
                        self.err("unexpected '|' in aggregate")
                    items.append(N.ElemAssoc(None, choices[0], line=st.line, col=st.col))
                if self.accept_op(","):
                    continue
                self.expect_op(")")
                break
        finally:
            self.leave()
        if len(items) == 1 and items[0].choices is None and not isinstance(items[0].value, N.Others):
            return items[0].value
        return N.Aggregate(items, line=t.line, col=t.col)

    def parse_name(self):
        t = self.tok
        if t.kind not in ("id", "xid"):
            self.err("expected a name but found " + self.describe(t))
        self.adv()
        return self.parse_name_suffix(N.Ident(t.val, line=t.line, col=t.col))

    def parse_target(self):
        if self.op("("):
            return self.parse_paren()
        return self.parse_name()

    # ------------------------------------------------------------------ declarations
    def parse_decl(self):
        t = self.tok
        if t.kind == "kw":
            v = t.val
            if v == "type":
                return self.parse_type_decl()
            if v == "subtype":
                self.adv()
                name = self.expect_ident()
                self.expect_kw("is")
                st = self.parse_subtype_ind()
                self.semi()
                return N.SubtypeDecl(name, st, line=t.line, col=t.col)
            if v in ("constant", "signal", "variable", "shared"):
                return self.parse_object_decl()
            if v == "file":
                self.adv()
                self.info_skip("file declaration", t)
                self.sync(())
                return N.Skipped("file", None, line=t.line, col=t.col)
            if v == "alias":
                return self.parse_alias()
            if v == "component":
                return self.parse_component()
            if v in ("function", "procedure", "pure", "impure"):
                return self.parse_subprogram()
            if v == "attribute":
                return self.parse_attribute()
            if v == "use":
                return self.parse_use()
            if v == "for":
                self.info_skip("configuration specification", t)
                self.sync(())
                return N.Skipped("config_spec", None, line=t.line, col=t.col)
            if v in ("disconnect", "group"):
                self.info_skip(v + " declaration", t)
                self.sync(())
                return N.Skipped(v, None, line=t.line, col=t.col)
            if v == "package":
                return self.parse_package_inst()
        elif t.kind == "id":
            if t.val in PSL_WORDS or (t.val == "default" and self.peek(1).kind in ("id", "kw")):
                self.info_skip("PSL declaration", t)
                self.sync(())
                return N.Skipped("psl", None, line=t.line, col=t.col)
            if t.val == "context" and self.peek(1).kind == "id":
                return self.parse_context([])
        self.err("unexpected %s in declarative part" % self.describe(t))

    def parse_package_inst(self):
        t = self.expect_kw("package")
        name = self.expect_ident()
        self.expect_kw("is")
        if not self.kw("new"):
            self.unsupported("nested package declaration", t)
            depth = 1
            while self.tok.kind != "eof":
                if self.kw("end") and self.peek(1).kind == "kw" and self.peek(1).val == "package":
                    self.adv()
                    self.adv()
                    self.sync(())
                    break
                self.adv()
            return N.Skipped("package", None, line=t.line, col=t.col)
        self.adv()
        target = self.parse_selected_path()
        gm = []
        if self.kw("generic"):
            self.adv()
            self.expect_kw("map")
            gm = self.parse_map_list()
        self.semi()
        return N.PackageInst(name, target, gm, line=t.line, col=t.col)

    def parse_type_decl(self):
        t = self.expect_kw("type")
        name = self.expect_ident()
        if self.accept_op(";"):
            return N.TypeDecl(name, None, line=t.line, col=t.col)
        self.expect_kw("is")
        d = self.tok
        definition = None
        if d.kind == "op" and d.val == "(":
            self.adv()
            lits = []
            while True:
                x = self.tok
                if x.kind in ("id", "xid"):
                    lits.append(x.val)
                elif x.kind == "char":
                    lits.append("'%s'" % x.val)
                else:
                    self.err("expected an enumeration literal but found " + self.describe(x))
                self.adv()
                if not self.accept_op(","):
                    break
            self.expect_op(")")
            definition = N.EnumType(lits, line=d.line, col=d.col)
        elif d.kind == "kw" and d.val == "range":
            self.adv()
            rng = self.parse_range()
            if self.kw("units"):
                return N.TypeDecl(name, self.parse_physical_units(rng, d), line=t.line, col=t.col)
            else:
                definition = N.RangeType(rng, line=d.line, col=d.col)
        elif d.kind == "kw" and d.val == "array":
            self.adv()
            self.expect_op("(")
            idx = []
            unconstrained = False
            while True:
                r = self.parse_discrete_range()
                if isinstance(r, N.SubtypeInd) and r.box:
                    unconstrained = True
                elif self.op("<>"):  # "type_mark <>" tolerated
                    self.adv()
                    unconstrained = True
                idx.append(r)
                if not self.accept_op(","):
                    break
            self.expect_op(")")
            self.expect_kw("of")
            elem = self.parse_subtype_ind()
            definition = N.ArrayType(idx, unconstrained, elem, line=d.line, col=d.col)
        elif d.kind == "kw" and d.val == "record":
            self.adv()
            self.open.append("record")
            try:
                fields = self.parse_list(self.parse_record_field, ())
            finally:
                self.open.pop()
            self.finish("record", name)
            definition = N.RecordType(fields, line=d.line, col=d.col)
            return N.TypeDecl(name, definition, line=t.line, col=t.col)
        elif d.kind == "kw" and d.val in ("access", "file"):
            self.info_skip("%s type '%s'" % (d.val, name), d)
            self.sync(())
            return N.TypeDecl(name, N.Skipped(d.val, None, line=d.line, col=d.col), line=t.line, col=t.col)
        elif d.kind == "kw" and d.val == "protected":
            self.info_skip("protected type '%s'" % name, d)
            self.protected_types.add(name)
            self.skip_to_end("protected")
            return N.TypeDecl(name, N.Skipped("protected", None, line=d.line, col=d.col), line=t.line, col=t.col)
        else:
            self.err("unsupported type definition starting at " + self.describe(d))
        self.semi()
        return N.TypeDecl(name, definition, line=t.line, col=t.col)

    def parse_physical_units(self, rng, d):
        """``units base; name = 1000 base; ... end units [name]`` of a physical type declaration."""
        self.expect_kw("units")
        units = [N.UnitDecl(self.expect_ident(), None, None, line=d.line, col=d.col)]
        self.semi()
        while not self.kw("end"):
            if self.tok.kind == "eof":
                self.err("unterminated units declaration")
            ut = self.tok
            uname = self.expect_ident()
            self.expect_op("=")
            lt = self.tok
            if lt.kind in ("int", "real", "based"):
                self.adv()
                lit = N.Literal(lt.kind, lt.num if lt.kind == "based" else lt.val, None, line=lt.line, col=lt.col)
                if lt.kind == "based":
                    lit.text = "%d#%s#" % (lt.val[0], lt.val[1])
            else:
                lit = N.Literal("int", 1, None, line=lt.line, col=lt.col)
            ref = self.expect_ident()
            self.semi()
            units.append(N.UnitDecl(uname, lit, ref, line=ut.line, col=ut.col))
        self.expect_kw("end")
        self.expect_kw("units")
        if self.tok.kind in ("id", "xid"):
            self.adv()
        self.semi()
        return N.PhysicalType(rng, units, line=d.line, col=d.col)

    def skip_to_end(self, what):
        """Skip to and over ``end <what> [body] [name] ;``."""
        while self.tok.kind != "eof":
            if self.kw("end") and self.peek(1).kind == "kw" and self.peek(1).val == what:
                self.adv()
                self.adv()
                self.accept_kw("body")
                if self.tok.kind in ("id", "xid"):
                    self.adv()
                self.accept_op(";")
                return
            if self.at_unit_start():
                return
            self.adv()

    def parse_record_field(self):
        t = self.tok
        names = [(t.line, t.col, self.expect_ident())]
        while self.accept_op(","):
            names.append((self.tok.line, self.tok.col, self.expect_ident()))
        self.expect_op(":")
        st = self.parse_subtype_ind()
        self.semi()
        return [N.RecordField(n, st, line=l, col=c) for (l, c, n) in names]

    def parse_object_decl(self):
        t = self.tok
        shared = False
        if t.val == "shared":
            self.adv()
            shared = True
            cls = "variable"
            self.expect_kw("variable")
        else:
            cls = self.adv().val
        names = [(self.tok.line, self.tok.col, self.expect_ident())]
        while self.accept_op(","):
            names.append((self.tok.line, self.tok.col, self.expect_ident()))
        self.expect_op(":")
        st = self.parse_subtype_ind()
        if self.kw("register", "bus"):
            self.adv()
        init = None
        if self.accept_op(":="):
            init = self.parse_expr()
        self.semi()
        if shared and isinstance(st.mark, N.Ident) and st.mark.name in self.protected_types:
            self.info_skip("shared variable of protected type", t)
        return [N.ObjectDecl(cls, n, st, init, shared, line=l, col=c) for (l, c, n) in names]

    def parse_alias(self):
        t = self.expect_kw("alias")
        tk = self.tok
        if tk.kind == "str" or tk.kind == "char":
            name = self.adv().val
        else:
            name = self.expect_ident()
        st = None
        if self.accept_op(":"):
            st = self.parse_subtype_ind()
        self.expect_kw("is")
        target = self.parse_name()
        if self.op("["):  # signature
            while not self.op("]") and self.tok.kind != "eof":
                self.adv()
            self.accept_op("]")
        self.semi()
        return N.AliasDecl(name, st, target, line=t.line, col=t.col)

    def parse_component(self):
        t = self.expect_kw("component")
        name = self.expect_ident()
        self.accept_kw("is")
        c = N.ComponentDecl(name, [], [], line=t.line, col=t.col)
        self.open.append("component")
        try:
            if self.kw("generic"):
                c.generics = self.parse_generic_clause()
            if self.kw("port"):
                c.ports = self.parse_port_clause()
        finally:
            self.open.pop()
        self.finish("component", name)
        return c

    def parse_attribute(self):
        t = self.expect_kw("attribute")
        name = self.expect_ident()
        if self.accept_op(":"):
            tm = self.parse_type_mark()
            self.semi()
            return N.AttributeDecl(name, tm, line=t.line, col=t.col)
        self.expect_kw("of")
        targets = []
        while True:
            x = self.tok
            if x.kind == "kw" and x.val in ("others", "all"):
                targets.append(self.adv().val)
            elif x.kind in ("id", "xid", "str", "char"):
                targets.append(self.adv().val)
                if self.op("["):
                    while not self.op("]") and self.tok.kind != "eof":
                        self.adv()
                    self.accept_op("]")
            else:
                self.err("expected an attribute target")
            if not self.accept_op(","):
                break
        self.expect_op(":")
        cls = self.adv().val
        self.expect_kw("is")
        value = self.parse_expr()
        self.semi()
        if name not in SYNTH_ATTRS:
            self.info_skip("attribute specification '%s' (not synthesis-related)" % name, t)
        return N.AttributeSpec(name, targets, cls, value, line=t.line, col=t.col)

    def parse_subprogram(self):
        t = self.tok
        pure = True
        if t.val in ("pure", "impure"):
            pure = t.val == "pure"
            self.adv()
        kind = self.tok.val
        if kind not in ("function", "procedure"):
            self.err("expected 'function' or 'procedure'")
        self.adv()
        tn = self.tok
        if tn.kind in ("id", "xid", "str"):
            name = self.adv().val
            if tn.kind == "str":
                name = '"%s"' % name.lower()
        else:
            self.err("expected a subprogram name but found " + self.describe(tn))
        sp = N.Subprogram(kind, name, pure, [], None, [], None, False, line=t.line, col=t.col)
        try:
            if self.kw("generic"):
                self.unsupported("subprogram generics", tn)
                self.adv()
                self._skip_parens()
                if self.kw("generic"):
                    self.adv()
                    self.expect_kw("map")
                    self._skip_parens()
            if self.is_id("parameter"):
                self.adv()
            if self.op("("):
                sp.params = self.parse_interface_list("param")
            if kind == "function":
                self.expect_kw("return")
                sp.return_type = self.parse_type_mark()
        except ParseError as e:
            self.record(e)
            if self.cur_unit is not None:
                self.cur_unit.incomplete = True
            while self.tok.kind != "eof" and not self.kw("is") and not self.op(";") and not self.at_term(()):
                self.adv()
        if self.accept_op(";"):
            return sp
        self.expect_kw("is")
        if self.kw("new"):
            self.unsupported("subprogram instantiation", tn)
            self.sync(())
            return N.Skipped("subprogram_inst", name, line=t.line, col=t.col)
        sp.has_body = True
        sp.body = []
        self.open.append(kind)
        try:
            self.parse_list(self.parse_decl, ("begin",), sp.decls)
            if self.accept_kw("begin"):
                self.parse_list(self.parse_seq_stmt, (), sp.body)
            else:
                self.soft_err("expected 'begin' in %s '%s'" % (kind, name))
        finally:
            self.open.pop()
        self.finish(kind, name)
        return sp

    def _skip_parens(self):
        if not self.op("("):
            return
        depth = 0
        while self.tok.kind != "eof":
            if self.op("("):
                depth += 1
            elif self.op(")"):
                depth -= 1
                if depth == 0:
                    self.adv()
                    return
            self.adv()

    # ------------------------------------------------------------------ concurrent statements
    def parse_map_list(self):
        self.expect_op("(")
        self.enter()
        try:
            items = []
            while True:
                items.append(self.parse_assoc_item())
                if self.accept_op(","):
                    continue
                self.expect_op(")")
                break
        finally:
            self.leave()
        return items

    def parse_label(self):
        t = self.tok
        if t.kind in ("id", "xid") and self.peek(1).kind == "op" and self.peek(1).val == ":":
            self.adv()
            self.adv()
            return t.val
        return None

    def parse_concurrent_stmt(self):
        t0 = self.tok
        label = self.parse_label()
        t = self.tok
        postponed = False
        if t.kind == "kw" and t.val == "postponed":
            self.adv()
            postponed = True
            t = self.tok
        k = t.kind
        if k == "kw":
            v = t.val
            if v == "process":
                return self.parse_process(label, postponed)
            if v == "block":
                return self.parse_block(label)
            if v == "for":
                return self.parse_for_generate(label)
            if v == "if":
                return self.parse_if_generate(label)
            if v == "case":
                return self.parse_case_generate(label)
            if v == "with":
                return self.parse_selected(label, True)
            if v == "assert":
                self.info_skip("concurrent assertion", t)
                self.sync(())
                return N.Skipped("assert", None, line=t.line, col=t.col)
            if v in ("component", "entity", "configuration"):
                return self.parse_instance(label, None)
            if v == "end":
                self.err("unexpected 'end'")
        elif k in ("id", "xid") and label is None:
            # PSL directives
            if t.val in PSL_WORDS and self.peek(1).kind in ("id", "kw") or \
                    (t.val == "default" and self.peek(1).kind in ("id", "kw")):
                self.info_skip("PSL directive", t)
                self.sync(())
                return N.Skipped("psl", None, line=t.line, col=t.col)
        if k not in ("id", "xid") and not (k == "op" and t.val == "("):
            self.err("unexpected %s in concurrent statement" % self.describe(t))
        target = self.parse_target()
        if self.kw("generic", "port") and isinstance(target, (N.Ident, N.Select)):
            return self.parse_instance(label, target)
        if self.op("<="):
            self.adv()
            return self.finish_assign(label, target, "signal", t, postponed)
        if self.op(":="):
            self.err("variable assignment is not allowed in a concurrent statement")
        if self.op(";"):
            self.adv()
            return N.ProcCall(label, target, line=t.line, col=t.col)
        self.err("unexpected %s in concurrent statement" % self.describe(self.tok))

    def skip_assign_opts(self):
        guarded = False
        while True:
            if self.kw("guarded"):
                self.adv()
                guarded = True
            elif self.kw("transport", "inertial"):
                self.adv()
            elif self.kw("reject"):
                self.adv()
                self.parse_expr()
                self.expect_kw("inertial")
            elif self.is_id("force") and self.peek(1).kind in ("id", "int", "char", "str", "bitstr", "based", "kw", "op") \
                    and not (self.peek(1).kind == "op" and self.peek(1).val not in ("(", "-", "+")) \
                    and not (self.peek(1).kind == "kw" and self.peek(1).val not in ("in", "out", "not", "abs")):
                self.adv()
                if self.kw("in", "out"):
                    self.adv()
            elif self.is_id("release") and self.peek(1).kind == "op" and self.peek(1).val == ";":
                self.adv()
                break
            else:
                break
        return guarded

    def parse_waveform(self):
        if self.kw("unaffected"):
            t = self.adv()
            return [N.Unaffected(line=t.line, col=t.col)]
        vals = []
        while True:
            vals.append(self.parse_expr())
            if self.kw("after"):
                a = self.adv()
                self.parse_expr()
                self.info_skip("'after' clause", a)
            if not self.accept_op(","):
                break
        return vals

    def parse_cond_alts(self):
        alts = []
        while True:
            st = self.tok
            vals = self.parse_waveform()
            if self.kw("when"):
                self.adv()
                cond = self.parse_expr()
                alts.append(N.Alt(vals, cond, line=st.line, col=st.col))
                if self.accept_kw("else"):
                    continue
                break
            alts.append(N.Alt(vals, None, line=st.line, col=st.col))
            break
        return alts

    def finish_assign(self, label, target, kind, t, postponed=False):
        guarded = self.skip_assign_opts()
        if self.tok.kind == "op" and self.tok.val == ";" and self.toks[self.i - 1].kind == "id":
            # "release;" style
            self.adv()
            return N.SignalAssign(label, target, [], kind, guarded, line=t.line, col=t.col)
        alts = self.parse_cond_alts()
        self.semi()
        return N.SignalAssign(label, target, alts, kind, guarded, line=t.line, col=t.col)

    def parse_selected(self, label, concurrent):
        t = self.expect_kw("with")
        sel = self.parse_expr()
        self.expect_kw("select")
        matching = False
        if self.op("?"):
            self.adv()
            matching = True
        target = self.parse_target()
        if self.accept_op("<="):
            kind = "signal"
        elif self.accept_op(":="):
            kind = "variable"
        else:
            self.err("expected '<=' or ':=' in selected assignment")
        guarded = self.skip_assign_opts()
        alts = []
        while True:
            st = self.tok
            vals = self.parse_waveform()
            self.expect_kw("when")
            choices = self.parse_choices()
            alts.append(N.SelAlt(vals, choices, line=st.line, col=st.col))
            if self.accept_op(","):
                continue
            break
        self.semi()
        return N.SelectAssign(label, sel, matching, target, alts, kind, guarded, line=t.line, col=t.col)

    def parse_instance(self, label, target):
        t = self.tok
        kind = "component"
        arch = None
        if target is not None:
            unit = self.name_tuple(target)
        else:
            kw = self.adv().val
            kind = kw
            parts = [self.expect_ident()]
            while self.accept_op("."):
                parts.append(self.expect_ident())
            unit = tuple(parts)
            if kw == "entity" and self.op("("):
                self.adv()
                arch = self.expect_ident()
                self.expect_op(")")
        gm: list = []
        pm: list = []
        if self.kw("generic"):
            self.adv()
            self.expect_kw("map")
            gm = self.parse_map_list()
        if self.kw("port"):
            self.adv()
            self.expect_kw("map")
            pm = self.parse_map_list()
        self.semi()
        return N.Instance(label, kind, unit, arch, gm, pm, line=t.line, col=t.col)

    @staticmethod
    def name_tuple(e):
        parts = []
        while isinstance(e, N.Select):
            parts.append(e.suffix)
            e = e.prefix
        if isinstance(e, N.Ident):
            parts.append(e.name)
        parts.reverse()
        return tuple(parts)

    def parse_process(self, label, postponed):
        t = self.expect_kw("process")
        sens = None
        if self.op("("):
            self.adv()
            if self.kw("all"):
                self.adv()
                sens = "all"
            else:
                sens = []
                while True:
                    sens.append(self.parse_name())
                    if not self.accept_op(","):
                        break
            self.expect_op(")")
        self.accept_kw("is")
        p = N.ProcessStmt(label, sens, [], [], postponed, False, line=t.line, col=t.col)
        saved = self._pf
        self._pf = {"wait_for": False, "report": False, "io": False, "edge_wait": False, "bare_wait": False,
                    "any_wait": False, "edge": False}
        self.open.append("process")
        try:
            self.parse_list(self.parse_decl, ("begin",), p.decls)
            if self.accept_kw("begin"):
                self.parse_list(self.parse_seq_stmt, (), p.body)
            else:
                self.soft_err("expected 'begin' in process")
        finally:
            self.open.pop()
            pf = self._pf
            self._pf = saved
        if self.kw("end") and self.peek(1).kind == "kw" and self.peek(1).val == "postponed":
            self.adv()
            self.adv()
            self.accept_kw("process")
            if self.tok.kind in ("id", "xid"):
                self.adv()
            if not self.accept_op(";"):
                self.soft_err("expected ';' after 'end postponed process'")
        else:
            self.finish("process", label)
        if sens is None and not pf["edge_wait"] and not pf["edge"] and (
                pf["wait_for"] or pf["bare_wait"] or pf["report"] or pf["io"]):
            p.sim_only = True
            self._add("SIMULATION_ONLY_PROCESS",
                      "process%s looks simulation-only (wait for/report/file I/O, no clock); kept in the AST"
                      % ((" '%s'" % label) if label else ""), "info", False, t.line, t.col)
        return p

    def parse_decl_region(self, decls, allow_begin=True):
        """Declarations of a block / generate body, up to ``begin`` (consumed) or the first statement."""
        while self.tok.kind == "kw" and self.tok.val in DECL_START and self.tok.val != "package" \
                or (self.tok.kind == "kw" and self.tok.val == "package" and not self.at_unit_start()):
            start = self.i
            try:
                r = self.parse_decl()
                if isinstance(r, list):
                    decls.extend(r)
                elif r is not None:
                    decls.append(r)
            except ParseError as e:
                self.record(e)
                self.sync(("begin",))
            if self.i == start:
                self.adv()
        if allow_begin:
            self.accept_kw("begin")

    def parse_block(self, label):
        t = self.expect_kw("block")
        if label is None:
            self.soft_err("block statement without a label")
        b = N.BlockStmt(label, None, [], [], [], [], [], [], line=t.line, col=t.col)
        if self.op("("):
            self.adv()
            b.guard = self.parse_expr()
            self.expect_op(")")
        self.accept_kw("is")
        self.open.append("block")
        try:
            if self.kw("generic"):
                b.generics = self.parse_generic_clause()
                if self.kw("generic"):
                    self.adv()
                    self.expect_kw("map")
                    b.generic_map = self.parse_map_list()
                    self.semi()
            if self.kw("port"):
                b.ports = self.parse_port_clause()
                if self.kw("port"):
                    self.adv()
                    self.expect_kw("map")
                    b.port_map = self.parse_map_list()
                    self.semi()
            self.parse_list(self.parse_decl, ("begin",), b.decls)
            if self.accept_kw("begin"):
                self.parse_list(self.parse_concurrent_stmt, (), b.stmts)
            else:
                self.soft_err("expected 'begin' in block")
        finally:
            self.open.pop()
        self.finish("block", label)
        return b

    def parse_gen_body(self, decls, stmts):
        self.parse_decl_region(decls)
        self.parse_list(self.parse_concurrent_stmt, ("elsif", "else", "when"), stmts)
        # VHDL-2008 branch terminator: "end [label];" (not "end generate")
        if self.kw("end"):
            nx = self.peek(1)
            if (nx.kind == "op" and nx.val == ";") or (nx.kind in ("id", "xid") and self.peek(2).kind == "op"
                                                       and self.peek(2).val == ";"):
                self.adv()
                if self.tok.kind in ("id", "xid"):
                    self.adv()
                self.adv()

    def parse_for_generate(self, label):
        t = self.expect_kw("for")
        var = self.expect_ident()
        self.expect_kw("in")
        self.enter()
        g = N.ForGenerate(label, var, None, [], [], line=t.line, col=t.col)
        self.open.append("generate")
        try:
            try:
                g.range = self.parse_discrete_range()
                self.expect_kw("generate")
            except ParseError as e:
                self.record(e)
                self.skip_to_kw("generate")
            self.parse_gen_body(g.decls, g.stmts)
        finally:
            self.open.pop()
            self.leave()
        self.finish("generate", label)
        return g

    def skip_to_kw(self, v):
        while self.tok.kind != "eof" and not self.kw(v) and not self.op(";") and not self.at_term(()):
            self.adv()
        self.accept_kw(v)

    def parse_if_generate(self, label):
        t = self.expect_kw("if")
        g = N.IfGenerate(label, [], None, line=t.line, col=t.col)
        self.open.append("generate")
        self.enter()
        try:
            first = True
            while True:
                bt = self.tok
                blabel = None
                if self.tok.kind in ("id", "xid") and self.peek(1).kind == "op" and self.peek(1).val == ":":
                    blabel = self.adv().val
                    self.adv()
                cond = None
                try:
                    cond = self.parse_expr()
                    self.expect_kw("generate")
                except ParseError as e:
                    self.record(e)
                    self.skip_to_kw("generate")
                br = N.GenBranch(blabel, cond, [], [], line=bt.line, col=bt.col)
                self.parse_gen_body(br.decls, br.stmts)
                g.branches.append(br)
                if self.kw("elsif"):
                    self.adv()
                    continue
                if self.kw("else"):
                    et = self.adv()
                    elabel = None
                    if self.tok.kind in ("id", "xid") and self.peek(1).kind == "op" and self.peek(1).val == ":":
                        elabel = self.adv().val
                        self.adv()
                    self.expect_kw("generate")
                    eb = N.GenBranch(elabel, None, [], [], line=et.line, col=et.col)
                    self.parse_gen_body(eb.decls, eb.stmts)
                    g.else_branch = eb
                break
        finally:
            self.open.pop()
            self.leave()
        self.finish("generate", label)
        return g

    def parse_case_generate(self, label):
        t = self.expect_kw("case")
        g = N.CaseGenerate(label, None, [], line=t.line, col=t.col)
        self.open.append("generate")
        self.enter()
        try:
            try:
                g.selector = self.parse_expr()
                self.expect_kw("generate")
            except ParseError as e:
                self.record(e)
                self.skip_to_kw("generate")
            while self.kw("when"):
                wt = self.adv()
                alabel = None
                if self.tok.kind in ("id", "xid") and self.peek(1).kind == "op" and self.peek(1).val == ":":
                    alabel = self.adv().val
                    self.adv()
                try:
                    choices = self.parse_choices()
                    self.expect_op("=>")
                except ParseError as e:
                    self.record(e)
                    choices = []
                    while self.tok.kind != "eof" and not self.op("=>") and not self.op(";") and not self.at_term(()):
                        self.adv()
                    self.accept_op("=>")
                alt = N.GenAlt(alabel, choices, [], [], line=wt.line, col=wt.col)
                self.parse_gen_body(alt.decls, alt.stmts)
                g.alts.append(alt)
        finally:
            self.open.pop()
            self.leave()
        self.finish("generate", label)
        return g

    # ------------------------------------------------------------------ sequential statements
    SEQ_TERMS = ("else", "elsif", "when")

    def parse_seq_stmt(self):
        t0 = self.tok
        label = self.parse_label()
        t = self.tok
        k = t.kind
        if k == "kw":
            v = t.val
            if v == "if":
                return self.parse_if(label)
            if v == "case":
                return self.parse_case(label)
            if v == "for":
                return self.parse_for_loop(label)
            if v == "while":
                return self.parse_while(label)
            if v == "loop":
                return self.parse_plain_loop(label)
            if v == "next" or v == "exit":
                self.adv()
                ll = None
                if self.tok.kind in ("id", "xid"):
                    ll = self.adv().val
                cond = None
                if self.accept_kw("when"):
                    cond = self.parse_expr()
                self.semi()
                cls = N.NextStmt if v == "next" else N.ExitStmt
                return cls(label, ll, cond, line=t.line, col=t.col)
            if v == "return":
                self.adv()
                val = None
                if not self.op(";"):
                    val = self.parse_expr()
                self.semi()
                return N.ReturnStmt(label, val, line=t.line, col=t.col)
            if v == "null":
                self.adv()
                self.semi()
                return N.NullStmt(label, line=t.line, col=t.col)
            if v == "wait":
                return self.parse_wait(label)
            if v == "assert":
                self.info_skip("assertion", t)
                self.sync(())
                return N.Skipped("assert", None, line=t.line, col=t.col)
            if v == "report":
                self.info_skip("report statement", t)
                if self._pf is not None:
                    self._pf["report"] = True
                self.sync(())
                return N.Skipped("report", None, line=t.line, col=t.col)
            if v == "with":
                return self.parse_selected(label, False)
            self.err("unexpected %s in sequential statement" % self.describe(t))
        if k not in ("id", "xid") and not (k == "op" and t.val == "("):
            self.err("unexpected %s in sequential statement" % self.describe(t))
        target = self.parse_target()
        if self.op("<="):
            self.adv()
            return self.finish_assign(label, target, "signal", t)
        if self.op(":="):
            self.adv()
            alts = self.parse_cond_alts()
            self.semi()
            return N.SignalAssign(label, target, alts, "variable", False, line=t.line, col=t.col)
        if self.op(";"):
            self.adv()
            if self._pf is not None:
                nm = target.prefix if isinstance(target, N.Apply) else target
                nm = nm.suffix if isinstance(nm, N.Select) else getattr(nm, "name", "")
                if nm in FILE_IO_CALLS:
                    self._pf["io"] = True
                    self.info_skip("file I/O / simulation call '%s'" % nm, t)
            return N.ProcCall(label, target, line=t.line, col=t.col)
        self.err("unexpected %s in sequential statement" % self.describe(self.tok))

    def cond_until(self, kw):
        """Parse a condition followed by ``kw``; on error skip to ``kw``."""
        try:
            c = self.parse_expr()
            self.expect_kw(kw)
            return c
        except ParseError as e:
            self.record(e)
            while self.tok.kind != "eof" and not self.kw(kw) and not self.op(";") and not self.at_term(self.SEQ_TERMS):
                self.adv()
            self.accept_kw(kw)
            return None

    def parse_if(self, label):
        t = self.expect_kw("if")
        self.enter()
        s = N.IfStmt(label, [], None, line=t.line, col=t.col)
        self.open.append("if")
        try:
            ct = self.tok
            cond = self.cond_until("then")
            body = self.parse_list(self.parse_seq_stmt, ("else", "elsif"))
            s.branches.append(N.IfBranch(cond, body, line=ct.line, col=ct.col))
            while self.kw("elsif"):
                et = self.adv()
                cond = self.cond_until("then")
                body = self.parse_list(self.parse_seq_stmt, ("else", "elsif"))
                s.branches.append(N.IfBranch(cond, body, line=et.line, col=et.col))
            if self.accept_kw("else"):
                s.else_body = self.parse_list(self.parse_seq_stmt, ())
        finally:
            self.open.pop()
            self.leave()
        self.finish("if", label)
        return s

    def parse_case(self, label):
        t = self.expect_kw("case")
        self.enter()
        matching = False
        if self.op("?"):
            self.adv()
            matching = True
        s = N.CaseStmt(label, None, matching, [], line=t.line, col=t.col)
        self.open.append("case")
        try:
            s.selector = self.cond_until("is")
            # tolerate stray statements before the first alternative
            while not self.kw("when") and not self.at_term(()):
                start = self.i
                self.record(ParseError("expected 'when' in case statement", self.tok))
                self.sync(("when",))
                if self.i == start:
                    self.adv()
            while self.kw("when"):
                wt = self.adv()
                try:
                    choices = self.parse_choices()
                    self.expect_op("=>")
                except ParseError as e:
                    self.record(e)
                    choices = []
                    while self.tok.kind != "eof" and not self.op("=>") and not self.op(";") and not self.at_term(self.SEQ_TERMS):
                        self.adv()
                    self.accept_op("=>")
                body = self.parse_list(self.parse_seq_stmt, ("when",))
                s.alts.append(N.CaseAlt(choices, body, line=wt.line, col=wt.col))
        finally:
            self.open.pop()
            self.leave()
        self.finish("case", label)
        return s

    def _end_loop(self, label):
        self.finish("loop", label)

    def parse_for_loop(self, label):
        t = self.expect_kw("for")
        self.enter()
        s = N.ForLoop(label, None, None, [], line=t.line, col=t.col)
        self.open.append("loop")
        try:
            try:
                s.var = self.expect_ident()
                self.expect_kw("in")
                s.range = self.parse_discrete_range()
                self.expect_kw("loop")
            except ParseError as e:
                self.record(e)
                self.skip_to_kw("loop")
            self.parse_list(self.parse_seq_stmt, (), s.body)
        finally:
            self.open.pop()
            self.leave()
        self._end_loop(label)
        return s

    def parse_while(self, label):
        t = self.expect_kw("while")
        self.enter()
        s = N.WhileLoop(label, None, [], line=t.line, col=t.col)
        self.open.append("loop")
        try:
            s.cond = self.cond_until("loop")
            self.parse_list(self.parse_seq_stmt, (), s.body)
        finally:
            self.open.pop()
            self.leave()
        self._end_loop(label)
        return s

    def parse_plain_loop(self, label):
        t = self.expect_kw("loop")
        self.enter()
        s = N.LoopStmt(label, [], line=t.line, col=t.col)
        self.open.append("loop")
        try:
            self.parse_list(self.parse_seq_stmt, (), s.body)
        finally:
            self.open.pop()
            self.leave()
        self._end_loop(label)
        return s

    def parse_wait(self, label):
        t = self.expect_kw("wait")
        on = None
        until = None
        timeout = None
        pf = self._pf
        if self.accept_kw("on"):
            on = []
            while True:
                on.append(self.parse_name())
                if not self.accept_op(","):
                    break
        if self.accept_kw("until"):
            until = self.parse_expr()
        if self.kw("for"):
            f = self.adv()
            timeout = self.parse_expr()
            self.info_skip("'wait for' delay", f)
            if pf is not None:
                pf["wait_for"] = True
        self.semi()
        if pf is not None:
            pf["any_wait"] = True
            if until is not None and _mentions_edge(until):
                pf["edge_wait"] = True
            elif on is None and until is None and timeout is None:
                pf["bare_wait"] = True
        return N.WaitStmt(label, on, until, timeout, line=t.line, col=t.col)


def _mentions_edge(e):
    stack = [e]
    while stack:
        x = stack.pop()
        if isinstance(x, N.Attr) and x.name == "event":
            return True
        if isinstance(x, N.Apply) and isinstance(x.prefix, N.Ident) and x.prefix.name in ("rising_edge", "falling_edge"):
            return True
        stack.extend(x.children())
    return False
