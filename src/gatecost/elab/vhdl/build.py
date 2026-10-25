"""Module construction, hierarchy and the elaborator front object."""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass, field

from ..._lazy import LazyMap
from ...frontend.vhdl import nodes as N
from ...frontend.vhdl.nodes import walk
from ...ir import Builder, Const, Design, Origin, SigSpec
from . import vtypes as T
from .conc import ConcMixin, base_names
from .core import Ctx, NotConst, Unsupported, int_val
from .decl import DeclMixin
from .expr import ExprMixin
from .inst import InstMixin, flat_ports
from .ops import OpsMixin
from .stmt import StmtMixin
from .values import Scope, Sym, Val


class Elab(ExprMixin, OpsMixin, StmtMixin, DeclMixin, ConcMixin, InstMixin):
    """Evaluator/elaborator bound to one IR builder (one per module under construction)."""

    def __init__(self, ctx, builder):
        self.ctx = ctx
        self.b = builder
        self.file = None
        self.cur_lib = "work"
        self.prefix = ""
        self.proc = None
        self.call_depth = 0
        self.steps = 0
        self.ctl_iters = 0
        self.loop_iters = 0          # unrolled loop-body executions in the current process / top-level call
        self.mem_cache = {}
        self.state_names = set()
        self.mem_names = set()
        self.onehot_types = set()


class BuildCtx(Ctx):
    debug = bool(os.environ.get("GATECOST_VHDL_DEBUG"))

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._scratch = None
        self.mod_units = {}      # module name -> (entity, architecture)

    def scratch(self):
        if self._scratch is None:
            self._scratch = Elab(self, Builder("__scratch__"))
        return self._scratch

    def load_pkg(self, env, generics=None):
        sc = self.scratch()
        old = (sc.file, sc.cur_lib, sc.prefix)
        try:
            sc.load_package(env, generics)
        finally:
            sc.file, sc.cur_lib, sc.prefix = old

    def ensure_module(self, key, ent, arch, gvals, params, cp, modname, parent, node):
        if key in self.design.modules or key in self.building:
            return
        if self.only is not None and not self.in_cone(ent.name) and self.building:
            self.stubbed.add(key)
            return
        if len(self.building) > self.limits["max_hier_depth"]:
            self.diag("LIMIT_REACHED", "instance hierarchy deeper than %d levels" % len(self.building), node.file if hasattr(node, "file") else None)
            return
        self.building.append(key)
        try:
            try:
                build_module(self, ent, arch, gvals, params, cp, key, modname)
            except RecursionError:
                self.diag("LIMIT_REACHED", "recursion limit while elaborating '%s'" % modname, arch.file, arch.line, arch.col)
                build_module(self, ent, arch, gvals, params, cp, key, modname, ports_only=True)
            except Exception as ex:
                if self.debug:
                    raise
                self.diag("UNSUPPORTED_CONSTRUCT", "elaboration of '%s' failed (%s: %s); ports only" % (modname, type(ex).__name__, ex),
                          arch.file, arch.line, arch.col)
                build_module(self, ent, arch, gvals, params, cp, key, modname, ports_only=True)
        finally:
            self.building.pop()
        self.mod_units[modname] = (ent, arch)


def check_duplicate_names(E, ent, arch):
    """VHDL identifiers are case-insensitive: a generic, port, or object declared twice in the entity
    interface / entity and architecture declarative parts is illegal.  The elaborator would silently
    keep one of them, so the clash is reported (DUPLICATE_DECLARATION, affects cost)."""
    seen = {}
    items = [("generic", g, ent.file) for g in ent.generics or ()]
    items += [("port", p, ent.file) for p in ent.ports or ()]
    items += [(d.cls, d, ent.file) for d in ent.decls or () if isinstance(d, N.ObjectDecl)]
    items += [(d.cls, d, arch.file) for d in arch.decls or () if isinstance(d, N.ObjectDecl)]
    for kind, node, file in items:
        key = str(node.name).lower()
        if key in seen:
            pk, pn = seen[key]
            E.ctx.diag("DUPLICATE_DECLARATION",
                       "%s '%s' is already declared as %s '%s' (line %s) in the same declarative region "
                       "(VHDL identifiers are case-insensitive); only one declaration is used, the cost is unreliable"
                       % (kind, node.name, pk, pn.name, getattr(pn, "line", "?")),
                       file, getattr(node, "line", None), getattr(node, "col", None))
        else:
            seen[key] = (kind, node)


