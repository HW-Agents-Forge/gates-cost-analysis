"""Select trees (IR.md 6, annotation only): nested ``MUX`` / ``PMUX`` / ``PRIORITY`` nodes flattened per bit.

The lowering of nested ``if`` / ``case`` statements produces trees of mux-family nodes in which every inner node
repeats the "hold" value: ``c1 ? (c2 ? a : h) : h``.  Costed node by node, every level is a full-width mux, whereas
the function is ``(c1 & c2) ? a : h``: one 2:1 mux per bit and one AND on the 1-bit selects.

A mux-family node is *internal* when its output has a single consumer reference (``fanout == 1``, possibly through
whole-net wiring copies), and that reference is a **data** input (``A`` or ``B``, inside a single arm) of another
mux-family node -- never a select, never a port, never a shared node.  A *root* is a mux-family node that is not
internal and has at least one internal node below it; the root and the internal nodes reached from it form a tree.

Flattening (exact, per output bit ``b`` of the root that is demanded and not known): every bit of the tree output
is the leaf reached by the unique path whose conditions hold.  The *position* of a leaf occurrence is the path of
arm indices from the root (``MUX``: 0 = ``A``/else, 1 = ``B``/then; ``PMUX``/``PRIORITY``: ``j`` = case ``j``,
``k`` = default).  Its condition is the conjunction, along the path, of the arm conditions (``MUX``: ``S`` /
``~S``; ``PMUX`` case ``j``: ``s_j``, default: no ``s``; ``PRIORITY`` case ``j``: ``s_j`` and no earlier ``s``,
default: no ``s``).  The conditions of the positions partition the input space (one-hot ``PMUX`` selects assumed,
as everywhere in the IR), so the bit equals ``OR_leaf (OR_{p in pos(leaf)} cond(p)) & leaf``: a one-hot select of
its *distinct* leaves.  Leaves are net bits (resolved through wiring connections, so the same bit reached under
two names is one leaf), known constant bits (0 / 1), or don't-care bits (dropped: they may take any value).

For every tree the pass records, in ``ModuleIR.mux_trees`` (plain data, consumed by the cost model):

* ``root``: node id; ``nodes``: ids of the absorbed internal nodes (their cost moves to the root);
* ``bits``: histogram ``[V, c0, c1, dflt, count]`` of the costed root bits with at least two distinct leaves:
  ``V`` distinct variable leaves, ``c0`` / ``c1`` whether the constants 0 / 1 are leaves, ``dflt`` the kind of
  the leaf chosen as the default of the bit (2 = variable, 0 / 1 = that constant): the leaf with the most
  positions (ties: the smallest sorted position list), whose condition is never needed;
* ``gates``: ``[id, AND2, OR2]`` the 2-input gates on 1-bit signals that build the conditions of the other leaves,
  shared by every bit: one AND per non-root position on the path to a needed position (``act(parent) & arm
  condition``), ``k - 1`` masking ANDs per ``PRIORITY`` node with a needed position, ``k - 1`` ORs for a needed
  non-root ``PMUX`` default (``~|s``), and ``n - 1`` ORs per distinct set of ``n > 1`` positions carrying one
  non-default leaf.  A gate is identified (``id``, local to the module) by its logical condition -- the select
  signals and arm indices along its path -- so the trees of the signals assigned by one statement, which test the
  same conditions, share them; ``sel_and`` / ``sel_or`` are the per-tree totals before sharing.

The pass changes no structure, attribute, tag, key or hash: a design without such trees is analyzed exactly as
before.  Positions are structural (arm indices), never node or net ids, so renaming and reordering the source
leave the records unchanged.  Linear: every (node, bit) of a tree is visited once.
"""
from __future__ import annotations

from bisect import bisect_right

from .core import ModuleIR, Net, Node
from .graph import consumers
from .knownbits import sig_known
from .sigspec import Const, SigSpec, Slice, mask

MUX_FAMILY = ("MUX", "PMUX", "PRIORITY")
_DEPTH = 64          # wiring hops followed when resolving a bit or bridging a connection

#: a leaf of a flattened bit: ``("c", 0 | 1)`` constant, ``("n", net id, bit)`` net bit, ``("x",)`` don't care
Leaf = tuple
#: arm indices from the root to an arm of a tree node
Path = tuple[int, ...]


def _is_mux(nd: object) -> bool:
    return (nd.__class__ is Node and nd.op in MUX_FAMILY and nd.alive and len(nd.outputs) == 1
            and not nd.attrs.get("tristate"))


def _arm_of(item: Node, name: str, pos: int, chunk: Slice) -> int | None:
    """Arm index of the data chunk ``pos`` of input ``name`` of a mux-family node, or None (select, or a chunk
    that straddles two arms of ``B``)."""
    if name == "S":
        return None
    if item.op == "MUX":
        return 0 if name == "A" else 1 if name == "B" else None
    k = int(item.attrs.get("k", 0))
    if name == "A":
        return k
    if name != "B":
        return None
    w = item.outputs[0].width
    if w <= 0:
        return None
    off = 0
    sig = item.input("B")
    for c in sig.chunks[:pos]:
        off += c.width
    arm = off // w
    if arm >= k or off + chunk.width > (arm + 1) * w:
        return None
    return arm


