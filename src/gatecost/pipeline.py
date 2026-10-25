"""Analysis pipeline shared by ``cost`` and ``diff``.

    sources -> elaborate -> cost tree (+ provenance facts) -> [cache] -> differ -> DiffResult

``analyze_side`` turns one source set into a :class:`SideResult` (cost tree, assumptions) and
caches it as JSON keyed by ``cache.analysis_key`` (file contents, defines, include contents,
profile, tool version and a fingerprint of the tool's own code).  ``run_diff`` resolves the
baseline, analyzes both sides with identical defines/top/incdirs/profile and calls the differ.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

from . import cache as cache_mod
from .config import ProjectConfig
from .params import canon as canon_params
from .params import differing as params_differing
from .params import params_json
from .cost.tree import DesignCostTree
from .discovery import RTL_EXT, VERILOG_EXT
from .errors import GatecostError
from .result import Diagnostic

_MAX_INCLUDE_FILES = 2000
_MAX_INCLUDE_BYTES = 64 * 1024 * 1024


# --------------------------------------------------------------------------- cached record


@dataclass(slots=True, frozen=True)
class SideResult:
    """What is cached per analyzed source set."""

    tree: DesignCostTree
    assumptions: tuple = ()
    notes: tuple = ()
    tops: tuple = ()
    module_assumptions: dict = field(default_factory=dict)   # module key -> assumptions
    global_assumptions: tuple = ()
    index: dict | None = None        # per-file facts of a full Verilog analysis (see ``build_index``)


cache_mod.register(SideResult)

_FP: str | None = None


def code_fingerprint() -> str:
    """Hash of the sizes and mtimes of the tool's own sources: any code change invalidates caches."""
    global _FP
    if _FP is None:
        h = hashlib.blake2b(digest_size=8)
        base = Path(__file__).resolve().parent
        for dp, dn, fn in os.walk(base):
            dn[:] = sorted(d for d in dn if d != "__pycache__")
            for f in sorted(fn):
                if f.endswith((".py", ".toml")):
                    try:
                        st = os.stat(os.path.join(dp, f))
                    except OSError:
                        continue
                    h.update(f"{os.path.relpath(os.path.join(dp, f), base)}:{st.st_size}:{st.st_mtime_ns}".encode())
        _FP = h.hexdigest()
    return _FP


def rel_name(p: Path, root: Path) -> str:
    try:
        return Path(os.path.abspath(p)).relative_to(Path(os.path.abspath(root))).as_posix()
    except ValueError:
        return Path(p).as_posix()


# --------------------------------------------------------------------------- inputs


@dataclass
class Inputs:
    root: Path
    files: list
    incdirs: list
    defines: dict
    texts: dict = field(default_factory=dict)       # rel name -> text (RTL files and headers)
    file_items: list = field(default_factory=list)  # (rel, bytes) of the compiled sources
    include_items: list = field(default_factory=list)
    params: dict = field(default_factory=dict)      # typed top-level parameter / generic overrides


def read_inputs(files, incdirs, defines, root: Path, params=None) -> Inputs:
    inp = Inputs(Path(root), list(files), list(incdirs), dict(defines), params=dict(params or {}))
    seen = set()
    for f in inp.files:
        try:
            data = Path(f).read_bytes()
        except OSError:
            data = b""
        n = rel_name(Path(f), root)
        seen.add(n)
        inp.file_items.append((n, data))
        inp.texts[n] = data.decode("utf-8", "replace")
    total = 0
    count = 0
    for d in inp.incdirs:
        for dp, dn, fn in os.walk(d):
            dn[:] = sorted(x for x in dn if x not in (".git", ".gatecost", "__pycache__"))
            for f in sorted(fn):
                p = Path(dp) / f
                n = rel_name(p, root)
                if n in seen:
                    continue
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                count += 1
                total += len(data)
                if count > _MAX_INCLUDE_FILES or total > _MAX_INCLUDE_BYTES:
                    break
                seen.add(n)
                inp.include_items.append((n, data))
                if p.suffix.lower() in RTL_EXT:
                    inp.texts[n] = data.decode("utf-8", "replace")
    _add_implicit_includes(inp, seen, root)
    return inp


_INCLUDE_DIRECTIVE = re.compile(r'`include\s*(?:"([^"\n]+)"|<([^>\n]+)>)')


