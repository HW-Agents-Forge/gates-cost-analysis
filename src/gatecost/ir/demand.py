"""Dead-logic elimination (IR.md 6.2) and demanded bits (IR.md 6.3)."""
from __future__ import annotations

from .core import Conn, ModuleIR, Node
from .knownbits import sig_known
from .sigspec import Const, SigSpec, Slice, mask

_ROOT_OPS = ("INST", "BLACKBOX", "MEM")


def _clip(sig: SigSpec, m: int) -> int:
    return m & mask(sig.width)


def operand_demands(nd: Node, dy: int) -> list[tuple[SigSpec, int]]:
    """Demanded mask of every operand of a combinational node, given the demand ``dy``
    on its (first) output."""
    op = nd.op
    ins = dict(nd.inputs)
    res: list[tuple[SigSpec, int]] = []
    if dy == 0:
        return res
    k = dy.bit_length()
    low = mask(k)
    if op in ("ADD", "SUB", "NEG", "MUL", "POW", "SUM"):
        for _, s in nd.inputs:
            res.append((s, _clip(s, low)))
    elif op in ("DIV", "MOD"):
        for _, s in nd.inputs:
            res.append((s, mask(s.width)))
    elif op == "SHL":
        res.append((ins["A"], _clip(ins["A"], low)))
        res.append((ins["B"], mask(ins["B"].width)))
    elif op in ("SHR", "SAR", "ROL", "ROR"):
        for _, s in nd.inputs:
            res.append((s, mask(s.width)))
    elif op in ("AND", "OR"):
        ak, av = sig_known(ins["A"])
        bk, bv = sig_known(ins["B"])
        if op == "AND":
            res.append((ins["A"], dy & ~(bk & ~bv) & mask(ins["A"].width)))
            res.append((ins["B"], dy & ~(ak & ~av) & mask(ins["B"].width)))
        else:
            res.append((ins["A"], dy & ~(bk & bv) & mask(ins["A"].width)))
            res.append((ins["B"], dy & ~(ak & av) & mask(ins["B"].width)))
    elif op in ("XOR", "XNOR"):
        res.append((ins["A"], _clip(ins["A"], dy)))
        res.append((ins["B"], _clip(ins["B"], dy)))
    elif op == "NOT":
        res.append((ins["A"], _clip(ins["A"], dy)))
    elif op in ("RED_AND", "RED_OR", "RED_XOR", "DECODE", "ENCODE", "ROM"):
        res.append((ins["A"], mask(ins["A"].width)))
    elif op in ("EQ", "NE", "LT", "LE", "GT", "GE"):
        res.append((ins["A"], mask(ins["A"].width)))
        res.append((ins["B"], mask(ins["B"].width)))
    elif op == "MUX":
        sk, sv = sig_known(ins["S"])
        da = db = dy
        if sk & 1:
            if sv & 1:
                da = 0
            else:
                db = 0
        else:
            res.append((ins["S"], 1))
        res.append((ins["A"], _clip(ins["A"], da)))
        res.append((ins["B"], _clip(ins["B"], db)))
    elif op in ("PMUX", "PRIORITY"):
        kk = nd.attrs["k"]
        w = nd.outputs[0].width
        sk, sv = sig_known(ins["S"])
        res.append((ins["S"], mask(kk)))
        res.append((ins["A"], _clip(ins["A"], dy)))
        bm = 0
        for i in range(kk):
            if (sk >> i) & 1 and not (sv >> i) & 1:
                continue
            bm |= (dy & mask(w)) << (i * w)
        res.append((ins["B"], _clip(ins["B"], bm)))
    return res


def _full_inputs(nd: Node) -> list[tuple[SigSpec, int]]:
    return [(s, mask(s.width)) for _, s in nd.inputs]


