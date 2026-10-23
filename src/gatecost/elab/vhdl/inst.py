"""Instantiation: entity/component resolution, generic and port maps, hierarchy sharing, foreign modules."""
from __future__ import annotations

from ...frontend.vhdl import nodes as N
from ...ir import Const, Design, SigSpec
from ...ir.sigspec import mask
from . import vtypes as T
from .core import NotConst, Unsupported, int_val
from .values import Scope, Sym, Val


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
    def param_of(self, v):
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
        self.ctx.diag("UNRESOLVED_PARAM", "generic '%s' of '%s' has no value or default; assumed %s" % (
            g.name, ent.name, "32" if gt.kind == "int" else "0"), ent.file, g.line, g.col)
        self.ctx.assume("unresolved parameter %s of %s assumed 32" % (g.name, ent.name) if gt.kind == "int"
                        else "unresolved parameter %s of %s assumed 0" % (g.name, ent.name))
        if gt.kind == "int":
            return self.fit(int_val(32), gt)
        if gt.kind == "enum" and gt.cls == "bool":
            return self.fit(Val(gt, None, 0), gt)
        if gt.width:
            return Val(gt, SigSpec.const(0, gt.width))
        return int_val(32)

    def bind_generics(self, ent, esc, provider):
        """Evaluate entity generics in ``esc`` (declaring them); returns {name: Val}."""
        vals = {}
        for pos, g in enumerate(ent.generics or ()):
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

    def entity_ports(self, ent, esc):
        out = []
        for p in ent.ports or ():
            try:
                pt = self.resolve_subtype(p.subtype, esc)
            except Exception:
                pt = T.T_STD_LOGIC
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

        gvals = self.bind_generics(ent, esc, provider)
        params = {k: self.param_of(v) for k, v in gvals.items()}
        ports = self.entity_ports(ent, esc)
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
            if isinstance(f.prefix, N.Ident) and f.prefix.name in ports and False:
                pass
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
            else:
                self.diag("UNSUPPORTED_CONSTRUCT", "instance output to an indexed/aggregate target is not supported", a)

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


def base_name_of(e):
    while isinstance(e, (N.Apply, N.Slice, N.Select)):
        e = e.prefix
    return e.name if isinstance(e, N.Ident) else None
