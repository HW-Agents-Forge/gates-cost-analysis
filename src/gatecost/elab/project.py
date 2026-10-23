"""Project-level elaboration context and the Verilog/SystemVerilog entry points."""
from __future__ import annotations

import os

from gatecost.frontend.verilog import ast as A
from gatecost.ir import Builder, Const, Design, Origin, SigSpec, mask
from gatecost.ir.limits import Limits as IRLimits
from gatecost.result import Diagnostic

from .base import BaseEv
from .module import ModElab, _guard, build_header, param_decls
from .scan import scan_module
from .symbols import ISym, PSym, Scope, V

DEFAULT_LIMITS = {
    "macro_depth": 64,
    "include_depth": 32,
    "generate_iterations": 4096,
    "loop_unroll": 1024,
    "call_depth": 32,
    "max_instances": 100_000,
    "expr_depth": 500,
    "const_func_steps": 100_000,
    "const_func_total_steps": 600_000,     # per function, summed over its calls (bounds wall time)
    "max_hier_depth": 128,
    "const_port_variants": 256,
}


class Ctx:
    """State shared by all module elaborations of one project."""

    def __init__(self, parsed, root, limits=None, side="after", only=None, propagate_const_ports=True):
        self.parsed = parsed
        self.root = os.fspath(root) if root is not None else None
        self.modmap = parsed.module_map()
        self.ifmap = parsed.interface_map()
        self.pkgmap = parsed.package_map()
        self.udps = set(parsed.udps)
        self.limits = dict(DEFAULT_LIMITS)
        self.func_steps = {}
        for k, v in (limits or {}).items():
            if k in self.limits and isinstance(v, int):
                self.limits[k] = v
        il = IRLimits()
        self.ir_limits = IRLimits(
            max_nodes=int((limits or {}).get("max_nodes", il.max_nodes)),
            max_nets=int((limits or {}).get("max_nets", il.max_nets)),
            max_net_width=int((limits or {}).get("max_net_width", il.max_net_width)),
            max_mem_depth=int((limits or {}).get("max_mem_depth", il.max_mem_depth)))
        self.side = side
        self.design = Design()
        self.scratch = Builder("$scratch")
        self.sink = []
        self._sink_seen = set()
        self.assumptions = set()
        self._origins = {}
        self._disp = {}
        self.headers = {}
        self.inprogress = set()
        self.pkg_scopes = {}
        self.stubbed = set()
        self.total_inst = 0
        self._inst_warned = False
        self.opts = {"const_ports": propagate_const_ports}
        self.only = set(only) if only is not None else None
        self.needed = set()
        self.variants = {}
        self.missing_names = set()
        self.module_digest = {}
        self.roots_hint = None
        self.needed_hint = None
        self.scans = {}                # id(module) -> (module, ModScan)
        self.foreign = None            # callable(name) -> foreign module of the other language, or None

    def scan(self, mod):
        e = self.scans.get(id(mod))
        if e is None:
            e = self.scans[id(mod)] = (mod, scan_module(mod))
        return e[1]

    # ------------------------------------------------------------------ locations & diagnostics
    def disp(self, f):
        d = self._disp.get(f)
        if d is None:
            d = f
            if self.root and f and os.path.isabs(f):
                try:
                    r = os.path.relpath(f, self.root)
                    if not r.startswith(".."):
                        d = r.replace(os.sep, "/")
                except ValueError:
                    pass
            self._disp[f] = d
        return d

    def origin(self, loc):
        o = self._origins.get(loc)
        if o is None:
            f, line, col = loc
            mu = self.parsed.macro_uses.get((f, line))
            if mu:
                _m, df, dl = mu[0][0], mu[0][1], mu[0][2]
                o = Origin(self.disp(f), line, col, self.disp(df) if df else None, dl or 0, 0)
            else:
                o = Origin(self.disp(f), line, col)
            self._origins[loc] = o
        return o

    def add_diag(self, code, message, severity, affects, origin):
        key = (code, message, origin.file if origin else None, origin.line if origin else None)
        if key in self._sink_seen:
            return
        self._sink_seen.add(key)
        self.sink.append(Diagnostic(code, message, severity, affects, self.side,
                                    origin.file if origin else None, origin.line if origin else None,
                                    origin.column if origin else None))

    # ------------------------------------------------------------------ packages
    def unit_scope(self):
        if "$unit" in self.pkgmap:
            return self.pkg_scope("$unit", None)
        return None

    def pkg_scope(self, name, requester):
        if name in self.pkg_scopes:
            return self.pkg_scopes[name]
        pk = self.pkgmap.get(name)
        if pk is None:
            self.pkg_scopes[name] = None
            return None
        sc = Scope(None, "", name)
        self.pkg_scopes[name] = sc
        ev = BaseEv(self, sc, self.scratch)
        ev._cur = pk
        if name != "$unit":
            u = self.unit_scope()
            if u is not None:
                sc.wild.append(u)
        for it in pk.items:
            if isinstance(it, A.Function):
                ev.register_function(it)
        for it in pk.items:
            if type(it) in (A.ParamDecl, A.Typedef, A.Import):
                _guard(ev, ev.declare_a0, it, None)
        return sc

    # ------------------------------------------------------------------ interfaces
    def modport_dirs(self, iface, modport):
        if not modport:
            return None
        for it in iface.items:
            if isinstance(it, A.Modport) and it.name == modport:
                m = {"input": "in", "output": "out", "inout": "out", "ref": "in"}
                return {p.name: m.get(p.direction, "in") for p in it.ports}
        return None

    def assigned_members(self, mod, port):
        out = set()
        for it in mod.items:
            for n in A.walk(it):
                if type(n) in (A.ContAssign, A.ProcAssign):
                    stack = [n.lhs]
                    while stack:
                        x = stack.pop()
                        if isinstance(x, A.Concat):
                            stack.extend(x.items)
                            continue
                        while isinstance(x, (A.Index, A.Select)):
                            x = x.base
                        if isinstance(x, A.Member) and isinstance(x.base, A.Ident) and x.base.name == port:
                            out.add(x.name)
        return out

    def lookup_foreign(self, name):
        if self.foreign is None:
            return None
        try:
            return self.foreign(name)
        except Exception:
            return None

    def verilog_lookup(self, name):
        """Foreign lookup offered to the VHDL elaborator: exact name, then a unique case-insensitive match."""
        mod = self.modmap.get(name)
        if mod is None:
            low = name.lower()
            c = [m for n, m in self.modmap.items() if n.lower() == low]
            mod = c[0] if len(c) == 1 else None
        if mod is None:
            return None
        return VerilogForeign(self, mod)

    def iface_of(self, pi):
        return self.ifmap.get(pi.iface)

    # ------------------------------------------------------------------ headers & modules
    def get_header(self, mod, ov):
        parts = []
        for k in sorted(ov):
            v = ov[k]
            if isinstance(v, V):
                c = v.sig.const_value()
                parts.append((k, ("v", c.value, c.width, c.xmask, v.signed)))
            elif isinstance(v, ISym):
                parts.append((k, ("i", v.iface.name, tuple(sorted((str(a), str(b)) for a, b in getattr(v, "pkeys", {}).items())),
                                  tuple((m, s.pt.w) for m, s in v.members.items()))))
            else:
                parts.append((k, ("t", v.describe())))
        key = (id(mod), tuple(parts))
        h = self.headers.get(key)
        if h is None:
            h = build_header(self, mod, ov)
            for k, v in ov.items():
                if k.startswith("$if:"):
                    h.iface_syms[k[4:]] = v
            self.headers[key] = h
        return h

    def count_instances(self, n):
        self.total_inst += n

    def missing(self, name, msg, ev, node):
        self.missing_names.add(name)
        ev.diag("MISSING_MODULE", msg, node, True)

    def ensure_module(self, mod, hdr, cp, depth, node):
        name = mod.name
        if cp:
            vk = (name, tuple(sorted(hdr.params.items(), key=lambda kv: kv[0])).__repr__())
            s = self.variants.setdefault(vk, set())
            sig = Design.make_key(name, hdr.params, cp)
            if sig not in s and len(s) >= self.limits["const_port_variants"]:
                cp = {}
            else:
                s.add(sig)
        key = Design.make_key(name, hdr.params, cp)
        if key in self.design.modules or key in self.inprogress:
            return key
        if self.only is not None and name not in self.needed:
            self.stubbed.add(key)
            return key
        self.inprogress.add(key)
        try:
            skip = depth > self.limits["max_hier_depth"]
            m = None
            if not skip:
                try:
                    me = ModElab(self, hdr, cp, depth)
                    m = me.run()
                except RecursionError:
                    skip = True
                except Exception as exc:       # never abort the whole analysis
                    if os.environ.get("GATECOST_ELAB_DEBUG"):
                        raise
                    skip = True
                    self.add_diag("INTERNAL", f"internal error elaborating '{name}' "
                                  f"({type(exc).__name__}: {exc}); module replaced by a black box",
                                  "error", True, self.origin(mod.loc) if mod.loc else None)
            if m is None or skip and m is None:
                m = self.port_stub(mod, hdr, cp, key, depth)
            self.design.add(m)
        finally:
            self.inprogress.discard(key)
        return key

    def port_stub(self, mod, hdr, cp, key, depth):
        b = Builder(mod.name, hdr.params, cp, limits=self.ir_limits)
        for pi in hdr.ports:
            if pi.iface is not None and pi.member_of is None:
                continue
            b.port(pi.name, pi.dir, pi.pt.w, pi.pt.signed)
        o = self.origin(mod.loc) if mod.loc else None
        b.module.diag("LIMIT_REACHED", f"module '{mod.name}' replaced by a black box "
                      f"(hierarchy depth limit {self.limits['max_hier_depth']} or internal error)",
                      "warning", True, o)
        b.failed = True
        return b.finalize(check="off")


