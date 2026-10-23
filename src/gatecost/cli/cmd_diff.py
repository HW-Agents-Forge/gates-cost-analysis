"""``diff`` command (CLI.md section 2.2): baseline, two analyses, differ, reporters, budgets."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..cost import load_profile
from ..errors import EXIT_BUDGET, EXIT_OK, GatecostError
from ..report import render
from ..report.json_fmt import render_json
from ..result import Diagnostic, DiffResult, Output
from .cmd_cost import collect_sources


def _render(out: Output, settings, limit=None) -> str:
    if settings.format == "json":
        return render_json(out, include_meta=not settings.no_meta, limit=limit)
    return render(out, settings.format, max_lines=settings.max_lines, verbose=settings.verbose, quiet=settings.quiet)


def run(args, settings, cfg, elaborate=None) -> tuple[str, int]:
    from .. import pipeline

    overrides = cfg.get("cost", "override", None) or None
    profile = load_profile(settings.profile, overrides, Path.cwd())
    if elaborate is None:
        from .cmd_cost import get_elaborator

        elaborate = get_elaborator()
    run = pipeline.run_diff(args, settings, cfg, profile, collect_sources, elaborate)
    res: DiffResult = run.result
    if run.operations is not None:
        res = replace(res, operations=run.operations)
    diags = list(run.diagnostics)
    # persistent diagnostics of the current state that affect the figure (new ones come with the result)
    seen = {(d.code, d.file, d.message) for d in res.diagnostics_new}
    tree = getattr(run, "after_tree", None)
    if tree is not None:
        for d in list(tree.diagnostics) + [x for m in tree.modules.values() for x in m.diagnostics]:
            k = (d.code, d.file, d.message)
            if d.affects_cost and k not in seen:
                seen.add(k)
                diags.append(d)
    code = EXIT_OK
    basis = settings.basis if settings.basis in ("typ", "max") else "typ"
    msgs = []
    if res.after is not None and settings.budget is not None:
        v = getattr(res.after, basis)
        if v > settings.budget * 1000:
            msgs.append(f"total {round(v / 1000)} GE ({basis}) exceeds budget {settings.budget:g} GE")
    if res.delta is not None and settings.fail_on_increase is not None:
        v = getattr(res.delta, basis)
        if v > settings.fail_on_increase * 1000:
            msgs.append(f"delta +{round(v / 1000)} GE ({basis}) exceeds limit {settings.fail_on_increase:g} GE")
    for m in msgs:
        code = EXIT_BUDGET
        diags.append(Diagnostic("BUDGET_EXCEEDED", m, "error", True, "after"))
    seen2, uniq = set(), []
    for d in diags:
        k = (d.code, d.message, d.file, d.line)
        if k not in seen2:
            seen2.add(k)
            uniq.append(d)
    status = "partial" if any(d.affects_cost for d in uniq if d.code != "BUDGET_EXCEEDED") or \
        any(d.affects_cost for d in res.diagnostics_new) else "ok"
    out = Output(command="diff", status=status,
                 profile={"name": run.profile_name, "nand2_area_um2": profile.nand2_area_um2},
                 config=run.config, result=res, diagnostics=tuple(uniq),
                 meta={"elapsed_ms": getattr(run, "elapsed_ms", 0), "cache": run.cache_state})
    return _render(out, settings, getattr(args, "limit", None)), code
