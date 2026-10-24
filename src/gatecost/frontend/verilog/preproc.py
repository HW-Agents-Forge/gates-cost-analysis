"""Verilog/SystemVerilog preprocessor (FRONTEND.md section 2.2).

Input: a :class:`SourceFile`.  Output: a :class:`PreprocResult` holding the preprocessed text and
a :class:`LineMap` from output lines back to ``(file, line)`` of the original sources.

Design notes
------------
* The output keeps *one output line per source line*: removed text (directives, excluded regions,
  macro definitions) is replaced by the same number of newlines, and a macro expansion is placed on
  the line of its use site (continuation lines consumed by macro arguments are re-emitted as
  newlines after the expansion).  Therefore locations of macro-expanded code are attributed to the
  use site, and the macro definition is available as secondary information in
  ``PreprocResult.macro_uses`` (``{output line: [(macro, def_file, def_line), ...]}``).
  Columns are exact up to the first expansion on a line and approximate after it.
* Comments are left in the output (the lexer skips them) except synthesis pragma comments:
  ``translate_off``/``translate_on`` regions are excluded, and ``// synopsys full_case
  parallel_case`` comments are turned into an attribute ``(* full_case, parallel_case *)``.
* Files without a backtick and without pragma-like words take a fast path and are returned as is.
"""
from __future__ import annotations

import os
import re
from bisect import bisect_right
from dataclasses import dataclass, field

from gatecost.frontend.common.diagnostics import DiagSink
from gatecost.frontend.common.reader import SourceFile, read_source, resolve_include
from .limits import Limits

_IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"

_FILE_RE = re.compile(
    r"""(?P<lc>//[^\n]*)
      | (?P<bc>/\*.*?(?:\*/|\Z))
      | (?P<str>"(?:[^"\\\n]|\\.)*(?:"|(?=\n)|\Z))
      | `(?P<tick>""" + _IDENT + r""")
      | (?P<esc>\\\S+)""",
    re.S | re.X,
)
_TICKSTR_RE = re.compile(r'(?P<str>"(?:[^"\\\n]|\\.)*")|`(?P<tick>' + _IDENT + ")", re.S)
_QUICK_RE = re.compile(r"synopsys|synthesis|pragma|cadence|translate_|exemplar", re.I)
_TRANSLATE_RE = re.compile(
    r"\b(?:synopsys|synthesis|pragma|cadence|rtl_synthesis|exemplar)\s+(?:translate|synthesis)_(off|on)\b", re.I)
_FULLPAR_RE = re.compile(
    r"\b(?:synopsys|synthesis|pragma)\s+((?:full_case|parallel_case)(?:\s+(?:full_case|parallel_case))*)\s*$", re.I)