def build_module(ctx, ent, arch, gvals, params, cp, key, modname, ports_only=False):
    n_assume = len(ctx.assumptions)
    b = Builder(modname, params, cp, limits=ctx.ir_limits, key=key,
                origin=Origin(ctx.disp(ent.file) if ent.file else "<unknown>", ent.line or 0, ent.col or 0))
    E = Elab(ctx, b)
    E.file = ent.file
    E.cur_lib = ent.library
    lsc = Scope(None, file=ent.file, lib=ent.library)
    E.apply_context(lsc, ent.context)
    if arch.context is not ent.context:
        E.apply_context(lsc, arch.context)
    esc = Scope(lsc, file=ent.file, lib=ent.library)
    for g in ent.generics or ():
        v = gvals.get(g.name)
        if isinstance(v, T.VType):
            esc.declare(Sym("type", g.name, v))
        elif v is not None:
            esc.declare(Sym("const", g.name, v.t, val=v))
    E.prescan([ent, arch])
    check_duplicate_names(E, ent, arch)
    assigned = set()
    for n in walk(arch):
        if isinstance(n, (N.SignalAssign, N.SelectAssign)):
            assigned.update(base_names(n.target))
        elif isinstance(n, N.Instance):
            for a in n.port_map or ():
                if a.actual is not None:
                    assigned.update(base_names(a.actual))
    hints = {k[5:]: v for k, v in params.items() if k.startswith("$len:")}
    for pn, mode, pt, default in E.entity_ports(ent, esc, hints):
        mode = (mode or "in").lower()
        if mode in ("inout", "buffer"):
            direction = "out" if (pn in assigned or mode == "buffer") else "in"
        elif mode == "out":
            direction = "out"
        else:
            direction = "in"
        nets = []
        for fn, ft in flat_ports(pn, pt):
            signed = bool(ft.signed) if ft.kind == "int" else ft.cls == "s"
            nets.append(b.port(fn, direction, ft.width, signed))
        sig = SigSpec.concat([n.sig for n in nets])
        s = Sym("obj", pn, pt, sig=sig)
        s.mode = "port"
        esc.declare_unique(s)
    if ports_only:
        m = b.finalize(check="off")
        ctx.design.add(m)
        m.assumptions.extend(ctx.assumptions[n_assume:])
        return m
    E.declare_items(ent.decls, esc, None)
    asc = Scope(esc, file=arch.file, lib=arch.library, label=arch.name)
    E.file = arch.file
    E.cur_lib = arch.library
    E.declare_items(arch.decls, asc, None)
    E.elab_stmts(arch.stmts, asc)
    m = b.finalize(check="diag")
    for k in range(0):
        pass
    for p in ctx.diags and []:
        pass
    ctx.design.add(m)
    m.assumptions.extend(ctx.assumptions[n_assume:])
    return m


