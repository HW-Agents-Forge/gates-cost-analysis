"""Shared helpers for the VHDL front-end tests."""
from gatecost.frontend.vhdl import nodes as N
from gatecost.frontend.vhdl import parse_text


def parse(src):
    return parse_text(src)


def codes(result, severity=None):
    return [d.code for d in result.diagnostics if severity is None or d.severity == severity]


def errors(result):
    return [d for d in result.diagnostics if d.severity == "error"]


def arch(src, body="", decls=""):
    """Parse an architecture wrapper around ``body`` and return (result, architecture)."""
    text = "architecture a of e is\n%s\nbegin\n%s\nend architecture a;\n" % (decls, body)
    if src:
        text = src + text
    r = parse(text)
    return r, r.architectures[("work", "e")][-1]


def stmts(body, decls=""):
    r, a = arch("", body, decls)
    assert not errors(r), [(d.line, d.message) for d in errors(r)]
    return a.stmts


def proc(body, decls=""):
    """Sequential statements inside a process."""
    r, a = arch("", "process begin\n%s\nend process;" % body, decls)
    assert not errors(r), [(d.line, d.message) for d in errors(r)]
    return a.stmts[0].body


def expr(text):
    """Parse an expression as the RHS of a signal assignment."""
    s = stmts("x <= %s;" % text)
    return s[0].alts[0].values[0]


def show(e):
    """Compact prefix rendering of an expression for assertions."""
    if isinstance(e, N.Ident):
        return e.name
    if isinstance(e, N.Literal):
        return repr(e.value)
    if isinstance(e, N.BitStringLit):
        return "bits(%s,%s,%s,%s)" % (e.size, e.base, e.sign, e.digits)
    if isinstance(e, N.Binary):
        return "(%s %s %s)" % (show(e.left), e.op, show(e.right))
    if isinstance(e, N.Unary):
        return "(%s %s)" % (e.op, show(e.operand))
    if isinstance(e, N.Apply):
        return "%s[%s]" % (show(e.prefix), ", ".join((show(a.formal) + "=>" if a.formal else "") + show(a.actual)
                                                       for a in e.args))
    if isinstance(e, N.Slice):
        return "%s{%s}" % (show(e.prefix), show(e.range))
    if isinstance(e, N.Range):
        return "%s %s %s" % (show(e.left), e.direction, show(e.right))
    if isinstance(e, N.Select):
        return "%s.%s" % (show(e.prefix), e.suffix)
    if isinstance(e, N.Attr):
        return "%s'%s%s" % (show(e.prefix), e.name, "(%s)" % show(e.arg) if e.arg is not None else "")
    if isinstance(e, N.Qualified):
        return "%s'(%s)" % (show(e.type_mark), show(e.operand))
    if isinstance(e, N.Aggregate):
        return "agg(%s)" % ", ".join(
            ("|".join(show(c) for c in i.choices) + "=>" if i.choices else "") + show(i.value) for i in e.items)
    if isinstance(e, N.Others):
        return "others"
    if isinstance(e, N.Open):
        return "open"
    if isinstance(e, N.PhysicalLit):
        return "%s %s" % (show(e.value), e.unit)
    return type(e).__name__