def _sole_data_use(nd: Node, cons: dict) -> tuple[Node, int] | None:
    """``(parent, arm)`` when the only use of ``nd``'s output is one data arm of another mux-family node (through
    whole-net wiring copies), else None."""
    cur: Net = nd.outputs[0]
    for _ in range(_DEPTH):
        if cur.port or cur.fanout != 1:
            return None
        refs = cons.get(cur.id, ())
        if len(refs) != 1:
            return None
        item, name, pos, chunk = refs[0]
        if item.__class__ is Node:
            if not _is_mux(item) or item is nd:
                return None
            arm = _arm_of(item, name, pos, chunk)
            return None if arm is None else (item, arm)
        # a connection: only an exact whole-net copy is followed
        if (item.lo != 0 or item.width != item.net.width or len(item.src.chunks) != 1
                or chunk.lo != 0 or chunk.width != cur.width):
            return None
        cur = item.net
    return None


def _live_arms(nd: Node) -> list[tuple[int, SigSpec]]:
    """Data arms that can be selected, in order: ``(arm index, SigSpec)``."""
    ins = dict(nd.inputs)
    sk, sv = sig_known(ins["S"])
    if nd.op == "MUX":
        if sk & 1:
            return [(1, ins["B"])] if sv & 1 else [(0, ins["A"])]
        return [(0, ins["A"]), (1, ins["B"])]
    k = int(nd.attrs.get("k", 0))
    w = nd.outputs[0].width
    arms: list[tuple[int, SigSpec]] = []
    for j in range(k):
        if (sk >> j) & 1:
            if (sv >> j) & 1:          # always selected: later arms and the default are unreachable
                arms.append((j, ins["B"].slice(j * w, w)))
                return arms
            continue                   # never selected
        arms.append((j, ins["B"].slice(j * w, w)))
    arms.append((k, ins["A"]))
    return arms


def _k_eff(nd: Node) -> int:
    """Number of non-default arms that can be selected."""
    if nd.op == "MUX":
        return 1
    return sum(1 for a, _ in _live_arms(nd) if a != int(nd.attrs.get("k", 0)))


class _Ctx:
    """Per-module lookups shared by every tree."""

    def __init__(self, m: ModuleIR) -> None:
        self.m = m
        cons = consumers(m)
        self.parent: dict[int, tuple[Node, int]] = {}
        for nd in m.nodes:
            if _is_mux(nd):
                r = _sole_data_use(nd, cons)
                if r is not None:
                    self.parent[nd.id] = r
        self.sconns: dict[int, tuple[list[int], list]] = {}
        for nid, lst in m.conns_by_net.items():
            cs = sorted(lst, key=lambda c: c.lo)
            self.sconns[nid] = ([c.lo for c in cs], cs)
        self.arms: dict[int, list[tuple[int, list[tuple]]]] = {}

    def node_arms(self, nd: Node) -> list[tuple[int, list[tuple]]]:
        a = self.arms.get(nd.id)
        if a is None:
            a = self.arms[nd.id] = [(j, list(s.bits())) for j, s in _live_arms(nd)]
        return a

    def resolve(self, net: Net | None, bit: int) -> tuple:
        """``("c", v)`` / ``("x",)`` for a constant or don't-care bit, else ``("b", net, bit)`` with ``net`` driven
        by a node or by nothing (wiring connections are followed)."""
        for _ in range(_DEPTH):
            if net is None:
                return ("x",) if bit == 2 else ("c", bit)
            if (net.kmask >> bit) & 1:
                return ("c", (net.kval >> bit) & 1)
            if net.driver.__class__ is Node:
                return ("b", net, bit)
            ent = self.sconns.get(net.id)
            if ent is None:
                return ("b", net, bit)
            los, cs = ent
            i = bisect_right(los, bit) - 1
            if i < 0 or not (cs[i].lo <= bit < cs[i].lo + cs[i].width):
                return ("b", net, bit)
            c = cs[i]
            j = bit - c.lo
            pos = 0
            nxt = None
            for ch in c.src.chunks:
                if j < pos + ch.width:
                    if ch.__class__ is Const:
                        k = j - pos
                        nxt = (None, 2 if (ch.xmask >> k) & 1 else (ch.value >> k) & 1)
                    else:
                        nxt = (ch.net, ch.lo + j - pos)
                    break
                pos += ch.width
            if nxt is None:
                return ("b", net, bit)
            net, bit = nxt
        return ("b", net, bit)


class FlatTree:
    """One flattened tree: ``nodes`` maps the path of every tree node (root ``()``) to the node; ``bits`` lists,
    per costed root bit, ``(bit, {leaf: [positions]})``."""

    __slots__ = ("root", "nodes", "bits")

    def __init__(self, root: Node) -> None:
        self.root = root
        self.nodes: dict[Path, Node] = {(): root}
        self.bits: list[tuple[int, dict[Leaf, list[Path]]]] = []


