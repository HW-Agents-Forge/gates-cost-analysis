"""Hierarchical observability (IR.md 8, COST_MODEL.md 4.3): dead logic across instance boundaries.

Every Module IR is analyzed on its own, so without this pass the outputs of every module are roots and a child whose
outputs reach no output of the top keeps its full cost.  ``analyze_hierarchy`` analyzes the hierarchy parents first and
carries the *observed* bits of every instance output down to the child:

* an ``INST`` whose child (transitively) has no effect other than its outputs -- no black box, no instance of a
  module absent from the design, no recursion, no ``inout`` port, not replaced by a black box -- is tagged
  ``prunable``: it is live only when one of its outputs is demanded (then all its inputs are demanded, like a MEM),
  so a dead instance is removed together with the parent logic that only feeds it (loops through the instance
  included);
* a live instance whose outputs are only partly demanded instantiates a *context variant* of the child, keyed
  ``key{obs:port=mask,...}`` (``context_key``), a copy of the child whose output roots are restricted to the demanded
  bits (``ModuleIR.port_demand``).  The fully observed context keeps the plain key, so a design whose instance outputs
  are all observed is analyzed exactly as before.

When no instance observes every output, the plain key leaves the design (one variant takes over its IR); a module
that no live instance reaches keeps its plain key and is analyzed on its own, as before (it is simply not counted).
Instance nodes keep ``module_key`` (so parent keys and hashes do not depend on the context); the variant is named by
the non-identity attribute ``ctx_key`` (``child_key``).

``Design.unresolved_contexts`` lists the keys of modules absent from the design (stubbed by a changed-cone
elaboration, or missing) whose instances are not fully observed, or which keep an otherwise unobservable instance
alive: the cost substituted for such a stub would not be the one of a full elaboration.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .core import Conn, ModuleIR, Net, Node
from .limits import DEFAULT_LIMITS
from .sigspec import SigSpec, Slice, mask

if TYPE_CHECKING:
    from .design import Design

Context = tuple  # tuple[tuple[str, int], ...]: (output port, demanded mask) of the not fully observed ports
FULL: Context = ()


def context_key(key: str, ctx: Context) -> str:
    """Key of the variant of ``key`` whose outputs are observed as in ``ctx`` (the plain key when fully observed)."""
    if not ctx:
        return key
    return key + "{obs:" + ",".join(f"{p}={mk:#x}" for p, mk in ctx) + "}"


def child_key(nd: Node) -> str | None:
    """Module key an ``INST`` node is costed through (its context variant, if any)."""
    return nd.attrs.get("ctx_key") or nd.attrs.get("module_key")


# --------------------------------------------------------------------------- cloning


def clone_module(m: ModuleIR, key: str) -> ModuleIR:
    """Independent copy of a (not yet analyzed) Module IR under ``key``: same net and node ids, so the copy is
    structurally identical to the original."""
    c = ModuleIR.__new__(ModuleIR)
    c.__dict__.update(m.__dict__)
    c.key = key
    nets: dict[int, Net] = {}

    def net(n: Net) -> Net:
        x = nets.get(id(n))
        if x is None:
            x = Net(n.id, n.width, n.signed, n.name, n.port)
            x.kmask, x.kval, x.demand, x.fanout = n.kmask, n.kval, n.demand, n.fanout
            x.attrs = dict(n.attrs)
            x.driver = n.driver           # node drivers are remapped below
            nets[id(n)] = x
        return x

    sigs: dict[int, SigSpec] = {}

    def sig(s: SigSpec) -> SigSpec:
        r = sigs.get(id(s))
        if r is None:
            r = SigSpec(tuple(Slice(net(ch.net), ch.lo, ch.width) if ch.__class__ is Slice else ch
                              for ch in s.chunks))
            sigs[id(s)] = r                # the sources stay alive (owned by ``m``): ids are stable keys
        return r

    def val(v):
        cl = v.__class__
        if cl is SigSpec:
            return sig(v)
        if cl is Net:
            return net(v)
        if cl is tuple:
            return tuple(val(x) for x in v)
        if cl is list:
            return [val(x) for x in v]
        if cl is dict:
            return {k: val(x) for k, x in v.items()}
        return v

    for n in m.nets:
        net(n)
    nodes: dict[int, Node] = {}
    out_nodes = []
    for nd in m.nodes:
        x = Node(nd.id, nd.op, tuple((nm, sig(s)) for nm, s in nd.inputs), tuple(net(o) for o in nd.outputs),
                 val(nd.attrs), nd.origin)
        x.tags = dict(nd.tags)
        x.alive = nd.alive
        nodes[id(nd)] = x
        out_nodes.append(x)
    for nd in m.nodes:                     # members of fused nodes (only present after analysis)
        if nd.members:
            nodes[id(nd)].members = tuple(nodes.get(id(y), y) for y in nd.members)
    for x in list(nets.values()):
        d = x.driver
        if d.__class__ is Node:
            x.driver = nodes.get(id(d))
    c.nets = [nets[id(n)] for n in m.nets]
    c.nodes = out_nodes
    c.ports = [net(p) for p in m.ports]
    c.conns = [Conn(net(cn.net), cn.lo, sig(cn.src), cn.origin) for cn in m.conns]
    cb: dict[int, list[Conn]] = {}
    for cn in c.conns:
        cb.setdefault(cn.net.id, []).append(cn)
    c.conns_by_net = cb
    c.conn_keys = {}
    c.driven = dict(m.driven)
    c.diagnostics = list(m.diagnostics)
    c.assumptions = list(m.assumptions)
    c.share_sets = []
    c.mux_trees = []
    c.canonical_hash = None
    c.analyzed = False
    c.unobservable = dict(m.unobservable)
    c.port_demand = None if m.port_demand is None else dict(m.port_demand)
    return c


# --------------------------------------------------------------------------- the pass


def _order(design: "Design") -> list[str]:
    """Module keys reachable from the top, parents first (recursive instances are cut)."""
    state: dict[str, int] = {}
    post: list[str] = []
    mods = design.modules
    top = design.top
    if top is None or top not in mods:
        return []
    stack: list[tuple[str, list[str]]] = [(top, _children(design, top))]
    state[top] = 1
    while stack:
        key, kids = stack[-1]
        if kids:
            ck = kids.pop()
            if state.get(ck, 0) == 0:
                state[ck] = 1
                stack.append((ck, _children(design, ck)))
            continue
        stack.pop()
        state[key] = 2
        post.append(key)
    post.reverse()
    return post


def _children(design: "Design", key: str) -> list[str]:
    out = []
    for nd in design.instances(key):
        ck = nd.attrs.get("module_key")
        if ck in design.modules and not nd.tags.get("recursive"):
            out.append(ck)
    out.sort(reverse=True)
    return out


def _side_effects(design: "Design", order: list[str]) -> tuple[dict[str, bool], dict[str, frozenset]]:
    """Per key: ``hard`` (the module has an effect other than its outputs) and ``soft`` (keys of absent modules
    below it, assumed to have one)."""
    hard: dict[str, bool] = {}
    soft: dict[str, frozenset] = {}
    for key in reversed(order):            # children first
        m = design.modules[key]
        h = bool(m.is_blackbox or m.limit_reached or any(p.port == "inout" for p in m.ports))
        s: set[str] = set()
        for nd in m.nodes:
            if nd.op == "BLACKBOX":
                h = True
            elif nd.op == "INST":
                ck = nd.attrs.get("module_key")
                if nd.tags.get("recursive"):
                    h = True
                elif ck not in design.modules:
                    s.add(ck)
                else:
                    h = h or hard.get(ck, True)
                    s.update(soft.get(ck, ()))
        hard[key] = h
        soft[key] = frozenset(s)
    return hard, soft


def _context(nd: Node, child: ModuleIR) -> Context:
    """Observation context of an analyzed instance: the output ports whose bits are not all demanded."""
    names = nd.attrs.get("out_ports", ())
    if len(names) != len(nd.outputs):
        return FULL
    outs = {p.name: p for p in child.ports if p.port in ("out", "inout")}
    if len(outs) != len(names):
        return FULL
    res = []
    for name, o in zip(names, nd.outputs):
        p = outs.get(name)
        if p is None or p.port != "out" or p.width != o.width:
            return FULL
        full = mask(o.width)
        dm = o.demand & full
        if dm != full:
            res.append((name, dm))
    return tuple(sorted(res))


def analyze_hierarchy(design: "Design", **kw) -> bool:
    """Analyze every module of ``design`` with observability carried across instances.  Returns False (nothing
    done) when the design has no top in its module table; the caller then analyzes the modules on their own."""
    from .analyze import analyze_ir

    if design.top is None or design.top not in design.modules:
        return False
    design.detect_recursion()
    order = _order(design)
    hard, soft = _side_effects(design, order)
    pending: dict[str, set] = {order[0]: {FULL}}
    users: dict[tuple, list[Node]] = {}          # (child key, context) -> instances (for the variant limit)
    unresolved: set[str] = set()
    analyzed: set[str] = set()
    mods = design.modules

    def collect(ir: ModuleIR) -> None:
        for nd in ir.nodes:
            if nd.op != "INST" or nd.tags.get("recursive"):
                continue
            ck = nd.attrs.get("module_key")
            full = all(o.demand & mask(o.width) == mask(o.width) for o in nd.outputs)
            if ck not in mods:
                if not full:
                    unresolved.add(ck)
                continue
            if not full and not hard.get(ck, True) and soft.get(ck):
                unresolved.update(soft[ck])
            ctx = _context(nd, mods[ck])
            if ctx:
                nd.attrs["ctx_key"] = context_key(ck, ctx)
            else:
                nd.attrs.pop("ctx_key", None)
            pending.setdefault(ck, set()).add(ctx)
            users.setdefault((ck, ctx), []).append(nd)

    for key in order:
        m = mods[key]
        ctxs = pending.get(key, set())
        work: list[ModuleIR] = []
        partial = sorted(c for c in ctxs if c)
        cap = DEFAULT_LIMITS.max_context_variants
        if len(partial) > cap:
            for ctx in partial[cap:]:
                for nd in users.get((key, ctx), ()):
                    nd.attrs.pop("ctx_key", None)
            partial = partial[:cap]
            ctxs = set(partial) | {FULL}
        for i, ctx in enumerate(partial):
            vk = context_key(key, ctx)
            # the last variant takes over the plain IR when no instance observes every output
            v = m if (FULL not in ctxs and i == len(partial) - 1) else clone_module(m, vk)
            pd = {p.name or "": mask(p.width) for p in v.ports if p.port == "out"}
            pd.update(ctx)
            v.port_demand = pd
            v.key = vk
            mods[vk] = v
            work.append(v)
        if FULL in ctxs:
            work.insert(0, m)
        elif partial:
            del mods[key]
        for ir in work:
            for nd in ir.nodes:
                if nd.op == "INST" and not nd.tags.get("recursive"):
                    ck = nd.attrs.get("module_key")
                    if ck in mods and not hard.get(ck, True) and not soft.get(ck):
                        nd.tags["prunable"] = True
            analyze_ir(ir, **kw)
            analyzed.add(ir.key)
            collect(ir)
    for key in sorted(mods):
        if key not in analyzed:
            analyze_ir(mods[key], **kw)
    design.unresolved_contexts = tuple(sorted(k for k in unresolved if k is not None))
    return True


__all__ = ["analyze_hierarchy", "child_key", "clone_module", "context_key", "FULL"]
