"""Canonical keys: shape keys, named keys, module canonical hash (IR.md section 7).

Hash function: ``hashlib.blake2b`` with a 16-byte digest over length-prefixed fields.
Integers are encoded big-endian: 8 bytes when they fit in 62 bits, otherwise
length-prefixed.  Python's ``hash`` is never used for anything persisted or ordered.
"""
from __future__ import annotations

from hashlib import blake2b

from .core import COMMUTATIVE, NON_IDENTITY_ATTRS, Conn, ModuleIR, Net, Node
from .graph import consumers, topo_order
from .sigspec import Const, SigSpec, Slice


def H(data: bytes) -> bytes:
    return blake2b(data, digest_size=16).digest()


_SMALL = [b"\x00" + n.to_bytes(8, "big", signed=True) for n in range(1024)]


def _i(n: int) -> bytes:
    if 0 <= n < 1024:
        return _SMALL[n]
    if -(1 << 62) < n < (1 << 62):
        return b"\x00" + n.to_bytes(8, "big", signed=True)
    b = n.to_bytes((n.bit_length() + 8) // 8, "big", signed=True)
    return b"\x01" + len(b).to_bytes(4, "big") + b


def _s(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(4, "big") + b


def ser(v) -> bytes:
    """Canonical serialization of an attribute value."""
    if v is None:
        return b"N"
    if isinstance(v, bool):
        return b"T" if v else b"F"
    if isinstance(v, int):
        return b"I" + _i(v)
    if isinstance(v, str):
        return b"S" + _s(v)
    if isinstance(v, Const):
        return b"C" + _i(v.value) + _i(v.width) + _i(v.xmask)
    if isinstance(v, (tuple, list)):
        return b"L" + _i(len(v)) + b"".join(ser(x) for x in v)
    if isinstance(v, dict):
        return b"D" + _i(len(v)) + b"".join(_s(str(k)) + ser(x) for k, x in sorted(v.items()))
    if isinstance(v, bytes):
        return b"B" + _i(len(v)) + v
    return b"S" + _s(str(v))


def attrs_bytes(attrs: dict) -> bytes:
    items = sorted((k, v) for k, v in attrs.items() if k not in NON_IDENTITY_ATTRS)
    return _i(len(items)) + b"".join(_s(k) + ser(v) for k, v in items)


class KeyBuilder:
    def __init__(self, m: ModuleIR) -> None:
        self.m = m
        # net id -> list of atoms covering the net (lo-ordered)
        self.segs: dict[int, list[tuple]] = {}
        self.root_hash: dict[int, bytes] = {}
        self.in_bytes: dict[int, dict[str, bytes]] = {}

    # atoms: ('c', value, w, xmask) | ('r', hash, lo, w)
    def net_segments(self, net: Net) -> list[tuple]:
        s = self.segs.get(net.id)
        if s is None:
            s = [("r", self.root(net), 0, net.width)]
            self.segs[net.id] = s
        return s

    def root(self, net: Net) -> bytes:
        h = self.root_hash.get(net.id)
        if h is None:
            if net.port in ("in", "inout"):
                h = H(b"port" + _s(net.name or "") + _i(net.width))
            elif net.port == "out":
                h = H(b"outport" + _s(net.name or "") + _i(net.width))
            elif isinstance(net.driver, Node):
                nd = net.driver
                idx = nd.outputs.index(net)
                h = H(b"node" + (nd.shape or b"") + _i(idx) + _i(net.width))
            else:
                h = H(b"undriven" + _s(net.name or "") + _i(net.width))
            self.root_hash[net.id] = h
        return h

    def slice_atoms(self, net: Net, lo: int, w: int) -> list[tuple]:
        return cut_atoms(self.net_segments(net), lo, w)

    def sig_atoms(self, sig: SigSpec) -> list[tuple]:
        atoms: list[tuple] = []
        for c in sig.chunks:
            if isinstance(c, Const):
                new = [("c", c.value, c.width, c.xmask)]
            else:
                new = self.slice_atoms(c.net, c.lo, c.width)
            for a in new:
                if atoms:
                    last = atoms[-1]
                    if a[0] == "c" and last[0] == "c":
                        atoms[-1] = ("c", last[1] | (a[1] << last[2]), last[2] + a[2],
                                     last[3] | (a[3] << last[2]))
                        continue
                    if a[0] == "r" and last[0] == "r" and last[1] == a[1] and last[2] + last[3] == a[2]:
                        atoms[-1] = ("r", last[1], last[2], last[3] + a[3])
                        continue
                atoms.append(a)
        return atoms

    def sig_bytes(self, sig: SigSpec) -> bytes:
        parts = []
        for a in self.sig_atoms(sig):
            if a[0] == "c":
                parts.append(b"c" + _i(a[1]) + _i(a[2]) + _i(a[3]))
            else:
                parts.append(b"r" + a[1] + _i(a[2]) + _i(a[3]))
        return _i(len(parts)) + b"".join(parts)


def _state_label(m: ModuleIR, nd: Node, out: Net, cons: dict) -> str:
    if nd.op in ("INST", "BLACKBOX"):
        return nd.attrs.get("inst", f"{nd.op.lower()}")
    if nd.op == "MEM":
        return nd.attrs.get("mem_name", "mem")
    # DFF / LATCH: name of the register the output ends up in
    if out.name:
        return out.name
    best = None
    for item, _nm, _pos, ch in cons.get(out.id, ()):
        if isinstance(item, Conn) and item.src.single_slice() is not None \
                and ch.lo == 0 and ch.width == out.width and item.net.name:
            dn = item.net
            lab = dn.name if (item.lo == 0 and item.width == dn.width) else \
                f"{dn.name}[{item.lo + item.width - 1}:{item.lo}]"
            if best is None or lab < best:
                best = lab
    return best or f"_{nd.op.lower()}"


_ROLE_UNARY = {"NEG", "NOT", "POW", "RED_AND", "RED_OR", "RED_XOR", "DECODE", "ENCODE", "ROM"}


def compute_keys(m: ModuleIR, order=None) -> None:
    """Fill ``Node.shape``, ``Node.named_key`` and ``ModuleIR.canonical_hash``."""
    if order is None:
        order, _ = topo_order(m)
    cons = consumers(m)
    kb = KeyBuilder(m)
    # state outputs are sources: their root hash depends only on a label
    labels: dict[int, str] = {}
    for nd in m.nodes:
        if nd.is_state:
            for o in nd.outputs:
                lab = _state_label(m, nd, o, cons)
                labels[o.id] = lab
                if nd.op in ("INST", "BLACKBOX"):
                    pn = _port_name(nd, o)
                    kb.root_hash[o.id] = H(b"instout" + _s(nd.attrs.get("module_key") or nd.attrs.get("name", ""))
                                           + _s(lab) + _s(pn) + _i(o.width))
                elif nd.op == "MEM":
                    kb.root_hash[o.id] = H(b"memout" + _s(lab) + _s(o.name or "") + _i(o.width))
                else:
                    kb.root_hash[o.id] = H(b"reg" + _s(nd.op) + _s(lab) + _i(o.width))
    # forward: shapes
    ck: dict[int, bytes] = {}
    m.conn_keys = ck
    for it in order:
        if isinstance(it, Node):
            _node_shape(kb, it)
        else:
            ck[id(it)] = kb.sig_bytes(it.src)
            _conn_atoms(kb, it)
    # nodes outside the order (cycles were cut earlier; defensive)
    for nd in m.nodes:
        if nd.shape is None:
            _node_shape(kb, nd)
    _named_keys(m, order, cons, labels, kb)
    _module_hash(m, kb)


def _port_name(nd: Node, o: Net) -> str:
    ports = nd.attrs.get("out_ports", ())
    i = nd.outputs.index(o)
    return ports[i] if i < len(ports) else str(i)


def cut_atoms(segs: list[tuple], lo: int, w: int) -> list[tuple]:
    """Atoms covering bits ``[lo, lo+w)`` of a segment list."""
    out = []
    pos = 0
    for a in segs:
        aw = a[3] if a[0] == "r" else a[2]
        if pos + aw <= lo:
            pos += aw
            continue
        if pos >= lo + w:
            break
        x = max(lo, pos) - pos
        y = min(lo + w, pos + aw) - pos
        out.append(_cut(a, x, y - x))
        pos += aw
    return out


def _conn_atoms(kb: KeyBuilder, c: Conn) -> None:
    net = c.net
    segs = kb.net_segments(net)
    src = kb.sig_atoms(c.src)
    hi = c.lo + c.width
    kb.segs[net.id] = (cut_atoms(segs, 0, c.lo) + src + cut_atoms(segs, hi, net.width - hi))


def _cut(a: tuple, x: int, w: int) -> tuple:
    if a[0] == "r":
        return ("r", a[1], a[2] + x, w)
    return ("c", (a[1] >> x) & ((1 << w) - 1), w, (a[3] >> x) & ((1 << w) - 1))


def _node_shape(kb: KeyBuilder, nd: Node) -> None:
    ins = [(nm, kb.sig_bytes(s)) for nm, s in nd.inputs]
    attrs = nd.attrs
    if nd.op in COMMUTATIVE and len(ins) == 2:
        if ins[0][1] > ins[1][1]:
            # canonical operand order (independent of net ids and creation order)
            nd.inputs = (("A", nd.inputs[1][1]), ("B", nd.inputs[0][1]))
        ins = [("A", min(ins[0][1], ins[1][1])), ("B", max(ins[0][1], ins[1][1]))]
    elif nd.op == "SUM":
        signs = nd.attrs["signs"]
        d = dict(ins)
        sigs = dict(nd.inputs)
        tl = sorted(((signs[int(nm[1:])], bts, sigs[nm]) for nm, bts in ins if nm.startswith("T")),
                    key=lambda x: (x[0], x[1]))
        new_inputs = [(f"T{i}", sg_) for i, (_s_, _b_, sg_) in enumerate(tl)]
        nd.attrs["signs"] = tuple(t[0] for t in tl)
        out = [("T", _i(sg) + bts) for sg, bts, _x in tl]
        if "MA" in d:
            ma, mb = sigs["MA"], sigs["MB"]
            if d["MA"] > d["MB"]:
                ma, mb = mb, ma
            new_inputs += [("MA", ma), ("MB", mb)]
            x, y = sorted((d["MA"], d["MB"]))
            out.append(("M", _i(nd.attrs.get("mul_sign", 1)) + x + y))
        nd.inputs = tuple(new_inputs)
        ins = out
        attrs = {k: v for k, v in nd.attrs.items() if k not in ("signs", "mul_sign")}
    kb.in_bytes[nd.id] = dict(ins)
    data = b"shape" + _s(nd.op) + attrs_bytes(attrs)
    data += _i(len(nd.outputs)) + b"".join(_i(o.width) for o in nd.outputs)
    data += _i(len(ins)) + b"".join(_s(nm) + h for nm, h in ins)
    nd.shape = H(data)


def _roles(nd: Node) -> dict[str, str]:
    op = nd.op
    if op in COMMUTATIVE:
        return {"A": "lhs", "B": "rhs"}
    if op in _ROLE_UNARY:
        return {"A": "arg"}
    if op == "MUX":
        return {"A": "else", "B": "then", "S": "sel"}
    if op in ("PMUX", "PRIORITY"):
        return {"A": "default", "B": "case", "S": "sel"}
    if op in ("DFF",):
        return {"D": "d", "EN": "en", "SRST": "srst", "ARST": "arst", "CLK": "clk"}
    if op == "LATCH":
        return {"D": "d", "EN": "en", "RST": "rst"}
    if op in ("SUB", "DIV", "MOD", "LT", "LE", "GT", "GE", "SHL", "SHR", "SAR", "ROL", "ROR"):
        return {"A": "lhs", "B": "rhs"}
    return {}


def _named_keys(m: ModuleIR, order, cons, labels, kb: KeyBuilder) -> None:
    net_best: dict[int, tuple] = {}

    def commutative_rank(item: Node, name: str) -> str:
        ib = kb.in_bytes[item.id]
        sa = ib["A"]
        sb = ib["B"]
        if sa == sb:
            return "lhs"
        lo_first = sa < sb
        if name == "A":
            return "lhs" if lo_first else "rhs"
        return "rhs" if lo_first else "lhs"

    def role_of(item: Node, name: str, pos: int, chunk: Slice) -> str:
        sig = dict(item.inputs)[name]
        if item.op in COMMUTATIVE and len(item.inputs) == 2:
            r = commutative_rank(item, name)
        elif item.op in ("PMUX", "PRIORITY") and name == "B":
            w = item.outputs[0].width
            off = sum(c.width for c in sig.chunks[:pos])
            r = f"case{off // w}"
        elif item.op == "SUM":
            r = "term" if name.startswith("T") else "mul"
        elif item.op in ("INST", "BLACKBOX", "MEM"):
            r = name
        else:
            r = _roles(item).get(name, name.lower())
        if len(sig.chunks) > 1 and item.op not in ("PMUX", "PRIORITY"):
            r += f"[{pos}]"
        return r

    MAXP = 12      # role paths are cut after this many steps (the depth is still recorded)

    def extend(r: tuple, role: str) -> tuple:
        target, path, depth = r
        if len(path) < MAXP:
            path = path + (role,)
        return (target, path, depth + 1)

    def nt(net: Net):
        v = net_best.get(net.id)
        if v is not None:
            return v
        if net.name and not net.attrs.get("loop_cut"):
            v = (net.name, (), 0)
            net_best[net.id] = v
            return v
        cands = []
        for item, nm, pos, ch in cons.get(net.id, ()):
            if isinstance(item, Conn):
                dn = item.net
                if dn.name:
                    cands.append((dn.name, (), 0))
                else:
                    r = net_best.get(dn.id)
                    if r is not None:
                        cands.append(r)
            elif item.is_state:
                lab = labels.get(item.outputs[0].id) if item.outputs else None
                if lab is None:
                    lab = item.attrs.get("inst", item.attrs.get("mem_name", item.op.lower()))
                cands.append(extend((lab, (), 0), role_of(item, nm, pos, ch)))
            else:
                r = net_best.get(item.outputs[0].id)
                if r is not None:
                    cands.append(extend(r, role_of(item, nm, pos, ch)))
        v = min(cands) if cands else ("", (), 0)
        net_best[net.id] = v
        return v

    # consumers first
    for it in reversed(order):
        if isinstance(it, Node):
            if not it.is_state and it.outputs:
                nt(it.outputs[0])
        else:
            nt(it.net)
    for nd in m.nodes:
        if nd.outputs and nd.outputs[0].id not in net_best:
            nt(nd.outputs[0])
    groups: dict[tuple, list[Node]] = {}
    for nd in m.nodes:
        depth = 0
        if nd.is_state and nd.outputs:
            target, path = labels.get(nd.outputs[0].id, nd.op.lower()), ()
        elif nd.outputs:
            target, path, depth = net_best[nd.outputs[0].id]
        else:
            target, path = nd.attrs.get("inst", nd.attrs.get("mem_name", nd.op.lower())), ()
        ptxt = ".".join(path) + (f"~{depth}" if depth > MAXP else "")
        key = (m.name, target, ptxt, nd.op)
        groups.setdefault(key, []).append(nd)
    for key, lst in groups.items():
        if len(lst) == 1:
            lst[0].named_key = key
        else:
            lst.sort(key=lambda n: (n.shape, n.id))
            for i, nd in enumerate(lst):
                nd.named_key = key if i == 0 else (key[0], key[1], key[2] + f"#{i}", key[3])


def _module_hash(m: ModuleIR, kb: KeyBuilder) -> None:
    items = []
    for nd in m.nodes:
        items.append(b"n" + nd.shape + _i(nd.attrs.get("merge_count", 1)))
    for p in m.ports:
        if p.port in ("out", "inout"):
            items.append(b"o" + H(_s(p.name or "") + _i(p.width) + kb.sig_bytes(p.sig)))
    items.sort()
    ports = sorted((p.port or "", p.name or "", p.width, int(p.signed)) for p in m.ports)
    data = b"module"
    data += _i(len(m.params)) + b"".join(_s(str(k)) + ser(_param(v)) for k, v in m.params)
    data += _i(len(m.const_ports)) + b"".join(_s(k) + ser(v) for k, v in m.const_ports)
    data += _i(len(ports)) + b"".join(_s(d) + _s(n) + _i(w) + _i(s) for d, n, w, s in ports)
    data += _i(len(items)) + b"".join(items)
    m.canonical_hash = H(data)


def _param(v):
    if isinstance(v, Const):
        return ("c", v.value, v.width, v.xmask)
    return v
