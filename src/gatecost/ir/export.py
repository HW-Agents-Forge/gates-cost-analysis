"""Plain-data export of an analyzed Module IR (for a cost-tree builder).

Every value is a plain Python object (dict / list / tuple / int / str / bool / None), so
the cost module needs no knowledge of IR classes.
"""
from __future__ import annotations

from .core import ModuleIR, Node
from .datapath import opinfo
from .dump import _canonical_order
from .keys import compute_keys
from .sigspec import Const


_SCALARS = frozenset({int, str, bool, type(None), float})


def _plain(v):
    c = v.__class__
    if c in _SCALARS:
        return v
    if c is tuple or c is list:
        return [x if x.__class__ in _SCALARS else _plain(x) for x in v]
    if c is dict:
        return {str(k): (x if x.__class__ in _SCALARS else _plain(x)) for k, x in v.items()}
    if c is Const:
        return {"value": v.value, "width": v.width, "xmask": v.xmask}
    if c is bytes:
        return v.hex()
    # subclasses of the plain types
    if isinstance(v, Const):
        return {"value": v.value, "width": v.width, "xmask": v.xmask}
    if isinstance(v, (tuple, list)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, bytes):
        return v.hex()
    return v


def export_origin(o) -> dict | None:
    if o is None:
        return None
    d = {"file": o.file, "line": o.line, "column": o.column}
    if o.def_file is not None:
        d["def_file"], d["def_line"], d["def_column"] = o.def_file, o.def_line, o.def_column
    return d


def export_node(nd: Node, members: bool = True) -> dict:
    eff = dict(nd.eff or {})
    ops = eff.pop("ops", {})
    d = {
        "id": nd.id,
        "op": nd.op,
        "attrs": _plain({k: v for k, v in nd.attrs.items()}),
        "origin": export_origin(nd.origin),
        "tags": _plain(dict(nd.tags)),
        "named_key": list(nd.named_key) if nd.named_key else None,
        "shape_key": nd.shape.hex() if nd.shape else None,
        "outputs": [{"width": o.width, "name": o.name, "known_mask": o.kmask, "known_val": o.kval,
                     "demand": o.demand, "fanout": o.fanout} for o in nd.outputs],
        "inputs": [],
        "eff": _plain(eff),
        "merge_count": nd.attrs.get("merge_count", 1),
        "group": nd.tags.get("group"),
        "shared": nd.tags.get("shared"),
        "share_set": nd.tags.get("share_set"),
        "fused_into": nd.tags.get("fused"),
        "const_folded": bool(nd.tags.get("const_folded")),
    }
    for name, sig in nd.inputs:
        info = ops.get(name) or opinfo(sig)
        d["inputs"].append({"name": name, **_plain({
            "width": sig.width, "eff": info["eff"], "sign_rep": info["sign_rep"], "tz": info["tz"],
            "const": info["const"], "known_mask": info["kmask"], "known_val": info["kval"],
            "is_const": info["const"] is not None})})
    if nd.op in ("INST", "BLACKBOX"):
        d["instance"] = {"name": nd.attrs.get("inst"), "module_key": nd.attrs.get("module_key"),
                         "blackbox": nd.attrs.get("name") if nd.op == "BLACKBOX" else None, "count": nd.attrs.get("count", 1),
                         "recursive": bool(nd.tags.get("recursive"))}
    if members and nd.members:
        d["members"] = [export_node(x, members=False) for x in nd.members]
    return d


def export_module(m: ModuleIR) -> dict:
    """Plain-data view of an analyzed module: ports, costed nodes (deterministic order),
    sharing candidate sets, diagnostics and the canonical hash."""
    if m.canonical_hash is None:
        compute_keys(m)
    nodes = [it for it in _canonical_order(m) if isinstance(it, Node)]
    return {
        "name": m.name,
        "key": m.key,
        "params": _plain(list(m.params)),
        "const_ports": _plain(list(m.const_ports)),
        "canonical_hash": m.canonical_hash.hex() if m.canonical_hash else None,
        "is_blackbox": m.is_blackbox,
        "limit_reached": m.limit_reached,
        "ports": [{"name": p.name, "dir": p.port, "width": p.width, "signed": p.signed,
                   "known_mask": p.kmask, "known_val": p.kval} for p in m.ports],
        "nodes": [export_node(n) for n in nodes],
        "share_sets": _plain(m.share_sets),
        "diagnostics": [{"code": d.code, "message": d.message, "severity": d.severity,
                         "affects_cost": d.affects_cost, "origin": export_origin(d.origin)}
                        for d in m.diagnostics],
    }
