"""Effective widths (6.4), datapath fusion (6.5) and resource sharing (6.6)."""
from __future__ import annotations

from .core import ARITH_FUSABLE, ModuleIR, Net, Node
from .graph import consumers
from .knownbits import _tz, sig_known
from .sigspec import Const, SigSpec, Slice, mask, top_rep


def popcount(x: int) -> int:
    return x.bit_count()


def opinfo(sig: SigSpec) -> dict:
    """Facts about one operand used by the cost model."""
    W = sig.width
    km, kv = sig_known(sig)
    zeros = km & ~kv
    full = (1 << W) - 1 if W > 0 else 0
    ub = (~zeros & full).bit_length()
    sr = top_rep(sig)
    es = W - sr if W else 0
    return {
        "width": W,
        "eff": min(ub, es) if W else 0,
        "ub": ub,
        "sign_rep": sr,
        "tz": min((~zeros & (zeros + 1)).bit_length() - 1, W),
        "const": kv if km == full else None,
        # sign-extended: the top bits repeat a (non-constant) net bit below them, as the elaborator emits for a
        # signed operand widened to the context width
        "sext": sr > 0 and _bit_atom(sig, W - 1)[0] is not None,
        "kmask": km,
        "kval": kv,
    }


def _bit_atom(sig: SigSpec, i: int) -> tuple:
    """Atom of bit ``i``: ``(net, bit)`` for a net bit, ``(None, v)`` for a constant bit."""
    pos = 0
    for c in sig.chunks:
        if i < pos + c.width:
            j = i - pos
            if isinstance(c, Const):
                return (None, 2 if (c.xmask >> j) & 1 else (c.value >> j) & 1)
            return (c.net, c.lo + j)
        pos += c.width
    return (None, 2)


def canon_atom(atom: tuple, depth: int = 8) -> tuple:
    """Resolve a net bit through bitwise nodes whose other operand bit is a neutral constant
    (``x ^ 0``, ``x ~^ 1``, ``x & 1``, ``x | 0``): the result is the bit it passes through."""
    while depth > 0:
        net, bit = atom
        if net is None:
            return atom
        nd = net.driver
        if not isinstance(nd, Node) or nd.op not in ("XOR", "XNOR", "AND", "OR") or len(nd.outputs) != 1 \
                or nd.outputs[0] is not net:
            return atom
        ins = dict(nd.inputs)
        a, b = _bit_atom(ins["A"], bit), _bit_atom(ins["B"], bit)
        neutral = 1 if nd.op in ("AND", "XNOR") else 0
        if b[0] is None and b[1] == neutral:
            atom = a
        elif a[0] is None and a[1] == neutral:
            atom = b
        else:
            return atom
        depth -= 1
    return atom


def _same_bits(sa: SigSpec, sb: SigSpec, mask_bits: int) -> int:
    """Subset of ``mask_bits`` where both signals resolve to the same net bit."""
    out = 0
    i = 0
    m = mask_bits
    while m:
        if m & 1:
            x, y = _bit_atom(sa, i), _bit_atom(sb, i)
            if x[0] is not None and canon_atom(x) == canon_atom(y):
                out |= 1 << i
        m >>= 1
        i += 1
    return out


def _complement_of(atom: tuple) -> tuple | None:
    """The atom this net bit is the complement of (``~x``, ``x ^ 1``, ``x ~^ 0``), or None."""
    net, bit = atom
    nd = net.driver if net is not None else None
    if not isinstance(nd, Node) or len(nd.outputs) != 1 or nd.outputs[0] is not net:
        return None
    ins = dict(nd.inputs)
    if nd.op == "NOT":
        x = _bit_atom(ins["A"], bit)
        return canon_atom(x) if x[0] is not None else None
    if nd.op in ("XOR", "XNOR"):
        a, b = _bit_atom(ins["A"], bit), _bit_atom(ins["B"], bit)
        flip = 1 if nd.op == "XOR" else 0           # constant that inverts the other operand
        if b[0] is None and b[1] == flip and a[0] is not None:
            return canon_atom(a)
        if a[0] is None and a[1] == flip and b[0] is not None:
            return canon_atom(b)
    return None


