"""Module elaboration: headers, signals, generate, instances, procedural blocks."""
from __future__ import annotations

import os

from gatecost.frontend.verilog import ast as A
from gatecost.ir import Builder, Design, LowerBlock, SigSpec, mask

from .base import BaseEv
from .decls import pack
from .expr import ElabError
from .scan import mem_names_of
from .symbols import BIT, ISym, KSym, PSym, PType, Scope, TSym, V, VSym, vec


class PortInfo:
    __slots__ = ("name", "dir", "pt", "node", "iface", "modport", "default", "member_of")

    def __init__(self, name, dir_, pt, node=None, iface=None, modport=None, default=None, member_of=None):
        self.name = name
        self.dir = dir_
        self.pt = pt
        self.node = node
        self.iface = iface
        self.modport = modport
        self.default = default
        self.member_of = member_of


class Hdr:
    """Evaluated module header: parameters, scope and ports for one parameter assignment."""

    __slots__ = ("mod", "scope", "params", "ports", "iface_syms", "inherited")

    def __init__(self, mod, scope, params, ports):
        self.mod = mod
        self.scope = scope
        self.params = params
        self.ports = ports
        self.iface_syms = {}
        self.inherited = {}          # hierarchical defparams from an ancestor: {"inst.path.param": V}


_DEBUG = bool(os.environ.get("GATECOST_ELAB_DEBUG"))
_DIR = {"input": "in", "output": "out", "inout": "inout", "ref": "in"}


def lvalue_root(e):
    while True:
        c = type(e)
        if c is A.Index or c is A.Select or c is A.Member:
            e = e.base
        else:
            return e


def lvalue_names(e, out):
    if isinstance(e, A.Concat):
        for i in e.items:
            lvalue_names(i, out)
        return
    r = lvalue_root(e)
    if isinstance(r, A.Ident):
        out.append(r.name)


def assigned_names(mod) -> set:
    """Root names assigned anywhere in a module (continuous, procedural, net initializers)."""
    out = []
    for it in mod.items:
        for n in A.walk(it):
            c = type(n)
            if c is A.ContAssign or c is A.ProcAssign:
                lvalue_names(n.lhs, out)
            elif c is A.NetDecl and n.init is not None:
                out.append(n.name)
    return set(out)


def param_decls(mod):
    """Non-local parameter declarations in positional order (header, then body)."""
    res = [p for p in mod.params if not p.local]
    res += [it for it in mod.items if isinstance(it, A.ParamDecl) and not it.local]
    return res


def pkey(v):
    """Canonical key value of a parameter."""
    if isinstance(v, PType):
        return "type:" + v.describe()
    if v.real is not None:
        return ("real", v.real)
    c = v.sig.const_value()
    if c is None:
        return "?"
    if c.xmask:
        return c
    if c.width == 32:
        return c.as_signed() if v.signed else c.value
    return c


def build_header(ctx, mod, overrides):
    scope = Scope(None, "", mod.name)
    unit = ctx.unit_scope()
    if unit is not None:
        scope.wild.append(unit)
    ev = BaseEv(ctx, scope, ctx.scratch)
    ev._cur = mod
    for it in mod.items:
        if isinstance(it, A.Function):
            ev.register_function(it)
    for pd in mod.params:
        _guard(ev, ev.declare_a0, pd, overrides)
    # ANSI ports cannot depend on body declarations (except imports), so the ports are built first and entered
    # in the scope: body parameters may then use them (``localparam W = $bits(data);``).
    late = mod.ansi
    for it in mod.items:
        if type(it) in ((A.Import,) if late else (A.ParamDecl, A.Typedef, A.Import)):
            _guard(ev, ev.declare_a0, it, overrides)
    ports = None
    if late:
        ports = _header_ports(ctx, ev, mod, overrides)
        for pi in ports:
            if pi.iface is None and pi.member_of is None:
                scope.syms.setdefault(pi.name, VSym(pi.name, pi.pt))
        for it in mod.items:
            if type(it) in (A.ParamDecl, A.Typedef):
                _guard(ev, ev.declare_a0, it, overrides)
    params = {}
    for pd in param_decls(mod):
        sym = scope.syms.get(pd.name)
        if isinstance(sym, PSym):
            params[pd.name] = pkey(sym.val)
        elif isinstance(sym, TSym):
            params[pd.name] = "type:" + sym.pt.describe()
    if ports is None:
        ports = _header_ports(ctx, ev, mod, overrides)
    for pi in ports:
        if pi.iface is not None and pi.member_of is None:
            isym = overrides.get("$if:" + pi.name)
            if isym is not None:
                params["$if:" + pi.name] = isym.iface.name + "(" + ",".join(
                    f"{k}={v}" for k, v in sorted(getattr(isym, "pkeys", {}).items())) + ")"
    for k, v in overrides.items():
        if k.startswith("$dp:") and isinstance(v, V):
            params[k] = pkey(v)
    hdr = Hdr(mod, scope, params, ports)
    for k, v in overrides.items():
        if k.startswith("$if:"):
            hdr.iface_syms[k[4:]] = v
        elif k.startswith("$dp:"):
            hdr.inherited[k[4:]] = v
    return hdr


def _guard(ev, fn, *a):
    try:
        fn(*a)
    except RecursionError:
        ev.diag("LIMIT_REACHED", "expression nesting too deep", a[0] if a else None, True)
    except ElabError as exc:
        ev.diag("UNSUPPORTED_CONSTRUCT", str(exc), a[0] if a else None, True)
    except Exception as exc:     # never abort the analysis
        ev.diag("INTERNAL", f"internal elaboration error ({type(exc).__name__}: {exc})",
                a[0] if a else None, True)


