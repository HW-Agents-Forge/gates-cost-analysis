"""Procedural statements: symbolic execution through ``ir.LowerBlock``; function/task inlining."""
from __future__ import annotations

from gatecost.frontend.verilog import ast as A
from gatecost.ir import LowerBlock, SigSpec

from .expr import ElabError
from .symbols import BIT, PSym, Scope, V, VSym


def qual_flags(qualifier, parallel_case=False, full_case=False):
    """``(parallel, full)`` for ``LowerBlock.case_``/``if_chain`` from a SystemVerilog qualifier and the
    ``parallel_case``/``full_case`` pragmas: ``unique`` is parallel and full, ``unique0`` parallel,
    ``priority`` full and explicitly prioritised."""
    par = True if parallel_case else None
    full = bool(full_case)
    if qualifier == "unique":
        par, full = True, True
    elif qualifier == "unique0":
        par = True
    elif qualifier == "priority":
        par, full = (par if par else False), True
    return par, full


class StepLimit(ElabError):
    pass


_CTRL = (A.Break, A.Continue, A.Return)


class StmtMixin:
    # ------------------------------------------------------------------ static helpers
    def has_ctrl(self, s) -> bool:
        k = id(s)
        r = self._ctrl_memo.get(k)
        if r is None:
            r = False
            stack = [s]
            while stack:
                n = stack.pop()
                if isinstance(n, _CTRL):
                    r = True
                    break
                if isinstance(n, A.Node):
                    stack.extend(A.children(n))
            self._ctrl_memo[k] = r
        return r

    def loop_ctrl(self, body):
        """(has break, has continue) for a loop body, not descending into nested loops."""
        brk = cnt = False
        stack = [body]
        while stack:
            n = stack.pop()
            if isinstance(n, A.Break):
                brk = True
            elif isinstance(n, A.Continue):
                cnt = True
            elif isinstance(n, (A.For, A.While, A.DoWhile, A.Repeat, A.Forever, A.Foreach)):
                continue
            elif isinstance(n, A.Node):
                stack.extend(A.children(n))
        return brk, cnt

    # ------------------------------------------------------------------ control flags
    def new_flag(self, name):
        net = self.b.net(1, name)
        self.blk.mark_variable(net)
        self.blk.assign(net, SigSpec.const(0, 1))
        self.created.append(net)
        return net

    def alive(self):
        """1-bit signal: no break/continue/return pending in the active frames."""
        b = self.b
        acc = None
        for fr in reversed(self.ctl):
            for key in ("brk", "cnt", "ret"):
                net = fr.get(key)
                if net is not None:
                    v = self.blk.read(net.sig)
                    acc = v if acc is None else b.or_(acc, v, 1)
            if fr["kind"] == "func":
                break
        if acc is None:
            return SigSpec.const(1, 1)
        return b.not_(acc, 1)

    def drop_nets(self, nets, kill=True):
        blk = self.blk
        if blk is None:
            return
        e = blk.env
        for net in nets:
            leak = net.id in e.val and net.id not in e.vis
            e.val.pop(net.id, None)
            e.vis.pop(net.id, None)
            e.assigned.pop(net.id, None)
            blk._order.pop(net.id, None)
            if kill and not leak and net.fanout == 0 and net.driver is None:
                net.driver = "dead"
                self.b._removed_nets += 1
                self.b.module.driven.pop(net.id, None)

    # ------------------------------------------------------------------ statement lists
    def exec_list(self, items):
        n = len(items)
        for idx in range(n):
            st = items[idx]
            self.exec_item(st)
            if self.ctl and idx + 1 < n and self.has_ctrl(st):
                a = self.alive()
                c = a.const_value()
                if c is not None:
                    if not c.value:
                        return
                    continue
                rest = items[idx + 1:]
                self.blk.if_(a, lambda rest=rest: self.exec_list(rest))
                return

    def exec_item(self, st):
        c = type(st)
        if c is A.VarDecl:
            self.declare_local(st)
        elif c is A.ParamDecl:
            self.declare_param(st, None)
        elif c is A.Typedef:
            self.declare_typedef(st)
        elif c is A.Import:
            self.declare_import(st)
        elif isinstance(st, A.Stmt):
            self.exec_stmt(st)

    def push_scope(self, name=""):
        sc = Scope(self.scope, self.scope.path, name)
        self.scope = sc
        return sc

    def declare_local(self, vd: A.VarDecl):
        pt = self.resolve_dtype(vd.dtype)
        ud = self.eval_dims(vd.dims)
        sym = VSym(vd.name, pt, tuple(ud))
        sym.local = True
        if ud:
            sym.elems = {}
        else:
            net = self.b.net(pt.w, vd.name, pt.signed)
            sym.net = net
            if self.blk is not None:
                self.blk.mark_variable(net)
            self.created.append(net)
            self.block_locals.append(net)
        self.scope.syms[vd.name] = sym
        if vd.init is not None and self.blk is not None and sym.net is not None:
            self.blk.assign(sym.net, self.assign_val(vd.init, pt.w, pt), signed=False)

    def elem_net(self, sym: VSym, i: int):
        d = sym.elems
        n = d.get(i)
        if n is None:
            n = d[i] = self.b.net(sym.pt.w, f"{sym.hier or sym.name}[{i}]", sym.pt.signed)
            if sym.local and self.blk is not None:
                self.blk.mark_variable(n)
        return n

    # ------------------------------------------------------------------ statements
    def exec_stmt(self, s):
        if self.depth > 0:
            self.fsteps += 1
            if self.fsteps > self.ctx.limits["const_func_steps"]:
                raise StepLimit("step limit")
        self._cur = s
        c = type(s)
        fn = self._dispatch.get(c)
        if fn is None:
            if c is A.Null:
                return
            self.diag("UNSUPPORTED_CONSTRUCT", f"statement {c.__name__}", s, True)
            return
        fn(self, s)

    def x_block(self, s: A.Block):
        old = self.scope
        saved_locals = self.block_locals
        self.block_locals = []
        self.push_scope(s.label or "")
        try:
            self.exec_list(s.items)
        finally:
            self.scope = old
            mine = self.block_locals
            self.block_locals = saved_locals
            self.drop_nets(mine)

    def x_assign(self, s: A.ProcAssign):
        if self.blk is None:
            self.diag("UNSUPPORTED_CONSTRUCT", "procedural assignment outside a procedural block", s, True)
            return
        self.b.origin = self.org(s.loc)
        parts = self.lvalue_parts(s.lhs)
        if not parts or any(p is None for p in parts):
            return
        tw = self.part_width(parts)
        nb = not s.blocking
        if s.op not in ("=", "<="):
            rhs = A.Binary(s.loc, s.op[:-1], s.lhs, s.rhs)
            sig = self.assign_val(rhs, tw, parts[0].pt if len(parts) == 1 else None)
        else:
            if nb and self.try_sync_read(parts, s):
                return
            sig = self.assign_val(s.rhs, tw, parts[0].pt if len(parts) == 1 else None)
        self.store_parts(parts, sig, nb)

    def try_sync_read(self, parts, s) -> bool:
        blk = self.blk
        if blk.kind != "clocked" or len(parts) != 1 or not isinstance(s.rhs, A.Index):
            return False
        p = parts[0]
        if p.mem is not None or p.elems is not None or p.dyn or p.net is None or p.vsym is None:
            return False
        if p.off != 0 or p.w != p.net.width:
            return False
        if self.assign_counts.get(p.vsym.name, 0) != 1 or p.vsym.is_port and p.vsym.direction == "in":
            return False
        r = self.resolve_ref(s.rhs, True)
        if r is None or r.mem is None or r.dyn or r.off != 0 or r.w != r.mem.width or r.oob:
            return False
        en = blk.path_condition()
        rd = r.mem.read(r.addr, clk=blk.clock, en=en, clk_edge=blk.clk_edge, origin=self.b.origin)
        self.b.connect(SigSpec.of(p.net, 0, p.net.width), rd.resize(p.net.width, False), self.b.origin)
        self.sync_read_done.add(p.net.id)
        return True

    # -- stores --------------------------------------------------------------------
    def store_parts(self, parts, sig: SigSpec, nb: bool):
        pos = 0
        for p in reversed(parts):
            piece = sig.slice(pos, p.w)
            pos += p.w
            self.store(p, piece, nb)

    def store(self, p, piece: SigSpec, nb: bool):
        blk = self.blk
        if p.mem is not None:
            mem = p.mem
            partial = bool(p.off) or p.w != mem.width or bool(p.dyn)
            if partial and not p.dyn and 0 <= p.off and p.off + p.w <= mem.width and p.w > 0:
                # constant part-select of a word: per-lane write enable (merged into one port by the IR)
                if blk is not None:
                    blk.mem_write(mem, p.addr, piece.resize(p.w, False), origin=self.b.origin, lane=(p.off, p.w))
                else:
                    mem.write(p.addr, piece.resize(p.w, False), origin=self.b.origin, lane=(p.off, p.w))
                return
            if partial:
                self.diag("UNSUPPORTED_CONSTRUCT", "memory word write with a variable part-select approximated by "
                          "a full-word write", self._cur_node(), True)
            if blk is not None:
                blk.mem_write(mem, p.addr, piece.resize(mem.width, False), origin=self.b.origin)
            else:
                mem.write(p.addr, piece.resize(mem.width, False), origin=self.b.origin)
            return
        if p.elems is not None:
            if blk is None:
                self.diag("UNSUPPORTED_CONSTRUCT", "dynamic index on a continuous assignment target",
                          self._cur_node(), True)
                return
            items = []
            for j, net in enumerate(p.elems):
                items.append(([SigSpec.const(j, p.eidx.width)],
                              lambda net=net: self.store_dyn(net, p.off, p.w, p.dyn, piece, nb)))
            blk.case_(p.eidx, items, None)
            return
        self.store_dyn(p.net, p.off, p.w, p.dyn, piece, nb)

    def _cur_node(self):
        return self._cur

    def store_dyn(self, net, off, w, dyn, piece, nb):
        blk = self.blk
        if not dyn:
            if net.width < off + w:
                return
            dst = SigSpec.of(net, off, w)
            if blk is not None:
                blk.assign(dst, piece, nonblocking=nb)
            else:
                self.b.connect(dst, piece, self.b.origin)
            return
        if blk is None:
            self.diag("UNSUPPORTED_CONSTRUCT", "dynamic index on a continuous assignment target",
                      self._cur_node(), True)
            return
        idx, stride, count = dyn[0]
        if idx is None:
            return
        if count > 4096:
            self.diag("LIMIT_REACHED", "dynamic write with more than 4096 positions is not supported",
                      self._cur_node(), True)
            return
        items = []
        for j in range(count):
            if off + j * stride + w > net.width:
                break
            items.append(([SigSpec.const(j, idx.width)],
                          lambda j=j: self.store_dyn(net, off + j * stride, w, dyn[1:], piece, nb)))
        blk.case_(idx, items, None)

    # -- if / case -------------------------------------------------------------------
    def x_if(self, s: A.If):
        arms = []
        cur = s
        els = None
        while True:
            arms.append((cur.cond, cur.then))
            nxt = cur.else_
            if isinstance(nxt, A.If):
                cur = nxt
                continue
            els = nxt
            break
        self.b.origin = self.org(s.loc)
        org = self.org(s.loc)
        run = lambda t: (lambda: self.exec_stmt(t) if t is not None else None)
        elsefn = (lambda: self.exec_stmt(els)) if els is not None else None
        clocked = self.blk.kind == "clocked"
        par, full = qual_flags(s.qualifier)

        def emit(arms):
            if clocked and len(arms) >= 2 and self.reset_like(arms[0][1]):
                # peel reset-like arms so that reset / enable patterns stay recognisable (IR.md section 5)
                self.blk.if_chain([(self.cond(arms[0][0]), run(arms[0][1]))], lambda: emit(arms[1:]), origin=org)
                return
            conds = [self.cond(c) for c, _ in arms]
            self.blk.if_chain([(cd, run(t)) for cd, (_c, t) in zip(conds, arms)], elsefn, origin=org,
                              parallel=par, full=full and elsefn is None)

        emit(arms)

    def reset_like(self, st) -> bool:
        """True when a statement only assigns constants (a reset arm)."""
        k = id(st)
        r = self._reset_memo.get(k)
        if r is None:
            r = self._reset_like(st)
            self._reset_memo[k] = r
        return r

    def _constish(self, e) -> bool:
        c = type(e)
        if c in (A.Number, A.Fill, A.String):
            return True
        if c is A.Ident:
            return isinstance(self.lookup(e.name), PSym)
        if c is A.ScopedName:
            return isinstance(self.lookup_scoped(e), PSym)
        if c is A.Unary:
            return self._constish(e.operand)
        if c is A.Concat:
            return all(self._constish(x) for x in e.items)
        if c is A.Replicate:
            return self._constish(e.count) and all(self._constish(x) for x in e.items)
        if c is A.AssignPattern:
            return all(self._constish(x.value if isinstance(x, A.PatternItem) else x) for x in e.items)
        if c is A.Binary:
            return self._constish(e.left) and self._constish(e.right)
        if c is A.SysCall:
            return all(self._constish(x) for x in e.args if isinstance(x, A.Expr))
        return False

    def _reset_like(self, st) -> bool:
        c = type(st)
        if c is A.ProcAssign:
            return self._constish(st.rhs)
        if c is A.Block:
            return all(self._reset_like(x) for x in st.items if isinstance(x, A.Stmt))
        if c in (A.For, A.While, A.Repeat):
            return self._reset_like(st.body)
        if c is A.If:
            return self._reset_like(st.then) and (st.else_ is None or self._reset_like(st.else_))
        return c is A.Null

    def x_case(self, s: A.Case):
        b = self.b
        par, full = qual_flags(s.qualifier, s.parallel_case, s.full_case)
        self.b.origin = self.org(s.loc)
        old = self._tm
        self._tm = {}
        try:
            w, sg = self.ty(s.expr)
            labs = []
            for it in s.items:
                if it.labels is None:
                    continue
                for lab in it.labels:
                    for x in ((lab.lo, lab.hi) if isinstance(lab, A.ValueRange) else (lab,)):
                        lw, ls = self.ty(x)
                        w, sg = max(w, lw), sg and ls
            sel = self.bv(s.expr, w, sg)
            ranged = s.inside or any(isinstance(l, A.ValueRange) for it in s.items
                                     if it.labels for l in it.labels)
            default = None
            items = []
            hits = []
            for it in s.items:
                if it.labels is None:
                    default = it
                    continue
                if ranged:
                    h = None
                    for lab in it.labels:
                        if isinstance(lab, A.ValueRange):
                            lo, hi = self.bv(lab.lo, w, sg), self.bv(lab.hi, w, sg)
                            t = b.land(b.ge(sel, lo, sa=sg, sb=sg), b.le(sel, hi, sa=sg, sb=sg))
                        else:
                            t = self.eq_wild(sel, self.bv(lab, w, sg), self.org(it.loc))
                        h = t if h is None else b.or_(h, t, 1)
                    hits.append((h, it))
                else:
                    labels = [self.bv(l, w, sg) for l in it.labels]
                    items.append((labels, (lambda body=it.body: self.exec_stmt(body) if body is not None else None)))
        finally:
            self._tm = old
        dfn = None
        if default is not None and default.body is not None:
            dfn = lambda: self.exec_stmt(default.body)
        if ranged:
            chain = [(h, (lambda body=it.body: self.exec_stmt(body) if body is not None else None))
                     for h, it in hits]
            if chain:
                self.blk.if_chain(chain, dfn, origin=self.org(s.loc), parallel=par, full=full and dfn is None)
            elif dfn:
                dfn()
            return
        self.blk.case_(sel, items, dfn, kind=s.kind, origin=self.org(s.loc), parallel=par,
                       full=full and dfn is None)

    # -- loops -------------------------------------------------------------------------
    def _loop_enter(self, body):
        brk, cnt = self.loop_ctrl(body)
        fr = {"kind": "loop"}
        if brk:
            fr["brk"] = self.new_flag("$brk")
        if cnt:
            fr["cnt"] = self.new_flag("$cnt")
        self.ctl.append(fr)
        return fr

    def _loop_leave(self, fr):
        self.ctl.pop()
        self.drop_nets([n for k, n in fr.items() if k in ("brk", "cnt")])

    def _run_iteration(self, fr, body):
        cnt = fr.get("cnt")
        if cnt is not None:
            self.blk.assign(cnt, SigSpec.const(0, 1))
        a = self.alive() if self.ctl else SigSpec.const(1, 1)
        c = a.const_value()
        if c is not None:
            if not c.value:
                return False
            self.exec_stmt(body)
        else:
            self.blk.if_(a, lambda: self.exec_stmt(body))
        return True

    def _loop_cond(self, cond, node):
        if cond is None:
            return True
        sig = self.cond(cond)
        c = sig.const_value()
        if c is None or c.xmask:
            self.diag("UNSUPPORTED_CONSTRUCT", "loop condition is not constant; loop not unrolled", node, True)
            return None
        return bool(c.value)

    def x_for(self, s: A.For):
        old = self.scope
        saved_locals = self.block_locals
        self.block_locals = []
        self.push_scope("for")
        fr = None
        try:
            for st in s.init or ():
                if isinstance(st, A.ProcAssign):
                    self.note_loop_var(st.lhs)
                self.exec_item(st)
            fr = self._loop_enter(s.body)
            limit = self.ctx.limits["loop_unroll"]
            it = 0
            while True:
                c = self._loop_cond(s.cond, s)
                if not c:
                    break
                if it >= limit:
                    self.diag("LIMIT_REACHED", f"loop unrolling limit ({limit}) reached; remaining iterations "
                              "ignored", s, True)
                    break
                it += 1
                if not self._run_iteration(fr, s.body):
                    break
                for st in s.step or ():
                    self.exec_item(st)
        finally:
            if fr is not None:
                self._loop_leave(fr)
            self.scope = old
            mine = self.block_locals
            self.block_locals = saved_locals
            self.drop_nets(mine)

    def note_loop_var(self, lhs):
        if isinstance(lhs, A.Ident):
            sym = self.lookup(lhs.name)
            if isinstance(sym, VSym) and sym.net is not None:
                self.forced_vars[sym.net.id] = sym.net

    def x_while(self, s: A.While):
        fr = self._loop_enter(s.body)
        try:
            limit = self.ctx.limits["loop_unroll"]
            it = 0
            while True:
                c = self._loop_cond(s.cond, s)
                if not c:
                    break
                if it >= limit:
                    self.diag("LIMIT_REACHED", f"loop unrolling limit ({limit}) reached", s, True)
                    break
                it += 1
                if not self._run_iteration(fr, s.body):
                    break
        finally:
            self._loop_leave(fr)

    def x_dowhile(self, s: A.DoWhile):
        fr = self._loop_enter(s.body)
        try:
            limit = self.ctx.limits["loop_unroll"]
            it = 0
            while True:
                if it >= limit:
                    self.diag("LIMIT_REACHED", f"loop unrolling limit ({limit}) reached", s, True)
                    break
                it += 1
                if not self._run_iteration(fr, s.body):
                    break
                c = self._loop_cond(s.cond, s)
                if not c:
                    break
        finally:
            self._loop_leave(fr)

    def x_repeat(self, s: A.Repeat):
        n = self.cint(s.count, None)
        if n is None:
            self.diag("UNSUPPORTED_CONSTRUCT", "repeat count is not constant", s, True)
            return
        limit = self.ctx.limits["loop_unroll"]
        if n > limit:
            self.diag("LIMIT_REACHED", f"loop unrolling limit ({limit}) reached", s, True)
            n = limit
        fr = self._loop_enter(s.body)
        try:
            for _ in range(n):
                if not self._run_iteration(fr, s.body):
                    break
        finally:
            self._loop_leave(fr)

    def x_forever(self, s):
        self.diag("UNSUPPORTED_CONSTRUCT", "forever loop is not synthesizable; skipped", s, True)

    def x_foreach(self, s: A.Foreach):
        arr = s.array
        sym = self.lookup(arr.name) if isinstance(arr, A.Ident) else None
        if not isinstance(sym, VSym):
            self.diag("UNSUPPORTED_CONSTRUCT", "foreach over an unsupported expression", s, True)
            return
        dims = list(sym.udims) + list(sym.pt.dims or ((sym.pt.w - 1, 0),))
        names = [v for v in (s.vars or ())]
        old = self.scope
        try:
            self._foreach(names, dims, 0, s)
        finally:
            self.scope = old

    def _foreach(self, names, dims, k, s):
        if k >= len(names) or k >= len(dims):
            self.exec_stmt(s.body)
            return
        nm = names[k]
        l, r = dims[k]
        step = 1 if l <= r else -1
        idxs = list(range(l, r + step, step))
        for i in idxs[: self.ctx.limits["loop_unroll"]]:
            self.scope = Scope(self.scope, self.scope.path)
            if nm:
                self.scope.syms[nm] = PSym(self.const_v(i))
            self._foreach(names, dims, k + 1, s)
            self.scope = self.scope.parent

    # -- control transfer --------------------------------------------------------------------
    def x_break(self, s):
        for fr in reversed(self.ctl):
            if fr["kind"] == "func":
                return
            if fr["kind"] == "loop":
                if "brk" in fr:
                    self.blk.assign(fr["brk"], SigSpec.const(1, 1))
                return

    def x_continue(self, s):
        for fr in reversed(self.ctl):
            if fr["kind"] == "func":
                return
            if fr["kind"] == "loop":
                if "cnt" in fr:
                    self.blk.assign(fr["cnt"], SigSpec.const(1, 1))
                return

    def x_return(self, s: A.Return):
        fr = None
        for f in reversed(self.ctl):
            if f["kind"] == "func":
                fr = f
                break
        if fr is None:
            return
        if s.value is not None and fr.get("retvar") is not None:
            rv = fr["retvar"]
            self.blk.assign(rv.net, self.assign_val(s.value, rv.pt.w, rv.pt))
        if fr.get("ret") is not None:
            self.blk.assign(fr["ret"], SigSpec.const(1, 1))

    def x_callstmt(self, s: A.CallStmt):
        call = s.call
        if not isinstance(call, A.Call):
            return
        f = self.find_func(call)
        if f is None:
            return
        self.invoke(f, call.args, call, False)

    def x_event(self, s: A.EventStmt):
        if s.body is not None:
            self.exec_stmt(s.body)

    # ------------------------------------------------------------------ functions
    def call_expr(self, e: A.Call) -> V:
        f = self.find_func(e)
        if f is None:
            return V(SigSpec.x(32), False)
        return self.invoke(f, e.args, e, True)

    def invoke(self, f, args, node, want_value: bool) -> V:
        ast = f.ast
        rt = self.func_rettype(f)
        rw = rt.w if rt is not None else 1
        xres = V(SigSpec.x(rw), bool(rt.signed) if rt else False)
        if ast.nonsynth:
            self.diag("UNSUPPORTED_CONSTRUCT", f"'{ast.name}' contains non-synthesizable constructs; "
                      "result unknown", node, True)
            return xres
        limit = self.ctx.limits["call_depth"]
        if self.depth >= limit:
            self.diag("LIMIT_REACHED", f"function call depth limit ({limit}) reached at '{ast.name}'", node, True)
            return xres
        own_blk = self.blk is None
        outer = self.scope
        saved = (self.ctl, self.created, self.block_locals, self._tm, self.tmp_cands)
        if own_blk:
            self.blk = LowerBlock(self.b, "comb")
        outermost = self.depth == 0
        if outermost:
            self.fsteps = 0
        self.depth += 1
        self.ctl = list(self.ctl)
        self.created = []
        self.block_locals = []
        self.tmp_cands = None
        result = xres
        try:
            blk = self.blk
            new = Scope(f.scope, f.scope.path, ast.name)
            # formal ports: types resolve in the function's own scope
            self.scope = new
            ports = []
            for p in ast.ports:
                pt = self.resolve_dtype(p.dtype) if p.dtype is not None else BIT
                if p.dims:
                    pass
                sym = VSym(p.name, pt)
                sym.local = True
                sym.net = self.b.net(pt.w, p.name, pt.signed)
                blk.mark_variable(sym.net)
                self.created.append(sym.net)
                new.syms[p.name] = sym
                ports.append((p, sym))
            retvar = None
            if rt is not None:
                retvar = VSym(ast.name, rt)
                retvar.local = True
                retvar.net = self.b.net(rt.w, ast.name, rt.signed)
                blk.mark_variable(retvar.net)
                self.created.append(retvar.net)
                new.syms[ast.name] = retvar
            # actual arguments, evaluated in the caller's scope
            self.scope = outer
            bound = {}
            pos = 0
            for a in args:
                if isinstance(a, A.NamedArg):
                    bound[a.name] = a.value
                else:
                    if pos < len(ports):
                        bound[ports[pos][0].name] = a
                    pos += 1
            ins = {}
            outs = {}
            for p, sym in ports:
                a = bound.get(p.name)
                if p.direction in ("output", "inout", "ref") and a is not None:
                    outs[p.name] = self.lvalue_parts(a)
                if p.direction in ("input", "inout", "ref", None):
                    if a is not None:
                        ins[p.name] = self.assign_val(a, sym.pt.w, sym.pt)
                    elif p.default is not None:
                        ins[p.name] = self.assign_val(p.default, sym.pt.w, sym.pt)
            self.scope = new
            for p, sym in ports:
                if p.name in ins:
                    blk.assign(sym.net, ins[p.name])
            fr = {"kind": "func", "retvar": retvar}
            body = list(ast.body)
            stmts = [x for x in body if isinstance(x, A.Stmt)]
            needs_flag = any(self.has_ctrl(x) for x in stmts[:-1]) or (
                bool(stmts) and self._nontail_return(stmts[-1]))
            if needs_flag:
                fr["ret"] = self.new_flag("$ret")
            self.ctl.append(fr)
            self.exec_list(body)
            if retvar is not None:
                result = V(self.rd_var(retvar, retvar.net), rt.signed)
            self.scope = outer
            for p, sym in ports:
                if p.name in outs and None not in outs[p.name]:
                    sig = self.rd_var(sym, sym.net)
                    tw = self.part_width(outs[p.name])
                    self.store_parts(outs[p.name], sig.resize(tw, sym.pt.signed), False)
        except StepLimit:
            self.diag("LIMIT_REACHED", f"constant-function step limit reached in '{ast.name}'", node, True)
            result = xres
        finally:
            self.scope = outer
            self.depth -= 1
            created = self.created
            (self.ctl, self.created, self.block_locals, self._tm, self.tmp_cands) = saved
            self.drop_nets(created)
            if own_blk:
                self.blk = None
        return result

    def _nontail_return(self, last) -> bool:
        """True when the last statement contains a return that is not simply the final statement."""
        if isinstance(last, A.Return):
            return False
        return self.has_ctrl(last)

    _dispatch = {}


StmtMixin._dispatch = {
    A.Block: StmtMixin.x_block, A.ProcAssign: StmtMixin.x_assign, A.If: StmtMixin.x_if,
    A.Case: StmtMixin.x_case, A.For: StmtMixin.x_for, A.While: StmtMixin.x_while,
    A.DoWhile: StmtMixin.x_dowhile, A.Repeat: StmtMixin.x_repeat, A.Forever: StmtMixin.x_forever,
    A.Foreach: StmtMixin.x_foreach, A.Break: StmtMixin.x_break, A.Continue: StmtMixin.x_continue,
    A.Return: StmtMixin.x_return, A.CallStmt: StmtMixin.x_callstmt, A.EventStmt: StmtMixin.x_event,
}
