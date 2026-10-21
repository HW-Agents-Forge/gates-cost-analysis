"""Procedural lowering: the symbolic executor interface (IR.md section 5).

A :class:`LowerBlock` models one ``always``/``process`` body.  The front-end walks the
statements and calls ``assign`` / ``if_`` / ``if_chain`` / ``case_``; at the end
``finish`` connects combinational results, infers latches, and turns clocked
assignments into ``DFF`` nodes with async reset, sync reset and enable recognition.
"""
from __future__ import annotations

from typing import Callable, Sequence

from .builder import Builder, IRBuildError
from .core import Net, Node, Origin
from .sigspec import Const, SigSpec, Slice, mask

_MUXLIKE = ("MUX", "PMUX", "PRIORITY")


class Env:
    """Snapshot of the symbolic environment."""

    __slots__ = ("val", "vis", "assigned")

    def __init__(self, val: dict, vis: dict, assigned: dict) -> None:
        self.val = val
        self.vis = vis
        self.assigned = assigned

    def copy(self) -> "Env":
        return Env(dict(self.val), dict(self.vis), dict(self.assigned))


def _self_sig(net: Net) -> SigSpec:
    return SigSpec((Slice(net, 0, net.width),))


def _splice(cur: SigSpec, lo: int, piece: SigSpec) -> SigSpec:
    w = piece.width
    parts = []
    if lo > 0:
        parts.append(cur.slice(0, lo))
    parts.append(piece)
    if lo + w < cur.width:
        parts.append(cur.slice(lo + w))
    return SigSpec.concat(parts)


def _atoms(sig: SigSpec) -> list:
    return list(sig.bits())