class VerilogForeign:
    """Foreign-module protocol (see ``gatecost.elab.vhdl``) for a Verilog/SystemVerilog module."""

    def __init__(self, ctx, mod):
        self.ctx = ctx
        self.mod = mod
        self.name = mod.name
        hdr = ctx.get_header(mod, {})
        self.parameters = {}
        for pd in param_decls(mod):
            sym = hdr.scope.syms.get(pd.name)
            if isinstance(sym, PSym):
                c = sym.val.sig.const_value()
                if c is not None and not c.xmask:
                    self.parameters[pd.name] = c.as_signed() if sym.val.signed else c.value

    def _hdr(self, params):
        low = {k.lower(): v for k, v in (params or {}).items()}
        ov = {}
        for pd in param_decls(self.mod):
            if pd.is_type:
                continue
            v = (params or {}).get(pd.name, low.get(pd.name.lower()))
            if isinstance(v, bool):
                v = int(v)
            if isinstance(v, int):
                ov[pd.name] = V(SigSpec.const(v & mask(32), 32), True)
            elif isinstance(v, Const):
                ov[pd.name] = V(SigSpec((v,)), False)
        return self.ctx.get_header(self.mod, ov)

    def ports(self, params):
        return [(p.name, p.dir, p.pt.w, bool(p.pt.signed)) for p in self._hdr(params).ports
                if p.iface is None]

    def elaborate(self, params, const_ports):
        hdr = self._hdr(params)
        cp = dict(const_ports or {}) if self.ctx.opts["const_ports"] else {}
        return self.ctx.ensure_module(self.mod, hdr, cp, 1, None)