def _add_implicit_includes(inp: Inputs, seen: set, root: Path) -> None:
    """Headers found through the including file's own directory belong to the analysis input as well: they
    are part of the cache key and of the line alignment, exactly like those of an include directory."""
    from .frontend.common.reader import resolve_include

    queue = [(Path(f), inp.texts.get(rel_name(Path(f), root), "")) for f in inp.files
             if Path(f).suffix.lower() in VERILOG_EXT]
    while queue and len(seen) < _MAX_INCLUDE_FILES:
        path, text = queue.pop()
        for m in _INCLUDE_DIRECTIVE.finditer(text):
            name = m.group(1) or m.group(2)
            if os.path.isabs(name):
                continue
            found = resolve_include(name, str(path.parent), inp.incdirs)
            if found is None:
                continue
            p = Path(found)
            n = rel_name(p, root)
            if n in seen:
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            seen.add(n)
            inp.include_items.append((n, data))
            inp.texts[n] = data.decode("utf-8", "replace")
            queue.append((p, inp.texts[n]))


def profile_digest(profile) -> str:
    raw = json.dumps(getattr(profile, "raw", {}), sort_keys=True, default=str)
    return profile.name + ":" + hashlib.blake2b(raw.encode(), digest_size=8).hexdigest()


def side_key(inp: Inputs, profile, top, limits, prefer=None, kind="tree") -> str:
    from . import version

    return cache_mod.analysis_key(
        inp.file_items, inp.defines, inp.include_items, profile_digest(profile),
        tool_version=version() + "+" + code_fingerprint(),
        extra={"kind": kind, "top": top, "limits": limits or {}, "params": canon_params(inp.params), "incdirs": [rel_name(Path(d), inp.root) for d in inp.incdirs],
               "prefer": sorted(prefer) if prefer is not None else None, "order": [n for n, _ in inp.file_items]})


# --------------------------------------------------------------------------- analysis of one side


def get_elaborator():
    try:
        from .elab import elaborate_project
    except ImportError:
        raise GatecostError("NOT_IMPLEMENTED", "the HDL elaborator is not available") from None
    return elaborate_project


def _as_diag(d, root=None) -> Diagnostic:
    if isinstance(d, Diagnostic):
        pass
    elif hasattr(d, "as_result_diagnostic"):
        d = d.as_result_diagnostic("after")
    elif isinstance(d, dict):
        d = Diagnostic(d["code"], d["message"], d.get("severity", "warning"), bool(d.get("affects_cost")),
                       d.get("side", "after"), d.get("file"), d.get("line"), d.get("column"))
    if root is not None and isinstance(d, Diagnostic):
        msg = _clean_paths(d.message, Path(root))
        if msg != d.message:
            d = replace(d, message=msg)
    return d


def _touches(tree: DesignCostTree, prefer) -> int:
    if not prefer:
        return 0
    return sum(1 for k, m in tree.modules.items() if tree.instance_counts.get(k, 0) and m.file in prefer)


def analyze_project(files, root, top, defines, incdirs, limits, profile, elaborate=None, *, side="after",
                    texts=None, prefer=None, facts_out=None, params=None):
    """Elaborate and build the cost tree.  Returns (tree, elab result, extra diagnostics, notes)."""
    from .cost import build_cost_tree

    elaborate = elaborate or get_elaborator()
    kw = dict(root=root, top=top, defines=defines, incdirs=incdirs, limits=limits, side=side)
    if params:
        kw["params"] = params
    res = elaborate(files, **kw)
    design = res.design
    tops = list(getattr(res, "tops", ()) or ())
    key = res.top_key
    if key is None or design is None or key not in design.modules:
        if top:
            raise GatecostError("TOP_NOT_FOUND", f"top module {top} not found in the sources")
        if not tops:
            raise GatecostError("NO_SOURCES", "no module or entity definition found in the sources")

    def mk(r, name):
        t = build_cost_tree(r.design, profile, config={"defines": defines, "top": top, "root": str(root),
                                                 "params": canon_params(params)},
                            extra_diagnostics=[_as_diag(d, root) for d in r.diagnostics])
        return _annotate(t, texts, defines, root, r, facts_out=facts_out)

    notes: list[str] = []
    if top is None and len(tops) > 1:
        cands = []
        for name in tops[:8]:
            r = res if (key is not None and (name == key or name == design.modules[key].name)) else elaborate(files, **{**kw, "top": name})
            if r.top_key is None:
                continue
            cands.append((name, mk(r, name), r))
        if cands:
            best = max(cands, key=lambda c: (_touches(c[1], prefer), c[1].total().typ, c[0]))
            notes.append("multiple top candidates: " + ", ".join(c[0] for c in cands)
                         + f"; reported {best[0]} (use --top to select)")
            for name, t, _ in cands:
                if t is not best[1]:
                    notes.append(f"top candidate {name} total={round(t.total().typ / 1000)} GE")
            return best[1], best[2], [], notes
    if key is None:
        raise GatecostError("TOP_NOT_FOUND", "no top module could be determined (use --top)")
    return mk(res, design.modules[key].name), res, [], notes