def _mux_special_bits(sa: SigSpec, sb: SigSpec, ss: SigSpec, bits: int) -> tuple[int, int]:
    """Of the variable bits ``bits`` of a 2:1 mux ``s ? sb : sa``: those whose arms are complements of one another
    (``s ? ~x : x`` = ``x ^ s``) and those where one arm is the select itself (``s ? s : x`` = ``s | x``,
    ``s ? x : s`` = ``s & x``).  Returns ``(xor_mask, andor_mask)``; every such bit needs one 2-input gate only."""
    xm = am = 0
    sel = _bit_atom(ss, 0) if ss.width == 1 else None
    sel = canon_atom(sel) if sel is not None and sel[0] is not None else None
    i = 0
    m = bits
    while m:
        if m & 1:
            x, y = _bit_atom(sa, i), _bit_atom(sb, i)
            if x[0] is not None and y[0] is not None:
                cx, cy = canon_atom(x), canon_atom(y)
                if _complement_of(x) == cy or _complement_of(y) == cx:
                    xm |= 1 << i
                elif sel is not None and (cx == sel or cy == sel):
                    am |= 1 << i
        m >>= 1
        i += 1
    return xm, am


def compute_eff(nd: Node) -> dict:
    """Post-folding widths used by the cost model (COST_MODEL.md 4.2); declared widths
    are kept alongside."""
    op = nd.op
    ins = dict(nd.inputs)
    out = nd.outputs[0] if nd.outputs else None
    if out is None:
        return {"w": 0}
    W = out.width
    dm = out.demand & mask(W)
    dw = dm.bit_length()
    km, kv = out.kmask, out.kval
    e: dict = {"declared": W, "demand": dm, "known": (km, kv)}
    info = {}
    if op not in ("MEM", "INST", "BLACKBOX"):
        for k, s in nd.inputs:
            info[k] = opinfo(s)
    e["ops"] = dict(info)
    a = info.get("A")
    b = info.get("B")
    if op in ("DFF", "LATCH", "MEM", "INST", "BLACKBOX"):
        e["w"] = W if op in ("DFF", "LATCH") else 0
        return e
    if km == mask(W) and op != "ROM" or dm == 0:
        e["w"] = 0
        e["const_folded"] = km == mask(W)
        return e
    if op in ("ADD", "SUB"):
        w = min(dw, max(a["eff"], b["eff"]) + 1)
        e.update(w=w, lo=min(min(a["tz"], b["tz"]), w), wa=a["eff"], wb=b["eff"])
    elif op == "NEG":
        e.update(w=min(dw, a["eff"] + 1), wa=a["eff"])
    elif op == "MUL":
        ta, tb = a["tz"], b["tz"]
        wa = max(a["eff"] - ta, 0)
        wb = max(b["eff"] - tb, 0)
        shift = ta + tb
        full = wa + wb if wa and wb else 0
        outw = min(max(dw - shift, 0), full)
        e.update(w=min(dw, W), wa=wa, wb=wb, shift=shift, full=full, out=outw,
                 truncated=outw < full,
                 signed=bool(nd.attrs.get("sa") or nd.attrs.get("sb") or a["sext"] or b["sext"]))
    elif op in ("DIV", "MOD"):
        e.update(w=min(dw, W), wa=a["eff"], wb=b["eff"])
    elif op == "POW":
        e.update(w=min(dw, W), wa=a["eff"], exp=nd.attrs["exp"])
    elif op in ("SHL", "SHR", "SAR", "ROL", "ROR"):
        e.update(w=min(dw, W), n=W, wa=a["eff"], ws=b["ub"])
    elif op in ("AND", "OR", "XOR", "XNOR", "NOT"):
        ak, av = a["kmask"], a["kval"]
        bk, bv = (b["kmask"], b["kval"]) if b else (0, 0)
        free = dm & ~km
        inv = 0
        if op == "AND":
            act = free & ~(ak & av) & ~(bk & bv)
        elif op == "OR":
            act = free & ~(ak & ~av) & ~(bk & ~bv)
        elif op == "NOT":
            act = free
        else:
            act = free & ~ak & ~bk
            kn1 = (ak & av) | (bk & bv)
            kn0 = (ak & ~av) | (bk & ~bv)
            inv = free & ~act & (kn1 if op == "XOR" else kn0)
        e.update(w=popcount(act), active=act, inv=popcount(inv))
    elif op in ("RED_AND", "RED_OR", "RED_XOR"):
        ak, av = a["kmask"], a["kval"]
        if op == "RED_AND":
            neutral = ak & av
        elif op == "RED_OR":
            neutral = ak & ~av
        else:
            neutral = ak
        e.update(w=a["width"] - popcount(neutral))
    elif op in ("EQ", "NE"):
        cw = a["width"]
        both = a["kmask"] & b["kmask"]
        nv = mask(cw) & ~both
        one = nv & (a["kmask"] ^ b["kmask"])
        n = popcount(nv)
        e.update(w=n, n=n, n_const=popcount(one), n_var=n - popcount(one),
                 const_operand=a["const"] is not None or b["const"] is not None)
    elif op in ("LT", "LE", "GT", "GE"):
        sg = bool(nd.attrs.get("sa") and nd.attrs.get("sb"))
        if sg:
            n = max(a["width"] - a["sign_rep"], b["width"] - b["sign_rep"])
        else:
            n = max(a["ub"], b["ub"])
        e.update(w=n, n=max(n, 1), signed=sg,
                 const_operand=a["const"] is not None or b["const"] is not None)
    elif op == "MUX":
        sk, sv = sig_known(ins["S"])
        if sk & 1:
            e.update(w=0, select_known=True)
        else:
            free = dm & ~km
            ak, bk = a["kmask"], b["kmask"]
            both_var = free & ~ak & ~bk
            both_var &= ~_same_bits(ins["A"], ins["B"], both_var)
            xor_bits, andor_bits = _mux_special_bits(ins["A"], ins["B"], ins["S"], both_var)
            const_a = free & ak & ~bk
            const_b = free & bk & ~ak
            both_const = free & ak & bk
            both_var &= ~(xor_bits | andor_bits)
            e.update(w=popcount(free), n_var=popcount(both_var), n_xor=popcount(xor_bits),
                     n_andor=popcount(andor_bits), n_const_a=popcount(const_a),
                     n_const_b=popcount(const_b), n_const_both=popcount(both_const),
                     tristate=bool(nd.attrs.get("tristate")))
    elif op in ("PMUX", "PRIORITY"):
        k = nd.attrs["k"]
        sk, sv = sig_known(ins["S"])
        k_eff = sum(1 for i in range(k) if not ((sk >> i) & 1 and not (sv >> i) & 1))
        e.update(w=popcount(dm & ~km), k=k, k_eff=k_eff)
    elif op == "DECODE":
        e.update(w=popcount(dm & ~km), n=a["ub"], outs=popcount(dm & ~km))
    elif op == "ENCODE":
        e.update(w=popcount(dm & ~km), n=a["ub"])
    elif op == "ROM":
        e.update(w=popcount(dm & ~km), n=a["ub"], depth=nd.attrs["depth"])
    elif op == "SUM":
        e.update(w=min(dw, W))
    else:
        e["w"] = min(dw, W)
    return e


