"""Helpers shared by the VHDL elaborator tests (no tests here)."""
from gatecost.elab.vhdl import elaborate_vhdl
from gatecost.elab.vhdl.build import BuildCtx, Elab
from gatecost.elab.vhdl.values import Scope
from gatecost.frontend.vhdl import parse_text
from gatecost.ir import Builder, Design, analyze_ir, dump

HDR = "library ieee;\nuse ieee.std_logic_1164.all;\nuse ieee.numeric_std.all;\n"


def unit(ports, decls="", body="", generics="", extra="", name="t", use=""):
    """Source text of entity ``name`` + architecture ``a``."""
    g = ("generic (%s);\n" % generics) if generics else ""
    return (HDR + use + extra + "entity %s is\n%s port (\n%s\n);\nend entity;\narchitecture a of %s is\n%s\nbegin\n%s\nend architecture;\n"
            % (name, g, ports, name, decls, body))


def elab(src, top="t", **kw):
    parsed = parse_text(src)
    d = Design()
    r = elaborate_vhdl(parsed, d, top=top, **kw)
    return r, d


def module(src, top="t", analyze=True, **kw):
    """Elaborate and return (module IR of the top, result, design)."""
    r, d = elab(src, top, **kw)
    assert r.top_key is not None, [x.message for x in r.diagnostics]
    for m in d.modules.values():
        if analyze:
            analyze_ir(m)
    return d.modules[r.top_key], r, d


def nodes(m, op=None):
    return [n for n in m.nodes if op is None or n.op == op]


def ops(m):
    return sorted(n.op for n in m.nodes)


def codes(r, m=None):
    out = [x.code for x in r.diagnostics]
    if m is not None:
        out += [x.code for x in m.diagnostics]
    return out


def probe(decls, expr, use="", hint=None, extra=""):
    """Evaluate a VHDL expression against signals declared in ``decls``; returns the Val."""
    src = HDR + use + extra + "entity p is end entity;\narchitecture a of p is\n%s\nbegin\nend architecture;\n" % decls
    parsed = parse_text(src)
    ctx = BuildCtx(parsed, Design(), "after", None, "binary", True, None, None)
    E = Elab(ctx, Builder("probe"))
    arch = parsed.architectures[("work", "p")][-1]
    E.file = arch.file
    lsc = E.unit_scope(arch)
    asc = Scope(lsc)
    E.declare_items(arch.decls, asc, None)
    e = parse_text(HDR + use + "entity q is end; architecture a of q is begin x <= %s; end;" % expr)
    ex = e.architectures[("work", "q")][-1].stmts[0].alts[0].values[0]
    v = E.eval(ex, asc, hint)
    if v.sig is None and v.t.kind == "enum":
        v = type(v)(v.t, E.sig_of(v), v.c, v.z)
    return v, ctx