def dep_hash(parsed, ent, arch):
    """Hash of an entity, its architecture and the user packages they (transitively) use."""
    items = []
    h = hashlib.blake2b(digest_size=16)
    h.update(("%s|%s" % (ent.hash, arch.hash if arch is not None else "")).encode())
    seen = set()
    stack = []

    def uses(unit):
        out = []
        ctxs = list(unit.context or []) + [d for d in (getattr(unit, "decls", None) or []) if isinstance(d, N.UseClause)]
        for c in ctxs:
            if isinstance(c, N.UseClause):
                for p in c.paths:
                    if len(p) >= 2:
                        out.append((p[0], p[1]))
        return out

    stack.extend(uses(ent))
    if arch is not None:
        stack.extend(uses(arch))
    while stack:
        lib, pkg = stack.pop()
        for (l, n), p in parsed.packages.items():
            if n == pkg and (lib == "work" or l == lib):
                if (l, n) in seen:
                    continue
                seen.add((l, n))
                items.append(("package", l, n, p.hash))
                stack.extend(uses(p))
                b = parsed.package_bodies.get((l, n))
                if b is not None:
                    items.append(("package_body", l, n, b.hash))
                    stack.extend(uses(b))
    for it in sorted(items, key=str):
        h.update(repr(it).encode())
    return h.hexdigest()


@dataclass
class VhdlElabResult:
    tops: list = field(default_factory=list)            # root entity names (as written in the source)
    top_key: str | None = None
    diagnostics: tuple = ()
    assumptions: tuple = ()
    unit_hashes: dict = field(default_factory=dict)     # module name -> digest of entity + architecture + used packages
    top_keys: dict = field(default_factory=dict)        # root entity name -> Module IR key
    stubbed: tuple = ()                                 # keys not built because of ``only_modules``

    @property
    def parsed_hashes(self):
        return self.unit_hashes


class _Foreign:
    """Adapter exposing a VHDL entity through the foreign-module protocol."""

    def __init__(self, vel, ent):
        self.vel = vel
        self.ent = ent
        self.name = ent.name
        self.parameters = {}
        E = vel.ctx.scratch()
        old = (E.file, E.cur_lib)
        try:
            esc = Scope(E.unit_scope(ent))
            vals = E.bind_generics(ent, esc, lambda g, pos, gt: None)
            self.parameters = {k: E.param_of(v) for k, v in vals.items()}
        finally:
            E.file, E.cur_lib = old

    def _gen(self, params):
        E = self.vel.ctx.scratch()
        esc = Scope(E.unit_scope(self.ent))
        low = {k.lower(): v for k, v in params.items()}

        def provider(g, pos, gt):
            if g.name in low:
                return self.vel.py_to_val(E, low[g.name], gt)
            return None
        vals = E.bind_generics(self.ent, esc, provider)
        return E, esc, vals

    def ports(self, params):
        E, esc, vals = self._gen(params)
        out = []
        for pn, mode, pt, d in E.entity_ports(self.ent, esc):
            for fn, ft in flat_ports(pn, pt):
                direction = "in" if mode in ("in", "linkage") else ("out" if mode in ("out", "buffer") else "inout")
                out.append((fn, direction, ft.width, bool(ft.signed) if ft.kind == "int" else ft.cls == "s"))
        return out

    def elaborate(self, params, const_ports):
        ctx = self.vel.ctx
        E, esc, vals = self._gen(params)
        p = {k: E.param_of(v) for k, v in vals.items()}
        arch = ctx.parsed.selected.get((self.ent.library, self.ent.name))
        if arch is None:
            return None
        key = Design.make_key(self.ent.name, p, const_ports)
        ctx.ensure_module(key, self.ent, arch, vals, p, const_ports, self.ent.name, E, self.ent)
        return key if key in ctx.design.modules else None