def effective_widths(m: ModuleIR) -> None:
    for nd in m.nodes:
        nd.eff = compute_eff(nd)
        if nd.eff.get("const_folded"):
            nd.tags["const_folded"] = True
        else:
            nd.tags.pop("const_folded", None)


# --------------------------------------------------------------------------
# Datapath fusion
# --------------------------------------------------------------------------
def _single_use(o: Net, cons: dict):
    """Follow a node output through single-use whole-net wiring.  Returns
    ``(consumer node, input name, operand SigSpec, [bridged connections, nets])`` or None."""
    bridged: list = []
    cur = o
    while True:
        refs = cons.get(cur.id, [])
        if len(refs) != 1 or cur.port or cur.fanout != 1:
            return None
        item, name, pos, chunk = refs[0]
        if isinstance(item, Node):
            if chunk.lo != 0:
                return None
            sig = dict(item.inputs)[name]
            if sig.single_slice() is None:
                return None
            return item, name, sig, bridged
        # connection: must be an exact whole-net copy
        if item.lo != 0 or item.width != item.net.width or item.src.single_slice() is None:
            return None
        if chunk.lo != 0 or chunk.width != cur.width:
            return None
        bridged.append((item, item.net))
        cur = item.net


def _ext_use(nd: Node, cons: dict):
    """A MUL whose whole product is only consumed through a pure zero/sign extension (VHDL
    ``resize(a * b, N)``: the product is built at its own width, then widened) that does not
    change its value: the multiplier does not truncate and the extension kind matches its
    signedness.  Same return shape as :func:`_single_use` (no bridged connections)."""
    o = nd.outputs[0]
    refs = cons.get(o.id, [])
    if not refs or o.port or o.fanout != len(refs):
        return None
    item, name = refs[0][0], refs[0][1]
    if not isinstance(item, Node) or any(r[0] is not item or r[1] != name for r in refs):
        return None
    sig = dict(item.inputs)[name]
    if sig.width <= o.width:
        return None
    sa, sb = bool(nd.attrs.get("sa")), bool(nd.attrs.get("sb"))
    if sa != sb:
        return None
    ins = dict(nd.inputs)
    ia, ib = opinfo(ins["A"]), opinfo(ins["B"])
    need = max(ia["eff"] - ia["tz"], 0) + max(ib["eff"] - ib["tz"], 0) + ia["tz"] + ib["tz"]
    if need > o.width:
        return None
    whole = SigSpec((Slice(o, 0, o.width),))
    if sig.key() != whole.resize(sig.width, sa).key():
        return None
    return item, name, sig, []


