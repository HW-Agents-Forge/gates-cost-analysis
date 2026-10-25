"""Declarations, use clauses, packages and the built-in library."""
from __future__ import annotations

from ...frontend.vhdl import ieee, nodes as N
from ...ir import SigSpec
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, Unsupported, int_val
from .values import FuncDef, PkgEnv, Scope, Sym, Val


class DeclMixin:
    # ------------------------------------------------------------------ context
    def apply_context(self, sc, items):
        for it in items or ():
            try:
                if isinstance(it, N.UseClause):
                    for path in it.paths:
                        self.apply_use(sc, path)
                elif isinstance(it, N.ContextRef):
                    for path in it.paths:
                        cd = self.ctx.parsed.contexts.get((path[0] if len(path) > 1 else "work", path[-1]))
                        if cd is None:
                            for (lib, nm), c in self.ctx.parsed.contexts.items():
                                if nm == path[-1]:
                                    cd = c
                        if cd is not None:
                            self.apply_context(sc, cd.items)
            except Exception as ex:
                if getattr(self.ctx, "debug", False):
                    raise
                self.diag("UNSUPPORTED_CONSTRUCT", "use clause ignored (%s)" % ex, it)

    def apply_use(self, sc, path):
        if len(path) < 2:
            return
        lib, pkg = path[0], path[1]
        last = path[2] if len(path) > 2 else None
        if (lib, pkg) in ieee.KNOWN_PACKAGES:
            if pkg not in sc.pkgs:
                sc.pkgs = list(sc.pkgs) + [pkg]
            if last and last != "all" and pkg == "std_logic_1164":
                pass
            return
        if lib == "work" and sc.lib and self.ctx.parsed.packages.get((sc.lib, pkg)) is None:
            lib_eff = None
        else:
            lib_eff = sc.lib if lib == "work" else lib
        env = self.get_package(lib_eff, pkg)
        if env is None:
            if pkg in ieee.WORK_MODEL_PACKAGES and lib == "work" and pkg not in sc.pkgs:
                sc.pkgs = list(sc.pkgs) + [pkg]
            return
        if last is None:
            sc.declare(Sym("pkg", pkg, val=env))
        elif last == "all":
            sc.uses.append(env)
        else:
            syms = env.scope.names.get(last) if env.scope is not None else None
            for s in syms or ():
                sc.declare(s)

    def get_package(self, lib, name):
        parsed = self.ctx.parsed
        name = name.lower()
        key = None
        if lib is not None and (lib, name) in parsed.packages:
            key = (lib, name)
        elif lib is None or lib == "work":
            for (l, n) in parsed.packages:
                if n == name and (lib is None or l == "work" or True):
                    key = (l, n)
                    if l == (self.lib_of_current()):
                        break
        elif lib is not None and parsed.library_unused(lib):
            for (l, n) in parsed.packages:
                if n == name:
                    key = (l, n)
                    break
        if key is None:
            return None
        env = self.ctx.pkgs.get(key)
        if env is None:
            env = PkgEnv(name, key[0], parsed.packages[key])
            self.ctx.pkgs[key] = env
            self.ctx.load_pkg(env)
        return env

    def lib_of_current(self):
        return getattr(self, "cur_lib", "work")

    def load_package(self, env, generics=None):
        """Populate a package scope (runs on the scratch evaluator)."""
        if env.loaded or env.loading:
            return
        env.loading = True
        u = env.unit
        base = Scope(None, file=u.file, lib=u.library)
        self.apply_context(base, u.context)
        env.scope = Scope(base, file=u.file, lib=u.library, label=u.name)
        self.file = u.file
        self.cur_lib = u.library
        for g in u.generics or ():
            v = (generics or {}).get(g.name)
            if v is not None:
                env.scope.declare(Sym("const", g.name, v.t, val=v))
        self.declare_items(u.decls, env.scope, None)
        body = self.ctx.parsed.package_bodies.get((env.lib, env.name))
        if body is not None:
            self.file = body.file
            bsc = Scope(env.scope, file=body.file, lib=body.library)
            self.apply_context(bsc, body.context)
            self.attach_body(env, body, bsc)
        env.loading = False
        env.loaded = True
        self.ctx.unit_deps.setdefault("__pkg__", set())

    def attach_body(self, env, body, bsc):
        for d in body.decls or ():
            try:
                if isinstance(d, N.Subprogram) and d.has_body:
                    syms = [s for s in env.scope.names.get(d.name, []) if s.kind == "func"]
                    hit = None
                    for s in syms:
                        fd = s.val
                        if fd.body_node is None and len(fd.node.params) == len(d.params) and \
                                [p.name for p in fd.node.params] == [p.name for p in d.params]:
                            hit = s
                            break
                    if hit is None:
                        for s in syms:
                            if s.val.body_node is None and len(s.val.node.params) == len(d.params):
                                hit = s
                                break
                    if hit is not None:
                        hit.val.body_node = d
                        hit.val.scope = bsc if False else hit.val.scope
                    else:
                        env.scope.declare(Sym("func", d.name, val=FuncDef(d, bsc)))
                elif isinstance(d, N.ObjectDecl) and d.cls == "constant" and d.init is not None:
                    s = next((x for x in env.scope.names.get(d.name, []) if x.kind == "const"), None)
                    if s is not None and s.val is None:
                        t = s.t
                        v = self.eval(d.init, bsc, t)
                        s.val = self.fit(v, t, d) if t.width is not None or t.kind in ("int", "real") else v
                else:
                    self.declare_items([d], bsc, None)
            except Exception as ex:
                if getattr(self.ctx, "debug", False):
                    raise
                self.diag("UNSUPPORTED_CONSTRUCT", "package body item skipped (%s)" % ex, d)

    # ------------------------------------------------------------------ declarations
    def declare_items(self, decls, sc, xc):
        for d in decls or ():
            try:
                self.declare_one(d, sc, xc)
            except (RecursionError,):
                raise
            except Exception as ex:
                if getattr(self.ctx, "debug", False):
                    raise
                self.diag("UNSUPPORTED_CONSTRUCT", "declaration skipped (%s: %s)" % (type(ex).__name__, ex), d)

    def declare_one(self, d, sc, xc):
        k = type(d)
        if k is N.ObjectDecl:
            if d.cls == "constant":
                self.decl_const(d, sc)
            elif d.cls == "signal":
                self.decl_obj(d, sc, None, "signal")
            else:
                self.decl_obj(d, sc, xc, "variable")
        elif k is N.TypeDecl:
            self.decl_type(d, sc)
        elif k is N.SubtypeDecl:
            t = self.resolve_subtype(d.subtype, sc)
            t = t.copy(name=d.name)
            sc.declare(Sym("type", d.name, t))
        elif k is N.Subprogram:
            sc.declare(Sym("func", d.name, val=FuncDef(d, sc)))
        elif k is N.ComponentDecl:
            sc.declare(Sym("comp", d.name, node=d, scope=sc))
        elif k is N.AliasDecl:
            self.decl_alias(d, sc)
        elif k is N.UseClause or k is N.ContextRef or k is N.LibraryClause:
            self.apply_context(sc, [d])
        elif k is N.AttributeSpec:
            self.decl_attr_spec(d, sc)
        elif k is N.PackageInst:
            self.decl_package_inst(d, sc)
        elif k is N.ViewDecl:
            sc.declare(Sym("view", d.name, node=d, scope=sc, extra=False))

    def decl_alias(self, d, sc):
        tgt = d.target
        if isinstance(tgt, N.Attr) and tgt.name == "converse":       # alias slave_view is master_view'converse
            vw = self.find_view(tgt, sc)
            if vw is not None:
                sc.declare(Sym("view", d.name, node=vw[0], scope=vw[1], extra=vw[2]))
                return
        if isinstance(tgt, (N.Ident, N.Select)):
            syms = self.name_syms(tgt, sc)
            if syms and syms[0].kind in ("type", "func", "pkg", "comp", "view"):
                for s in syms:
                    c = Sym(s.kind, d.name, s.t, val=s.val, node=s.node, scope=s.scope, extra=s.extra)
                    sc.declare(c)
                return
        t = self.resolve_subtype(d.subtype, sc) if d.subtype is not None else None
        sc.declare(Sym("alias", d.name, t, node=tgt, scope=sc))

    def decl_package_inst(self, d, sc):
        tgt = d.target
        path = list(tgt)
        name = path[-1]
        lib = path[-2] if len(path) > 1 else None
        env0 = self.get_package(None if lib == "work" else lib, name)
        if env0 is None and name in ("fixed_generic_pkg", "float_generic_pkg"):
            self.decl_ieee_generic_package(d, sc, name)
            return
        if env0 is None:
            self.diag("MISSING_UNIT", "generic package '%s' not found" % name, d)
            return
        gens = {}
        gl = env0.unit.generics or []
        for i, a in enumerate(d.generic_map or ()):
            fname = a.formal.name if a.formal is not None and isinstance(a.formal, N.Ident) else (gl[i].name if i < len(gl) else None)
            if fname is None:
                continue
            try:
                gens[fname] = self.eval(a.actual, sc)
            except Exception:
                pass
        env = PkgEnv(name, env0.lib, env0.unit)
        self.ctx.load_pkg(env, gens)
        sc.declare(Sym("pkg", d.name, val=env))

    def decl_ieee_generic_package(self, d, sc, name):
        """``package p is new ieee.fixed_generic_pkg|float_generic_pkg generic map (...)``: the built-in model is used,
        the generic actuals only change its defaults (rounding / overflow style, float format)."""
        model = {}
        gl = ("fixed_round_style", "fixed_overflow_style", "fixed_guard_bits", "no_warning") \
            if name == "fixed_generic_pkg" else \
            ("float_exponent_width", "float_fraction_width", "float_round_style", "float_denormalize",
             "float_check_error", "float_guard_bits", "no_warning", "fixed_pkg")
        for i, a in enumerate(d.generic_map or ()):
            f = a.formal.name if a.formal is not None and isinstance(a.formal, N.Ident) else (gl[i] if i < len(gl) else None)
            if f is None or a.actual is None:
                continue
            ln = self.flat_name(a.actual)
            if f in ("fixed_round_style", "fixed_overflow_style") and ln:
                model[f] = ln[-1]
            elif f in ("float_exponent_width", "float_fraction_width"):
                try:
                    model[f] = self.const_int(self.eval(a.actual, sc))
                except Exception:
                    pass
        sc.fmodel = dict(sc.fmodel or {}, **model)

    def decl_attr_spec(self, d, sc):
        if d.attr not in ("enum_encoding", "fsm_encoding", "syn_encoding"):
            return
        try:
            val = d.value
            txt = val.value if isinstance(val, N.Literal) and val.kind == "str" else ""
        except Exception:
            return
        txt = (txt or "").lower()
        for nm in d.targets or ():
            for s in sc.lookup(nm):
                if s.kind != "type" or s.t.kind != "enum" or not s.t.lits:
                    continue
                t = s.t
                n = len(t.lits)
                toks = txt.split()
                if "one" in txt and "hot" in txt or txt in ("onehot", "one-hot", "one_hot"):
                    if n > 2:
                        s.t = self.enum_variant(t, True)
                elif len(toks) == n and all(set(x) <= set("01") for x in toks):
                    w = max(len(x) for x in toks)
                    nt = t.copy(width=w, enc=[(int(x, 2), 0) for x in toks])
                    s.t = nt
                elif txt in ("sequential", "binary", "gray"):
                    pass

    def enum_variant(self, t, onehot):
        key = ("oh", id(t), onehot)
        v = self.ctx.entity_iface.get(key)
        if v is None:
            n = len(t.lits)
            if onehot:
                v = t.copy(width=n, enc=[(1 << i, 0) for i in range(n)], onehot=True)
            else:
                v = t
            self.ctx.entity_iface[key] = v
        return v

    def state_type(self, t, name):
        """One-hot variant of an enumeration used as a state register (profile policy)."""
        if self.ctx.fsm_encoding == "one_hot" and t.kind == "enum" and t.lits and len(t.lits) > 2 and \
                t.cls is None and not t.onehot and name in self.state_names:
            return self.enum_variant(t, True)
        if t.kind == "enum" and t.lits and t.cls is None and not t.onehot and t.name in self.onehot_types:
            return self.enum_variant(t, True)
        return t

    def decl_type(self, d, sc):
        df = d.definition
        name = d.name
        if isinstance(df, N.EnumType):
            t = T.enum_type(name, df.literals)
            sc.declare(Sym("type", name, t))
            for i, lit in enumerate(df.literals):
                sc.declare(Sym("enumlit", lit, t, pos=i))
            return
        if isinstance(df, N.ArrayType):
            elem = self.resolve_subtype(df.element, sc)
            idxs = df.indexes or []
            t = self.build_array_type(name, elem, idxs, df.unconstrained, sc)
            sc.declare(Sym("type", name, t))
            return
        if isinstance(df, N.RecordType):
            fields = [(f.name, self.resolve_subtype(f.subtype, sc)) for f in df.fields]
            t = T.record_type(name, fields)
            sc.declare(Sym("type", name, t))
            return
        if isinstance(df, N.PhysicalType):
            # physical types are integers counted in the base unit; the units are constants of that type
            rg = df.range
            lo, hi = self.const_int(self.eval(rg.left, sc)), self.const_int(self.eval(rg.right, sc))
            if rg.direction == "downto":
                lo, hi = hi, lo
            t = T.int_type(name, lo, hi)
            sc.declare(Sym("type", name, t))
            vals = {}
            for u in df.units:
                if u.ref is None:
                    v = 1
                else:
                    v = int(round(float(self.const_val(self.eval(u.value, sc))))) * vals.get(u.ref.lower(), 1)
                vals[u.name.lower()] = v
                sc.declare(Sym("const", u.name, t, val=int_val(v)))
            return
        if isinstance(df, N.RangeType):
            l = self.eval(df.range.left, sc) if isinstance(df.range, N.Range) else None
            r = self.eval(df.range.right, sc) if isinstance(df.range, N.Range) else None
            if l is None:
                raise Unsupported("range type")
            if l.t.kind == "real" or r.t.kind == "real":
                t = T.VType("real", name, "real")
            else:
                lo, hi = self.const_int(l), self.const_int(r)
                if df.range.direction == "downto":
                    lo, hi = hi, lo
                t = T.int_type(name, lo, hi)
            sc.declare(Sym("type", name, t))
            return
        t = T.T_UNKNOWN.copy(name=name)
        sc.declare(Sym("type", name, t))
        if df is not None:
            self.diag("UNSUPPORTED_CONSTRUCT", "type '%s' is not synthesizable" % name, d, severity="info", affects=False)

    def build_array_type(self, name, elem, idxs, unconstrained, sc):
        if not idxs:
            raise Unsupported("array type without index")
        if unconstrained:
            it = None
            try:
                it = self.try_type(idxs[0], sc) if not isinstance(idxs[0], N.Range) else None
            except Exception:
                it = None
            t = T.array_type(name, elem, index=it, cls=None)
            for ix in idxs[1:]:
                t = T.array_type(name, t)
            return t
        cur = elem
        for i in reversed(range(len(idxs))):
            ix = idxs[i]
            l, r, dr = self.eval_range(ix, sc)
            it = self.try_type(ix, sc) if not isinstance(ix, N.Range) else None
            cur = T.array_type(name if i == 0 else "", cur, l, r, dr, index=it)
        return cur

    # ------------------------------------------------------------------ objects
    def decl_const(self, d, sc):
        t = self.resolve_subtype(d.subtype, sc) if d.subtype is not None else None
        if d.init is None:
            sc.declare(Sym("const", d.name, t, val=None))
            return
        v = self.eval(d.init, sc, t)
        if t is None:
            t = v.t
        if t.kind == "array" and t.width is None:
            v = self.convert_to(t, v, d)
            t = v.t
        else:
            v = self.fit(v, t, d)
        if t.kind == "enum" and v.c is None and v.sig is not None:
            v = self.mk_elem(t, v.sig, v.z) if t.width == v.sig.width else v
        sc.declare(Sym("const", d.name, t, val=v))

    def mem_candidate(self, name, t):
        return name in self.mem_names and t.kind == "array" and t.left is not None and t.length and t.length >= 2 \
            and ((t.ewidth or 0) > 1 or t.elem.kind in ("array", "record"))

    def decl_obj(self, d, sc, xc, mode):
        t = self.resolve_subtype(d.subtype, sc)
        t = self.state_type(t, d.name)
        if t.kind == "array" and t.left is not None and t.length == 0:
            # null array (``std_logic_vector(-1 downto 0)``): no bits, assignments to it are no-ops
            s = Sym("obj", d.name, t, sig=SigSpec(()))
            s.mode = mode
            sc.declare(s)
            return
        if t.width is None or t.width <= 0:
            if t.kind == "array" and t.left is None and d.init is not None:
                v = self.eval(d.init, sc, t)
                t = self.convert_to(t, v, d).t
            elif t.kind in ("real", "other"):
                self.diag("UNSUPPORTED_CONSTRUCT", "%s '%s' of a non-synthesizable type is ignored" % (mode, d.name), d,
                          severity="info", affects=False)
                sc.declare(Sym("const", d.name, t, val=Val(t, None, 0.0 if t.kind == "real" else 0)))
                return
            else:
                self.ctx.assume("%s '%s' has no constant width; 1 bit assumed" % (mode, d.name))
                t = T.T_STD_LOGIC
        name = self.prefix + d.name
        if self.mem_candidate(d.name, t):
            mem = self.b.memory(name, t.length, t.ewidth, origin=self.org(d))
            s = Sym("mem", d.name, t, val=mem)
            s.mode = mode
            sc.declare(s)
            return
        signed = bool(t.signed) if t.kind == "int" else t.cls in T.SIGNED_CLASSES
        net = self.b.net(t.width, name, signed)
        s = Sym("obj", d.name, t, sig=net.sig)
        s.mode = mode
        if mode == "variable" and xc is not None:
            s.owner = xc
            if xc.clocked and d.name not in xc.state_vars:
                xc.blk.mark_variable(net)
            if d.init is not None and (not xc.clocked or d.name not in xc.state_vars):
                try:
                    v = self.fit(self.eval(d.init, sc, t), t, d)
                    xc.blk.assign(net, self.sig_of(v))
                except Exception:
                    pass
        sc.declare(s)
        return net

    # ------------------------------------------------------------------ memories
    def mem_read(self, sym, args, sc, node):
        t = sym.t
        mem = sym.val
        iv = self.eval(args[0].actual, sc, t.index)
        x, n, lo = self.index_pos_sig(t, iv, node)
        key = (id(mem), x.key())
        cache = self.mem_cache
        s = cache.get(key)
        if s is None:
            s = mem.read(x, origin=self.org(node))
            cache[key] = s
        return Val(t.elem, s)
