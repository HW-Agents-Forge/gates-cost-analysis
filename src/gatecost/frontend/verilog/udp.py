"""Lowering of Verilog user-defined primitives (UDPs) to an ordinary module.

``lower_udp(parser, i)`` reads the ``primitive ... endprimitive`` at token ``i`` and returns
``(module, None)`` -- a synthetic :class:`~.ast.Module` (``kind == "primitive"``) whose body is the
logic equivalent of the table -- or ``(None, reason)`` when the UDP cannot be lowered (the caller
then keeps the black box and reports ``reason``).

The module is produced by generating Verilog text and running it through the regular parser, so
the elaborator needs no UDP-specific code.

Supported
  * combinational UDPs: the truth table becomes a minimised sum of products (Quine-McCluskey with
    don't cares).  ``?`` and ``b`` are wildcards; rows containing an explicit ``x`` input are
    ignored (x never occurs in synthesised logic); an ``x`` output and input combinations not
    covered by any row are don't cares.
  * level-sensitive sequential UDPs (``reg`` output, no edge entries): a latch
    (``always @*`` with set / reset conditions; ``-`` and unlisted combinations hold).
  * edge-sensitive sequential UDPs whose effective entries all use one edge (``r``/``(01)``/``p``
    or ``f``/``(10)``/``n``) of one clock input: a flip-flop.  Entries that only hold the state
    (``-`` output) are ignored.
Not supported (black box + warning): asynchronous set/reset mixed with edges, entries that toggle,
several clocks, both clock edges, more than 10 data inputs.  ``initial`` values are ignored.
"""
from __future__ import annotations

import itertools
import re

from gatecost.frontend.common.diagnostics import DiagSink
from .lexer import lex

MAX_VARS = 10
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_LEVEL = {"0": (0,), "1": (1,), "?": (0, 1), "b": (0, 1), "B": (0, 1), "x": (), "X": ()}
_VALS = {"0": {0}, "1": {1}, "x": {"x"}, "X": {"x"}, "?": {0, 1, "x"}, "b": {0, 1}, "B": {0, 1}}
_EDGE_SHORT = {"r": "01", "R": "01", "f": "10", "F": "10", "p": "p", "P": "p", "n": "n", "N": "n", "*": "??"}


class _Unsupported(Exception):
    pass


def _edge_dirs(spec: str) -> frozenset:
    """Which of 'pos'/'neg' an edge spec (two chars, or p/n) matches (0/1 levels only)."""
    if spec == "p":
        return frozenset(("pos",))
    if spec == "n":
        return frozenset(("neg",))
    a, b = _VALS.get(spec[0]), _VALS.get(spec[1])
    if a is None or b is None:
        raise _Unsupported("malformed edge entry")
    out = set()
    if 0 in a and 1 in b:
        out.add("pos")
    if 1 in a and 0 in b:
        out.add("neg")
    return frozenset(out)


def _parse_field(s: str):
    """List of specs: ``('l', levels)`` or ``('e', dirs)``."""
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c.isspace():
            i += 1
        elif c == "(":
            j = s.find(")", i)
            body = re.sub(r"\s+", "", s[i + 1:j]) if j > 0 else ""
            if j < 0 or len(body) != 2:
                raise _Unsupported("malformed edge entry")
            out.append(("e", _edge_dirs(body)))
            i = j + 1
        elif c in _EDGE_SHORT:
            out.append(("e", _edge_dirs(_EDGE_SHORT[c])))
            i += 1
        elif c in _LEVEL:
            out.append(("l", _LEVEL[c]))
            i += 1
        else:
            raise _Unsupported(f"unexpected character '{c}' in the table")
    return out


def _minimize(nv: int, on: set, dc: set) -> list:
    """Cubes ``(value, dcmask)`` covering ``on`` (may use ``dc``); [] means constant 0."""
    if not on:
        return []
    if len(on | dc) == 1 << nv:
        return [(0, (1 << nv) - 1)]
    cur = {(m, 0) for m in on | dc}
    primes = set()
    while cur:
        used = set()
        nxt = set()
        for v, mk in cur:
            for b in range(nv):
                bit = 1 << b
                if mk & bit or v & bit:
                    continue
                if (v | bit, mk) in cur:
                    used.add((v, mk))
                    used.add((v | bit, mk))
                    nxt.add((v, mk | bit))
        primes |= cur - used
        cur = nxt
    primes = sorted(primes)

    def covers(p, m):
        return (m & ~p[1]) == p[0]

    def lits(p):
        return nv - bin(p[1]).count("1")

    left = set(on)
    chosen = []
    for m in sorted(on):
        c = [p for p in primes if covers(p, m)]
        if len(c) == 1 and c[0] not in chosen:
            chosen.append(c[0])
    for p in chosen:
        left -= {m for m in left if covers(p, m)}
    while left:
        best = max(primes, key=lambda p: (sum(1 for m in left if covers(p, m)), -lits(p)))
        chosen.append(best)
        left -= {m for m in left if covers(best, m)}
    return sorted(chosen)


