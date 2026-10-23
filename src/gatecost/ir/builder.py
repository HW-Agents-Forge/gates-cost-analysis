"""The IR builder: hash-consing, constant folding, identity rules (IR.md section 4)."""
from __future__ import annotations

from typing import Sequence

from .core import (
    COMMUTATIVE, Conn, ModuleIR, Net, Node, Origin, identity_attrs, module_key,
)
from .evalops import eval_const, ext
from .limits import DEFAULT_LIMITS, Limits
from .sigspec import Const, SigSpec, Slice, mask


class IRBuildError(Exception):
    """Programming error in a front-end (bad widths, bad operands)."""


def _fit(sig: SigSpec, w: int) -> SigSpec:
    """Zero-extend or truncate to ``w`` bits."""
    if sig.width == w:
        return sig
    return sig.zext(w) if sig.width < w else sig.slice(0, w)


def _is_const(sig: SigSpec) -> Const | None:
    return sig.const_value()


def _is_zero(sig: SigSpec) -> bool:
    c = sig.const_value()
    return c is not None and c.value == 0 and c.xmask == 0


def _is_ones(sig: SigSpec) -> bool:
    c = sig.const_value()
    return c is not None and c.xmask == 0 and c.value == mask(c.width) and c.width > 0


def _is_allx(sig: SigSpec) -> bool:
    c = sig.const_value()
    return c is not None and c.width > 0 and c.xmask == mask(c.width)


def _pow2(sig: SigSpec) -> int | None:
    """k if the signal is the constant 2**k (k >= 1), else None."""
    c = sig.const_value()
    if c is None or c.xmask or c.value < 2:
        return None
    v = c.value
    return v.bit_length() - 1 if v & (v - 1) == 0 else None


