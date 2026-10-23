"""Concurrent statements: processes, assignments, instances, generate, blocks."""
from __future__ import annotations

from ...frontend.vhdl import nodes as N
from ...frontend.vhdl.nodes import walk
from ...ir import Design, LowerBlock, Origin, SigSpec
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, StopLimit, Unsupported, int_val
from .stmt import XC
from .values import Scope, Sym, Val


def base_names(e):
    """Identifier names an assignment target ultimately writes."""
    out = []
    stack = [e]
    while stack:
        n = stack.pop()
        if isinstance(n, N.Ident):
            out.append(n.name)
        elif isinstance(n, (N.Apply, N.Slice, N.Select, N.Attr)):
            stack.append(n.prefix)
        elif isinstance(n, N.Aggregate):
            stack.extend(it.value for it in n.items)
    return out


def call_name(e):
    if isinstance(e, N.Apply):
        p = e.prefix
        if isinstance(p, N.Ident):
            return p.name
        if isinstance(p, N.Select):
            return p.suffix
    return None


def process_is_clocked(p):
    for n in walk(p):
        if isinstance(n, N.Apply) and call_name(n) in ("rising_edge", "falling_edge"):
            return True
        if isinstance(n, N.Attr) and n.name == "event":
            return True
        if isinstance(n, N.WaitStmt) and n.until is not None:
            return True
    return False


def expr_key(e):
    """Canonical text of a target expression (for matching branch targets)."""
    if isinstance(e, N.Ident):
        return e.name
    if isinstance(e, N.Literal):
        return repr(e.value)
    if isinstance(e, N.Select):
        return expr_key(e.prefix) + "." + e.suffix
    if isinstance(e, N.Apply):
        return expr_key(e.prefix) + "(" + ",".join(expr_key(a.actual) for a in e.args) + ")"
    if isinstance(e, N.Slice):
        return expr_key(e.prefix) + "[" + expr_key(e.range) + "]"
    if isinstance(e, N.Range):
        return expr_key(e.left) + e.direction + expr_key(e.right)
    if isinstance(e, N.Binary):
        return "(%s%s%s)" % (expr_key(e.left), e.op, expr_key(e.right))
    if isinstance(e, N.Unary):
        return "(%s%s)" % (e.op, expr_key(e.operand))
    return type(e).__name__ + str(id(e))


def is_z_expr(e):
    if isinstance(e, N.Literal) and e.kind == "char":
        return str(e.value).upper() == "Z"
    if isinstance(e, N.Literal) and e.kind == "str":
        return bool(e.value) and all(c.upper() == "Z" for c in e.value)
    if isinstance(e, N.Aggregate):
        return bool(e.items) and all(is_z_expr(it.value) for it in e.items)
    return False