def _widen(sig: SigSpec, width: int, signed: bool) -> SigSpec:
    return sig.slice(0, width) if sig.width >= width else sig.resize(width, signed)


def fuse_datapath(m: ModuleIR, enabled: bool = True) -> int:
    """Replace single-use ADD/SUB/NEG/MUL clusters by ``SUM`` nodes.  Returns count."""
    if not enabled:
        return 0
    cons = consumers(m)
    link: dict[int, tuple] = {}     # node id -> (consumer, input name, bridged)
    for nd in m.nodes:
        if nd.op not in ARITH_FUSABLE:
            continue
        r = _single_use(nd.outputs[0], cons)
        if r is None and nd.op == "MUL":
            r = _ext_use(nd, cons)
        if r is None:
            continue
        c, name, sig, bridged = r
        if c.op not in ("ADD", "SUB", "NEG"):
            continue
        w = c.outputs[0].width
        if sig.width != w:
            continue
        link[nd.id] = (c, name, bridged)
    children: dict[int, list[Node]] = {}
    by_id = {n.id: n for n in m.nodes}
    for nid, (c, name, br) in link.items():
        children.setdefault(c.id, []).append(by_id[nid])
    roots = [n for n in m.nodes if n.op in ("ADD", "SUB", "NEG") and n.id not in link
             and n.id in children]
    count = 0
    new_nodes: list[Node] = []
    dead_ids: set[int] = set()
    dead_nets: set[int] = set()
    dead_conns: set[int] = set()
    for root in roots:
        # collect the cluster (tree) top-down
        kids = children.get(root.id, [])
        cluster = []   # members other than root
        muls = []
        stack = [root]
        order_nodes = []
        while stack:
            cur = stack.pop()
            order_nodes.append(cur)
            for ch in sorted(children.get(cur.id, ()), key=lambda x: x.id):
                if ch.op == "MUL":
                    muls.append(ch)
                else:
                    stack.append(ch)
        # choose at most one MUL pair: the largest by effective operand widths
        keep_mul = None
        if muls:
            def area(mn):
                e = mn.eff or {}
                return (e.get("wa", 0) * e.get("wb", 0), -mn.id)
            keep_mul = max(muls, key=area)
        members = [n for n in order_nodes] + ([keep_mul] if keep_mul else [])
        if len(members) < 2:
            continue
        wroot = root.outputs[0].width
        terms: list[tuple[SigSpec, int]] = []
        multerm = None

        def build(nd: Node, sign: int) -> None:
            nonlocal multerm
            ins = nd.inputs
            for idx, (nm, sig) in enumerate(ins):
                if nd.op == "ADD":
                    s = sign
                elif nd.op == "SUB":
                    s = sign if nm == "A" else -sign
                else:
                    s = -sign
                child = None
                for ch in children.get(nd.id, ()):
                    l = link[ch.id]
                    if l[1] == nm:
                        child = ch
                if child is not None and (child is keep_mul or child.op != "MUL"):
                    if child is keep_mul:
                        ma = dict(child.inputs)
                        multerm = (_widen(ma["A"], wroot, bool(child.attrs.get("sa"))),
                                   _widen(ma["B"], wroot, bool(child.attrs.get("sb"))), s,
                                   bool(child.attrs.get("sa") or child.attrs.get("sb")))
                    else:
                        build(child, s)
                else:
                    terms.append((sig.slice(0, wroot), s))

        build(root, 1)
        inputs = [(f"T{i}", t[0]) for i, t in enumerate(terms)]
        signs = tuple(t[1] for t in terms)
        attrs = {"signs": signs, "k": len(terms) + (1 if multerm else 0)}
        if multerm:
            inputs += [("MA", multerm[0]), ("MB", multerm[1])]
            attrs["mul_sign"] = multerm[2]
            attrs["mul_signed"] = multerm[3]
        sumnode = Node(m.next_node_id, "SUM", tuple(inputs), (root.outputs[0],), attrs, root.origin)
        m.next_node_id += 1
        sumnode.members = tuple(sorted(members, key=lambda n: n.id))
        sumnode.tags["members"] = tuple(n.id for n in sumnode.members)
        root.outputs[0].driver = sumnode
        for mem in members:
            mem.tags["fused"] = sumnode.id
            dead_ids.add(mem.id)
            if mem is not root:
                for o in mem.outputs:
                    dead_nets.add(o.id)
                for (cn, net) in link[mem.id][2]:
                    dead_conns.add(id(cn))
                    dead_nets.add(net.id)
        new_nodes.append(sumnode)
        count += 1
    if not count:
        return 0
    m.nodes = [n for n in m.nodes if n.id not in dead_ids] + new_nodes
    m.conns = [c for c in m.conns if id(c) not in dead_conns]
    cb: dict[int, list] = {}
    for c in m.conns:
        cb.setdefault(c.net.id, []).append(c)
    m.conns_by_net = cb
    m.nets = [n for n in m.nets if n.id not in dead_nets]
    m.driven = {k: v for k, v in m.driven.items() if k not in dead_nets}
    m.recount_fanout()
    for sn in new_nodes:
        sn.eff = compute_sum_eff(sn)
    return count