def find_roots(parsed, exclude_lower=(), ctx=None):
    """Modules never instantiated by another module (file order); ``(roots, all names)``."""
    inst = set()
    for m in list(parsed.modules) + list(parsed.interfaces):
        inst.update(x for x in (ctx.scan(m) if ctx is not None else scan_module(m)).insts if x != m.name)
    names = []
    seen = set()
    for m in parsed.modules:
        if m.name not in seen:
            seen.add(m.name)
            names.append(m.name)
    roots = sorted(n for n in names if n not in inst and n.lower() not in exclude_lower)
    return roots, names


def static_graph(parsed, ctx=None):
    g = {}
    for m in parsed.modules:
        g[m.name] = set((ctx.scan(m) if ctx is not None else scan_module(m)).insts)
    return g


def needed_modules(parsed, changed, tops, ctx=None):
    g = static_graph(parsed, ctx)
    rev = {}
    for p, cs in g.items():
        for c in cs:
            rev.setdefault(c, set()).add(p)
    need = set(changed) | set(tops)
    stack = list(changed)
    while stack:
        x = stack.pop()
        for p in rev.get(x, ()):
            if p not in need:
                need.add(p)
                stack.append(p)
    return need


def elaborate_verilog(parsed, ctx: Ctx, top: str | None, exclude_lower=(), have_other_language=False):
    """Elaborate the roots (or ``top``) of a parsed design into ``ctx.design``.

    Returns ``(top_name, top_keys, tops)``."""
    if ctx.roots_hint is not None:           # the caller parsed only part of the project and knows the roots
        roots = list(ctx.roots_hint)
        names = list(dict.fromkeys(m.name for m in parsed.modules))
    else:
        roots, names = find_roots(parsed, exclude_lower, ctx)
    if have_other_language and not roots and top is None:
        return None, {}, []
    tops = list(roots)
    if top is not None:
        if top not in ctx.modmap:
            return None, {}, tops
        chosen = [top]
    elif roots:
        chosen = roots
    else:
        chosen = names[:1]
        ctx.add_diag("TOP_AMBIGUOUS", "no module is free of instantiation (instantiation cycle); the first "
                     "module is used as the top", "info", False, None)
        tops = list(names)
    if ctx.only is not None:
        ctx.needed = set(ctx.needed_hint) if ctx.needed_hint is not None else needed_modules(parsed, ctx.only, chosen, ctx)
    elif len(chosen) > 1:
        ctx.add_diag("TOP_AMBIGUOUS", "several top-level modules (" + ", ".join(chosen) + "); each root is "
                     "elaborated separately, use --top to select one", "info", False, None)
    keys = {}
    for nm in chosen:
        mod = ctx.modmap[nm]
        hdr = ctx.get_header(mod, {})
        keys[nm] = ctx.ensure_module(mod, hdr, {}, 0, None)
    return chosen[0] if chosen else None, keys, tops
