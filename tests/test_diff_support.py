"""Hand-built cost trees and invariant checkers shared by the differ tests (no tests here)."""
from __future__ import annotations

from hashlib import blake2b

from gatecost.cost.tree import CostOp, DesignCostTree, InstanceEdge, ModuleCost
from gatecost.result import Diagnostic, Ge3


def ge3(typ: int, lo: float = 0.8, hi: float = 1.25) -> Ge3:
    return Ge3(round(typ * lo), typ, round(typ * hi))


def op(kind="ADD", typ=1000, *, tgt=None, role="r", shape=None, cat=None, line=1, file="a.sv", widths=(8, 8),
       facts=None, tags=(), quality="good", sigma=None, cost=None, arch=None, members=(), breakdown=None,
       ins="", id=None, column=0, def_file=None, def_line=None, desc=""):
    """A CostOp under construction; ``mod()`` fills id and named key."""
    cat = cat or {"ADD": "addcmp", "MUL": "mul", "MUX": "select", "DFF": "seq", "AND": "logic", "DIV": "div",
                  "SHL": "shift", "MEM": "mem", "BLACKBOX": "blackbox", "PMUX": "select"}.get(kind, "logic")
    c = cost if cost is not None else ge3(typ * 1000)
    tgt = tgt or f"t{line}"
    return dict(id=id, op=kind, category=cat, quality=quality, cost=c,
                sigma=round(c.typ * 0.15) if sigma is None else sigma, file=file, line=line, column=column,
                def_file=def_file, def_line=def_line, tgt=tgt, role=role,
                shape_key=shape if shape is not None else f"{kind}|{tgt}|{widths}|{ins}",
                declared_widths=tuple(widths), effective_widths=tuple(widths), tags=tuple(tags),
                breakdown=breakdown or {}, arch=arch or {}, facts=facts or {}, description=desc, members=tuple(members))


def mod(name, ops=(), *, params=(), instances=(), file="a.sv", line=1, key=None, hash=None, diagnostics=()):
    key = key or (name + ("{" + ",".join(f"{k}={v}" for k, v in params) + "}" if params else ""))
    cops = []
    for i, o in enumerate(ops):
        d = dict(o)
        tgt, role = d.pop("tgt"), d.pop("role")
        if d["id"] is None:
            d["id"] = i
        d["named_key"] = (name, tgt, role, d["op"])
        cops.append(CostOp(**d))
    inst = tuple(InstanceEdge(*e) if not isinstance(e, InstanceEdge) else e for e in instances)
    if hash is None:
        h = blake2b(digest_size=16)
        for s in sorted(c.shape_key + "|" + str(c.cost.typ) for c in cops):
            h.update(s.encode())
        h.update(repr(params).encode())
        for e in sorted((e.child_key, e.count) for e in inst):
            h.update(repr(e).encode())
        hash = h.hexdigest()
    return ModuleCost(key=key, name=name, parameters=tuple(params), canonical_hash=hash, ops=tuple(cops),
                      instances=inst, diagnostics=tuple(diagnostics), file=file, line=line)


def tree(mods, top=None, *, config=None, diagnostics=(), unsupported_in=frozenset(), assumptions=()):
    by = {m.key: m for m in mods}
    top = top or mods[0].key
    counts: dict[str, int] = {}

    def walk(k, n):
        counts[k] = counts.get(k, 0) + n
        for e in by[k].instances:
            walk(e.child_key, n * e.count)

    walk(top, 1)
    return DesignCostTree(top_key=top, modules=by, instance_counts=counts, diagnostics=tuple(diagnostics),
                          assumptions=tuple(assumptions), config=config or {}, unsupported_in=unsupported_in)


def flat(ops, name="top", *, config=None, diagnostics=(), unsupported_in=frozenset(), assumptions=(), **kw):
    return tree([mod(name, ops, **kw)], config=config, diagnostics=diagnostics, unsupported_in=unsupported_in,
                assumptions=assumptions)


def sc(c):
    return (c.min, c.typ, c.max)


def add3(items):
    return (sum(i[0] for i in items), sum(i[1] for i in items), sum(i[2] for i in items))


def check_additive(tc, r, before, after):
    """Every view sums exactly to the total delta in every scenario."""
    tot = (after.total().min - before.total().min, after.total().typ - before.total().typ,
           after.total().max - before.total().max)
    tc.assertEqual(sc(r.delta_scenarios), tot)
    tc.assertEqual(r.delta.typ, tot[1])
    tc.assertEqual(add3([sc(c.scenario) for c in r.all_contributors]), tot)
    folded = tuple(sc(r.others_net))
    shown = add3([sc(c.scenario) for c in r.contributors])
    tc.assertEqual(tuple(a + b for a, b in zip(shown, folded)), tot)
    tc.assertEqual(add3([(v["scenario"]["min"], v["scenario"]["typ"], v["scenario"]["max"]) for v in r.by_line]), tot)
    tc.assertEqual(add3([(v["scenario"]["min"], v["scenario"]["typ"], v["scenario"]["max"]) for v in r.by_cause]), tot)
    tc.assertEqual(add3([sc(v) for v in r.category_scenarios.values()]), tot)
    tc.assertEqual(sum(r.by_category.values()), tot[1])
    mm = add3([(m["intrinsic"]["min"] + m["multiplicity"]["min"], m["intrinsic"]["typ"] + m["multiplicity"]["typ"],
                m["intrinsic"]["max"] + m["multiplicity"]["max"]) for m in r.by_module])
    tc.assertEqual(mm, tot)
    for c in r.all_contributors:
        if c.children:
            tc.assertEqual(add3([sc(x.scenario) for x in c.children]), sc(c.scenario) if c.reason != "multiplicity" else sc(c.scenario))
