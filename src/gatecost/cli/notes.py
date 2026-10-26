"""Reliability notes added by the CLI: duplicate modules found by implicit discovery, and the wording of the
confidence reason when ``DUPLICATE_MODULE`` diagnostics are what lowers it."""
from __future__ import annotations

import os
import re
from dataclasses import replace

from ..result import Confidence, Diagnostic

MAX_NAMES = 4
MAX_FILES = 3
_NAME = re.compile(r"'([^']+)'")
_PREV = re.compile(r"previous definition at (.+?):\d+\)")
_UNSUPPORTED = re.compile(r"^(\d+) unsupported constructs? in the changed cone")


def implicit_discovery(args, settings) -> bool:
    """True when the source set comes from walking the project (no path, filelist or ``sources`` globs)."""
    return not (getattr(args, "paths", None) or settings.filelists or settings.sources)


def _cap(items: list[str], n: int) -> str:
    return ", ".join(items[:n]) + (f", +{len(items) - n} more" if len(items) > n else "")


def discovery_duplicates(diags, implicit: bool) -> Diagnostic | None:
    """DISCOVERY_DUPLICATES warning naming the duplicated modules, their files and the retained one."""
    dups = [d for d in diags if d.code == "DUPLICATE_MODULE"]
    if not implicit or not dups:
        return None
    names: list[str] = []
    files: list[str] = []
    kept: list[str] = []
    for d in dups:
        m = _NAME.search(d.message)
        label = f"{d.message.split(' ', 1)[0]} {m.group(0)}" if m else "module"
        if label not in names:
            names.append(label)
        for f in [*(x.group(1) for x in [_PREV.search(d.message)] if x), d.file]:
            if f and f not in files:
                files.append(f)
        if d.file and d.file not in kept:
            kept.append(d.file)
    first = dups[0]
    msg = (f"implicit discovery mixes variants: {len(dups)} duplicate definition{'s' if len(dups) != 1 else ''} "
           f"({_cap(names, MAX_NAMES)}) across {_cap(sorted(files), MAX_FILES)}; last wins, kept {_cap(kept, MAX_FILES)}; "
           "pass a path, --top or --filelist")
    return Diagnostic("DISCOVERY_DUPLICATES", msg, "warning", True, "after", first.file, first.line, first.column)


def with_discovery_warning(diags, implicit: bool) -> list[Diagnostic]:
    """``diags`` with the discovery warning put first (it explains the DUPLICATE_MODULE lines that follow)."""
    w = discovery_duplicates(diags, implicit)
    return [w, *diags] if w is not None else list(diags)


def explain_confidence(conf: Confidence, diags) -> Confidence:
    """Name DUPLICATE_MODULE in the 'unsupported constructs' reason when duplicates are among the causes."""
    k = len({(d.message, d.file, d.line) for d in diags if d.code == "DUPLICATE_MODULE"})
    if not k or conf.level == "high":
        return conf
    out = []
    for r in conf.reasons:
        m = _UNSUPPORTED.match(r)
        if m is None:
            out.append(r)
            continue
        n = int(m.group(1))
        if k >= n:
            r = (f"{n} DUPLICATE_MODULE diagnostic{'s' if n != 1 else ''} (module defined in several files; "
                 "the last definition wins, the design may mix variants)")
        else:
            r = f"{r}, including {k} DUPLICATE_MODULE"
        out.append(r)
    return replace(conf, reasons=tuple(out))


_HEADER_EXT = (".vh", ".svh", ".h", ".inc")


def cone_filter(tree, diags) -> tuple[list[Diagnostic], int]:
    """``(diags without the cost-neutral notes of files outside the top's cone, how many were dropped)``.

    Parsing every discovered file reports e.g. ``IGNORED_REGION`` for modules the top never instantiates (hundreds
    on a large repository).  A diagnostic is dropped only when it cannot change the cost (``affects_cost`` false)
    and its file is a source file that hosts no module of the cone; headers and diagnostics without a file are
    kept, and so is every cost-affecting diagnostic (``UNSUPPORTED_CONSTRUCT``, parse errors, ...)."""
    cone_files = {os.path.normpath(m.file) for k, m in tree.modules.items() if m.file and tree.instance_counts.get(k, 0)}
    if not cone_files:
        return list(diags), 0
    kept: list[Diagnostic] = []
    dropped = 0
    for d in diags:
        if (d.file and not d.affects_cost and not d.file.lower().endswith(_HEADER_EXT)
                and os.path.normpath(d.file) not in cone_files):
            dropped += 1
        else:
            kept.append(d)
    return kept, dropped


def cost_wording(conf: Confidence) -> Confidence:
    """``cost`` has no changed cone: the unsupported-constructs reason speaks of the analyzed design."""
    return replace(conf, reasons=tuple(r.replace(" in the changed cone", " in the analyzed design", 1)
                                       if _UNSUPPORTED.match(r) else r for r in conf.reasons))


_SCALAR_TO_ARRAY = re.compile(r"^generic '([^']+)' skipped \(conversion of a scalar to an array type\)")


def string_param_hints(diags, params) -> list[Diagnostic]:
    """Make the elaborator's 'conversion of a scalar to an array type' actionable when the generic was set with
    ``--param``: a bare ``2`` is a number, a VHDL ``string`` generic needs the quoted form (CLI.md 2.1.1)."""
    names = {str(k).lower(): str(k) for k in (params or {})}
    out: list[Diagnostic] = []
    for d in diags:
        m = _SCALAR_TO_ARRAY.match(d.message) if d.code == "UNSUPPORTED_CONSTRUCT" else None
        if m is not None and m.group(1).lower() in names:
            n = names[m.group(1).lower()]
            d = replace(d, message=f"{d.message}; --param {n} was given an unquoted scalar but the generic is a string or array: "
                                   f"quote the value, e.g. --param {n}='\"2\"' (CLI.md 2.1.1)")
        out.append(d)
    return out