_IDENT_AFTER = re.compile(r"[ \t]*(" + _IDENT + ")")
_LOGICAL_RE = re.compile(r"(?:[^\n\\]|\\[^\n]|\\\n)*")
_INC_RE = re.compile(r'[ \t]*(?:"([^"\n]*)"|<([^>\n]*)>)')
_REST_RE = re.compile(r"[^\n]*")
_NETTYPE_RE = re.compile(r"[ \t]*(\w+)")
_WS_PAREN = re.compile(r"\s*\(")
_BODY_RE = re.compile(r'"(?:[^"\\]|\\.)*"|`"|``|' + _IDENT)
_CMT_RE = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)
_ARGTOK_RE = re.compile(r'[()\[\]{},]|"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)
_PASTE_RE = re.compile(r"\s*\x01\s*")

_IGNORED_DIRS = frozenset((
    "timescale", "pragma", "line", "begin_keywords", "end_keywords", "celldefine", "endcelldefine",
    "unconnected_drive", "nounconnected_drive", "default_decay_time", "default_trireg_strength",
    "delay_mode_distributed", "delay_mode_path", "delay_mode_unit", "delay_mode_zero",
    "suppress_faults", "nosuppress_faults", "enable_portfaults", "disable_portfaults",
    "protect", "endprotect", "protected", "endprotected",
))
_COND_DIRS = frozenset(("ifdef", "ifndef", "elsif", "else", "endif"))


@dataclass(slots=True)
class Macro:
    name: str
    params: tuple | None      # None for object-like; else tuple of (name, default | None)
    body: str
    file: str
    line: int


class LineMap:
    """Piecewise-linear map from output lines (1-based) to ``(file, original line)``."""

    __slots__ = ("files", "starts", "fidx", "orig")

    def __init__(self, files, starts, fidx, orig):
        self.files = files
        self.starts = starts
        self.fidx = fidx
        self.orig = orig

    def resolve(self, out_line: int) -> tuple[str, int]:
        i = bisect_right(self.starts, out_line) - 1
        if i < 0:
            i = 0
        return self.files[self.fidx[i]], self.orig[i] + (out_line - self.starts[i])

    def expand(self, nlines: int) -> tuple[list[str], list[int]]:
        """Per-output-line file and line lists, index ``out_line - 1``; length ``nlines``."""
        fl: list[str] = []
        ll: list[int] = []
        starts = self.starts
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else nlines + 1
            n = e - s
            if n <= 0:
                continue
            o = self.orig[i]
            ll.extend(range(o, o + n))
            fl.extend([self.files[self.fidx[i]]] * n)
        while len(fl) < nlines:  # defensive; cannot normally happen
            fl.append(fl[-1] if fl else "")
            ll.append(ll[-1] + 1 if ll else 1)
        return fl, ll


@dataclass(slots=True)
class PreprocResult:
    text: str
    linemap: LineMap
    macro_uses: dict          # output line -> list[(macro name, def file, def line)]
    nettype: list             # [(output line, "none" | "wire" | ...)] from `default_nettype / `resetall
    included: dict            # include path -> sha256 (all files read through `include)
    file: str = ""


class _State:
    __slots__ = ("chunks", "out_line", "starts", "fidx", "orig", "cur_f", "cur_off", "files", "fmap",
                 "macro_uses", "nettype", "included")

    def __init__(self):
        self.chunks: list[str] = []
        self.out_line = 1
        self.starts: list[int] = []
        self.fidx: list[int] = []
        self.orig: list[int] = []
        self.cur_f = -1
        self.cur_off = 0
        self.files: list[str] = []
        self.fmap: dict[str, int] = {}
        self.macro_uses: dict = {}
        self.nettype: list = []
        self.included: dict = {}

    def file_index(self, path: str) -> int:
        i = self.fmap.get(path)
        if i is None:
            i = len(self.files)
            self.files.append(path)
            self.fmap[path] = i
        return i

    def emit(self, s: str, fidx: int, src_line: int) -> None:
        if not s:
            return
        off = src_line - self.out_line
        if fidx != self.cur_f or off != self.cur_off:
            self.starts.append(self.out_line)
            self.fidx.append(fidx)
            self.orig.append(src_line)
            self.cur_f = fidx
            self.cur_off = off
        self.chunks.append(s)
        self.out_line += s.count("\n")


class Preprocessor:
    """Stateful preprocessor: macros persist across the files processed by one instance
    (a single compilation unit in command-line order)."""

    def __init__(self, defines: dict | None = None, incdirs=(), limits: Limits | None = None,
                 sink: DiagSink | None = None, loader=None):
        self.limits = limits or Limits()
        self.sink = sink if sink is not None else DiagSink()
        self.incdirs = tuple(os.fspath(d) for d in incdirs)
        self.loader = loader or read_source
        self.macros: dict[str, Macro] = {}
        self._cache: dict[str, SourceFile | None] = {}
        d = dict(defines or {})
        d.setdefault("SYNTHESIS", "1")
        for k, v in d.items():
            body = "1" if v is None or v is True else str(v)
            self.macros[k] = Macro(k, None, body, "<command line>", 0)
        self._limit_reported = False

    # ------------------------------------------------------------------ public

    def process(self, src: SourceFile) -> PreprocResult:
        text = src.text
        if "`" not in text and not _QUICK_RE.search(text):
            lm = LineMap([src.path], [1], [0], [1])
            return PreprocResult(text, lm, {}, [], {}, src.path)
        st = _State()
        self._run(src, st, 0, (os.path.realpath(src.path) if os.path.exists(src.path) else src.path,))
        lm = LineMap(st.files, st.starts or [1], st.fidx or [0], st.orig or [1])
        return PreprocResult("".join(st.chunks), lm, st.macro_uses, st.nettype, st.included, src.path)

    # ------------------------------------------------------------------ core

    def _run(self, src: SourceFile, st: _State, depth: int, stack: tuple) -> None:
        text = src.text
        n = len(text)
        sink = self.sink
        path = src.path
        fidx = st.file_index(path)
        macros = self.macros
        conds: list[list] = []   # [parent_active, taken, active, line]
        tr_off_line = 0          # 0 = region on
        cpos = 0
        cline = 1

        def line_at(p: int) -> int:
            nonlocal cpos, cline
            if p > cpos:
                cline += text.count("\n", cpos, p)
                cpos = p
            return cline

        def col_at(p: int) -> int:
            return p - text.rfind("\n", 0, p)

        active = True
        flush = 0

        def emit_range(a: int, b: int) -> None:
            if a >= b:
                return
            ln = line_at(a)
            seg = text[a:b] if active else "\n" * text.count("\n", a, b)
            if seg:
                st.emit(seg, fidx, ln)

        pos = 0
        search = _FILE_RE.search
        while pos < n:
            m = search(text, pos)
            if m is None:
                break
            kind = m.lastgroup
            if kind == "str" or kind == "esc":
                pos = m.end()
                continue
            s = m.start()
            if kind == "lc" or kind == "bc":
                pos = m.end()
                body = m.group()
                if len(body) < 14:
                    continue
                # a pragma starts the comment: prose such as "this shouldn't add pragma translate_off" is not one
                tm = _TRANSLATE_RE.match(body[2:].lstrip(" \t*"))
                if tm is not None:
                    emit_range(flush, s)
                    nl = body.count("\n")
                    if nl:
                        st.emit("\n" * nl, fidx, line_at(s))
                    flush = pos
                    if tm.group(1).lower() == "off":
                        if not tr_off_line:
                            tr_off_line = line_at(s)
                    elif tr_off_line:
                        a_line = tr_off_line
                        tr_off_line = 0
                        sink.add("IGNORED_REGION", f"translate_off region (lines {a_line}-{line_at(s)}) excluded",
                                 path, a_line, 1)
                    active = (conds[-1][2] if conds else True) and not tr_off_line
                    continue
                fm = _FULLPAR_RE.search(body.rstrip("*/ \t") if kind == "bc" else body)
                if fm is not None:
                    emit_range(flush, s)
                    names = ", ".join(sorted(set(re.findall(r"full_case|parallel_case", fm.group(1), re.I))))
                    nl = body.count("\n")
                    st.emit(("(* %s *)" % names.lower() if active else "") + "\n" * nl, fidx, line_at(s))
                    flush = pos
                continue
            # backtick directive or macro use
            name = m.group("tick")
            end = m.end()
            if name in _COND_DIRS:
                emit_range(flush, s)
                flush = pos = end
                if name == "ifdef" or name == "ifndef" or name == "elsif":
                    im = _IDENT_AFTER.match(text, end)
                    if im is None:
                        sink.add("PREPROC_ERROR", f"`{name} without a macro name", path, line_at(s), col_at(s))
                        defined = False
                    else:
                        defined = im.group(1) in macros
                        flush = pos = im.end()
                        if name == "ifndef":
                            defined = not defined
                    if name == "elsif":
                        if not conds:
                            sink.add("PREPROC_ERROR", "`elsif without `ifdef", path, line_at(s), col_at(s))
                        else:
                            c = conds[-1]
                            if c[1]:
                                c[2] = False
                            else:
                                c[2] = c[0] and defined
                                c[1] = defined
                    else:
                        parent = conds[-1][2] if conds else True
                        conds.append([parent, defined and parent, parent and defined, line_at(s)])
                elif name == "else":
                    if not conds:
                        sink.add("PREPROC_ERROR", "`else without `ifdef", path, line_at(s), col_at(s))
                    else:
                        c = conds[-1]
                        c[2] = c[0] and not c[1]
                        c[1] = True
                else:  # endif
                    if not conds:
                        sink.add("PREPROC_ERROR", "`endif without `ifdef", path, line_at(s), col_at(s))
                    else:
                        conds.pop()
                active = (conds[-1][2] if conds else True) and not tr_off_line
                continue
            if not active:
                pos = end
                continue
            # ---- active directives and macro uses
            emit_range(flush, s)
            if name == "define":
                dm = _IDENT_AFTER.match(text, end)
                if dm is None:
                    sink.add("PREPROC_ERROR", "`define without a macro name", path, line_at(s), col_at(s))
                    flush = pos = end
                    continue
                lm = _LOGICAL_RE.match(text, dm.end())
                lend = lm.end()
                self._define(dm.group(1), text[dm.end():lend], path, line_at(s), st, col_at(s))
                k = text.count("\n", s, lend)
                if k:
                    st.emit("\n" * k, fidx, line_at(s))
                flush = pos = lend
                continue
            if name == "undef":
                im = _IDENT_AFTER.match(text, end)
                if im:
                    macros.pop(im.group(1), None)
                    end = im.end()
                flush = pos = end
                continue
            if name == "undefineall":
                macros.clear()
                flush = pos = end
                continue
            if name == "include":
                im = _INC_RE.match(text, end)
                if im is None:
                    sink.add("PREPROC_ERROR", "malformed `include", path, line_at(s), col_at(s))
                    flush = pos = end
                    continue
                incname = im.group(1) if im.group(1) is not None else im.group(2)
                flush = pos = im.end()
                self._include(incname, path, line_at(s), col_at(s), st, depth, stack)
                continue
            if name == "default_nettype":
                nm = _NETTYPE_RE.match(text, end)
                if nm:
                    st.nettype.append((st.out_line, nm.group(1)))
                    end = nm.end()
                flush = pos = end
                continue
            if name == "resetall":
                st.nettype.append((st.out_line, "wire"))
                flush = pos = end
                continue
            if name in _IGNORED_DIRS:
                rm = _REST_RE.match(text, end)
                flush = pos = rm.end() if name not in ("celldefine", "endcelldefine") else end
                continue
            # macro use
            ln = line_at(s)
            site = (path, ln, col_at(s))
            rep, endp = self._use(name, text, end, 0, (), site, st)
            k = text.count("\n", s, endp)
            st.emit(rep.replace("\n", " ") + "\n" * k, fidx, ln)
            flush = pos = endp
        emit_range(flush, n)
        # end of file bookkeeping
        if tr_off_line:
            a_line = tr_off_line
            sink.add("IGNORED_REGION", f"translate_off region (lines {a_line}-{line_at(n)}) excluded "
                     "(no translate_on before end of file)", path, a_line, 1)
        for c in conds:
            sink.add("PREPROC_ERROR", "unterminated `ifdef/`ifndef", path, c[3], 1)

    # ------------------------------------------------------------------ define / use

    def _define(self, name: str, raw: str, path: str, line: int, st: _State, col: int) -> None:
        params = None
        body = raw
        if raw.startswith("("):
            depthp = 0
            close = -1
            for i, ch in enumerate(raw):
                if ch == "(":
                    depthp += 1
                elif ch == ")":
                    depthp -= 1
                    if depthp == 0:
                        close = i
                        break
            if close < 0:
                self.sink.add("PREPROC_ERROR", f"unterminated parameter list in `define {name}", path, line, col)
                close = len(raw) - 1
            plist = raw[1:close]
            body = raw[close + 1:]
            params = []
            if plist.strip():
                for part in self._split_top(plist):
                    part = part.strip()
                    if "=" in part:
                        pn, pd = part.split("=", 1)
                        params.append((pn.strip(), pd.strip()))
                    else:
                        params.append((part, None))
            params = tuple(params)
        body = body.replace("\\\r\n", "\n").replace("\\\n", "\n")
        if "/" in body:
            body = _CMT_RE.sub(lambda mo: mo.group() if mo.group()[0] == '"' else " ", body)
        body = body.replace("\n", " ").strip()
        self.macros[name] = Macro(name, params, body, path, line)

    @staticmethod
    def _split_top(s: str) -> list[str]:
        out, depth, cur = [], 0, []
        for ch in s:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                out.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        out.append("".join(cur))
        return out

    def _scan_args(self, s: str, open_idx: int):
        """Scan macro arguments; ``s[open_idx]`` is the '('.  Returns (args, index after ')') or (None, len)."""
        depth = 0
        args: list[str] = []
        cur: list[str] = []
        last = open_idx + 1
        pos = open_idx
        search = _ARGTOK_RE.search
        while True:
            m = search(s, pos)
            if m is None:
                return None, len(s)
            pos = m.end()
            c = s[m.start()]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth == 0:
                    cur.append(s[last:m.start()])
                    args.append("".join(cur).strip())
                    return args, pos
            elif c == "," and depth == 1:
                cur.append(s[last:m.start()])
                args.append("".join(cur).strip())
                cur = []
                last = pos
            elif c == "/":
                cur.append(s[last:m.start()])
                cur.append(" ")
                last = pos

    def _subst(self, mac: Macro, args: list[str], site) -> str:
        params = mac.params
        if len(args) == 1 and args[0] == "" and len(params) == 0:
            args = []
        if len(args) > len(params):
            self.sink.add("MACRO_ERROR", f"macro `{mac.name} called with {len(args)} arguments, expects {len(params)}",
                          site[0], site[1], site[2])
        vals = {}
        for i, (pn, pd) in enumerate(params):
            if i < len(args) and (args[i] != "" or pd is None):
                vals[pn] = args[i]
            elif pd is not None:
                vals[pn] = pd
            else:
                vals[pn] = ""
                if i >= len(args):
                    self.sink.add("MACRO_ERROR", f"macro `{mac.name}: missing argument '{pn}'", site[0], site[1], site[2])

        def rep(mo):
            t = mo.group()
            if t == '`"':
                return '"'
            if t == "``":
                return "\x01"
            if t[0] == '"':
                return t
            return vals.get(t, t)

        return _PASTE_RE.sub("", _BODY_RE.sub(rep, mac.body))

    def _use(self, name: str, s: str, end: int, depth: int, stack: tuple, site, st: _State):
        if name == "__FILE__":
            return '"%s"' % site[0], end
        if name == "__LINE__":
            return str(site[1]), end
        mac = self.macros.get(name)
        if mac is None:
            self.sink.add("UNDEFINED_MACRO", f"undefined macro `{name}", site[0], site[1], site[2])
            # an undefined function-like macro: drop its balanced argument list too, so the
            # arguments do not leak into the surrounding source as stray tokens
            wm = _WS_PAREN.match(s, end)
            if wm is not None:
                args, end2 = self._scan_args(s, wm.end() - 1)
                if args is not None:
                    end = end2
            return "", end
        if name in stack:
            self.sink.add("MACRO_ERROR", f"recursive expansion of macro `{name}", site[0], site[1], site[2])
            return "", end
        if depth >= self.limits.macro_depth:
            self.sink.add("LIMIT_REACHED", f"macro expansion depth limit ({self.limits.macro_depth}) reached at `{name}",
                          site[0], site[1], site[2])
            return "", end
        body = mac.body
        if mac.params is not None:
            wm = _WS_PAREN.match(s, end)
            if wm is None:
                self.sink.add("MACRO_ERROR", f"macro `{name} requires arguments", site[0], site[1], site[2])
                return "", end
            args, end = self._scan_args(s, wm.end() - 1)
            if args is None:
                self.sink.add("MACRO_ERROR", f"unterminated argument list for macro `{name}", site[0], site[1], site[2])
                return "", end
            body = self._subst(mac, args, site)
        st.macro_uses.setdefault(st.out_line, []).append((name, mac.file, mac.line))
        if "`" in body:
            body = self._rescan(body, depth + 1, stack + (name,), site, st)
        return body, end

    def _rescan(self, s: str, depth: int, stack: tuple, site, st: _State) -> str:
        out: list[str] = []
        pos = 0
        flush = 0
        search = _TICKSTR_RE.search
        conds: list[list] = []      # conditional directives inside the body: [parent_active, taken, active]
        active = True
        while True:
            m = search(s, pos)
            if m is None:
                break
            if m.lastgroup == "str":
                pos = m.end()
                continue
            name = m.group("tick")
            if active:
                out.append(s[flush:m.start()])
            if name in _COND_DIRS:
                pos = flush = m.end()
                if name == "else":
                    if conds:
                        c = conds[-1]
                        c[2] = c[0] and not c[1]
                        c[1] = True
                elif name == "endif":
                    if conds:
                        conds.pop()
                else:
                    im = _IDENT_AFTER.match(s, pos)
                    defined = False
                    if im is not None:
                        defined = (im.group(1) in self.macros) == (name != "ifndef")
                        pos = flush = im.end()
                    if name == "elsif":
                        if conds:
                            c = conds[-1]
                            c[2] = c[0] and not c[1] and defined
                            c[1] = c[1] or defined
                    else:
                        parent = conds[-1][2] if conds else True
                        conds.append([parent, parent and defined, parent and defined])
                active = conds[-1][2] if conds else True
                continue
            if not active:
                pos = flush = m.end()
                continue
            if name in _IGNORED_DIRS or name in ("define", "undef", "include", "default_nettype"):
                pos = flush = m.end()
                continue
            rep, endp = self._use(name, s, m.end(), depth, stack, site, st)
            out.append(rep)
            pos = flush = endp
        if active:
            out.append(s[flush:])
        return "".join(out)

    # ------------------------------------------------------------------ include

    def _include(self, name: str, path: str, line: int, col: int, st: _State, depth: int, stack: tuple) -> None:
        sink = self.sink
        if depth + 1 > self.limits.include_depth:
            sink.add("LIMIT_REACHED", f"include depth limit ({self.limits.include_depth}) reached at '{name}'",
                     path, line, col)
            return
        found = resolve_include(name, os.path.dirname(path) or ".", self.incdirs)
        if found is None:
            sink.add("INCLUDE_NOT_FOUND", f"include file '{name}' not found", path, line, col)
            return
        real = os.path.realpath(found)
        if real in stack:
            sink.add("INCLUDE_CYCLE", f"include cycle: '{name}' includes itself (directly or indirectly)",
                     path, line, col)
            return
        src = self._cache.get(real)
        if src is None:
            try:
                src = self.loader(found)
            except OSError:
                sink.add("INCLUDE_NOT_FOUND", f"include file '{name}' cannot be read", path, line, col)
                return
            self._cache[real] = src
        st.included[found] = src.sha256
        self._run(src, st, depth + 1, stack + (real,))