def _annotate(tree, texts, defines, root, res, **kw):
    try:
        from .cost.provenance import annotate_tree
    except ImportError:
        return tree
    if texts is None:
        return tree
    return annotate_tree(tree, texts, defines, **kw)


def _clean_paths(text: str, root: Path) -> str:
    """Messages must not contain absolute paths (OUTPUT.md section 1)."""
    for r in {str(root), str(Path(root).resolve())}:
        text = text.replace(r.rstrip("/") + "/", "")
    return text


def compose_assumptions(global_a, module_a: dict, tree: DesignCostTree, notes, root) -> tuple:
    out: list[str] = []
    for a in list(global_a) + [x for k in sorted(tree.instance_counts) for x in module_a.get(k, ())] + list(notes):
        a = _clean_paths(a, root)
        if a not in out:
            out.append(a)
    return tuple(out)


def make_side(tree, res, notes, root, index=None) -> SideResult:
    design = res.design
    mod_a = {}
    union = set()
    for k, m in getattr(design, "modules", {}).items():
        for a in getattr(m, "assumptions", ()) or ():
            union.add(a)
    for k in tree.modules:
        m = design.modules.get(k) if design is not None else None
        if m is not None:
            mod_a[k] = tuple(getattr(m, "assumptions", ()) or ())
    glob = tuple(sorted(a for a in (getattr(res, "assumptions", None) or ()) if a not in union))
    return SideResult(tree, compose_assumptions(glob, mod_a, tree, notes, root), tuple(notes),
                      tuple(getattr(res, "tops", ()) or ()), mod_a, glob, index)


def analyze_side(inp: Inputs, profile, top, limits, cache, *, elaborate=None, side="after", prefer=None) -> SideResult:
    # ``prefer`` only steers the choice among several top candidates: a single-top analysis is stored
    # under a key without it, so it is shared by every edit of the same sources (baseline of a diff).
    key1 = keyp = None
    if cache.enabled:
        key1 = side_key(inp, profile, top, limits, None, kind="tree1")
        hit = cache.get(key1)
        if isinstance(hit, SideResult):
            return hit
        keyp = side_key(inp, profile, top, limits, prefer or (), kind="tree")
        hit = cache.get(keyp)
        if isinstance(hit, SideResult):
            return hit
    facts: dict = {}
    tree, res, _, notes = analyze_project(inp.files, inp.root, top, inp.defines, inp.incdirs, limits, profile,
                                          elaborate, side=side, texts=inp.texts, prefer=prefer, facts_out=facts, params=inp.params)
    sr = make_side(tree, res, notes, inp.root, build_index(inp, res, facts) if not notes else None)
    if keyp:
        cache.put(keyp if notes else key1, sr)
    return sr


# --------------------------------------------------------------------------- per-file index (changed cone)

_CTX_TEXT = re.compile(r"`(?:define|undef|undefineall|include)\b|(?<![\w$])(?:bind|primitive)\b")


def _file_index(facts: dict, inp: Inputs, ifdefs: dict) -> dict | None:
    """``{"files": {rel: {"mods", "ctx", "ifd", "diags"}}}`` from per-file parse facts, or None if unusable."""
    from .elab import _abs

    if not facts or "" in facts or not inp.files:
        return None
    rels = {}
    for f in inp.files:
        n = rel_name(Path(f), inp.root)
        if n.startswith(("/", "..")):
            return None
        rels[_abs(f, inp.root)] = n
    out = {}
    for path, ft in facts.items():
        n = rels.get(path)
        if n is None or n not in ifdefs:
            return None
        out[n] = {"mods": ft["mods"], "ctx": bool(ft["unit"] or _CTX_TEXT.search(inp.texts.get(n, ""))),
                  "ifd": list(ifdefs[n]), "diags": tuple(ft["diags"])}
    if set(out) != set(rels.values()):
        return None
    return {"files": out}


def build_index(inp: Inputs, res, ifdefs: dict) -> dict | None:
    """Facts about every source file of a full analysis; a later analysis of the changed cone uses them to
    parse only the files it needs.  None when the project cannot be handled that way (VHDL, ...)."""
    try:
        if any(Path(f).suffix.lower() not in VERILOG_EXT for f in inp.files):
            return None
        facts = res.file_facts
        return _file_index(facts, inp, ifdefs) if facts is not None else None
    except Exception:  # best-effort optimisation: None means a full analysis, the result is identical
        return None


