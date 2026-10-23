"""A single large Module IR for the IR speed benchmark (IR.md section 11)."""
from __future__ import annotations

import random

from tests.ir_dsl import Mod, cat


def build_big_module(n_nodes: int, seed: int = 1, width: int = 32):
    """Build (without analyzing) a module of about ``n_nodes`` nodes: registered datapath stages
    mixing add/sub/mul/compare/mux/logic/shift, every stage observable through an output."""
    rnd = random.Random(seed)
    m = Mod("big")
    clk = m.inp("clk", 1)
    rst = m.inp("rst_n", 1)
    ins = [m.inp(f"i{k}", width) for k in range(8)]
    sels = [m.inp(f"s{k}", 1) for k in range(4)]
    pool = list(ins)
    outs = []
    k = 0
    while len(m.b.module.nodes) < n_nodes:
        a, b = rnd.choice(pool), rnd.choice(pool)
        c = rnd.choice(pool)
        s = rnd.choice(sels)
        sel = rnd.random()
        if sel < 0.2:
            v = (a + b).trunc(width)
        elif sel < 0.3:
            v = (a - c).trunc(width)
        elif sel < 0.35:
            v = a[0:width // 2].mul(b[0:width // 2], width)
        elif sel < 0.5:
            v = (a ^ b) & c
        elif sel < 0.65:
            v = s.mux(a, b)
        elif sel < 0.75:
            v = (a < b).mux(a, c)
        elif sel < 0.8:
            v = cat(a[0:width // 2], (b >> 3)[0:width // 2])
        else:
            from tests.ir_dsl import E
            v = E(m, m.b.dff(((a | b) ^ c).s, clk.s, arst=rst.s, arst_value=0))
        pool.append(v)
        if len(pool) > 64:
            pool.pop(rnd.randrange(8, len(pool)))
        k += 1
        if k % 50 == 0:
            outs.append(v)
    for j, v in enumerate(outs):
        m.out(f"o{j}", v)
    return m.done(analyze=False)
