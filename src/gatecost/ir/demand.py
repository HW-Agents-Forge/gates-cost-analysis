"""Dead-logic elimination (IR.md 6.2) and demanded bits (IR.md 6.3)."""
from __future__ import annotations

from .core import Conn, ModuleIR, Node
from .knownbits import sig_known
from .sigspec import Const, SigSpec, Slice, mask

_ROOT_OPS = ("INST", "BLACKBOX")        # always live; a MEM is live only when some read port is observed


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
    cget = conns_of.get
    work: list = []
    queued: set[int] = set()
    qadd = queued.add
    wpush = work.append
    Node_ = Node
    Slice_ = Slice

    def add(sig: SigSpec, mk: int) -> None:
        pos = 0
        for c in sig.chunks:
            w = c.width
            if c.__class__ is Slice_:
                n = c.net
                lo = c.lo
                new = (((mk >> pos) & ((1 << w) - 1)) << lo) & ~n.demand
                if new:
                    n.demand |= new
                    d = n.driver
                    if d.__class__ is Node_ and id(d) not in queued:
                        qadd(id(d))
                        wpush(d)
                    cl = cget(n.id)
                    if cl:
                        hi = lo + w
                        for cn in cl:
                            if cn.lo < hi and cn.lo + cn.src.width > lo and id(cn) not in queued:
                                qadd(id(cn))
                                wpush(cn)
            pos += w

    for p in m.ports:
        if p.port in ("out", "inout"):
            add(p.sig, mask(p.width))
    for nd in m.nodes:
        if nd.op in _ROOT_OPS:
            for s, mk in _full_inputs(nd):
                add(s, mk)
    pop = work.pop
    discard = queued.discard
    while work:
        it = pop()
        discard(id(it))
        if it.__class__ is Conn:
            dm = (it.net.demand >> it.lo) & ((1 << it.src.width) - 1)
            if dm:
                add(it.src, dm)
            continue
        nd = it
        op = nd.op
        if op == "DFF" or op == "LATCH":
            if nd.outputs[0].demand:
                for s, mk in _full_inputs(nd):
                    add(s, mk)
            continue
        if op in _ROOT_OPS:
            continue
        if op == "MEM":
            if any(o.demand for o in nd.outputs):
                for s, mk in _full_inputs(nd):
                    add(s, mk)
            continue
        dy = nd.outputs[0].demand
        if not dy:
            continue
        inp = nd.inputs
        if op == "ADD" or op == "SUB":
            low = (1 << dy.bit_length()) - 1
            s = inp[0][1]
            add(s, low & ((1 << s.width) - 1))
            s = inp[1][1]
            add(s, low & ((1 << s.width) - 1))
        elif op == "XOR" or op == "XNOR":
            s = inp[0][1]
            add(s, dy & ((1 << s.width) - 1))
            s = inp[1][1]
            add(s, dy & ((1 << s.width) - 1))
        elif op == "MUX":
            sa = inp[0][1]
            sb = inp[1][1]
            ss = inp[2][1]
            sk, sv = sig_known(ss)
            da = db = dy
            if sk & 1:
                if sv & 1:
                    da = 0
                else:
                    db = 0
            else:
                add(ss, 1)
            add(sa, da & ((1 << sa.width) - 1))
            add(sb, db & ((1 << sb.width) - 1))
        else:
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
    dead: set = set()
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
            _replace_with_const(m, nd, Const(dv, w), dead)
            removed += 1
    if dead:
        m.nodes = [x for x in m.nodes if id(x) not in dead]
    compute_demand(m)
    live_nodes = []
    for nd in m.nodes:
        if node_live(nd):
            live_nodes.append(nd)
        else:
            nd.alive = False
            removed += 1
            if nd.op in ("DFF", "LATCH", "MEM"):
                m.unobservable["regs"] += 1
            elif nd.op != "NOT":
                m.unobservable["ops"] += 1
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


def _replace_with_const(m: ModuleIR, nd: Node, c: Const, dead: set | None = None) -> None:
    out = nd.outputs[0]
    nd.alive = False
    nd.outputs = ()
    if dead is None:
        m.nodes = [x for x in m.nodes if x is not nd]
    else:
        dead.add(id(nd))
    out.driver = None
    conn = Conn(out, 0, SigSpec((c,)), nd.origin)
    m.conns.append(conn)
    m.conns_by_net.setdefault(out.id, []).append(conn)
    m.diag("CONSTANT_REGISTER",
           f"register {out.name or out.id} is constant {c.value:#x}; costed as a constant",
           "info", False, nd.origin)
