"""``cost`` command (CLI.md section 2.3): discovery, elaboration, cost tree, reporting.

``run(args, settings, cfg) -> (text, exit_code)``.  The elaborator is imported lazily through
:func:`get_elaborator` (tests monkeypatch it).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .. import REPO_ROOT
from ..cost import (
    Profile, build_cost_tree, category_totals, load_profile, module_totals, tree_confidence,
)
from ..cost.tree import DesignCostTree
from ..discovery import RTL_EXT, discover, language_of
from ..errors import EXIT_BUDGET, EXIT_OK, GatecostError
from ..report import render
from ..report.json_fmt import render_json
from ..result import CostResult, Diagnostic, Ge3, ModuleCost, Output

SEARCHED = "*.v *.sv *.vh *.svh *.vhd *.vhdl"
MAX_CANDIDATE_TOPS = 8


def get_elaborator():
    try:
        from ..elab import elaborate_project
    except ImportError:
        raise GatecostError("NOT_IMPLEMENTED", "cost needs the HDL elaborator, which is not available yet") from None
    return elaborate_project


# -- filelists ---------------------------------------------------------------------------

_DEF_RE = re.compile(r"\+define\+(.*)")


def parse_filelist(path: Path, root: Path, *, _seen=None, _depth=0) -> tuple[list[Path], list[str], dict[str, str], list[Diagnostic]]:
    """Sources, include directories and defines of a ``.f`` file (``-f``/``-F`` nest)."""
    seen = _seen if _seen is not None else set()
    diags: list[Diagnostic] = []
    files: list[Path] = []
    inc: list[str] = []
    defs: dict[str, str] = {}
    path = path.resolve()
    if path in seen or _depth > 16:
        return files, inc, defs, diags
    seen.add(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        if _depth > 0:
            diags.append(Diagnostic("SOURCE_NOT_FOUND", f"nested filelist {path.name} cannot be read: "
                                    f"{getattr(exc, 'strerror', None) or exc}", "warning", True, "after", None, None))
            return files, inc, defs, diags
        raise GatecostError("BAD_ARGUMENT", f"cannot read filelist {path.name}: {getattr(exc, 'strerror', None) or exc}") from None
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    base = path.parent

    def resolve(tok: str) -> Path:
        tok = os.path.expandvars(tok)
        p = Path(tok)
        if p.is_absolute():
            return p
        for cand in (base / p, root / p, Path.cwd() / p):
            if cand.exists():
                return cand
        return base / p

    toks: list[str] = []
    for line in text.splitlines():
        line = re.split(r"//|#", line, maxsplit=1)[0].strip()
        toks.extend(line.split())
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if t in ("-f", "-F") and i < len(toks):
            f, i2, d2, dg = parse_filelist(resolve(toks[i]), root, _seen=seen, _depth=_depth + 1)
            files += f
            inc += i2
            defs.update(d2)
            diags += dg
            i += 1
        elif t.startswith("+incdir+"):
            inc += [str(resolve(x)) for x in t[len("+incdir+"):].split("+") if x]
        elif t.startswith("+define+"):
            for x in t[len("+define+"):].split("+"):
                if x:
                    n, _, v = x.partition("=")
                    defs[n] = v if _ else "1"
        elif t in ("-y", "-v", "-incdir", "-I") and i < len(toks):
            if t in ("-incdir", "-I"):
                inc.append(str(resolve(toks[i])))
            i += 1
        elif t.startswith("-I") and len(t) > 2:
            inc.append(str(resolve(t[2:])))
        elif t.startswith("-D") and len(t) > 2:
            n, _, v = t[2:].partition("=")
            defs[n] = v if _ else "1"
        elif t.startswith(("+", "-")):
            continue
        else:
            p = resolve(t)
            if p.suffix.lower() in RTL_EXT:
                if p.is_file():
                    files.append(p)
                else:
                    diags.append(Diagnostic("FILE_NOT_FOUND", f"filelist {path.name}: {t} not found", "warning", True,
                                            "after", None, None))
    return files, inc, defs, diags


# -- source set --------------------------------------------------------------------------

def collect_sources(args, settings, cfg):
    """(files as absolute paths, incdirs, defines, diagnostics) per CLI.md / DISTRIBUTION.md 4."""
    root = settings.root
    langs = tuple(cfg.get("project", "languages", None) or ("verilog", "vhdl"))
    langs = tuple("verilog" if l in ("verilog", "systemverilog") else l for l in (x.lower() for x in langs))
    diags: list[Diagnostic] = []
    files: list[Path] = []
    incdirs = list(settings.incdirs)
    defines = {}
    explicit = list(getattr(args, "paths", None) or [])
    skill = REPO_ROOT

    for pth in explicit:
        p = Path(pth)
        if not p.exists():
            raise GatecostError("BAD_ARGUMENT", f"path not found: {pth}")
        p = p.resolve()
        if p.is_file():
            files.append(p)
        else:
            rel, _ = discover(p, excludes=settings.exclude, skill_dir=skill, languages=langs)
            files += [p / r for r in rel]
    fl_files: list[Path] = []
    for fl in settings.filelists:
        f, inc, d, dg = parse_filelist(Path(fl), root)
        fl_files += f
        incdirs += inc
        defines.update(d)
        diags += dg
    files += fl_files
    if not explicit and not fl_files:
        if settings.sources:
            rel, _ = discover(root, excludes=settings.exclude, skill_dir=skill, globs=settings.sources, languages=langs)
        else:
            rel, _ = discover(root, excludes=settings.exclude, skill_dir=skill, languages=langs)
        files += [root / r for r in rel]
    seen, uniq = set(), []
    for f in files:
        f = f.resolve()
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    # headers are found through include directories, not compiled on their own
    uniq = [f for f in uniq if f.suffix.lower() not in (".vh", ".svh")]
    uniq = [f for f in uniq if language_of(f) in langs]
    defines.update(settings.defines)
    seen_i, inc_u = set(), []
    for d in incdirs:
        if d not in seen_i:
            seen_i.add(d)
            inc_u.append(d)
    if not uniq:
        where = ", ".join(explicit) if explicit else "."
        raise GatecostError("NO_SOURCES", f"no RTL files found under {where} (searched {SEARCHED})")
    return uniq, inc_u, defines, diags


# -- result ------------------------------------------------------------------------------

def _g(g: Ge3) -> dict:
    return {"min": g.min / 1000, "typ": g.typ / 1000, "max": g.max / 1000}


def operations_detail(tree: DesignCostTree) -> tuple[dict, ...]:
    out = []
    for key in sorted(tree.modules):
        m = tree.modules[key]
        n = tree.instance_counts.get(key, 0)
        for o in m.ops:
            out.append({
                "module": m.name, "module_key": key, "id": o.id, "op": o.op, "category": o.category,
                "quality": o.quality, "file": o.file, "line": o.line, "column": o.column,
                "description": o.description, "instances": n, "ge": _g(o.cost),
                "declared_widths": list(o.declared_widths), "effective_widths": list(o.effective_widths),
                "tags": list(o.tags), "breakdown": dict(o.breakdown), "arch": dict(o.arch),
                "facts": dict(o.facts), "named_key": [str(x) for x in o.named_key], "shape_key": o.shape_key,
            })
    return tuple(out)


def make_result(tree: DesignCostTree, profile: Profile, by: str, detail: bool, assumed=()) -> CostResult:
    tot = module_totals(tree)
    top = tree.modules[tree.top_key]
    rows = []
    if by == "module":
        for key, m in tree.modules.items():
            own, total = tot[key]
            rows.append(ModuleCost(m.name, tree.instance_counts.get(key, 0), own, total, dict(m.parameters)))
        rows.sort(key=lambda r: (-r.total.typ * max(r.instances, 1), r.module))
    elif by == "file":
        acc: dict[str, list[int]] = {}
        for key, m in tree.modules.items():
            n = tree.instance_counts.get(key, 0)
            for o in m.ops:
                a = acc.setdefault(o.file or "-", [0, 0, 0])
                a[0] += o.cost.min * n
                a[1] += o.cost.typ * n
                a[2] += o.cost.max * n
        for f, a in sorted(acc.items(), key=lambda kv: (-kv[1][1], kv[0])):
            g = Ge3(*a)
            rows.append(ModuleCost(f, 1, g, g, {}))
    total = tree.total()
    return CostResult(
        top=top.name, total=total, confidence=tree_confidence(tree, profile), modules=tuple(rows),
        by_category=category_totals(tree), assumed=tuple(assumed),
        operations=operations_detail(tree) if detail else None)


def _render(out: Output, settings) -> str:
    if settings.format == "json":
        return render_json(out, include_meta=not settings.no_meta)
    return render(out, settings.format, max_lines=settings.max_lines, verbose=settings.verbose, quiet=settings.quiet)


def _as_diag(d) -> Diagnostic:
    if isinstance(d, Diagnostic):
        return d
    if hasattr(d, "as_result_diagnostic"):
        return d.as_result_diagnostic("after")
    if isinstance(d, dict):
        return Diagnostic(d["code"], d["message"], d.get("severity", "warning"), bool(d.get("affects_cost")),
                          d.get("side", "after"), d.get("file"), d.get("line"), d.get("column"))
    return d


def _dedupe(diags) -> tuple[Diagnostic, ...]:
    seen, out = set(), []
    for d in diags:
        k = (d.code, d.message, d.file, d.line)
        if k not in seen:
            seen.add(k)
            out.append(d)
    return tuple(out)


def analyze_project(files, root, top, defines, incdirs, limits, profile, elaborate=None):
    """Elaborate and build the cost tree.  Returns (tree, elab result, extra diagnostics, extra assumptions)."""
    from ..pipeline import analyze_project as ap

    return ap(files, root, top, defines, incdirs, limits, profile, elaborate or get_elaborator())


def run(args, settings, cfg) -> tuple[str, int]:
    overrides = cfg.get("cost", "override", None) or None
    profile = load_profile(settings.profile, overrides, Path.cwd())
    files, incdirs, defines, fdiags = collect_sources(args, settings, cfg)
    limits = dict(cfg.data.get("limits", {})) if getattr(cfg, "data", None) else {}
    from .. import pipeline
    from ..cache import resolve_cache

    cache = resolve_cache(settings)
    t0 = time.perf_counter()
    inp = pipeline.read_inputs(files, incdirs, defines, settings.root)
    sr = pipeline.analyze_side(inp, profile, settings.top, limits, cache, elaborate=get_elaborator(), side="after")
    tree = sr.tree
    assumed = list(sr.assumptions)
    result = make_result(tree, profile, getattr(args, "by", "module") or "module", getattr(args, "detail", None) == "ops", assumed)
    diags = list(tree.diagnostics)
    for m in tree.modules.values():
        diags += m.diagnostics
    diags = list(fdiags) + diags + list(profile.diagnostics) + list(settings.diagnostics)
    code = EXIT_OK
    if settings.budget is not None:
        basis = settings.basis if settings.basis in ("typ", "max") else "typ"
        value = getattr(result.total, basis)
        if value > settings.budget * 1000:
            code = EXIT_BUDGET
            diags.append(Diagnostic("BUDGET_EXCEEDED", f"total {round(value / 1000)} GE ({basis}) exceeds budget "
                                    f"{settings.budget:g} GE", "error", True, "after"))
    diags = _dedupe(diags)
    status = "partial" if any(d.affects_cost for d in diags if d.code != "BUDGET_EXCEEDED") else "ok"
    cfg_out = {"top": result.top, "defines": dict(defines), "sources": len(files)}
    out = Output(command="cost", status=status, profile={"name": profile.name, "nand2_area_um2": profile.nand2_area_um2},
                 config=cfg_out, result=result, diagnostics=diags,
                 meta={"elapsed_ms": round((time.perf_counter() - t0) * 1000), "cache": cache.state})
    return _render(out, settings), code