# --------------------------------------------------------------------------- changed cone (FRONTEND.md section 5)

_V_MOD = re.compile(r"(?<![\w$])(?:macro)?module\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)")
_V_UNSAFE = re.compile(r"(?<![\w$])(?:package|interface|program|class|bind|config|checker|clocking|import|export)\b|"
                       r"`(?:define|undef|include|undefineall|resetall|pragma|begin_keywords)\b")
_H_ENT = re.compile(r"\bentity\s+(\w+)\s+is\b|\barchitecture\s+\w+\s+of\s+(\w+)\s+is\b", re.I)
_H_UNSAFE = re.compile(r"\b(?:package|configuration|context|use\s+work)\b", re.I)


def cone_names(changed, texts_before: dict, texts_after: dict) -> set | None:
    """Names of the modules defined in the changed files, or None when the edit may reach beyond them
    (headers, filelists, packages, macros, ...): the full analysis is then required."""
    from .discovery import VERILOG_EXT, VHDL_EXT

    names: set[str] = set()
    for rel in changed:
        ext = Path(rel).suffix.lower()
        if ext in (".vh", ".svh") or ext not in (VERILOG_EXT | VHDL_EXT):
            return None
        for text in (texts_before.get(rel, ""), texts_after.get(rel, "")):
            if ext in VHDL_EXT:
                if _H_UNSAFE.search(text):
                    return None
                for m in _H_ENT.finditer(text):
                    names.add((m.group(1) or m.group(2)))
            else:
                clean = re.sub(r"//[^\n]*|/\*.*?\*/", " ", text, flags=re.S)
                if _V_UNSAFE.search(clean):
                    return None
                names.update(_V_MOD.findall(clean))
    return names or None


def _block_name(key: str) -> str:
    for i, ch in enumerate(key):
        if ch in "([#{":
            return key[:i]
    return key


def _recount(modules: dict, top_key: str) -> dict:
    order: list[str] = []
    state: dict[str, int] = {}

    def dfs(k: str) -> None:
        state[k] = 1
        for e in modules[k].instances:
            if e.child_key in modules and state.get(e.child_key, 0) == 0:
                dfs(e.child_key)
        state[k] = 2
        order.append(k)

    dfs(top_key)
    order.reverse()
    counts = {top_key: 1}
    for k in order:
        c = counts.get(k, 0)
        for e in modules[k].instances:
            if e.child_key in modules:
                counts[e.child_key] = counts.get(e.child_key, 0) + c * e.count
    return counts


class _ConePlan:
    """Which sources a changed-cone analysis has to parse, and what it knows about the others."""

    __slots__ = ("files", "roots", "need", "diags", "texts", "ifdef_extra", "order", "parsed", "total")


