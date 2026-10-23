"""Line alignment between two revisions of the sources (DIFF.md sections 3.4.3 and 6).

Lines are token-normalized (comments and whitespace stripped, blank lines dropped from the
sequence) and aligned with ``difflib.SequenceMatcher``. Alignments are computed lazily per
file and memoized, so modules whose operations all match in the key tiers cost nothing.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from difflib import SequenceMatcher
from posixpath import basename

_TOKEN = re.compile(r"[A-Za-z_$`][\w$']*|\d[\w']*|\S")


def normalize_lines(text: str, vhdl: bool = False) -> list[str]:
    """One normalized string per source line ('' for blank or comment-only lines)."""
    out: list[str] = []
    in_block = False
    for raw in text.splitlines():
        buf: list[str] = []
        i, n = 0, len(raw)
        in_str = False
        while i < n:
            c = raw[i]
            if in_block:
                j = raw.find("*/", i)
                if j < 0:
                    i = n
                else:
                    in_block = False
                    i = j + 2
                continue
            if in_str:
                buf.append(c)
                if c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
                buf.append(c)
                i += 1
                continue
            if vhdl:
                if raw.startswith("--", i):
                    break
            else:
                if raw.startswith("//", i):
                    break
                if raw.startswith("/*", i):
                    in_block = True
                    i += 2
                    continue
            buf.append(c)
            i += 1
        out.append(" ".join(_TOKEN.findall("".join(buf))))
    return out


class FileAlign:
    """Alignment of one before file against one after file (1-based line numbers)."""

    __slots__ = ("eq", "repl", "after_equal", "_eq_sorted", "nafter")

    def __init__(self, before_text: str, after_text: str, vhdl: bool):
        a = normalize_lines(before_text, vhdl)
        b = normalize_lines(after_text, vhdl)
        self.nafter = len(b)
        ia = [i for i, s in enumerate(a) if s]
        ib = [j for j, s in enumerate(b) if s]
        sm = SequenceMatcher(None, [a[i] for i in ia], [b[j] for j in ib], autojunk=False)
        self.eq: dict[int, int] = {}
        self.repl: dict[int, tuple[int, int, int]] = {}
        self.after_equal: set[int] = set()
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    self.eq[ia[i1 + k] + 1] = ib[j1 + k] + 1
                    self.after_equal.add(ib[j1 + k] + 1)
            elif tag == "replace":
                lo, hi = ib[j1] + 1, ib[j2 - 1] + 1
                span_a, span_b = i2 - i1, j2 - j1
                for k in range(span_a):
                    pt = ib[j1 + (k * span_b) // span_a] + 1
                    self.repl[ia[i1 + k] + 1] = (lo, hi, pt)
        self._eq_sorted = sorted(self.eq)

    def map_line(self, line: int) -> tuple[int, int, int] | None:
        """(lo, hi, point) of after lines corresponding to a before line, or None."""
        e = self.eq.get(line)
        if e is not None:
            return (e, e, e)
        return self.repl.get(line)

    def after_line_changed(self, line: int) -> bool:
        """True when an after line is code that has no equal counterpart in the before file."""
        if line in self.after_equal:
            return False
        return True

    def nearest_surviving(self, line: int, window: int = 3) -> int | None:
        """After line of the closest surviving before line within ``window`` lines."""
        best: tuple[int, int, int] | None = None
        for k in range(1, window + 1):
            for cand in (line - k, line + k):
                if cand in self.eq:
                    best = (k, 0 if cand < line else 1, self.eq[cand])
                    return best[2]
        return None


class Aligner:
    """File-level front for ``FileAlign`` with the fallbacks used by the differ."""

    def __init__(self, before_sources: Mapping[str, str] | None, after_sources: Mapping[str, str] | None):
        self.bs = before_sources
        self.as_ = after_sources
        self.identity = before_sources is None or after_sources is None
        self._cache: dict[str, tuple[str, FileAlign] | None] = {}
        self._base: dict[str, list[str]] | None = None

    def _after_name(self, bfile: str) -> str | None:
        assert self.as_ is not None
        if bfile in self.as_:
            return bfile
        if self._base is None:
            self._base = {}
            for f in self.as_:
                self._base.setdefault(basename(f), []).append(f)
        c = self._base.get(basename(bfile), [])
        return c[0] if len(c) == 1 else None

    def file_align(self, bfile: str) -> tuple[str, FileAlign] | None:
        if self.identity:
            return None
        if bfile in self._cache:
            return self._cache[bfile]
        res = None
        if bfile in self.bs:  # type: ignore[operator]
            af = self._after_name(bfile)
            if af is not None:
                vhdl = af.lower().endswith((".vhd", ".vhdl"))
                res = (af, FileAlign(self.bs[bfile], self.as_[af], vhdl))  # type: ignore[index]
        self._cache[bfile] = res
        return res

    def map_line(self, bfile: str, line: int) -> tuple[str, int, int, int] | None:
        """(after file, lo, hi, point) for a before location, or None when it has no counterpart."""
        if self.identity:
            return (bfile, line, line, line)
        fa = self.file_align(bfile)
        if fa is None:
            return None
        m = fa[1].map_line(line)
        return None if m is None else (fa[0], *m)

    def nearest_surviving(self, bfile: str, line: int) -> tuple[str, int] | None:
        if self.identity:
            return None
        fa = self.file_align(bfile)
        if fa is None:
            return None
        e = fa[1].nearest_surviving(line)
        return None if e is None else (fa[0], e)

    def before_line_changed(self, bfile: str, line: int) -> bool:
        """Whether a before location lies on a line without an equal counterpart (unknown: True)."""
        fa = self.file_align(bfile)
        return True if fa is None else line not in fa[1].eq

    def after_line_changed(self, afile: str, line: int) -> bool:
        """Whether an after location lies on a changed line (unknown without sources: True)."""
        if self.identity or self.as_ is None or afile not in self.as_:
            return True
        # find the before file that aligns to this after file
        bfile = afile if afile in (self.bs or {}) else None
        if bfile is None:
            base = basename(afile)
            c = [f for f in (self.bs or {}) if basename(f) == base]
            if len(c) != 1:
                return True  # a new file: all its lines are new
            bfile = c[0]
        fa = self.file_align(bfile)
        if fa is None:
            return True
        return fa[1].after_line_changed(line)