class Builder:
    """Creates a :class:`ModuleIR`.  See the package docstring for the API tour."""

    def __init__(self, name: str, params: dict | None = None, const_ports: dict | None = None,
                 limits: Limits | None = None, key: str | None = None,
                 origin: Origin | None = None) -> None:
        self.module = ModuleIR(name, params, const_ports, key)
        self.module.origin = origin
        self.limits = limits or DEFAULT_LIMITS
        self.origin: Origin | None = origin   # default origin for new nodes
        self.failed = False
        self._table: dict = {}                # hash-consing table (single dict)
        self._node_keys: dict[int, tuple] = {}
        self._removed_nodes = 0
        self._removed_nets = 0
        self._const_port_values = dict(const_ports or {})

    # ------------------------------------------------------------------ nets
    def _limit(self, what: str, detail: str) -> None:
        if not self.failed:
            self.failed = True
            self.module.limit_reached = what
            self.module.diag("LIMIT_REACHED", f"{what}: {detail}", "warning", True, self.origin)

    def new_group(self) -> int:
        g = self.module.next_group
        self.module.next_group += 1
        return g

    def net(self, width: int, name: str | None = None, signed: bool = False) -> Net:
        """Create a net (wire/reg).  It is driven later by ``connect`` or a state node."""
        return self._new_net(width, name, signed, None)

    def _new_net(self, width: int, name: str | None, signed: bool, port: str | None) -> Net:
        if width < 1:
            raise IRBuildError(f"net width must be >= 1 (got {width})")
        m = self.module
        if width > self.limits.max_net_width:
            self._limit("net width", f"{name or 'net'} has {width} bits (limit {self.limits.max_net_width})")
        elif len(m.nets) - self._removed_nets >= self.limits.max_nets:
            self._limit("net count", f"more than {self.limits.max_nets} nets")
        n = Net(m.next_net_id, width, signed, name, port)
        m.next_net_id += 1
        m.nets.append(n)
        return n

    def port(self, name: str, direction: str, width: int, signed: bool = False) -> Net:
        """Declare a module port; returns its net.  ``direction``: in / out / inout."""
        if direction not in ("in", "out", "inout"):
            raise IRBuildError(f"bad port direction {direction!r}")
        n = self._new_net(width, name, signed, direction)
        self.module.ports.append(n)
        if direction in ("in", "inout"):
            n.driver = "port"
            self.module.driven[n.id] = mask(width)
        cv = self._const_port_values.get(name)
        if cv is not None and direction == "in":
            if cv.width != width:
                cv = ext(cv, width, False)
            n.kmask = cv.kmask
            n.kval = cv.value
            n.attrs["const"] = cv
        return n

    # ------------------------------------------------------------- coercion
    def sig(self, x, lo: int = 0, width: int | None = None) -> SigSpec:
        return SigSpec.of(x, lo, width)

    def const(self, value: int, width: int, xmask: int = 0) -> SigSpec:
        return SigSpec.const(value, width, xmask)

    @staticmethod
    def _s(x) -> SigSpec:
        if isinstance(x, SigSpec):
            return x
        return SigSpec.of(x)

    # ----------------------------------------------------------- connections
    def connect(self, dst, src, origin: Origin | None = None) -> None:
        """Drive ``dst`` (a net, or a SigSpec of net slices such as a concatenation lvalue)
        from ``src``.  Wiring only: costs nothing.  Widths must match."""
        dst_s = self._s(dst)
        src_s = self._s(src)
        if dst_s.width != src_s.width:
            raise IRBuildError(f"connect width mismatch: {dst_s.width} <- {src_s.width}")
        pos = 0
        for ch in dst_s.chunks:
            if not isinstance(ch, Slice):
                raise IRBuildError("connect destination must be net slices")
            self._connect_slice(ch.net, ch.lo, src_s.slice(pos, ch.width), origin)
            pos += ch.width

    def _connect_slice(self, net: Net, lo: int, src: SigSpec, origin) -> None:
        m = self.module
        w = src.width
        bits = mask(w) << lo
        drv = m.driven.get(net.id, 0)
        over = drv & bits
        if over:
            m.diag("MULTIPLE_DRIVERS",
                   f"net {net.name or net.id}: bits {_bits_str(over)} driven more than once; "
                   "first driver kept", "warning", True, origin or self.origin)
            # keep only the undriven runs
            pos = 0
            while pos < w:
                if (over >> (lo + pos)) & 1:
                    pos += 1
                    continue
                end = pos
                while end < w and not (over >> (lo + end)) & 1:
                    end += 1
                self._connect_slice(net, lo + pos, src.slice(pos, end - pos), origin)
                pos = end
            return
        m.driven[net.id] = drv | bits
        c = Conn(net, lo, src, origin or self.origin)
        m.conns.append(c)
        m.conns_by_net.setdefault(net.id, []).append(c)
        self._ref(src, 1)

    @staticmethod
    def _ref(sig: SigSpec, d: int) -> None:
        for c in sig.chunks:
            if isinstance(c, Slice):
                c.net.fanout += d

    # ------------------------------------------------------------- node core
    def _emit(self, op: str, ins: Sequence[tuple[str, SigSpec]], out_widths: Sequence[int],
              attrs: dict, origin: Origin | None, *, cons: bool = True,
              group: int | None = None, outs: Sequence[Net] | None = None,
              out_names: Sequence[str | None] | None = None) -> tuple[Node, bool]:
        """Create (or find) a node.  Returns ``(node, existed)``."""
        ins = tuple(ins)
        if op in COMMUTATIVE and len(ins) == 2 and ins[0][1].key() > ins[1][1].key():
            ins = ((ins[0][0], ins[1][1]), (ins[1][0], ins[0][1]))
        key = None
        if cons:
            key = (op, identity_attrs(attrs), tuple([s.key() for _, s in ins]), tuple(out_widths))
            ex = self._table.get(key)
            if ex is not None and ex.alive:
                return ex, True
        m = self.module
        if len(m.nodes) - self._removed_nodes >= self.limits.max_nodes:
            self._limit("node count", f"more than {self.limits.max_nodes} nodes")
        node_outs = []
        for i, w in enumerate(out_widths):
            if outs is not None and m.driven.get(outs[i].id, 0):
                # the requested output net already has a driver: first driver kept, the new node gets a fresh net
                m.diag("MULTIPLE_DRIVERS", f"net {outs[i].name or outs[i].id} driven more than once; first driver kept",
                       "warning", True, origin or self.origin)
                n = self._new_net(w, None, False, None)
            elif outs is not None:
                n = outs[i]
            else:
                nm = out_names[i] if out_names else None
                n = self._new_net(w, nm, False, None)
            node_outs.append(n)
        node = Node(m.next_node_id, op, ins, tuple(node_outs), attrs, origin or self.origin)
        m.next_node_id += 1
        if group is not None:
            node.tags["group"] = group
        for n in node_outs:
            m.driven[n.id] = m.driven.get(n.id, 0) | mask(n.width)
            n.driver = node
        for _, s in ins:
            self._ref(s, 1)
        m.nodes.append(node)
        if key is not None:
            self._table[key] = node
            self._node_keys[node.id] = key
        return node, False

    def _result(self, node: Node) -> SigSpec:
        return node.outputs[0].sig

    def discard(self, sig: SigSpec) -> None:
        """Remove the drivers of ``sig`` if nothing else uses them (garbage collection).

        Used by block finalizers so that recognised enable/reset MUXes are never kept.
        """
        work = [c.net.driver for c in sig.chunks
                if isinstance(c, Slice) and isinstance(c.net.driver, Node)]
        seen = set()
        while work:
            node = work.pop()
            if node.id in seen or not node.alive:
                continue
            seen.add(node.id)
            if node.op in ("DFF", "LATCH", "MEM", "INST", "BLACKBOX"):
                continue
            if any(o.fanout > 0 or o.port for o in node.outputs):
                continue
            node.alive = False
            self._removed_nodes += 1
            key = self._node_keys.pop(node.id, None)
            if key is not None and self._table.get(key) is node:
                del self._table[key]
            for o in node.outputs:
                o.driver = "dead"
                self.module.driven.pop(o.id, None)
                self._removed_nets += 1
            for _, s in node.inputs:
                self._ref(s, -1)
                for c in s.chunks:
                    if isinstance(c, Slice) and isinstance(c.net.driver, Node):
                        work.append(c.net.driver)

    def _sweep(self) -> None:
        """Remove combinational nodes that nothing uses (leftovers of lowering)."""
        for node in reversed(self.module.nodes):
            if node.alive and node.op not in ("DFF", "LATCH", "MEM", "INST", "BLACKBOX"):
                if all(o.fanout == 0 and not o.port for o in node.outputs):
                    self.discard_node(node)

    def discard_node(self, node: Node) -> None:
        self.discard(node.outputs[0].sig)

    # -------------------------------------------------------- generic op API
    def op(self, op: str, *operands, width: int, sa: bool = False, sb: bool = False,
           origin: Origin | None = None, name: str | None = None, group: int | None = None,
           **attrs) -> SigSpec:
        """Generic entry point: ``b.op("ADD", a, b, width=8)``.  Returns a SigSpec."""
        fn = _DISPATCH.get(op)
        if fn is None:
            raise IRBuildError(f"unknown or non-buildable op {op}")
        ops = [self._s(o) for o in operands]
        if self.failed:
            return SigSpec.x(width)
        r = fn(self, ops, width, sa, sb, origin, name, group, attrs)
        return r

    # convenience wrappers -------------------------------------------------
    def add(self, a, b, width, sa=False, sb=False, **kw): return self.op("ADD", a, b, width=width, sa=sa, sb=sb, **kw)
    def sub(self, a, b, width, sa=False, sb=False, **kw): return self.op("SUB", a, b, width=width, sa=sa, sb=sb, **kw)
    def neg(self, a, width, sa=False, **kw): return self.op("NEG", a, width=width, sa=sa, **kw)
    def mul(self, a, b, width, sa=False, sb=False, **kw): return self.op("MUL", a, b, width=width, sa=sa, sb=sb, **kw)
    def div(self, a, b, width, sa=False, sb=False, **kw): return self.op("DIV", a, b, width=width, sa=sa, sb=sb, **kw)
    def mod(self, a, b, width, sa=False, sb=False, **kw): return self.op("MOD", a, b, width=width, sa=sa, sb=sb, **kw)
    def pow(self, a, exp, width, sa=False, **kw): return self.op("POW", a, width=width, sa=sa, exp=exp, **kw)
    def shl(self, a, b, width, sa=False, **kw): return self.op("SHL", a, b, width=width, sa=sa, **kw)
    def shr(self, a, b, width, sa=False, **kw): return self.op("SHR", a, b, width=width, sa=sa, **kw)
    def sar(self, a, b, width, sa=False, **kw): return self.op("SAR", a, b, width=width, sa=sa, **kw)
    def rol(self, a, b, width, **kw): return self.op("ROL", a, b, width=width, **kw)
    def ror(self, a, b, width, **kw): return self.op("ROR", a, b, width=width, **kw)
    def and_(self, a, b, width=None, **kw): return self.op("AND", a, b, width=self._bw(a, b, width), **kw)
    def or_(self, a, b, width=None, **kw): return self.op("OR", a, b, width=self._bw(a, b, width), **kw)
    def xor(self, a, b, width=None, **kw): return self.op("XOR", a, b, width=self._bw(a, b, width), **kw)
    def xnor(self, a, b, width=None, **kw): return self.op("XNOR", a, b, width=self._bw(a, b, width), **kw)
    def not_(self, a, width=None, **kw): return self.op("NOT", a, width=self._s(a).width if width is None else width, **kw)
    def red_and(self, a, **kw): return self.op("RED_AND", a, width=1, **kw)
    def red_or(self, a, **kw): return self.op("RED_OR", a, width=1, **kw)
    def red_xor(self, a, **kw): return self.op("RED_XOR", a, width=1, **kw)
    def eq(self, a, b, **kw): return self.op("EQ", a, b, width=1, **kw)
    def ne(self, a, b, **kw): return self.op("NE", a, b, width=1, **kw)
    def lt(self, a, b, sa=False, sb=False, **kw): return self.op("LT", a, b, width=1, sa=sa, sb=sb, **kw)
    def le(self, a, b, sa=False, sb=False, **kw): return self.op("LE", a, b, width=1, sa=sa, sb=sb, **kw)
    def gt(self, a, b, sa=False, sb=False, **kw): return self.op("GT", a, b, width=1, sa=sa, sb=sb, **kw)
    def ge(self, a, b, sa=False, sb=False, **kw): return self.op("GE", a, b, width=1, sa=sa, sb=sb, **kw)

    def _bw(self, a, b, width):
        return max(self._s(a).width, self._s(b).width) if width is None else width

    def mux(self, s, a, b, width=None, **kw):
        """``s ? b : a`` (note: ``a`` is the S=0 input, as in IR.md)."""
        a, b = self._s(a), self._s(b)
        return self.op("MUX", a, b, s, width=max(a.width, b.width) if width is None else width, **kw)

    def pmux(self, default, cases: Sequence, selects, width=None, **kw):
        """One-hot parallel mux: ``cases`` is a list of k SigSpecs, ``selects`` k 1-bit
        SigSpecs (or one k-bit SigSpec)."""
        return self._pmux_like("PMUX", default, cases, selects, width, kw)

    def priority(self, default, cases: Sequence, selects, width=None, **kw):
        """Priority-ordered mux: first set select wins."""
        return self._pmux_like("PRIORITY", default, cases, selects, width, kw)

    def _pmux_like(self, op, default, cases, selects, width, kw):
        d = self._s(default)
        cs = [self._s(c) for c in cases]
        w = d.width if width is None else width
        if isinstance(selects, (list, tuple)):
            sel = SigSpec.concat([self._s(x) for x in selects])
        else:
            sel = self._s(selects)
        if sel.width != len(cs):
            raise IRBuildError("select width must equal number of cases")
        b = SigSpec.concat([_fit(c, w) for c in cs])
        return self.op(op, d, b, sel, width=w, k=len(cs), **kw)

    # logical helpers (Verilog !, &&, ||; VHDL boolean conversions) -----------------
    def to_bool(self, x) -> SigSpec:
        """1-bit truth value of a vector (``x != 0``)."""
        s = self._s(x)
        return s if s.width == 1 else self.red_or(s)

    def lnot(self, x, **kw) -> SigSpec:
        return self.not_(self.to_bool(x), 1, **kw)

    def land(self, a, c, **kw) -> SigSpec:
        return self.and_(self.to_bool(a), self.to_bool(c), 1, **kw)

    def lor(self, a, c, **kw) -> SigSpec:
        return self.or_(self.to_bool(a), self.to_bool(c), 1, **kw)

    def index_mux(self, arms: Sequence, index, group: int | None = None, **kw) -> SigSpec:
        """Variable-index read ``arms[index]`` (array element, bit or part select) as a
        binary MUX tree of ``len(arms) - 1`` MUXes; out-of-range indices are don't-care.
        All arms are resized (zero extension) to the widest arm."""
        arms = [self._s(a) for a in arms]
        idx = self._s(index)
        w = max(a.width for a in arms)
        cur = [a.zext(w) for a in arms]
        levels = max(1, (len(cur) - 1).bit_length()) if len(cur) > 1 else 0
        for lvl in range(min(levels, idx.width)):
            if len(cur) % 2:
                cur.append(SigSpec.x(w))
            cur = [self.mux(idx.slice(lvl, 1), cur[i], cur[i + 1], w, group=group, **kw)
                   for i in range(0, len(cur), 2)]
        return cur[0]

    def decode(self, a, width=None, **kw):
        a = self._s(a)
        return self.op("DECODE", a, width=(1 << a.width) if width is None else width, **kw)

    def encode(self, a, width=None, kind="priority", **kw):
        a = self._s(a)
        return self.op("ENCODE", a, width=max(1, (a.width - 1).bit_length()) if width is None else width,
                       kind=kind, **kw)

    def rom(self, addr, contents: Sequence[int], width: int, **kw):
        addr = self._s(addr)
        return self.op("ROM", addr, width=width, contents=tuple(int(c) & mask(width) for c in contents),
                       depth=len(contents), **kw)

    # ----------------------------------------------------------------- state
    def dff(self, d, clk, *, en=None, srst=None, arst=None, srst_value=None, arst_value=None,
            srst_polarity: int = 1, arst_polarity: int = 1, clk_edge: str = "pos",
            init: int | None = None, enable_recognized: bool = False, q: Net | None = None,
            origin: Origin | None = None, name: str | None = None) -> SigSpec:
        """Flip-flop bank.  ``en`` is active high.  Reset values are ints/Consts of the
        register width.  If ``q`` is given the flop drives that existing net."""
        d = self._s(d)
        w = d.width
        if self.failed:
            return SigSpec.x(w)
        clk = self._s(clk)
        ins: list = [("D", d), ("CLK", clk)]
        attrs: dict = {"clk_edge": clk_edge, "enable_recognized": bool(enable_recognized),
                       "init": init, "merge_count": 1}
        if en is not None:
            en = self._s(en)
            if _is_const(en) is not None and _is_ones(en):
                en = None
        if en is not None:
            ins.append(("EN", en))
        sv = av = None
        if srst is not None:
            ins.append(("SRST", self._s(srst)))
            sv = _rv(srst_value, w)
            attrs["srst_value"] = sv
            attrs["srst_polarity"] = srst_polarity
        if arst is not None:
            ins.append(("ARST", self._s(arst)))
            av = _rv(arst_value, w)
            attrs["reset_value"] = av
            attrs["arst_polarity"] = arst_polarity
        kind = "none"
        if srst is not None and arst is not None:
            kind = "both"
        elif srst is not None:
            kind = "sync"
        elif arst is not None:
            kind = "async"
        attrs["reset_kind"] = kind
        if kind == "sync":
            attrs["reset_value"] = sv
        return self._state_node("DFF", ins, w, attrs, q, origin, name)

    def latch(self, d, en, *, rst=None, rst_value=None, rst_polarity: int = 1, polarity: int = 1,
              inferred: bool = False, q: Net | None = None, origin: Origin | None = None,
              name: str | None = None) -> SigSpec:
        d = self._s(d)
        w = d.width
        if self.failed:
            return SigSpec.x(w)
        ins: list = [("D", d), ("EN", self._s(en))]
        attrs: dict = {"polarity": polarity, "inferred": bool(inferred), "merge_count": 1}
        if rst is not None:
            ins.append(("RST", self._s(rst)))
            attrs["reset_value"] = _rv(rst_value, w)
            attrs["rst_polarity"] = rst_polarity
        return self._state_node("LATCH", ins, w, attrs, q, origin, name)

    def _state_node(self, op, ins, w, attrs, q, origin, name) -> SigSpec:
        if q is not None and q.width != w:
            raise IRBuildError(f"{op} output net width {q.width} != data width {w}")
        node, existed = self._emit(op, ins, [w], attrs, origin,
                                   outs=[q] if (q is not None) else None,
                                   out_names=[name])
        if existed:
            node.attrs["merge_count"] = node.attrs.get("merge_count", 1) + 1
            res = node.outputs[0].sig
            if q is not None:
                self.connect(q, res, origin)
                return q.sig
            return res
        return node.outputs[0].sig

    # ----------------------------------------------------------------- memory
    def memory(self, name: str, depth: int, width: int, origin: Origin | None = None) -> "MemHandle":
        if depth > self.limits.max_mem_depth:
            self._limit("memory depth", f"{name} has {depth} words (limit {self.limits.max_mem_depth})")
        return MemHandle(self, name, depth, width, origin or self.origin)

    # ----------------------------------------------------------- hierarchy
    def inst(self, module_key_: str, inst_name: str, inputs: dict, outputs: dict,
             count: int = 1, origin: Origin | None = None, op: str = "INST",
             **attrs) -> dict[str, SigSpec]:
        """Instance of a child Module IR (or ``blackbox`` below).

        ``inputs``: port name -> SigSpec; ``outputs``: port name -> Net (existing, to be
        driven by the instance) or int (width; a fresh net is created).  Returns a dict
        port name -> SigSpec of the output nets.  Instances are never hash-consed.
        """
        if self.failed:
            return {k: SigSpec.x(v if isinstance(v, int) else v.width) for k, v in outputs.items()}
        ins = tuple((p, self._s(s)) for p, s in sorted(inputs.items()))
        onames = sorted(outputs)
        outs = []
        for p in onames:
            o = outputs[p]
            outs.append(o if isinstance(o, Net) else self._new_net(o, None, False, None))
        a = dict(attrs)
        a["inst"] = inst_name          # instance name (hierarchical path element)
        a["count"] = count
        a["out_ports"] = tuple(onames)
        if op == "INST":
            a["module_key"] = module_key_
        else:
            a["name"] = module_key_    # library unit name (IR.md 3.1: BLACKBOX attr `name`)
        node, _ = self._emit(op, ins, [o.width for o in outs], a, origin, cons=False,
                             outs=outs)
        return {p: o.sig for p, o in zip(onames, outs)}

    def blackbox(self, name: str, inst_name: str, inputs: dict, outputs: dict,
                 count: int = 1, origin: Origin | None = None, **attrs) -> dict[str, SigSpec]:
        """Instance of an unknown or library unit (cost from the profile)."""
        return self.inst(name, inst_name, inputs, outputs, count, origin, op="BLACKBOX", **attrs)

    # ------------------------------------------------------------- finalize
    def defer_diag(self, net, code: str, message: str, severity: str = "warning", affects_cost: bool = False,
                   origin: Origin | None = None) -> None:
        """Diagnostic about ``net`` that is kept only if the net is read (or is a port) in the finished module."""
        pend = self.__dict__.setdefault("_deferred_diags", [])
        pend.append((net, code, message, severity, affects_cost, origin))

    def finalize(self, check: str = "raise") -> ModuleIR:
        """Finish the module: cut combinational loops, filter removed objects and check
        invariants (``check``: ``"raise"``, ``"diag"`` or ``"off"``)."""
        from .check import check_module, cut_loops

        m = self.module
        if self.failed:
            return make_blackbox_module(m)
        for mh in getattr(self, "_mems", ()):
            mh.seal()
        self._sweep()
        m.nodes = [n for n in m.nodes if n.alive]
        m.nets = [n for n in m.nets if n.driver != "dead"]
        m.recount_fanout()
        for net, code, message, severity, affects, origin in self.__dict__.get("_deferred_diags", ()):
            if net.fanout > 0 or net.port:
                m.diag(code, message, severity, affects, origin)
        cut_loops(m)
        if check != "off":
            problems = check_module(m)
            if problems:
                if check == "raise":
                    from .check import IRInvariantError
                    raise IRInvariantError(problems)
                for p in problems:
                    m.diag("INTERNAL", f"IR invariant violated: {p}", "error", True)
        return m


