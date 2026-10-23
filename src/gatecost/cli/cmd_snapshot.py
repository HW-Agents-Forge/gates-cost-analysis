"""`snapshot` command (CLI.md section 2.4)."""
from __future__ import annotations

import time
from pathlib import Path

from .. import snapshot as snap
from ..errors import GatecostError
from ..report import render
from ..report.json_fmt import render_json
from ..result import MessageResult, Output, SnapshotResult


def _render(out: Output, settings) -> str:
    if settings.format == "json":
        return render_json(out, include_meta=not settings.no_meta)
    return render(out, settings.format, max_lines=settings.max_lines, verbose=settings.verbose, quiet=settings.quiet)


def run(args, settings, cfg) -> str:
    root = Path(settings.root)
    profile = {"name": settings.profile}
    diags = tuple(settings.diagnostics)
    if args.list:
        snaps = snap.list_snapshots(root)
        lines = tuple(
            f"SNAPSHOT {s.id} files={len(s.files)} modules={s.modules} created={time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(s.created_ns / 1e9))}"
            for s in snaps
        ) or ("SNAPSHOT none",)
        data = {"snapshots": [{"id": s.id, "files": len(s.files), "modules": s.modules, "created_ns": s.created_ns} for s in snaps]}
        return _render(Output(command="snapshot", profile=profile, result=MessageResult(lines, data), diagnostics=diags), settings)
    if args.delete is not None or args.all:
        if args.delete is None and not args.all:
            raise GatecostError("BAD_ARGUMENT", "--delete needs an ID or --all")
        removed = snap.delete(root, args.delete, args.all)
        res = MessageResult((f"SNAPSHOT deleted={len(removed)}" + (f" id={','.join(removed)}" if removed else ""),), {"deleted": removed})
        return _render(Output(command="snapshot", profile=profile, result=res, diagnostics=diags), settings)
    info, d = snap.create(settings, args.paths, args.keep)
    res = SnapshotResult(info.id, len(info.files), info.modules)
    return _render(Output(command="snapshot", profile=profile, result=res, diagnostics=diags + tuple(d)), settings)
