"""Dependency graph utilities: deterministic topological order, consumers."""
from __future__ import annotations

import heapq
from bisect import bisect_right

from .core import Conn, ModuleIR, Node
from .sigspec import SigSpec, Slice


def _item_inputs(item) -> list[SigSpec]:
    if isinstance(item, Node):
        return [s for _, s in item.inputs]
    return [item.src]


def _sorted_conns(m: ModuleIR) -> dict[int, tuple[list[int], list[Conn]]]:
    out = {}
    for nid, lst in m.conns_by_net.items():
        cs = sorted(lst, key=lambda c: c.lo)
        out[nid] = ([c.lo for c in cs], cs)
    return out


def drivers_of_chunk(chunk: Slice, sconns) -> list:
    """Combinational driver items (Node or Conn) of the bits of a chunk."""
    net = chunk.net
    d = net.driver
    res = []
    if isinstance(d, Node):
        if not d.is_state:
            res.append(d)
    ent = sconns.get(net.id)
    if ent is not None:
        los, cs = ent
        lo, hi = chunk.lo, chunk.lo + chunk.width
        # conns are disjoint and sorted by lo; find the first that may overlap
        i = bisect_right(los, lo) - 1
        if i < 0:
            i = 0
        while i < len(cs):
            c = cs[i]
            if c.lo >= hi:
                break
            if c.lo + c.width > lo:
                res.append(c)
            i += 1
    return res


def topo_order(m: ModuleIR, sconns=None):
    """Deterministic topological order of all nodes and connections.

    State elements (DFF, LATCH, MEM, INST, BLACKBOX) do not propagate dependencies
    from inputs to outputs, so registers break feedback.  Returns ``(order, cyclic)``
    where ``cyclic`` lists items that could not be ordered (combinational loops and
    everything downstream of them).
    """
    if sconns is None:
        sconns = _sorted_conns(m)
    items: list = list(m.nodes) + list(m.conns)
    idx = {}
    for i, it in enumerate(items):
        idx[id(it)] = i
    npred = [0] * len(items)
    succ: list[list[int]] = [[] for _ in items]
    for i, it in enumerate(items):
        seen = set()
        for sig in _item_inputs(it):
            for ch in sig.chunks:
                if isinstance(ch, Slice):
                    for d in drivers_of_chunk(ch, sconns):
                        j = idx.get(id(d))
                        if j is None or j in seen:
                            continue
                        seen.add(j)
                        succ[j].append(i)
                        npred[i] += 1
    heap = [i for i in range(len(items)) if npred[i] == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        i = heapq.heappop(heap)
        order.append(items[i])
        for k in succ[i]:
            npred[k] -= 1
            if npred[k] == 0:
                heapq.heappush(heap, k)
    if len(order) == len(items):
        return order, []
    placed = {id(x) for x in order}
    return order, [it for it in items if id(it) not in placed]


def item_origin(item):
    return item.origin


def consumers(m: ModuleIR) -> dict[int, list[tuple]]:
    """net id -> list of ``(consumer item, input name, chunk position, chunk)``."""
    res: dict[int, list] = {}
    for n in m.nodes:
        for name, sig in n.inputs:
            for pos, ch in enumerate(sig.chunks):
                if isinstance(ch, Slice):
                    res.setdefault(ch.net.id, []).append((n, name, pos, ch))
    for c in m.conns:
        for pos, ch in enumerate(c.src.chunks):
            if isinstance(ch, Slice):
                res.setdefault(ch.net.id, []).append((c, "src", pos, ch))
    return res