def _sop(cubes: list, names: list) -> str:
    if not cubes:
        return "1'b0"
    terms = []
    for v, mk in cubes:
        ls = [(n if (v >> i) & 1 else "~" + n) for i, n in enumerate(names) if not (mk >> i) & 1]
        if not ls:
            return "1'b1"
        terms.append("(" + " & ".join(ls) + ")")
    return " | ".join(terms)


def _combos(specs, positions):
    """All assignments (bit ``positions[k]`` = value of spec k) matched by a row."""
    sets = [(0, 1) if s[0] == "e" else s[1] for s in specs]
    for vals in itertools.product(*sets):
        yield sum(v << k for k, v in zip(positions, vals))


def _header(p, i):
    """Walk the tokens between the UDP name and ``table``: (name, ports in order, dirs, regs, table text)."""
    kinds, texts, offs = p.kinds, p.texts, p.offs
    n = len(kinds)
    if i + 1 >= n or kinds[i + 1] != "ID":
        raise _Unsupported("missing primitive name")
    name = texts[i + 1]
    j = i + 2
    order: list = []
    dirs: dict = {}
    regs: set = set()
    depth = 0
    cur_dir = None
    cur_reg = False
    while j < n and kinds[j] not in ("table", "endprimitive", "EOF"):
        k = kinds[j]
        if k == "(":
            depth += 1
        elif k == ")":
            depth -= 1
        elif k == "initial":
            while j < n and kinds[j] not in (";", "EOF"):
                j += 1
        elif k in ("input", "output", "inout"):
            cur_dir, cur_reg = k, False
        elif k == "reg":
            cur_reg = True
        elif k == ";":
            cur_dir, cur_reg = None, False
        elif k == "[":
            raise _Unsupported("vector port")
        elif k == "ID":
            nm = texts[j]
            if depth >= 1 and nm not in order:
                order.append(nm)
            if cur_dir:
                dirs[nm] = cur_dir
            if cur_reg:
                regs.add(nm)
        j += 1
    if j >= n or kinds[j] != "table":
        raise _Unsupported("no table")
    t = j
    while j < n and kinds[j] not in ("endtable", "endprimitive", "EOF"):
        j += 1
    if j >= n or kinds[j] != "endtable":
        raise _Unsupported("unterminated table")
    body = re.sub(r"//[^\n]*|/\*.*?\*/", " ", p.text[offs[t] + len("table"):offs[j]], flags=re.S)
    return name, order, dirs, regs, body