def make_blackbox_module(m: ModuleIR) -> ModuleIR:
    """Replace the body of ``m`` by an opaque black box keeping its ports (limits)."""
    m.nodes = []
    m.conns = []
    m.conns_by_net = {}
    m.nets = list(m.ports)
    m.driven = {p.id: (mask(p.width) if p.port in ("in", "inout") else 0) for p in m.ports}
    for p in m.ports:
        p.fanout = 0
        p.driver = "port" if p.port in ("in", "inout") else None
    m.is_blackbox = True
    return m


def _rv(v, w: int) -> Const:
    if v is None:
        return Const(0, w)
    if isinstance(v, Const):
        return ext(v, w, False)
    return Const(int(v), w)


def _bits_str(m: int) -> str:
    return ",".join(str(i) for i in range(m.bit_length()) if (m >> i) & 1)


class MemHandle:
    """Collects the read/write ports of one memory; the MEM node is created up front so
    asynchronous read data can be used immediately."""

    def __init__(self, b: Builder, name: str, depth: int, width: int, origin) -> None:
        self.b = b
        self.name = name
        self.depth = depth
        self.width = width
        self.origin = origin
        self.reads: list[dict] = []
        self.writes: list[dict] = []
        self.node: Node | None = None
        self._outs: list[Net] = []
        b.__dict__.setdefault("_mems", []).append(self)
        self._sealed = False
        self._pending: list[tuple[str, SigSpec]] = []

    def read(self, addr, *, clk=None, en=None, clk_edge: str = "pos", origin=None) -> SigSpec:
        """Add a read port.  Synchronous when ``clk`` is given, else asynchronous."""
        b = self.b
        addr = b._s(addr)
        i = len(self.reads)
        out = b._new_net(self.width, f"{self.name}.r{i}", False, None)
        self.reads.append({"addr": addr, "clk": None if clk is None else b._s(clk), "en": None if en is None else b._s(en),
                           "edge": clk_edge, "out": out})
        return out.sig

    def write(self, addr, data, *, en=None, clk=None, clk_edge: str = "pos", origin=None,
              lane=None) -> None:
        """Add a write port.  ``lane=(lo, width)`` marks a partial-word write (``data`` is then
        the lane value only): at ``seal`` time lane writes with the same address and clock and
        disjoint lanes are merged into ONE port with a per-lane enable vector (input
        ``w<i>_be``, attribute ``wlanes``), like a RAM with byte enables."""
        b = self.b
        w = {"addr": b._s(addr), "en": None if en is None else b._s(en),
             "clk": None if clk is None else b._s(clk), "edge": clk_edge}
        if lane is None:
            w["data"] = _fit(b._s(data), self.width)
        else:
            lo, lw = lane
            w["lane"] = (lo, lw)
            w["data"] = _fit(b._s(data), lw)
        self.writes.append(w)

    def _merge_lanes(self) -> None:
        """Merge disjoint partial-word writes (see :meth:`write`) into byte-enable ports."""
        b = self.b
        groups: dict = {}
        for w in self.writes:
            if "lane" in w:
                k = (w["addr"].key(), None if w["clk"] is None else w["clk"].key(), w["edge"])
                groups.setdefault(k, []).append(w)
        merged_ids: dict = {}
        for k, ws in groups.items():
            ws.sort(key=lambda w: w["lane"][0])
            ok = True
            end = 0
            for w in ws:
                lo, lw = w["lane"]
                if lo < end:
                    ok = False
                    break
                end = lo + lw
            if ok and len(ws) > 1:
                merged_ids[id(ws[0])] = ws
                for w in ws[1:]:
                    merged_ids[id(w)] = None
        out = []
        for w in self.writes:
            if "lane" not in w:
                out.append(w)
                continue
            if id(w) not in merged_ids:
                lo, lw = w["lane"]       # lone or overlapping lane: a full-word port with the lane placed in it
                d = SigSpec.concat([_x(lo), w["data"], _x(self.width - lo - lw)]) if (lo or lw < self.width) else w["data"]
                nw = dict(w)
                del nw["lane"]
                nw["data"] = d
                nw["lanes"] = ((lo, lw),)
                nw["be"] = None
                out.append(nw)
                continue
            ws = merged_ids[id(w)]
            if ws is None:
                continue
            parts = []
            pos = 0
            bes = []
            for x in ws:
                lo, lw = x["lane"]
                if lo > pos:
                    parts.append(_x(lo - pos))
                parts.append(x["data"])
                pos = lo + lw
                bes.append(x["en"] if x["en"] is not None else SigSpec.const(1, 1))
            if pos < self.width:
                parts.append(_x(self.width - pos))
            en = None          # the per-lane enables carry the write enable
            nw = {"addr": w["addr"], "data": SigSpec.concat(parts), "en": en, "clk": w["clk"], "edge": w["edge"],
                  "lanes": tuple(x["lane"] for x in ws), "be": SigSpec.concat(bes)}
            out.append(nw)
        self.writes = out

    def seal(self) -> None:
        if self._sealed:
            return
        self._sealed = True
        b = self.b
        if any("lane" in w for w in self.writes):
            self._merge_lanes()
        ins: list = []
        rstyles = []
        for i, r in enumerate(self.reads):
            ins.append((f"r{i}_addr", r["addr"]))
            if r["en"] is not None:
                ins.append((f"r{i}_en", r["en"]))
            if r["clk"] is not None:
                ins.append((f"r{i}_clk", r["clk"]))
            rstyles.append("sync" if r["clk"] is not None else "async")
        for i, w in enumerate(self.writes):
            ins.append((f"w{i}_addr", w["addr"]))
            ins.append((f"w{i}_data", w["data"]))
            if w["en"] is not None:
                ins.append((f"w{i}_en", w["en"]))
            if w.get("be") is not None:
                ins.append((f"w{i}_be", w["be"]))
            if w["clk"] is not None:
                ins.append((f"w{i}_clk", w["clk"]))
        style = "none"
        if rstyles:
            style = rstyles[0] if all(s == rstyles[0] for s in rstyles) else "mixed"
        attrs = {"depth": self.depth, "width": self.width, "rports": len(self.reads),
                 "wports": len(self.writes), "read_style": style, "rstyles": tuple(rstyles),
                 "mem_name": self.name}
        lanes = tuple(w.get("lanes") for w in self.writes)
        if any(l is not None and (len(l) > 1 or l[0][1] < self.width) for l in lanes):
            attrs["wlanes"] = tuple(l if l is not None else ((0, self.width),) for l in lanes)
        outs = [r["out"] for r in self.reads]
        if not outs:
            outs = []
        node, _ = b._emit("MEM", ins, [o.width for o in outs], attrs, self.origin, cons=False,
                          outs=outs)
        self.node = node


