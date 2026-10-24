"""The differ engine (DIFF.md). See ``gatecost.diff`` for the public API and fact conventions."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import isqrt

from ..cost.delta import confidence as _confidence
from ..cost.delta import delta_range
from ..cost.tree import CostOp, DesignCostTree, ModuleCost
from ..result import Confidence, Contributor, Diagnostic, DiffResult, Ge3
from .align import Aligner

Z = Ge3(0, 0, 0)


def _add(a: Ge3, b: Ge3) -> Ge3:
    return Ge3(a.min + b.min, a.typ + b.typ, a.max + b.max)


def _sub(a: Ge3, b: Ge3) -> Ge3:
    return Ge3(a.min - b.min, a.typ - b.typ, a.max - b.max)


def _mulk(a: Ge3, k: int) -> Ge3:
    return Ge3(a.min * k, a.typ * k, a.max * k)


def _neg(a: Ge3) -> Ge3:
    return Ge3(-a.min, -a.typ, -a.max)


def _nz(a: Ge3) -> bool:
    return bool(a.min or a.typ or a.max)


def _num(milli: int):
    return milli // 1000 if milli % 1000 == 0 else round(milli / 1000, 3)


def _gd(g: Ge3) -> dict:
    return {"min": g.min, "typ": g.typ, "max": g.max}


def _ged(g: Ge3) -> dict:
    return {"min": _num(g.min), "typ": _num(g.typ), "max": _num(g.max)}


def _sge(milli: int) -> str:
    q = (abs(milli) + 500) // 1000
    v = -q if milli < 0 else q
    return f"+{v}" if v > 0 else str(v)


def _fmt_params(params: tuple[tuple[str, str], ...]) -> str:
    return "{" + ",".join(f"{k}={v}" for k, v in params) + "}" if params else ""


# --------------------------------------------------------------------------- records


@dataclass(slots=True)
class Pair:
    """A matched (or one-sided) module elaboration with its instance counts."""

    mb: ModuleCost | None
    ma: ModuleCost | None
    nb: int
    na: int
    status: str  # changed | added | removed | renamed
    intr: Ge3 = Z
    mult: Ge3 = Z

    @property
    def name(self) -> str:
        return (self.ma or self.mb).name  # type: ignore[union-attr]

    @property
    def params(self) -> tuple[tuple[str, str], ...]:
        return (self.ma or self.mb).parameters  # type: ignore[union-attr]


class Item:
    """Atomic contribution: its scenario deltas sum exactly to the total delta."""

    __slots__ = (
        "kind", "pair", "file", "line", "side", "category", "quality", "op", "widths", "reason",
        "description", "d", "per", "instances", "sq", "cause", "flags", "module",
    )

    def __init__(self, **kw):
        self.cause = None
        self.flags = ()
        self.per = None
        self.sq = 0
        self.op = ""
        self.widths = ()
        for k, v in kw.items():
            setattr(self, k, v)


# --------------------------------------------------------------------------- classification


def _changed_names(db, da, both: bool = False) -> list[str]:
    """Names whose value differs; with ``both`` only names present on the two sides count."""
    if not isinstance(db, Mapping) and not isinstance(da, Mapping):
        return []
    db = db if isinstance(db, Mapping) else {}
    da = da if isinstance(da, Mapping) else {}
    names = (set(db) & set(da)) if both else (set(db) | set(da))
    return sorted((n for n in names if db.get(n) != da.get(n)), key=str)


def _has(tags, t) -> bool:
    return t in tags


def classify(ob: CostOp, oa: CostOp) -> tuple[str, str | None, list[str]]:
    """Reason for a modified pair by comparing facts in the fixed order of DIFF.md section 5."""
    if oa.op == "BLACKBOX" or ob.op == "BLACKBOX":
        return "blackbox", None, []
    fb, fa = ob.facts, oa.facts
    names = _changed_names(fb.get("params"), fa.get("params"), True)
    if names:
        return "parameter", "param", names
    names = _changed_names(fb.get("consts"), fa.get("consts"))
    if names:
        return "constant", "const", names
    names = _changed_names(fb.get("widths"), fa.get("widths"), True)
    if names or ob.effective_widths != oa.effective_widths or ob.declared_widths != oa.declared_widths:
        return "width", "width", names
    if fb.get("struct") != fa.get("struct"):
        return "structure", None, []
    sb, sa = _has(ob.tags, "shared"), _has(oa.tags, "shared")
    if sb != sa or (sb and len(ob.members) != len(oa.members)) or fb.get("sharing") != fa.get("sharing"):
        return "sharing", None, []
    fb_, fa_ = _has(ob.tags, "fused"), _has(oa.tags, "fused")
    if fb_ != fa_ or (fb_ and len(ob.members) != len(oa.members)) or fb.get("fusion") != fa.get("fusion"):
        return "fusion", None, []
    if fb.get("reg") != fa.get("reg") or (ob.category == "seq" and set(ob.breakdown) != set(oa.breakdown)):
        return "register", None, []
    if ob.arch != oa.arch:
        return "architecture", None, []
    return "structure", None, []


def _base_desc(op: CostOp) -> str:
    if op.description:
        return op.description
    w = op.effective_widths or op.declared_widths
    return f"{op.op} {'x'.join(str(x) for x in w)}" if w else op.op


def _width_str(op: CostOp) -> str:
    w = op.effective_widths or op.declared_widths
    return "x".join(f"w{x}" for x in w)


def _describe(reason: str, ob: CostOp | None, oa: CostOp | None, names: list[str]) -> str:
    if oa is None:
        return _base_desc(ob)  # type: ignore[arg-type]
    if ob is None or reason in ("added", "removed", "blackbox"):
        return _base_desc(oa)
    if reason == "width":
        wb, wa = ob.effective_widths or ob.declared_widths, oa.effective_widths or oa.declared_widths
        if len(wb) == len(wa):
            diff = [(x, y) for x, y in zip(wb, wa) if x != y]
            if diff:
                return f"{oa.op} " + ",".join(f"w{x}->w{y}" for x, y in diff)
        return f"{oa.op} {_width_str(ob)}->{_width_str(oa)}"
    if reason == "constant":
        n = names[0] if names else "const"
        return f"{oa.op} constant {n} {ob.facts.get('consts', {}).get(n)}->{oa.facts.get('consts', {}).get(n)}"
    if reason == "parameter":
        n = names[0]
        return f"{oa.op} parameter {n} {ob.facts.get('params', {}).get(n)}->{oa.facts.get('params', {}).get(n)}"
    if reason == "sharing":
        return f"{oa.op} " + ("now shared" if "shared" in oa.tags else "no longer shared")
    if reason == "fusion":
        return f"{oa.op} " + ("now fused" if "fused" in oa.tags else "no longer fused")
    if reason == "register":
        return f"{oa.op} register attributes changed"
    if reason == "architecture":
        return f"{oa.op} architecture {ob.arch.get('typ')}->{oa.arch.get('typ')}"
    return f"{_base_desc(oa)} (structure changed)"


# --------------------------------------------------------------------------- matching


def _skey(o: CostOp):
    return (o.line, o.column, o.id)


def match_ops(obs, oas, aligner: Aligner):
    """Operation matching tiers 1-4. Returns (pairs [(ob, oa, tier)], removed, added)."""
    obs = sorted(obs, key=_skey)
    oas = sorted(oas, key=_skey)
    pairs: list[tuple[CostOp, CostOp, int]] = []
    used_a: set[int] = set()
    idx: dict = defaultdict(list)
    for o in oas:
        if o.named_key:
            idx[o.named_key].append(o)
    rem_b: list[CostOp] = []
    for ob in obs:
        lst = idx.get(ob.named_key) if ob.named_key else None
        if lst:
            oa = lst.pop(0)
            used_a.add(oa.id)
            pairs.append((ob, oa, 1))
        else:
            rem_b.append(ob)
    rem_a = [o for o in oas if o.id not in used_a]
    if rem_b and rem_a:
        sidx: dict = defaultdict(list)
        for o in rem_a:
            if o.shape_key:
                sidx[o.shape_key].append(o)
        rb2: list[CostOp] = []
        for ob in rem_b:
            lst = sidx.get(ob.shape_key) if ob.shape_key else None
            if lst:
                oa = lst.pop(0)
                used_a.add(oa.id)
                pairs.append((ob, oa, 2))
            else:
                rb2.append(ob)
        rem_b = rb2
        rem_a = [o for o in rem_a if o.id not in used_a]
    if rem_b and rem_a:
        by_kind: dict = defaultdict(list)
        for o in rem_a:
            by_kind[(o.op, o.file)].append(o)
        cands = []
        for ob in rem_b:
            m = aligner.map_line(ob.file, ob.line)
            if m is None:
                continue
            afile, lo, hi, pt = m
            for oa in by_kind.get((ob.op, afile), ()):
                if lo <= oa.line <= hi:
                    cands.append((abs(oa.line - pt), ob.line, ob.id, oa.line, oa.id, ob, oa))
        cands.sort(key=lambda c: c[:5])
        used_b: set[int] = set()
        for _, _, _, _, _, ob, oa in cands:
            if ob.id in used_b or oa.id in used_a:
                continue
            used_b.add(ob.id)
            used_a.add(oa.id)
            pairs.append((ob, oa, 3))
        rem_b = [o for o in rem_b if o.id not in used_b]
        rem_a = [o for o in rem_a if o.id not in used_a]
    return pairs, rem_b, rem_a


# --------------------------------------------------------------------------- the differ


class _Differ:
    def __init__(self, before: DesignCostTree, after: DesignCostTree, aligner: Aligner):
        self.b, self.a, self.al = before, after, aligner
        self.items: list[Item] = []
        self.pairs: list[Pair] = []
        self.structural: list[dict] = []
        self.sq_total = 0
        self.changed_keys_b: set[str] = set()
        self.changed_keys_a: set[str] = set()
        self.removed_shapes: dict[str, list[tuple[str, CostOp]]] = defaultdict(list)
        self.added_shapes: dict[str, list[tuple[str, CostOp]]] = defaultdict(list)
        self.cfg_b = (before.config or {}).get("macro_values") or (before.config or {}).get("defines") or {}
        self.cfg_a = (after.config or {}).get("macro_values") or (after.config or {}).get("defines") or {}

    # ---- module / instance matching
    def build_pairs(self):
        b, a = self.b, self.a
        nb = {k: n for k, n in b.instance_counts.items() if n > 0 and k in b.modules}
        na = {k: n for k, n in a.instance_counts.items() if n > 0 and k in a.modules}
        common = sorted(set(nb) & set(na))
        ub = set(nb) - set(common)
        ua = set(na) - set(common)
        pairs: dict[tuple[str | None, str | None], Pair] = {}
        work: list[Pair] = []

        def mk(kb, ka, status="changed"):
            p = Pair(b.modules[kb] if kb else None, a.modules[ka] if ka else None,
                     nb.get(kb, 0) if kb else 0, na.get(ka, 0) if ka else 0, status)
            pairs[(kb, ka)] = p
            return p

        for k in common:
            p = mk(k, k)
            if not self._skippable(p):
                work.append(p)
        # path-based pairing of parameter-set changes / renames, starting from changed parents
        i = 0
        pending = list(work)
        while i < len(pending):
            p = pending[i]
            i += 1
            eb = {e.path: e for e in p.mb.instances}
            ea = {e.path: e for e in p.ma.instances}
            for path in sorted(set(eb) & set(ea)):
                cb, ca = eb[path].child_key, ea[path].child_key
                if cb == ca or cb not in ub or ca not in ua:
                    continue
                mb_, ma_ = b.modules[cb], a.modules[ca]
                same_name = mb_.name == ma_.name
                same_hash = bool(mb_.canonical_hash) and mb_.canonical_hash == ma_.canonical_hash
                if same_name or same_hash:
                    ub.discard(cb)
                    ua.discard(ca)
                    q = mk(cb, ca, "changed" if same_name else "renamed")
                    pending.append(q)
                    work.append(q)
        # leftovers: unique name on each side
        by_nb: dict[str, list[str]] = defaultdict(list)
        by_na: dict[str, list[str]] = defaultdict(list)
        for k in sorted(ub):
            by_nb[b.modules[k].name].append(k)
        for k in sorted(ua):
            by_na[a.modules[k].name].append(k)
        for name in sorted(set(by_nb) & set(by_na)):
            if len(by_nb[name]) == 1 and len(by_na[name]) == 1:
                kb, ka = by_nb[name][0], by_na[name][0]
                ub.discard(kb)
                ua.discard(ka)
                work.append(mk(kb, ka))
        # renames by equal shape hash
        hb: dict[str, list[str]] = defaultdict(list)
        for k in sorted(ub):
            h = b.modules[k].canonical_hash
            if h:
                hb[h].append(k)
        for k in sorted(ua):
            h = a.modules[k].canonical_hash
            if h and hb.get(h):
                kb = hb[h].pop(0)
                ub.discard(kb)
                ua.discard(k)
                work.append(mk(kb, k, "renamed"))
        for k in sorted(ub):
            work.append(mk(k, None, "removed"))
        for k in sorted(ua):
            work.append(mk(None, k, "added"))
        self.pairs = work

    @staticmethod
    def _skippable(p: Pair) -> bool:
        h = p.mb.canonical_hash  # type: ignore[union-attr]
        return bool(h) and h == p.ma.canonical_hash and p.nb == p.na  # type: ignore[union-attr]

    # ---- per pair work
    def run(self):
        self.build_pairs()
        for p in self.pairs:
            self.process(p)
        self.moved_hints()

    def process(self, p: Pair):
        if p.mb is not None:
            self.changed_keys_b.add(p.mb.key)
        if p.ma is not None:
            self.changed_keys_a.add(p.ma.key)
        if p.mb is None:
            self.structural.append({"kind": "module_added", "module": p.name, "parameters": dict(p.params),
                                    "description": f"module {p.name}{_fmt_params(p.params)} added (x{p.na})"})
            for o in sorted(p.ma.ops, key=_skey):  # type: ignore[union-attr]
                self.add_op(p, o)
            return
        if p.ma is None:
            self.structural.append({"kind": "module_removed", "module": p.name, "parameters": dict(p.params),
                                    "description": f"module {p.name}{_fmt_params(p.params)} removed (x{p.nb})"})
            for o in sorted(p.mb.ops, key=_skey):
                self.remove_op(p, o)
            return
        if p.mb.name != p.ma.name:
            self.structural.append({"kind": "renamed", "module": p.ma.name, "from": p.mb.name, "to": p.ma.name,
                                    "description": f"module {p.mb.name} renamed to {p.ma.name}"})
        if p.mb.parameters != p.ma.parameters:
            self.structural.append({
                "kind": "parameter_change", "module": p.ma.name, "from": dict(p.mb.parameters),
                "to": dict(p.ma.parameters),
                "description": f"module {p.ma.name} parameters {_fmt_params(p.mb.parameters) or '{}'}"
                               f" -> {_fmt_params(p.ma.parameters) or '{}'}"})
        self.instance_changes(p)
        mult: dict[tuple[str, str], list] = {}  # (category, quality) -> [Ge3, sq]
        dn = p.na - p.nb

        def acc_mult(ob: CostOp, sq_extra: int):
            if dn and _nz(ob.cost):
                e = mult.setdefault((ob.category, ob.quality), [Z, 0, ob])
                e[0] = _add(e[0], _mulk(ob.cost, dn))
                e[1] += sq_extra

        if p.mb.canonical_hash and p.mb.canonical_hash == p.ma.canonical_hash:
            for ob in p.mb.ops:
                if dn and _nz(ob.cost):
                    s2 = (ob.sigma * p.nb) ** 2 + (ob.sigma * p.na) ** 2
                    self.sq_total += s2
                    acc_mult(ob, s2)
        else:
            pairs, rem_b, rem_a = match_ops(p.mb.ops, p.ma.ops, self.al)
            for ob, oa, tier in pairs:
                dint = _mulk(_sub(oa.cost, ob.cost), p.na)
                has_mult = bool(dn) and _nz(ob.cost)
                if not _nz(dint) and not has_mult:
                    continue
                s2 = (ob.sigma * p.nb) ** 2 + (oa.sigma * p.na) ** 2
                self.sq_total += s2
                if _nz(dint):
                    self.modified(p, ob, oa, dint, s2)
                    acc_mult(ob, 0)
                else:
                    acc_mult(ob, s2)
            for ob in rem_b:
                dint = _mulk(_neg(ob.cost), p.na)
                if _nz(ob.cost):
                    s2 = (ob.sigma * p.nb) ** 2
                    self.sq_total += s2
                    self.remove_op(p, ob, count=p.na, sq=s2, matched_module=True)
                    acc_mult(ob, 0)
            for oa in rem_a:
                if _nz(oa.cost):
                    self.add_op(p, oa, count=p.na)
        for (cat, q), (d, s2, ref) in sorted(mult.items()):
            if not _nz(d):
                continue
            p.mult = _add(p.mult, d)
            self.items.append(Item(
                kind="mul", pair=p, file=p.ma.file, line=p.ma.line, side="after", category=cat, quality=q,
                reason="multiplicity", description=f"{p.name} instances x{p.nb}->x{p.na}",
                d=d, instances=p.na, sq=s2, module=p.name))

    def instance_changes(self, p: Pair):
        eb = {e.path: e for e in p.mb.instances}
        ea = {e.path: e for e in p.ma.instances}
        lb = [e for path, e in sorted(eb.items()) if path not in ea]
        la = [e for path, e in sorted(ea.items()) if path not in eb]
        for path in sorted(set(eb) & set(ea)):
            x, y = eb[path], ea[path]
            if x.count != y.count:
                self.structural.append({
                    "kind": "instance_count", "module": p.name, "path": path, "from": x.count, "to": y.count,
                    "description": f"instance {p.name}.{path} count {x.count}->{y.count}"})
        keys_a: dict[str, list] = defaultdict(list)
        for e in la:
            keys_a[e.child_key].append(e)
        rest_b = []
        for e in lb:
            if keys_a.get(e.child_key):
                keys_a[e.child_key].pop(0)  # same child under another path: instance renamed
            else:
                rest_b.append(e)
        rest_a = [e for lst in keys_a.values() for e in lst]
        for e in rest_b:
            self.structural.append({"kind": "instance_removed", "module": p.name, "path": e.path, "child": e.child_key,
                                    "description": f"instance {p.name}.{e.path} removed ({self._cname(self.b, e.child_key)})"})
        for e in sorted(rest_a, key=lambda e: e.path):
            self.structural.append({"kind": "instance_added", "module": p.name, "path": e.path, "child": e.child_key,
                                    "description": f"instance {p.name}.{e.path} added ({self._cname(self.a, e.child_key)})"})

    @staticmethod
    def _cname(t: DesignCostTree, key: str) -> str:
        m = t.modules.get(key)
        return m.name if m else key

    # ---- items
    def add_op(self, p: Pair, o: CostOp, count: int | None = None):
        if not _nz(o.cost):
            return
        n = p.na if count is None else count
        reason = "blackbox" if o.op == "BLACKBOX" else "added"
        s2 = (o.sigma * n) ** 2
        self.sq_total += s2
        cause = self.define_cause(o, "added")
        self.items.append(Item(
            kind="int", pair=p, file=o.file, line=o.line, side="after", category=o.category, quality=o.quality,
            op=o.op, widths=o.effective_widths or o.declared_widths, reason=reason, description=_describe(reason, None, o, []),
            d=_mulk(o.cost, n), per=o.cost, instances=n, sq=s2, cause=cause, flags=self.flags_of(o), module=p.name))
        if o.op == "BLACKBOX":
            self.structural.append({"kind": "blackbox_added", "module": p.name, "description": f"black box {_base_desc(o)} added in {p.name}"})
        if o.shape_key:
            self.added_shapes[o.shape_key].append((p.name, o))

    def remove_op(self, p: Pair, o: CostOp, count: int | None = None, sq: int | None = None, matched_module: bool = False):
        if not _nz(o.cost):
            return
        n = p.nb if count is None else count
        s2 = (o.sigma * n) ** 2 if sq is None else sq
        if sq is None:
            self.sq_total += s2
        f, ln, side = self.removed_loc(o)
        reason = "blackbox" if o.op == "BLACKBOX" else "removed"
        cause = self.define_cause(o, "removed")
        self.items.append(Item(
            kind="int", pair=p, file=f, line=ln, side=side, category=o.category, quality=o.quality, op=o.op,
            widths=o.effective_widths or o.declared_widths, reason=reason, description=_describe(reason, o, None, []),
            d=_mulk(_neg(o.cost), n), per=_neg(o.cost), instances=n, sq=s2, cause=cause, flags=self.flags_of(o),
            module=p.name))
        if o.op == "BLACKBOX":
            self.structural.append({"kind": "blackbox_removed", "module": p.name, "description": f"black box {_base_desc(o)} removed from {p.name}"})
        if o.shape_key:
            self.removed_shapes[o.shape_key].append((p.name, o))

    def modified(self, p: Pair, ob: CostOp, oa: CostOp, dint: Ge3, s2: int):
        reason, fkind, names = classify(ob, oa)
        cause = self.trace(ob, oa, fkind, names)
        if cause is None:
            cause = self.define_cause_modified(ob, oa)
        self.items.append(Item(
            kind="int", pair=p, file=oa.file, line=oa.line, side="after", category=oa.category, quality=oa.quality,
            op=oa.op, widths=oa.effective_widths or oa.declared_widths, reason=reason,
            description=_describe(reason, ob, oa, names), d=dint, per=_sub(oa.cost, ob.cost), instances=p.na, sq=s2,
            cause=cause, flags=self.flags_of(oa) + self.flags_of(ob), module=p.name))
        if oa.op == "BLACKBOX":
            self.structural.append({"kind": "blackbox_changed", "module": p.name,
                                    "description": f"black box {_base_desc(oa)} cost changed in {p.name}"})

    @staticmethod
    def flags_of(o: CostOp) -> tuple[str, ...]:
        return tuple(t for t in o.tags if t in ("unknown_blackbox", "macro_no_table"))

    def removed_loc(self, o: CostOp) -> tuple[str, int, str]:
        if self.al.identity:
            return o.file, o.line, "before"
        m = self.al.map_line(o.file, o.line)
        if m is not None:
            return m[0], m[3], "after"
        s = self.al.nearest_surviving(o.file, o.line)
        if s is not None:
            return s[0], s[1], "after"
        return o.file, o.line, "before"

    # ---- cause tracing
    def trace(self, ob: CostOp, oa: CostOp, fkind: str | None, names: list[str]):
        if fkind is None:
            return None
        okey = {"param": "param_origins", "const": "const_origins", "width": "width_origins"}[fkind]
        vkey = {"param": "params", "const": "consts", "width": "widths"}[fkind]
        for n in names:
            org = (oa.facts.get(okey) or {}).get(n)
            if not org:
                continue
            f, ln = org
            if self.al.after_line_changed(f, ln):
                kind = "parameter" if fkind == "param" or (fkind == "width" and n in (oa.facts.get("params") or {})) else "constant"
                return {"kind": kind, "name": n, "file": f, "line": ln,
                        "from": str((ob.facts.get(vkey) or {}).get(n)), "to": str((oa.facts.get(vkey) or {}).get(n))}
        return None

    def define_cause(self, o: CostOp, side: str):
        defs = o.facts.get("defines")
        if not defs:
            return None
        origins = o.facts.get("define_origins") or {}
        for n in sorted(defs):
            org = origins.get(n)
            cfg_diff = self.cfg_b.get(n) != self.cfg_a.get(n)
            src_diff = False
            if org:
                src_diff = (self.al.before_line_changed(org[0], org[1]) if side == "removed"
                            else self.al.after_line_changed(org[0], org[1]))
            if cfg_diff or src_diff:
                if side == "added":
                    frm, to = self.cfg_b.get(n), defs[n]
                else:
                    frm, to = defs[n], self.cfg_a.get(n)
                return {"kind": "define", "name": n, "file": org[0] if org else None, "line": org[1] if org else None,
                        "from": "undefined" if frm is None else str(frm), "to": "undefined" if to is None else str(to)}
        return None

    def define_cause_modified(self, ob: CostOp, oa: CostOp):
        names = _changed_names(ob.facts.get("defines"), oa.facts.get("defines"))
        if not names:
            return None
        n = names[0]
        org = (oa.facts.get("define_origins") or {}).get(n)
        fb, fa = (ob.facts.get("defines") or {}).get(n), (oa.facts.get("defines") or {}).get(n)
        return {"kind": "define", "name": n, "file": org[0] if org else None, "line": org[1] if org else None,
                "from": "undefined" if fb is None else str(fb), "to": "undefined" if fa is None else str(fa)}

    # ---- moved hints
    def moved_hints(self):
        agg: dict[tuple[str, str], int] = defaultdict(int)
        for sk, rem in self.removed_shapes.items():
            add = self.added_shapes.get(sk)
            if not add:
                continue
            for (mb, _), (ma, _) in zip(rem, add):
                if mb != ma:
                    agg[(mb, ma)] += 1
        for (mb, ma), n in sorted(agg.items()):
            self.structural.append({"kind": "moved", "from": mb, "to": ma, "count": n,
                                    "description": f"{n} operation(s) moved from {mb} to {ma}"})


# --------------------------------------------------------------------------- contributors


def _leaf(it: Item) -> Contributor:
    return Contributor(
        file=it.file, line=it.line, reason=it.reason, description=it.description,
        delta=delta_range(it.d, isqrt(it.sq)), op=it.op, widths=tuple(it.widths), category=it.category,
        module=it.module, instances=it.instances, per_instance=it.per, location_side=it.side, cause=it.cause,
        scenario=it.d)


def _order(c: Contributor):
    return (-abs(c.scenario.typ), -abs(c.scenario.max - c.scenario.min), c.file, c.line, c.description, c.reason)


def _merge(items: list[Item], leafs: list[Contributor], *, file, line, reason, description, cause, side, module) -> Contributor:
    d = Z
    sq = 0
    for it in items:
        d = _add(d, it.d)
        sq += it.sq
    insts = {it.instances for it in items}
    cats = defaultdict(int)
    for it in items:
        cats[it.category] += abs(it.d.typ)
    cat = max(sorted(cats), key=lambda k: cats[k]) if cats else ""
    return Contributor(
        file=file, line=line, reason=reason, description=description, delta=delta_range(d, isqrt(sq)),
        op="", widths=(), category=cat, module=module, instances=insts.pop() if len(insts) == 1 else 1,
        per_instance=None, location_side=side, cause=cause, children=tuple(sorted(leafs, key=_order)), scenario=d)


def build_contributors(items: list[Item]) -> list[Contributor]:
    out: list[Contributor] = []
    by_cause: dict[tuple, list[Item]] = defaultdict(list)
    by_line: dict[tuple, list[Item]] = defaultdict(list)
    by_mult: dict[int, list[Item]] = defaultdict(list)
    for it in items:
        if it.kind == "mul":
            by_mult[id(it.pair)].append(it)
        elif it.cause is not None:
            c = it.cause
            if c["kind"] == "define":  # a macro is one cause wherever its condition sits
                by_cause[(c["kind"], c["name"], None, None, c["from"], c["to"])].append(it)
            else:
                by_cause[(c["kind"], c["name"], c["file"], c["line"], c["from"], c["to"])].append(it)
        else:
            by_line[(it.file, it.line, it.side, it.module, it.reason)].append(it)
    for key, its in by_cause.items():
        c = its[0].cause
        reasons = {i.reason for i in its}
        reason = reasons.pop() if len(reasons) == 1 else {"constant": "constant"}.get(c["kind"], "parameter")
        n = len(its)
        what = "op" if n == 1 else "ops"
        mods = {i.module for i in its}
        loc_file = c["file"] if c["file"] is not None else its[0].file
        loc_line = c["line"] if c["line"] is not None else its[0].line
        prefix = "define " if c["kind"] == "define" else ""
        out.append(_merge(its, [_leaf(i) for i in its], file=loc_file, line=loc_line, reason=reason,
                          description=(f"{prefix}{c['name']} {c['from']}->{c['to']} ({n} {what})" if c["from"] != c["to"]
                                       else f"{prefix}{c['name']} condition changed ({n} {what})"), cause=c,
                          side="after", module=mods.pop() if len(mods) == 1 else ""))
    for (f, ln, side, mod, reason), its in by_line.items():
        if len(its) == 1:
            out.append(_leaf(its[0]))
        else:
            descs = []
            for i in sorted(its, key=lambda i: i.description):
                if i.description not in descs:
                    descs.append(i.description)
            out.append(_merge(its, [_leaf(i) for i in its], file=f, line=ln, reason=reason,
                              description=" + ".join(descs), cause=None, side=side, module=mod))
    for its in by_mult.values():
        p = its[0].pair
        out.append(_merge(its, [], file=its[0].file, line=its[0].line, reason="multiplicity",
                          description=its[0].description, cause=None, side="after", module=p.name))
    out.sort(key=_order)
    return out


# --------------------------------------------------------------------------- diagnostics


def _all_diags(t: DesignCostTree) -> dict[tuple, Diagnostic]:
    d: dict[tuple, Diagnostic] = {}
    for x in t.diagnostics:
        d.setdefault((x.code, x.file, x.message), x)
    for k in sorted(t.modules):
        for x in t.modules[k].diagnostics:
            d.setdefault((x.code, x.file, x.message), x)
    return d


def _config_diffs(b: DesignCostTree, a: DesignCostTree) -> list[str]:
    out = []
    cb, ca = b.config or {}, a.config or {}
    for k in ("profile", "defines", "top"):
        if k in cb and k in ca and cb[k] != ca[k]:
            if k == "top" and (cb[k] is None or ca[k] is None):
                continue                # auto-detected on one side, pinned to the same module on the other
            out.append(k)
    return out


def not_comparable(reason: str, top: str | None = None, **kw) -> DiffResult:
    return DiffResult("NOT_COMPARABLE", Confidence("low", (reason,)), top=top, **kw)


def diff_trees(
    before: DesignCostTree | None,
    after: DesignCostTree | None,
    *,
    before_sources: Mapping[str, str] | None = None,
    after_sources: Mapping[str, str] | None = None,
    threshold_ge: float = 5,
    config_mismatch: bool | list[str] | tuple[str, ...] | None = None,
) -> DiffResult:
    if before is None or after is None:
        side = "baseline" if before is None else "current"
        return not_comparable(f"{side} could not be analyzed")
    top_a = after.modules[after.top_key].name if after.top_key in after.modules else None
    top_b = before.modules[before.top_key].name if before.top_key in before.modules else None
    if top_b is None or top_a is None:
        missing = "baseline" if top_b is None else "current"
        name = (after.top_key if top_b is None else before.top_key)
        return not_comparable(f"{missing} has no module named {name.split('{')[0].split('#')[0]} (top changed?)", top_a or top_b)

    cfg = _config_diffs(before, after) if config_mismatch is None else (
        list(config_mismatch) if isinstance(config_mismatch, (list, tuple)) else (["configuration"] if config_mismatch else []))
    thr = round(threshold_ge * 1000)
    d = _Differ(before, after, Aligner(before_sources, after_sources))
    d.run()

    tot_b, tot_a = before.total(), after.total()
    dtot = _sub(tot_a, tot_b)
    sigma = isqrt(d.sq_total)
    items = [i for i in d.items if _nz(i.d)]

    # diagnostics
    db_, da_ = _all_diags(before), _all_diags(after)
    new = tuple(replace(da_[k], side="after") for k in sorted(set(da_) - set(db_), key=str))
    resolved = tuple(replace(db_[k], side="before") for k in sorted(set(db_) - set(da_), key=str))
    structural = list(d.structural)
    for x in new:
        structural.append({"kind": "diagnostic_new", "code": x.code, "description": f"diagnostic {x.code} appeared: {x.message}"})
    for x in resolved:
        structural.append({"kind": "diagnostic_resolved", "code": x.code, "description": f"diagnostic {x.code} resolved: {x.message}"})
    diag_extra: list[Diagnostic] = []
    if cfg:
        diag_extra.append(Diagnostic("CONFIG_MISMATCH", f"baseline and current differ in: {', '.join(cfg)}", "warning", True, "both"))
    top_changed = top_a != top_b
    if top_changed:
        diag_extra.append(Diagnostic("TOP_CHANGED", f"top module changed from {top_b} to {top_a}", "warning", True, "both"))
    new = new + tuple(diag_extra)

    all_c = build_contributors(items)
    shown = [c for c in all_c if abs(c.scenario.typ) >= thr]
    folded = [c for c in all_c if abs(c.scenario.typ) < thr]
    others_net = Z
    for c in folded:
        others_net = _add(others_net, c.scenario)

    # views
    cat: dict[str, Ge3] = {}
    for i in items:
        cat[i.category] = _add(cat.get(i.category, Z), i.d)
    cat = {k: v for k, v in sorted(cat.items()) if _nz(v)}
    lines: dict[tuple, list] = {}
    for i in items:
        e = lines.setdefault((i.file, i.line, i.side), [Z, 0])
        e[0] = _add(e[0], i.d)
        e[1] += i.sq
    by_line = tuple(
        {"file": f, "line": ln, "location_side": s, "scenario": _gd(v), "delta": _gd(delta_range(v, isqrt(s2)))}
        for (f, ln, s), (v, s2) in sorted(lines.items()))
    by_cause = tuple(
        {"cause": c.cause, "file": c.file, "line": c.line, "reason": c.reason, "description": c.description,
         "scenario": _gd(c.scenario), "delta": _gd(c.delta)} for c in all_c)
    pm: dict[int, list] = {id(p): [Z, Z, 0] for p in d.pairs}
    for i in items:
        e = pm[id(i.pair)]
        if i.kind == "mul":
            e[1] = _add(e[1], i.d)
        else:
            e[0] = _add(e[0], i.d)
        e[2] += i.sq
    by_module = []
    verbose = []
    for p in d.pairs:
        intr, mult, s2 = pm[id(p)]
        if not _nz(intr) and not _nz(mult) and p.status == "changed":
            continue
        entry = {
            "module": p.name, "parameters": dict(p.params), "status": p.status,
            "instances_before": p.nb, "instances_after": p.na,
            "intrinsic_ge": _ged(intr), "multiplicity_ge": _ged(mult),
            "intrinsic": _gd(intr), "multiplicity": _gd(mult),
        }
        if p.mb is not None and p.ma is not None and p.mb.name != p.ma.name:
            entry["renamed_from"] = p.mb.name
        by_module.append((abs(intr.typ + mult.typ), p.name, entry))
        verbose.append((abs(intr.typ + mult.typ), p.name,
                        f"MODULE {p.name}{_fmt_params(p.params)} x{p.nb}->x{p.na} intrinsic={_sge(intr.typ)} multiplicity={_sge(mult.typ)}"))
    by_module.sort(key=lambda t: (-t[0], t[1]))
    verbose.sort(key=lambda t: (-t[0], t[1]))

    # confidence and verdict
    rng_d = delta_range(dtot, sigma)
    bad = {("b", k) for k in d.changed_keys_b & before.unsupported_in} | {("a", k) for k in d.changed_keys_a & after.unsupported_in}
    for tag, tree, ks in (("b", before, d.changed_keys_b), ("a", after, d.changed_keys_a)):
        for k in ks:
            if any(x.severity == "error" or x.code == "UNSUPPORTED_CONSTRUCT" for x in tree.modules[k].diagnostics):
                bad.add((tag, k))
    unsupported = len(bad)
    contributions = [(i.category, i.quality, abs(i.d.typ)) for i in items]
    flags = {f for i in items for f in i.flags}
    if not items:
        conf = Confidence("high", (), 0.0)
        verdict = "NO_CHANGE"
    else:
        conf = _confidence(rng_d, contributions, flags, unsupported)
        t = abs(dtot.typ)
        incl0 = rng_d.min <= 0 <= rng_d.max
        if t < thr:
            verdict = "NEGLIGIBLE"
        elif incl0:
            verdict = "UNCERTAIN"
        else:
            verdict = "INCREASE" if dtot.typ > 0 else "DECREASE"
    reasons = list(conf.reasons)
    if cfg:
        reasons.append(f"configuration mismatch ({', '.join(cfg)} differ)")
    if top_changed:
        reasons.append(f"top module changed ({top_b} -> {top_a})")
    if cfg or top_changed:
        conf = Confidence("low", tuple(reasons), conf.range_ratio)
    if "profile" in cfg:
        verdict = "NOT_COMPARABLE"
    assumed = list(after.assumptions)
    for x in before.assumptions:
        if x not in assumed:
            assumed.append(x)
    pct = None if tot_b.typ == 0 else dtot.typ / tot_b.typ * 100.0
    return DiffResult(
        verdict=verdict, confidence=conf, top=top_a, before=tot_b, after=tot_a, delta=rng_d, percent=pct,
        contributors=tuple(shown), others_count=len(folded), others_net=others_net,
        by_module=tuple(e for _, _, e in by_module), by_category={k: v.typ for k, v in cat.items()},
        structural=tuple(structural), diagnostics_new=new, diagnostics_resolved=resolved, assumed=tuple(assumed),
        modules_verbose=tuple(s for _, _, s in verbose), delta_scenarios=dtot, all_contributors=tuple(all_c),
        by_line=by_line, by_cause=by_cause, category_scenarios=cat)
