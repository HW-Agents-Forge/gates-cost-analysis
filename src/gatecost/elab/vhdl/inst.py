"""Instantiation: entity/component resolution, generic and port maps, hierarchy sharing, foreign modules."""
from __future__ import annotations

from ...frontend.vhdl import nodes as N
from ...ir import Const, Design, LowerBlock, SigSpec
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, Unsupported, int_val
from .stmt import XC
from .values import Scope, Sym, Val


DEFAULT_UNCONSTRAINED_PORT_LEN = 8


class ForeignModule:
    """Protocol for modules of another language (documented in ``__init__``).

    ``name``; ``parameters``: {name: default}; ``ports(params) -> [(name, 'in'|'out'|'inout', width, signed)]``;
    ``elaborate(params, const_ports) -> module key | None`` (adds the module to the shared Design).
    """


def flat_ports(name, t):
    if t.kind == "record":
        out = []
        for fname, ft in t.fields:
            out.extend(flat_ports("%s.%s" % (name, fname), ft))
        return out
    return [(name, t)]


class InstMixin:
    # ------------------------------------------------------------------ parameters
    def type_key(self, t):
        if t.kind == "array" and t.left is not None:
            return "type:%s(%s %s %s)" % (t.name, t.left, t.dir, t.right)
        return "type:%s/%s" % (t.name, t.width)

    def type_from_actual(self, x, sc):
        """VHDL-2008 generic type actual: a type mark, optionally with an index constraint."""
        t = self.try_type(x, sc)
        if t is not None:
            return t
        if isinstance(x, N.Slice):
            base = self.try_type(x.prefix, sc)
            if base is not None and base.kind == "array" and isinstance(x.range, N.Range):
                l, r, d = self.eval_range(x.range, sc)
                return T.constrain(base, l, r, d)
        if isinstance(x, N.Apply) and len(x.args) == 1:
            base = self.try_type(x.prefix, sc)
            if base is not None and base.kind == "array" and self.is_range_expr(x.args[0].actual, sc):
                l, r, d = self.eval_range(x.args[0].actual, sc)
                return T.constrain(base, l, r, d)
        return None

    def param_of(self, v):
        if isinstance(v, T.VType):
            return self.type_key(v)
        t = v.t
        if t.kind == "int" or t.kind == "real":
            return v.c if v.c is not None else self.const_val(v)
        if t.kind == "enum":
            if t.cls == "bool":
                return bool(self.const_int(v))
            return self.const_int(v) if v.c is not None else self.const_int(v)
        if t.kind == "array":
            s = self.sig_of(v)
            c = s.const_value()
            if t.cls == "str" and c is not None:
                return "".join(chr((c.value >> (8 * (s.width // 8 - 1 - i))) & 255) for i in range(s.width // 8))
            if c is not None:
                return c
        return str(id(v))

    def default_generic(self, g, gt, ent):
        guess = None
        if gt.kind == "int":
            n = 32
            if getattr(gt, "low", None) is not None and getattr(gt, "high", None) is not None:
                n = min(max(n, gt.low), gt.high)       # 32, brought into the subtype's range
            guess = self.fit(int_val(n), gt)
            shown = str(n)
        else:
            shown = "0"
        self.ctx.diag("UNRESOLVED_PARAM", "generic '%s' of '%s' has no value or default; assumed %s" % (
            g.name, ent.name, shown), ent.file, g.line, g.col)
        self.ctx.assume("unresolved parameter %s of %s assumed %s" % (g.name, ent.name, shown))
        if gt.kind == "int":
            return guess
        if gt.kind == "enum" and gt.cls == "bool":
            return self.fit(Val(gt, None, 0), gt)
        if gt.width:
            return Val(gt, SigSpec.const(0, gt.width))
        return int_val(32)

    def bind_generics(self, ent, esc, provider, type_provider=None):
        """Evaluate entity generics in ``esc`` (declaring them); returns {name: Val} (a ``VType`` for generic types)."""
        vals = {}
        for pos, g in enumerate(ent.generics or ()):
            if g.kind == "type":
                gt = type_provider(g, pos) if type_provider is not None else None
                if gt is None:
                    gt = T.T_STD_LOGIC
                    self.ctx.assume("generic type '%s' of '%s' has no actual; std_logic assumed" % (g.name, ent.name))
                esc.declare(Sym("type", g.name, gt))
                vals[g.name] = gt
                continue
            if g.kind != "constant":
                continue
            try:
                gt = self.resolve_subtype(g.subtype, esc) if g.subtype is not None else T.T_INTEGER
                v = provider(g, pos, gt)
                if v is None and g.default is not None:
                    v = self.eval(g.default, esc, gt)
                if v is None:
                    v = self.default_generic(g, gt, ent)
                if gt.kind == "array" and gt.width is None:
                    v = self.convert_to(gt, v, g)
                    gt = v.t
                else:
                    v = self.fit(v, gt, g)
                esc.declare(Sym("const", g.name, gt, val=v))
                vals[g.name] = v
            except Exception as ex:
                if getattr(self.ctx, "debug", False):
                    raise
                self.diag("UNSUPPORTED_CONSTRUCT", "generic '%s' skipped (%s)" % (g.name, ex), g)
        return vals

    def port_length_hints(self, ent, esc, s, sc):
        """Element counts of unconstrained entity ports, taken from the port map actuals (VHDL: an unconstrained
        port takes its index range from the association)."""
        hints = {}
        formals = {}

        def fpath(f):
            parts = []
            while isinstance(f, N.Select):
                parts.append(f.suffix)
                f = f.prefix
            if isinstance(f, N.Ident):
                return ".".join([f.name] + parts[::-1])
            return None

        for i, a in enumerate(s.port_map or ()):
            if a.formal is None:
                if i < len(ent.ports or ()):
                    formals[ent.ports[i].name] = a
            else:
                k = fpath(a.formal)
                if k is not None:
                    formals[k] = a

        def actual_width(p, a):
            if (p.mode or "in") in ("in", "linkage"):
                return self.sig_of(self.eval(a.actual, sc)).width
            return self.lvalue(a.actual, sc).width

        for p in ent.ports or ():
            try:
                pt = self.resolve_subtype(p.subtype, esc)
            except Exception:
                continue
            if pt.kind == "record" and pt.width is None:
                whole = formals.get(p.name)
                whole_t = None
                if whole is not None and whole.actual is not None and not isinstance(whole.actual, N.Open):
                    try:
                        whole_t = self.eval(whole.actual, sc).t
                    except Exception:
                        whole_t = None
                for path, ft in T.unconstrained_paths(pt):
                    a = formals.get(p.name + "." + path)
                    ew = ft.elem.width or 1
                    try:
                        if a is not None and a.actual is not None and not isinstance(a.actual, N.Open):
                            w = actual_width(p, a)
                        else:
                            at = T.field_at(whole_t, path) if whole_t is not None else None
                            w = at.width if at is not None else None
                    except Exception:
                        continue
                    if w and w % ew == 0:
                        hints[p.name + "." + path] = w // ew
                continue
            if pt.kind != "array" or pt.width is not None:
                continue
            a = formals.get(p.name)
            if a is None or a.actual is None or isinstance(a.actual, N.Open):
                continue
            ew = pt.elem.width or 1
            try:
                w = actual_width(p, a)
            except Exception:
                continue
            if w and w % ew == 0:
                hints[p.name] = w // ew
        return hints

    def entity_ports(self, ent, esc, hints=None):
        out = []
        for p in ent.ports or ():
            try:
                pt = self.resolve_subtype(p.subtype, esc)
            except Exception:
                pt = T.T_STD_LOGIC
            if pt.kind == "record" and pt.width is None:
                new = {}
                for path, ft in T.unconstrained_paths(pt):
                    n = (hints or {}).get(p.name + "." + path)
                    if n is None:
                        n = DEFAULT_UNCONSTRAINED_PORT_LEN
                        self.ctx.assume("unconstrained record field '%s.%s' of '%s' has no actual to take its range "
                                        "from; %d elements assumed" % (p.name, path, ent.name, n))
                    new[path] = T.constrain(ft, n - 1, 0, "downto")
                pt = T.constrain_fields(pt, new)
            if (pt.width is None or pt.width <= 0) and hints and p.name in hints and pt.kind == "array":
                pt = T.constrain(pt, hints[p.name] - 1, 0, "downto")
            if (pt.width is None or pt.width <= 0) and pt.kind == "array" and pt.left is None and pt.elem is not None \
                    and pt.elem.width:
                self.ctx.assume("unconstrained port '%s' of '%s' has no actual to take its range from; "
                                "%d elements assumed" % (p.name, ent.name, DEFAULT_UNCONSTRAINED_PORT_LEN))
                pt = T.constrain(pt, DEFAULT_UNCONSTRAINED_PORT_LEN - 1, 0, "downto")
            if pt.width is None or pt.width <= 0:
                self.ctx.assume("port '%s' of '%s' has no constant width; 1 bit assumed" % (p.name, ent.name))
                pt = T.T_STD_LOGIC
            out.append((p.name, p.mode or "in", pt, p.default))
            esc.declare_unique(Sym("obj", p.name, pt, sig=SigSpec.x(pt.width)))     # visible to later port types
        return out

    def unit_scope(self, unit):
        lsc = Scope(None, file=unit.file, lib=unit.library)
        self.cur_lib = unit.library
        self.apply_context(lsc, unit.context)
        return lsc

    # ------------------------------------------------------------------ resolution
    def find_entity_for(self, name, lib, sc):
        parsed = self.ctx.parsed
        if lib is not None and lib != "work":
            e = parsed.entities.get((lib, name))
            if e is not None:
                return e
        e = parsed.entities.get((sc.lib or "work", name))
        if e is not None:
            return e
        return parsed.find_entity(name)

    def arch_for(self, ent, arch_name):
        parsed = self.ctx.parsed
        lst = parsed.architectures.get((ent.library, ent.name), [])
        if arch_name:
            for a in lst:
                if a.name == arch_name.lower():
                    return a
        return parsed.selected.get((ent.library, ent.name))

    # ------------------------------------------------------------------ instance
    def elab_instance(self, s, sc):
        if s.kind == "configuration":
            cfg = self.ctx.parsed.configurations.get((sc.lib, s.unit[-1])) or None
            if cfg is None:
                self.diag("MISSING_UNIT", "configuration '%s' not found" % s.unit[-1], s)
                return
            name, lib = cfg.entity, None
        else:
            name = s.unit[-1]
            lib = s.unit[-2] if len(s.unit) > 1 else None
        comp = None
        if s.kind == "component":
            cs = [x for x in sc.lookup(name) if x.kind == "comp"]
            comp = cs[0].node if cs else None
        ent = self.find_entity_for(name, lib, sc)
        label = self.prefix + (s.label or name)
        o = self.org(s)
        if self.ctx.instances >= self.ctx.limits["max_instances"]:
            self.diag("LIMIT_REACHED", "total instance limit %d reached; '%s' not elaborated" % (self.ctx.limits["max_instances"], label), s)
            return
        self.ctx.instances += 1
        if ent is None:
            fm = self.lookup_foreign(name)
            if fm is not None:
                return self.inst_foreign(s, sc, fm, label)
            return self.inst_blackbox(s, sc, name, comp, label)
        arch = self.arch_for(ent, s.arch if s.kind == "entity" else None)
        if arch is None:
            self.diag("MISSING_UNIT", "entity '%s' has no architecture" % ent.name, s)
            return self.inst_blackbox(s, sc, name, comp, label)
        esc = Scope(self.unit_scope(ent))
        gmap = {}
        gpos = []
        for i, a in enumerate(s.generic_map or ()):
            if a.formal is not None and isinstance(a.formal, N.Ident):
                gmap[a.formal.name] = a
            else:
                gpos.append(a)
        gl = [g for g in (ent.generics or ()) if g.kind == "constant"]

        def provider(g, pos, gt):
            a = gmap.get(g.name)
            if a is None and pos < len(gpos) and not gmap:
                a = gpos[pos]
            if a is None or isinstance(a.actual, N.Open):
                return None
            return self.eval(a.actual, sc, gt)

        def type_provider(g, pos):
            a = gmap.get(g.name)
            if a is None and pos < len(gpos) and not gmap:
                a = gpos[pos]
            if a is None or isinstance(a.actual, N.Open):
                return None
            return self.type_from_actual(a.actual, sc)

        gvals = self.bind_generics(ent, esc, provider, type_provider)
        params = {k: self.param_of(v) for k, v in gvals.items()}
        hints = self.port_length_hints(ent, esc, s, sc)
        for k, v in hints.items():
            params["$len:" + k] = v
        ports = self.entity_ports(ent, esc, hints)
        self.declare_items(ent.decls, esc, None)
        inputs, outs, conns = self.map_ports(s, sc, ports, label)
        cp = Design.const_port_signature(inputs) if self.ctx.propagate else {}
        modname = ent.name if arch is self.ctx.parsed.selected.get((ent.library, ent.name)) else "%s.%s" % (ent.name, arch.name)
        key = Design.make_key(modname, params, cp)
        self.ctx.ensure_module(key, ent, arch, gvals, params, cp, modname, self, s)
        outputs = {}
        for pn, w in outs.items():
            outputs[pn] = w
        res = self.b.inst(key, label, inputs, outputs, 1, origin=o)
        self.connect_outputs(conns, res, s)

    # ------------------------------------------------------------------ port maps
    def formal_part(self, f, ports, sc):
        """(port name, bit offset, width, subtype) addressed by a formal designator."""
        if isinstance(f, N.Ident):
            p = ports.get(f.name)
            if p is None:
                raise Unsupported("unknown formal '%s'" % f.name)
            return f.name, 0, p[2].width, p[2]
        if isinstance(f, N.Select):
            pn, off, w, t = self.formal_part(f.prefix, ports, sc)
            if t.kind != "record" or f.suffix not in t.offs:
                raise Unsupported("bad formal record field")
            o, fw = t.offs[f.suffix]
            return pn, off + o, fw, dict(t.fields)[f.suffix]
        if isinstance(f, N.Apply):
            if (isinstance(f.prefix, N.Ident) and f.prefix.name not in ports and len(f.args) == 1
                    and f.prefix.name in CONVERSIONS):
                # conversion function / type conversion on an output formal: the bits are the same
                return self.formal_part(f.args[0].actual, ports, sc)
            pn, off, w, t = self.formal_part(f.prefix, ports, sc)
            ex = f.args[0].actual
            if self.is_range_expr(ex, sc):
                l, r, d = self.eval_range(ex, sc)
                return self._slice_part(pn, off, t, l, r, d)
            i = self.const_int(self.eval(ex, sc))
            p = T.index_to_pos(t, i)
            ew = t.ewidth or 1
            return pn, off + p * ew, ew, t.elem
        if isinstance(f, N.Slice):
            pn, off, w, t = self.formal_part(f.prefix, ports, sc)
            l, r, d = self.eval_range(f.range, sc)
            return self._slice_part(pn, off, t, l, r, d)
        raise Unsupported("formal designator")

    def _slice_part(self, pn, off, t, l, r, d):
        lo_i, hi_i = min(l, r), max(l, r)
        ew = t.ewidth or 1
        if t.dir == "downto":
            p_lo, p_hi = lo_i - t.right, hi_i - t.right
        else:
            p_lo, p_hi = t.right - hi_i, t.right - lo_i
        return pn, off + p_lo * ew, (p_hi - p_lo + 1) * ew, T.constrain(t, l, r, d)

    def map_ports(self, s, sc, ports, label):
        """Evaluate a port map: returns (inputs {flat: SigSpec}, outputs {flat: width}, connections)."""
        pd = {p[0]: p for p in ports}
        pieces = {}     # port -> [(off, width, assoc)]
        order = [p[0] for p in ports]
        for i, a in enumerate(s.port_map or ()):
            try:
                if a.formal is None:
                    if i >= len(order):
                        continue
                    pn = order[i]
                    pt = pd[pn][2]
                    part = (pn, 0, pt.width, pt)
                else:
                    part = self.formal_part(a.formal, pd, sc)
                pieces.setdefault(part[0], []).append((part, a))
            except Exception as ex:
                if getattr(self.ctx, "debug", False):
                    raise
                self.diag("UNSUPPORTED_CONSTRUCT", "port association skipped (%s)" % ex, a)
        inputs = {}
        outs = {}
        conns = []
        for pn, mode, pt, default in ports:
            flats = flat_ports(pn, pt)
            lst = pieces.get(pn, [])
            if mode in ("in", "linkage"):
                full = None
                covered = []
                for part, a in lst:
                    if isinstance(a.actual, N.Open):
                        continue
                    try:
                        v = self.eval(a.actual, sc, part[3])
                        v = self.fit(v, part[3], a)
                        sg = self.sig_of(v)
                        if sg.width != part[2]:
                            sg = sg.resize(part[2], False)
                        covered.append((part[1], sg))
                    except Exception as ex:
                        if getattr(self.ctx, "debug", False):
                            raise
                        self.diag("UNSUPPORTED_CONSTRUCT", "port '%s' actual skipped (%s)" % (pn, ex), a)
                if not covered and default is not None:
                    try:
                        dv = self.fit(self.eval(default, sc, pt), pt)
                        covered.append((0, self.sig_of(dv)))
                    except Exception:
                        pass
                bits = SigSpec.x(pt.width)
                for off, sg in sorted(covered, key=lambda x: x[0]):
                    bits = SigSpec.concat([bits.slice(0, off), sg, bits.slice(off + sg.width)]) if off + sg.width <= pt.width else bits
                self.split_flat(flats, bits, inputs)
            else:
                for fn, ft in flats:
                    outs[fn] = ft.width
                for part, a in lst:
                    if isinstance(a.actual, N.Open):
                        continue
                    try:
                        lv = self.lvalue(a.actual, sc)
                        if len(lst) == 1 and len(flats) == 1 and lv.parts is None and lv.mem is None and not lv.dyn \
                                and part[1] == 0 and part[2] == pt.width:
                            sl = self.lv_static_sig(lv).single_slice()
                            if sl is not None and sl.lo == 0 and sl.width == sl.net.width == pt.width \
                                    and sl.net.port is None and not self.b.module.driven.get(sl.net.id, 0):
                                outs[flats[0][0]] = sl.net       # the instance drives the net directly
                                continue
                        conns.append((pn, flats, part, lv, a))
                    except Exception as ex:
                        if getattr(self.ctx, "debug", False):
                            raise
                        self.diag("UNSUPPORTED_CONSTRUCT", "port '%s' actual is not assignable (%s)" % (pn, ex), a)
        return inputs, outs, conns

    def split_flat(self, flats, bits, inputs):
        off = 0
        for fn, ft in flats:
            inputs[fn] = bits.slice(off, ft.width)
            off += ft.width

    def connect_outputs(self, conns, res, s):
        for pn, flats, part, lv, a in conns:
            full = SigSpec.concat([res[fn] for fn, _ in flats])
            piece = full.slice(part[1], part[2])
            if lv.parts is None and lv.mem is None and not lv.dyn:
                tgt = self.lv_static_sig(lv)
                if piece.width != tgt.width:
                    piece = piece.resize(tgt.width, False)
                self.b.connect(tgt, piece, self.org(a))
                continue
            # indexed / aggregate / memory target: a small combinational block distributes the instance output
            try:
                blk = LowerBlock(self.b, "comb", origin=self.org(a))
                xc = XC(blk, False, None, "concurrent")
                saved = self.proc
                self.proc = xc
                try:
                    self.do_assign(xc, lv, Val(lv.t, piece.resize(lv.width, False)) if lv.t is not None else Val(T.T_SLV, piece.resize(lv.width, False)), a)
                    blk.finish()
                finally:
                    self.proc = saved
            except Exception as ex:
                if getattr(self.ctx, "debug", False):
                    raise
                self.diag("UNSUPPORTED_CONSTRUCT", "instance output to this target is not supported (%s)" % ex, a)

    # ------------------------------------------------------------------ black boxes / foreign
    def inst_blackbox(self, s, sc, name, comp, label):
        self.diag("MISSING_MODULE", "entity or module '%s' not found; instance '%s' is a black box" % (name, s.label or name), s)
        inputs, outs = {}, {}
        conns = []
        dirs = {}
        if comp is not None:
            for p in comp.ports or ():
                try:
                    dirs[p.name] = (p.mode or "in", self.resolve_subtype(p.subtype, sc))
                except Exception:
                    dirs[p.name] = (p.mode or "in", None)
        for i, a in enumerate(s.port_map or ()):
            if a.actual is None or isinstance(a.actual, N.Open):
                continue
            pn = a.formal.name if isinstance(a.formal, N.Ident) else (a.formal and base_name_of(a.formal)) or ("p%d" % i)
            mode, pt = dirs.get(pn, ("in", None))
            try:
                if mode in ("in", "linkage") or pn not in dirs:
                    v = self.eval(a.actual, sc, pt)
                    inputs[pn] = self.sig_of(v)
                else:
                    lv = self.lvalue(a.actual, sc)
                    outs[pn] = lv.width
                    conns.append((pn, [(pn, lv.t)], (pn, 0, lv.width, lv.t), lv, a))
            except Exception as ex:
                self.diag("UNSUPPORTED_CONSTRUCT", "black box port '%s' skipped (%s)" % (pn, ex), a)
        res = self.b.blackbox(name, label, inputs, outs, 1, origin=self.org(s))
        self.connect_outputs(conns, res, s)

    def lookup_foreign(self, name):
        fl = self.ctx.foreign_lookup
        if fl is None:
            return None
        for n in (name, name.lower()):
            try:
                fm = fl(n)
            except Exception:
                fm = None
            if fm is not None:
                return fm
        return None

    def inst_foreign(self, s, sc, fm, label):
        gmap = {}
        defaults = dict(getattr(fm, "parameters", {}) or {})
        for i, a in enumerate(s.generic_map or ()):
            if a.formal is not None and isinstance(a.formal, N.Ident):
                nm = a.formal.name
            else:
                names = list(defaults)
                nm = names[i] if i < len(names) else None
            if nm is None or isinstance(a.actual, N.Open):
                continue
            real = next((k for k in defaults if k.lower() == nm.lower()), nm)
            try:
                gmap[real] = self.param_of(self.eval(a.actual, sc))
            except Exception as ex:
                self.diag("UNSUPPORTED_CONSTRUCT", "generic '%s' of foreign module skipped (%s)" % (nm, ex), a)
        params = dict(defaults)
        params.update(gmap)
        plist = fm.ports(params)
        inputs, outs, conns = {}, {}, []
        byname = {}
        for i, a in enumerate(s.port_map or ()):
            if a.formal is not None and isinstance(a.formal, N.Ident):
                byname[a.formal.name] = a
            elif i < len(plist):
                byname[plist[i][0].lower()] = a
        for pn, d, w, sg in plist:
            a = byname.get(pn.lower())
            if a is None or isinstance(a.actual, N.Open):
                if d == "in":
                    inputs[pn] = SigSpec.x(w)
                else:
                    outs[pn] = w
                continue
            try:
                if d == "in":
                    v = self.eval(a.actual, sc)
                    inputs[pn] = self.sig_of(v).resize(w, self.is_signed(v))
                else:
                    lv = self.lvalue(a.actual, sc)
                    outs[pn] = w
                    conns.append((pn, [(pn, lv.t)], (pn, 0, min(w, lv.width), lv.t), lv, a))
            except Exception as ex:
                self.diag("UNSUPPORTED_CONSTRUCT", "foreign port '%s' skipped (%s)" % (pn, ex), a)
        cp = Design.const_port_signature(inputs) if self.ctx.propagate else {}
        key = fm.elaborate(params, cp)
        if key is None:
            self.diag("MISSING_MODULE", "foreign module '%s' could not be elaborated; black box" % fm.name, s)
            res = self.b.blackbox(fm.name, label, inputs, outs, 1, origin=self.org(s))
        else:
            res = self.b.inst(key, label, inputs, outs, 1, origin=self.org(s))
        self.connect_outputs(conns, res, s)


CONVERSIONS = frozenset(("std_logic_vector", "std_ulogic_vector", "unsigned", "signed", "bit_vector", "to_stdlogicvector",
                         "to_stdulogicvector", "to_bitvector", "to_bit_vector", "to_slv", "to_unsigned_vec"))


def base_name_of(e):
    while isinstance(e, (N.Apply, N.Slice, N.Select)):
        e = e.prefix
    return e.name if isinstance(e, N.Ident) else None
