"""AST node classes of the Verilog/SystemVerilog front-end.

Every node is a ``__slots__`` class with a ``loc`` slot ``(file, line, col)`` (original source file
and line; code produced by macro expansion is attributed to the use site) followed by the fields
documented on the class.  Node constructors take ``(loc, *fields)`` positionally.  Optional children
are ``None``; sequences are tuples.  See the package docstring (``gatecost.frontend.verilog``) for
the overview.  Utilities: :func:`walk`, :func:`children`, and ``node.fields()``.
"""
from __future__ import annotations


class Node:
    __slots__ = ("loc",)
    _fields: tuple = ()
    _nohash: tuple = ()

    def fields(self):
        """``[(name, value), ...]`` for the node-specific fields (excluding ``loc``)."""
        return [(f, getattr(self, f, None)) for f in self._fields]

    def __repr__(self):
        parts = []
        for f in self._fields:
            v = getattr(self, f, None)
            if v is None or v == () or v is False:
                continue
            parts.append(f"{f}={v!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


def node(cls):
    """Class decorator: build ``_fields`` and a positional ``__init__(self, loc, *fields)``."""
    fields: tuple = ()
    for base in reversed(cls.__mro__[1:]):
        fields += tuple(getattr(base, "__own_slots__", ()))
    own = tuple(cls.__dict__.get("__slots__", ()))
    cls.__own_slots__ = own
    fields += own
    cls._fields = fields
    src = "def __init__(self, loc" + "".join(f", {f}" for f in fields) + "):\n    self.loc = loc\n"
    src += "".join(f"    self.{f} = {f}\n" for f in fields)
    ns: dict = {}
    exec(src, ns)
    cls.__init__ = ns["__init__"]
    return cls


def _is_node_seq(v):
    return isinstance(v, tuple)


def children(n: Node):
    """Direct child nodes (flattening tuples), in field order."""
    out = []
    for f in n._fields:
        v = getattr(n, f, None)
        if v is None:
            continue
        if v.__class__ is tuple:
            for x in v:
                if isinstance(x, Node):
                    out.append(x)
                elif x.__class__ is tuple:
                    out.extend(y for y in x if isinstance(y, Node))
        elif isinstance(v, Node):
            out.append(v)
    return out


def walk(n: Node):
    """Pre-order iteration over a subtree (iterative; safe for very deep trees)."""
    stack = [n]
    pop = stack.pop
    extend = stack.extend
    while stack:
        x = pop()
        yield x
        kids = []
        for f in x._fields:
            v = getattr(x, f, None)
            if v is None:
                continue
            if v.__class__ is tuple:
                for y in v:
                    if isinstance(y, Node):
                        kids.append(y)
                    elif y.__class__ is tuple:
                        kids.extend(z for z in y if isinstance(z, Node))
            elif isinstance(v, Node):
                kids.append(v)
        if kids:
            kids.reverse()
            extend(kids)


# ---------------------------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------------------------

class Expr(Node):
    __slots__ = ()


@node
class Number(Expr):
    """Integer literal.  ``size`` int|None; ``base`` in ``b o d h`` (``d`` for plain decimals);
    ``signed``; ``digits`` lower-case, no underscores; ``value`` int or None when it has x/z/?;
    ``based`` False for a plain decimal like ``42``."""
    __slots__ = ("size", "base", "signed", "digits", "value", "based")


@node
class Fill(Expr):
    """Unsized fill literal ``'0 '1 'x 'z``: ``bit`` is one of ``"0" "1" "x" "z"``."""
    __slots__ = ("bit",)


@node
class Real(Expr):
    """Real (or time) literal; rejected in synthesizable contexts by the elaborator."""
    __slots__ = ("value",)


@node
class String(Expr):
    """String literal; ``value`` includes no quotes (escapes kept verbatim)."""
    __slots__ = ("value",)


@node
class Ident(Expr):
    """Simple identifier."""
    __slots__ = ("name",)


@node
class ScopedName(Expr):
    """Package-scoped name ``scope::name`` (``scope`` may itself contain ``::``)."""
    __slots__ = ("scope", "name")


@node
class Member(Expr):
    """``base.name``: struct field, hierarchical reference, interface member, or method."""
    __slots__ = ("base", "name")


@node
class Index(Expr):
    """``base[index]`` (bit select, array element, memory word)."""
    __slots__ = ("base", "index")


@node
class Select(Expr):
    """Part select ``base[left : right]`` (mode ``":"``), ``base[left +: right]`` (``"+:"``) or
    ``base[left -: right]`` (``"-:"``)."""
    __slots__ = ("base", "mode", "left", "right")


@node
class Unary(Expr):
    """Prefix operator ``op`` in ``+ - ! ~ & | ^ ~& ~| ~^ ^~`` (the last four are reductions
    together with ``& | ^``)."""
    __slots__ = ("op", "operand")


@node
class Binary(Expr):
    """Binary operator ``op`` (``+ - * / % ** << >> <<< >>> < <= > >= == != === !== ==? !=? & | ^ ~^ ^~ && ||``)."""
    __slots__ = ("op", "left", "right")


@node
class Ternary(Expr):
    """``cond ? then : else_``."""
    __slots__ = ("cond", "then", "else_")


@node
class Concat(Expr):
    """``{a, b, c}`` (``items`` tuple of Expr)."""
    __slots__ = ("items",)


@node
class Replicate(Expr):
    """``{count{a, b}}``."""
    __slots__ = ("count", "items")


@node
class Streaming(Expr):
    """Streaming concatenation ``{<< size {items}}`` / ``{>> ...}``: ``op`` is ``"<<"`` or
    ``">>"``; ``size`` is an Expr, a DataType, or None; items may be :class:`StreamWith`."""
    __slots__ = ("op", "size", "items")


@node
class StreamWith(Expr):
    """``expr with [select]`` inside a streaming concatenation (``select`` is Index/Select)."""
    __slots__ = ("expr", "select")


@node
class NamedArg(Expr):
    """``.name(value)`` argument of a call (``value`` may be None)."""
    __slots__ = ("name", "value")


@node
class Call(Expr):
    """Function call ``func(args)``; ``func`` is Ident, ScopedName or Member; ``args`` are Expr or
    NamedArg (empty positional arguments are ``None``)."""
    __slots__ = ("func", "args")


@node
class SysCall(Expr):
    """System function call, e.g. ``$clog2(N)``; ``name`` includes the ``$``; arguments may be
    DataType nodes (``$bits(logic [3:0])``)."""
    __slots__ = ("name", "args")


@node
class Cast(Expr):
    """``target'(expr)``.  ``target`` is a DataType for builtin types (``IntType`` for ``int``,
    ``logic`` ...; ``OtherType`` with kind ``"signed"``/``"unsigned"`` for sign casts) or an Expr
    (Ident/ScopedName naming a typedef, or a Number/expression giving a size, e.g. ``(W+1)'(x)``)."""
    __slots__ = ("target", "expr")


@node
class PatternItem(Node):
    """``key: value`` entry in an assignment pattern; ``key`` is an Expr, a DataType, or the
    string ``"default"``."""
    __slots__ = ("key", "value")


@node
class AssignPattern(Expr):
    """``'{...}`` assignment pattern; ``prefix`` optional type (``T'{...}``); ``items`` is a tuple
    of Expr (positional, possibly Replicate) or PatternItem (keyed)."""
    __slots__ = ("prefix", "items")


@node
class ValueRange(Node):
    """``[lo : hi]`` inside ``inside {...}`` or a case-inside label."""
    __slots__ = ("lo", "hi")


@node
class Inside(Expr):
    """``expr inside {items}``; items are Expr or ValueRange."""
    __slots__ = ("expr", "items")


# ---------------------------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------------------------

class DataType(Node):
    __slots__ = ()


@node
class Dim(Node):
    """A dimension.  ``kind``: ``"range"`` ``[left:right]``, ``"size"`` ``[left]`` (unpacked
    size), ``"unsized"`` ``[]``, ``"assoc"`` ``[type]``/``[*]``, ``"queue"`` ``[$]``."""
    __slots__ = ("kind", "left", "right")


@node
class IntType(DataType):
    """Integer vector/atom type.  ``kind`` in ``logic bit reg byte shortint int longint integer
    time``; ``signed`` True/False/None (None = default for the kind); ``dims`` packed dimensions."""
    __slots__ = ("kind", "signed", "dims")


@node
class ImplicitType(DataType):
    """No type keyword: ``[7:0]`` or ``signed [7:0]`` (net/port with implicit type)."""
    __slots__ = ("signed", "dims")


@node
class OtherType(DataType):
    """``real shortreal realtime string event chandle void`` (and ``signed``/``unsigned`` when used
    as a cast target: kind ``"signed"``/``"unsigned"``)."""
    __slots__ = ("kind",)


@node
class NamedType(DataType):
    """Reference to a typedef/interface-defined type: ``name`` is Ident or ScopedName."""
    __slots__ = ("name", "dims")


@node
class EnumMember(Node):
    """Enum constant ``name [= init]``; ``count`` for ``name[N]`` ranges (rare)."""
    __slots__ = ("name", "init", "count")


@node
class EnumType(DataType):
    """``enum [base] {members}``; ``base`` DataType or None."""
    __slots__ = ("base", "members", "dims")


@node
class StructMember(Node):
    """One struct/union member (one node per declared name)."""
    __slots__ = ("dtype", "name", "dims", "init")


@node
class StructType(DataType):
    """``struct``/``union`` (``is_union``); ``packed``; ``signed``; ``members`` StructMember tuple."""
    __slots__ = ("is_union", "packed", "signed", "members", "dims")


# ---------------------------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------------------------

class Item(Node):
    __slots__ = ()


@node
class Port(Item):
    """A port.  In the header of an ANSI module/function ``direction`` is always set (inherited
    from the previous port when omitted).  In a non-ANSI header the ports carry only ``name``
    (direction None); the matching ``Port`` nodes with directions and types appear in the module
    ``items``.  ``direction`` in ``input output inout ref``; ``net_type`` e.g. ``wire`` or None;
    ``dtype`` DataType or None; ``dims`` unpacked dims; ``default`` Expr or None; ``iface`` /
    ``modport`` for interface ports (``iface == "interface"`` for the generic form)."""
    __slots__ = ("direction", "net_type", "dtype", "name", "dims", "default", "iface", "modport")


@node
class NetDecl(Item):
    """Net declaration (one per name): ``net_type`` ``wire tri wand ...``; ``init`` is the
    ``wire x = expr`` continuous assignment."""
    __slots__ = ("net_type", "dtype", "name", "dims", "init")


@node
class VarDecl(Item):
    """Variable declaration (one per name); ``const``/``static`` flags."""
    __slots__ = ("dtype", "name", "dims", "init", "const", "static")


@node
class ParamDecl(Item):
    """``parameter``/``localparam`` (one per name).  ``dtype`` None when untyped; for type
    parameters ``is_type`` is True and ``init`` is a DataType (or None)."""
    __slots__ = ("local", "dtype", "name", "init", "is_type")


@node
class Typedef(Item):
    __slots__ = ("name", "dtype", "dims")


@node
class Import(Item):
    """``import pkg::name`` / ``import pkg::*`` (``name == "*"``); one node per imported item."""
    __slots__ = ("pkg", "name")


@node
class Genvar(Item):
    __slots__ = ("name",)


@node
class Defparam(Item):
    """``defparam path = value``; ``path`` is a Member/Index/Ident chain."""
    __slots__ = ("path", "value")


@node
class ContAssign(Item):
    """Continuous assignment ``assign lhs = rhs`` (one per pair)."""
    __slots__ = ("lhs", "rhs")


@node
class EventItem(Node):
    """``posedge x`` / ``negedge x`` / ``edge x`` / plain ``x``; ``edge`` is that keyword or None."""
    __slots__ = ("edge", "expr")


@node
class EventControl(Node):
    """``@(...)``: ``star`` True for ``@*`` / ``@(*)``; else ``items`` tuple of EventItem."""
    __slots__ = ("star", "items")


@node
class Always(Item):
    """``always``/``always_comb``/``always_ff``/``always_latch`` (``kind``); ``event`` an
    EventControl or None (always_comb/latch, or no timing control); ``body`` a statement."""
    __slots__ = ("kind", "event", "body")


@node
class ParamAssign(Node):
    """Parameter override: ``name`` None when positional; ``value`` Expr, DataType, or None."""
    __slots__ = ("name", "value")


@node
class PortConn(Node):
    """Instance port connection.  ``kind``: ``"named"`` (.a(x), ``expr`` may be None for .a()),
    ``"implicit"`` (.a), ``"wildcard"`` (.*), ``"positional"`` (``name`` None, ``expr`` may be None)."""
    __slots__ = ("kind", "name", "expr")


@node
class Instance(Item):
    """Module/interface/UDP instance (one per instance name).  ``module`` the type name,
    ``params`` tuple of ParamAssign, ``dims`` array-of-instances dimensions."""
    __slots__ = ("module", "params", "name", "dims", "conns")


@node
class GateInst(Item):
    """Gate/switch primitive instance (``and nand or nor xor xnor buf not bufif0 ... pullup``)."""
    __slots__ = ("gate", "name", "dims", "terminals")


@node
class ModportPort(Node):
    __slots__ = ("direction", "name")


@node
class Modport(Item):
    """``modport name (input a, output b)``; one node per modport name."""
    __slots__ = ("name", "ports")


@node
class Function(Item):
    """Function or task (``is_task``).  ``rettype`` DataType or None; ``ports`` Port tuple (ANSI or
    1995-style declarations); ``body`` tuple of declarations (VarDecl/ParamDecl) and statements in
    source order.  ``nonsynth`` True when the body contains delays/events/waits/system tasks:
    then ``body`` is empty and the construct is reported as an ignored region."""
    __slots__ = ("name", "is_task", "rettype", "automatic", "ports", "body", "nonsynth")


@node
class GenBlock(Item):
    """Generate block ``begin [: label] items end`` (or the implicit block of a single item)."""
    __slots__ = ("label", "items")


@node
class GenFor(Item):
    """``for (init; cond; step) body``: ``init``/``step`` tuples of statements (ProcAssign,
    Genvar, VarDecl); ``body`` is a GenBlock."""
    __slots__ = ("init", "cond", "step", "body")


@node
class GenIf(Item):
    """``if (cond) then else else_``; ``then`` is a GenBlock; ``else_`` is a GenBlock, GenIf or None."""
    __slots__ = ("cond", "then", "else_")


@node
class GenCaseItem(Node):
    """``labels`` tuple of Expr, or None for ``default``; ``body`` GenBlock."""
    __slots__ = ("labels", "body")


@node
class GenCase(Item):
    __slots__ = ("expr", "items")


# ---------------------------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------------------------

class Stmt(Node):
    __slots__ = ()


@node
class Block(Stmt):
    """``begin [: label] ... end``; ``items`` mixes declarations and statements in source order."""
    __slots__ = ("label", "items")


@node
class ProcAssign(Stmt):
    """Procedural assignment.  ``op`` is ``"="`` or a compound operator (``"+="`` ...; ``i++`` is
    normalised to ``+= 1``); ``blocking`` False for ``<=``."""
    __slots__ = ("lhs", "op", "rhs", "blocking")


@node
class If(Stmt):
    """``qualifier`` in ``unique unique0 priority`` or None."""
    __slots__ = ("qualifier", "cond", "then", "else_")


@node
class CaseItem(Node):
    """``labels`` tuple of Expr/ValueRange, or None for ``default``."""
    __slots__ = ("labels", "body")


@node
class Case(Stmt):
    """``kind`` in ``case casez casex``; ``qualifier`` as for If; ``inside`` for ``case ... inside``;
    ``full_case``/``parallel_case`` from attributes or ``// synopsys`` pragmas."""
    __slots__ = ("kind", "qualifier", "inside", "expr", "items", "full_case", "parallel_case")


@node
class For(Stmt):
    __slots__ = ("init", "cond", "step", "body")


@node
class While(Stmt):
    __slots__ = ("cond", "body")


@node
class DoWhile(Stmt):
    __slots__ = ("body", "cond")


@node
class Repeat(Stmt):
    __slots__ = ("count", "body")


@node
class Forever(Stmt):
    __slots__ = ("body",)


@node
class Foreach(Stmt):
    __slots__ = ("array", "vars", "body")


@node
class CallStmt(Stmt):
    """Task/void-function call statement; ``call`` is a Call."""
    __slots__ = ("call",)


@node
class Return(Stmt):
    __slots__ = ("value",)


@node
class Break(Stmt):
    __slots__ = ()


@node
class Continue(Stmt):
    __slots__ = ()


@node
class Null(Stmt):
    """Empty statement ``;``."""
    __slots__ = ()


@node
class EventStmt(Stmt):
    """``@(...) stmt`` inside a procedural body (wait-style code); ``body`` may be None."""
    __slots__ = ("event", "body")


# ---------------------------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------------------------

@node
class Module(Node):
    """``module``/``macromodule``.  ``params`` header ``#(...)`` ParamDecl tuple; ``ports`` header
    Port tuple; ``ansi`` False for a non-ANSI header; ``items`` body items in source order
    (generate regions are flattened; skipped regions are absent); ``incomplete`` True when a parse
    error removed or truncated content; ``default_nettype`` ``"wire"`` or ``"none"`` ...;
    ``file`` source file; ``end_line`` line of ``endmodule``.  ``digest()`` is the normalized AST
    hash (see ``gatecost.frontend.verilog.norm``)."""
    __slots__ = ("name", "kind", "params", "ports", "ansi", "items", "incomplete", "default_nettype",
                 "file", "end_line", "_digest")

    def digest(self) -> str:
        d = self._digest
        if d is None:
            from .norm import node_hash
            d = self._digest = node_hash(self)
        return d


@node
class Interface(Module):
    """``interface``: same fields as Module; ``items`` may contain Modport nodes."""
    __slots__ = ()


@node
class Package(Node):
    """``package`` (name ``"$unit"`` for compilation-unit-scope items collected from all files)."""
    __slots__ = ("name", "items", "incomplete", "file", "end_line", "_digest")

    def digest(self) -> str:
        d = self._digest
        if d is None:
            from .norm import node_hash
            d = self._digest = node_hash(self)
        return d


for _c in (Module, Interface, Package):
    _c._nohash = ("file", "end_line", "_digest")
for _c in (Number,):
    pass
