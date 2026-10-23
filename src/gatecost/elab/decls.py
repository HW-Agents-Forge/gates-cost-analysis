"""Declarations: types, parameters, typedefs, enums, imports, functions."""
from __future__ import annotations

from gatecost.frontend.verilog import ast as A
from gatecost.ir import SigSpec, mask

from .symbols import BIT, FSym, PSym, PType, TSym, V, vec

MAX_BITS = 1 << 20
_ATOMS = {"byte": (8, True), "shortint": (16, True), "int": (32, True), "longint": (64, True),
          "integer": (32, True), "time": (64, False)}


def pack(dims, base: PType, signed: bool) -> PType:
    """Prepend packed dimensions ``dims`` (outermost first) to ``base``."""
    if not dims:
        if base.signed == signed:
            return base
        return PType(base.w, signed, base.dims, base.base, base.fields, base.enum)
    total = base.w
    for l, r in dims:
        total *= abs(l - r) + 1
    if total > MAX_BITS:
        total = MAX_BITS          # the IR rejects the module (net width limit); keep arithmetic cheap
    nb = base.base if base.dims else base
    return PType(total, signed, tuple(dims) + tuple(base.dims), nb, None)


class DeclMixin:
    # ------------------------------------------------------------------ dimensions & types
    def eval_dims(self, dims):
        out = []
        for d in dims or ():
            if d.kind == "range":
                l = self.cint(d.left, None)
                r = self.cint(d.right, None)
                if l is None or r is None:
                    self.diag("UNRESOLVED_PARAM", "dimension bound is not constant; assumed [31:0]", d, True)
                    self.assume("non-constant dimension assumed to be 32 bits")
                    l, r = 31, 0
                out.append((l, r))
            elif d.kind == "size":
                n = self.cint(d.left, None)
                if n is None or n < 1:
                    self.diag("UNRESOLVED_PARAM", "array size is not constant; assumed 32", d, True)
                    n = 32
                out.append((0, n - 1))
            else:
                self.diag("UNSUPPORTED_CONSTRUCT", f"{d.kind} dimension", d, True)
                out.append((0, 0))
        return out

    def resolve_dtype(self, dt) -> PType:
        if dt is None:
            return BIT
        c = type(dt)
        if c is A.IntType:
            kind = dt.kind
            if kind in _ATOMS:
                w, sg = _ATOMS[kind]
                if dt.signed is not None:
                    sg = bool(dt.signed)
                base = PType(w, sg, ((w - 1, 0),), BIT)
                dims = self.eval_dims(dt.dims)
                return pack(dims, base, sg) if dims else base
            sg = bool(dt.signed)
            dims = self.eval_dims(dt.dims)
            return pack(dims, BIT, sg) if dims else (BIT if not sg else PType(1, True, (), BIT))
        if c is A.ImplicitType:
            sg = bool(dt.signed)
            dims = self.eval_dims(dt.dims)
            return pack(dims, BIT, sg) if dims else (BIT if not sg else PType(1, True, (), BIT))
        if c is A.NamedType:
            return self._resolve_named(dt)
        if c is A.EnumType:
            return self._resolve_enum(dt)
        if c is A.StructType:
            return self._resolve_struct(dt)
        if c is A.OtherType:
            if dt.kind in ("signed", "unsigned"):
                return PType(1, dt.kind == "signed", (), BIT)
            self.diag("UNSUPPORTED_CONSTRUCT", f"type '{dt.kind}' is not synthesizable; assumed 1 bit", dt, True)
            return BIT
        return BIT

    def _resolve_named(self, dt: A.NamedType) -> PType:
        n = dt.name
        sym = None
        if isinstance(n, A.Ident):
            sym = self.lookup(n.name)
        elif isinstance(n, A.ScopedName):
            sym = self.lookup_scoped(n)
        if not isinstance(sym, TSym):
            nm = getattr(n, "name", "?")
            self.diag("UNRESOLVED_NAME", f"type '{nm}' is not declared; assumed 32 bits", dt, True)
            self.assume(f"unknown type '{nm}' assumed to be 32 bits wide")
            pt = vec(32)
        else:
            pt = sym.pt
        dims = self.eval_dims(dt.dims)
        return pack(dims, pt, pt.signed) if dims else pt

    def _resolve_enum(self, dt: A.EnumType) -> PType:
        base = self.resolve_dtype(dt.base) if dt.base is not None else None
        vals = []
        nxt = 0
        scope = self.scope
        for m in dt.members:
            if m.init is not None:
                v = self.cint(m.init, None)
                if v is None:
                    self.diag("UNRESOLVED_PARAM", f"enum value of '{m.name}' is not constant", m, True)
                    v = nxt
                nxt = v
            names = [m.name]
            if m.count:
                names = [f"{m.name}{i}" for i in range(self.cint(m.count, 1) or 1)]
            for nm in names:
                vals.append((nm, nxt))
                nxt += 1
        if base is None:
            top = max([v for _n, v in vals] + [0])
            lo = min([v for _n, v in vals] + [0])
            w = max(1, top.bit_length())
            sg = lo < 0
            if sg:
                w = max(w, (-lo).bit_length() + 1, top.bit_length() + 1)
            pt = PType(w, sg, ((w - 1, 0),), BIT)
        else:
            pt = PType(base.w, base.signed, base.dims or ((base.w - 1, 0),), base.base)
        pt = PType(pt.w, pt.signed, pt.dims, pt.base, None, {n: v for n, v in vals})
        for nm, v in vals:
            scope.syms[nm] = PSym(V(SigSpec.const(v & mask(pt.w), pt.w), pt.signed), pt)
        dims = self.eval_dims(dt.dims)
        return pack(dims, pt, pt.signed) if dims else pt

    def _resolve_struct(self, dt: A.StructType) -> PType:
        mems = []
        for m in dt.members:
            mp = self.resolve_dtype(m.dtype)
            dims = self.eval_dims(m.dims)
            if dims:
                mp = pack(dims, mp, mp.signed)
            mems.append((m.name, mp))
        fields = {}
        if dt.is_union:
            w = max([p.w for _n, p in mems] + [1])
            for nm, p in mems:
                fields[nm] = (0, p)
        else:
            off = 0
            for nm, p in reversed(mems):
                fields[nm] = (off, p)
                off += p.w
            w = max(1, off)
            fields = dict(reversed(list(fields.items())))
        pt = PType(w, bool(dt.signed), (), None, fields)
        dims = self.eval_dims(dt.dims)
        return pack(dims, pt, pt.signed) if dims else pt

    # ------------------------------------------------------------------ parameters etc.
    def coerce(self, v: V, pt: PType | None) -> V:
        if pt is None:
            return v
        return V(v.sig.resize(pt.w, v.signed), pt.signed)

    def declare_param(self, pd: A.ParamDecl, override=None):
        scope = self.scope
        if pd.is_type:
            if override is not None and isinstance(override, PType):
                pt = override
            elif isinstance(pd.init, A.DataType):
                pt = self.resolve_dtype(pd.init)
            else:
                self.diag("UNRESOLVED_PARAM", f"type parameter '{pd.name}' has no value; assumed 32 bits",
                          pd, True)
                self.assume(f"type parameter '{pd.name}' assumed to be a 32-bit vector")
                pt = vec(32)
            scope.syms[pd.name] = TSym(pt)
            return
        pt = self.resolve_dtype(pd.dtype) if pd.dtype is not None else None
        if override is not None and isinstance(override, V):
            v = override
        elif pd.init is not None:
            v = self.val(pd.init)
            if v.sig.const_value() is None:
                self.diag("UNRESOLVED_PARAM", f"value of '{pd.name}' is not constant; assumed 0", pd, True)
                self.assume(f"non-constant parameter '{pd.name}' assumed to be 0")
                v = V(SigSpec.const(0, v.w or 32), v.signed)
        else:
            self.diag("UNRESOLVED_PARAM", f"parameter '{pd.name}' has no value; assumed to be 32", pd, True)
            self.assume(f"parameter '{pd.name}' without value assumed to be 32")
            v = self.const_v(32)
        v = self.coerce(v, pt)
        scope.syms[pd.name] = PSym(v, pt)

    def declare_typedef(self, td: A.Typedef):
        pt = self.resolve_dtype(td.dtype)
        if td.dims:
            dims = self.eval_dims(td.dims)
            pt = pack(dims, pt, pt.signed)
        self.scope.syms[td.name] = TSym(pt)

    def declare_import(self, im: A.Import):
        sc = self.ctx.pkg_scope(im.pkg, self)
        if sc is None:
            self.diag("MISSING_UNIT", f"package '{im.pkg}' not found", im, True)
            return
        if im.name == "*":
            if sc not in self.scope.wild:
                self.scope.wild.append(sc)
        else:
            sym = sc.syms.get(im.name)
            if sym is None:
                self.diag("UNRESOLVED_NAME", f"'{im.name}' not found in package '{im.pkg}'", im, True)
            else:
                self.scope.syms[im.name] = sym

    def register_function(self, f: A.Function):
        self.scope.syms[f.name] = FSym(f, self.scope)

    def declare_a0(self, it, overrides=None):
        """Declarations that do not create hardware (parameters, types, imports, functions)."""
        c = type(it)
        if c is A.ParamDecl:
            ov = overrides.get(it.name) if (overrides and not it.local) else None
            self.declare_param(it, ov)
        elif c is A.Typedef:
            self.declare_typedef(it)
        elif c is A.Import:
            self.declare_import(it)
        elif c is A.Function:
            self.register_function(it)
        elif c is A.Genvar:
            pass

    # ------------------------------------------------------------------ functions
    def find_func(self, call: A.Call):
        f = call.func
        sym = None
        if isinstance(f, A.Ident):
            sc = self.scope
            while sc is not None:
                sym = sc.syms.get(f.name)
                if isinstance(sym, FSym):
                    break
                for pk in sc.wild:
                    sym = pk.syms.get(f.name)
                    if isinstance(sym, FSym):
                        break
                if isinstance(sym, FSym):
                    break
                sc = sc.parent
        elif isinstance(f, A.ScopedName):
            sym = self.lookup_scoped(f)
        if isinstance(sym, FSym):
            return sym
        nm = getattr(f, "name", "?")
        key = ("fn", nm)
        if key not in self._unres:
            self._unres.add(key)
            self.diag("UNRESOLVED_NAME", f"function '{nm}' not found; result assumed unknown", call, True)
        return None

    def func_rettype(self, f: FSym):
        if f.rt is None:
            ast = f.ast
            rt = False
            if not ast.is_task:
                if isinstance(ast.rettype, A.OtherType) and ast.rettype.kind == "void":
                    rt = False
                elif ast.rettype is None:
                    rt = BIT
                else:
                    old = self.scope
                    self.scope = f.scope
                    try:
                        rt = self.resolve_dtype(ast.rettype)
                    finally:
                        self.scope = old
            f.rt = rt
        return f.rt or None