def compute_sum_eff(sn: Node) -> dict:
    out = sn.outputs[0]
    W = out.width
    dm = out.demand & mask(W)
    dw = dm.bit_length()
    ins = dict(sn.inputs)
    term_effs = []
    for nm, sig in sn.inputs:
        if nm.startswith("T"):
            term_effs.append(opinfo(sig)["eff"])
    mul = None
    if "MA" in ins:
        ia, ib = opinfo(ins["MA"]), opinfo(ins["MB"])
        wa, wb = max(ia["eff"] - ia["tz"], 0), max(ib["eff"] - ib["tz"], 0)
        mul = {"wa": wa, "wb": wb, "shift": ia["tz"] + ib["tz"], "const_a": ia["const"],
               "const_b": ib["const"],
               "signed": bool(sn.attrs.get("mul_signed") or ia["sext"] or ib["sext"])}
        term_effs.append(wa + wb + mul["shift"])
    k = len(term_effs)
    grow = (k - 1).bit_length() if k > 1 else 0
    w = min(dw, max(term_effs) + grow) if term_effs else 0
    e = {"declared": W, "demand": dm, "known": (out.kmask, out.kval), "w": w, "k": k,
         "terms": tuple(term_effs), "mul": mul, "signs": sn.attrs["signs"],
         "ops": {nm: opinfo(sig) for nm, sig in sn.inputs}}
    return e


# --------------------------------------------------------------------------
# Resource sharing
# --------------------------------------------------------------------------
def _share_class(nd: Node) -> str | None:
    if nd.op == "MUL":
        return "MUL"
    if nd.op == "DIV":
        return "DIV"
    if nd.op in ("ADD", "SUB"):
        e = nd.eff or {}
        if nd.outputs[0].width >= 8 and e.get("w", 0) >= 8:
            return "ADDSUB"
    return None


def mark_divmod_pairs(m: ModuleIR) -> int:
    """Tag a MOD that has the same operands as a DIV (``tags['divmod_with'] = DIV id``): the
    pair is costed as one divider (COST_MODEL.md 3.6)."""
    divs = {}
    for nd in m.nodes:
        nd.tags.pop("divmod_with", None)
        if nd.op == "DIV":
            ins = dict(nd.inputs)
            divs.setdefault((ins["A"].key(), ins["B"].key(), nd.attrs.get("sa"), nd.attrs.get("sb")), nd)
    n = 0
    for nd in m.nodes:
        if nd.op == "MOD":
            ins = dict(nd.inputs)
            d = divs.get((ins["A"].key(), ins["B"].key(), nd.attrs.get("sa"), nd.attrs.get("sb")))
            if d is not None:
                nd.tags["divmod_with"] = d.id
                n += 1
    return n