# --------------------------------------------------------------------------
# Per-operation construction rules
# --------------------------------------------------------------------------
def _mk(b: Builder, op, ins, widths, attrs, origin, name, group) -> SigSpec:
    node, _ = b._emit(op, ins, widths, attrs, origin, group=group, out_names=[name])
    return node.outputs[0].sig


def _x(w: int) -> SigSpec:
    return SigSpec.x(w)


def _fold_all_const(op: str, sigs: list[SigSpec], attrs: dict, w: int) -> SigSpec | None:
    cs = []
    for s in sigs:
        c = s.const_value()
        if c is None:
            return None
        cs.append(c)
    return SigSpec((eval_const(op, cs, attrs, w),))


def _arith2(b, ops, w, sa, sb, origin, name, group, attrs, op):
    A, B = ops
    sa = bool(sa and A.width < w)
    sb = bool(sb and B.width < w)
    A = A.resize(w, sa)
    B = B.resize(w, sb)
    at = {"sa": sa, "sb": sb}
    r = _fold_all_const(op, [A, B], at, w)
    if r is not None:
        return r
    if op == "ADD":
        if _is_zero(A) or _is_allx(A):
            return B
        if _is_zero(B) or _is_allx(B):
            return A
    elif op == "SUB":
        if _is_zero(B) or _is_allx(B):
            return A
        if A == B:
            return SigSpec.const(0, w)
    else:  # MUL
        if _is_zero(A) or _is_zero(B) or _is_allx(A) or _is_allx(B):
            return SigSpec.const(0, w)
        for X, Y in ((A, B), (B, A)):
            c = X.const_value()
            if c is not None and not c.xmask and c.value == 1:
                return Y
        for X, Y in ((A, B), (B, A)):
            k = _pow2(X)
            if k is not None:
                return SigSpec.const(0, w) if k >= w else SigSpec.concat([SigSpec.const(0, k), Y.slice(0, w - k)])
    if op == "SUB" and A == B:
        return SigSpec.const(0, w)
    return _mk(b, op, [("A", A), ("B", B)], [w], at, origin, name, group)