def _cone_plan(inp: Inputs, ref: SideResult, names: set, changed, top, limits) -> _ConePlan | None:
    """Parse only the changed files, the files that must always be read (macros, packages, ...), the changed
    modules' ancestors up to the top and the direct children of those (their headers).  Everything else is
    known from the baseline's per-file index.  None whenever that cannot be proven equivalent to parsing all."""
    from .elab import _abs, quick_facts

    idx = ref.index
    if not idx or changed is None or len(ref.tops) > 1:
        return None
    ents = idx["files"]
    rels = [n for n, _ in inp.file_items]
    relset = set(rels)
    changed = set(changed)
    if len(relset) != len(rels):
        return None
    for r in rels:
        if Path(r).suffix.lower() not in VERILOG_EXT or r.startswith(("/", "..")):
            return None
        if r not in changed and r not in ents:
            return None
    for r in ents:
        if r not in relset and r not in changed:
            return None
    ctx_files = {r for r in rels if r not in changed and ents[r]["ctx"]}
    phase1 = [r for r in rels if r in changed or r in ctx_files]
    path_of = {rel_name(Path(f), inp.root): f for f in inp.files}
    fresh: dict = {}
    if phase1:
        facts = quick_facts([path_of[r] for r in phase1], root=inp.root, defines=inp.defines, incdirs=inp.incdirs,
                            limits=limits)
        if "" in facts:
            return None
        back = {_abs(path_of[r], inp.root): r for r in phase1}
        for path, ft in facts.items():
            fresh[back[path]] = ft
    # the instantiation graph of the whole project
    defs: dict = {}
    insts: dict = {}
    order: list = []
    for r in rels:
        ft = fresh.get(r) if r in fresh else ents[r]
        for name, kind, ins in ft["mods"]:
            if name in defs:
                return None                  # redefinition: the full parse reports it
            defs[name] = (r, kind)
            insts[name] = ins
            if kind == "module":
                order.append(name)
    inst_all = set()
    for name, ins in insts.items():
        inst_all.update(x for x in ins if x != name)
    roots = sorted(n for n in order if n not in inst_all)
    if not roots:
        return None
    if top is not None:
        if top not in defs:
            return None
        chosen = [top]
    else:
        if len(roots) > 1:
            return None
        chosen = roots
    rev: dict = {}
    for name in order:
        for c in insts[name]:
            rev.setdefault(c, set()).add(name)
    need = set(names) | set(chosen)
    stack = list(names)
    while stack:
        x = stack.pop()
        for p in rev.get(x, ()):
            if p not in need:
                need.add(p)
                stack.append(p)
    keep = set(phase1)
    for m in need:
        d = defs.get(m)
        if d is not None:
            keep.add(d[0])
            for c in insts[m]:
                dc = defs.get(c)
                if dc is not None:
                    keep.add(dc[0])
    plan = _ConePlan()
    plan.files = [f for f in inp.files if rel_name(Path(f), inp.root) in keep]
    plan.roots = roots
    plan.need = need
    plan.diags = [d for r in rels if r not in keep for d in ents[r]["diags"]]
    plan.texts = {k: v for k, v in inp.texts.items() if k in keep or k not in relset or _CTX_TEXT.search(v)}
    plan.ifdef_extra = {r: ents[r]["ifd"] for r in rels if r not in keep}
    plan.order = list(inp.texts)
    plan.parsed = len(keep)
    plan.total = len(rels)
    return plan


cone_stats: dict = {}        # what the last changed-cone analysis parsed (tests, diagnostics)


def analyze_cone(inp: Inputs, profile, top, limits, cache, ref: SideResult, names: set, ref_key, *, elaborate,
                 side="after", changed=None) -> SideResult | None:
    """Elaborate only the changed modules (and their ancestors); substitute the costs of the unchanged
    modules from the baseline analysis.  Returns None whenever that is not provably safe."""
    from .cost import build_cost_tree

    if len(ref.tops) > 1:
        return None
    key = None
    if cache.enabled:
        key = side_key(inp, profile, top, limits, None, kind="cone:" + ref_key + ":" + ",".join(sorted(names)))
        hit = cache.get(key)
        if isinstance(hit, SideResult):
            return hit
    elaborate = elaborate or get_elaborator()
    names = set(names)
    res = None
    use_plan = changed is not None and not os.environ.get("GATECOST_NO_SUBSET")
    plan = None
    for _ in range(4):
        plan = _cone_plan(inp, ref, names, changed, top, limits) if use_plan else None
        kw = dict(root=inp.root, top=top, defines=inp.defines, incdirs=inp.incdirs, limits=limits, side=side,
                  only_modules=names)
        if inp.params:
            kw["params"] = inp.params
        try:
            if plan is not None:
                try:
                    res = elaborate(plan.files, roots_hint=plan.roots, needed_hint=plan.need, extra_parse_diags=plan.diags, **kw)
                except TypeError:          # an elaborator without the cone extensions: parse everything
                    use_plan = False
                    plan = None
                    res = elaborate(inp.files, **kw)
            else:
                res = elaborate(inp.files, **kw)
        except TypeError:
            return None
        stubs = set(getattr(res, "stubbed", ()) or ())
        missing = [k for k in stubs if k not in ref.tree.modules]
        if not missing:
            break
        names |= {_block_name(k) for k in missing}
    else:
        return None
    if res is None or res.top_key is None or len(getattr(res, "tops", ()) or ()) > 1 or res.design is None:
        return None
    design = res.design
    if res.top_key not in design.modules:
        return None
    cone_stats.clear()
    cone_stats.update(parsed=plan.parsed if plan else len(inp.files), total=len(inp.files))
    partial = build_cost_tree(design, profile, config={"defines": inp.defines, "top": top, "root": str(inp.root),
                                                 "params": canon_params(inp.params)},
                              extra_diagnostics=[_as_diag(d, inp.root) for d in res.diagnostics], stubbed=stubs)
    if plan is not None:
        partial = _annotate(partial, plan.texts, inp.defines, inp.root, res, ifdef_extra=plan.ifdef_extra,
                            file_order=plan.order)
    else:
        partial = _annotate(partial, inp.texts, inp.defines, inp.root, res)
    modules = dict(partial.modules)
    mod_a = {k: tuple(getattr(design.modules[k], "assumptions", ()) or ()) for k in partial.modules if k in design.modules}
    unsupported = set(partial.unsupported_in)
    stack = sorted(stubs)
    while stack:
        k = stack.pop()
        if k in modules:
            continue
        m = ref.tree.modules.get(k)
        if m is None:
            return None
        modules[k] = m
        mod_a[k] = ref.module_assumptions.get(k, ())
        if k in ref.tree.unsupported_in:
            unsupported.add(k)
        stack.extend(e.child_key for e in m.instances if e.child_key in ref.tree.modules)
    counts = _recount(modules, partial.top_key)
    modules = {k: v for k, v in sorted(modules.items()) if k in counts}
    tree = DesignCostTree(partial.top_key, modules, counts, partial.diagnostics, (), partial.config,
                          frozenset(k for k in unsupported if k in counts))
    glob = tuple(sorted(a for a in (getattr(res, "assumptions", None) or ())
                        if not any(a in v for v in mod_a.values()) and a not in ref.global_assumptions) )
    glob = tuple(sorted(set(glob) | set(ref.global_assumptions)))
    sr = SideResult(tree, compose_assumptions(glob, mod_a, tree, (), inp.root), (), tuple(res.tops or ()), mod_a, glob)
    if key:
        cache.put(key, sr)
    return sr