def _build(p, i):
    name, order, dirs, regs, table = _header(p, i)
    if len(order) < 2:
        raise _Unsupported("a primitive needs an output and at least one input")
    out, ins = order[0], order[1:]
    if dirs.get(out) != "output" or any(dirs.get(x) != "input" for x in ins):
        raise _Unsupported("unsupported port declarations")
    if not all(_NAME.match(nm) for nm in order):
        raise _Unsupported("unsupported port name")
    n = len(ins)
    if n > MAX_VARS + 1:
        raise _Unsupported(f"more than {MAX_VARS} inputs")
    seq = out in regs
    rows = []
    for ent in table.split(";"):
        if not ent.strip():
            continue
        parts = ent.split(":")
        if len(parts) not in (2, 3):
            raise _Unsupported("malformed table entry")
        specs = _parse_field(parts[0])
        if len(specs) != n:
            raise _Unsupported("table entry width does not match the input count")
        if len(parts) == 3:
            seq = True
        elif seq:
            raise _Unsupported("mixed combinational and sequential entries")
        cur = None
        if len(parts) == 3:
            cs = _parse_field(parts[1])
            if len(cs) != 1 or cs[0][0] != "l":
                raise _Unsupported("malformed current-state field")
            cur = cs[0][1]
        nx = parts[-1].strip()
        if len(nx) != 1 or nx not in "01xX-":
            raise _Unsupported("malformed output field")
        rows.append((specs, cur, nx))
    if not rows:
        raise _Unsupported("empty table")
    if seq != all(r[1] is not None for r in rows):
        raise _Unsupported("mixed combinational and sequential entries")
    hdr = f"module {name}(output{' reg' if seq else ''} {out}, input {', input '.join(ins)});\n"

    if not seq:
        on, off = set(), set()
        for specs, _cur, nx in rows:
            if any(s[0] == "e" for s in specs) or nx == "-":
                raise _Unsupported("edge or '-' entry in a combinational primitive")
            if nx in "xX":
                continue
            (on if nx == "1" else off).update(_combos(specs, list(range(n))))
        dc = set(range(1 << n)) - on - off
        return hdr + f"  assign {out} = {_sop(_minimize(n, on, dc), ins)};\nendmodule\n"

    # ---- sequential: classify, per data-input combination, how the state may evolve
    edge_rows = [r for r in rows if any(s[0] == "e" for s in r[0])]
    level_rows = [r for r in rows if not any(s[0] == "e" for s in r[0])]
    clk = pol = None
    eff = []
    for specs, cur, nx in edge_rows:
        if nx in "xX-":
            continue                      # holds / unknown: no effect on the synthesised state
        epos = [k for k, s in enumerate(specs) if s[0] == "e"]
        if len(epos) != 1:
            raise _Unsupported("entry with several edges")
        d = specs[epos[0]][1]
        if not d:
            continue
        if len(d) != 1:
            raise _Unsupported("entry sensitive to both clock edges")
        if clk is None:
            clk, pol = epos[0], next(iter(d))
        elif clk != epos[0] or pol != next(iter(d)):
            raise _Unsupported("several clock inputs or edges")
        eff.append((specs, cur, nx))
    if eff:
        if any(nx in "01" for _s, _c, nx in level_rows):
            raise _Unsupported("level entries mixed with edge entries (asynchronous set/reset)")
        use = []
        for specs, cur, nx in edge_rows:        # explicit holds on the clock edge are part of the function
            if nx == "-" and specs[clk][0] == "e" and pol in specs[clk][1] and \
                    sum(1 for x in specs if x[0] == "e") == 1:
                use.append((specs, cur, nx))
        use += eff
        data = [k for k in range(n) if k != clk]
    else:
        if edge_rows:
            raise _Unsupported("edge entries without an effective transition")
        use = level_rows
        data = list(range(n))
    nd = len(data)
    arr = ({}, {})
    for specs, cur, nx in use:
        v = "h" if nx == "-" else (None if nx in "xX" else int(nx))
        for combo in _combos([specs[k] for k in data], list(range(nd))):
            for c in cur:
                arr[c][combo] = v
    dnames = [ins[k] for k in data]
    cls = {}
    for combo in range(1 << nd):
        e0, e1 = arr[0].get(combo), arr[1].get(combo)
        e0 = 0 if e0 == "h" else e0
        e1 = 1 if e1 == "h" else e1
        sv = e0 in (1, None) and e1 in (1, None)       # forcing 1 is valid for both current values
        rv = e0 in (0, None) and e1 in (0, None)       # forcing 0
        hv = e0 in (0, None) and e1 in (1, None)       # holding
        if not (sv or rv or hv):
            raise _Unsupported("an entry toggles the state")
        cls[combo] = (sv, rv, hv)
    full = set(range(1 << nd))
    assign = "<=" if eff else "="
    if eff:
        head = f"always @({'posedge' if pol == 'pos' else 'negedge'} {ins[clk]})"
    else:
        head = "always @*"
    if not any(h and not s and not r for s, r, h in cls.values()):
        # no combination needs to hold: the next state is a function of the data inputs
        on = {c for c, (s, r, h) in cls.items() if s and not r}
        off = {c for c, (s, r, h) in cls.items() if r and not s}
        expr = _sop(_minimize(nd, on, full - on - off), dnames)
        body = f"{head} {out} {assign} {expr};"
    else:
        # update-enable E (must or may update) and the value F written when enabled
        e_on = {c for c, (s, r, h) in cls.items() if not h}
        e_off = {c for c, (s, r, h) in cls.items() if h and not s and not r}
        f_on = {c for c, (s, r, h) in cls.items() if s and not r}
        f_off = {c for c, (s, r, h) in cls.items() if r and not s}
        en = _sop(_minimize(nd, e_on, full - e_on - e_off), dnames)
        val = _sop(_minimize(nd, f_on, full - f_on - f_off), dnames)
        body = f"{head} if ({en}) {out} {assign} {val};"
    return hdr + f"  {body}\nendmodule\n"


def lower_udp(p, i):
    """See the module docstring.  ``p`` is the Parser positioned anywhere, ``i`` the ``primitive`` token."""
    from .parser import Parser
    try:
        text = _build(p, i)
    except _Unsupported as e:
        return None, str(e)
    file, line, _col = p.loc(i)
    nl = text.count("\n") + 1
    sub = Parser(text, lex(text), [file] * nl, [line] * nl, [], DiagSink(), p.limits, file)
    unit = sub.parse_unit()
    if sub.nerr or len(unit.modules) != 1 or unit.modules[0].incomplete:
        return None, "internal error lowering the table"
    m = unit.modules[0]
    m.kind = "primitive"
    return m, None
