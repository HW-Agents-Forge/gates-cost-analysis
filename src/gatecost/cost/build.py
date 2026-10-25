"""Cost-tree builder: analyzed IR design -> ``DesignCostTree`` (SPEC.md 5.5, COST_MODEL.md 4).

``build_cost_tree(design, profile, *, config=None, extra_diagnostics=())`` maps every costed
node of every reachable Module IR to a :class:`CostOp` through :func:`cost_op`, using the
effective widths and constant facts computed by ``analyze_ir``.  Savings (sharing, fusion,
folding) sit on the surviving operation and are zero on the others, so the design total is
exactly ``sum(op cost * instance count)`` in every scenario.

``tree_confidence(tree, profile)`` derives the confidence of a whole tree.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Any

from .. import _gc
from ..result import Confidence, Diagnostic, Ge3
from .acc import OpCost, zero
from .delta import confidence
from .ops import cost_op
from .profile import Profile
from .sharing import shared_leaf_cost
from .tree import CostOp, DesignCostTree, InstanceEdge, ModuleCost
from .units import pm

CATEGORIES = ("seq", "addcmp", "mul", "select", "logic", "shift", "div", "mem")
_UNSUPPORTED_CODES = {"UNSUPPORTED_CONSTRUCT", "PARSE_ERROR", "LIMIT_REACHED", "MISSING_MODULE",
                      "UNRESOLVED_PARAM", "UNRESOLVED_OVERLOAD", "RECURSIVE_INSTANCE", "INTERNAL"}


class _Item:
    """A costed node before it becomes a CostOp."""

    __slots__ = ("node", "cost", "desc", "eff_w", "decl_w", "facts", "tags", "members", "ids")

    def __init__(self, node, cost, desc, eff_w=(), decl_w=(), facts=None, tags=(), members=()):
        self.node = node
        self.cost = cost
        self.desc = desc
        self.eff_w = tuple(eff_w)
        self.decl_w = tuple(decl_w)
        self.facts = facts or {}
        self.tags = list(tags)
        self.members = tuple(members)


def _rel(file: str, root: str | None) -> str:
    if not file or root is None:
        return file or ""
    f = file.replace("\\", "/")
    r = root.replace("\\", "/").rstrip("/")
    if f.startswith(r + "/"):
        return f[len(r) + 1:]
    return f


def _fmt_param(v) -> str:
    if isinstance(v, dict) and "value" in v and "width" in v:
        return f"{v['width']}'h{v['value']:x}"
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def _block_name(key: str) -> str:
    for i, ch in enumerate(key):
        if ch in "([#{":
            return key[:i]
    return key


def _inp(node: dict, name: str) -> dict | None:
    for i in node["inputs"]:
        if i["name"] == name:
            return i
    return None


# ---------------------------------------------------------------------------------------
# per-node costing

def _cost_node(nd: dict, p: Profile) -> tuple[OpCost, str, tuple, dict, list]:
    """(cost, description, effective widths, facts, extra tags) of one exported node."""
    op = nd["op"]
    e = nd["eff"]
    w = e.get("w", 0)
    A, B = _inp(nd, "A"), _inp(nd, "B")
    facts: dict = {}
    tags: list = []
    outw = nd["outputs"][0]["width"] if nd["outputs"] else 0

    def const_of(i):
        return None if i is None or not i.get("is_const") else i["const"]

    if op in ("ADD", "SUB"):
        ca, cb = const_of(A), const_of(B)
        lo = e.get("lo", 0)
        n = w
        if n <= 0:
            return zero("addcmp"), f"{op} w0", (0,), facts, tags
        if cb is not None or (ca is not None and op == "ADD"):
            k = cb if cb is not None else ca
            facts["const"] = k
            oc = cost_op(op, {"n": n, "const": k}, p)
            return oc, f"{op} w{n} const", (n,), facts, tags
        n = max(n - lo, 0)
        if n == 0:
            return zero("addcmp"), f"{op} w0", (0,), facts, tags
        at = {"n": n}
        narrow = min(e.get("wa", 0), e.get("wb", 0)) - lo
        if 0 < narrow < n - 1:
            at["m"] = narrow
            facts["narrow_operand"] = narrow
        return cost_op(op, at, p), f"{op} w{n}", (n,), facts, tags
    if op == "NEG":
        if w <= 0:
            return zero("addcmp"), "NEG w0", (0,), facts, tags
        return cost_op("NEG", {"n": w}, p), f"NEG w{w}", (w,), facts, tags
    if op == "MUL":
        wa, wb, o = e.get("wa", 0), e.get("wb", 0), e.get("out", 0)
        if not wa or not wb or o <= 0:
            return zero("mul"), "MUL 0x0", (0, 0), facts, tags
        ca, cb = const_of(A), const_of(B)
        if ca is not None or cb is not None:
            k = ca if ca is not None else cb
            n = wb if ca is not None else wa
            facts["const"] = k
            return cost_op("MUL", {"n": n, "const": k, "out": o}, p), f"MUL w{n} const", (n,), facts, tags
        n, m = max(wa, wb), min(wa, wb)
        at = {"n": n, "m": m, "signed": bool(e.get("signed"))}
        if e.get("truncated"):
            at["trunc"] = o
            facts["trunc"] = o
        return cost_op("MUL", at, p), f"MUL {n}x{m}", (n, m), facts, tags
    if op in ("DIV", "MOD"):
        wa, wb = e.get("wa", 0), e.get("wb", 0)
        if nd["tags"].get("divmod_with") is not None:
            tags.append("divmod_shared")
            return zero("div", "poor"), f"{op} {wa}/{wb}", (wa, wb), facts, tags
        if not wa:
            return zero("div", "poor"), f"{op} 0/0", (0, 0), facts, tags
        sg = bool(nd["attrs"].get("sa") and nd["attrs"].get("sb"))
        cb = const_of(B)
        if cb is not None and cb > 0:
            facts["const"] = cb
            return cost_op(op, {"n": wa, "const": cb, "signed": sg}, p), f"{op} w{wa} const", (wa,), facts, tags
        return cost_op(op, {"n": wa, "m": max(wb, 1), "signed": sg}, p), f"{op} {wa}/{max(wb, 1)}", (wa, wb), facts, tags
    if op == "POW":
        wa = e.get("wa", 0)
        if not wa or w <= 0:
            return zero("mul"), "POW w0", (0,), facts, tags
        return (cost_op("POW", {"n": wa, "exp": e["exp"], "out": w}, p), f"POW w{wa} exp={e['exp']}",
                (wa,), facts, tags)
    if op in ("SHL", "SHR", "SAR", "ROL", "ROR"):
        s = e.get("ws", 0)
        n = max(w, 1)
        if w <= 0 or s == 0:
            return zero("shift", "medium"), f"{op} w{n}", (n,), facts, tags
        lhs_one = bool(op == "SHL" and A is not None and A.get("is_const") and A["const"] == 1)
        if lhs_one:
            tags.append("lhs_one")
        return (cost_op(op, {"n": n, "s": s, "lhs_one": lhs_one}, p), f"{op} w{n} by {s}", (n, s), facts, tags)
    if op in ("AND", "OR", "XOR", "XNOR", "NOT"):
        if w <= 0:
            return zero("logic"), f"{op} w0", (0,), facts, tags
        return cost_op(op, {"n": w}, p), f"{op} w{w}", (w,), facts, tags
    if op in ("RED_AND", "RED_OR", "RED_XOR"):
        if w <= 1:
            return zero("logic"), f"{op} w{max(w, 0)}", (max(w, 0),), facts, tags
        return cost_op(op, {"n": w}, p), f"{op} w{w}", (w,), facts, tags
    if op in ("EQ", "NE", "LT", "LE", "GT", "GE"):
        n = e.get("n", 0)
        if n <= 0:
            return zero("addcmp"), f"{op} w0", (0,), facts, tags
        at = {"n": n, "signed": bool(e.get("signed"))}
        if e.get("const_operand"):
            at["const_unknown"] = True
            facts["const_operand"] = True
        d = f"{op} w{n}" + (" const" if e.get("const_operand") else "")
        return cost_op(op, at, p), d, (n,), facts, tags
    if op == "MUX":
        if e.get("select_known") or w <= 0:
            return zero("select"), "MUX 2:1 w0", (0,), facts, tags
        nv, nca, ncb, ncc = (e.get("n_var", 0), e.get("n_const_a", 0), e.get("n_const_b", 0),
                             e.get("n_const_both", 0))
        parts = []
        if nv:
            parts.append(cost_op("MUX", {"k": 2, "w": nv}, p))
        if e.get("n_xor"):
            parts.append(cost_op("MUX", {"k": 2, "w": e["n_xor"], "complement": True}, p))
        if nca + ncb + e.get("n_andor", 0):
            parts.append(cost_op("MUX", {"k": 2, "w": nca + ncb + e.get("n_andor", 0), "const_inputs": 1}, p))
        if ncc:
            parts.append(cost_op("MUX", {"k": 2, "w": ncc, "const_inputs": 2}, p))
        oc = parts[0] if parts else zero("select")
        for x in parts[1:]:
            oc = oc + x
        if e.get("tristate"):
            tags.append("tristate")
        return oc, f"MUX 2:1 w{w}", (2, w), facts, tags
    if op in ("PMUX", "PRIORITY"):
        k = e.get("k_eff", e.get("k", 1))
        if w <= 0 or k <= 0:
            return zero("select", "medium"), f"{op} 0 w0", (0, 0), facts, tags
        facts["struct"] = {"k": k}
        return cost_op(op, {"k": k, "w": w}, p), f"{op} {k} w{w}", (w,), facts, tags
    if op == "DECODE":
        s = e.get("n", 0)
        if s <= 0 or w <= 0:
            return zero("select", "medium"), "DECODE 0", (0,), facts, tags
        return cost_op("DECODE", {"s": s}, p), f"DECODE {s}", (s,), facts, tags
    if op == "ENCODE":
        k = A["width"] if A else 1
        if w <= 0:
            return zero("logic", "medium"), f"ENCODE {k}", (k,), facts, tags
        pr = nd["attrs"].get("kind", "priority") == "priority"
        return cost_op("ENCODE", {"k": k, "priority": pr}, p), f"ENCODE {k}", (k,), facts, tags
    if op == "ROM":
        depth = nd["attrs"].get("depth", 0)
        width = outw
        if w <= 0 or depth < 1:
            return zero("mem", "poor"), f"ROM {depth}x{width}", (depth, width), facts, tags
        return (cost_op("ROM", {"depth": depth, "width": width, "contents": nd["attrs"].get("contents")}, p),
                f"ROM {depth}x{width}", (depth, width), facts, tags)
    if op == "DFF":
        n = outw
        a = nd["attrs"]
        kind = a.get("reset_kind", "none")
        en = _inp(nd, "EN") is not None
        at = {"n": n, "async_reset": kind in ("async", "both"), "sync_reset": kind in ("sync", "both"),
              "enable": en}
        facts.update(reset=kind, enable=en, enable_recognized=bool(a.get("enable_recognized")))
        facts["reg"] = {"reset": kind, "enable": en}
        oc = cost_op("DFF", at, p)
        mc = nd.get("merge_count", 1)
        if mc > 1:
            tags.append("merged")
            facts["merge_count"] = mc
            oc = OpCost(oc.min, oc.typ, oc.max * mc, oc.arch,
                        oc.breakdown, oc.category, oc.quality, oc.flags)
        return oc, f"DFF x{n} (register stage)", (n,), facts, tags
    if op == "LATCH":
        return cost_op("LATCH", {"n": outw}, p), f"LATCH x{outw}", (outw,), facts, tags
    if op == "MEM":
        a = nd["attrs"]
        d, wd = a["depth"], a["width"]
        sync = a.get("read_style") == "sync"
        facts.update(rports=a.get("rports", 1), wports=a.get("wports", 1), read_style=a.get("read_style"))
        if a.get("rom") and not a.get("wports"):
            # never written, filled by an initial block / $readmem: a ROM per read port (+ the read register)
            R = max(a.get("rports", 1), 1)
            oc = cost_op("ROM", {"depth": d, "width": wd, "contents": a.get("init")}, p).scaled(R)
            if sync:
                oc = oc + cost_op("DFF", {"n": wd * R}, p)
            return oc, f"ROM {d}x{wd}", (d, wd), facts, tags
        oc = cost_op("MEM", {"depth": d, "width": wd, "rports": max(a.get("rports", 1), 1),
                             "wports": max(a.get("wports", 1), 1), "sync_read": sync,
                             "const_reads": sum(1 for i in nd["inputs"]
                                                if i["name"].endswith("_addr") and i["name"].startswith("r")
                                                and i.get("is_const"))}, p)
        return oc, f"MEM {d}x{wd}", (d, wd), facts, tags
    if op == "SUM":
        return _cost_sum(nd, p)
    # every op of ir.core.ALL_OPS is handled above (INST and BLACKBOX by _cost_module)
    raise ValueError(f"cannot cost node op {op!r}: not an IR operation known to the cost-tree builder")


def _cost_sum(nd: dict, p: Profile):
    e = nd["eff"]
    w = e.get("w", 0)
    k = e.get("k", 0)
    mul = e.get("mul")
    facts: dict = {"terms": list(e.get("terms", ())), "signs": list(e.get("signs", ()))}
    tags = ["fused"]
    facts["struct"] = {"k": k}
    if w <= 0 or k < 2:
        return zero("addcmp"), f"SUM {k} w{w}", (w,), facts, tags
    nT = k - (1 if mul else 0)
    terms = [int(t) for t in e.get("terms", ())]
    if not mul:
        if len(terms) == k and all(t > 0 for t in terms):
            return cost_op("SUM", {"terms": terms, "w": w}, p), f"SUM {k} w{w}", (w,), facts, tags
        return cost_op("SUM", {"k": k, "w": w}, p), f"SUM {k} w{w}", (w,), facts, tags
    wa, wb = mul["wa"], mul["wb"]
    if not wa or not wb:
        if nT >= 2:
            return cost_op("SUM", {"k": nT, "w": w}, p), f"SUM {nT} w{w}", (w,), facts, tags
        return zero("addcmp"), f"SUM {nT} w{w}", (w,), facts, tags
    if mul.get("const_a") is not None or mul.get("const_b") is not None:
        kconst = mul["const_a"] if mul.get("const_a") is not None else mul["const_b"]
        n = wb if mul.get("const_a") is not None else wa
        m = cost_op("MUL", {"n": n, "const": kconst, "out": w}, p)
        s = cost_op("SUM", {"k": nT + 1, "w": w}, p)
        facts["const"] = kconst
        facts["struct"] = {"k": k, "mul": "const"}
        return m + s, f"MAC w{n} const +{nT}", (w,), facts, tags
    n, mm = max(wa, wb), min(wa, wb)
    oc = cost_op("MAC", {"n": n, "m": mm, "c": w, "signed": bool(mul.get("signed"))}, p)
    if nT > 1:
        fa = p.P["FA"] * w * (nT - 1)
        add_max = cost_op("ADD", {"n": w}, p).max * (nT - 1)
        oc = OpCost(oc.min + fa, oc.typ + fa, max(oc.max + add_max, oc.typ + fa), oc.arch, oc.breakdown,
                    oc.category, oc.quality, oc.flags)
    facts["struct"] = {"k": k, "mul": [n, mm]}
    return oc, f"MAC {n}x{mm} +{nT}", (w,), facts, tags


# ---------------------------------------------------------------------------------------
# sharing

def _apply_sharing(items: dict[int, _Item], share_sets: list[dict], p: Profile) -> None:
    for st in share_sets:
        leaves = [items[i] for i in st["nodes"] if i in items and items[i].cost.typ > 0]
        if len(leaves) < 2:
            continue
        costs = [it.cost for it in leaves]
        wa = max((it.node["eff"].get("wa", 0) for it in leaves), default=0)
        wb = max((it.node["eff"].get("wb", 0) for it in leaves), default=0)
        wo = max((it.node["eff"].get("w", 0) for it in leaves), default=0)
        sh = shared_leaf_cost(costs, wa, wb, wo, p)
        un = (sum(c.min for c in costs), sum(c.typ for c in costs), sum(c.max for c in costs))
        survivor = max(leaves, key=lambda it: (it.cost.typ, -it.node["id"]))
        for it in leaves:
            it.tags.append("shared")
        if (sh.min, sh.typ, sh.max) == un:
            continue
        survivor.cost = OpCost(sh.min, sh.typ, sh.max, survivor.cost.arch, survivor.cost.breakdown,
                               survivor.cost.category, survivor.cost.quality, survivor.cost.flags)
        survivor.members = tuple(sorted(it.node["id"] for it in leaves if it is not survivor))
        survivor.facts["shared_with"] = list(survivor.members)
        for it in leaves:
            if it is not survivor:
                c = it.cost
                it.cost = OpCost(0, 0, 0, c.arch, {}, c.category, c.quality, c.flags)
                it.tags.append("shared_zeroed")


# ---------------------------------------------------------------------------------------

def _diag(d: dict | object, root, side="after") -> Diagnostic:
    if isinstance(d, Diagnostic):
        return d
    if isinstance(d, dict):
        o = d.get("origin")
        return Diagnostic(d["code"], d["message"], d.get("severity", "warning"), bool(d.get("affects_cost")),
                          side, _rel(o["file"], root) if o else None, o["line"] if o else None,
                          o.get("column") if o else None)
    return d.as_result_diagnostic(side)


def _node_failure(nd: dict[str, Any], exc: BaseException, root: str | None) -> Diagnostic:
    """``INTERNAL`` diagnostic (error, affects cost) for a node whose cost could not be computed."""
    o = nd.get("origin") or {}
    reason = str(exc) or type(exc).__name__
    if isinstance(exc, KeyError):
        reason = f"missing attribute {reason}"
    return Diagnostic("INTERNAL", f"cannot cost {nd.get('op')} node {nd.get('id')}: {reason} (zero assumed)",
                      "error", True, "after", _rel(o.get("file", ""), root) or None, o.get("line"),
                      o.get("column"))


def _cost_module(key: str, mod: dict, ir, profile: Profile, root, fusion: bool):
    items: dict[int, _Item] = {}
    diags: list[Diagnostic] = [_diag(d, root) for d in mod["diagnostics"]]
    unsupported = any(d.affects_cost for d in diags)
    if mod["is_blackbox"] or mod["limit_reached"]:
        unsupported = True
    nodes = []
    for nd in mod["nodes"]:
        if nd["op"] == "SUM" and not fusion and nd.get("members"):
            nodes.extend(nd["members"])
        else:
            nodes.append(nd)
    for nd in nodes:
        op = nd["op"]
        if op == "INST":
            continue
        if op == "BLACKBOX":
            name = nd["instance"]["blackbox"] or ""
            oc = cost_op("BLACKBOX", {"name": name}, profile)
            cnt = nd["instance"]["count"] or 1
            tags = ["blackbox"]
            if "unknown_blackbox" in oc.flags:
                o = nd["origin"] or {}
                diags.append(Diagnostic("MISSING_MODULE", f"black box {name} has no cost in the profile (zero assumed)",
                                        "warning", True, "after", _rel(o.get("file", ""), root) or None, o.get("line")))
                tags.append("unknown_blackbox")
                unsupported = True
            oc = OpCost(oc.min * cnt, oc.typ * cnt, oc.max * cnt, oc.arch, oc.breakdown, "blackbox",
                        oc.quality, oc.flags)
            d = f"BLACKBOX {name}" + (f" x{cnt}" if cnt > 1 else "")
            items[nd["id"]] = _Item(nd, oc, d, (), (), {"name": name, "count": cnt}, tags)
            continue
        try:
            oc, desc, effw, facts, tags = _cost_node(nd, profile)
        except (ValueError, KeyError, ArithmeticError) as exc:
            # A node the cost model cannot handle (unknown op, missing or invalid attribute) is an
            # invariant breach between the IR and the cost model; report it on that node, cost it
            # zero and keep the rest of the design instead of aborting the whole run.
            if os.environ.get("GATECOST_ELAB_DEBUG"):
                raise
            diags.append(_node_failure(nd, exc, root))
            unsupported = True
            continue
        decl = ((_inp(nd, "A") or {}).get("width", 0), (_inp(nd, "B") or {}).get("width", 0)) \
            if op in ("MUL", "DIV", "MOD") else ((nd["outputs"][0]["width"],) if nd["outputs"] else ())
        if nd["const_folded"]:
            tags.append("const_folded")
        if nd["origin"] and nd["origin"].get("def_file") is not None:
            tags.append("macro")
        for fl in oc.flags:
            tags.append(fl)
        items[nd["id"]] = _Item(nd, oc, desc, effw, decl, facts, tags)
        if nd["op"] == "SUM":
            items[nd["id"]].members = tuple(m["id"] for m in nd.get("members", ()))
    _apply_sharing(items, mod["share_sets"], profile)
    rms = {q: profile.F[f"rms_{q}"] for q in ("good", "medium", "poor")}
    ops: list[CostOp] = []
    for nid in sorted(items):
        it = items[nid]
        c = it.cost
        keep = c.min or c.typ or c.max or "shared_zeroed" in it.tags
        if not keep:
            continue
        nd = it.node
        o = nd["origin"] or {}
        tags = tuple(dict.fromkeys(it.tags))
        ops.append(CostOp(
            id=nid, op=nd["op"],
            category=c.category, quality=c.quality, cost=c.ge3, sigma=pm(c.typ, rms[c.quality]),
            file=_rel(o.get("file", ""), root), line=o.get("line", 0), column=o.get("column", 0),
            def_file=_rel(o["def_file"], root) if o.get("def_file") is not None else None,
            def_line=o.get("def_line"),
            named_key=tuple(nd["named_key"] or ()), shape_key=nd["shape_key"] or "",
            declared_widths=it.decl_w, effective_widths=it.eff_w, tags=tags,
            breakdown=dict(c.breakdown), arch=dict(c.arch), facts=it.facts, description=it.desc,
            members=it.members))
    return ops, diags, unsupported


@_gc.nogc
def build_cost_tree(design, profile: Profile, *, config: dict | None = None,
                    extra_diagnostics=(), stubbed=()) -> DesignCostTree:
    """Cost tree of an analyzed ``ir.Design`` (analyzes modules that are not analyzed yet).

    ``stubbed`` lists module keys that the elaborator left unbuilt (changed-cone elaboration): instance
    edges to them are kept and they are not reported as missing; the caller supplies their cost."""
    from ..ir import analyze_ir, export_design

    config = dict(config or {})
    root = config.pop("root", None)
    for key in design.keys():
        m = design.modules[key]
        if not m.analyzed:
            analyze_ir(m, fusion=profile.datapath_fusion)
    exp = export_design(design)
    counts = {k: v for k, v in exp["instance_counts"].items() if k in exp["modules"]}
    modules: dict[str, ModuleCost] = {}
    unsupported: set[str] = set()
    all_diags: list[Diagnostic] = []
    stubbed = set(stubbed)
    missing = set(exp["missing_modules"]) - stubbed
    for key in sorted(counts):
        mod = exp["modules"][key]
        ir = design.modules[key]
        ops, diags, unsup = _cost_module(key, mod, ir, profile, root, profile.datapath_fusion)
        edges = []
        for e in exp["edges"]:
            if e["parent"] != key or e["recursive"]:
                continue
            if e["child"] in exp["modules"] or e["child"] in stubbed:
                edges.append(InstanceEdge(e["name"] or "", e["child"], int(e["count"] or 1)))
        # instances of modules absent from the sources: black boxes costed through the profile
        nid = max([o.id for o in ops] + [n["id"] for n in mod["nodes"]] + [0]) + 1
        for nd in mod["nodes"]:
            if nd["op"] == "INST" and nd["instance"]["module_key"] in missing:
                name = _block_name(nd["instance"]["module_key"])
                oc = cost_op("BLACKBOX", {"name": name}, profile)
                cnt = nd["instance"]["count"] or 1
                o = nd["origin"] or {}
                tags = ["blackbox"]
                if "unknown_blackbox" in oc.flags:
                    tags.append("unknown_blackbox")
                    unsup = True
                    diags.append(Diagnostic("MISSING_MODULE", f"module {name} not found; zero cost assumed",
                                            "warning", True, "after", _rel(o.get("file", ""), root) or None,
                                            o.get("line")))
                g = Ge3(oc.min * cnt, oc.typ * cnt, oc.max * cnt)
                ops.append(CostOp(
                    id=nid, op="BLACKBOX", category="blackbox", quality=oc.quality, cost=g,
                    sigma=pm(g.typ, profile.F[f"rms_{oc.quality}"]), file=_rel(o.get("file", ""), root),
                    line=o.get("line", 0), column=o.get("column", 0), named_key=tuple(nd["named_key"] or ()),
                    shape_key=nd["shape_key"] or "", tags=tuple(tags), arch=dict(oc.arch),
                    facts={"name": name, "count": cnt},
                    description=f"BLACKBOX {name}" + (f" x{cnt}" if cnt > 1 else "")))
                nid += 1
        ops.sort(key=lambda o: o.id)
        ir_o = ir.origin
        params = tuple((str(k), _fmt_param(v)) for k, v in mod["params"])
        modules[key] = ModuleCost(
            key=key, name=mod["name"], parameters=params, canonical_hash=mod["canonical_hash"] or "",
            ops=tuple(ops), instances=tuple(edges), diagnostics=tuple(diags),
            file=_rel(ir_o.file, root) if ir_o else "", line=ir_o.line if ir_o else 0)
        if unsup:
            unsupported.add(key)
        all_diags.extend(diags)
    tdiags: list[Diagnostic] = []
    for d in list(design.diagnostics) + list(extra_diagnostics):
        tdiags.append(_diag(d, root))
    assumptions: list[str] = []
    for key in sorted(counts):
        for a in design.modules[key].assumptions:
            if a not in assumptions:
                assumptions.append(a)
    cfg = {"profile": profile.name, "defines": dict(config.get("defines") or {}), "top": config.get("top"),
           "params": list(config.get("params") or [])}
    for k, v in config.items():
        cfg.setdefault(k, v)
    return DesignCostTree(top_key=design.top, modules=modules, instance_counts=counts,
                          diagnostics=tuple(tdiags), assumptions=tuple(assumptions), config=cfg,
                          unsupported_in=frozenset(unsupported))


def tree_confidence(tree: DesignCostTree, profile: Profile) -> Confidence:
    """Confidence of a whole-design estimate (COST_MODEL.md 5.2 applied to the total)."""
    tot = tree.total()
    contribs = []
    flags = set()
    for k, m in tree.modules.items():
        n = tree.instance_counts.get(k, 0)
        for o in m.ops:
            contribs.append((o.category, o.quality, abs(o.cost.typ) * n))
            for f in ("unknown_blackbox", "macro_no_table"):
                if f in o.tags:
                    flags.add(f)
    uns = 0
    seen = set()
    for k, m in tree.modules.items():
        if tree.instance_counts.get(k, 0) and k in tree.unsupported_in:
            uns += sum(1 for d in m.diagnostics if d.affects_cost) or 1
            seen.update(id(d) for d in m.diagnostics)
    uns += sum(1 for d in tree.diagnostics if d.affects_cost and id(d) not in seen)
    c = confidence(tot, contribs, flags, uns)
    if "unknown_blackbox" in flags and not uns:
        pass
    return c


def module_totals(tree: DesignCostTree) -> dict[str, tuple[Ge3, Ge3]]:
    """key -> (own, total) per instance; total includes children weighted by edge counts."""
    own: dict[str, Ge3] = {}
    for k, m in tree.modules.items():
        own[k] = Ge3(sum(o.cost.min for o in m.ops), sum(o.cost.typ for o in m.ops),
                     sum(o.cost.max for o in m.ops))
    tot: dict[str, Ge3] = {}

    def go(k: str) -> Ge3:
        if k in tot:
            return tot[k]
        tot[k] = own[k]  # cycle guard
        a, b, c = own[k].min, own[k].typ, own[k].max
        for e in tree.modules[k].instances:
            if e.child_key in tree.modules:
                ch = go(e.child_key)
                a += ch.min * e.count
                b += ch.typ * e.count
                c += ch.max * e.count
        tot[k] = Ge3(a, b, c)
        return tot[k]

    for k in tree.modules:
        go(k)
    return {k: (own[k], tot[k]) for k in tree.modules}


def category_totals(tree: DesignCostTree) -> dict[str, int]:
    """typ milli-GE per category over the whole design (instance-weighted)."""
    out = {c: 0 for c in CATEGORIES}
    for k, m in tree.modules.items():
        n = tree.instance_counts.get(k, 0)
        for o in m.ops:
            out[o.category] = out.get(o.category, 0) + o.cost.typ * n
    if not out.get("blackbox"):
        out.pop("blackbox", None)
    return out
