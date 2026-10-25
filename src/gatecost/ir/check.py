"""Invariant checker and combinational-loop cutting (IR.md sections 6, 12)."""
from __future__ import annotations

from .core import Conn, ModuleIR, Net, Node
from .graph import _item_inputs, _sorted_conns, drivers_of_chunk, topo_order
from .sigspec import Const, SigSpec, Slice, mask


class IRInvariantError(AssertionError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("IR invariants violated:\n  " + "\n  ".join(problems))
        self.problems = problems


def _expect_widths(n: Node) -> list[str]:
    errs: list[str] = []
    op = n.op
    ins = {k: s for k, s in n.inputs}
    y = n.outputs[0].width if n.outputs else None

    def eq(what, a, b):
        if a != b:
            errs.append(f"node {n.id} {op}: {what} width {a} != expected {b}")

    if op in ("ADD", "SUB", "MUL"):
        eq("A", ins["A"].width, y); eq("B", ins["B"].width, y)
    elif op in ("NEG", "POW", "NOT"):
        eq("A", ins["A"].width, y)
    elif op in ("DIV", "MOD"):
        eq("B", ins["B"].width, ins["A"].width)
    elif op in ("SHL", "SHR", "SAR", "ROL", "ROR"):
        eq("A", ins["A"].width, y)
    elif op in ("AND", "OR", "XOR", "XNOR"):
        eq("A", ins["A"].width, y); eq("B", ins["B"].width, y)
    elif op in ("RED_AND", "RED_OR", "RED_XOR"):
        eq("Y", y, 1)
    elif op in ("EQ", "NE", "LT", "LE", "GT", "GE"):
        eq("Y", y, 1); eq("B", ins["B"].width, ins["A"].width)
    elif op == "MUX":
        eq("A", ins["A"].width, y); eq("B", ins["B"].width, y); eq("S", ins["S"].width, 1)
    elif op in ("PMUX", "PRIORITY"):
        k = n.attrs.get("k")
        eq("k", k, ins["S"].width); eq("A", ins["A"].width, y)
        eq("B", ins["B"].width, (k or 0) * (y or 0))
    elif op == "DECODE":
        if y > (1 << ins["A"].width):
            errs.append(f"node {n.id} DECODE: output wider than 2**input")
    elif op == "ROM":
        eq("Y", y, n.attrs.get("width"))
    elif op == "SUM":
        for k, s in n.inputs:
            eq(k, s.width, y)
    elif op == "DFF":
        eq("D", ins["D"].width, y); eq("CLK", ins["CLK"].width, 1)
        for k in ("EN", "SRST", "ARST"):
            if k in ins:
                eq(k, ins[k].width, 1)
    elif op == "LATCH":
        eq("D", ins["D"].width, y); eq("EN", ins["EN"].width, 1)
        if "RST" in ins:
            eq("RST", ins["RST"].width, 1)
    elif op == "MEM":
        for i in range(n.attrs.get("wports", 0)):
            eq(f"w{i}_data", ins[f"w{i}_data"].width, n.attrs["width"])
        for i, o in enumerate(n.outputs):
            eq(f"r{i}_data", o.width, n.attrs["width"])
    return errs


def check_module(m: ModuleIR) -> list[str]:
    """Return a list of invariant violations (empty when the IR is valid)."""
    errs: list[str] = []
    nets = {}
    for n in m.nets:
        if n.id in nets:
            errs.append(f"duplicate net id {n.id}")
        nets[n.id] = n
        if n.width < 1:
            errs.append(f"net {n.id} has width {n.width}")
    ids = set()
    for nd in m.nodes:
        if nd.id in ids:
            errs.append(f"duplicate node id {nd.id}")
        ids.add(nd.id)

    def chunks_ok(sig: SigSpec, what: str) -> None:
        for c in sig.chunks:
            if isinstance(c, Slice):
                if nets.get(c.net.id) is not c.net:
                    errs.append(f"{what}: references net {c.net.id} not in module")
                elif c.lo < 0 or c.width < 1 or c.lo + c.width > c.net.width:
                    errs.append(f"{what}: chunk [{c.lo}+:{c.width}] outside net {c.net.id} "
                                f"of width {c.net.width}")
            elif c.width < 1:
                errs.append(f"{what}: empty constant chunk")

    driven: dict[int, int] = {}

    def drive(net: Net, bits: int, who: str) -> None:
        cur = driven.get(net.id, 0)
        if cur & bits:
            errs.append(f"net {net.name or net.id}: multiple drivers ({who})")
        driven[net.id] = cur | bits

    for p in m.ports:
        if nets.get(p.id) is not p:
            errs.append(f"port {p.name} not in module nets")
        if p.port in ("in", "inout"):
            drive(p, mask(p.width), f"port {p.name}")
    for nd in m.nodes:
        for name, sig in nd.inputs:
            chunks_ok(sig, f"node {nd.id} {nd.op}.{name}")
        for o in nd.outputs:
            if nets.get(o.id) is not o:
                errs.append(f"node {nd.id} output net {o.id} not in module")
            if o.driver is not nd:
                errs.append(f"net {o.id} driver is not node {nd.id}")
            drive(o, mask(o.width), f"node {nd.id}")
        errs.extend(_expect_widths(nd))
    for c in m.conns:
        chunks_ok(c.src, "connection source")
        if nets.get(c.net.id) is not c.net:
            errs.append(f"connection destination net {c.net.id} not in module")
            continue
        if c.lo < 0 or c.lo + c.width > c.net.width:
            errs.append(f"connection destination out of range on net {c.net.id}")
            continue
        drive(c.net, mask(c.width) << c.lo, "connection")
    _, cyc = topo_order(m)
    if cyc:
        errs.append(f"combinational loop through {len(cyc)} items")
    return errs


def cut_loops(m: ModuleIR) -> int:
    """Report and cut combinational loops.  Returns the number of cuts.

    The cut replaces one dependency of the loop by a fresh undriven net, i.e. the loop
    is broken at a register-like boundary; ``COMBINATIONAL_LOOP`` is reported.
    """
    cuts = 0
    for _ in range(100_000):
        sconns = _sorted_conns(m)
        _, cyc = topo_order(m, sconns)
        if not cyc:
            return cuts
        cycset = {id(x): x for x in cyc}
        # dependency adjacency inside the leftover set
        deps: dict[int, list] = {}
        for it in cyc:
            lst = []
            for sig in _item_inputs(it):
                for ch in sig.chunks:
                    if isinstance(ch, Slice):
                        for d in drivers_of_chunk(ch, sconns):
                            if id(d) in cycset:
                                lst.append((d, ch))
            deps[id(it)] = lst
        # find a cycle by DFS from the first item
        state: dict[int, int] = {}
        # id(item) -> its index in ``path``; written once, when the item is pushed (state 0 -> 1).  An
        # item reached with state 1 is still on the path, so its recorded index is current: O(1)
        # instead of rebuilding ``[id(x) for x in path]`` and scanning it.
        path_pos: dict[int, int] = {}
        found = None
        cyc_path: list = []
        for start in cyc:
            if id(start) in state:
                continue
            stack = [(start, 0)]
            path = [start]
            state[id(start)] = 1
            path_pos[id(start)] = 0
            while stack and found is None:
                it, i = stack[-1]
                lst = deps[id(it)]
                if i >= len(lst):
                    state[id(it)] = 2
                    stack.pop()
                    path.pop()
                    continue
                stack[-1] = (it, i + 1)
                d, ch = lst[i]
                st = state.get(id(d), 0)
                if st == 1:
                    found = (it, d, ch)
                elif st == 0:
                    state[id(d)] = 1
                    stack.append((d, 0))
                    path_pos[id(d)] = len(path)
                    path.append(d)
            if found is not None:
                cyc_path = path[path_pos[id(found[1])]:]
                break
        if found is None:
            return cuts
        it, d, ch = found
        # a connection with a multi-chunk source is one dependency item: reading one bit of its net also
        # "reads" the other chunks (``ent <- {v, f(ent[32])}``).  Split such connections of the cycle by
        # chunk and look again before declaring a loop.
        wide = [x for x in cyc_path if isinstance(x, Conn) and len(x.src.chunks) > 1]
        if wide:
            _split_conns(m, wide)
            continue
        _cut_edge(m, it, ch)
        cuts += 1
    return cuts


def _conn_parts(conn: Conn) -> list[Conn]:
    """One connection per source chunk of ``conn`` (same destination bits, same origin)."""
    parts = []
    pos = 0
    for c in conn.src.chunks:
        parts.append(Conn(conn.net, conn.lo + pos, SigSpec((c,)), conn.origin))
        pos += c.width
    return parts


def _split_conn(m: ModuleIR, conn: Conn) -> None:
    """Replace ``conn`` by one connection per source chunk (same destination bits, same origin)."""
    _split_conns(m, [conn])


def _split_conns(m: ModuleIR, conns: list[Conn]) -> None:
    """Split every connection of ``conns`` in place, in one pass over ``m.conns``.

    Same result and order as splicing them one at a time (each is replaced by its parts at its own
    position), but O(len(m.conns)) instead of O(len(conns) * len(m.conns)).
    """
    repl: dict[int, list[Conn]] = {}
    for conn in conns:
        if id(conn) not in repl:
            repl[id(conn)] = _conn_parts(conn)
    m.conns[:] = _spliced(m.conns, repl)                       # in place: aliases stay valid
    for net_id in dict.fromkeys(c.net.id for c in conns):     # first-seen order, each net once
        lst = m.conns_by_net[net_id]
        lst[:] = _spliced(lst, repl)


def _spliced(lst: list[Conn], repl: dict[int, list[Conn]]) -> list[Conn]:
    out: list[Conn] = []
    for x in lst:
        r = repl.get(id(x))
        if r is None:
            out.append(x)
        else:
            out.extend(r)
    return out


def _cut_edge(m: ModuleIR, item, chunk: Slice) -> None:
    fresh = Net(m.next_net_id, chunk.width, False, f"_loopcut{m.next_net_id}", None)
    m.next_net_id += 1
    fresh.attrs["loop_cut"] = True
    m.nets.append(fresh)

    def rewrite(sig: SigSpec) -> SigSpec:
        cs = []
        done = False
        for c in sig.chunks:
            if not done and isinstance(c, Slice) and c is chunk or (
                    not done and isinstance(c, Slice) and c.net is chunk.net
                    and c.lo == chunk.lo and c.width == chunk.width):
                cs.append(Slice(fresh, 0, chunk.width))
                done = True
            else:
                cs.append(c)
        return SigSpec(cs)

    chunk.net.fanout -= 1
    fresh.fanout += 1
    if isinstance(item, Node):
        new_inputs = []
        replaced = False
        for name, sig in item.inputs:
            if not replaced and any(c is chunk or (isinstance(c, Slice) and c.net is chunk.net
                                                  and c.lo == chunk.lo and c.width == chunk.width)
                                    for c in sig.chunks):
                sig = rewrite(sig)
                replaced = True
            new_inputs.append((name, sig))
        item.inputs = tuple(new_inputs)
        where = item.origin
    else:
        item.src = rewrite(item.src)
        where = item.origin
    m.diag("COMBINATIONAL_LOOP",
           f"combinational loop through net {chunk.net.name or chunk.net.id}; cut at that signal",
           "warning", True, where)