def _header_ports(ctx, ev, mod, overrides):
    ports = []
    assigned = None
    decl_types = {}
    body_ports = {}
    if not mod.ansi:
        for it in mod.items:
            if type(it) is A.Port:
                body_ports[it.name] = it
            elif type(it) in (A.VarDecl, A.NetDecl):
                decl_types.setdefault(it.name, it)
        plist = []
        for hp in mod.ports:
            bp = body_ports.get(hp.name)
            if bp is None:
                ev.diag("UNRESOLVED_NAME", f"port '{hp.name}' has no direction; assumed input", hp, True)
                bp = A.Port(hp.loc, "input", None, None, hp.name, (), None, None, None)
            plist.append(bp)
    else:
        plist = list(mod.ports)
    for p in plist:
        dt = p.dtype
        d = decl_types.get(p.name)
        if d is not None and (dt is None or (isinstance(dt, A.ImplicitType) and not dt.dims and not dt.signed)):
            dt = d.dtype
        if p.iface:
            isym = overrides.get("$if:" + p.name)
            if isym is None:
                ev.diag("UNSUPPORTED_CONSTRUCT", f"interface port '{p.name}' without a known interface instance",
                        p, True)
                continue
            ports.append(PortInfo(p.name, "in", BIT, p, p.iface, p.modport))
            mdirs = ctx.modport_dirs(isym.iface, p.modport)
            amem = ctx.assigned_members(mod, p.name)
            for mname, ms in isym.members.items():
                dr = mdirs.get(mname) if mdirs else None
                if dr is None:
                    dr = "out" if mname in amem else "in"
                ports.append(PortInfo(f"{p.name}.{mname}", dr, ms.pt, p, p.iface, p.modport, None, p.name))
            continue
        pt = ev.resolve_dtype(dt) if dt is not None else BIT
        if p.dims:
            ud = ev.eval_dims(p.dims)
            if ud:
                ev.diag("UNSUPPORTED_CONSTRUCT", f"unpacked array port '{p.name}' is flattened into one vector",
                        p, True)
                pt = pack(ud, pt, pt.signed)
        dr = _DIR.get(p.direction, "in")
        if dr == "inout":
            if assigned is None:
                assigned = assigned_names(mod)
            dr = "out" if p.name in assigned else "in"
        ports.append(PortInfo(p.name, dr, pt, p, None, None, p.default))
    return ports


