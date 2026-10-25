"""Command implementations. Analysis commands arrive in later stages."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .. import REPO_ROOT
from ..config import Settings
from ..discovery import VERILOG_EXT, VHDL_EXT, discover
from ..errors import GatecostError
from ..paths import state_dir, user_cache_dir
from ..result import MessageResult

INIT_TEMPLATE = """\
# gatecost project file. Every key is optional; command-line options override it.
[project]
# top = "top_core"                 # auto-detected if absent
{sources}
{incdirs}
# filelists = ["rtl/files.f"]
# exclude = ["rtl/legacy/**"]      # added to the built-in exclusions
# defines = {{ SYNTHESIS = "1" }}
# params = {{ DATA_WIDTH = 8 }}      # parameters / generics of the top
# languages = ["verilog", "vhdl"]

# [project.libraries]              # VHDL library mapping (default: all in "work")
# core = ["rtl/core/*.vhd"]

# [project.architectures]          # VHDL architecture per entity when several exist
# cpu = "rtl"

[cost]
# profile = "generic-asic"

# [cost.override.policy]
# enable_style = "mux"
# fsm_encoding = "binary"

[budget]
# max_ge = 50000
# max_delta_ge = 500
# basis = "typ"

[report]
# max_lines = 10
# threshold_ge = 5
# format = "agent"

[limits]
# generate_iterations = 4096
# loop_unroll = 1024
# inline_depth = 32
# max_instances = 100000

[cache]
# enabled = true
# dir = ""
"""


def not_implemented(command: str) -> GatecostError:
    return GatecostError("NOT_IMPLEMENTED", f"{command} is not implemented yet")


def cmd_init(root: Path, force: bool, skill_dir: Path) -> MessageResult:
    target = root / "gatecost.toml"
    if target.exists() and not force:
        raise GatecostError("BAD_ARGUMENT", "gatecost.toml already exists (use --force to overwrite)")
    rtl, _ = discover(root, excludes=[], skill_dir=skill_dir)
    exts = sorted({p.suffix.lower() for p in rtl})
    globs = [f"**/*{e}" for e in exts if e in VERILOG_EXT | VHDL_EXT and e not in (".vh", ".svh")]
    sources = "sources = [" + ", ".join(f'"{g}"' for g in globs) + "]" if globs else '# sources = ["rtl/**/*.sv"]'
    inc = sorted({p.parent.as_posix() for p in rtl if p.suffix.lower() in (".vh", ".svh")})
    incdirs = "incdirs = [" + ", ".join(f'"{d}"' for d in inc) + "]" if inc else '# incdirs = ["rtl/include"]'
    target.write_text(INIT_TEMPLATE.format(sources=sources, incdirs=incdirs), encoding="utf-8")
    return MessageResult((f"INIT wrote gatecost.toml sources={len(rtl)}",), {"path": "gatecost.toml"})


def cmd_clean(settings_root: Path, global_: bool, snapshots: bool) -> MessageResult:
    removed: list[str] = []
    st = state_dir(settings_root)
    targets = [st / "cache"] + ([st / "snapshots"] if snapshots else [])
    if global_:
        targets.append(user_cache_dir())
    for t in targets:
        if t.exists():
            shutil.rmtree(t, ignore_errors=True)
            removed.append(t.name)
    return MessageResult((f"CLEAN removed={len(removed)}",), {"removed": removed})


def cmd_validate(file: str) -> MessageResult:
    from ..report.validate import validate_json

    import sys

    try:
        text = sys.stdin.read() if file == "-" else Path(file).read_text(encoding="utf-8")
        doc = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise GatecostError("BAD_ARGUMENT", f"cannot read JSON from {file}: {exc}") from None
    errors = validate_json(doc)
    if errors:
        raise _Invalid(errors)
    return MessageResult(("OK",))


class _Invalid(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
