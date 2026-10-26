"""One fused pass over a module's AST for the pre-scans the elaborator needs.

Instead of walking every module item several times (instantiated names, identifier references per
item, memory-name inference), :func:`scan_module` visits each node once and records all of it.
The result is cached per module object (``Ctx.scans``) so parameterised re-elaborations share it.
"""
from __future__ import annotations

from gatecost.frontend.verilog import ast as A

_Node = A.Node
_FIELDS: dict = {}


class ModScan:
    __slots__ = ("insts", "refs", "sig_names", "loopvars", "lval_nodes", "procw", "index_nodes")

    def __init__(self) -> None:
        self.insts: set = set()          # module names of Instance nodes (type names as written)
        self.refs: dict = {}             # identifier -> {item index}
        self.sig_names: set = set()
        self.loopvars: set = set()
        self.lval_nodes: set = set()
        self.procw: set = set()
        self.index_nodes: list = []      # Index nodes whose base is an Ident


def scan_module(mod) -> ModScan:
    sc = ModScan()
    insts, refs = sc.insts, sc.refs
    sig_names, loopvars, lval_nodes, procw = sc.sig_names, sc.loopvars, sc.lval_nodes, sc.procw
    index_nodes = sc.index_nodes
    Ident, Instance, VarDecl, NetDecl, Port = A.Ident, A.Instance, A.VarDecl, A.NetDecl, A.Port
    For, GenFor, ContAssign, ProcAssign, Index = A.For, A.GenFor, A.ContAssign, A.ProcAssign, A.Index
    Concat, Select, Member, Genvar = A.Concat, A.Select, A.Member, A.Genvar
    for p in mod.ports:
        sig_names.add(p.name)
    fields_of = _FIELDS
    for idx, it in enumerate(mod.items):
        stack = [it]
        pop = stack.pop
        extend = stack.extend
        while stack:
            x = pop()
            c = x.__class__
            if c is Ident:
                s = refs.get(x.name)
                if s is None:
                    refs[x.name] = {idx}
                else:
                    s.add(idx)
                continue
            if c is Instance:
                insts.add(x.module)
            elif c is VarDecl or c is NetDecl or c is Port:
                sig_names.add(x.name)
            elif c is For or c is GenFor:
                for st in x.init or ():
                    if isinstance(st, ProcAssign) and isinstance(st.lhs, Ident):
                        loopvars.add(st.lhs.name)
                    elif isinstance(st, (VarDecl, Genvar)):
                        loopvars.add(st.name)
            elif c is ContAssign or c is ProcAssign:
                st2 = [x.lhs]
                while st2:
                    y = st2.pop()
                    if isinstance(y, Concat):
                        st2.extend(y.items)
                    else:
                        lval_nodes.add(id(y))
                        while isinstance(y, (Index, Select, Member)):
                            y = y.base
                            lval_nodes.add(id(y))
                        if c is ProcAssign and isinstance(y, Ident):
                            procw.add(y.name)
            elif c is Index and x.base.__class__ is Ident:
                index_nodes.append(x)
            fl = fields_of.get(c)
            if fl is None:
                fl = fields_of[c] = c._fields
            kids = []
            for f in fl:
                v = getattr(x, f, None)
                if v is None:
                    continue
                if v.__class__ is tuple:
                    for y in v:
                        if isinstance(y, _Node):
                            kids.append(y)
                        elif y.__class__ is tuple:
                            kids.extend(z for z in y if isinstance(z, _Node))
                elif isinstance(v, _Node):
                    kids.append(v)
            if kids:
                extend(kids)
    return sc


def mem_names_of(sc: ModScan) -> set:
    dyn, loopw = set(), set()
    sig_minus = sc.sig_names - sc.loopvars
    for n in sc.index_nodes:
        names = {x.name for x in A.walk(n.index) if type(x) is A.Ident}
        if names & sig_minus:
            dyn.add(n.base.name)
        elif names & sc.loopvars and id(n) in sc.lval_nodes:
            loopw.add(n.base.name)
    return (dyn - loopw) & sc.procw


def dyn_index_names_of(sc: ModScan) -> set:
    """Arrays indexed somewhere (read or write) by an expression naming a signal (not only loop variables)."""
    sig_minus = sc.sig_names - sc.loopvars
    out = set()
    for n in sc.index_nodes:
        if n.base.name not in out and any(type(x) is A.Ident and x.name in sig_minus for x in A.walk(n.index)):
            out.add(n.base.name)
    return out