def _op_add(b, ops, w, sa, sb, o, n, g, at): return _arith2(b, ops, w, sa, sb, o, n, g, at, "ADD")
def _op_sub(b, ops, w, sa, sb, o, n, g, at): return _arith2(b, ops, w, sa, sb, o, n, g, at, "SUB")
def _op_mul(b, ops, w, sa, sb, o, n, g, at): return _arith2(b, ops, w, sa, sb, o, n, g, at, "MUL")


def _op_neg(b, ops, w, sa, sb, o, n, g, at):
    (A,) = ops
    sa = bool(sa and A.width < w)
    A = A.resize(w, sa)
    r = _fold_all_const("NEG", [A], {"sa": sa}, w)
    if r is not None:
        return r
    return _mk(b, "NEG", [("A", A)], [w], {"sa": sa}, o, n, g)


def _divmod(b, ops, w, sa, sb, o, n, g, at, op):
    A, B = ops
    cw = max(A.width, B.width)
    A = A.resize(cw, sa)
    B = B.resize(cw, sb)
    s = bool(sa and sb)
    attrs = {"sa": s, "sb": s}
    r = _fold_all_const(op, [A, B], attrs, w)
    if r is not None:
        return r
    if not s:
        k = _pow2(B)
        if k is not None:
            if op == "DIV":
                return A.slice(min(k, cw), max(cw - k, 0)).resize(w)
            return A.slice(0, min(k, cw)).resize(w)
        c = B.const_value()
        if c is not None and not c.xmask and c.value == 1:
            return A.resize(w) if op == "DIV" else SigSpec.const(0, w)
    return _mk(b, op, [("A", A), ("B", B)], [w], attrs, o, n, g)