def mark_sharing(m: ModuleIR) -> int:
    """Mark same-class arithmetic leaves of exclusive branches of one ``group``."""
    mark_divmod_pairs(m)
    groups: dict[int, list[Node]] = {}
    for nd in m.nodes:
        if nd.op in ("MUX", "PMUX", "PRIORITY") and "group" in nd.tags:
            groups.setdefault(nd.tags["group"], []).append(nd)
        nd.tags.pop("shared", None)
        nd.tags.pop("share_set", None)
    m.share_sets = []
    total = 0
    for g in sorted(groups):
        gn = groups[g]
        by_out = {nd.outputs[0].id: nd for nd in gn}
        arm_nodes = set()
        # arms of every node
        arms_of: dict[int, list[tuple[int, SigSpec]]] = {}
        for nd in gn:
            ins = dict(nd.inputs)
            w = nd.outputs[0].width
            if nd.op == "MUX":
                arms = [(0, ins["A"]), (1, ins["B"])]
            else:
                k = nd.attrs["k"]
                arms = [(k, ins["A"])] + [(i, ins["B"].slice(i * w, w)) for i in range(k)]
            arms_of[nd.id] = arms
            for _, s in arms:
                for c in s.chunks:
                    if isinstance(c, Slice) and c.net.id in by_out:
                        arm_nodes.add(by_out[c.net.id].id)
        roots = [nd for nd in gn if nd.id not in arm_nodes]
        leaves: list[tuple[Node, tuple]] = []
        seen_nodes = set()

        def walk(nd: Node, path: tuple) -> None:
            if nd.id in seen_nodes:
                return
            seen_nodes.add(nd.id)
            for idx, s in arms_of[nd.id]:
                for ci, c in enumerate(s.chunks):
                    p = path + ((nd.id, idx), ("c", ci))
                    if not isinstance(c, Slice):
                        continue
                    sub = by_out.get(c.net.id)
                    if sub is not None:
                        walk(sub, path + ((nd.id, idx),))
                        continue
                    d = _resolve_leaf(c, m)
                    if d is not None and _share_class(d):
                        leaves.append((d, p))

        for r in sorted(roots, key=lambda n: n.id):
            walk(r, ())
        by_class: dict[str, list] = {}
        for d, p in leaves:
            by_class.setdefault(_share_class(d), []).append((d, p))
        for cls in sorted(by_class):
            sets: list[list] = []
            for d, p in sorted(by_class[cls], key=lambda x: x[0].id):
                for st in sets:
                    if all(_exclusive(p, q) for _, q in st):
                        st.append((d, p))
                        break
                else:
                    sets.append([(d, p)])
            for st in sets:
                if len(st) < 2:
                    continue
                idx = len(m.share_sets)
                m.share_sets.append({"group": g, "class": cls, "nodes": [d.id for d, _ in st]})
                for d, _ in st:
                    d.tags["shared"] = g
                    d.tags["share_set"] = idx
                total += len(st)
    return total


def _resolve_leaf(c: Slice, m: ModuleIR) -> Node | None:
    """The single-use arithmetic node whose whole output is the chunk ``c`` (possibly
    through whole-net connection copies), or None."""
    for _ in range(64):
        net = c.net
        if c.lo != 0 or c.width != net.width or net.fanout != 1 or net.port:
            return None
        d = net.driver
        if isinstance(d, Node):
            return d
        cs = m.conns_by_net.get(net.id, ())
        if len(cs) != 1:
            return None
        cn = cs[0]
        sl = cn.src.single_slice()
        if cn.lo != 0 or cn.width != net.width or sl is None:
            return None
        c = sl
    return None


def _exclusive(p: tuple, q: tuple) -> bool:
    for a, b in zip(p, q):
        if a == b:
            continue
        # first divergence: exclusive only if same mux node, different arm index
        return a[0] == b[0] and not isinstance(a[0], str) and a[1] != b[1]
    return False