class ConcMixin:
    # ------------------------------------------------------------------ prescan
    def prescan(self, units):
        """Find state registers, one-hot candidates and memories from the syntax."""
        hw = set()
        procs = []
        for u in units:
            for g in (getattr(u, "ports", None) or ()):
                hw.add(g.name)
            for n in walk(u):
                if isinstance(n, N.ObjectDecl) and n.cls in ("signal", "variable"):
                    hw.add(n.name)
                elif isinstance(n, N.Port):
                    hw.add(n.name)
                elif isinstance(n, N.Param):
                    hw.add(n.name)
                elif isinstance(n, N.ProcessStmt):
                    procs.append(n)
        decl_type = {}
        for u in units:
            for n in walk(u):
                if isinstance(n, N.ObjectDecl) and n.cls in ("signal", "variable") and n.subtype is not None:
                    m = n.subtype.mark if isinstance(n.subtype, N.SubtypeInd) else n.subtype
                    fl = self.flat_name(m) if m is not None else None
                    if fl:
                        decl_type[n.name] = fl[-1]
        state = set()
        mems = set()
        for p in procs:
            if p.sim_only or not process_is_clocked(p):
                continue
            for n in walk(p):
                if isinstance(n, (N.SignalAssign, N.SelectAssign)):
                    for nm in base_names(n.target):
                        state.add(nm)
                    tgt = n.target
                    if isinstance(tgt, N.Apply) and isinstance(tgt.prefix, N.Ident):
                        for a in tgt.args:
                            if any(isinstance(x, N.Ident) and x.name in hw for x in walk(a.actual)):
                                mems.add(tgt.prefix.name)
                    # a(i)(j) : base of nested apply
                    t = tgt
                    while isinstance(t, (N.Apply, N.Select, N.Slice)):
                        if isinstance(t, N.Apply) and isinstance(t.prefix, N.Ident):
                            if any(isinstance(x, N.Ident) and x.name in hw for a in t.args for x in walk(a.actual)):
                                mems.add(t.prefix.name)
                        t = t.prefix
        self.state_names = state
        self.mem_names = mems
        self.onehot_types = set()
        if self.ctx.fsm_encoding == "one_hot":
            for nm in state:
                tn = decl_type.get(nm)
                if tn:
                    self.onehot_types.add(tn)

    # ------------------------------------------------------------------ statement dispatch
    def elab_stmts(self, stmts, sc):
        for s in stmts or ():
            try:
                self.elab_stmt(s, sc)
            except StopLimit:
                raise
            except RecursionError:
                self.diag("LIMIT_REACHED", "expression nesting too deep; statement skipped", s)
            except Exception as ex:
                if getattr(self.ctx, "debug", False):
                    raise
                self.diag("UNSUPPORTED_CONSTRUCT", "concurrent statement skipped (%s: %s)" % (type(ex).__name__, ex), s)

    def elab_stmt(self, s, sc):
        k = type(s)
        saved = self.b.origin
        self.b.origin = self.org(s)
        try:
            if k is N.SignalAssign or k is N.SelectAssign:
                self.conc_assign(s, sc)
            elif k is N.ProcessStmt:
                self.elab_process(s, sc)
            elif k is N.Instance:
                self.elab_instance(s, sc)
            elif k is N.ForGenerate:
                self.gen_for(s, sc)
            elif k is N.IfGenerate:
                self.gen_if(s, sc)
            elif k is N.CaseGenerate:
                self.gen_case(s, sc)
            elif k is N.BlockStmt:
                self.elab_block(s, sc)
            elif k is N.ProcCall:
                p = N.ProcessStmt(s.label, "all", [], [s], False, False, line=s.line, col=s.col)
                self.elab_process(p, sc)
            elif k is N.Skipped:
                pass
        finally:
            self.b.origin = saved

    # ------------------------------------------------------------------ concurrent assignment
    def conc_assign(self, s, sc):
        if isinstance(s, N.SignalAssign) and len(s.alts) == 1 and s.alts[0].cond is None:
            lv = self.lvalue(s.target, sc)
            if lv.parts is None and lv.mem is None and not lv.dyn:
                if isinstance(s.alts[0].values[0], N.Unaffected):
                    return
                v = self.eval(s.alts[0].values[0], sc, lv.t)
                v = self.fit(v, lv.t, s) if lv.t is not None else v
                sg = self.sig_of(v)
                if sg.width != lv.width:
                    sg = sg.resize(lv.width, False)
                self.b.connect(self.lv_static_sig(lv), sg, self.org(s))
                return
        if isinstance(s, N.SignalAssign) and len(s.alts) > 1 and s.alts[-1].cond is None:
            lv = self.lvalue(s.target, sc)
            if lv.parts is None and lv.mem is None and not lv.dyn:
                vals = [self.eval(a.values[0], sc, lv.t) for a in s.alts]
                res = vals[-1]
                for a, v in reversed(list(zip(s.alts[:-1], vals[:-1]))):
                    res = self.cond_mux(self.cond_bit(self.eval(a.cond, sc)), res, v, s)
                res = self.fit(res, lv.t, s) if lv.t is not None else res
                sg = self.sig_of(res)
                if sg.width != lv.width:
                    sg = sg.resize(lv.width, False)
                self.b.connect(self.lv_static_sig(lv), sg, self.org(s))
                return
        blk = LowerBlock(self.b, "comb", origin=self.org(s))
        xc = XC(blk, False, None, "concurrent")
        saved = self.proc
        self.proc = xc
        try:
            self.run_stmt(xc, s, sc)
            blk.finish()
        finally:
            self.proc = saved

    # ------------------------------------------------------------------ processes
    def split_and(self, c):
        out = []
        stack = [c]
        while stack:
            n = stack.pop()
            if isinstance(n, N.Binary) and n.op == "and":
                stack.append(n.right)
                stack.append(n.left)
            else:
                out.append(n)
        return out

    def parse_edge(self, cond):
        """(clock expr node, 'pos'|'neg', remaining terms) if ``cond`` contains a clock edge, else None."""
        terms = self.split_and(cond)
        edge = None
        rest = []
        used = set()
        for i, t in enumerate(terms):
            nm = call_name(t)
            if nm in ("rising_edge", "falling_edge") and t.args:
                edge = (t.args[0].actual, "pos" if nm == "rising_edge" else "neg")
                used.add(i)
                break
        if edge is None:
            ev = None
            for i, t in enumerate(terms):
                if isinstance(t, N.Attr) and t.name == "event":
                    ev = (i, t.prefix)
                    break
            if ev is not None:
                i, clk = ev
                key = expr_key(clk)
                for j, t in enumerate(terms):
                    if j == i or not isinstance(t, N.Binary) or t.op != "=":
                        continue
                    for a, b in ((t.left, t.right), (t.right, t.left)):
                        if expr_key(a) == key and isinstance(b, N.Literal) and b.kind == "char" and b.value in "01":
                            edge = (clk, "pos" if b.value == "1" else "neg")
                            used.update((i, j))
                if edge is None:
                    # `not clk'stable and clk = '1'`
                    pass
            if edge is None:
                for i, t in enumerate(terms):
                    if isinstance(t, N.Unary) and t.op == "not" and isinstance(t.operand, N.Attr) and t.operand.name == "stable":
                        clk = t.operand.prefix
                        key = expr_key(clk)
                        for j, u in enumerate(terms):
                            if isinstance(u, N.Binary) and u.op == "=":
                                for a, b in ((u.left, u.right), (u.right, u.left)):
                                    if expr_key(a) == key and isinstance(b, N.Literal) and b.kind == "char" and b.value in "01":
                                        edge = (clk, "pos" if b.value == "1" else "neg")
                                        used.update((i, j))
        if edge is None:
            return None
        rest = [t for i, t in enumerate(terms) if i not in used]
        return edge[0], edge[1], rest

    def reset_term(self, cond, sc):
        """(signal SigSpec, active level) for simple asynchronous reset conditions."""
        try:
            if isinstance(cond, N.Binary) and cond.op == "=":
                for a, b in ((cond.left, cond.right), (cond.right, cond.left)):
                    if isinstance(b, N.Literal) and b.kind == "char" and b.value in "01" and isinstance(a, (N.Ident, N.Select, N.Apply)):
                        v = self.eval(a, sc)
                        s = self.sig_of(v)
                        if s.width == 1:
                            return s, int(b.value)
            elif isinstance(cond, N.Ident):
                s = self.sig_of(self.eval(cond, sc))
                if s.width == 1:
                    return s, 1
            elif isinstance(cond, N.Unary) and cond.op == "not" and isinstance(cond.operand, N.Ident):
                s = self.sig_of(self.eval(cond.operand, sc))
                if s.width == 1:
                    return s, 0
        except Exception:
            pass
        return None

    def classify_process(self, p, sc):
        """('clocked', clk_node, edge, resets[(branch)], body_stmts, rest_terms) or ('comb', ...)."""
        body = p.body or []
        if body and isinstance(body[0], N.WaitStmt) and body[0].until is not None:
            pe = self.parse_edge(body[0].until)
            if pe is not None:
                return ("clocked", pe[0], pe[1], [], body[1:], pe[2])
        for s in body:
            if isinstance(s, N.IfStmt):
                for k, br in enumerate(s.branches):
                    pe = self.parse_edge(br.cond)
                    if pe is not None:
                        return ("clocked", pe[0], pe[1], s.branches[:k], br.body, pe[2])
                break
        return ("comb", None, None, [], [x for x in body if not isinstance(x, N.WaitStmt)], [])

    def state_var_names(self, p):
        """Process variables that are read before being written on some path (registers)."""
        vars_ = {d.name for d in (p.decls or ()) if isinstance(d, N.ObjectDecl) and d.cls == "variable"}
        if not vars_:
            return set()
        state = set()

        def reads(e, defined):
            for n in walk(e):
                if isinstance(n, N.Ident) and n.name in vars_ and n.name not in defined:
                    state.add(n.name)

        def scan(stmts, defined):
            defined = set(defined)
            for s in stmts:
                if isinstance(s, N.SignalAssign):
                    for a in s.alts:
                        for v in a.values:
                            reads(v, defined)
                        if a.cond is not None:
                            reads(a.cond, defined)
                    tgt = s.target
                    if isinstance(tgt, N.Ident):
                        if tgt.name in vars_ and len(s.alts) == 1 and s.alts[0].cond is None:
                            defined.add(tgt.name)
                    else:
                        reads_idx = [x for x in walk(tgt) if x is not tgt]
                        for n in reads_idx:
                            if isinstance(n, N.Ident) and n.name in vars_ and n.name not in defined and n.name not in base_names(tgt):
                                state.add(n.name)
                        for nm in base_names(tgt):
                            if nm in vars_ and nm not in defined:
                                state.add(nm)       # partial write of an undefined variable: keeps old bits
                elif isinstance(s, N.IfStmt):
                    outs = []
                    for br in s.branches:
                        reads(br.cond, defined)
                        outs.append(scan(br.body, defined))
                    if s.else_body is not None:
                        outs.append(scan(s.else_body, defined))
                        defined = set.intersection(*outs) if outs else defined
                    else:
                        for o in outs:
                            pass
                elif isinstance(s, N.CaseStmt):
                    reads(s.selector, defined)
                    outs = [scan(a.body, defined) for a in s.alts]
                    has_others = any(isinstance(c, N.Others) for a in s.alts for c in a.choices)
                    if outs and has_others:
                        defined = set.intersection(*outs)
                elif isinstance(s, (N.ForLoop, N.WhileLoop, N.LoopStmt)):
                    if isinstance(s, N.ForLoop):
                        reads(s.range, defined)
                    scan(s.body, defined)
                else:
                    reads(s, defined)
            return defined

        scan(p.body or [], set())
        return state

    def elab_process(self, p, sc):
        if p.sim_only:
            self.diag("SIMULATION_ONLY_PROCESS", "process%s is simulation-only and skipped" % ((" '%s'" % p.label) if p.label else ""), p)
            return
        kind, clk_node, edge, resets, body, rest = self.classify_process(p, sc)
        if not any(isinstance(n, (N.SignalAssign, N.SelectAssign, N.ProcCall)) for n in walk(p)):
            return
        pscope = Scope(sc, label=p.label or "")
        clk_sig = None
        if kind == "clocked":
            cv = self.eval(clk_node, sc)
            clk_sig = self.sig_of(cv)
            if clk_sig.width != 1:
                clk_sig = clk_sig.slice(0, 1)
        async_resets = []
        for br in resets:
            r = self.reset_term(br.cond, sc)
            if r is not None:
                async_resets.append(r)
        if len(async_resets) != len(resets):
            async_resets = async_resets[:0] if not async_resets else async_resets
        blk = LowerBlock(self.b, "clocked" if kind == "clocked" else "comb", clock=clk_sig, clk_edge=edge or "pos",
                         async_resets=async_resets if kind == "clocked" else (), origin=self.org(p),
                         name=p.label)
        xc = XC(blk, kind == "clocked", None, "process")
        xc.state_vars = self.state_var_names(p) if kind == "clocked" else set()
        saved = self.proc
        self.proc = xc
        try:
            self.declare_items(p.decls, pscope, xc)

            def run_body():
                if rest:
                    c = None
                    for t in rest:
                        b_ = self.cond_bit(self.eval(t, sc))
                        c = b_ if c is None else self.b.and_(c, b_, 1)
                    blk.if_(c, lambda: self.run_stmts(xc, body, pscope))
                else:
                    self.run_stmts(xc, body, pscope)

            if kind == "clocked" and resets:
                arms = []
                for br in resets:
                    c = self.cond_bit(self.eval(br.cond, sc))
                    arms.append((c, (lambda br=br: self.run_stmts(xc, br.body, pscope))))
                blk.if_chain(arms, run_body, origin=self.org(p))
            else:
                run_body()
            blk.finish()
        finally:
            self.proc = saved

    # ------------------------------------------------------------------ generate / blocks
    def with_prefix(self, name):
        old = self.prefix
        self.prefix = old + name + "."
        return old

    def gen_scope(self, sc, label, decls):
        g = Scope(sc, label=label or "")
        self.declare_items(decls, g, None)
        return g

    def gen_for(self, s, sc):
        l, r, d = self.eval_range(s.range, sc)
        vals = list(range(l, r + 1)) if d == "to" else list(range(l, r - 1, -1))
        lim = self.ctx.limits["generate_iterations"]
        if len(vals) > lim:
            self.diag("LIMIT_REACHED", "for-generate '%s' with %d iterations exceeds the limit %d" % (s.label, len(vals), lim), s)
            vals = vals[:lim]
        for v in vals:
            g = Scope(sc, label=s.label or "")
            g.declare(Sym("const", s.var, T.T_UINT, val=int_val(v)))
            old = self.with_prefix("%s(%d)" % (s.label or "gen", v))
            try:
                self.declare_items(s.decls, g, None)
                self.elab_stmts(s.stmts, g)
            finally:
                self.prefix = old

    def gen_if(self, s, sc):
        branches = list(s.branches or ())
        chosen = None
        for br in branches:
            c = self.eval(br.cond, sc)
            try:
                ok = bool(self.const_int(c))
            except NotConst:
                self.diag("UNSUPPORTED_CONSTRUCT", "if-generate condition is not constant; branch skipped", br)
                continue
            if ok:
                chosen = br
                break
        if chosen is None:
            chosen = s.else_branch
        if chosen is None:
            return
        g = Scope(sc, label=chosen.label or s.label or "")
        old = self.with_prefix(chosen.label or s.label or "gen")
        try:
            self.declare_items(chosen.decls, g, None)
            self.elab_stmts(chosen.stmts, g)
        finally:
            self.prefix = old

    def gen_case(self, s, sc):
        sel = self.eval(s.selector, sc)
        chosen = None
        for alt in s.alts:
            for ch in alt.choices:
                if isinstance(ch, N.Others):
                    chosen = chosen or alt
                    continue
                if self.is_range_expr(ch, sc):
                    l, r, d = self.eval_range(ch, sc)
                    cs = self.const_int(sel)
                    if min(l, r) <= cs <= max(l, r):
                        chosen = alt
                        break
                else:
                    lv = self.eval(ch, sc, sel.t)
                    if sel.c is not None and lv.c is not None:
                        if sel.c == lv.c:
                            chosen = alt
                            break
                    elif self.const_val(sel) == self.const_val(lv):
                        chosen = alt
                        break
            else:
                continue
            break
        if chosen is None:
            return
        g = Scope(sc, label=chosen.label or s.label or "")
        old = self.with_prefix(chosen.label or s.label or "gen")
        try:
            self.declare_items(chosen.decls, g, None)
            self.elab_stmts(chosen.stmts, g)
        finally:
            self.prefix = old

    def elab_block(self, s, sc):
        g = Scope(sc, label=s.label or "")
        if s.guard is not None:
            self.diag("UNSUPPORTED_CONSTRUCT", "guarded block '%s': the guard is ignored, guarded assignments are "
                      "costed as unconditional" % (s.label or "block"), s)
            self.ctx.assume("guard of block '%s' ignored" % (s.label or "block"))
        old = self.with_prefix(s.label or "block")
        try:
            for a in s.port_map or ():
                if a.formal is not None and isinstance(a.formal, N.Ident):
                    g.declare(Sym("alias", a.formal.name, None, node=a.actual, scope=sc))
            for a in s.generic_map or ():
                if a.formal is not None and isinstance(a.formal, N.Ident):
                    try:
                        g.declare(Sym("const", a.formal.name, None, val=self.eval(a.actual, sc)))
                    except Exception:
                        pass
            self.declare_items(s.decls, g, None)
            self.elab_stmts(s.stmts, g)
        finally:
            self.prefix = old