def _op_div(b, ops, w, sa, sb, o, n, g, at): return _divmod(b, ops, w, sa, sb, o, n, g, at, "DIV")
def _op_mod(b, ops, w, sa, sb, o, n, g, at): return _divmod(b, ops, w, sa, sb, o, n, g, at, "MOD")


def _op_pow(b, ops, w, sa, sb, o, n, g, at):
    (A,) = ops
    exp = int(at["exp"])
    sa = bool(sa and A.width < w)
    A = A.resize(w, sa)
    attrs = {"sa": sa, "exp": exp}
    r = _fold_all_const("POW", [A], attrs, w)
    if r is not None:
        return r
    if exp == 0:
        return SigSpec.const(1, w)
    if exp == 1:
        return A
    return _mk(b, "POW", [("A", A)], [w], attrs, o, n, g)


def _shift(b, ops, w, sa, sb, o, n, g, at, op):
    A, B = ops
    sa = bool(sa and A.width < w) if op in ("SHL", "SHR", "SAR") else False
    A = A.resize(w, sa)
    attrs = {"sa": sa} if op in ("SHL", "SHR", "SAR") else {}
    r = _fold_all_const(op, [A, B], attrs, w)
    if r is not None:
        return r
    c = B.const_value()
    if c is not None:
        if c.xmask:
            return _x(w)
        k = c.value
        if op == "SHL":
            return SigSpec.const(0, w) if k >= w else SigSpec.concat([SigSpec.const(0, k), A.slice(0, w - k)])
        if op == "SHR":
            return SigSpec.const(0, w) if k >= w else A.slice(k, w - k).zext(w)
        if op == "SAR":
            return A.slice(w - 1, 1).repeat(w) if k >= w else A.slice(k, w - k).sext(w)
        k %= w
        if op == "ROL":
            k = (w - k) % w     # rotate left by k == low part is the top k bits
            return A if k == 0 else SigSpec.concat([A.slice(k, w - k), A.slice(0, k)])
        return A if k == 0 else SigSpec.concat([A.slice(k, w - k), A.slice(0, k)])
    if _is_zero(A):
        return SigSpec.const(0, w)
    return _mk(b, op, [("A", A), ("B", B)], [w], attrs, o, n, g)


