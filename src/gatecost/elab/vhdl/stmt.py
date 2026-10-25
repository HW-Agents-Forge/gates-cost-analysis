"""Sequential statements through ir.LowerBlock, subprogram inlining, lvalues, loops."""
from __future__ import annotations

from ...frontend.vhdl import nodes as N
from ...ir import LowerBlock, SigSpec
from ...ir.limits import DEFAULT_LIMITS as _IR_DEFAULT_LIMITS
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, StopLimit, Unsupported, bool_const, int_val
from .values import LV, FuncDef, Scope, Sym, Val


class XC:
    """Execution context of a sequential region (process, function call, concurrent statement)."""

    def __init__(self, blk, clocked=False, parent=None, kind="process"):
        self.blk = blk
        self.clocked = clocked
        self.parent = parent
        self.kind = kind
        self.touched = []
        self.loops = []
        self.fn = None
        self.locals = []        # nets to drop at the end (function / procedure locals)
        self.state_vars = set()


class FnCtx:
    def __init__(self, ret_t):
        self.ret_t = ret_t
        self.ret_net = None
        self.done = None


def contains(node, classes):
    from ...frontend.vhdl.nodes import walk
    for n in walk(node):
        if isinstance(n, classes):
            return True
    return False


class StmtMixin:
    # ------------------------------------------------------------------ lvalues
    def lvalue(self, e, sc):
        if isinstance(e, N.Ident):
            syms = sc.lookup(e.name)
            if not syms:
                raise Unsupported("unknown assignment target '%s'" % e.name)
            s = syms[0]
            if s.kind == "obj":
                lv = LV(s.t, s.sig)
                lv.off = 0
                lv.width = s.sig.width
                lv.sym = s
                return lv
            if s.kind == "alias":
                lv = self.lvalue(s.node, s.scope)
                if s.t is not None and s.t.kind == "array" and s.t.width == lv.width:
                    lv.t = s.t
                return lv
            if s.kind == "mem":
                raise Unsupported("assignment to a whole memory")
            raise Unsupported("'%s' is not assignable" % e.name)
        if isinstance(e, N.Select):
            q = self.qualified_syms(e, sc)
            if q is not None:
                raise Unsupported("assignment to a package object")
            base = self.lvalue(e.prefix, sc)
            t = base.t
            if t.kind != "record" or e.suffix not in t.offs:
                raise Unsupported("no record field '%s'" % e.suffix)
            off, w = t.offs[e.suffix]
            ft = dict(t.fields)[e.suffix]
            return self.sub_lv(base, ft, off, w)
        if isinstance(e, N.Apply):
            p = e.prefix
            if isinstance(p, (N.Ident, N.Select)):
                syms = self.name_syms(p, sc)
                if syms and syms[0].kind == "mem":
                    return self.mem_lv(syms[0], e.args, sc, e)
                if syms and syms[0].kind == "type" or (not syms and isinstance(p, N.Ident) and p.name in T.BUILTIN_TYPES):
                    return self.lvalue(e.args[0].actual, sc)
            base = self.lvalue(p, sc)
            for a in e.args:
                ex = a.actual
                if self.is_range_expr(ex, sc):
                    base = self.slice_lv_any(base, ex, sc, e)
                else:
                    base = self.index_lv(base, ex, sc, e)
            return base
        if isinstance(e, N.Slice):
            base = self.lvalue(e.prefix, sc)
            return self.slice_lv_any(base, e.range, sc, e)
        if isinstance(e, N.ExternalName):
            raise Unsupported("assignment to the external name '<< %s %s >>' (not resolved across the hierarchy)"
                              % (e.cls, e.path))
        if isinstance(e, N.Aggregate):
            parts = [self.lvalue(it.value, sc) for it in e.items]
            lv = LV(None, parts=parts)
            lv.width = sum(p.width for p in parts)
            return lv
        raise Unsupported("assignment target %s" % type(e).__name__)

    def sub_lv(self, base, t, off, w):
        lv = LV(t, base.sig)
        lv.off = base.off + off
        lv.width = w
        lv.dyn = list(base.dyn)
        lv.sym = base.sym
        lv.mem, lv.addr = base.mem, base.addr
        return lv

    def slice_lv_any(self, base, rng, sc, node):
        try:
            l, r, d = self.eval_range(rng, sc)
        except NotConst:
            return self.dyn_slice_lv(base, rng, sc, node)
        return self.slice_lv(base, l, r, d)

    def dyn_slice_lv(self, base, rng, sc, node):
        """Target ``a(i+3 downto i)``: constant width, variable position (decoded write)."""
        n, low, d = self.dyn_slice_bounds(rng, sc)
        t = base.t
        if t.kind != "array" or t.left is None:
            raise Unsupported("variable slice of an unconstrained or non-array target")
        ew = t.ewidth or 1
        iv = self.eval(low, sc, t.index)
        x, _cnt, lo = self.index_pos_sig(t, iv, node)
        count = t.length - n + 1
        if count < 1:
            raise Unsupported("variable slice wider than the target")
        offs = []
        for a in range(count):
            i = lo + a
            p_lo = T.index_to_pos(t, i) if t.dir == "downto" else T.index_to_pos(t, i + n - 1)
            offs.append(p_lo * ew)
        nt = T.constrain(t, n - 1, 0, "downto") if d == "downto" else T.constrain(t, 0, n - 1, "to")
        lv = self.sub_lv(base, nt, 0, n * ew)
        lv.dyn = base.dyn + [(x, offs)]
        self.ctx.assume("write to a slice at a variable position costed as a %d-way decoded write" % count)
        return lv

    def slice_lv(self, base, l, r, d):
        t = base.t
        ew = t.ewidth or 1
        if t.left is None:
            raise Unsupported("slice of an unconstrained target")
        lo_i, hi_i = min(l, r), max(l, r)
        if t.dir == "downto":
            p_lo, p_hi = lo_i - t.right, hi_i - t.right
        else:
            p_lo, p_hi = t.right - hi_i, t.right - lo_i
        p_lo = max(p_lo, 0)
        p_hi = min(p_hi, t.length - 1)
        nt = T.constrain(t, l, r, d)
        return self.sub_lv(base, nt, p_lo * ew, (p_hi - p_lo + 1) * ew)

    def index_lv(self, base, ex, sc, node):
        t = base.t
        if t.kind != "array":
            raise Unsupported("indexing a non-array target")
        ew = t.ewidth or 1
        iv = self.eval(ex, sc, t.index)
        try:
            i = self.const_int(iv)
        except NotConst:
            x, n, lo = self.index_pos_sig(t, iv, node)
            offs = [T.index_to_pos(t, lo + j) * ew for j in range(n)]
            lv = self.sub_lv(base, t.elem, 0, ew)
            lv.dyn = base.dyn + [(x, offs)]
            return lv
        p = T.index_to_pos(t, i)
        if not (0 <= p < t.length):
            raise Unsupported("index %d outside the target range" % i)
        return self.sub_lv(base, t.elem, p * ew, ew)

    def mem_lv(self, sym, args, sc, node):
        mem, t = sym.val, sym.t
        iv = self.eval(args[0].actual, sc, t.index)
        x, n, lo = self.index_pos_sig(t, iv, node)
        lv = LV(t.elem, None, mem=mem, addr=x)
        lv.width = t.ewidth or 1
        lv.off = 0
        return lv

    def lv_static_sig(self, lv):
        if lv.sig is None:
            raise Unsupported("memory target")
        return lv.sig.slice(lv.off, lv.width)

    # ------------------------------------------------------------------ assignment
    def do_assign(self, xc, lv, val, node, nonblocking=None):
        blk = xc.blk
        o = self.org(node)
        if lv.parts is not None:
            s = self.sig_of(val)
            w = sum(p.width for p in lv.parts)
            if s.width != w:
                s = s.resize(w, False)
            pos = w
            for p in lv.parts:
                pos -= p.width
                part = Val(p.t, s.slice(pos, p.width)) if p.t is not None else Val(T.T_SLV, s.slice(pos, p.width))
                self.do_assign(xc, p, part, node, nonblocking)
            return
        if lv.mem is not None:
            v = self.fit(val, lv.t, node) if lv.t is not None else val
            sg = self.sig_of(v)
            mw = lv.mem.width
            if lv.dyn:
                self.diag("UNSUPPORTED_CONSTRUCT", "memory word write at a variable position approximated by a "
                          "full-word write", node)
            elif lv.off or lv.width != mw:
                # partial word (byte enables): a lane write, merged into one port with per-lane enables by the IR
                blk.mem_write(lv.mem, lv.addr, sg.resize(lv.width, False), origin=o, lane=(lv.off, lv.width))
                return
            blk.mem_write(lv.mem, lv.addr, sg.resize(mw, False), origin=o)
            return
        if lv.width == 0 and not lv.dyn:
            return                                   # null-range target
        lim = self.ctx.ir_limits or _IR_DEFAULT_LIMITS
        if lv.width > lim.max_net_width:
            # a target this wide (typically from a garbage-in generic) would only be resized, never costed
            self.diag("LIMIT_REACHED", "assignment to a target of %d bits exceeds the net width limit (%d); skipped"
                      % (lv.width, lim.max_net_width), node)
            return
        nb = self.is_nb(xc, lv) if nonblocking is None else nonblocking
        t = lv.t
        v = self.fit(val, t, node) if t is not None else val
        s = self.sig_of(v)
        if s.width != lv.width:
            s = s.resize(lv.width, False)
        if not lv.dyn:
            blk.assign(self.lv_static_sig(lv), s, nonblocking=nb, origin=o)
            return
        base = lv.sig
        sl = base.single_slice()
        if sl is None:
            raise Unsupported("dynamic index into a concatenated target")
        net, lo0 = sl.net, sl.lo + lv.off
        dyn = lv.dyn
        w = lv.width

        def rec(i, start):
            if i == len(dyn):
                blk.assign(SigSpec.of(net, start, w), s, nonblocking=nb, origin=o)
                return
            x, offs = dyn[i]
            items = []
            cap = self.ctx.limits["generate_iterations"]
            if len(offs) > cap and len(offs) <= (1 << x.width):
                self.diag("LIMIT_REACHED", "dynamic write into %d positions exceeds the expansion limit %d; truncated" % (len(offs), cap), node)
            for j, off in enumerate(offs):
                if j >= (1 << x.width) or j >= cap:
                    break
                items.append(([SigSpec.const(j, x.width)], (lambda j=j, off=off: rec(i + 1, start + off))))
            blk.case_(x, items, None, origin=o)

        rec(0, lo0)

    def is_nb(self, xc, lv):
        if not xc.clocked:
            return False
        s = lv.sym
        return not (s is not None and s.mode == "variable")

    def target_type(self, lv):
        return lv.t

    # ------------------------------------------------------------------ cond mux (tri-state aware)
    def cond_mux(self, cond, a, b_, node):
        """cond ? b_ : a  (a when cond is 0), tri-state flag when an arm is high impedance."""
        o = self.org(node)
        sa, sb = self.sig_of(a), self.sig_of(b_)
        w = max(sa.width, sb.width)
        sa, sb = sa.resize(w, False), sb.resize(w, False)
        tri = bool(a.z or b_.z) and not (a.z and b_.z)
        if tri:
            r = self.b.mux(cond, sa, sb, w, origin=o, tristate=True)
        else:
            from ..expr import mux_runs
            r = mux_runs(self.b, cond, sa, sb, w, origin=o)
        t = b_.t if b_.t.kind != "enum" or True else a.t
        t = a.t if (b_.z and not a.z) else b_.t
        return Val(t, r, None, 0)

    # ------------------------------------------------------------------ statement list
    def run_stmts(self, xc, stmts, sc):
        n = len(stmts)
        for k, s in enumerate(stmts):
            mark = len(xc.touched)
            self.run_stmt(xc, s, sc)
            if len(xc.touched) > mark and k + 1 < n:
                flags = list(dict.fromkeys(xc.touched[mark:]))
                stop = self.flags_set(xc, flags)
                rest = stmts[k + 1:]
                if stop is None:
                    self.run_stmts(xc, rest, sc)
                else:
                    xc.blk.if_(self.b.not_(stop, 1), lambda: self.run_stmts(xc, rest, sc))
                return

    def flags_set(self, xc, flags):
        sig = None
        for f in flags:
            v = xc.blk.read(f.sig)
            c = v.const_value()
            if c is not None and c.value == 0:
                continue
            sig = v if sig is None else self.b.or_(sig, v, 1)
        if sig is not None:
            c = sig.const_value()
            if c is not None and c.value == 0:
                return None
        return sig

    def run_stmt(self, xc, s, sc):
        h = _STMT.get(type(s))
        if h is None:
            if isinstance(s, N.Skipped):
                return
            raise Unsupported("statement %s" % type(s).__name__)
        self.ctx_steps()
        saved = self.b.origin
        self.b.origin = self.org(s)
        try:
            h(self, xc, s, sc)
        except (StopLimit, RecursionError):
            raise
        except Exception as ex:
            if getattr(self.ctx, "debug", False):
                raise
            self.diag("UNSUPPORTED_CONSTRUCT", "statement skipped (%s: %s)" % (type(ex).__name__, ex), s)
        finally:
            self.b.origin = saved

    def ctx_steps(self):
        self.steps += 1
        if self.steps > self.ctx.limits["const_steps"] and self.call_depth > 0:
            raise StopLimit("interpreter steps")

    # ------------------------------------------------------------------ individual statements
    def mem_slice_parts(self, e, sc):
        """(memory prefix node, [index, ...]) for ``mem(a to b)`` / ``mem(a to b)`` written as slice or range
        application on a name that denotes a memory with constant bounds, else None."""
        if isinstance(e, N.Ident):                       # the whole memory
            syms = sc.lookup(e.name)
            if syms and syms[0].kind == "mem":
                t = syms[0].t
                l, r = t.left, t.right
                return e, (list(range(l, r + 1)) if t.dir == "to" else list(range(l, r - 1, -1)))
            return None
        if isinstance(e, N.Slice):
            pre, rng = e.prefix, e.range
        elif isinstance(e, N.Apply) and len(e.args) == 1 and self.is_range_expr(e.args[0].actual, sc):
            pre, rng = e.prefix, e.args[0].actual
        else:
            return None
        if not isinstance(pre, (N.Ident, N.Select)):
            return None
        syms = self.name_syms(pre, sc)
        if not syms or syms[0].kind != "mem":
            return None
        try:
            l, r, d = self.eval_range(rng, sc)
        except Exception:
            return None
        idx = list(range(l, r + 1)) if d == "to" else list(range(l, r - 1, -1))
        return pre, idx

    def mem_aggregate_words(self, agg, didx, sc):
        """One expression per index of ``didx`` for a (positional / named / ``others``) aggregate, else None."""
        words = {}
        others = None
        pos = 0
        for it in agg.items:
            if it.choices is None:
                if pos >= len(didx):
                    return None
                words[didx[pos]] = it.value
                pos += 1
                continue
            for ch in it.choices:
                if isinstance(ch, N.Others):
                    others = it.value
                elif isinstance(ch, N.Range) or (isinstance(ch, N.Attr) and ch.name in ("range", "reverse_range")):
                    l, r, _d = self.eval_range(ch, sc)
                    for i in range(min(l, r), max(l, r) + 1):
                        words[i] = it.value
                else:
                    words[self.const_int(self.eval(ch, sc))] = it.value
        out = []
        for i in didx:
            v = words.get(i, others)
            if v is None:
                return None
            out.append(v)
        return out

    def st_mem_slice_assign(self, xc, s, sc):
        """``mem(0 to n-1) <= mem(1 to n)`` (a word shift register) / ``mem(a to b) <= (others => x)``:
        one assignment per word.  Returns False when the statement has another shape."""
        if len(s.alts) != 1 or s.alts[0].cond is not None or len(s.alts[0].values) != 1:
            return False
        dst = self.mem_slice_parts(s.target, sc)
        if dst is None:
            return False
        pre, didx = dst
        rhs = s.alts[0].values[0]
        src = self.mem_slice_parts(rhs, sc)

        def elem(prefix, i):
            lit = N.Literal("int", i, None, line=s.line, col=s.col)
            return N.Apply(prefix, [N.Assoc(None, lit, line=s.line, col=s.col)], line=s.line, col=s.col)

        if src is not None:
            spre, sidx = src
            if len(sidx) != len(didx):
                return False
            values = [elem(spre, j) for j in sidx]
        elif isinstance(rhs, N.Aggregate):
            values = self.mem_aggregate_words(rhs, didx, sc)
            if values is None:
                return False
        else:
            return False
        # the right-hand sides all read the old contents: evaluate them before any word is written
        syn = []
        for i, v in zip(didx, values):
            syn.append(N.SignalAssign(s.label, elem(pre, i), [N.Alt([v], None, line=s.line, col=s.col)], s.kind,
                                      s.guarded, line=s.line, col=s.col))
        if xc.clocked or src is None:
            for st in syn:
                self.st_assign(xc, st, sc)
            return True
        # combinational / variable shift: read all sources first, then write
        read = [self.eval(st.alts[0].values[0], sc, self.lvalue(st.target, sc).t) for st in syn]
        for st, v in zip(syn, read):
            self.do_assign(xc, self.lvalue(st.target, sc), v, s)
        return True

    def st_assign(self, xc, s, sc):
        tgt = s.target
        if isinstance(tgt, (N.Slice, N.Apply)) and self.st_mem_slice_assign(xc, s, sc):
            return
        if isinstance(tgt, N.Aggregate) or True:
            lv = self.lvalue(tgt, sc)
        alts = s.alts
        hint = lv.t
        if len(alts) == 1 and alts[0].cond is None:
            v = self.eval(alts[0].values[0], sc, hint)
            if isinstance(alts[0].values[0], N.Unaffected):
                return
            self.do_assign(xc, lv, v, s)
            return
        vals = []
        for a in alts:
            if a.values and isinstance(a.values[0], N.Unaffected):
                vals.append(None)
            else:
                vals.append(self.eval(a.values[0], sc, hint))
        tri = any(v is not None and v.z for v in vals)
        if tri:
            res = vals[-1]
            for a, v in reversed(list(zip(alts[:-1], vals[:-1]))):
                c = self.cond_bit(self.eval(a.cond, sc))
                res = self.cond_mux(c, res, v, s)
            self.do_assign(xc, lv, res, s)
            return
        arms = []
        for a, v in zip(alts, vals):
            if a.cond is None:
                continue
            c = self.cond_bit(self.eval(a.cond, sc))
            arms.append((c, (lambda v=v: self.do_assign(xc, lv, v, s) if v is not None else None)))
        last = vals[-1] if alts[-1].cond is None else None
        els = (lambda: self.do_assign(xc, lv, last, s)) if last is not None else None
        xc.blk.if_chain(arms, els, origin=self.org(s))

    def sel_choices(self, ch_list, sel, sc):
        """Constants (SigSpecs of the selector width) for case choices; ranges expand; None marks 'others'."""
        out = []
        sw = self.sig_of(sel).width if sel.sig is not None or sel.t.kind != "int" else None
        for ch in ch_list:
            if isinstance(ch, N.Others):
                out.append(None)
                continue
            if self.is_range_expr(ch, sc):
                l, r, d = self.eval_range(ch, sc)
                lo, hi = min(l, r), max(l, r)
                out.append(("v", lo) if lo == hi else ("r", lo, hi))
                continue
            lab = self.eval(ch, sc, sel.t)
            out.append(("val", lab))
        return out

    def label_sig(self, lab, sel, selsig):
        w = selsig.width
        if lab[0] == "v":
            return SigSpec.const(lab[1] & mask(w), w)
        v = lab[1]
        if v.t.kind == "int" and v.sig is None:
            return SigSpec.const(int(v.c) & mask(w), w)
        if sel.t.kind == "enum" and v.t.kind == "enum" and v.c is not None and sel.t.lits is not None:
            v = self.fit(v, sel.t)
        s = self.sig_of(v)
        if s.width != w:
            s = s.resize(w, self.is_signed(v))
        return s

    def case_items(self, xc, alts, sel, sc):
        selsig = self.sig_of(sel) if (sel.sig is not None or sel.c is None or sel.t.kind != "int") else None
        if selsig is None:
            selsig = self.sig_of(sel)
        items = []
        default_fn = None
        for alt in alts:
            labels = self.sel_choices(alt.choices, sel, sc)
            body = alt.body if hasattr(alt, "body") else None
            fn = (lambda body=body: self.run_stmts(xc, body, sc)) if body is not None else None
            yield labels, fn, selsig

    def st_case(self, xc, s, sc):
        sel = self.eval(s.selector, sc)
        if sel.c is not None and sel.sig is None and sel.t.kind in ("int", "enum"):
            for alt in s.alts:
                for lab in self.sel_choices(alt.choices, sel, sc):
                    if lab is None or (lab[0] == "v" and lab[1] == sel.c) or (lab[0] == "val" and lab[1].c == sel.c):
                        self.run_stmts(xc, alt.body, sc)
                        return
            return
        selsig = self.sig_of(sel)
        if self.try_case_rom(xc, s, sel, selsig, sc):
            return
        self.exec_case(xc, selsig, sel, [(alt.choices, alt.body) for alt in s.alts], sc, s)

    def try_case_rom(self, xc, s, sel, selsig, sc):
        """A ``case`` whose alternatives each assign one constant to the same target is a lookup table."""
        if getattr(s, "matching", False) or not (3 <= selsig.width <= 12):
            return False
        tgt = None
        alts = []
        for alt in s.alts:
            body = alt.body or []
            if len(body) != 1 or not isinstance(body[0], N.SignalAssign):
                return False
            a = body[0]
            if len(a.alts) != 1 or a.alts[0].cond is not None or not a.alts[0].values:
                return False
            if tgt is None:
                tgt = a.target
            elif repr(a.target) != repr(tgt):
                return False
            alts.append((alt.choices, a.alts[0].values[0]))
        if tgt is None:
            return False
        try:
            lv = self.lvalue(tgt, sc)
            if lv.parts is not None or lv.mem is not None or lv.dyn or lv.t is None:
                return False
            alts = [(ch, self.eval(e, sc, lv.t)) for ch, e in alts]
        except Exception:
            return False
        return self.rom_from_alts(xc, lv, sel, selsig, alts, sc, s)

    def rom_from_alts(self, xc, lv, sel, selsig, alts, sc, node):
        """Build a ROM from ``(choices, constant Val)`` alternatives of a case / selected assignment."""
        if lv.parts is not None or lv.mem is not None or lv.dyn or lv.t is None or not (3 <= selsig.width <= 12):
            return False
        w = selsig.width
        table = {}
        default = None
        n = 0
        for choices, v in alts:
            if v is None or v.z:
                return False
            try:
                cs = self.sig_of(self.fit(v, lv.t, node)).resize(lv.width, False).const_value()
            except Exception:
                return False
            if cs is None or cs.xmask:
                return False
            for l in self.sel_choices(choices, sel, sc):
                if l is None:
                    default = cs.value
                    continue
                if l[0] == "r":
                    return False
                lc = self.label_sig(l, sel, selsig).const_value()
                if lc is None or lc.xmask:
                    return False
                n += 1
                table.setdefault(lc.value & mask(w), cs.value)
        if n < 6 or (default is None and len(table) < (1 << w)):
            return False
        if sel.t.kind == "enum" and sel.t.onehot:
            return False
        contents = [table.get(i, default) for i in range(1 << w)]
        rom = self.b.rom(selsig, contents, lv.width, origin=self.org(node))
        self.do_assign(xc, lv, Val(lv.t, rom), node)
        return True

    def exec_case(self, xc, selsig, sel, alts, sc, node, run=None):
        """Shared by case statements and selected assignments; ``run(body)`` executes an alternative."""
        run = run or (lambda body: self.run_stmts(xc, body, sc))
        items = []
        default_fn = None
        use_chain = bool(sel.t.kind == "enum" and sel.t.onehot and sel.t.width > 1)
        exp = []
        for choices, body in alts:
            labs = self.sel_choices(choices, sel, sc)
            exp.append((labs, body))
            if any(l is not None and l[0] == "r" for l in labs):
                use_chain = True
        blk = xc.blk
        o = self.org(node)
        if use_chain:
            arms = []
            for labs, body in exp:
                if any(l is None for l in labs):
                    default_fn = (lambda body=body: run(body))
                    continue
                conds = []
                for l in labs:
                    if l[0] == "r":
                        w = selsig.width
                        sg = self.is_signed(sel)
                        lo_c = SigSpec.const(l[1] & mask(w), w)
                        hi_c = SigSpec.const(l[2] & mask(w), w)
                        conds.append(self.b.and_(self.b.ge(selsig, lo_c, sa=sg, sb=sg), self.b.le(selsig, hi_c, sa=sg, sb=sg), 1))
                        continue
                    ls = self.label_sig(l, sel, selsig)
                    cv = ls.const_value()
                    if sel.t.kind == "enum" and sel.t.onehot and cv is not None and cv.value and cv.value & (cv.value - 1) == 0:
                        conds.append(selsig.slice(cv.value.bit_length() - 1, 1))
                    else:
                        conds.append(self.b.eq(selsig, ls))
                c = conds[0]
                for x in conds[1:]:
                    c = self.b.or_(c, x, 1)
                arms.append((c, (lambda body=body: run(body))))
            blk.if_chain(arms, default_fn, origin=o)
            return
        for labs, body in exp:
            if any(l is None for l in labs):
                default_fn = (lambda body=body: run(body))
                labs = [l for l in labs if l is not None]
                if not labs:
                    continue
            if labs:
                items.append(([self.label_sig(l, sel, selsig) for l in labs], (lambda body=body: run(body))))
        blk.case_(selsig, items, default_fn, origin=o)

    def st_select(self, xc, s, sc):
        lv = self.lvalue(s.target, sc)
        sel = self.eval(s.selector, sc)
        hint = lv.t
        vals = {}
        tri = False
        alts = []
        for a in s.alts:
            if a.values and isinstance(a.values[0], N.Unaffected):
                v = None
            else:
                v = self.eval(a.values[0], sc, hint)
                tri = tri or bool(v.z)
            alts.append((a.choices, v))
        if sel.c is not None and sel.sig is None and sel.t.kind in ("int", "enum"):
            for choices, v in alts:
                for lab in self.sel_choices(choices, sel, sc):
                    if lab is None or (lab[0] == "v" and lab[1] == sel.c) or (lab[0] == "val" and lab[1].c == sel.c):
                        if v is not None:
                            self.do_assign(xc, lv, v, s)
                        return
            return
        selsig = self.sig_of(sel)
        if tri:
            self.select_tri(xc, lv, selsig, sel, alts, sc, s)
            return
        if not getattr(s, "matching", False) and self.rom_from_alts(xc, lv, sel, selsig, alts, sc, s):
            return
        self.exec_case(xc, selsig, sel, [(ch, v) for ch, v in alts], sc, s,
                       run=lambda v: self.do_assign(xc, lv, v, s) if v is not None else None)

    def select_tri(self, xc, lv, selsig, sel, alts, sc, node):
        res = None
        conds = []
        default = None
        for choices, v in alts:
            labs = self.sel_choices(choices, sel, sc)
            if any(l is None for l in labs):
                default = v
                continue
            c = None
            for l in labs:
                e1 = self.b.eq(selsig, self.label_sig(l, sel, selsig))
                c = e1 if c is None else self.b.or_(c, e1, 1)
            conds.append((c, v))
        res = default if default is not None else Val(lv.t, SigSpec.x(lv.width), None, mask(lv.width))
        for c, v in reversed(conds):
            res = self.cond_mux(c, res, v, node)
        self.do_assign(xc, lv, res, node)

    def tri_rewrite(self, s):
        """if/else chains assigning 'Z' in some branch become conditional assignments (tri-state MUX)."""
        from .conc import expr_key, is_z_expr
        if s.else_body is None:
            return None
        bodies = [(br.cond, br.body) for br in s.branches] + [(None, s.else_body)]
        maps = []
        for cond, body in bodies:
            m = {}
            for st in body:
                if not isinstance(st, N.SignalAssign) or len(st.alts) != 1 or st.alts[0].cond is not None:
                    return None
                k = expr_key(st.target)
                if k in m:
                    return None
                m[k] = st
            maps.append(m)
        keys = list(maps[0])
        if not keys or any(set(m) != set(keys) for m in maps):
            return None
        if not any(is_z_expr(m[k].alts[0].values[0]) for m in maps for k in keys):
            return None
        out = []
        for k in keys:
            alts = [N.Alt([m[k].alts[0].values[0]], cond, line=s.line, col=s.col) for (cond, _), m in zip(bodies, maps)]
            st0 = maps[0][k]
            out.append(N.SignalAssign(None, st0.target, alts, st0.kind, False, line=st0.line, col=st0.col))
        return out

    def const_expr(self, e, sc):
        """True when ``e`` mentions no signal, port or variable (a constant such as a reset value)."""
        from ...frontend.vhdl.nodes import walk
        for n in walk(e):
            if isinstance(n, N.Ident):
                syms = sc.lookup(n.name)
                if syms and syms[0].kind in ("obj", "mem", "alias"):
                    return False
        return True

    def reset_like(self, body, sc):
        """A branch that only assigns constants (a reset arm)."""
        if not body:
            return False
        for st in body:
            if not isinstance(st, N.SignalAssign) or len(st.alts) != 1 or st.alts[0].cond is not None:
                return False
            if not all(self.const_expr(v, sc) for v in st.alts[0].values):
                return False
        return True

    def st_if(self, xc, s, sc):
        rw = self.tri_rewrite(s)
        if rw is not None:
            for st in rw:
                self.st_assign(xc, st, sc)
            return
        if xc.clocked and len(s.branches) >= 2 and self.reset_like(s.branches[0].body, sc):
            # peel a reset-like arm so that reset / enable recognition stays possible (IR.md section 5)
            rest = N.IfStmt(s.label, s.branches[1:], s.else_body, line=s.line, col=s.col)
            c0 = self.cond_bit(self.eval(s.branches[0].cond, sc))
            xc.blk.if_chain([(c0, lambda: self.run_stmts(xc, s.branches[0].body, sc))],
                            lambda: self.st_if(xc, rest, sc), origin=self.org(s))
            return
        arms = []
        for br in s.branches:
            c = self.eval(br.cond, sc)
            arms.append((self.cond_bit(c), (lambda body=br.body: self.run_stmts(xc, body, sc))))
        els = (lambda: self.run_stmts(xc, s.else_body, sc)) if s.else_body else None
        xc.blk.if_chain(arms, els, origin=self.org(s))

    def st_null(self, xc, s, sc):
        return

    # ---- loops
    def loop_flag(self, xc, name):
        net = self.b.net(1, name)
        xc.blk.mark_variable(net)
        xc.locals.append(net)
        xc.blk.assign(net, SigSpec.const(0, 1))
        return net

    def st_for(self, xc, s, sc):
        l, r, d = self.eval_range(s.range, sc)
        if d == "to":
            vals = range(l, r + 1)
        else:
            vals = range(l, r - 1, -1)
        lim = self.ctx.limits["loop_unroll"]
        n = len(vals)
        if n > lim:
            self.diag("LIMIT_REACHED", "for loop with %d iterations exceeds the unroll limit %d" % (n, lim), s)
            vals = vals[:lim]
        self.loop_body(xc, s, sc, ((s.var, v) for v in vals), s.body)

    def loop_body(self, xc, s, sc, it, body):
        has_ctl = contains_ctl(s)
        label = getattr(s, "label", None)
        lp = None
        if has_ctl:
            ex = self.loop_flag(xc, "loop_exit")
            lp = {"label": label, "exit": ex, "next": None}
            xc.loops.append(lp)
        try:
            # nested unrolling multiplies (1024 x 1024 ...), and every exit/next flag makes each later
            # iteration costlier (quadratic): bound the work per process
            budget = self.ctx.limits.get("process_loop_iterations", 16384)
            cbudget = self.ctx.limits.get("process_ctl_loop_iterations", 1024)
            for var, v in it:
                self.loop_iters += 1
                if has_ctl:
                    self.ctl_iters += 1
                if self.loop_iters > budget or self.ctl_iters > cbudget:
                    self.diag("LIMIT_REACHED", "loop unrolling budget of %d iterations per process exhausted; "
                              "remaining iterations skipped" % (cbudget if self.ctl_iters > cbudget else budget), s)
                    break
                isc = Scope(sc)
                if var is not None:
                    isc.declare(Sym("const", var, T.T_UINT, val=int_val(v)))
                if lp is not None:
                    stop = self.flags_set(xc, [lp["exit"]] + ([xc.fn.done] if xc.fn and xc.fn.done is not None else []))
                    sc_ = stop.const_value() if stop is not None else None
                    if sc_ is not None and sc_.value:
                        break
                    nx = self.loop_flag(xc, "loop_next")
                    lp["next"] = nx
                    mark = len(xc.touched)
                    if stop is None:
                        self.run_stmts(xc, body, isc)
                    else:
                        xc.blk.if_(self.b.not_(stop, 1), lambda: self.run_stmts(xc, body, isc))
                    xc.touched[:] = [f for f in xc.touched if f is not nx]
                else:
                    self.run_stmts(xc, body, isc)
        finally:
            if lp is not None:
                xc.loops.pop()
                xc.touched[:] = [f for f in xc.touched if f is not lp["exit"]]

    def st_while(self, xc, s, sc):
        lim = self.ctx.limits["loop_unroll"]

        def it():
            n = 0
            while True:
                c = self.eval(s.cond, sc)
                cb = self.cond_bit(c)
                cb = xc.blk.read(cb)
                k = cb.const_value()
                if k is None:
                    self.diag("UNSUPPORTED_CONSTRUCT", "while loop with a non-constant condition is not synthesizable here", s)
                    return
                if not k.value:
                    return
                n += 1
                if n > lim:
                    self.diag("LIMIT_REACHED", "while loop exceeds the unroll limit %d" % lim, s)
                    return
                yield (None, 0)
        self.loop_body(xc, s, sc, it(), s.body)

    def st_loop(self, xc, s, sc):
        lim = self.ctx.limits["loop_unroll"]

        def it():
            for n in range(lim):
                yield (None, 0)
            self.diag("LIMIT_REACHED", "loop without a constant exit exceeds the unroll limit %d" % lim, s)
        # one pass at least; the exit flag stops the unrolling once it is constant
        if not contains(s, (N.ExitStmt,)):
            self.diag("UNSUPPORTED_CONSTRUCT", "infinite loop", s)
            return
        self.loop_body(xc, s, sc, it(), s.body)

    def _find_loop(self, xc, label):
        for lp in reversed(xc.loops):
            if label is None or lp["label"] == label:
                return lp
        return None

    def st_exit(self, xc, s, sc, kind="exit"):
        lp = self._find_loop(xc, s.loop_label)
        if lp is None:
            raise Unsupported("exit/next outside a loop")
        flag = lp["exit"] if kind == "exit" else lp["next"]
        if flag is None:
            raise Unsupported("next outside an unrolled iteration")
        if s.cond is not None:
            c = self.cond_bit(self.eval(s.cond, sc))
            xc.blk.if_(c, lambda: xc.blk.assign(flag, SigSpec.const(1, 1)))
        else:
            xc.blk.assign(flag, SigSpec.const(1, 1))
        xc.touched.append(flag)

    def st_next(self, xc, s, sc):
        self.st_exit(xc, s, sc, "next")

    def st_return(self, xc, s, sc):
        fn = xc.fn
        if fn is None:
            raise Unsupported("return outside a function")
        if s.value is not None:
            rh = fn.ret_t if fn.ret_t is not None and fn.ret_t.width else None
            if rh is None:
                rd = getattr(fn, "ret_decl", None)
                if rd is not None and rd.kind in ("array", "record") and isinstance(s.value, N.Aggregate):
                    rh = rd         # unconstrained return type: lets ``(x'range => '0')`` find its element type
            v = self.eval(s.value, sc, rh)
            if fn.ret_t is not None and fn.ret_t.width and not (fn.ret_t.kind == "array" and fn.ret_t.width is None):
                v = self.fit(v, fn.ret_t, s)
            sig = self.sig_of(v)
            if fn.ret_net is None:
                fn.ret_net = self.b.net(sig.width, "ret")
                fn.ret_t_eff = v.t
                xc.locals.append(fn.ret_net)
                fn.ret_z = 0
            xc.blk.assign(fn.ret_net, sig.resize(fn.ret_net.width, self.is_signed(v)))
            fn.ret_z |= v.z
        if fn.done is None:
            fn.done = self.b.net(1, "done")
            xc.locals.append(fn.done)
            xc.blk.assign(fn.done, SigSpec.const(0, 1))
        xc.blk.assign(fn.done, SigSpec.const(1, 1))
        xc.touched.append(fn.done)

    def st_wait(self, xc, s, sc):
        self.diag("UNSUPPORTED_CONSTRUCT", "wait statement inside a process body is not synthesizable here", s)

    def st_proccall(self, xc, s, sc):
        call = s.call
        if isinstance(call, N.Ident):
            name, args, prefix = call.name, [], call
        elif isinstance(call, N.Apply):
            args, prefix = call.args, call.prefix
        else:
            raise Unsupported("procedure call form")
        syms = self.name_syms(prefix, sc) if isinstance(prefix, (N.Ident, N.Select)) else None
        syms = [x for x in (syms or []) if x.kind == "func"]
        if not syms:
            return   # std.env / textio calls etc.
        self.call_procedure(xc, syms, args, s, sc)

    # ------------------------------------------------------------------ subprograms
    def user_params_match(self, fd, vals, args=None):
        """Do the argument types fit the parameters?  `args` (association list) maps named
        associations onto their formals; without it `vals` is positional."""
        params = fd.node.params
        if args is None:
            if len(params) != len(vals):
                return False
            pairs = list(zip(params, vals))
        else:
            by_name = {a.formal.name: v for a, v in zip(args, vals) if isinstance(a.formal, N.Ident)}
            pos = [v for a, v in zip(args, vals) if a.formal is None]
            pairs = []
            pi = 0
            for p in params:
                if p.name in by_name:
                    pairs.append((p, by_name[p.name]))
                elif pi < len(pos):
                    pairs.append((p, pos[pi]))
                    pi += 1
        for p, v in pairs:
            try:
                pt = self.resolve_subtype(p.subtype, fd.scope)
            except Exception:
                continue
            if pt is v.t:
                continue
            if pt.kind != v.t.kind:
                return False
            if pt.kind == "array" and pt.cls != v.t.cls and pt.cls is not None and v.t.cls is not None:
                return False
            if pt.kind == "array" and pt.elem is not None and v.t.elem is not None and pt.elem.kind != v.t.elem.kind:
                return False        # e.g. a 2-D matrix type (array of arrays) against a plain vector
            if pt.kind == "enum" and pt.lits is not v.t.lits and pt.cls != v.t.cls:
                return False
        return True

    def ret_fits(self, fd, hint):
        """Does the return type of ``fd`` fit the expected type ``hint`` (overloads that differ by result only)?"""
        rn = fd.node.return_type
        if rn is None or hint is None:
            return True
        try:
            rt = self.resolve_subtype(rn, fd.scope)
        except Exception:
            return True
        if rt.kind != hint.kind:
            return False
        if rt.kind == "array":
            return rt.cls == hint.cls or rt.cls is None or hint.cls is None
        if rt.kind == "enum":
            return rt.lits is hint.lits or (rt.cls is not None and rt.cls == hint.cls)
        return True

    def pick_overload(self, syms, args, sc, hint=None):
        cands = []
        n = len(args)
        for s in syms:
            fd = s.val
            if fd is None:
                continue
            ps = fd.node.params
            req = sum(1 for p in ps if p.default is None)
            if req <= n <= len(ps):
                if all(a.formal is None or any(isinstance(a.formal, N.Ident) and a.formal.name == p.name for p in ps) for a in args):
                    cands.append(s)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0].val
        # prefer matching argument types, then the expected result type
        vals = []
        try:
            vals = [self.eval(a.actual, sc) for a in args]
        except Exception:
            pass
        if len(vals) == n:
            ok = []
            if all(a.formal is None for a in args):
                ok = [s for s in cands if len(s.val.node.params) == n and self.user_params_match(s.val, vals)]
            if not ok:
                # named associations, or parameters left to their defaults: match the supplied ones
                ok = [s for s in cands if self.user_params_match(s.val, vals, args)]
            if ok:
                if len(ok) > 1 and hint is not None:
                    ok = [s for s in ok if self.ret_fits(s.val, hint)] or ok
                return ok[0].val
        if hint is not None:
            fit = [s for s in cands if self.ret_fits(s.val, hint)]
            if fit:
                return fit[-1].val
        return cands[-1].val

    def user_overload_fits(self, syms, args, sc):
        """False only when every user overload of arity-compatible shape is ruled out by the argument types
        (a user ``resize(T_SLM, ...)`` must not hide ``numeric_std.resize``)."""
        n = len(args)
        cands = []
        for s in syms:
            fd = s.val
            if fd is None:
                continue
            ps = fd.node.params
            req = sum(1 for p in ps if p.default is None)
            if req <= n <= len(ps) and all(
                    a.formal is None or any(isinstance(a.formal, N.Ident) and a.formal.name == p.name for p in ps)
                    for a in args):
                cands.append(fd)
        if not cands:
            return False
        try:
            vals = [self.eval(a.actual, sc) for a in args]
        except Exception:
            return True
        return any(self.user_params_match(fd, vals, args) for fd in cands)

    def bind_params(self, fd, args, sc, fscope, xc_caller=None, procedure=False, outer_xc=None):
        node = fd.node
        ps = node.params
        by_name = {}
        pos = []
        for a in args:
            if a.formal is not None and isinstance(a.formal, N.Ident):
                by_name[a.formal.name] = a
            else:
                pos.append(a)
        post = []
        pi = 0
        for p in ps:
            a = by_name.get(p.name)
            if a is None and pi < len(pos):
                a = pos[pi]
                pi += 1
            elif a is None and p.name not in by_name:
                pass
            pt = None
            try:
                pt = self.resolve_subtype(p.subtype, fscope) if p.subtype is not None else None
            except Exception:
                pt = None
            if a is None:
                if p.default is None:
                    raise Unsupported("missing argument for parameter '%s'" % p.name)
                v = self.eval(p.default, fscope, pt)
                fscope.declare(Sym("const", p.name, v.t, val=self.fit(v, pt) if pt is not None and pt.width is not None else v))
                continue
            mode = p.mode or "in"
            if procedure and mode in ("out", "inout") or (procedure and p.cls in ("signal", "variable") and mode != "in"):
                lv = self.lvalue(a.actual, sc)
                if lv.parts is None and lv.mem is None and not lv.dyn:
                    s2 = Sym("obj", p.name, lv.t or pt, sig=self.lv_static_sig(lv))
                    s2.mode = lv.sym.mode if lv.sym is not None else "signal"
                    s2.owner = lv.sym.owner if lv.sym is not None else None
                    fscope.declare(s2)
                else:
                    t = lv.t or pt
                    net = self.b.net(lv.width, p.name)
                    xc_caller.locals.append(net)
                    xc_caller.blk.mark_variable(net)
                    s2 = Sym("obj", p.name, t, sig=net.sig)
                    s2.mode = "variable"
                    s2.owner = xc_caller
                    fscope.declare(s2)
                    if mode == "inout":
                        xc_caller.blk.assign(net, self.sig_of(self.eval(a.actual, sc, t)))
                    post.append((lv, net, t))
                continue
            if procedure and p.cls == "signal":
                try:
                    lv = self.lvalue(a.actual, sc)
                    s2 = Sym("obj", p.name, lv.t or pt, sig=self.lv_static_sig(lv))
                    s2.mode = "signal"
                    fscope.declare(s2)
                    continue
                except Exception:
                    pass
            v = self.eval(a.actual, sc, pt)
            if pt is not None and pt.kind in ("int", "real", "enum") and pt.width is not None or (pt is not None and pt.kind == "array" and pt.width is not None):
                v = self.fit(v, pt)
            fscope.declare(Sym("const", p.name, v.t, val=v))
        return post

    def call_user(self, syms, args, e, sc, hint):
        fd = self.pick_overload(syms, args, sc, hint)
        if fd is None:
            self.diag("UNRESOLVED_OVERLOAD", "no matching overload of '%s'" % syms[0].name, e)
            return self.unknown(hint)
        node = fd.node
        if node.kind != "function":
            raise Unsupported("procedure used as a function")
        body = fd.body_node
        if body is None or not body.body and not body.decls and not body.has_body:
            self.diag("UNSUPPORTED_CONSTRUCT", "function '%s' has no body" % node.name, e)
            return self.unknown(hint)
        if self.call_depth >= self.ctx.limits["inline_depth"]:
            self.diag("LIMIT_REACHED", "function inlining depth %d reached at '%s'" % (self.ctx.limits["inline_depth"], node.name), e)
            return self.unknown(hint)
        self.call_depth += 1
        xc = None
        if self.call_depth == 1:
            self.steps = 0
            if self.proc is None:
                self.loop_iters = self.ctl_iters = 0
        saved_file = self.file
        saved_origin = self.b.origin
        try:
            fscope = Scope(fd.scope)
            self.file = fscope.file or self.file
            blk = LowerBlock(self.b, "comb", origin=self.org(e))
            xc = XC(blk, False, None, "function")
            ret_t = self.resolve_subtype(node.return_type, fscope) if node.return_type is not None else None
            xc.fn = FnCtx(ret_t if ret_t is not None and ret_t.width is not None else None)
            xc.fn.ret_decl = ret_t
            self.bind_params(fd, args, sc, fscope, xc)
            self.declare_items(body.decls, fscope, xc)
            self.run_stmts(xc, body.body, fscope)
            fn = xc.fn
            if fn.ret_net is None:
                self.diag("UNSUPPORTED_CONSTRUCT", "function '%s' returns no value on this path" % node.name, e)
                return self.unknown(hint)
            sig = blk.read(fn.ret_net.sig)
            rt = ret_t if (ret_t is not None and ret_t.width == sig.width) else getattr(fn, "ret_t_eff", ret_t)
            if rt is not None and rt.kind == "array" and rt.width is None:
                n = sig.width // (rt.ewidth or 1)
                rt = T.constrain(rt, n - 1, 0, "downto")
            if rt is None:
                rt = T.T_STD_LOGIC
            v = Val(rt, sig, None, getattr(fn, "ret_z", 0))
            if rt.kind == "int" and sig.const_value() is not None and not sig.const_value().xmask:
                c = sig.const_value()
                v.c = c.as_signed() if rt.signed else c.value
                v.sig = None if rt.width is None else sig
            elif rt.kind == "enum" and sig.const_value() is not None and rt.enc:
                cv = sig.const_value()
                for i, (ev, ex) in enumerate(rt.enc):
                    if ev == cv.value and ex == cv.xmask:
                        v.c = i
                        break
            return v
        except StopLimit:
            self.diag("LIMIT_REACHED", "constant-function interpreter step limit reached in '%s'" % node.name, e)
            return self.unknown(hint)
        finally:
            self.call_depth -= 1
            self.file = saved_file
            self.b.origin = saved_origin
            self.drop_locals(xc)

    def drop_locals(self, xc):
        if xc is None:
            return
        for net in xc.locals:
            if net.fanout == 0 and net.driver is None:
                net.driver = "dead"
                self.b.module.driven.pop(net.id, None)

    def call_procedure(self, xc, syms, args, e, sc):
        fd = self.pick_overload(syms, args, sc)
        if fd is None:
            self.diag("UNRESOLVED_OVERLOAD", "no matching overload of procedure '%s'" % syms[0].name, e)
            return
        node = fd.node
        body = fd.body_node
        if body is None:
            self.diag("UNSUPPORTED_CONSTRUCT", "procedure '%s' has no body" % node.name, e)
            return
        if self.call_depth >= self.ctx.limits["inline_depth"]:
            self.diag("LIMIT_REACHED", "procedure inlining depth reached at '%s'" % node.name, e)
            return
        self.call_depth += 1
        saved_file = self.file
        try:
            fscope = Scope(fd.scope)
            self.file = fscope.file or self.file
            sub = XC(xc.blk, xc.clocked, xc, "procedure")
            sub.loops = []
            post = self.bind_params(fd, args, sc, fscope, xc, procedure=True)
            self.declare_items(body.decls, fscope, sub)
            sub.fn = FnCtx(None)
            sub.fn.ret_net = None
            sub.fn.ret_t = None
            self.run_stmts(sub, body.body, fscope)
            for lv, net, t in post:
                self.do_assign(xc, lv, Val(t, xc.blk.read(net.sig)), e)
        finally:
            self.call_depth -= 1
            self.file = saved_file
            # drop the procedure's local variables from the caller's environment
            env = xc.blk.env
            for net in sub.locals:
                env.assigned.pop(net.id, None)
                env.val.pop(net.id, None)
                env.vis.pop(net.id, None)
            self.drop_locals(sub)


def contains_ctl(node):
    from ...frontend.vhdl.nodes import walk
    for n in walk(node):
        if isinstance(n, (N.ExitStmt, N.NextStmt, N.ReturnStmt)) and n is not node:
            return True
    return False


_STMT = {
    N.SignalAssign: StmtMixin.st_assign, N.SelectAssign: StmtMixin.st_select, N.IfStmt: StmtMixin.st_if,
    N.CaseStmt: StmtMixin.st_case, N.ForLoop: StmtMixin.st_for, N.WhileLoop: StmtMixin.st_while,
    N.LoopStmt: StmtMixin.st_loop, N.ExitStmt: StmtMixin.st_exit, N.NextStmt: StmtMixin.st_next,
    N.ReturnStmt: StmtMixin.st_return, N.NullStmt: StmtMixin.st_null, N.WaitStmt: StmtMixin.st_wait,
    N.ProcCall: StmtMixin.st_proccall,
}