# --------------------------------------------------------------------------- diff


@dataclass
class DiffRun:
    result: object
    diagnostics: list
    config: dict
    profile_name: str
    cache_state: str
    status: str = "ok"
    operations: tuple | None = None


def _rebase(paths, after_root: Path, side_root: Path, per_side: bool = False):
    """Paths of the after tree mapped to ``side_root``.  A relative path inside the after tree (as seen from
    the working directory) is rebased; with ``per_side`` (``--before``/``--after``) any other relative path
    that exists under the side's own root is read from that root, so both sides use their own copy."""
    out = []
    for p in paths or ():
        ap = Path(p)
        if ap.is_absolute():
            full = ap
        else:
            full = Path.cwd() / ap
            if per_side and not _within(full, after_root) and (side_root / ap).exists():
                out.append(str(side_root / ap))
                continue
        try:
            rel = Path(os.path.abspath(full)).relative_to(Path(os.path.abspath(after_root)))
            out.append(str(side_root / rel))
        except ValueError:
            out.append(str(p))
    return out


def _within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        return True
    except ValueError:
        return False


def _side_settings(settings, after_root: Path, side_root: Path, per_side: bool = False):
    s = copy.copy(settings)
    s.root = side_root
    s.incdirs = _rebase(settings.incdirs, after_root, side_root, per_side)
    s.filelists = _rebase(settings.filelists, after_root, side_root, per_side)
    return s


def _overlay(before_root: Path, after_root: Path, settings, outside: set) -> tuple[Path, callable]:
    """Before-view in which files outside the PATH restriction equal their after content."""
    from .baseline import _DirTree, select_sources

    tmp = Path(tempfile.mkdtemp(prefix="gatecost-view-")).resolve()
    rels = set(select_sources(_DirTree(before_root), _side_settings(settings, after_root, before_root)))
    for r in rels:
        if r in outside:
            continue
        dst = tmp / r
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(before_root / r, dst)
    for r in outside:
        src = after_root / r
        if src.is_file():
            dst = tmp / r
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    return tmp, lambda: shutil.rmtree(tmp, ignore_errors=True)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def side_configs(args, settings, cfg, bl):
    """``(settings, cfg)`` of the after and the before side, and diagnostics.

    One project file normally serves both sides.  When the file differs between the sides it is read from each
    side's own tree: the file as of the base reference / snapshot (git, snapshot), or the ``gatecost.toml`` of
    each directory (``--before``/``--after`` without ``--config``)."""
    from .baseline import CONFIG_NAME, _config_rel
    from .config import load_project_file, settings_for

    diags: list[Diagnostic] = []
    after = before = (settings, cfg)

    def load(path: Path):
        try:
            return load_project_file(path)
        except GatecostError as e:
            diags.append(Diagnostic("CONFIG_MISMATCH", f"cannot read the project file of the baseline ({e.message}); "
                                    "the current one is used for both sides", "warning", True, "before"))
            return None

    if bl.kind == "dirs":
        if not getattr(args, "config", None):
            def own(root: Path):
                f = Path(root) / CONFIG_NAME
                if not f.is_file():
                    return None
                c = load(f)
                return None if c is None else (settings_for(args, Path(root), f, c), c)

            after = own(Path(bl.after_root)) or after
            before = own(Path(bl.before_root)) or before
        return after, before, diags
    rel = _config_rel(settings)
    if rel is None:
        return after, before, diags
    f_before = Path(bl.before_root) / rel
    f_after = Path(settings.config_path) if settings.config_path else Path(settings.root) / rel
    raw_b, raw_a = _read_bytes(f_before), _read_bytes(f_after)
    if raw_b == raw_a:
        return after, before, diags
    cfg_b = load(f_before) if raw_b is not None else ProjectConfig()
    if cfg_b is not None:
        before = (settings_for(args, Path(settings.root), f_after, cfg_b), cfg_b)
    return after, before, diags