def _mk_shift(op):
    return lambda b, ops, w, sa, sb, o, n, g, at: _shift(b, ops, w, sa, sb, o, n, g, at, op)


def _bitwise2(b, ops, w, sa, sb, o, n, g, at, op):
    A, B = _fit(ops[0], w), _fit(ops[1], w)
    r = _fold_all_const(op, [A, B], {}, w)
    if r is not None:
        return r
    for X, Y in ((A, B), (B, A)):
        if _is_allx(X):
            return Y
    if op == "AND":
        if _is_zero(A) or _is_zero(B):
            return SigSpec.const(0, w)
        if _is_ones(A):
            return B
        if _is_ones(B):
            return A
        if A == B:
            return A
    elif op == "OR":
        if _is_zero(A):
            return B
        if _is_zero(B):
            return A
        if _is_ones(A) or _is_ones(B):
            return SigSpec.const(mask(w), w)
        if A == B:
            return A
    elif op == "XOR":
        if A == B:
            return SigSpec.const(0, w)
        if _is_zero(A):
            return B
        if _is_zero(B):
            return A
    else:  # XNOR
        if A == B:
            return SigSpec.const(mask(w), w)
        if _is_ones(A):
            return B
        if _is_ones(B):
            return A
        if _is_zero(A):
            return b.not_(B, w, origin=o)
        if _is_zero(B):
            return b.not_(A, w, origin=o)
    return _mk(b, op, [("A", A), ("B", B)], [w], {}, o, n, g)


def _mk_bw(op):
    return lambda b, ops, w, sa, sb, o, n, g, at: _bitwise2(b, ops, w, sa, sb, o, n, g, at, op)


def _op_not(b, ops, w, sa, sb, o, n, g, at):
    A = _fit(ops[0], w)
    r = _fold_all_const("NOT", [A], {}, w)
    if r is not None:
        return r
    sl = A.single_slice()
    if sl is not None and sl.lo == 0 and sl.width == sl.net.width:
        d = sl.net.driver
        if isinstance(d, Node) and d.op == "NOT" and d.alive and d.outputs[0] is sl.net:
            return d.inputs[0][1]
    return _mk(b, "NOT", [("A", A)], [w], {}, o, n, g)


def _red(b, ops, w, sa, sb, o, n, g, at, op):
    (A,) = ops
    r = _fold_all_const(op, [A], {}, 1)
    if r is not None:
        return r
    if A.width == 1:
        return A
    return _mk(b, op, [("A", A)], [1], {}, o, n, g)


def _mk_red(op):
    return lambda b, ops, w, sa, sb, o, n, g, at: _red(b, ops, w, sa, sb, o, n, g, at, op)


def _eqne(b, ops, w, sa, sb, o, n, g, at, op):
    A, B = ops
    cw = max(A.width, B.width)
    A, B = A.zext(cw), B.zext(cw)
    r = _fold_all_const(op, [A, B], {}, 1)
    if r is not None:
        return r
    if A == B:
        return SigSpec.const(1 if op == "EQ" else 0, 1)
    if cw == 1:
        # one-bit comparison with a constant is a wire or an inverter (language neutrality:
        # VHDL ``en = '1'`` and Verilog ``en`` give the same IR)
        for X, Y in ((A, B), (B, A)):
            c = X.const_value()
            if c is not None and not c.xmask:
                same = (c.value == 1) == (op == "EQ")
                return Y if same else b.not_(Y, 1, origin=o)
    return _mk(b, op, [("A", A), ("B", B)], [1], {}, o, n, g)


def _mk_eq(op):
    return lambda b, ops, w, sa, sb, o, n, g, at: _eqne(b, ops, w, sa, sb, o, n, g, at, op)