class LowerBlock:
    def __init__(self, b: Builder, kind: str, *, clock=None, clk_edge: str = "pos",
                 async_resets: Sequence[tuple] = (), origin: Origin | None = None,
                 name: str | None = None) -> None:
        if kind not in ("comb", "clocked"):
            raise IRBuildError("block kind must be 'comb' or 'clocked'")
        if kind == "clocked" and clock is None:
            raise IRBuildError("clocked block needs a clock")
        self.b = b
        self.kind = kind
        self.clock = None if clock is None else b._s(clock)
        self.clk_edge = clk_edge
        self.async_resets = [(b._s(s), int(l)) for s, l in async_resets]
        self.origin = origin
        self.name = name
        self.env = Env({}, {}, {})
        self._variables: set[int] = set()
        self._path: list[tuple] = []     # (cond SigSpec, polarity)
        self._origin_stack: list[Origin | None] = []
        self._order: dict[int, Net] = {}  # nets in first-assignment order

    # -- origin handling -------------------------------------------------
    def _push_origin(self, origin):
        self._origin_stack.append(self.b.origin)
        if origin is not None:
            self.b.origin = origin
        elif self.b.origin is None:
            self.b.origin = self.origin

    def _pop_origin(self):
        self.b.origin = self._origin_stack.pop()

    # -- environment -----------------------------------------------------
    def snapshot(self) -> Env:
        return self.env.copy()

    def restore(self, env: Env) -> None:
        self.env = env.copy()

    def mark_variable(self, net: Net) -> None:
        """Treat ``net`` as a block-local variable (VHDL variable): combinational even
        in a clocked block."""
        self._variables.add(net.id)

    def read(self, x) -> SigSpec:
        """Value seen by a read now: blocking assignments are visible, non-blocking
        ones are not."""
        sig = self.b._s(x)
        vis = self.env.vis
        if not vis:
            return sig
        out = []
        for c in sig.chunks:
            if isinstance(c, Slice) and c.net.id in vis:
                out.extend(vis[c.net.id].slice(c.lo, c.width).chunks)
            else:
                out.append(c)
        return SigSpec(out)

    def assign(self, lhs, rhs, nonblocking: bool = False, signed: bool = False,
               origin: Origin | None = None) -> None:
        """``lhs = rhs`` / ``lhs <= rhs``.  ``lhs`` is a net or a SigSpec of net slices
        (concatenations allowed).  ``rhs`` is resized to the lhs width."""
        lhs = self.b._s(lhs)
        rhs = self.b._s(rhs).resize(lhs.width, signed)
        pos = 0
        for ch in lhs.chunks:
            if not isinstance(ch, Slice):
                raise IRBuildError("assignment target must be net slices")
            net = ch.net
            piece = rhs.slice(pos, ch.width)
            pos += ch.width
            e = self.env
            cur = e.val.get(net.id)
            e.val[net.id] = _splice(cur if cur is not None else _self_sig(net), ch.lo, piece)
            if nonblocking and self.kind == "clocked":
                pass
            else:
                cv = e.vis.get(net.id)
                e.vis[net.id] = _splice(cv if cv is not None else _self_sig(net), ch.lo, piece)
            e.assigned[net.id] = e.assigned.get(net.id, 0) | (mask(ch.width) << ch.lo)
            self._order.setdefault(net.id, net)

    def assign_indexed(self, net: Net, index, elem_width: int, count: int, value,
                       nonblocking: bool = False, origin: Origin | None = None) -> None:
        """``net[index*elem_width +: elem_width] = value`` with a non-constant index.

        Expanded to an exclusive case over the ``count`` possible positions."""
        idx = self.b._s(index)
        items = []
        for j in range(count):
            def fn(j=j):
                self.assign(SigSpec.of(net, j * elem_width, elem_width), value, nonblocking)
            items.append(([SigSpec.const(j, idx.width)], fn))
        self.case_(idx, items, None, origin=origin)

    def mem_write(self, mem, addr, data, en=None, origin: Origin | None = None) -> None:
        """Write port of a memory, enabled by the current path condition (and ``en``)."""
        cond = self.path_condition()
        if en is not None:
            en = self.b._s(en)
            cond = en if cond is None else self.b.and_(cond, en, 1)
        mem.write(addr, self.read(data), en=cond, clk=self.clock, clk_edge=self.clk_edge,
                  origin=origin)

    def path_condition(self) -> SigSpec | None:
        """AND of the conditions leading to the current statement (None = unconditional)."""
        cond = None
        for c, pol in self._path:
            t = c if pol else self.b.not_(c, 1)
            cond = t if cond is None else self.b.and_(cond, t, 1)
        return cond

    # -- condition helpers -------------------------------------------------
    def _bool(self, c) -> SigSpec:
        c = self.b._s(c)
        if c.width == 1:
            return c
        return self.b.red_or(c)

    @staticmethod
    def _const_bool(c: SigSpec) -> bool | None:
        k = c.const_value()
        if k is None:
            return None
        if k.xmask:
            return False
        return bool(k.value)

    # -- if / elsif / else -------------------------------------------------
    def if_(self, cond, then_fn: Callable | None = None, else_fn: Callable | None = None,
            origin: Origin | None = None) -> None:
        self.if_chain([(cond, then_fn)], else_fn, origin)

    def if_chain(self, arms: Sequence[tuple], else_fn: Callable | None = None,
                 origin: Origin | None = None) -> None:
        """``if c0 .. elsif c1 .. else ..``.  Conditions known to be mutually exclusive
        (EQ of one signal with distinct constants) give a PMUX, otherwise a PRIORITY."""
        self._push_origin(origin)
        try:
            self._if_chain(arms, else_fn)
        finally:
            self._pop_origin()

    def _if_chain(self, arms, else_fn) -> None:
        arms = [(self._bool(c), fn) for c, fn in arms]
        base = self.snapshot()
        live: list[tuple] = []
        default_fn = else_fn
        for c, fn in arms:
            kb = self._const_bool(c)
            if kb is None:
                live.append((c, fn))
            elif kb:
                default_fn = fn
                break
        if not live:
            if default_fn:
                default_fn()
            return
        envs = []
        for i, (c, fn) in enumerate(live):
            self.restore(base)
            mark = len(self._path)
            for j in range(i):
                self._path.append((live[j][0], 0))
            self._path.append((c, 1))
            if fn:
                fn()
            del self._path[mark:]
            envs.append(self.snapshot())
        self.restore(base)
        mark = len(self._path)
        for c, _ in live:
            self._path.append((c, 0))
        if default_fn:
            default_fn()
        del self._path[mark:]
        env_def = self.snapshot()
        exclusive = len(live) == 1 or _exclusive_conds([c for c, _ in live])
        self._merge(envs, env_def, [c for c, _ in live], exclusive)

    # -- case ---------------------------------------------------------------
    def case_(self, sel, items: Sequence[tuple], default_fn: Callable | None = None,
              kind: str = "case", origin: Origin | None = None) -> None:
        """``case (sel)``.  ``items`` is a list of ``(labels, fn)``; each label is a
        Const/SigSpec.  Don't-care bits of a label (``xmask``) are wildcards (casez/casex,
        VHDL ``-``): only the known bits are compared."""
        self._push_origin(origin)
        try:
            self._case(self.b._s(sel), items, default_fn)
        finally:
            self._pop_origin()

    def _label_hit(self, sel: SigSpec, label) -> SigSpec:
        b = self.b
        lab = b._s(label)
        c = lab.const_value()
        if c is None:
            return b.eq(sel, lab)
        if c.width != sel.width:
            c = Const(c.value, sel.width, c.xmask)
        km = c.kmask
        if km == 0:
            return SigSpec.const(1, 1)
        # gather the compared bit runs
        parts_s, parts_c = [], []
        i = 0
        w = sel.width
        while i < w:
            if not (km >> i) & 1:
                i += 1
                continue
            j = i
            while j < w and (km >> j) & 1:
                j += 1
            parts_s.append(sel.slice(i, j - i))
            parts_c.append(SigSpec.const(c.value >> i, j - i))
            i = j
        return b.eq(SigSpec.concat(parts_s), SigSpec.concat(parts_c))

    def _case(self, sel: SigSpec, items, default_fn) -> None:
        b = self.b
        # hit conditions
        hits: list[SigSpec] = []
        pats: list[list] = []
        all_const = True
        for labels, _ in items:
            hs = []
            ps = []
            for lab in labels:
                lab_s = b._s(lab)
                hs.append(self._label_hit(sel, lab_s))
                c = lab_s.const_value()
                if c is None:
                    all_const = False
                else:
                    ps.append(Const(c.value, sel.width, c.xmask) if c.width != sel.width else c)
            h = hs[0]
            for x in hs[1:]:
                h = b.or_(h, x, 1)
            hits.append(h)
            pats.append(ps)
        base = self.snapshot()
        # constant selector: execute the matching item only
        live = []
        for idx_, ((labels, fn), h) in enumerate(zip(items, hits)):
            kb = self._const_bool(h)
            if kb is False:
                continue
            live.append(((labels, fn), h, kb, idx_))
        stop = None
        for i, (it, h, kb, _ix) in enumerate(live):
            if kb:
                stop = i
                break
        if stop is not None:
            default_fn = live[stop][0][1]
            live = live[:stop]
        if not live:
            if default_fn:
                default_fn()
            return
        # exclusivity / exhaustiveness (only decidable for constant labels)
        exclusive = False
        exhaustive = False
        if all_const and stop is None:
            allp = [p for (_it, _h, _k, ix) in live for p in pats[ix]]
            exclusive = _patterns_disjoint(allp)
            if exclusive and sel.width <= 16 and default_fn is None:
                covered = sum(1 << bin(mask(sel.width) & p.xmask).count("1") for p in allp)
                exhaustive = covered >= (1 << sel.width)
        elif stop is None:
            exclusive = False
        else:
            ps = [p for (_it, _h, _k, ix) in live for p in pats[ix]]
            exclusive = all_const and _patterns_disjoint(ps)
        envs = []
        for i, ((labels, fn), h, _k, _ix) in enumerate(live):
            self.restore(base)
            mark = len(self._path)
            if exclusive:
                self._path.append((h, 1))
            else:
                for j in range(i):
                    self._path.append((live[j][1], 0))
                self._path.append((h, 1))
            if fn:
                fn()
            del self._path[mark:]
            envs.append(self.snapshot())
        self.restore(base)
        mark = len(self._path)
        for _it, h, _k, _ix in live:
            self._path.append((h, 0))
        if exhaustive:
            env_def = envs[-1]
            envs = envs[:-1]
            live = live[:-1]
        else:
            if default_fn:
                default_fn()
            env_def = self.snapshot()
        del self._path[mark:]
        if not live:
            self.env = env_def.copy()
            return
        self._merge(envs, env_def, [h for _it, h, _k, _ix in live], exclusive)

    # -- merging --------------------------------------------------------------
    def _merge(self, envs: list[Env], env_def: Env, selects: list[SigSpec],
               exclusive: bool) -> None:
        """Combine branch environments with MUX / PMUX / PRIORITY nodes (one group)."""
        g = self.b.new_group()
        # a branch on an asynchronous-reset signal must keep its MUX over the whole value,
        # even for bits where reset and normal value coincide (the reset still exists)
        full = any(_cond_level(s, a) is not None for s in selects for a, _l in self.async_resets)
        new = Env({}, {}, {})
        keys: list[int] = []
        seen = set()
        for e in [env_def] + envs:
            for k in list(e.val) + list(e.vis):
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        for e in [env_def] + envs:
            for k, v in e.assigned.items():
                new.assigned[k] = new.assigned.get(k, 0) | v
        nets = self._net_lookup
        for k in keys:
            net = nets(k)
            for kind in ("val", "vis"):
                vals = []
                for e in envs:
                    d = getattr(e, kind)
                    vals.append(d.get(k) if k in d else _self_sig(net))
                dd = getattr(env_def, kind)
                dv = dd.get(k) if k in dd else _self_sig(net)
                if all(v == dv for v in vals):
                    if k in dd or any(k in getattr(e, kind) for e in envs):
                        getattr(new, kind)[k] = dv
                    continue
                getattr(new, kind)[k] = self._merge_value(net.width, vals, dv, selects,
                                                          exclusive, g, full)
        self.env = new

    def _net_lookup(self, nid: int) -> Net:
        return self._order[nid]

    def _merge_value(self, w: int, vals: list[SigSpec], dv: SigSpec, selects: list[SigSpec],
                     exclusive: bool, group: int, full: bool = False) -> SigSpec:
        b = self.b
        datoms = _atoms(dv)
        aatoms = [_atoms(v) for v in vals]
        diff = [full or any(a[i] != datoms[i] for a in aatoms) for i in range(w)]
        pieces = []
        i = 0
        while i < w:
            j = i
            d = diff[i]
            while j < w and diff[j] == d:
                j += 1
            if not d:
                pieces.append(dv.slice(i, j - i))
            else:
                n = j - i
                dflt = dv.slice(i, n)
                arms = []
                for v, s in zip(vals, selects):
                    sl = v.slice(i, n)
                    if exclusive and sl == dflt:
                        continue
                    arms.append((s, sl))
                if len(arms) == 1:
                    pieces.append(b.op("MUX", dflt, arms[0][1], arms[0][0], width=n, group=group,
                                       raw=True))
                else:
                    op = "PMUX" if exclusive else "PRIORITY"
                    pieces.append(b.op(op, dflt, SigSpec.concat([a for _, a in arms]),
                                       SigSpec.concat([s for s, _ in arms]), width=n,
                                       k=len(arms), group=group, raw=True))
            i = j
        return SigSpec.concat(pieces)

    # -- finalization -----------------------------------------------------------
    def finish(self) -> None:
        """Connect combinational results, infer latches, create flops."""
        b = self.b
        saved = b.origin
        if self.origin is not None:
            b.origin = self.origin
        try:
            for nid in sorted(self.env.assigned):
                net = self._order[nid]
                amask = self.env.assigned[nid]
                final = self.env.val[nid]
                i = 0
                while i < net.width:
                    if not (amask >> i) & 1:
                        i += 1
                        continue
                    j = i
                    while j < net.width and (amask >> j) & 1:
                        j += 1
                    run = final.slice(i, j - i)
                    if self.kind == "clocked" and nid not in self._variables:
                        self._finish_reg(net, i, run)
                    else:
                        self._finish_comb(net, i, run)
                    i = j
        finally:
            b.origin = saved

    # write-enable analysis -----------------------------------------------------
    # A "we map" is a list of (width, cond) segments covering a SigSpec; cond is a 1-bit
    # SigSpec that is true when those bits are written (do not keep the previous value of
    # the target net).  ``None`` stands for "written on every path".
    def _we(self, sig: SigSpec, net: Net, lo: int, memo: dict):
        parts = []
        found = False
        pos = 0
        for c in sig.chunks:
            if isinstance(c, Const):
                parts.append([(c.width, _ONE)])
            else:
                h = self._we_chunk(c, net, lo + pos, memo)
                if h is None:
                    parts.append([(c.width, _ONE)])
                else:
                    found = True
                    parts.append(h)
            pos += c.width
        if not found:
            return None
        return _coalesce([seg for p in parts for seg in p])

    def _combine(self, maps: list, fn) -> list:
        """Align segment maps of equal total width and combine the conditions with ``fn``."""
        cum = []
        for mp in maps:
            acc, lst = 0, []
            for w, _c in mp:
                acc += w
                lst.append(acc)
            cum.append(lst)
        bounds = sorted({e for lst in cum for e in lst})
        out = []
        idx = [0] * len(maps)
        prev = 0
        for e in bounds:
            conds = []
            for i, mp in enumerate(maps):
                while cum[i][idx[i]] <= prev:
                    idx[i] += 1
                conds.append(mp[idx[i]][1])
            out.append((e - prev, fn(conds)))
            prev = e
        return _coalesce(out)

    def _we_chunk(self, c: Slice, net: Net, abs_pos: int, memo: dict):
        if c.net is net:
            if c.lo == abs_pos:
                return [(c.width, _ZERO)]
            return None
        d = c.net.driver
        if not isinstance(d, Node) or d.op not in _MUXLIKE or not d.alive:
            return None
        key = ('h', c.net.id, c.lo, c.width, abs_pos)
        if key in memo:
            return memo[key]
        memo[key] = None
        b = self.b
        w = c.width
        ins = dict(d.inputs)
        A = ins["A"].slice(c.lo, w)
        S = ins["S"]
        one = [(w, _ONE)]
        if d.op == "MUX":
            hA = self._we(A, net, abs_pos, memo)
            hB = self._we(ins["B"].slice(c.lo, w), net, abs_pos, memo)
            if hA is None and hB is None:
                return None
            r = self._combine([hA or one, hB or one], lambda cs: _mux1(b, S, cs[0], cs[1]))
        else:
            k = d.attrs["k"]
            ow = d.outputs[0].width
            arms = [ins["B"].slice(i * ow + c.lo, w) for i in range(k)]
            hs = [self._we(a, net, abs_pos, memo) for a in arms]
            hd = self._we(A, net, abs_pos, memo)
            if hd is None and all(h is None for h in hs):
                return None

            def fn(cs, S=S, k=k):
                h = cs[0]
                for i in reversed(range(k)):
                    h = _mux1(b, S.slice(i, 1), h, cs[1 + i])
                return h
            r = self._combine([hd or one] + [h or one for h in hs], fn)
        memo[key] = r
        return r

    def _strip(self, sig: SigSpec, net: Net, lo: int, memo: dict) -> SigSpec:
        """``sig`` with the hold references replaced by don't-care."""
        parts = []
        pos = 0
        for c in sig.chunks:
            if isinstance(c, Const):
                parts.append(c)
            else:
                parts.append(self._strip_chunk(c, net, lo + pos, memo))
            pos += c.width
        return SigSpec.concat(parts)

    def _strip_chunk(self, c: Slice, net: Net, abs_pos: int, memo: dict) -> SigSpec:
        if c.net is net:
            if c.lo == abs_pos:
                return SigSpec.x(c.width)
            return SigSpec((c,))
        d = c.net.driver
        if not isinstance(d, Node) or d.op not in _MUXLIKE or not d.alive:
            return SigSpec((c,))
        key = ('s', c.net.id, c.lo, c.width, abs_pos)
        if key in memo:
            return memo[key]
        b = self.b
        w = c.width
        ins = dict(d.inputs)
        S = ins["S"]
        grp = d.tags.get("group")
        A = ins["A"].slice(c.lo, w)
        if d.op == "MUX":
            Bv = ins["B"].slice(c.lo, w)
            sA = self._strip(A, net, abs_pos, memo)
            sB = self._strip(Bv, net, abs_pos, memo)
            if sA == A and sB == Bv:
                r = SigSpec((c,))
            else:
                r = b.op("MUX", sA, sB, S, width=w, group=grp)
        else:
            k = d.attrs["k"]
            ow = d.outputs[0].width
            arms = [ins["B"].slice(i * ow + c.lo, w) for i in range(k)]
            sarms = [self._strip(a, net, abs_pos, memo) for a in arms]
            sA = self._strip(A, net, abs_pos, memo)
            if sA == A and sarms == arms:
                r = SigSpec((c,))
            else:
                r = b.op(d.op, sA, SigSpec.concat(sarms), S, width=w, k=k, group=grp)
        memo[key] = r
        return r

    # combinational -------------------------------------------------------------
    def _finish_comb(self, net: Net, lo: int, run: SigSpec) -> None:
        b = self.b
        memo: dict = {}
        hold = self._we(run, net, lo, memo)
        if hold is None:
            b.connect(SigSpec.of(net, lo, run.width), run)
            return
        i = 0
        for n, hb in hold:
            seg = run.slice(i, n)
            dst = SigSpec.of(net, lo + i, n)
            hc = hb.const_value()
            if hc is not None and hc.value == 0:
                # never assigned on any path: no driver
                b.module.diag("INFERRED_LATCH",
                              f"signal {net.name or net.id}[{lo+i+n-1}:{lo+i}] is never assigned",
                              "warning", False, b.origin)
            elif hc is not None and hc.value == 1:
                b.connect(dst, seg)
            else:
                en = hb
                d = self._strip(seg, net, lo + i, memo)
                q = b.latch(d, en, inferred=True, origin=b.origin)
                b.connect(dst, q)
                b.module.diag("INFERRED_LATCH",
                              f"latch inferred for {net.name or net.id}[{lo+i+n-1}:{lo+i}]: "
                              "not assigned on every path", "warning", False, b.origin)
            i += n
        b.discard(run)

    # clocked -----------------------------------------------------------------------
    def _finish_reg(self, net: Net, lo: int, run: SigSpec) -> None:
        b = self.b
        w = run.width
        memo: dict = {}
        segs = [{"w": w, "d": run, "arst": None, "srst": None, "en": None}]
        # A: asynchronous reset / set
        for (asig, level) in self.async_resets[:1]:
            segs = self._peel(segs, lambda S, A, B, _a=asig, _l=level: self._match_async(S, A, B, _a, _l),
                              "arst")
        # B: synchronous reset (constant arm of the outermost mux)
        segs = self._peel(segs, self._match_sync, "srst")
        # C: clock enable
        segs = self._peel_enable(segs, net, lo, memo)
        # coalesce neighbours with identical controls
        merged: list[dict] = []
        for s in segs:
            if merged and _same_ctrl(merged[-1], s):
                m = merged[-1]
                m["d"] = SigSpec.concat([m["d"], s["d"]])
                m["w"] += s["w"]
                for k in ("arst", "srst"):
                    if m[k] is not None:
                        m[k] = (m[k][0], m[k][1], SigSpec.concat([m[k][2], s[k][2]]))
            else:
                merged.append({**s})
        pos = 0
        for s in merged:
            n = s["w"]
            whole = (lo == 0 and n == net.width and len(merged) == 1)
            qnet = net if whole else None
            ar = s["arst"]
            sr = s["srst"]
            q = b.dff(
                s["d"], self.clock, en=s["en"],
                srst=sr[0] if sr else None, srst_value=sr[2].const_value() if sr else None,
                srst_polarity=sr[1] if sr else 1,
                arst=ar[0] if ar else None, arst_value=ar[2].const_value() if ar else None,
                arst_polarity=ar[1] if ar else 1,
                clk_edge=self.clk_edge, enable_recognized=s["en"] is not None,
                q=qnet, origin=b.origin)
            if not whole:
                b.connect(SigSpec.of(net, lo + pos, n), q)
            pos += n
        b.discard(run)

    # recognizers: return (arm_kind, sig, polarity, reset_arm_sig, normal_arm_sig) or None
    def _match_async(self, S: SigSpec, A: SigSpec, B: SigSpec, asig: SigSpec, level: int):
        ls = _cond_level(S, asig)
        if ls is None:
            return None
        if ls == level:
            rarm, narm = B, A
        else:
            rarm, narm = A, B
        if rarm.const_value() is None:
            return None
        return (asig, level, rarm, narm)

    def _match_sync(self, S: SigSpec, A: SigSpec, B: SigSpec):
        cb, ca = B.const_value(), A.const_value()
        if cb is not None and ca is not None:
            return None
        if cb is not None:
            return (S, 1, B, A)
        if ca is not None:
            return (S, 0, A, B)
        return None

    def _peel(self, segs: list[dict], matcher, field: str) -> list[dict]:
        out = []
        for s in segs:
            if s[field] is not None:
                out.append(s)
                continue
            d = s["d"]
            pos = 0
            for c in d.chunks:
                n = c.width
                sub = dict(s)
                sub["w"] = n
                sub["d"] = SigSpec((c,))
                for other in ("arst", "srst"):
                    r = sub[other]
                    if r is not None:
                        sub[other] = (r[0], r[1], r[2].slice(pos, n))
                if isinstance(c, Slice):
                    drv = c.net.driver
                    if isinstance(drv, Node) and drv.op == "MUX" and drv.alive and \
                            not drv.attrs.get("tristate"):
                        ins = dict(drv.inputs)
                        r = matcher(ins["S"], ins["A"].slice(c.lo, n), ins["B"].slice(c.lo, n))
                        if r is not None:
                            sig, pol, rarm, narm = r
                            sub[field] = (sig, pol, rarm)
                            sub["d"] = narm
                out.append(sub)
                pos += n
        return out

    def _peel_enable(self, segs: list[dict], net: Net, lo: int, memo: dict) -> list[dict]:
        b = self.b
        out = []
        pos = 0
        for s in segs:
            d = s["d"]
            n = s["w"]
            hold = self._we(d, net, lo + pos, memo)
            if hold is None:
                out.append(s)
                pos += n
                continue
            i = 0
            for wseg, hb in hold:
                hc = hb.const_value()
                sub = dict(s)
                sub["w"] = wseg
                seg_d = d.slice(i, wseg)
                if hc is not None:
                    sub["d"] = seg_d
                else:
                    en = hb
                    sub["en"] = en if s["en"] is None else b.and_(s["en"], en, 1)
                    sub["d"] = self._strip(seg_d, net, lo + pos + i, memo)
                for k in ("arst", "srst"):
                    if sub[k] is not None:
                        r = sub[k]
                        sub[k] = (r[0], r[1], r[2].slice(i, wseg))
                out.append(sub)
                i += wseg
            pos += n
        return out