class VhdlElaborator:
    """Elaborates VHDL entities into a (shared) :class:`gatecost.ir.Design`."""

    def __init__(self, parsed, design, *, limits=None, side="after", fsm_encoding="binary", propagate_port_constants=True,
                 foreign_lookup=None, ir_limits=None, root=None, only_modules=None):
        self.ctx = BuildCtx(parsed, design, side, limits, fsm_encoding, propagate_port_constants, foreign_lookup, ir_limits,
                            root, only_modules)

    def lookup(self, name):
        """Foreign-module protocol for the other language: the VHDL entity ``name`` or None."""
        parsed = self.ctx.parsed
        ent = parsed.find_entity(name)
        if ent is None:
            low = name.lower()
            for (l, n), e in parsed.entities.items():
                if n == low:
                    ent = e
                    break
        if ent is None or parsed.selected.get((ent.library, ent.name)) is None:
            return None
        return _Foreign(self, ent)

    def py_to_val(self, E, x, gt):
        if isinstance(x, bool):
            return E.fit(Val(T.T_BOOLEAN, None, 1 if x else 0), gt) if gt.kind == "enum" else int_val(int(x))
        if isinstance(x, int):
            return E.fit(int_val(x), gt)
        if isinstance(x, float):
            return Val(T.T_REAL, None, x)
        if isinstance(x, Const):
            return Val(gt, SigSpec((x,)))
        if isinstance(x, str):
            return E.string_val(x, gt if gt.kind == "array" else None)
        return int_val(0)

    def roots(self, top):
        parsed = self.ctx.parsed
        if top is not None:
            low = top.lower()
            ents = [e for (l, n), e in parsed.entities.items() if n == low]
            return ents
        inst = set()
        for u in parsed.units:
            if isinstance(u, N.Architecture):
                for n in walk(u):
                    if isinstance(n, N.Instance) and n.unit:
                        inst.add(n.unit[-1])
        roots = [e for (l, n), e in parsed.entities.items() if n not in inst]
        return roots or list(parsed.entities.values())

    def elaborate(self, top=None, generics=None):
        ctx = self.ctx
        old = sys.getrecursionlimit()
        if old < 30000:
            sys.setrecursionlimit(30000)
        res = VhdlElabResult()
        try:
            ents = self.roots(top)
            if top is not None and not ents:
                ctx.diag("MISSING_UNIT", "top entity '%s' not found" % top)
            for ent in ents:
                try:
                    key = self.elab_root(ent, generics)
                except RecursionError:
                    ctx.diag("LIMIT_REACHED", "recursion limit while elaborating '%s'" % ent.name, ent.file, ent.line, ent.col)
                    key = None
                except Exception as ex:
                    if ctx.debug:
                        raise
                    ctx.diag("UNSUPPORTED_CONSTRUCT", "elaboration of '%s' failed (%s: %s)" % (ent.name, type(ex).__name__, ex),
                             ent.file, ent.line, ent.col)
                    key = None
                if key is not None:
                    res.tops.append(ent.name)
                    res.top_keys[ent.name] = key
        finally:
            if old < 30000:
                sys.setrecursionlimit(old)
        if top is not None and res.tops:
            res.top_key = res.top_keys[res.tops[0]]
        elif len(res.tops) == 1:
            res.top_key = res.top_keys[res.tops[0]]
        if ctx.design.top is None and res.top_key is not None:
            ctx.design.top = res.top_key
        res.stubbed = tuple(sorted(ctx.stubbed))
        res.diagnostics = tuple(ctx.diags)
        res.assumptions = tuple(ctx.assumptions)
        units = dict(ctx.mod_units)
        parsed = ctx.parsed
        res.unit_hashes = LazyMap(lambda: {name: dep_hash(parsed, ent, arch) for name, (ent, arch) in units.items()})
        return res

    def elab_root(self, ent, generics):
        ctx = self.ctx
        parsed = ctx.parsed
        arch = parsed.selected.get((ent.library, ent.name))
        if arch is None:
            ctx.diag("MISSING_UNIT", "entity '%s' has no architecture" % ent.name, ent.file, ent.line, ent.col)
            return None
        E = Elab(ctx, Builder("__top__"))
        E.file = ent.file
        esc = Scope(E.unit_scope(ent))
        low = {k.lower(): v for k, v in (generics or {}).items()}

        def provider(g, pos, gt):
            if g.name in low:
                return self.py_to_val(E, low[g.name], gt)
            return None

        gvals = E.bind_generics(ent, esc, provider)
        params = {k: E.param_of(v) for k, v in gvals.items()}
        key = Design.make_key(ent.name, params, {})
        ctx.ensure_module(key, ent, arch, gvals, params, {}, ent.name, E, ent)
        return key if key in ctx.design.modules else None
