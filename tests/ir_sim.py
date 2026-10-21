"""Tiny reference simulator for IR modules (tests only).

Don't-care bits are read as 0.  Registers are updated with ``step``; latches are
transparent (their output is computed from D when enabled, else the stored state).
"""
from __future__ import annotations

from gatecost.ir import Const, Slice, mask
from gatecost.ir.core import Conn, Node
from gatecost.ir.evalops import eval_const
from gatecost.ir.graph import topo_order


class Sim:
    def __init__(self, m) -> None:
        self.m = m
        self.order, cyc = topo_order(m)
        assert not cyc, "combinational loop"
        self.state: dict[int, int] = {}      # DFF/LATCH node id -> value
        # registers with a reset start in their reset state (the IR treats a register whose D
        # equals its reset value as a constant)
        for nd in m.nodes:
            if nd.op == "DFF" and nd.attrs.get("reset_kind", "none") != "none":
                self.state[nd.id] = nd.attrs["reset_value"].value
        self.mem: dict[int, dict[int, int]] = {}

    def sig(self, sig, vals) -> int:
        v = pos = 0
        for c in sig.chunks:
            if isinstance(c, Const):
                x = c.value
            else:
                x = (vals.get(c.net.id, 0) >> c.lo) & mask(c.width)
            v |= x << pos
            pos += c.width
        return v

    def comb(self, inputs: dict[str, int]) -> dict[int, int]:
        """Evaluate combinational logic; returns net id -> value."""
        vals: dict[int, int] = {}
        for p in self.m.ports:
            if p.port in ("in", "inout"):
                vals[p.id] = inputs.get(p.name, 0) & mask(p.width)
        # state outputs first
        for nd in self.m.nodes:
            if nd.op in ("DFF", "LATCH"):
                vals[nd.outputs[0].id] = self.state.get(nd.id, 0)
        for it in self.order:
            if isinstance(it, Conn):
                v = self.sig(it.src, vals)
                cur = vals.get(it.net.id, 0)
                mm = mask(it.width) << it.lo
                vals[it.net.id] = (cur & ~mm) | (v << it.lo)
                continue
            nd = it
            if nd.op == "DFF":
                continue
            if nd.op == "LATCH":
                ins = dict(nd.inputs)
                en = self.sig(ins["EN"], vals)
                if en == nd.attrs.get("polarity", 1):
                    self.state[nd.id] = self.sig(ins["D"], vals)
                vals[nd.outputs[0].id] = self.state.get(nd.id, 0)
                continue
            if nd.op in ("MEM", "INST", "BLACKBOX"):
                if nd.op == "MEM":
                    ins = dict(nd.inputs)
                    for i, o in enumerate(nd.outputs):
                        a = self.sig(ins[f"r{i}_addr"], vals)
                        vals[o.id] = self.mem.get(nd.id, {}).get(a, 0)
                continue
            vals[nd.outputs[0].id] = self.eval_node(nd, vals)
        return vals

    def eval_node(self, nd: Node, vals) -> int:
        ins = dict(nd.inputs)
        w = nd.outputs[0].width
        if nd.op == "SUM":
            total = 0
            signs = nd.attrs["signs"]
            for i, sg in enumerate(signs):
                total += sg * self.sig(ins[f"T{i}"], vals)
            if "MA" in ins:
                total += nd.attrs["mul_sign"] * self.sig(ins["MA"], vals) * self.sig(ins["MB"], vals)
            return total & mask(w)
        cs = []
        for k in ("A", "B", "S"):
            if k in ins:
                cs.append(Const(self.sig(ins[k], vals), ins[k].width))
        c = eval_const(nd.op, cs, nd.attrs, w)
        return c.value

    def outputs(self, vals) -> dict[str, int]:
        return {p.name: vals.get(p.id, 0) for p in self.m.ports if p.port in ("out", "inout")}

    def step(self, inputs: dict[str, int]) -> dict[str, int]:
        """One clock edge: evaluate, then update registers.  Returns outputs *before* the edge."""
        vals = self.comb(inputs)
        outs = self.outputs(vals)
        nxt = {}
        for nd in self.m.nodes:
            if nd.op != "DFF":
                continue
            ins = dict(nd.inputs)
            q = self.state.get(nd.id, 0)
            a = nd.attrs
            if "ARST" in ins and self.sig(ins["ARST"], vals) == a.get("arst_polarity", 1):
                nxt[nd.id] = a["reset_value"].value
                continue
            if "SRST" in ins and self.sig(ins["SRST"], vals) == a.get("srst_polarity", 1):
                v = a["reset_value"] if a["reset_kind"] == "sync" else a["srst_value"]
                nxt[nd.id] = v.value
                continue
            if "EN" in ins and not self.sig(ins["EN"], vals):
                nxt[nd.id] = q
                continue
            nxt[nd.id] = self.sig(ins["D"], vals)
        self.state.update(nxt)
        return outs

    def apply_async(self, inputs: dict[str, int]) -> None:
        """Apply asynchronous resets that are currently active."""
        vals = self.comb(inputs)
        for nd in self.m.nodes:
            if nd.op == "DFF" and "ARST" in dict(nd.inputs):
                if self.sig(dict(nd.inputs)["ARST"], vals) == nd.attrs.get("arst_polarity", 1):
                    self.state[nd.id] = nd.attrs["reset_value"].value