_ZERO = SigSpec.const(0, 1)
_ONE = SigSpec.const(1, 1)


def _coalesce(segs: list) -> list:
    out: list = []
    for w, c in segs:
        if out and out[-1][1] == c:
            out[-1] = (out[-1][0] + w, c)
        else:
            out.append((w, c))
    return out


def _mux1(b: Builder, S: SigSpec, a: SigSpec, c: SigSpec) -> SigSpec:
    """1-bit ``S ? c : a`` on enable conditions, built from gates when an input is constant."""
    ca, cc = a.const_value(), c.const_value()
    if ca is not None and cc is not None:
        return b.op("MUX", a, c, S, width=1)
    if ca is not None:
        return b.and_(S, c, 1) if ca.value == 0 else b.or_(b.not_(S, 1), c, 1)
    if cc is not None:
        return b.and_(b.not_(S, 1), a, 1) if cc.value == 0 else b.or_(S, a, 1)
    return b.op("MUX", a, c, S, width=1)


def _same_ctrl(a: dict, b: dict) -> bool:
    def k(x):
        return None if x is None else (x[0].key(), x[1])
    return (k(a["arst"]) == k(b["arst"]) and k(a["srst"]) == k(b["srst"])
            and (a["en"].key() if a["en"] is not None else None)
            == (b["en"].key() if b["en"] is not None else None))


