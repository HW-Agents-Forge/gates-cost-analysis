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


_FIELDS: dict = {}          # class -> hashed field names
_Node = A.Node


def _lit(v) -> str:
    """Text of a scalar (non-node, non-tuple) field value."""
    if v is None:
        return "~"
    if v is True:
        return "T"
    if v is False:
        return "F"
    if v.__class__ is str or isinstance(v, str):
        return repr(v)
    return str(v)


def canon(root) -> str:
    out: list[str] = []
    stack: list = [root]
    pop = stack.pop
    # Stack entries: ``str`` = literal text to emit, Node / tuple = still to be expanded.
    while stack:
        x = pop()
        if x.__class__ is str:
            out.append(x)
        elif isinstance(x, _Node):
            cls = x.__class__
            fields = _FIELDS.get(cls)
            if fields is None:
                skip = cls._nohash
                fields = _FIELDS[cls] = tuple(f for f in cls._fields if f not in skip)
            seq: list = []
            buf = "(" + cls.__name__
            for f in fields:
                v = getattr(x, f, None)
                buf += " "
                if v.__class__ is tuple:
                    if f == "items" and isinstance(x, _SORT_ITEMS):
                        decls = sorted([canon(c) for c in v if isinstance(c, _DECLS)])
                        buf += "["
                        for d in decls:
                            buf += d + " "
                        for c in v:
                            if not isinstance(c, _DECLS):
                                seq.append(buf)
                                seq.append(c)
                                buf = " "
                        buf += "]"
                    elif isinstance(x, A.Instance) and f in ("params", "conns") and v and \
                            all((c.name is not None) if isinstance(c, A.ParamAssign) else c.kind != "positional"
                                for c in v):
                        buf += "[" + " ".join(sorted([canon(c) for c in v])) + "]"
                    else:
                        buf += "["
                        for c in v:
                            if isinstance(c, _Node) or c.__class__ is tuple:
                                seq.append(buf)
                                seq.append(c)
                                buf = " "
                            else:
                                buf += _lit(c) + " "
                        buf += "]"
                elif isinstance(v, _Node):
                    seq.append(buf)
                    seq.append(v)
                    buf = ""
                else:
                    buf += _lit(v)
            seq.append(buf + ")")
            seq.reverse()
            stack.extend(seq)
        elif x.__class__ is tuple:
            # nested tuple (not produced by the parser, kept for robustness)
            seq = ["["]
            for c in x:
                if isinstance(c, _Node) or c.__class__ is tuple:
                    seq.append(c)
                    seq.append(" ")
                else:
                    seq.append(_lit(c) + " ")
            seq.append("]")
            seq.reverse()
            stack.extend(seq)
        else:
            out.append(_lit(x))
    return "".join(out)


def node_hash(root) -> str:
    return hashlib.sha256(canon(root).encode("utf-8")).hexdigest()
