"""Source discovery in the host project (DISTRIBUTION.md section 4)."""
from __future__ import annotations

import os
import re
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


_VHDL_UNIT_HEAD = {
    "library": re.compile(r"library\s+\w+(?:\s*,\s*\w+)*\s*;", re.I),
    "use": re.compile(r"use\s+[\w.]+(?:\.all)?\s*;", re.I),
    "entity": re.compile(r"entity\s+\w+\s+is\b", re.I),
    "architecture": re.compile(r"architecture\s+\w+\s+of\s+\w+\s+is\b", re.I),
    "package": re.compile(r"package\s+(?:body\s+)?\w+\s+is\b", re.I),
    "context": re.compile(r"context\s+\w+\s+is\b", re.I),
    "configuration": re.compile(r"configuration\s+\w+\s+of\s+\w+\s+is\b", re.I),
}
_FIRST_WORD = re.compile(r"[A-Za-z_]\w*")


def looks_like_vhdl(text: str) -> bool:
    """True when the first non-comment token of ``text`` starts a VHDL design unit or context clause
    (``library``/``use``/``entity``/``architecture``/``package ... is``/...), as opposed to ``module``,
    a compiler directive or an SV ``package x;``.  Comments (``--``, ``//``, ``/* */``) are skipped."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace() or c == "\ufeff":
            i += 1
        elif text.startswith("--", i) or text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    m = _FIRST_WORD.match(text, i)
    if m is None:
        return False
    head = _VHDL_UNIT_HEAD.get(m.group(0).lower())
    return head is not None and head.match(text, i) is not None


def sniff_is_vhdl(path: str | Path) -> bool:
    """``looks_like_vhdl`` on the beginning of a file (the leading comment block may be long: it is read in
    growing chunks until a token appears)."""
    size = 8192
    try:
        with open(path, "rb") as fh:
            while True:
                fh.seek(0)
                raw = fh.read(size)
                text = raw.decode("utf-8", "replace")
                if looks_like_vhdl(text):
                    return True
                if len(raw) < size or size >= 1 << 20 or not _only_comments(text):
                    return False
                size *= 8
    except OSError:
        return False


def _only_comments(text: str) -> bool:
    """True when ``text`` (a prefix of a file) ends inside the leading comment block, so a larger prefix is needed."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace() or c == "\ufeff":
            i += 1
        elif text.startswith("--", i) or text.startswith("//", i):
            j = text.find("\n", i)
            if j < 0:
                return True
            i = j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                return True
            i = j + 2
        else:
            return False
    return True


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
                    rel = p.relative_to(root)
                    dirs = [Path(*rel.parts[:i + 1]) for i in range(len(rel.parts) - 1)]
                    if not any(_skip_dir(d, d.name, excludes, skill_dir, root / d) for d in dirs):
                        rtl.append(rel)
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
