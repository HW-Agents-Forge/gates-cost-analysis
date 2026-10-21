"""Stable textual dump of a Module IR (IR.md section 9)."""
from __future__ import annotations

import heapq

from .core import INPUT_NAMES, Conn, ModuleIR, Net, Node
from .graph import _item_inputs, _sorted_conns, drivers_of_chunk
from .keys import H, compute_keys, ser
from .sigspec import Const, SigSpec, Slice, mask


def _hex(v: int) -> str:
    return f"{v:x}"


def fmt_const(c: Const) -> str:
    if c.xmask:
        bits = "".join("x" if (c.xmask >> i) & 1 else str((c.value >> i) & 1)
                       for i in range(c.width - 1, -1, -1))
        return f"{c.width}'b{bits}"
    return f"{c.width}'h{_hex(c.value)}"


class _Namer:
    def __init__(self, normalize: bool) -> None:
        self.normalize = normalize
        self.names: dict[int, str] = {}
        self.counter = 0
        self.used: set[str] = set()

    def name(self, net: Net) -> str:
        n = self.names.get(net.id)
        if n is not None:
            return n
        if net.port or (net.name and not self.normalize and net.name not in self.used
                        and not net.attrs.get("loop_cut")):
            n = net.name
        else:
            self.counter += 1
            n = f"n{self.counter}"
        self.used.add(n)
        self.names[net.id] = n
        return n

    def chunk(self, c) -> str:
        if isinstance(c, Const):
            return fmt_const(c)
        nm = self.name(c.net)
        if c.width == 1:
            return f"{nm}[{c.lo}]"
        return f"{nm}[{c.lo + c.width - 1}:{c.lo}]"

    def sig(self, s: SigSpec) -> str:
        if not s.chunks:
            return "{}"
        if len(s.chunks) == 1:
            return self.chunk(s.chunks[0])
        return "{" + ", ".join(self.chunk(c) for c in reversed(s.chunks)) + "}"

    def netdecl(self, n: Net) -> str:
        return f"{self.name(n)}[{n.width - 1}:0]"


def _fmt_attr(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, Const):
        return fmt_const(v)
    if isinstance(v, (tuple, list)):
        if len(v) > 16 and all(isinstance(x, int) for x in v):
            return "#" + H(ser(v)).hex()[:8]
        return "[" + ",".join(_fmt_attr(x) for x in v) + "]"
    if v is None:
        return "none"
    return str(v)


def _canonical_order(m: ModuleIR) -> list:
    """Topological order with ties broken by shape key (IR.md section 9)."""
    sconns = _sorted_conns(m)
    items: list = list(m.nodes) + list(m.conns)
    idx = {id(it): i for i, it in enumerate(items)}
    npred = [0] * len(items)
    succ: list[list[int]] = [[] for _ in items]
    for i, it in enumerate(items):
        seen = set()
        for sig in _item_inputs(it):
            for ch in sig.chunks:
                if isinstance(ch, Slice):
                    for d in drivers_of_chunk(ch, sconns):
                        j = idx[id(d)]
                        if j not in seen:
                            seen.add(j)
                            succ[j].append(i)
                            npred[i] += 1
    ck = getattr(m, "conn_keys", {})

    def prio(i: int):
        it = items[i]
        if isinstance(it, Node):
            return (0, it.shape or b"", repr(it.named_key), it.id)
        return (1, ck.get(id(it), b""), it.net.name or "", it.lo)

    heap = [(prio(i), i) for i in range(len(items)) if npred[i] == 0]
    heapq.heapify(heap)
    out = []
    while heap:
        _, i = heapq.heappop(heap)
        out.append(items[i])
        for k in succ[i]:
            npred[k] -= 1
            if npred[k] == 0:
                heapq.heappush(heap, (prio(k), k))
    placed = {id(x) for x in out}
    out.extend(sorted((it for it in items if id(it) not in placed),
                      key=lambda it: (isinstance(it, Conn), getattr(it, "id", 0))))
    return out


def dump(m: ModuleIR, normalize: bool = False, analysis: bool = True) -> str:
    """Return the stable textual dump.  ``normalize=True`` drops internal net names and
    source locations (used to compare front-ends)."""
    if m.canonical_hash is None:
        compute_keys(m)
    nm = _Namer(normalize)
    lines: list[str] = []
    rest = m.key[len(m.name):]
    hdr = m.name
    if rest.startswith("("):
        j = rest.index(")") + 1
        hdr += " " + rest[:j]
        rest = rest[j:]
    if rest.startswith("["):
        hdr += " " + rest
    lines.append(f"module {hdr}")
    if m.is_blackbox:
        lines.append(f"  blackbox limit={m.limit_reached}")
    for p in (sorted(m.ports, key=lambda x: x.name or "") if normalize else m.ports):
        lines.append(f"  port {p.port:<3} {nm.netdecl(p)}")
    order = _canonical_order(m)
    groups: dict[int, int] = {}

    def gid(g: int) -> str:
        if g not in groups:
            groups[g] = len(groups) + 1
        return f"g{groups[g]}"

    node_lines: list[Node] = []
    for it in order:
        if isinstance(it, Conn):
            dst = SigSpec.of(it.net, it.lo, it.width)
            lines.append(f"  {nm.sig(dst)} <- {nm.sig(it.src)}")
            continue
        nd = it
        node_lines.append(nd)
        if nd.op in INPUT_NAMES:
            args = ", ".join(nm.sig(s) for _, s in nd.inputs)
        else:
            args = ", ".join(f"{k}={nm.sig(s)}" for k, s in nd.inputs)
        attrs = []
        for k in sorted(nd.attrs):
            v = nd.attrs[k]
            if k == "merge_count" and v == 1:
                continue
            if k in ("rstyles", "out_ports"):
                continue
            if k in ("inst", "mem_name") and normalize:
                continue
            attrs.append(f"{k}={_fmt_attr(v)}")
        for k in sorted(nd.tags):
            v = nd.tags[k]
            if k == "group":
                attrs.append(f"group={gid(v)}")
            elif k == "shared":
                attrs.append(f"shared={gid(v)}")
            elif k == "members":
                attrs.append(f"members={len(v)}")
            elif k == "fused":
                attrs.append("fused=1")
            elif k == "share_set":
                continue
            else:
                attrs.append(f"{k}={_fmt_attr(v)}")
        outs = ", ".join(nm.netdecl(o) for o in nd.outputs) or "-"
        line = f"  net {outs} = {nd.op}({args})"
        if attrs:
            line += " " + " ".join(attrs)
        if nd.origin is not None and not normalize:
            line += f"   @{nd.origin.short()}"
        lines.append(line)
    if analysis and m.analyzed:
        for nd in node_lines:
            for o in nd.outputs:
                lines.append(_analysis_line(nm, nd, o))
    return "\n".join(lines) + "\n"


def _known_str(n: Net) -> str:
    if n.kmask == 0:
        return "none"
    if n.width <= 64:
        return "".join("x" if not (n.kmask >> i) & 1 else str((n.kval >> i) & 1)
                       for i in range(n.width - 1, -1, -1))
    return f"mask={n.kmask:#x},val={n.kval:#x}"


def _analysis_line(nm: _Namer, nd: Node, o: Net) -> str:
    name = nm.name(o)
    w = (nd.eff or {}).get("w")
    s = f"  known {name}: {_known_str(o)}  demand {name}: {o.demand:#x}"
    if w is not None and o is nd.outputs[0]:
        s += f"  eff_w({nd.op})={w}"
    return s
