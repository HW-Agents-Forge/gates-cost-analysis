"""Reusable richer test designs (also run by the determinism subprocess test)."""
from __future__ import annotations

import random

from gatecost.ir import (
    Builder, Const, Design, LowerBlock, Origin, SigSpec, analyze_ir, dump, export_module,
)


def k(v, w):
    return SigSpec.const(v, w)


def rich_module(order_seed: int | None = None, name: str = "rich"):
    """A module with ports, wires, case/if lowering, registers, sharing, fusion, memory
    and an instance.  ``order_seed`` shuffles the order of independent declarations and
    statements; the canonical hash and the normalized dump must not change."""
    b = Builder(name, params={"W": 8})
    rnd = random.Random(order_seed)
    decl = [("a", "in", 8), ("b", "in", 8), ("c", "in", 8), ("d", "in", 8), ("sel", "in", 2),
            ("en", "in", 1), ("rst_n", "in", 1), ("clk", "in", 1),
            ("y", "out", 8), ("z", "out", 16), ("r", "out", 8), ("m", "out", 8)]
    if order_seed is not None:
        rnd.shuffle(decl)
    P = {n: b.port(n, d, w) for n, d, w in decl}
    steps = []

    def comb_case():
        blk = LowerBlock(b, "comb", origin=Origin("rich.v", 10, 1))
        blk.assign(P["y"], P["a"].sig)
        blk.case_(P["sel"].sig, [
            ([k(0, 2)], lambda: blk.assign(P["y"], b.mul(P["b"].sig, P["c"].sig, 8))),
            ([k(1, 2)], lambda: blk.assign(P["y"], b.mul(P["c"].sig, P["d"].sig, 8))),
            ([k(2, 2)], lambda: blk.assign(P["y"], b.add(P["a"].sig, P["d"].sig, 8)))])
        blk.finish()

    def fused():
        t = b.net(16, "t")
        b.connect(t, b.mul(P["a"].sig.zext(16), P["b"].sig.zext(16), 16))
        b.connect(P["z"], b.add(t.sig, P["c"].sig.sext(16), 16, sb=True))

    def counter():
        q = b.net(8, "count")
        blk = LowerBlock(b, "clocked", clock=P["clk"], async_resets=[(P["rst_n"], 0)],
                         origin=Origin("rich.v", 30, 1))
        blk.if_(b.not_(P["rst_n"].sig), lambda: blk.assign(q, k(0, 8), True),
                lambda: blk.if_(P["en"].sig, lambda: blk.assign(q, b.add(q.sig, k(1, 8), 8), True)))
        blk.finish()
        b.connect(P["r"], q)

    def memory():
        mem = b.memory("ram", 16, 8)
        b.connect(P["m"], mem.read(P["a"].sig[0:4]))
        blk = LowerBlock(b, "clocked", clock=P["clk"])
        blk.if_(P["en"].sig, lambda: blk.mem_write(mem, P["b"].sig[0:4], P["c"].sig))
        blk.finish()

    steps = [comb_case, fused, counter, memory]
    if order_seed is not None:
        rnd.shuffle(steps)
    for s in steps:
        s()
    return b.finalize()


def design_text(order_seed=None) -> str:
    m = rich_module(order_seed)
    analyze_ir(m)
    import json
    return dump(m) + m.canonical_hash.hex() + "\n" + json.dumps(export_module(m), sort_keys=True)


if __name__ == "__main__":
    print(design_text())
