"""AST node classes of the VHDL front-end (documented in the package docstring)."""
from __future__ import annotations


class Node:
    """Base class: every node has ``line`` and ``col`` (1-based); the rest is in ``_fields``.

    Constructor: positional values in ``_fields`` order (missing ones are ``None``), plus the
    keywords ``line`` and ``col``.
    """

    __slots__ = ("line", "col")
    _fields: tuple = ()
    _unordered: tuple = ()

    def __init__(self, *args, line=0, col=0, **kw):
        self.line = line
        self.col = col
        f = self._fields
        n = len(args)
        for i in range(len(f)):
            setattr(self, f[i], args[i] if i < n else kw.get(f[i]))

    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, ", ".join("%s=%r" % (k, getattr(self, k, None)) for k in self._fields))

    def children(self):
        """Yield direct child nodes (in field order)."""
        for k in self._fields:
            v = getattr(self, k, None)
            if isinstance(v, Node):
                yield v
            elif isinstance(v, (list, tuple)):
                for x in v:
                    if isinstance(x, Node):
                        yield x
                    elif isinstance(x, (list, tuple)):
                        for y in x:
                            if isinstance(y, Node):
                                yield y


def walk(node):
    """Iterative pre-order traversal of ``node`` and all descendants."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(list(n.children())))


def _make_init(fields, extra=""):
    params = "".join(", %s=None" % f for f in fields)
    body = "".join("    self.%s = %s\n" % (f, f) for f in fields)
    src = "def __init__(self%s, *, line=0, col=0):\n    self.line = line\n    self.col = col\n%s%s" % (
        params, body, extra)
    ns = {}
    exec(src, ns)
    return ns["__init__"]


def _n(name, fields, base=Node, unordered=(), doc=""):
    """Create a ``__slots__`` node class."""
    ns = {"__slots__": tuple(fields), "_fields": tuple(fields), "_unordered": tuple(unordered),
          "__doc__": doc or name, "__init__": _make_init(fields)}
    return type(name, (base,), ns)


# ---------------------------------------------------------------- expressions / names
Ident = _n("Ident", ["name"])
Literal = _n("Literal", ["kind", "value", "text"])
BitStringLit = _n("BitStringLit", ["size", "base", "sign", "digits"])
PhysicalLit = _n("PhysicalLit", ["value", "unit"])
Unary = _n("Unary", ["op", "operand"])
Binary = _n("Binary", ["op", "left", "right"])
Select = _n("Select", ["prefix", "suffix"])
Apply = _n("Apply", ["prefix", "args"])
Assoc = _n("Assoc", ["formal", "actual"])
Slice = _n("Slice", ["prefix", "range"])
Attr = _n("Attr", ["prefix", "name", "arg"])
Qualified = _n("Qualified", ["type_mark", "operand"])
Aggregate = _n("Aggregate", ["items"])
ElemAssoc = _n("ElemAssoc", ["choices", "value"])
Range = _n("Range", ["left", "direction", "right"])
Others = _n("Others", [])
Open = _n("Open", [])
Unaffected = _n("Unaffected", [])
Skipped = _n("Skipped", ["kind", "text"])
SubtypeInd = _n("SubtypeInd", ["mark", "resolution", "index_constraints", "range", "box"])

# ---------------------------------------------------------------- declarations
Generic = _n("Generic", ["name", "kind", "subtype", "default"])
Port = _n("Port", ["name", "mode", "subtype", "default"])
Param = _n("Param", ["name", "cls", "mode", "subtype", "default"])
LibraryClause = _n("LibraryClause", ["names"])
UseClause = _n("UseClause", ["paths"])
ContextRef = _n("ContextRef", ["paths"])
TypeDecl = _n("TypeDecl", ["name", "definition"])
EnumType = _n("EnumType", ["literals"])
ArrayType = _n("ArrayType", ["indexes", "unconstrained", "element"])
RecordType = _n("RecordType", ["fields"])
RecordField = _n("RecordField", ["name", "subtype"])
RangeType = _n("RangeType", ["range"])
SubtypeDecl = _n("SubtypeDecl", ["name", "subtype"])
ObjectDecl = _n("ObjectDecl", ["cls", "name", "subtype", "init", "shared"])
AliasDecl = _n("AliasDecl", ["name", "subtype", "target"])
AttributeDecl = _n("AttributeDecl", ["name", "type_mark"])
AttributeSpec = _n("AttributeSpec", ["attr", "targets", "entity_class", "value"])
ComponentDecl = _n("ComponentDecl", ["name", "generics", "ports"])
Subprogram = _n("Subprogram", ["kind", "name", "pure", "params", "return_type", "decls", "body", "has_body"],
                unordered=("decls",))
PackageInst = _n("PackageInst", ["name", "target", "generic_map"])

# ---------------------------------------------------------------- statements
Alt = _n("Alt", ["values", "cond"])
SelAlt = _n("SelAlt", ["values", "choices"])
SignalAssign = _n("SignalAssign", ["label", "target", "alts", "kind", "guarded"])
SelectAssign = _n("SelectAssign", ["label", "selector", "matching", "target", "alts", "kind", "guarded"])
ProcessStmt = _n("ProcessStmt", ["label", "sensitivity", "decls", "body", "postponed", "sim_only"],
                 unordered=("decls",))
ProcCall = _n("ProcCall", ["label", "call"])
Instance = _n("Instance", ["label", "kind", "unit", "arch", "generic_map", "port_map"])
BlockStmt = _n("BlockStmt", ["label", "guard", "generics", "ports", "generic_map", "port_map", "decls", "stmts"],
               unordered=("decls", "stmts"))
ForGenerate = _n("ForGenerate", ["label", "var", "range", "decls", "stmts"], unordered=("decls", "stmts"))
GenBranch = _n("GenBranch", ["label", "cond", "decls", "stmts"], unordered=("decls", "stmts"))
IfGenerate = _n("IfGenerate", ["label", "branches", "else_branch"])
GenAlt = _n("GenAlt", ["label", "choices", "decls", "stmts"], unordered=("decls", "stmts"))
CaseGenerate = _n("CaseGenerate", ["label", "selector", "alts"])
IfBranch = _n("IfBranch", ["cond", "body"])
IfStmt = _n("IfStmt", ["label", "branches", "else_body"])
CaseAlt = _n("CaseAlt", ["choices", "body"])
CaseStmt = _n("CaseStmt", ["label", "selector", "matching", "alts"])
ForLoop = _n("ForLoop", ["label", "var", "range", "body"])
WhileLoop = _n("WhileLoop", ["label", "cond", "body"])
LoopStmt = _n("LoopStmt", ["label", "body"])
NextStmt = _n("NextStmt", ["label", "loop_label", "cond"])
ExitStmt = _n("ExitStmt", ["label", "loop_label", "cond"])
ReturnStmt = _n("ReturnStmt", ["label", "value"])
NullStmt = _n("NullStmt", ["label"])
WaitStmt = _n("WaitStmt", ["label", "on", "until", "timeout"])


# ---------------------------------------------------------------- design units
class DesignUnit(Node):
    """Base of design units.  Extra (non-hashed) slots: ``file``, ``library``, ``incomplete``, ``hash``."""

    __slots__ = ("name", "context", "file", "library", "incomplete", "_hash", "end_line")

    @property
    def hash(self):
        """Normalized AST hash, computed on first use."""
        h = self._hash
        if h is None:
            from .hashing import node_hash
            h = self._hash = node_hash(self)
        return h

    @hash.setter
    def hash(self, v):
        self._hash = v


def _unit(name, fields, unordered=()):
    allf = ("name", "context") + tuple(fields)
    init = _make_init(allf, "    self.file = file\n    self.library = library\n    self.incomplete = False\n"
                            "    self._hash = None\n    self.end_line = line\n")
    # allow file= / library= keywords
    src_params = "".join(", %s=None" % f for f in allf)
    src = ("def __init__(self%s, *, line=0, col=0, file=None, library='work'):\n    self.line = line\n"
           "    self.col = col\n%s    self.file = file\n    self.library = library\n    self.incomplete = False\n"
           "    self._hash = None\n    self.end_line = line\n") % (
        src_params, "".join("    self.%s = %s\n" % (f, f) for f in allf))
    ns = {}
    exec(src, ns)
    return type(name, (DesignUnit,), {"__slots__": tuple(fields), "_fields": allf, "_unordered": tuple(unordered),
                                       "__doc__": name, "__init__": ns["__init__"]})


Entity = _unit("Entity", ["generics", "ports", "decls", "stmts"], unordered=("decls", "stmts"))
Architecture = _unit("Architecture", ["entity", "decls", "stmts"], unordered=("decls", "stmts"))
Package = _unit("Package", ["generics", "decls"], unordered=("decls",))
PackageBody = _unit("PackageBody", ["decls"], unordered=("decls",))
Configuration = _unit("Configuration", ["entity", "architecture"])
ContextDecl = _unit("ContextDecl", ["items"])

EXPR_NODES = (Ident, Literal, BitStringLit, PhysicalLit, Unary, Binary, Select, Apply, Slice, Attr,
              Qualified, Aggregate, Range, Others, Open, Unaffected)
ALL_NODE_CLASSES = tuple(v for v in list(globals().values())
                         if isinstance(v, type) and issubclass(v, Node) and v not in (Node, DesignUnit))
