"""Normalized AST hash (FRONTEND.md section 5).

``node_hash(module)`` is a SHA-256 over a canonical serialization of the AST that ignores source
locations (hence whitespace and comments) and is insensitive to the order of independent
declarations: within a module/interface/package/generate block, net/variable/port/parameter/typedef/
import/genvar/defparam/modport declarations are sorted; named parameter overrides and named port
connections of an instance are sorted.  Statements, concurrent items (assign/always/instances) and
header port/parameter lists keep their order.  Serialization is iterative (no recursion on tree
depth).
"""
from __future__ import annotations

import hashlib

from . import ast as A

_DECLS = (A.NetDecl, A.VarDecl, A.Port, A.ParamDecl, A.Typedef, A.Import, A.Genvar, A.Defparam, A.Modport)
_SORT_ITEMS = (A.Module, A.Package, A.GenBlock)   # Interface is a Module subclass


def canon(root) -> str:
    out: list[str] = []
    stack: list = [(1, root)]
    push = stack.append
    while stack:
        lit, x = stack.pop()
        if lit == 0:
            out.append(x)
            continue
        if isinstance(x, A.Node):
            cls = type(x)
            skip = cls._nohash
            seq: list = [(0, "(" + cls.__name__)]
            for f in cls._fields:
                if f in skip:
                    continue
                v = getattr(x, f, None)
                seq.append((0, " "))
                if isinstance(v, tuple):
                    if f == "items" and isinstance(x, _SORT_ITEMS):
                        decls = sorted(canon(c) for c in v if isinstance(c, _DECLS))
                        seq.append((0, "["))
                        for s in decls:
                            seq.append((0, s))
                            seq.append((0, " "))
                        for c in v:
                            if not isinstance(c, _DECLS):
                                seq.append((1, c))
                                seq.append((0, " "))
                        seq.append((0, "]"))
                    elif isinstance(x, A.Instance) and f in ("params", "conns") and v and \
                            all((c.name is not None) if isinstance(c, A.ParamAssign) else c.kind != "positional"
                                for c in v):
                        seq.append((0, "[" + " ".join(sorted(canon(c) for c in v)) + "]"))
                    else:
                        seq.append((0, "["))
                        for c in v:
                            seq.append((1, c))
                            seq.append((0, " "))
                        seq.append((0, "]"))
                else:
                    seq.append((1, v))
            seq.append((0, ")"))
            stack.extend(reversed(seq))
        elif x is None:
            out.append("~")
        elif x is True:
            out.append("T")
        elif x is False:
            out.append("F")
        elif isinstance(x, tuple):
            # nested tuple (not produced by the parser, kept for robustness)
            out.append("[")
            seq = []
            for c in x:
                seq.append((1, c))
                seq.append((0, " "))
            seq.append((0, "]"))
            stack.extend(reversed(seq))
        elif isinstance(x, str):
            out.append(repr(x))
        else:
            out.append(str(x))
    return "".join(out)


def node_hash(root) -> str:
    return hashlib.sha256(canon(root).encode("utf-8")).hexdigest()
