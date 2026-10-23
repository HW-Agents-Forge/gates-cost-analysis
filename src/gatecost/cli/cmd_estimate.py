"""``estimate`` and ``profiles`` commands (CLI.md sections 2.6 and 3)."""
from __future__ import annotations

import re
from pathlib import Path

from .. import estimate as est
from ..cost import Profile, dump_profile, list_profiles, load_profile
from ..errors import GatecostError
from ..report import render
from ..report.json_fmt import render_json
from ..result import MessageResult, Output


def _render(out: Output, settings) -> str:
    if settings.format == "json":
        return render_json(out, include_meta=not settings.no_meta)
    return render(out, settings.format, max_lines=settings.max_lines, verbose=settings.verbose, quiet=settings.quiet)


def _load(settings, cfg) -> Profile:
    overrides = cfg.get("cost", "override", None) or None
    base = Path.cwd()
    return load_profile(settings.profile, overrides, base)


def _profile_info(p: Profile) -> dict:
    return {"name": p.name, "nand2_area_um2": p.nand2_area_um2}


def parse_widths(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not text:
        return out
    for item in text.split(","):
        m = re.fullmatch(r"\s*([A-Za-z_]\w*)\s*=\s*(\d+)\s*", item)
        if not m or int(m.group(2)) < 1:
            raise GatecostError("BAD_ARGUMENT", f"--widths expects NAME=WIDTH[,NAME=WIDTH...], got {item.strip()!r}")
        out[m.group(1)] = int(m.group(2))
    return out


def run_profiles(args, settings) -> str:
    if args.action == "show":
        p = load_profile(args.name, None, Path.cwd())
        text = dump_profile(p)
        res = MessageResult(tuple(text.rstrip("\n").split("\n")), {"profile": p.raw})
        return _render(Output(command="profiles", profile=_profile_info(p), result=res, diagnostics=p.diagnostics), settings)
    rows = list_profiles()
    lines = tuple(f"PROFILE {n} {d}".rstrip() for n, d in rows)
    res = MessageResult(lines, {"profiles": [{"name": n, "description": d} for n, d in rows]})
    return _render(Output(command="profiles", result=res), settings)


def run_estimate(args, settings, cfg) -> str:
    widths = parse_widths(args.widths)
    prof = _load(settings, cfg)
    count = args.count if args.count is not None else 1
    if count < 1:
        raise GatecostError("BAD_ARGUMENT", "--count must be >= 1")
    lang = args.lang or "verilog"
    if args.rtl or args.rtl_file:
        if args.rtl_file:
            try:
                fragment = Path(args.rtl_file).read_text(encoding="utf-8")
            except OSError as exc:
                raise GatecostError("BAD_ARGUMENT", f"cannot read {args.rtl_file}: {exc.strerror}") from None
        else:
            fragment = args.rtl
        result = est.estimate_rtl(fragment, widths, lang, prof)
        if count > 1:
            from dataclasses import replace

            from ..result import Ge3

            g = result.ge
            result = replace(result, ge=Ge3(g.min * count, g.typ * count, g.max * count),
                             breakdown={k: v * count for k, v in result.breakdown.items()},
                             normalized=f"{count} * ({result.normalized})")
    elif args.compare:
        result = est.compare_expressions(args.compare, prof, count)
    else:
        result = est.estimate_expression(args.expression, prof, count)
    diags = tuple(prof.diagnostics) + tuple(settings.diagnostics)
    out = Output(command="estimate", profile=_profile_info(prof), result=result, diagnostics=diags)
    return _render(out, settings)


def run(args, settings, cfg) -> str:
    """Render the result of ``estimate`` or ``profiles`` (raises GatecostError on failure)."""
    if args.command == "profiles":
        return run_profiles(args, settings)
    return run_estimate(args, settings, cfg)
