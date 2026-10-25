"""Provenance facts for cost-tree operations (see the convention in ``gatecost.diff``).

The IR does not record which parameter, localparam, port width or macro condition determined an
operation, so this module recovers it from the sources: it indexes constant declarations, net
declarations (with their ranges), instance parameter overrides and ``ifdef`` regions of every
source text, evaluates the constants of each module elaboration (``ModuleCost.parameters`` give
the values of the module parameters), and links every operation to the constants reachable from
the statement it originates from (identifiers on the statement and, through the declarations of
those identifiers, the constants in their ranges).  Facts written into ``CostOp.facts``:

    params / param_origins   constants (parameters, localparams, package constants) -> value, (file, line)
    widths / width_origins   declared widths of the signals on the statement -> bits, declaration (file, line)
    consts                   {"operand": K} for a constant operand
    defines / define_origins macros whose definedness gates the statement -> value or None, (file, line)

It is a heuristic over text and never changes a cost.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import replace

from .tree import DesignCostTree

_V_EXT = (".v", ".vh", ".sv", ".svh")
_NUM = re.compile(r"(\d*)'[sS]?([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)|\d[\d_]*")
_ID = re.compile(r"(?<![\w$`'.])([A-Za-z_]\w*)")
_V_KW = set("""module endmodule input output inout wire reg logic bit integer int parameter localparam assign always
always_ff always_comb always_latch begin end if else case casez casex endcase default for while generate endgenerate
genvar posedge negedge or and not signed unsigned function endfunction task endtask return initial real time byte
shortint longint tri supply0 supply1 var automatic static typedef enum struct packed unique priority import export""".split())
_H_KW = set("""library use all entity is port in out inout of architecture begin end process if then elsif else case when
others for loop generate signal variable constant to downto and or xor nand nor xnor not mod rem abs rising_edge
falling_edge event std_logic std_logic_vector unsigned signed integer natural positive resize to_unsigned to_signed
to_integer ieee numeric_std std_logic_1164 null wait until after open with select map generic component package
body type subtype range function return true false boolean bit bit_vector real time work""".split())


# --------------------------------------------------------------------------- text helpers


_BLANK_V = re.compile(r'//[^\n]*|/\*.*?(?:\*/|\Z)|"[^"\n]*["\n]?', re.S)
_BLANK_H = re.compile(r'--[^\n]*|"[^"\n]*["\n]?')
_NOT_NL = re.compile(r"[^\n]")


def _blank_rep(m) -> str:
    g = m.group()
    if g.startswith("/*"):
        return _NOT_NL.sub(" ", g)
    return " " * len(g)


def _blank(text: str, vhdl: bool) -> str:
    """Comments and string literals replaced by spaces (newlines kept)."""
    return (_BLANK_H if vhdl else _BLANK_V).sub(_blank_rep, text)


def _balanced(text: str, i: int, open_c="(", close_c=")") -> int:
    """Index just after the group opened at ``text[i]``, or -1."""
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


def _split_top(text: str, sep: str = ",") -> list[tuple[int, str]]:
    """Split at top-level separators; returns (offset, piece)."""
    out, depth, start = [], 0, 0
    for j, c in enumerate(text):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == sep and depth == 0:
            out.append((start, text[start:j]))
            start = j + 1
    out.append((start, text[start:]))
    return out


_NUMLIT = re.compile(r"\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+")
_BASED = re.compile(r"\b\d+#\w+#")
_idents_memo: dict = {}


def idents(expr: str, vhdl: bool) -> list[str]:
    key = (expr, vhdl)
    r = _idents_memo.get(key)
    if r is None:
        if len(_idents_memo) > 200_000:
            _idents_memo.clear()
        kw = _H_KW if vhdl else _V_KW
        seen, out = set(), []
        clean = _NUMLIT.sub(" ", expr)
        if vhdl:
            clean = _BASED.sub(" ", clean)
        for m in _ID.finditer(clean):
            n = m.group(1)
            if vhdl:
                n = n.lower()
            if n in kw or n in seen:
                continue
            seen.add(n)
            out.append(n)
        r = _idents_memo[key] = tuple(out)
    return list(r)


# --------------------------------------------------------------------------- tiny evaluator

_TOK = re.compile(r"\s*(?:(\d*'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+)|(\d[\d_]*)|(\$?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)?)|(\*\*|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^~!<>?:(),]))")


def _tokens(s: str):
    out, pos = [], 0
    s = s.strip()
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m or m.end() == pos:
            return None
        pos = m.end()
        out.append(m.group(m.lastindex))
        if m.lastindex is None:
            return None
    return out


def _lit(t: str):
    if "'" in t:
        w, b, digits = re.match(r"(\d*)'[sS]?([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)", t).groups()
        if re.search(r"[xXzZ?]", digits):
            return None
        return int(digits.replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[b.lower()])
    return int(t.replace("_", ""))


def evaluate(expr: str, env: dict):
    """Integer value of a constant expression (names looked up in ``env``) or None."""
    toks = _tokens(expr)
    if not toks:
        return None
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def eat(t=None):
        v = peek()
        if t is not None and v != t:
            raise ValueError(f"expected {t!r}, got {v!r}")
        pos[0] += 1
        return v

    prec = [["||"], ["&&"], ["|"], ["^"], ["&"], ["==", "!="], ["<", ">", "<=", ">="], ["<<", ">>"], ["+", "-"], ["*", "/", "%"], ["**"]]

    def ternary():
        c = binary(0)
        if peek() == "?":
            eat()
            a = ternary()
            eat(":")
            b = ternary()
            return a if c else b
        return c

    def binary(level):
        if level >= len(prec):
            return unary()
        v = binary(level + 1)
        while peek() in prec[level]:
            op = eat()
            r = binary(level + 1)
            v = _apply(op, v, r)
        return v

    def unary():
        t = peek()
        if t == "-":
            eat()
            return -unary()
        if t == "+":
            eat()
            return unary()
        if t == "~":
            eat()
            return ~unary()
        if t == "!":
            eat()
            return int(not unary())
        return atom()

    def atom():
        t = eat()
        if t is None:
            raise ValueError("unexpected end of expression")
        if t == "(":
            v = ternary()
            eat(")")
            return v
        if t[0].isdigit():
            return _lit(t)
        if t.startswith("$"):
            eat("(")
            args = [ternary()]
            while peek() == ",":
                eat()
                args.append(ternary())
            eat(")")
            if t == "$clog2":
                return max(args[0] - 1, 0).bit_length()
            raise ValueError(f"unsupported system function {t}")
        name = t.split("::")[-1]
        if peek() == "(" and name.lower() in ("integer", "natural", "to_integer", "natural"):
            eat()
            v = ternary()
            eat(")")
            return v
        v = env.get(name, env.get(name.lower()))
        if not isinstance(v, int):
            raise ValueError(f"{name} has no integer value")
        return v

    try:
        v = ternary()
        if pos[0] != len(toks):
            return None
        return v
    except (ValueError, ZeroDivisionError, OverflowError, TypeError, RecursionError):
        # RecursionError: deeply nested text; without it annotate_tree would drop the provenance of the whole tree
        return None


def _apply(op, a, b):
    if a is None or b is None:
        raise ValueError(f"operand of {op!r} has no value")
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return int(a / b) if b else (_ for _ in ()).throw(ZeroDivisionError())
    if op == "%":
        return a % b
    if op == "**":
        if b < 0 or b > 256:
            raise ValueError(f"exponent {b} outside [0, 256]")
        return a ** b
    if op == "<<":
        if b > 4096:
            raise ValueError(f"shift amount {b} above 4096")
        return a << b
    if op == ">>":
        return a >> b
    if op == "&":
        return a & b
    if op == "|":
        return a | b
    if op == "^":
        return a ^ b
    return int({"&&": a and b, "||": a or b, "==": a == b, "!=": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op])


def dim_width(dim: str, vhdl: bool, env: dict):
    """Width in bits of a range text (``W-1:0``, ``W-1 downto 0``) or None."""
    if vhdl:
        m = re.match(r"(.+?)\s+(downto|to)\s+(.+)$", dim.strip(), re.I)
        if not m:
            return None
        a, b = evaluate(m.group(1), env), evaluate(m.group(3), env)
    else:
        parts = _split_top(dim, ":")
        if len(parts) != 2:
            return None
        a, b = evaluate(parts[0][1], env), evaluate(parts[1][1], env)
    if a is None or b is None:
        return None
    return abs(a - b) + 1


# --------------------------------------------------------------------------- file index


class Const:
    __slots__ = ("name", "scope", "local", "file", "line", "rhs", "disp")

    def __init__(self, name, scope, local, file, line, rhs, disp=None):
        self.name, self.scope, self.local, self.file, self.line, self.rhs = name, scope, local, file, line, rhs
        self.disp = disp or name


class Decl:
    __slots__ = ("name", "scope", "file", "line", "dims")

    def __init__(self, name, scope, file, line, dims):
        self.name, self.scope, self.file, self.line, self.dims = name, scope, file, line, dims


class FileIdx:
    def __init__(self, name: str, text: str):
        self.name = name
        self.vhdl = not name.lower().endswith(_V_EXT)
        self.text = self.raw = _blank(text, self.vhdl)
        if self.vhdl:
            self.text = self.text.lower()
        self.starts = [0]
        for m in re.finditer("\n", self.text):
            self.starts.append(m.end())
        self.scopes: list[tuple[int, str, str]] = []   # (start pos, kind, name)
        self.consts: list[Const] = []
        self.decls: list[Decl] = []
        self.ifdefs: list[tuple[int, int, list[str], int]] = []   # (first line, last line, names, ifdef line)
        self.macros: dict[str, tuple[int, str]] = {}
        self._scan()

    def line(self, pos: int) -> int:
        return bisect_right(self.starts, pos)

    def scope_at(self, pos: int) -> str:
        cur = ""
        for st, _k, nm in self.scopes:
            if st <= pos:
                cur = nm
            else:
                break
        return cur

    def statement(self, line: int, limit: int = 8) -> str:
        if line < 1 or line > len(self.starts):
            return ""
        a = self.starts[line - 1]
        b = self.starts[min(line - 1 + limit, len(self.starts) - 1)] if line - 1 + limit < len(self.starts) else len(self.text)
        seg = self.text[a:b]
        j = seg.find(";")
        return seg if j < 0 else seg[: j + 1]

    # -- scanning
    def _scan(self) -> None:
        t = self.text
        if self.vhdl:
            self._scan_vhdl(t)
        else:
            self._scan_verilog(t)

    def _scan_verilog(self, t: str) -> None:
        for m in re.finditer(r"\b(module|macromodule|interface|package|program)\b\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)", t):
            self.scopes.append((m.start(), m.group(1), m.group(2)))
        for m in re.finditer(r"\b(parameter|localparam)\b", t):
            self._verilog_params(t, m)
        self._verilog_decls(t)
        # macros and ifdef regions
        stack: list[tuple[list[str], int]] = []
        for m in re.finditer(r"`(define|undef|ifdef|ifndef|elsif|else|endif)\b[ \t]*([A-Za-z_]\w*)?([^\n]*)", t):
            kind, nm, rest = m.group(1), m.group(2), m.group(3)
            ln = self.line(m.start())
            if kind == "define" and nm:
                self.macros[nm] = (ln, rest.strip())
            elif kind == "undef" and nm:
                self.macros.pop(nm, None)
            elif kind in ("ifdef", "ifndef"):
                stack.append(([nm] if nm else [], ln))
                stack[-1] = (stack[-1][0], ln)
            elif kind == "elsif" and stack and nm:
                stack[-1][0].append(nm)
            elif kind == "endif" and stack:
                names, st = stack.pop()
                if names:
                    self.ifdefs.append((st + 1, ln - 1, list(names), st))

    def _verilog_params(self, t: str, m) -> None:
        local = m.group(1) == "localparam"
        i = m.end()
        depth = 0
        j = i
        while j < len(t):
            c = t[j]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    break
            elif c == ";" and depth == 0:
                break
            elif depth == 0 and re.match(r"\b(parameter|localparam)\b", t[j:j + 10]) and (j == 0 or not t[j - 1].isalnum()):
                break
            j += 1
        seg = t[i:j]
        for off, item in _split_top(seg):
            if "=" not in item:
                continue
            k = _top_eq(item)
            if k < 0:
                continue
            lhs, rhs = item[:k], item[k + 1:]
            mm = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*$", lhs)
            if not mm:
                continue
            pos = i + off + mm.start(1)
            self.consts.append(Const(mm.group(1), self.scope_at(pos), local, self.name, self.line(pos), rhs.strip()))

    def _verilog_decls(self, t: str) -> None:
        kws = r"input|output|inout|wire|reg|logic|bit|integer|int|tri|supply0|supply1|byte|shortint|longint"
        pat = re.compile(rf"\b(?:{kws})\b")
        ms = list(pat.finditer(t))
        k = 0
        while k < len(ms):
            m = ms[k]
            k += 1
            # merge "output reg" style sequences
            end = m.end()
            while k < len(ms) and t[end:ms[k].start()].strip() in ("", "signed", "unsigned", "var"):
                end = ms[k].end()
                k += 1
            depth = 0
            j = end
            while j < len(t):
                c = t[j]
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    depth -= 1
                    if depth < 0:
                        break
                elif c == ";" and depth == 0:
                    break
                elif depth == 0 and pat.match(t, j) and not t[j - 1].isalnum() and t[j - 1] != "_":
                    break
                j += 1
            seg = t[end:j]
            pos0 = end
            s = seg
            dims = []
            while True:
                mm = re.match(r"\s*(?:(?:signed|unsigned|var)\b\s*)?\[", s)
                if not mm:
                    break
                lb = s.index("[", 0)
                rb = _balanced(s, lb, "[", "]")
                if rb < 0:
                    break
                dims.append(s[lb + 1:rb - 1])
                s = s[rb:]
                pos0 += rb
            s2 = re.sub(r"^\s*(?:signed|unsigned|var)\b", "", s)
            pos0 += len(s) - len(s2)
            for off, item in _split_top(s2):
                mm = re.match(r"\s*([A-Za-z_]\w*)", item)
                if not mm:
                    continue
                p = pos0 + off + mm.start(1)
                self.decls.append(Decl(mm.group(1), self.scope_at(p), self.name, self.line(p), list(dims)))

    def _scan_vhdl(self, t: str) -> None:
        for m in re.finditer(r"\b(entity|architecture|package)\b(?:\s+body)?\s+(\w+)(?:\s+of\s+(\w+))?\s+is\b", t):
            self.scopes.append((m.start(), m.group(1), m.group(3) or m.group(2)))
        for m in re.finditer(r"\bgeneric\s*\(", t):
            end = _balanced(t, m.end() - 1)
            if end < 0:
                continue
            body_off = m.end()
            for off, item in _split_top(t[body_off:end - 1], ";"):
                self._vhdl_names(item, body_off + off, local=False, const=True)
        for m in re.finditer(r"\bconstant\b", t):
            j = t.find(";", m.end())
            if j < 0:
                continue
            self._vhdl_names(t[m.end():j], m.end(), local=True, const=True)
        for m in re.finditer(r"\bport\s*\(", t):
            end = _balanced(t, m.end() - 1)
            if end < 0:
                continue
            body_off = m.end()
            for off, item in _split_top(t[body_off:end - 1], ";"):
                self._vhdl_names(item, body_off + off, local=False, const=False)
        for m in re.finditer(r"\b(signal|variable)\b", t):
            j = t.find(";", m.end())
            if j > 0:
                self._vhdl_names(t[m.end():j], m.end(), local=False, const=False)

    def _vhdl_names(self, item: str, off: int, *, local: bool, const: bool) -> None:
        mm = re.match(r"\s*([\w\s,]+?)\s*:\s*(.*)$", item, re.S)
        if not mm:
            return
        names = re.findall(r"\w+", mm.group(1))
        rest = mm.group(2)
        init = None
        k = rest.find(":=")
        if k >= 0:
            rest, init = rest[:k], rest[k + 2:].strip()
        for nm in names:
            p = off + item.find(nm)
            if const and init is not None:
                self.consts.append(Const(nm, self.scope_at(p), local, self.name, self.line(p), init, self.raw[p:p + len(nm)]))
            elif not const:
                dims = []
                for pm in re.finditer(r"\(", rest):
                    e = _balanced(rest, pm.start())
                    if e > 0 and re.search(r"\b(downto|to)\b", rest[pm.start():e]):
                        dims.append(rest[pm.start() + 1:e - 1])
                        break
                self.decls.append(Decl(nm, self.scope_at(p), self.name, self.line(p), dims))


def _top_eq(s: str) -> int:
    depth = 0
    for j, c in enumerate(s):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "=" and depth == 0:
            return j
    return -1


# --------------------------------------------------------------------------- environments


class E:
    """A constant with its value, origin and the constants it was derived from."""

    __slots__ = ("name", "value", "origin", "deps", "disp")

    def __init__(self, name, value, origin, deps, disp=None):
        self.name, self.value, self.origin, self.deps = name, value, origin, deps
        self.disp = disp or name


class Env:
    def __init__(self):
        self.entries: dict[str, E] = {}

    def values(self) -> dict:
        return {k: e.value for k, e in self.entries.items() if isinstance(e.value, int)}


def _parse_value(s: str):
    s = s.strip()
    v = evaluate(s, {})
    return v if v is not None else s


class Annotator:
    def __init__(self, tree: DesignCostTree, texts: dict, defines: dict, ifdef_extra: dict | None = None,
                 file_order=None):
        self.tree = tree
        self.ifdef_extra = ifdef_extra or {}         # file -> names of its ifdef regions, for files not in ``texts``
        self.file_order = file_order
        self.defines = {k: str(v) for k, v in (defines or {}).items()}
        self.files: dict[str, FileIdx] = {n: FileIdx(n, t) for n, t in texts.items()}
        self.pkg: dict[str, list[Const]] = {}
        for f in self.files.values():
            for c in f.consts:
                if self._scope_kind(f, c.scope) == "package":
                    self.pkg.setdefault(c.name.lower() if f.vhdl else c.name, []).append(c)
        self.macros: dict[str, tuple[str, int, str]] = {}
        for n in sorted(self.files):
            for k, (ln, v) in self.files[n].macros.items():
                self.macros[k] = (n, ln, v)
        self._pkg_env: dict[str, E] = {}
        self.envs: dict[str, Env] = {}
        self.parent: dict[str, tuple[str, str]] = {}
        for pk in sorted(tree.modules):
            for e in tree.modules[pk].instances:
                self.parent.setdefault(e.child_key, (pk, e.path))

    @staticmethod
    def _scope_kind(f: FileIdx, scope: str) -> str:
        for _s, k, nm in f.scopes:
            if nm == scope:
                return k
        return ""

    # -- package constants
    def pkg_entry(self, name: str) -> E | None:
        key = name.lower() if any(f.vhdl for f in self.files.values()) and name.lower() in self.pkg else name
        if key in self._pkg_env:
            return self._pkg_env[key]
        cs = self.pkg.get(key)
        if not cs:
            return None
        c = cs[-1]
        self._pkg_env[key] = E(c.name, None, (c.file, c.line), [], c.disp)   # recursion guard
        f = self.files[c.file]
        deps = []
        vals = {}
        for i in idents(c.rhs, f.vhdl):
            if i == c.name.lower() or i == c.name:
                continue
            d = self.pkg_entry(i)
            if d is not None:
                deps.append(d)
                if isinstance(d.value, int):
                    vals[i] = d.value
        v = evaluate(c.rhs, vals)
        e = E(c.name, v if v is not None else c.rhs.strip(), (c.file, c.line), deps, c.disp)
        self._pkg_env[key] = e
        return e

    # -- module environment
    def env_for(self, key: str) -> Env:
        if key in self.envs:
            return self.envs[key]
        env = Env()
        self.envs[key] = env
        m = self.tree.modules[key]
        f = self.files.get(m.file)
        if f is None:
            return env
        scope = m.name.lower() if f.vhdl else m.name
        keyparams = {(k.lower() if f.vhdl else k): v for k, v in m.parameters}
        overrides = self._overrides(key, m, f)
        for c in f.consts:
            if c.scope != scope:
                continue
            nm = c.name
            vals = env.values()
            deps = []
            origin = (c.file, c.line)
            val = None
            if not c.local and nm in keyparams:
                val = _parse_value(keyparams[nm])
                ov = overrides.get(nm)
                if ov is not None:
                    origin = ov[0]
                    deps = ov[1]
            else:
                refs = [env.entries.get(i) or self.pkg_entry(i) for i in idents(c.rhs, f.vhdl) if i != nm]
                deps = [r for r in refs if r is not None]
                val = evaluate(c.rhs, {**{d.name: d.value for d in deps if isinstance(d.value, int)}, **vals})
                if val is None:
                    val = c.rhs.strip()
            env.entries[nm] = E(nm, val, origin, deps, c.disp)
        return env

    def _overrides(self, key: str, m, f: FileIdx) -> dict:
        """{param: ((file, line), [parent entries])} for the instance that created this elaboration."""
        par = self.parent.get(key)
        if par is None:
            return {}
        pkey, path = par
        pm = self.tree.modules[pkey]
        pf = self.files.get(pm.file)
        if pf is None:
            return {}
        penv = self.env_for(pkey)
        inst = re.sub(r"\[.*?\]", "", path).split(".")[-1]
        name = m.name.lower() if pf.vhdl else m.name
        inst_l = inst.lower() if pf.vhdl else inst
        t = pf.text
        out = {}
        if pf.vhdl:
            pat = re.compile(rf"\b{re.escape(inst_l)}\s*:\s*(?:entity\s+\w+\.)?{re.escape(name)}\b[^;]*?\bgeneric\s+map\s*\(")
            mm = pat.search(t)
            if not mm:
                return {}
            end = _balanced(t, mm.end() - 1)
            body = t[mm.end():end - 1]
            for off, item in _split_top(body):
                am = re.match(r"\s*(\w+)\s*=>\s*(.*)$", item, re.S)
                if am:
                    p = mm.end() + off + am.start(1)
                    out[am.group(1)] = ((pf.name, pf.line(p)), self._deps(am.group(2), pf, penv))
            return out
        for mm in re.finditer(rf"\b{re.escape(name)}\b\s*(#\s*\()?", t):
            pos = mm.end()
            body, bstart = "", 0
            if mm.group(1):
                end = _balanced(t, mm.end() - 1)
                if end < 0:
                    continue
                body, bstart, pos = t[mm.end():end - 1], mm.end(), end
            if not re.match(rf"\s*{re.escape(inst_l)}\s*[\[(]", t[pos:pos + len(inst_l) + 40]):
                continue
            order = [c.name for c in f.consts if c.scope == name and not c.local]
            for idx, (off, item) in enumerate(_split_top(body)):
                am = re.match(r"\s*\.\s*(\w+)\s*\((.*)\)\s*$", item, re.S)
                if am:
                    p = bstart + off + am.start(1)
                    out[am.group(1)] = ((pf.name, pf.line(p)), self._deps(am.group(2), pf, penv))
                elif item.strip() and idx < len(order):
                    p = bstart + off + (len(item) - len(item.lstrip()))
                    out[order[idx]] = ((pf.name, pf.line(p)), self._deps(item, pf, penv))
            return out
        return out

    def _deps(self, expr: str, pf: FileIdx, penv: Env) -> list:
        res = []
        for i in idents(expr, pf.vhdl):
            e = penv.entries.get(i) or self.pkg_entry(i)
            if e is not None:
                res.append(e)
        return res

    # -- operations
    def annotate(self) -> DesignCostTree:
        tree = self.tree
        mods = {}
        for key, m in tree.modules.items():
            f = self.files.get(m.file)
            if f is None or not m.ops:
                mods[key] = m
                continue
            env = self.env_for(key)
            scope = m.name.lower() if f.vhdl else m.name
            decls = {d.name: d for d in f.decls if d.scope == scope}
            vals, dwm = env.values(), {}
            ops = tuple(self._op(o, f, env, decls, scope, vals, dwm) for o in m.ops)
            mods[key] = replace(m, ops=ops)
        cfg = dict(tree.config or {})
        mv = {}
        if self.ifdef_extra:
            order = list(self.file_order or ()) or list(self.files)
        else:
            order = list(self.files)
        for fn in order:
            f = self.files.get(fn)
            if f is not None:
                for n in self.ifdef_names(f):
                    mv[n] = self._macro(n)
            else:
                for n in self.ifdef_extra.get(fn, ()):
                    mv[n] = self._macro(n)
        cfg["macro_values"] = {**{k: v for k, v in self.defines.items()}, **{k: v for k, v in mv.items() if v is not None}}
        return replace(tree, modules=mods, config=cfg)

    @staticmethod
    def ifdef_names(f: FileIdx) -> list:
        return [n for _a, _b, names, _l in f.ifdefs for n in names]

    def _macro(self, n: str):
        if n in self.defines:
            return self.defines[n]
        if n in self.macros:
            return self.macros[n][2] or "1"
        return None

    def _op(self, o, f: FileIdx, env: Env, decls: dict, scope: str, vals=None, dwm=None):
        if vals is None:
            vals, dwm = env.values(), {}
        if o.op == "BLACKBOX" or not o.file or o.line <= 0:
            return o
        of = self.files.get(o.file, f)
        stmt = of.statement(o.line)
        names = idents(stmt, of.vhdl)
        nk = o.named_key[1] if len(o.named_key) > 1 and isinstance(o.named_key[1], str) else ""
        if nk:
            tm = re.match(r"[A-Za-z_]\w*", nk)
            if tm:
                t = tm.group(0).lower() if of.vhdl else tm.group(0)
                if t not in names:
                    names.append(t)
        facts = dict(o.facts)
        entries: dict[str, E] = {}
        widths, worg = {}, {}
        for nm in names:
            e = env.entries.get(nm)
            if e is not None:
                entries[nm] = e
            d = decls.get(nm)
            if d is None:
                continue
            for dim in d.dims:
                for i in idents(dim, of.vhdl):
                    e2 = env.entries.get(i) or self.pkg_entry(i)
                    if e2 is not None:
                        entries.setdefault(i, e2)
            if d.dims and len(widths) < 6:
                w = 1
                ok = True
                for dim in d.dims:
                    dk = (dim, of.vhdl)
                    if dk in dwm:
                        dw = dwm[dk]
                    else:
                        dw = dwm[dk] = dim_width(dim, of.vhdl, vals)
                    if dw is None:
                        ok = False
                        break
                    w *= dw
                if ok:
                    widths[nm] = w
                    worg[nm] = (d.file, d.line)
        # transitive closure
        allp: dict[str, E] = {}
        stack = list(entries.values())
        guard = 0
        while stack and guard < 200:
            guard += 1
            e = stack.pop()
            if e.name in allp and allp[e.name] is not e:
                continue
            if e.name in allp:
                continue
            allp[e.name] = e
            stack.extend(e.deps)
        if allp:
            facts["params"] = {e.disp: _disp(e.value) for k, e in sorted(allp.items())}
            facts["param_origins"] = {e.disp: e.origin for k, e in sorted(allp.items())}
        if widths:
            facts["widths"] = widths
            facts["width_origins"] = worg
        if "const" in facts and "consts" not in facts:
            facts["consts"] = {"operand": facts["const"]}
        # macros
        defs, dorg = {}, {}
        for a, b, mnames, ifl in of.ifdefs:
            if a <= o.line <= b:
                for n in mnames:
                    defs[n] = self._macro(n)
                    if n in self.macros:
                        dorg[n] = (self.macros[n][0], self.macros[n][1])
                    else:
                        dorg[n] = (of.name, ifl)
        if defs:
            facts["defines"] = defs
            facts["define_origins"] = dorg
        return replace(o, facts=facts)


def _disp(v):
    return v if isinstance(v, int) else (str(v) if v is not None else None)


def annotate_tree(tree: DesignCostTree, texts: dict, defines: dict | None = None, *, facts_out: dict | None = None,
                  ifdef_extra: dict | None = None, file_order=None) -> DesignCostTree:
    """Return ``tree`` with provenance facts added to its operations (never raises).

    ``facts_out`` receives ``{file: [names in its ifdef regions]}``; ``ifdef_extra`` and ``file_order`` let a
    caller that passes only part of the sources supply the ifdef names of the others (cone analysis)."""
    try:
        ann = Annotator(tree, texts, defines or {}, ifdef_extra, file_order)
        out = ann.annotate()
        if facts_out is not None:
            for n, f in ann.files.items():
                facts_out[n] = ann.ifdef_names(f)
        return out
    except Exception:  # provenance is best effort: attribution degrades, costs are unaffected
        import os

        if os.environ.get("GATECOST_ELAB_DEBUG"):
            raise
        return tree