class ModElab(BaseEv):
    def __init__(self, ctx, hdr: Hdr, cports: dict, depth: int):
        mod = hdr.mod
        ob = ctx.origin(mod.loc) if mod.loc else None
        b = Builder(mod.name, hdr.params, cports, limits=ctx.ir_limits, origin=ob)
        super().__init__(ctx, hdr.scope, b, b)
        self.hdr = hdr
        self.mod = mod
        self.root = hdr.scope
        self.cports = cports
        self.hier_depth = depth
        self.deferred = []
        self.defparams = dict(hdr.inherited)        # {'inst.path.param': V}
        self.refs = None
        self.mem_names = None
        self.item_idx = -1
        self.allow_implicit = False
        self.key = b.module.key

    # ------------------------------------------------------------------ prescans
    def build_refs(self):
        self.refs = self.ctx.scan(self.mod).refs

    def compute_mem_names(self):
        self.mem_names = mem_names_of(self.ctx.scan(self.mod))

    # ------------------------------------------------------------------ driver
    def run(self):
        b = self.b
        self.declare_ports()
        self.process_items(self.mod.items, True)
        for fn in self.deferred:
            self._safe(fn)
        self.deferred = []
        m = b.finalize(check="diag")
        return m

    def _safe(self, fn, *a, node=None):
        old_scope = self.scope
        try:
            fn(*a)
        except RecursionError:
            if _DEBUG:
                raise
            self.diag("LIMIT_REACHED", "expression or hierarchy nesting too deep", node, True)
        except ElabError as exc:
            self.diag("UNSUPPORTED_CONSTRUCT", str(exc), node, True)
        except Exception as exc:
            if _DEBUG:
                raise
            self.diag("INTERNAL", f"internal elaboration error ({type(exc).__name__}: {exc})", node, True)
        finally:
            self.scope = old_scope
            self.blk = None if self.depth == 0 else self.blk

    def declare_ports(self):
        b = self.b
        for pi in self.hdr.ports:
            if pi.iface is not None and pi.member_of is None:
                src = self.hdr.iface_syms.get(pi.name)
                isym = ISym(pi.name, src.iface if src is not None else self.ctx.iface_of(pi), self.scope)
                if src is not None:
                    isym.pkeys = src.pkeys
                self.scope.syms[pi.name] = isym
                continue
            pt = pi.pt
            net = b.port(pi.name, pi.dir, pt.w, pt.signed)
            sym = VSym(pi.name if pi.member_of is None else pi.name, pt, (), net)
            sym.is_port = True
            sym.direction = pi.dir
            cv = net.attrs.get("const")
            if cv is not None and pi.dir == "in":
                sym.const_sig = SigSpec.const(cv.value, cv.width, cv.xmask)
            if pi.member_of is not None:
                isym = self.scope.syms.get(pi.member_of)
                mname = pi.name.split(".", 1)[1]
                if isinstance(isym, ISym):
                    isym.members[mname] = sym
                    isym.order.append(mname)
            else:
                self.scope.syms[pi.name] = sym

    # ------------------------------------------------------------------ items
    def process_items(self, items, root: bool):
        for it in items:
            c = type(it)
            self._cur = it
            if c is A.NetDecl:
                self._safe(self.decl_net, it, node=it)
            elif c is A.VarDecl:
                self._safe(self.decl_var, it, node=it)
            elif not root and c in (A.ParamDecl, A.Typedef, A.Import, A.Function):
                self._safe(self.declare_a0, it, None, node=it)
            elif c is A.Defparam:
                self._safe(self.collect_defparam, it, node=it)
        for idx, it in enumerate(items):
            c = type(it)
            self._cur = it
            if root:
                self.item_idx = idx
            if c is A.ContAssign:
                self._safe(self.do_cont, it, node=it)
            elif c is A.NetDecl:
                if it.init is not None:
                    self._safe(self.do_netinit, it, node=it)
            elif c is A.Always:
                self._safe(self.do_always, it, node=it)
            elif c is A.Instance:
                self._safe(self.do_instance, it, node=it)
            elif c is A.GateInst:
                self._safe(self.do_gate, it, node=it)
            elif c is A.GenFor:
                self._safe(self.do_genfor, it, node=it)
            elif c is A.GenIf:
                self._safe(self.do_genif, it, node=it)
            elif c is A.GenCase:
                self._safe(self.do_gencase, it, node=it)
            elif c is A.GenBlock:
                self._safe(self.do_genblock, it, self.scope, node=it)

    def _hier_path(self, p):
        """Dotted text of a hierarchical name (``g[1].u.W``) or None; indices are constant-evaluated."""
        if isinstance(p, A.Ident):
            return p.name
        if isinstance(p, A.Member):
            b = self._hier_path(p.base)
            return None if b is None else f"{b}.{p.name}"
        if isinstance(p, A.Index):
            b = self._hier_path(p.base)
            if b is None:
                return None
            v = self.val(p.index).sig.const_value()
            if v is None or v.xmask:
                return None
            return f"{b}[{v.value}]"
        return None

    def collect_defparam(self, d):
        text = self._hier_path(d.path)
        parts = text.split(".") if text else []
        if len(parts) >= 2 and parts[0] == self.mod.name and len(parts) > 2:
            parts = parts[1:]
        if len(parts) < 2:
            self.diag("UNSUPPORTED_CONSTRUCT", "defparam target is not a hierarchical instance parameter", d, True)
            return
        v = self.val(d.value)
        if v.sig.const_value() is None:
            self.diag("UNRESOLVED_PARAM", "defparam value is not constant", d, True)
            return
        self.defparams[".".join(parts)] = v
        self.assume("defparam applied as an instance parameter override")

    # ------------------------------------------------------------------ signals
    def declare_signal(self, name, pt, ud, scope=None):
        scope = scope or self.scope
        sym = VSym(name, pt, tuple(ud))
        sym.hier = self.path + name
        if ud:
            total = 1
            for l, r in ud:
                total *= abs(l - r) + 1
            if self.mem_names is None:
                self.compute_mem_names()
            if total > 1 and (name in self.mem_names or total > 4096):
                sym.mem = self.b.memory(self.path + name, total, pt.w, origin=self.org(self._cur.loc if self._cur else None))
            else:
                sym.elems = {}
        else:
            sym.net = self.b.net(pt.w, self.path + name, pt.signed)
        scope.syms[name] = sym
        return sym

    def decl_net(self, nd: A.NetDecl):
        ex = self.scope.syms.get(nd.name)
        if isinstance(ex, VSym) and ex.is_port:
            return
        pt = self.resolve_dtype(nd.dtype)
        ud = self.eval_dims(nd.dims)
        sym = self.declare_signal(nd.name, pt, ud)
        if nd.net_type in ("supply0", "supply1") and sym.net is not None:
            self.b.connect(sym.net, SigSpec.const(mask(pt.w) if nd.net_type == "supply1" else 0, pt.w))

    def decl_var(self, vd: A.VarDecl):
        ex = self.scope.syms.get(vd.name)
        if isinstance(ex, VSym) and ex.is_port:
            return
        if vd.const and vd.init is not None and not vd.dims:
            pt = self.resolve_dtype(vd.dtype)
            v = self.val(vd.init)
            if v.sig.const_value() is not None:
                self.scope.syms[vd.name] = PSym(self.coerce(v, pt), pt)
                return
        pt = self.resolve_dtype(vd.dtype)
        ud = self.eval_dims(vd.dims)
        self.declare_signal(vd.name, pt, ud)

    def implicit_net(self, e):
        if not self.allow_implicit or self.mod.default_nettype == "none":
            return None
        sym = VSym(e.name, BIT)
        sym.net = self.b.net(1, self.path + e.name)
        self.root.syms[e.name] = sym
        return sym

    # ------------------------------------------------------------------ assigns
    def do_cont(self, a: A.ContAssign):
        self.b.origin = self.org(a.loc)
        self.allow_implicit = True
        try:
            parts = self.lvalue_parts(a.lhs)
        finally:
            self.allow_implicit = False
        if not parts or any(p is None for p in parts):
            return
        tw = self.part_width(parts)
        sig = self.assign_val(a.rhs, tw, parts[0].pt if len(parts) == 1 else None)
        self.store_parts(parts, sig, False)

    def do_netinit(self, nd: A.NetDecl):
        sym = self.scope.syms.get(nd.name)
        if not isinstance(sym, VSym) or sym.net is None:
            return
        self.b.origin = self.org(nd.loc)
        sig = self.assign_val(nd.init, sym.pt.w, sym.pt)
        self.b.connect(SigSpec.of(self.store_net(sym), 0, sym.pt.w), sig, self.b.origin)
        cv = sig.const_value()
        if cv is not None and sym.const_sig is None and not sym.is_port and sym.wired is None:
            sym.const_sig = SigSpec.const(cv.value, cv.width, cv.xmask)      # a wire tied to a constant reads as that constant

    # ------------------------------------------------------------------ gates
    def do_gate(self, g: A.GateInst):
        b = self.b
        o = self.org(g.loc)
        b.origin = o
        gate = g.gate
        t = g.terminals
        self.allow_implicit = True
        try:
            if gate in ("and", "nand", "or", "nor", "xor", "xnor"):
                if len(t) < 2:
                    return
                ins = [self.val(x).sig.slice(0, 1) for x in t[1:]]
                fn = {"and": b.and_, "nand": b.and_, "or": b.or_, "nor": b.or_, "xor": b.xor, "xnor": b.xor}[gate]
                acc = ins[0]
                for x in ins[1:]:
                    acc = fn(acc, x, 1, origin=o)
                if gate in ("nand", "nor", "xnor"):
                    acc = b.not_(acc, 1, origin=o)
                self._gate_out(t[0], acc)
            elif gate in ("buf", "not"):
                if len(t) < 2:
                    return
                x = self.val(t[-1]).sig.slice(0, 1)
                if gate == "not":
                    x = b.not_(x, 1, origin=o)
                for out in t[:-1]:
                    self._gate_out(out, x)
            elif gate in ("bufif0", "bufif1", "notif0", "notif1"):
                if len(t) < 3:
                    return
                d = self.val(t[1]).sig.slice(0, 1)
                en = self.val(t[2]).sig.slice(0, 1)
                if gate.startswith("notif"):
                    d = b.not_(d, 1, origin=o)
                if gate.endswith("0"):
                    en = b.not_(en, 1, origin=o)
                self.assume("tri-state driver ('z') costed as a multiplexer with enable")
                self._gate_out(t[0], b.mux(en, SigSpec.x(1), d, 1, tristate=True, origin=o))
            elif gate in ("pullup", "pulldown"):
                self.assume(f"{gate} primitive ignored")
            else:
                self.diag("UNSUPPORTED_CONSTRUCT", f"switch primitive '{gate}' is not supported", g, True)
        finally:
            self.allow_implicit = False

    def _gate_out(self, e, sig):
        parts = self.lvalue_parts(e)
        if not parts or any(p is None for p in parts):
            return
        tw = self.part_width(parts)
        self.store_parts(parts, sig.resize(tw, False), False)

    # ------------------------------------------------------------------ generate
    def _child_scope(self, label, suffix=""):
        old = self.scope
        sc = Scope(old, old.path, label or "")
        return sc

    def do_genblock(self, g: A.GenBlock, parent):
        old_scope, old_path = self.scope, self.path
        sc = Scope(old_scope, old_scope.path, g.label or "")
        self.scope = sc
        if g.label:
            self.path = old_path + g.label + "."
            old_scope.syms[g.label] = KSym(scope=sc)
        try:
            self.process_items(g.items, False)
        finally:
            self.scope, self.path = old_scope, old_path

    def do_genfor(self, g: A.GenFor):
        old_scope, old_path = self.scope, self.path
        var = None
        start = None
        for st in g.init or ():
            if isinstance(st, A.ProcAssign) and isinstance(st.lhs, A.Ident):
                var = st.lhs.name
                start = st.rhs
        if var is None:
            self.diag("UNSUPPORTED_CONSTRUCT", "generate-for without a loop variable", g, True)
            return
        val = self.cint(start, None)
        if val is None:
            self.diag("UNRESOLVED_PARAM", "generate-for start value is not constant", g, True)
            return
        label = g.body.label or ""
        limit = self.ctx.limits["generate_iterations"]
        arr = {}
        if label:
            old_scope.syms[label] = KSym(arr=arr)
        n = 0
        try:
            while True:
                sc = Scope(old_scope, old_scope.path, label)
                sc.syms[var] = PSym(self.const_v(val))
                self.scope = sc
                c = self.cint(g.cond, None) if g.cond is not None else 1
                if c is None:
                    self.diag("UNRESOLVED_PARAM", "generate-for condition is not constant", g, True)
                    break
                if not c:
                    break
                if n >= limit:
                    self.diag("LIMIT_REACHED", f"generate iteration limit ({limit}) reached; remaining "
                              "iterations ignored", g, True)
                    break
                n += 1
                arr[val] = sc
                self.path = old_path + (f"{label}[{val}]." if label else f"genblk[{val}].")
                self.process_items(g.body.items, False)
                nv = val
                for st in g.step or ():
                    if isinstance(st, A.ProcAssign) and isinstance(st.lhs, A.Ident) and st.lhs.name == var:
                        rhs = st.rhs if st.op == "=" else A.Binary(st.loc, st.op[:-1], st.lhs, st.rhs)
                        r = self.cint(rhs, None)
                        if r is None:
                            self.diag("UNRESOLVED_PARAM", "generate-for step is not constant", g, True)
                            return
                        nv = r
                if nv == val:
                    self.diag("LIMIT_REACHED", "generate-for loop does not advance", g, True)
                    break
                val = nv
        finally:
            self.scope, self.path = old_scope, old_path

    def _gen_enter(self, blk, label_default=""):
        old_scope, old_path = self.scope, self.path
        sc = Scope(old_scope, old_scope.path, blk.label or "")
        self.scope = sc
        if blk.label:
            self.path = old_path + blk.label + "."
            old_scope.syms[blk.label] = KSym(scope=sc)
        return old_scope, old_path

    def _gen_body(self, blk):
        if blk is None:
            return
        if isinstance(blk, A.GenIf):
            self.do_genif(blk)
            return
        old_scope, old_path = self._gen_enter(blk)
        try:
            self.process_items(blk.items, False)
        finally:
            self.scope, self.path = old_scope, old_path

    def do_genif(self, g: A.GenIf):
        c = self.cint(g.cond, None)
        if c is None:
            self.diag("UNRESOLVED_PARAM", "generate-if condition is not constant; else branch assumed", g, True)
            self.assume("non-constant generate-if condition assumed false")
            c = 0
        self._gen_body(g.then if c else g.else_)

    def do_gencase(self, g: A.GenCase):
        sel = self.cint(g.expr, None)
        if sel is None:
            self.diag("UNRESOLVED_PARAM", "generate-case selector is not constant; default assumed", g, True)
        default = None
        for it in g.items:
            if it.labels is None:
                default = it
                continue
            if sel is not None:
                for lab in it.labels:
                    if self.cint(lab, None) == sel:
                        self._gen_body(it.body)
                        return
        if default is not None:
            self._gen_body(default.body)

    # ------------------------------------------------------------------ always
    def do_always(self, a: A.Always):
        kind = a.kind
        ev = a.event
        body = a.body
        mode = None
        if kind in ("always_comb", "always_latch"):
            mode = "comb"
        else:
            if ev is None:
                inner = body
                rest = ()
                if isinstance(body, A.Block) and body.items and isinstance(body.items[0], A.EventStmt):
                    inner = body.items[0]
                    rest = body.items[1:]
                if isinstance(inner, A.EventStmt) and inner.event is not None:
                    ev = inner.event
                    items = ((inner.body,) if inner.body is not None else ()) + tuple(rest)
                    body = A.Block(a.loc, None, items)
                else:
                    self.diag("IGNORED_REGION", "always block without an event control is not synthesizable; "
                              "skipped", a, False, "info")
                    return
            if ev.star or not any(i.edge for i in ev.items):
                mode = "comb"
            else:
                mode = "clocked"
        origin = self.org(a.loc)
        self.b.origin = origin
        self.ctl = []
        self.forced_vars = {}
        self.rbw = set()
        self.tmp_cands = None
        self.sync_read_done = set()
        self.assign_counts = {}
        blocking = set()
        for n in A.walk(body):
            if type(n) is A.ProcAssign:
                names = []
                lvalue_names(n.lhs, names)
                for nm in names:
                    self.assign_counts[nm] = self.assign_counts.get(nm, 0) + 1
                    if n.blocking:
                        blocking.add(nm)
        if mode == "comb":
            blk = LowerBlock(self.b, "comb", origin=origin)
        else:
            clk, resets = self._pick_clock(ev, body)
            if clk is None:
                return
            csig, cedge = clk
            blk = LowerBlock(self.b, "clocked", clock=csig, clk_edge=cedge, async_resets=resets, origin=origin)
            if self.refs is None:
                self.build_refs()
            cands = set()
            for nm in blocking:
                sym = self.scope.lookup(nm)
                if (isinstance(sym, VSym) and sym.net is not None and not sym.is_port and not sym.udims
                        and self.item_idx >= 0 and self.refs.get(nm, set()) <= {self.item_idx}):
                    cands.add(sym)
            self.tmp_cands = cands or None
        self.blk = blk
        try:
            self.exec_stmt(body)
        finally:
            try:
                if self.tmp_cands:
                    for sym in self.tmp_cands:
                        if sym.net.id not in self.rbw:
                            blk.mark_variable(sym.net)
                self.drop_nets(list(self.forced_vars.values()), kill=False)
                self.resolve_conflicts(blk, a)
                blk.finish()
            finally:
                self.blk = None
                self.tmp_cands = None

    def resolve_conflicts(self, blk, node):
        """A net already driven by another block / assignment keeps its first driver."""
        drv = self.b.module.driven
        bad = []
        for nid, bits in blk.env.assigned.items():
            net = blk._order.get(nid)
            if net is not None and drv.get(nid, 0) & bits:
                bad.append(net)
                self.diag("MULTIPLE_DRIVERS", f"signal '{net.name or net.id}' is driven by more than one "
                          "process or assignment; the first driver is kept", node, True)
        if bad:
            self.drop_nets(bad, kill=False)

    def _lead_names(self, body):
        s = body
        while isinstance(s, A.Block):
            first = None
            for x in s.items:
                if isinstance(x, A.Stmt):
                    first = x
                    break
            if first is None:
                return set()
            s = first
        if isinstance(s, A.If):
            return {n.name for n in A.walk(s.cond) if type(n) is A.Ident}
        return set()

    def _pick_clock(self, ev, body):
        edges = [i for i in ev.items if i.edge]
        names = self._lead_names(body)

        def nm(item):
            r = lvalue_root(item.expr)
            return r.name if isinstance(r, A.Ident) else None

        resets = [i for i in edges if len(edges) > 1 and nm(i) in names]
        clocks = [i for i in edges if i not in resets]
        if not clocks:
            clocks, resets = edges[:1], edges[1:]
        clk = clocks[0]
        resets = resets + clocks[1:]
        csig = self.val(clk.expr).sig.slice(0, 1)
        res = []
        for r in resets:
            rs = self.val(r.expr).sig.slice(0, 1)
            res.append((rs, 1 if r.edge == "posedge" else 0))
        return (csig, "neg" if clk.edge == "negedge" else "pos"), res

    # ------------------------------------------------------------------ instances
    def eval_overrides(self, mod, it, inst_name):
        names = [p.name for p in param_decls(mod)]
        decls = {p.name: p for p in param_decls(mod)}
        ov = {}
        pos = 0
        for pa in it.params or ():
            if pa.name is None:
                nm = names[pos] if pos < len(names) else None
                pos += 1
            else:
                nm = pa.name
            if nm is None or nm not in decls:
                self.diag("UNRESOLVED_PARAM", f"module '{mod.name}' has no parameter '{pa.name or pos - 1}'", it, True)
                continue
            if pa.value is None:
                continue
            d = decls[nm]
            if d.is_type:
                ov[nm] = self.resolve_dtype(pa.value) if isinstance(pa.value, A.DataType) else self.type_of_expr(pa.value)
                continue
            v = self.val(pa.value)
            if v.sig.const_value() is None:
                self.diag("UNRESOLVED_PARAM", f"override of '{nm}' on '{inst_name}' is not constant; default used",
                          it, True)
                self.assume(f"non-constant override of parameter '{nm}' ignored (default used)")
                continue
            ov[nm] = v
        pre = self.path + inst_name + "."
        for full, v in self.defparams.items():
            if not full.startswith(pre):
                continue
            rest = full[len(pre):]
            if "." not in rest:
                if rest in decls and rest not in ov:
                    ov[rest] = v
            else:
                ov["$dp:" + rest] = v        # forwarded to the child (applies to its own instances)
        return ov

    def do_instance(self, it: A.Instance):
        ctx = self.ctx
        name = it.module
        mod = ctx.modmap.get(name)
        if mod is None and name in ctx.ifmap:
            self.do_iface_inst(it)
            return
        if mod is None:
            fm = ctx.lookup_foreign(name)
            if fm is not None:
                self.do_foreign_inst(it, fm)
                return
            self.deferred.append(self._defer(lambda: self.do_blackbox(it)))
            return
        iname = self.path + it.name
        ov = self.eval_overrides(mod, it, it.name)
        # interface ports
        for p in (mod.ports if mod.ansi else [x for x in mod.items if type(x) is A.Port]):
            if getattr(p, "iface", None):
                conn = self._port_conn_expr(it, mod, p.name, ctx)
                isym = None
                if isinstance(conn, A.Ident):
                    isym = self.lookup(conn.name)
                if isinstance(isym, ISym):
                    ov["$if:" + p.name] = isym
        hdr = ctx.get_header(mod, ov)
        n = 1
        if it.dims:
            ds = self.eval_dims(it.dims)
            n = 1
            for l, r in ds:
                n *= abs(l - r) + 1
            lim = ctx.limits["max_instances"]
            if n > lim:
                self.diag("LIMIT_REACHED", f"instance array of {n} exceeds the instance limit ({lim})", it, True)
                n = lim
        conns = self._build_conns(it, hdr)
        self.b.origin = self.org(it.loc)
        self.allow_implicit = True
        try:
            for ce in conns.values():
                if isinstance(ce, A.Ident) and self.lookup(ce.name) is None:
                    self.implicit_net(ce)
            elems = [self._conn_element(hdr, conns, k, n, it) for k in range(n)]
        finally:
            self.allow_implicit = False
        ctx.count_instances(n)
        # group identical elements into a single counted instance
        keys = []
        for ins, outs in elems:
            cp = self._cports(ins) if ctx.opts["const_ports"] else {}
            keys.append(ctx.ensure_module(mod, hdr, cp, self.hier_depth + 1, it))
        same = (n > 1 and len(set(keys)) == 1 and all(e[0] == elems[0][0] for e in elems)
                and all(not any(o[1] for o in e[1].values()) for e in elems))
        if same:
            self._emit_inst(keys[0], iname, elems[0], n, it, hdr)
        else:
            for k, el in enumerate(elems):
                self._emit_inst(keys[k], iname if n == 1 else f"{iname}[{k}]", el, 1, it, hdr)

    def _defer(self, fn):
        scope, path, idx = self.scope, self.path, self.item_idx

        def run():
            old = (self.scope, self.path)
            self.scope, self.path = scope, path
            try:
                fn()
            finally:
                self.scope, self.path = old
        return run

    def _cports(self, ins):
        return Design.const_port_signature(ins)

    def _port_conn_expr(self, it, mod, pname, ctx):
        names = [p.name for p in (mod.ports)]
        for i, pc in enumerate(it.conns or ()):
            if pc.kind == "named" and pc.name == pname:
                return pc.expr
            if pc.kind == "implicit" and pc.name == pname:
                return A.Ident(pc.loc, pc.name)
            if pc.kind == "positional" and i < len(names) and names[i] == pname:
                return pc.expr
        return None

    def _build_conns(self, it, hdr):
        conns = {}
        wild = False
        top_ports = [p for p in hdr.ports if p.member_of is None]
        for i, pc in enumerate(it.conns or ()):
            if pc.kind == "named":
                conns[pc.name] = pc.expr
            elif pc.kind == "implicit":
                conns[pc.name] = A.Ident(pc.loc, pc.name)
            elif pc.kind == "wildcard":
                wild = True
            elif i < len(top_ports):
                conns[top_ports[i].name] = pc.expr
            else:
                self.diag("UNSUPPORTED_CONSTRUCT", "too many positional port connections", it, True)
        if wild:
            for p in top_ports:
                if p.name not in conns and isinstance(self.lookup(p.name), (VSym, ISym)):
                    conns[p.name] = A.Ident(it.loc, p.name)
        for nm in conns:
            if nm not in {p.name for p in top_ports}:
                self.diag("UNRESOLVED_NAME", f"module '{hdr.mod.name}' has no port '{nm}'", it, True)
        return conns

    def _conn_element(self, hdr, conns, k, n, it):
        """(inputs, outputs) of element ``k`` of an instance array: outputs map port ->
        ``(Net|width, post_connect_info)``."""
        b = self.b
        ins = {}
        outs = {}
        for pi in hdr.ports:
            if pi.iface is not None and pi.member_of is None:
                continue
            pt = pi.pt
            pw = pt.w
            if pi.member_of is not None:
                ce = conns.get(pi.member_of)
                isym = self.lookup(ce.name) if isinstance(ce, A.Ident) else None
                mname = pi.name.split(".", 1)[1]
                msym = isym.members.get(mname) if isinstance(isym, ISym) else None
                if msym is None:
                    if pi.dir == "in":
                        ins[pi.name] = SigSpec.x(pw)
                    else:
                        outs[pi.name] = (b.net(pw), None)
                    continue
                if pi.dir == "in":
                    ins[pi.name] = self.read_sym(msym).resize(pw, False)
                else:
                    tgt = msym.net
                    outs[pi.name] = (b.net(pw), ("members", [(tgt, msym)]))
                continue
            e = conns.get(pi.name)
            if pi.dir == "in":
                if e is None:
                    ins[pi.name] = SigSpec.x(pw)
                    continue
                if n > 1:
                    v = self.val(e)
                    if v.w == n * pw:
                        ins[pi.name] = v.sig.slice(k * pw, pw)
                        continue
                    ins[pi.name] = self.assign_val(e, pw, pt)
                else:
                    ins[pi.name] = self.assign_val(e, pw, pt)
            else:
                if e is None:
                    outs[pi.name] = (pw, None)
                    continue
                parts = self.lvalue_parts(e)
                if not parts or any(p is None for p in parts):
                    outs[pi.name] = (pw, None)
                    continue
                tw = self.part_width(parts)
                if n > 1 and tw == n * pw:
                    # split targets: slice the concatenated lvalue
                    outs[pi.name] = (b.net(pw), ("slice", parts, k * pw, pw, tw))
                else:
                    p0 = parts[0]
                    if (len(parts) == 1 and p0.net is not None and not p0.dyn and not p0.elems and p0.mem is None
                            and p0.off == 0 and p0.w == p0.net.width == pw and p0.vsym is not None
                            and not p0.vsym.is_port and p0.net.driver is None and n == 1):
                        outs[pi.name] = (p0.net, None)
                    else:
                        outs[pi.name] = (b.net(pw), ("parts", parts, tw, pt.signed))
        return ins, outs

    def _emit_inst(self, key, iname, el, count, it, hdr, many=False, k=0):
        b = self.b
        ins, outs = el
        outputs = {p: o[0] for p, o in outs.items()}
        res = b.inst(key, iname, ins, outputs, count=count, origin=self.org(it.loc))
        for p, (net, info) in outs.items():
            if info is None:
                continue
            sig = res[p]
            if info[0] == "parts":
                _t, parts, tw, sg = info
                self.store_parts(parts, sig.resize(tw, False), False)
            elif info[0] == "slice":
                _t, parts, lo, w, tw = info
                # drive only this element's slice of the concatenated target
                self._store_slice(parts, sig, lo, w)
            else:
                for tnet, _ms in info[1]:
                    b.connect(SigSpec.of(tnet, 0, tnet.width), sig.resize(tnet.width, False), b.origin)

    def _store_slice(self, parts, sig, lo, w):
        pos = 0
        for p in reversed(parts):
            a, bnd = max(lo, pos), min(lo + w, pos + p.w)
            if a < bnd:
                sub = type(p)()
                for s in p.__slots__:
                    setattr(sub, s, getattr(p, s))
                sub.off = p.off + (a - pos)
                sub.w = bnd - a
                self.store(sub, sig.slice(a - lo, bnd - a), False)
            pos += p.w

    # -- instances of modules of the other language ----------------------------------------------
    def do_foreign_inst(self, it: A.Instance, fm):
        ctx = self.ctx
        names = list(getattr(fm, "parameters", {}) or {})
        params = dict(fm.parameters)
        pos = 0
        for pa in it.params or ():
            if pa.name is None:
                nm = names[pos] if pos < len(names) else None
                pos += 1
            else:
                nm = next((k for k in names if k.lower() == pa.name.lower()), pa.name)
            if nm is None or pa.value is None or isinstance(pa.value, A.DataType):
                continue
            v = self.cint(pa.value, None)
            if v is None:
                self.diag("UNRESOLVED_PARAM", f"override of '{nm}' is not constant; default used", it, True)
                continue
            params[nm] = v
        plist = fm.ports(params)
        ports = [PortInfo(n, d, vec(w, sg)) for n, d, w, sg in plist]
        hdr = Hdr(None, None, {}, ports)
        hdr.mod = type("M", (), {"name": fm.name})()
        by_low = {p.name.lower(): p.name for p in ports}
        conns = {}
        for i, pc in enumerate(it.conns or ()):
            if pc.kind == "named":
                conns[by_low.get(pc.name.lower(), pc.name)] = pc.expr
            elif pc.kind == "implicit":
                conns[by_low.get(pc.name.lower(), pc.name)] = A.Ident(pc.loc, pc.name)
            elif pc.kind == "positional" and i < len(ports):
                conns[ports[i].name] = pc.expr
        self.b.origin = self.org(it.loc)
        self.allow_implicit = True
        try:
            for ce in conns.values():
                if isinstance(ce, A.Ident) and self.lookup(ce.name) is None:
                    self.implicit_net(ce)
            ins, outs = self._conn_element(hdr, conns, 0, 1, it)
        finally:
            self.allow_implicit = False
        cp = Design.const_port_signature(ins) if ctx.opts["const_ports"] else {}
        try:
            key = fm.elaborate(params, cp)
        except Exception:
            if _DEBUG:
                raise
            key = None
        if key is None:
            ctx.missing(it.module, f"module '{it.module}' of the other language could not be elaborated; "
                        "instance treated as a black box", self, it)
            res = self.b.blackbox(it.module, self.path + it.name, ins, {p: o[0] for p, o in outs.items()},
                                  count=1, origin=self.org(it.loc))
            for p, (net, info) in outs.items():
                if info is not None and info[0] == "parts":
                    self.store_parts(info[1], res[p].resize(info[2], False), False)
            return
        self._emit_inst(key, self.path + it.name, (ins, outs), 1, it, hdr)

    # -- black boxes ------------------------------------------------------------------------------
    def do_blackbox(self, it: A.Instance):
        ctx = self.ctx
        b = self.b
        name = it.module
        self.b.origin = self.org(it.loc)
        if name in ctx.udps:
            ctx.missing(name, f"user-defined primitive '{name}' could not be lowered to logic; instance treated as a black box",
                        self, it)
        else:
            ctx.missing(name, f"module '{name}' not found; instance treated as a black box", self, it)
        ins = {}
        outputs = {}
        post = []
        claimed: set = set()
        self.allow_implicit = True
        try:
            for i, pc in enumerate(it.conns or ()):
                if pc.kind == "wildcard":
                    continue
                pname = pc.name if pc.name else f"p{i}"
                e = pc.expr if pc.kind != "implicit" else A.Ident(pc.loc, pc.name)
                if e is None:
                    continue
                if self._guess_output(e):
                    parts = self.lvalue_parts(e)
                    if parts and all(p is not None for p in parts):
                        # a net connected to several ports of one black box is driven by at most one of them
                        bits = {(id(p.net), p.off + k) for p in parts if p.net is not None and not p.dyn
                                for k in range(p.w)}
                        if bits and bits & claimed:
                            ins[pname] = self.val(e).sig
                            continue
                        claimed |= bits
                        tw = self.part_width(parts)
                        net = b.net(tw)
                        outputs[pname] = net
                        post.append((parts, net, tw))
                        continue
                v = self.val(e)
                ins[pname] = v.sig
        finally:
            self.allow_implicit = False
        n = 1
        if it.dims:
            n = 1
            for l, r in self.eval_dims(it.dims):
                n *= abs(l - r) + 1
        iname = self.path + it.name
        b.blackbox(name, iname, ins, outputs, count=max(1, n), origin=self.org(it.loc))
        for parts, net, tw in post:
            self.store_parts(parts, net.sig, False)

    def _guess_output(self, e) -> bool:
        if isinstance(e, A.Concat):
            return all(self._guess_output(x) for x in e.items)
        r = lvalue_root(e)
        if not isinstance(r, A.Ident) or not isinstance(e, (A.Ident, A.Index, A.Select, A.Member)):
            return False
        sym = self.lookup(r.name)
        if not isinstance(sym, VSym) or sym.net is None:
            return sym is None
        if sym.is_port and sym.direction == "in":
            return False
        drv = self.b.module.driven.get(sym.net.id, 0)
        return drv == 0

    # -- interfaces ---------------------------------------------------------------------------------
    def do_iface_inst(self, it: A.Instance):
        ctx = self.ctx
        if it.dims:
            self.diag("UNSUPPORTED_CONSTRUCT", f"interface array '{it.name}' is not supported; its members and "
                      "the logic reading them are skipped", it, True)
            self.assume(f"interface array '{it.name}' skipped")
            return
        iface = ctx.ifmap[it.module]
        ov = self.eval_overrides(iface, it, it.name)
        hdr = ctx.get_header(iface, ov)
        old_scope, old_path = self.scope, self.path
        isc = Scope(hdr.scope, "", it.name)
        isym = ISym(it.name, iface, isc)
        self.scope = isc
        self.path = old_path + it.name + "."
        old_idx, self.item_idx = self.item_idx, -2
        try:
            # ports of the interface alias the connected signals
            for pi in hdr.ports:
                e = None
                for i, pc in enumerate(it.conns or ()):
                    if pc.kind == "named" and pc.name == pi.name:
                        e = pc.expr
                    elif pc.kind == "implicit" and pc.name == pi.name:
                        e = A.Ident(pc.loc, pc.name)
                    elif pc.kind == "positional" and i < len(hdr.ports) and hdr.ports[i].name == pi.name:
                        e = pc.expr
                sym = self.lookup_in(old_scope, e.name) if isinstance(e, A.Ident) else None
                if isinstance(sym, VSym):
                    isc.syms[pi.name] = sym
                else:
                    net = self.b.net(pi.pt.w, self.path + pi.name, pi.pt.signed)
                    s2 = VSym(pi.name, pi.pt, (), net)
                    isc.syms[pi.name] = s2
                    if e is not None and pi.dir == "in":
                        self.b.connect(net, self.assign_val(e, pi.pt.w))
            self.process_items(iface.items, False)
            for nm, s in isc.syms.items():
                if isinstance(s, VSym):
                    isym.members[nm] = s
                    isym.order.append(nm)
        finally:
            self.scope, self.path = old_scope, old_path
            self.item_idx = old_idx
        isym.pkeys = hdr.params
        self.scope.syms[it.name] = isym

    def lookup_in(self, scope, name):
        return scope.lookup(name)
