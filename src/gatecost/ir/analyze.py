"""The analysis pipeline (IR.md section 6): ``analyze_ir``."""
from __future__ import annotations

from .core import Conn, ModuleIR, Net, Node
from .datapath import effective_widths, fuse_datapath, mark_sharing
from .demand import compute_demand, dead_logic
from .graph import topo_order
from .keys import compute_keys
from .knownbits import known_bits, sig_known
from .sigspec import Const, SigSpec, mask


def _replace_node(m: ModuleIR, nd: Node, sig: SigSpec) -> None:
    out = nd.outputs[0]
    nd.alive = False
    m.nodes = [x for x in m.nodes if x is not nd]
    out.driver = None
    conn = Conn(out, 0, sig, nd.origin)
    m.conns.append(conn)
    m.conns_by_net.setdefault(out.id, []).append(conn)


def _fold_const_mux(m: ModuleIR, nd: Node, nots: dict) -> SigSpec | None:
    """MUX whose data inputs are both constants: per-bit wire / inverter / constant on S
    (IR.md 4.3; the lowering keeps such MUXes so that reset patterns stay recognisable)."""
    ins = dict(nd.inputs)
    ca, cb = ins["A"].const_value(), ins["B"].const_value()
    if ca is None or cb is None or nd.attrs.get("tristate"):
        return None
    S = ins["S"]
    parts = []
    for i in range(nd.outputs[0].width):
        ax, bx = (ca.xmask >> i) & 1, (cb.xmask >> i) & 1
        a0, b0 = (ca.value >> i) & 1, (cb.value >> i) & 1
        if ax and bx:
            parts.append(SigSpec.x(1))
        elif ax:
            parts.append(SigSpec.const(b0, 1))
        elif bx:
            parts.append(SigSpec.const(a0, 1))
        elif a0 == b0:
            parts.append(SigSpec.const(a0, 1))
        elif b0 == 1:
            parts.append(S)
        else:
            k = S.key()
            if k not in nots:
                out = Net(m.next_net_id, 1)
                m.next_net_id += 1
                m.nets.append(out)
                inv = Node(m.next_node_id, "NOT", (("A", S),), (out,), {}, nd.origin)
                m.next_node_id += 1
                out.driver = inv
                m.driven[out.id] = 1
                m.nodes.append(inv)
                nots[k] = out.sig
            parts.append(nots[k])
    return SigSpec.concat(parts)


def fold_known(m: ModuleIR, order) -> int:
    """Replace nodes whose outputs are fully known by constants and MUXes with a known
    select by a wire to the selected input.  Returns the number of replaced nodes."""
    n = 0
    nots: dict = {}
    for it in list(order):
        if not isinstance(it, Node) or it.is_state or len(it.outputs) != 1:
            continue
        out = it.outputs[0]
        if out.kmask == mask(out.width):
            _replace_node(m, it, SigSpec((Const(out.kval, out.width),)))
            n += 1
        elif it.op == "MUX" and not it.attrs.get("tristate"):
            sk, sv = sig_known(dict(it.inputs)["S"])
            if sk & 1:
                ins = dict(it.inputs)
                _replace_node(m, it, ins["B"] if sv & 1 else ins["A"])
                n += 1
            else:
                r = _fold_const_mux(m, it, nots)
                if r is not None:
                    _replace_node(m, it, r)
                    n += 1
    return n


def analyze_ir(m: ModuleIR, *, fusion: bool = True, sharing: bool = True) -> ModuleIR:
    """Run the analysis passes on a built Module IR (in place) and return it.

    Order: known bits, dead logic, demanded bits, effective widths, datapath fusion,
    resource sharing marks, keys.  Pure function of the Module IR: repeated calls give
    identical annotations.
    """
    if m.is_blackbox:
        compute_keys(m, [])
        m.analyzed = True
        return m
    m.recount_fanout()
    has_state = any(n.op == "DFF" for n in m.nodes)
    clean = False
    order = None
    for _ in range(4):
        order, _cyc = topo_order(m)
        known_bits(m, order, rounds=4 if has_state else 1)
        changed = fold_known(m, order)
        changed += dead_logic(m, order)       # leaves valid demanded bits behind
        if not changed:
            clean = True
            break
    if not clean:
        order, _cyc = topo_order(m)
        known_bits(m, order, rounds=4 if has_state else 1)
        compute_demand(m)
    m.recount_fanout()
    effective_widths(m)
    if fusion:
        if fuse_datapath(m):
            order = None
    if sharing:
        mark_sharing(m)
    if order is None:
        order, _cyc = topo_order(m)
    compute_keys(m, order)
    m.analyzed = True
    return m