def _cmp(b, ops, w, sa, sb, o, n, g, at, op):
    A, B = ops
    cw = max(A.width, B.width)
    A = A.resize(cw, sa)
    B = B.resize(cw, sb)
    s = bool(sa and sb)
    attrs = {"sa": s, "sb": s}
    r = _fold_all_const(op, [A, B], attrs, 1)
    if r is not None:
        return r
    if A == B:
        return SigSpec.const(1 if op in ("LE", "GE") else 0, 1)
    return _mk(b, op, [("A", A), ("B", B)], [1], attrs, o, n, g)


def _mk_cmp(op):
    return lambda b, ops, w, sa, sb, o, n, g, at: _cmp(b, ops, w, sa, sb, o, n, g, at, op)


def _op_mux(b, ops, w, sa, sb, o, n, g, at):
    A, B, S = _fit(ops[0], w), _fit(ops[1], w), ops[2]
    if S.width != 1:
        raise IRBuildError(f"MUX select must be 1 bit (got {S.width})")
    tri = bool(at.get("tristate", False))
    attrs = {"tristate": True} if tri else {}
    sc = S.const_value()
    if sc is not None:
        if sc.xmask:
            return A
        return B if sc.value else A
    if A == B:
        return A
    if not tri:
        if _is_allx(A):
            return B
        if _is_allx(B):
            return A
    ca, cb = A.const_value(), B.const_value()
    if ca is not None and cb is not None and not tri and not at.get("raw"):
        return _mux_const_data(b, S, ca, cb, w, o)
    return _mk(b, "MUX", [("A", A), ("B", B), ("S", S)], [w], attrs, o, n, g)


def _mux_const_data(b: Builder, S: SigSpec, ca: Const, cb: Const, w: int, o) -> SigSpec:
    """MUX with two constant data inputs: per-bit logic on S (wire / NOT / constant)."""
    parts: list[SigSpec] = []
    nots: SigSpec | None = None
    for i in range(w):
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
            if nots is None:
                nots = b.not_(S, 1, origin=o)
            parts.append(nots)
    return SigSpec.concat(parts)


def _op_pmux(b, ops, w, sa, sb, o, n, g, at, op="PMUX"):
    A = _fit(ops[0], w)
    k = int(at.get("k", ops[2].width))
    S = ops[2]
    B = ops[1]
    if B.width != k * w or S.width != k:
        raise IRBuildError(f"{op}: expected B width {k*w} and S width {k}")
    arms = []     # (select bit, data)
    default = A
    sels = S.split(1)
    datas = B.split(w) if w > 0 else [B.slice(0, 0)] * k
    for i in range(k):
        si = sels[i]
        di = datas[i]
        c = si.const_value()
        if c is not None:
            if c.xmask or c.value == 0:
                continue
            # constant 1: this arm wins over everything after it (first-hit semantics)
            default = di
            break
        arms.append((si, di))
    if not arms:
        return default
    if all(d == default for _, d in arms):
        return default
    if len(arms) == 1:
        return _op_mux(b, [default, arms[0][1], arms[0][0]], w, False, False, o, n, g,
                       {"raw": at.get("raw", False)})
    Sn = SigSpec.concat([s for s, _ in arms])
    Bn = SigSpec.concat([d for _, d in arms])
    return _mk(b, op, [("A", default), ("B", Bn), ("S", Sn)], [w], {"k": len(arms)}, o, n, g)


def _op_prio(b, ops, w, sa, sb, o, n, g, at):
    return _op_pmux(b, ops, w, sa, sb, o, n, g, at, "PRIORITY")


def _op_decode(b, ops, w, sa, sb, o, n, g, at):
    (A,) = ops
    if w > (1 << A.width):
        raise IRBuildError("DECODE output wider than 2**input width")
    r = _fold_all_const("DECODE", [A], {}, w)
    if r is not None:
        return r
    return _mk(b, "DECODE", [("A", A)], [w], {}, o, n, g)


def _op_encode(b, ops, w, sa, sb, o, n, g, at):
    (A,) = ops
    attrs = {"kind": at.get("kind", "priority")}
    r = _fold_all_const("ENCODE", [A], attrs, w)
    if r is not None:
        return r
    return _mk(b, "ENCODE", [("A", A)], [w], attrs, o, n, g)


def _op_rom(b, ops, w, sa, sb, o, n, g, at):
    (A,) = ops
    contents = tuple(at["contents"])
    attrs = {"contents": contents, "depth": len(contents), "width": w}
    r = _fold_all_const("ROM", [A], attrs, w)
    if r is not None:
        return r
    return _mk(b, "ROM", [("A", A)], [w], attrs, o, n, g)


_DISPATCH = {
    "ADD": _op_add, "SUB": _op_sub, "MUL": _op_mul, "NEG": _op_neg,
    "DIV": _op_div, "MOD": _op_mod, "POW": _op_pow,
    "SHL": _mk_shift("SHL"), "SHR": _mk_shift("SHR"), "SAR": _mk_shift("SAR"),
    "ROL": _mk_shift("ROL"), "ROR": _mk_shift("ROR"),
    "AND": _mk_bw("AND"), "OR": _mk_bw("OR"), "XOR": _mk_bw("XOR"), "XNOR": _mk_bw("XNOR"),
    "NOT": _op_not,
    "RED_AND": _mk_red("RED_AND"), "RED_OR": _mk_red("RED_OR"), "RED_XOR": _mk_red("RED_XOR"),
    "EQ": _mk_eq("EQ"), "NE": _mk_eq("NE"),
    "LT": _mk_cmp("LT"), "LE": _mk_cmp("LE"), "GT": _mk_cmp("GT"), "GE": _mk_cmp("GE"),
    "MUX": _op_mux, "PMUX": _op_pmux, "PRIORITY": _op_prio,
    "DECODE": _op_decode, "ENCODE": _op_encode, "ROM": _op_rom,
}