def _cond_level(S: SigSpec, asig: SigSpec) -> int | None:
    """Level of ``asig`` for which condition ``S`` is true (1/0), or None if unrelated."""
    if S == asig:
        return 1
    sl = S.single_slice()
    if sl is None or sl.lo != 0 or sl.width != sl.net.width:
        return None
    d = sl.net.driver
    if not isinstance(d, Node) or not d.alive:
        return None
    ins = dict(d.inputs)
    if d.op == "NOT":
        return 0 if ins["A"] == asig else (1 if _cond_level(ins["A"], asig) == 0 else None)
    if d.op in ("EQ", "NE"):
        for x, y in (("A", "B"), ("B", "A")):
            if ins[x] == asig and ins[y].const_value() is not None:
                v = ins[y].const_value()
                if v.xmask or v.value > 1:
                    return None
                lvl = v.value
                return lvl if d.op == "EQ" else 1 - lvl
    return None


def _exclusive_conds(conds: list[SigSpec]) -> bool:
    """True when the conditions are EQ of one signal with pairwise distinct constants."""
    seen = {}
    for c in conds:
        sl = c.single_slice()
        if sl is None:
            return False
        d = sl.net.driver
        if not isinstance(d, Node) or d.op != "EQ":
            return False
        ins = dict(d.inputs)
        cv = ins["B"].const_value()
        sig = ins["A"]
        if cv is None:
            cv = ins["A"].const_value()
            sig = ins["B"]
        if cv is None or cv.xmask:
            return False
        key = sig.key()
        if key in seen and cv.value in seen[key]:
            return False
        if seen and key not in seen:
            return False
        seen.setdefault(key, set()).add(cv.value)
    return True


def _patterns_disjoint(pats: list[Const]) -> bool:
    for i in range(len(pats)):
        for j in range(i):
            p, q = pats[i], pats[j]
            common = p.kmask & q.kmask
            if not ((p.value ^ q.value) & common):
                return False
    return True