def compute_demand(m: ModuleIR) -> None:
    """Backward worklist propagation of demanded bits from the roots (output ports,
    instance/black-box/memory inputs, and the inputs of live registers)."""
    for n in m.nets:
        n.demand = 0
    conns_of = m.conns_by_net
    work: list = []
    queued: set[int] = set()

    def push(item) -> None:
        if id(item) not in queued:
            queued.add(id(item))
            work.append(item)

    def add(sig: SigSpec, mk: int) -> None:
        pos = 0
        for c in sig.chunks:
            w = c.width
            if isinstance(c, Slice):
                bits = (mk >> pos) & mask(w)
                n = c.net
                new = (bits << c.lo) & ~n.demand
                if new:
                    n.demand |= new
                    d = n.driver
                    if isinstance(d, Node):
                        push(d)
                    for cn in conns_of.get(n.id, ()):
                        if cn.lo < c.lo + w and cn.lo + cn.width > c.lo:
                            push(cn)
            pos += w

    for p in m.ports:
        if p.port in ("out", "inout"):
            add(p.sig, mask(p.width))
    for nd in m.nodes:
        if nd.op in _ROOT_OPS:
            for s, mk in _full_inputs(nd):
                add(s, mk)
    while work:
        it = work.pop()
        queued.discard(id(it))
        if isinstance(it, Conn):
            dm = (it.net.demand >> it.lo) & mask(it.width)
            if dm:
                add(it.src, dm)
            continue
        nd = it
        if nd.op in ("DFF", "LATCH"):
            if nd.outputs[0].demand:
                for s, mk in _full_inputs(nd):
                    add(s, mk)
            continue
        if nd.op in _ROOT_OPS:
            continue
        dy = nd.outputs[0].demand
        for s, mk in operand_demands(nd, dy):
            add(s, mk)


def node_live(nd: Node) -> bool:
    if nd.op in _ROOT_OPS:
        return True
    return any(o.demand for o in nd.outputs)


def dead_logic(m: ModuleIR, order) -> int:
    """Remove unreachable nodes/connections; replace constant registers by constants.
    Returns the number of removed nodes."""
    removed = 0
    # registers whose D is constant equal to the reset value
    for nd in list(m.nodes):
        if nd.op != "DFF":
            continue
        a = nd.attrs
        kind = a.get("reset_kind", "none")
        if kind == "none":
            continue
        ins = dict(nd.inputs)
        dk, dv = sig_known(ins["D"])
        w = nd.outputs[0].width
        if dk != mask(w):
            continue
        vals = []
        if kind in ("async", "sync", "both"):
            vals.append(a["reset_value"])
        if kind == "both":
            vals.append(a["srst_value"])
        ok = all(not ((dv ^ v.value) & v.kmask) for v in vals)
        if ok:
            _replace_with_const(m, nd, Const(dv, w))
            removed += 1
    compute_demand(m)
    live_nodes = []
    for nd in m.nodes:
        if node_live(nd):
            live_nodes.append(nd)
        else:
            nd.alive = False
            removed += 1
            for o in nd.outputs:
                o.driver = None
    m.nodes = live_nodes
    keep = []
    for c in m.conns:
        if (c.net.demand >> c.lo) & mask(c.width):
            keep.append(c)
    m.conns = keep
    cb: dict[int, list] = {}
    for c in keep:
        cb.setdefault(c.net.id, []).append(c)
    m.conns_by_net = cb
    # drop nets that are no longer referenced
    m.recount_fanout()
    referenced = set()
    for p in m.ports:
        referenced.add(p.id)
    for nd in m.nodes:
        for o in nd.outputs:
            referenced.add(o.id)
        for _, s in nd.inputs:
            for c in s.chunks:
                if isinstance(c, Slice):
                    referenced.add(c.net.id)
    for c in m.conns:
        referenced.add(c.net.id)
        for ch in c.src.chunks:
            if isinstance(ch, Slice):
                referenced.add(ch.net.id)
    m.nets = [n for n in m.nets if n.id in referenced]
    m.driven = {nid: v for nid, v in m.driven.items() if nid in referenced}
    return removed


def _replace_with_const(m: ModuleIR, nd: Node, c: Const) -> None:
    out = nd.outputs[0]
    nd.alive = False
    nd.outputs = ()
    m.nodes = [x for x in m.nodes if x is not nd]
    out.driver = None
    conn = Conn(out, 0, SigSpec((c,)), nd.origin)
    m.conns.append(conn)
    m.conns_by_net.setdefault(out.id, []).append(conn)
    m.diag("CONSTANT_REGISTER",
           f"register {out.name or out.id} is constant {c.value:#x}; costed as a constant",
           "info", False, nd.origin)
