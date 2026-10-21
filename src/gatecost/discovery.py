"""Source discovery in the host project (DISTRIBUTION.md section 4)."""
from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

VERILOG_EXT = {".v", ".vh", ".sv", ".svh"}
VHDL_EXT = {".vhd", ".vhdl"}
RTL_EXT = VERILOG_EXT | VHDL_EXT
ALWAYS_EXCLUDED = {".git", ".gatecost", ".claude", ".opencode", "node_modules", "__pycache__"}


def language_of(path: str | Path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in VERILOG_EXT:
        return "verilog"
    if ext in VHDL_EXT:
        return "vhdl"
    return None


def _skip_dir(rel: Path, name: str, excludes: list[str], skill_dir: Path, full: Path) -> bool:
    if name in ALWAYS_EXCLUDED or full.resolve() == skill_dir.resolve():
        return True
    if rel.as_posix() == ".github/skills":
        return True
    return any(fnmatch(rel.as_posix(), pat) or fnmatch(name, pat) for pat in excludes)


def discover(root: Path, *, excludes: list[str], skill_dir: Path, globs: list[str] | None = None, languages: tuple[str, ...] = ("verilog", "vhdl")) -> tuple[list[Path], list[Path]]:
    """Return (rtl files, filelists) sorted, as paths relative to `root` with forward slashes."""
    root = root.resolve()
    rtl: list[Path] = []
    flists: list[Path] = []
    if globs:
        for pattern in globs:
            for p in sorted(root.glob(pattern)):
                if p.is_file() and language_of(p) in languages:
                    rtl.append(p.relative_to(root))
        return sorted(set(rtl)), flists
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_here = here.relative_to(root)
        dirnames[:] = sorted(
            d for d in dirnames if not _skip_dir(rel_here / d, d, excludes, skill_dir, here / d)
        )
        for fn in sorted(filenames):
            rel = rel_here / fn
            if language_of(fn) in languages:
                rtl.append(rel)
            elif fn.endswith(".f"):
                flists.append(rel)
    return rtl, flists