def _flatten(ctx: _Ctx, root: Node) -> FlatTree | None:
    out = root.outputs[0]
    w = out.width
    free = out.demand & ~out.kmask & mask(w)
    t = FlatTree(root)
    parent = ctx.parent
    b = 0
    while free:
        if free & 1:
            leaves: dict[Leaf, list[Path]] = {}
            stack: list[tuple[Node, int, Path]] = [(root, b, ())]
            seen: set[tuple[int, int]] = set()
            while stack:
                nd, bit, path = stack.pop()
                for a, atoms in ctx.node_arms(nd):
                    net, i = atoms[bit]
                    r = ctx.resolve(net, i)
                    p = path + (a,)
                    if r[0] == "b":
                        y = r[1].driver
                        if y.__class__ is Node and parent.get(y.id) == (nd, a) and (y.id, r[2]) not in seen \
                                and r[1] is y.outputs[0]:
                            seen.add((y.id, r[2]))
                            t.nodes[p] = y
                            stack.append((y, r[2], p))
                            continue
                        leaf: Leaf = ("n", r[1].id, r[2])
                    else:
                        leaf = r
                    leaves.setdefault(leaf, []).append(p)
            t.bits.append((b, leaves))
        free >>= 1
        b += 1
    if len(t.nodes) < 2:
        return None
    return t


def _sel_key(nd: Node) -> tuple:
    """Logical identity of the selects of a node: two nodes with the same key have the same arm conditions."""
    return (nd.op, nd.input("S").key())


def _summary(t: FlatTree, ids: dict[tuple, int]) -> dict:
    """Plain-data record of a tree (see the module docstring); ``ids`` interns the select gates of the module."""
    hist: dict[tuple[int, int, int, int], int] = {}
    needed: set[Path] = set()
    or_sets: set[tuple[Path, ...]] = set()
    for _b, leaves in t.bits:
        live = {lf: sorted(ps) for lf, ps in leaves.items() if lf[0] != "x"}
        if len(live) < 2:
            continue
        dflt = min(live, key=lambda lf: (-len(live[lf]), live[lf]))
        v = sum(1 for lf in live if lf[0] == "n")
        sig = (v, int(("c", 0) in live), int(("c", 1) in live), 2 if dflt[0] == "n" else dflt[1])
        hist[sig] = hist.get(sig, 0) + 1
        for lf, ps in live.items():
            if lf == dflt:
                continue
            needed.update(ps)
            if len(ps) > 1:
                or_sets.add(tuple(ps))
    closure: set[Path] = set()
    for p in needed:
        while p and p not in closure:
            closure.add(p)
            p = p[:-1]
    skeys = {np: _sel_key(nd) for np, nd in t.nodes.items()}

    def cond(p: Path) -> tuple:
        return tuple((skeys[p[:i]], p[i]) for i in range(len(p)))

    gates: dict[tuple, tuple[int, int]] = {}             # gate key -> (AND2, OR2)
    for p in closure:
        if len(p) >= 2:                                  # act(node) & arm condition
            gates[("and", cond(p))] = (1, 0)
    for st in or_sets:                                   # one leaf at several positions
        gates[("or", tuple(sorted(cond(p) for p in st)))] = (0, len(st) - 1)
    for np in {p[:-1] for p in closure}:
        nd = t.nodes.get(np)
        if nd is None:
            continue
        ke = _k_eff(nd)
        if nd.op == "PRIORITY" and ke > 1:               # s_j & ~s_<j masks
            gates[("mask", skeys[np])] = (ke - 1, 0)
        elif nd.op == "PMUX" and np and ke > 1 and np + (int(nd.attrs.get("k", 0)),) in closure:
            gates[("nor", skeys[np])] = (0, ke - 1)       # ~|s of a non-root default
    rec = []
    for g, (na, no) in gates.items():
        i = ids.get(g)
        if i is None:
            i = ids[g] = len(ids)
        rec.append([i, na, no])
    rec.sort()
    return {
        "root": t.root.id,
        "nodes": sorted(nd.id for p, nd in t.nodes.items() if p),
        "bits": [[*sig, n] for sig, n in sorted(hist.items())],
        "gates": rec,
        "sel_and": sum(r[1] for r in rec),
        "sel_or": sum(r[2] for r in rec),
    }


def flatten_mux_trees(m: ModuleIR) -> list[FlatTree]:
    """Every select tree of ``m`` (roots in node order), flattened per bit."""
    ctx = _Ctx(m)
    internal = ctx.parent
    if not internal:
        return []
    roots = sorted({id(p): p for p, _a in internal.values() if p.id not in internal}.values(), key=lambda n: n.id)
    trees = []
    for r in roots:
        t = _flatten(ctx, r)
        if t is not None:
            trees.append(t)
    return trees


def mark_mux_trees(m: ModuleIR) -> int:
    """Fill ``m.mux_trees`` (see the module docstring); returns the number of trees."""
    ids: dict[tuple, int] = {}
    m.mux_trees = [_summary(t, ids) for t in flatten_mux_trees(m)]
    return len(m.mux_trees)


__all__ = ["MUX_FAMILY", "FlatTree", "flatten_mux_trees", "mark_mux_trees"]
