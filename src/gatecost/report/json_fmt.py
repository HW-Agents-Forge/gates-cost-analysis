"""The `json` format (OUTPUT.md section 3). Keys are emitted in a fixed order."""
from __future__ import annotations

import json
from dataclasses import fields, is_dataclass

from .. import version
from ..result import (
    CompareResult,
    Confidence,
    Contributor,
    CostResult,
    Diagnostic,
    DiffResult,
    EstimateResult,
    Ge3,
    MessageResult,
    Output,
    SnapshotResult,
)

SCHEMA_VERSION = 1


def _num(milli: int):
    """Milli-GE to GE rounded to 0.001; integers when exact."""
    if milli % 1000 == 0:
        return milli // 1000
    return round(milli / 1000, 3)


def _g(g: Ge3 | None):
    if g is None:
        return None
    return {"min": _num(g.min), "typ": _num(g.typ), "max": _num(g.max)}


def _conf(c: Confidence) -> dict:
    d: dict = {"level": c.level}
    if c.range_ratio is not None:
        d["range_ratio"] = c.range_ratio
    d["reasons"] = list(c.reasons)
    return d


def _contrib(c: Contributor, rank: int | None = None) -> dict:
    d: dict = {}
    if rank is not None:
        d["rank"] = rank
    d.update(
        file=c.file,
        line=c.line,
        location_side=c.location_side,
        reason=c.reason,
        cause=c.cause,
        description=c.description,
        op=c.op,
        widths=list(c.widths),
        category=c.category,
        module=c.module,
        instances=c.instances,
        delta_ge=_g(c.delta),
        per_instance_ge=_g(c.per_instance or c.delta),
        children=[_contrib(x) for x in c.children],
    )
    return d


def diagnostic_dict(d: Diagnostic) -> dict:
    return {
        "code": d.code,
        "severity": d.severity,
        "affects_cost": d.affects_cost,
        "side": d.side,
        "file": d.file,
        "line": d.line,
        "column": d.column,
        "message": d.message,
    }


def _estimate(r: EstimateResult) -> dict:
    return {
        "input": {"kind": r.kind, "text": r.text},
        "normalized": r.normalized,
        "ge": _g(r.ge),
        "architecture": r.architecture,
        "breakdown": r.breakdown,
        "confidence": _conf(r.confidence),
    }


def _result(r, limit: int | None = None):
    if isinstance(r, DiffResult):
        cs = r.contributors if limit is None else r.contributors[:limit]
        return {
            "verdict": r.verdict,
            "confidence": _conf(r.confidence),
            "top": r.top,
            "before": None if r.before is None else {"ge": _g(r.before)},
            "after": None if r.after is None else {"ge": _g(r.after)},
            "delta": None if r.delta is None else {"ge": _g(r.delta), "percent": r.percent},
            "contributors": [_contrib(c, i + 1) for i, c in enumerate(cs)],
            "others": {"count": r.others_count, "net_ge": _g(r.others_net)},
            "by_module": list(r.by_module),
            "by_category": {k: _num(v) for k, v in r.by_category.items()},
            "structural": list(r.structural),
            "diagnostics_delta": {
                "new": [diagnostic_dict(d) for d in r.diagnostics_new],
                "resolved": [diagnostic_dict(d) for d in r.diagnostics_resolved],
            },
            **({"operations": list(r.operations)} if r.operations is not None else {}),
        }
    if isinstance(r, CostResult):
        return {
            "top": r.top,
            "total": {"ge": _g(r.total)},
            "confidence": _conf(r.confidence),
            "modules": [
                {
                    "module": m.module,
                    "parameters": m.parameters,
                    "instances": m.instances,
                    "own_ge": _g(m.own),
                    "total_ge": _g(m.total),
                }
                for m in r.modules
            ],
            "by_category": {k: _num(v) for k, v in r.by_category.items()},
            "operations": list(r.operations or ()),
        }
    if isinstance(r, EstimateResult):
        return _estimate(r)
    if isinstance(r, CompareResult):
        return {
            "basis": r.basis,
            "alternatives": [_estimate(a) for a in r.alternatives],
            "best": r.best_index,
            "saves_ge": _g(r.saves),
            "ranges": "overlap" if r.overlap else "disjoint",
        }
    if isinstance(r, SnapshotResult):
        return {"id": r.id, "files": r.files, "modules": r.modules}
    if isinstance(r, MessageResult):
        return {"lines": list(r.lines), **r.data}
    if is_dataclass(r):
        return {f.name: getattr(r, f.name) for f in fields(r)}
    return r


def envelope(out: Output, *, include_meta: bool = True, limit: int | None = None) -> dict:
    env: dict = {
        "schema_version": SCHEMA_VERSION,
        "tool": "gatecost",
        "command": out.command,
        "status": out.status,
        "profile": out.profile,
        "config": out.config,
        "result": None if out.status == "error" else _result(out.result, limit),
    }
    if out.status == "error" and out.error:
        env["error"] = {"code": out.error[0], "message": out.error[1]}
    env["diagnostics"] = [diagnostic_dict(d) for d in out.diagnostics]
    if include_meta:
        meta = {"version": version()}
        meta.update(out.meta or {})
        env["meta"] = meta
    return env


def render_json(out: Output, *, include_meta: bool = True, limit: int | None = None) -> str:
    return json.dumps(envelope(out, include_meta=include_meta, limit=limit), indent=2, ensure_ascii=False) + "\n"
