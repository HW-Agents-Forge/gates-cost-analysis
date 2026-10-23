"""Helpers for the elaborator tests."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from gatecost.elab import elaborate_project
from gatecost.frontend.verilog import parse_text
from gatecost.ir import dump

from tests.ir_sim import Sim  # noqa: F401


def elab(src: str | dict, top: str | None = None, **kw):
    """Elaborate Verilog/SystemVerilog source text (or ``{name: text}``) in a temporary directory."""
    files = src if isinstance(src, dict) else {"t.sv": src}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        paths = []
        for name, text in files.items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            paths.append(Path(name))
        return elaborate_project(paths, root=root, top=top, **kw)


def top_module(res):
    return res.design.modules[res.top_key]


def codes(res, affecting=False):
    return sorted({d.code for d in res.diagnostics if (d.affects_cost or not affecting)})


def ops(m):
    """Multiset of node ops of a Module IR (after analysis)."""
    out: dict = {}
    for n in m.nodes:
        out[n.op] = out.get(n.op, 0) + 1
    return out


def width_sign(decls: str, expr: str):
    """Self-determined ``(width, signed)`` of ``expr`` given signal declarations."""
    from gatecost.elab.module import ModElab
    from gatecost.elab.project import Ctx

    parsed = parse_text(f"module t; {decls} localparam X = {expr}; endmodule")
    mod = parsed.modules[0]
    ctx = Ctx(parsed, None)
    hdr = ctx.get_header(mod, {})
    me = ModElab(ctx, hdr, {}, 0)
    me.process_items(mod.items, True)
    init = [i for i in mod.items if getattr(i, "name", None) == "X"][0].init
    me._tm = {}
    return me.ty(init)


def comb(m, **inputs):
    """Evaluate a combinational module: returns ``{output name: value}``."""
    s = Sim(m)
    return s.outputs(s.comb(inputs))