def config_differences(sa, sb) -> list[str]:
    """Keys of the project configuration (resolved settings) that differ between the sides."""
    out: list[str] = []
    if sa.top is not None and sb.top is not None and sa.top != sb.top:
        out.append("top")      # a top pinned on one side only is reported by TOP_CHANGED when it really moves
    for k in sorted(set(sa.defines) | set(sb.defines)):
        if sa.defines.get(k) != sb.defines.get(k):
            out.append(f"defines.{k}")
    out += [f"params.{k}" for k in params_differing(sa.params, sb.params)]
    for key in ("sources", "incdirs", "filelists", "exclude"):
        if list(getattr(sa, key)) != list(getattr(sb, key)):
            out.append(key)
    return out


def run_diff(args, settings, cfg, profile, collect_sources, elaborate=None) -> DiffRun:
    from .baseline import resolve_baseline
    from .cache import resolve_cache
    from .diff import diff_trees
    from .result import DiffResult  # noqa: F401

    t0 = time.perf_counter()
    cache = resolve_cache(settings)
    diags: list[Diagnostic] = list(settings.diagnostics) + list(profile.diagnostics)
    bl = resolve_baseline(settings, args)
    cleanups = [bl.cleanup]
    try:
        diags += list(bl.diagnostics)
        (set_a, cfg_a), (set_b, cfg_b), cdiags = side_configs(args, settings, cfg, bl)
        diags += cdiags
        per_side = bl.kind == "dirs"

        def side_values(st_, cf_):
            """Defines and parameters of one side, and its limits (a snapshot supplies those recorded)."""
            d = dict(st_.defines)
            if bl.kind == "snapshot" and bl.defines is not None and not (getattr(args, "define", None) or cf_.get("project", "defines")):
                d = dict(bl.defines)
            p = dict(st_.params)
            if bl.kind == "snapshot" and bl.params is not None and not (getattr(args, "param", None) or cf_.get("project", "params")):
                p = dict(bl.params)
            lim = dict(cf_.data.get("limits", {})) if getattr(cf_, "data", None) else {}
            return d, p, lim

        defines, params, limits = side_values(set_a, cfg_a)
        defines_b, params_b, limits_b = side_values(set_b, cfg_b)
        set_a = copy.copy(set_a)
        set_a.params = params
        set_b = copy.copy(set_b)
        set_b.params = params_b
        set_b.defines = defines_b
        cfg_diff = config_differences(replace_defines(set_a, defines), set_b)
        if limits != limits_b:
            cfg_diff.append("limits")
        if (bl.kind == "snapshot" and bl.profile and bl.profile != settings.profile
                and getattr(args, "profile", None) is None and cfg.get("cost", "profile") is None
                and not os.environ.get("GATECOST_PROFILE")):
            from .cost import load_profile

            profile = load_profile(bl.profile, cfg.get("cost", "override", None) or None, Path.cwd())
        st = copy.copy(set_a)
        st.defines = defines
        after_root = Path(bl.after_root).resolve()
        before_root = Path(bl.before_root).resolve()
        none_args = SimpleNamespace(paths=[])
        files_a, inc_a, defs_a, fd_a = collect_sources(none_args, _side_settings(st, after_root, after_root, per_side), cfg_a)
        diags += list(fd_a)

        # PATH restriction: outside changes are neutralised in the before view
        outside = set(bl.all_changed_files) - set(bl.changed_files)
        if outside and bl.changed_files is not None:
            before_root, cl = _overlay(before_root, after_root, set_b, outside)
            cleanups.append(cl)
        changed = bool(bl.changed_files) or bool(cfg_diff)

        inp_a = read_inputs(files_a, inc_a, defs_a, after_root, params)
        top = set_a.top
        top_b = set_b.top
        prefer = set(bl.all_changed_files)
        before_ok = True
        reason = ""
        srb = sra = None
        inp_b = inp_a
        if not changed:
            sra = srb = analyze_side(inp_a, profile, top, limits, cache, elaborate=elaborate, side="after", prefer=prefer)
        else:
            try:
                files_b, inc_b, defs_b, fd_b = collect_sources(
                    none_args, _side_settings(set_b, after_root, before_root, per_side), cfg_b)
                inp_b = read_inputs(files_b, inc_b, defs_b, before_root, params_b)
                srb = analyze_side(inp_b, profile, top_b, limits_b, cache, elaborate=elaborate, side="before", prefer=prefer)
            except GatecostError as e:
                if e.code in ("NO_SOURCES", "TOP_NOT_FOUND"):
                    before_ok = False
                    reason = ("baseline has no RTL sources" if e.code == "NO_SOURCES"
                              else f"baseline has no module named {top_b} (top changed?)")
                else:
                    raise
            if before_ok:
                top_a = top
                if top is None and top_b is None and len(srb.tops) > 1:
                    top_a = _top_name(srb.tree)       # several roots: stay on the same one when it still exists
                names = cone_names(bl.changed_files, inp_b.texts, inp_a.texts) if cache is not None and not cfg_diff and top == top_b else None
                if names and not os.environ.get("GATECOST_NO_CONE"):
                    sra = analyze_cone(inp_a, profile, top, limits, cache, srb, names,
                                       side_key(inp_b, profile, top_b, limits_b, prefer), elaborate=elaborate,
                                       changed=bl.changed_files)
                if sra is None:
                    try:
                        sra = analyze_side(inp_a, profile, top_a, limits, cache, elaborate=elaborate, side="after", prefer=prefer)
                    except GatecostError as e:
                        if top_a != top and e.code == "TOP_NOT_FOUND":
                            sra = analyze_side(inp_a, profile, top, limits, cache, elaborate=elaborate, side="after", prefer=prefer)
                        else:
                            raise
        if not before_ok:
            sra = analyze_side(inp_a, profile, top, limits, cache, elaborate=elaborate, side="after", prefer=prefer)
        if not before_ok:
            from .diff.engine import not_comparable

            res = not_comparable(reason, _top_name(sra.tree))
            return DiffRun(res, diags, _cfg_out(sra.tree, defines, inp_a), profile.name, cache.state, "ok")
        # source texts for the line alignment (unchanged files are identical on both sides)
        from .diff.engine import _config_diffs

        covered = {k.split(".")[0] for k in cfg_diff}
        mismatch = cfg_diff + [k for k in _config_diffs(srb.tree, sra.tree) if k not in covered]
        res = diff_trees(srb.tree, sra.tree, before_sources=inp_b.texts, after_sources=inp_a.texts,
                         threshold_ge=settings.threshold_ge, config_mismatch=mismatch)
        res = _merge_assumed(res, srb, sra)
        ops = None
        if getattr(args, "detail", None) == "ops":
            ops = _changed_ops(res, sra.tree)
        run = DiffRun(res, diags, _cfg_out(sra.tree, defines, inp_a), profile.name, cache.state, "ok", ops)
        run.after_tree = sra.tree  # type: ignore[attr-defined]
        run.elapsed_ms = round((time.perf_counter() - t0) * 1000)  # type: ignore[attr-defined]
        return run
    finally:
        for c in reversed(cleanups):
            try:
                c()
            except OSError:
                pass


def replace_defines(settings, defines):
    s = copy.copy(settings)
    s.defines = defines
    return s


def _merge_assumed(res, srb, sra):
    a = list(dict.fromkeys(list(sra.assumptions) + [x for x in srb.assumptions if x not in sra.assumptions]))
    return replace(res, assumed=tuple(a))


def _top_name(tree: DesignCostTree) -> str | None:
    m = tree.modules.get(tree.top_key)
    return m.name if m else None


def _cfg_out(tree, defines, inp) -> dict:
    return {"top": _top_name(tree), "defines": dict(defines), "params": params_json(inp.params),
            "sources": len(inp.files)}


def _changed_ops(res, tree) -> tuple:
    out = []
    mods = {b["module"] for b in res.by_module}
    from .cli.cmd_cost import operations_detail

    for d in operations_detail(tree):
        if d["module"] in mods:
            out.append(d)
    return tuple(out)
