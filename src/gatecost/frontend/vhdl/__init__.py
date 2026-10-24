"""VHDL front-end, syntactic half (FRONTEND.md section 10): reader, lexer, parser, AST, IEEE model, hashes.

Public API
----------
``parse_files(paths, library_map=None, *, architectures=None, side="after", max_depth=500) -> ParsedVhdl``
    Parse files in order (compile order).  ``library_map`` maps a file or directory path to a
    library name (default ``work``).  ``architectures`` maps entity name to a chosen architecture name.
``parse_text(text, path="<string>", library="work", ...) -> ParsedVhdl``
    Same for a string.  Never raises: problems become diagnostics.

``ParsedVhdl`` fields (keys are ``(library, lower-case name)``):

``entities``           ``{(lib, name): Entity}`` (last definition wins, DUPLICATE_MODULE)
``architectures``      ``{(lib, entity): [Architecture, ...]}`` in compile order
``selected``           ``{(lib, entity): Architecture}`` chosen architecture (project file, then a
                       ``configuration``, else the last compiled; AMBIGUOUS_ARCHITECTURE otherwise)
``packages``/``package_bodies``/``configurations``/``contexts``  similarly
``units``              every unit in compile order;  ``diagnostics``  ``gatecost.result.Diagnostic`` list
``hashes``             ``{("entity", lib, name): hash, ("architecture", lib, entity, arch): hash, ...}``
``file_hashes``        ``{path: content hash}`` (sha256 of the raw bytes, for the parse cache)
Helpers: ``find_entity(name, lib=None)``, ``find_package(name, lib=None)``, ``architecture_of(entity, lib)``.

General AST conventions
-----------------------
Every node is a ``__slots__`` class (module ``nodes``) with ``line``/``col`` (1-based) plus the fields
below; ``Node.children()`` / ``nodes.walk(node)`` traverse.  Names are lower-cased strings; extended
identifiers keep their backslashes and case.  Design units additionally have ``file``, ``library``,
``incomplete`` (a parse error occurred inside), ``hash`` (normalized AST hash: independent declarations and
concurrent statements are order-insensitive; comments, whitespace and skipped regions do not matter),
``end_line`` and ``context``: the ``LibraryClause``/``UseClause``/``ContextRef`` list written *before the
unit itself* (an architecture also sees its entity's context, a package body its package's).  Missing
optional fields are ``None``.  Deeply left-nested operator chains are possible (``a+b+c+...`` is a
left-leaning ``Binary`` tree): traverse iteratively (``nodes.walk``).

Design units
~~~~~~~~~~~~
``Entity(name, context, generics: [Generic], ports: [Port], decls, stmts)``  (stmts: passive statements)
``Architecture(name, context, entity: str, decls, stmts)``
``Package(name, context, generics: [Generic], decls)``    ``PackageBody(name, context, decls)``
``Configuration(name, context, entity, architecture: str|None)``  body skipped; first ``for <arch>`` is kept
``ContextDecl(name, context, items)``
``LibraryClause(names)``, ``UseClause(paths: [tuple[str,...]])`` e.g. ``("ieee","numeric_std","all")``,
``ContextRef(paths)``.

Interface elements (one node per declared name)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``Generic(name, kind: "constant"|"type"|"subprogram", subtype, default)``
``Port(name, mode: "in"|"out"|"inout"|"buffer"|"linkage", subtype, default)``
``Param(name, cls: "constant"|"signal"|"variable"|"file", mode, subtype, default)`` (subprogram parameter)

Subtypes and types
~~~~~~~~~~~~~~~~~~
``SubtypeInd(mark, resolution, index_constraints: [discrete range]|None, range: Range|expr|None, box)``
    ``mark``: ``Ident``/``Select``/``Attr`` naming the type; ``box`` is true for ``natural range <>``.
    Element constraints after a second ``( )`` group are parsed and dropped.
``TypeDecl(name, definition)`` with definition one of:
    ``EnumType(literals: [str])`` (character literals keep their quotes, ``"'0'"``),
    ``ArrayType(indexes, unconstrained: bool, element: SubtypeInd)`` (indexes: ``Range``/``SubtypeInd``/
    expression naming a type or ``x'range``), ``RecordType(fields: [RecordField(name, subtype)])``,
    ``RangeType(range)`` (integer/real), ``PhysicalType(range, units: [UnitDecl(name, value, ref)])``, ``Skipped(kind)`` for access/file/protected types,
    ``None`` for an incomplete type.
``SubtypeDecl(name, subtype)``, ``AliasDecl(name, subtype, target)``, ``AttributeDecl(name, type_mark)``,
``AttributeSpec(attr, targets: [str], entity_class, value)``, ``ComponentDecl(name, generics, ports)``,
``ObjectDecl(cls: "constant"|"signal"|"variable", name, subtype, init, shared)``,
``Subprogram(kind: "function"|"procedure", name ("\"+\"" for operators), pure, params: [Param],
    return_type, decls, body: [stmt]|None, has_body)``, ``PackageInst(name, target: tuple, generic_map)``.
``Skipped(kind, text)`` stands for any skipped item (files, disconnections, configuration specs, ...).

Expressions
~~~~~~~~~~~
``Ident(name)``; ``Select(prefix, suffix: str)`` (``a.b``; suffix ``"all"`` for ``.all``);
``Apply(prefix, args: [Assoc(formal|None, actual)])`` is every ``name(...)`` that is not an explicit
slice: indexing, function call, type conversion (the elaborator decides);
``Slice(prefix, range: Range)`` is ``name(l downto r)``/``name(l to r)``;
``Attr(prefix, name, arg|None)`` (``x'length``, ``x'range(2)``, ``t'pos(c)``); ``Qualified(type_mark, operand)``;
``Aggregate(items: [ElemAssoc(choices|None, value)])`` (choices: expressions, ``Range``, ``Others``);
a parenthesized single expression is returned unwrapped;
``Unary(op, operand)`` (ops ``+ - not abs ?? and or nand nor xor xnor``: the last six are 2008
reductions); ``Binary(op, left, right)`` (ops: logical, relational incl. ``?=`` family, shifts, ``+ - &``,
``* / mod rem``, ``**``); ``Range(left, direction: "to"|"downto", right)``; ``Literal(kind: "int"|"real"|
"based"|"char"|"str"|"null", value, text)``; ``BitStringLit(size|None, base: "b"|"o"|"x"|"d", sign:
"u"|"s"|None, digits: upper-case without underscores)``; ``PhysicalLit(value: Literal, unit)``;
``Others()``, ``Open()``, ``Unaffected()``; ``Skipped("external_name"|"access")``.

Statements (concurrent and sequential share assignment nodes)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``SignalAssign(label, target, alts: [Alt(values: [expr], cond|None)], kind: "signal"|"variable", guarded)``
    simple = one alternative with ``cond None``; conditional = several, the last without ``cond``.
    ``after`` clauses are dropped; ``values`` holds the waveform elements.
``SelectAssign(label, selector, matching: bool, target, alts: [SelAlt(values, choices)], kind, guarded)``
``ProcessStmt(label, sensitivity: None|"all"|[name expr], decls, body, postponed, sim_only)``
``ProcCall(label, call)`` (call is ``Ident``/``Apply``/``Select``)
``Instance(label, kind: "entity"|"component"|"configuration", unit: tuple[str,...], arch|None,
    generic_map: [Assoc], port_map: [Assoc])``
``BlockStmt(label, guard, generics, ports, generic_map, port_map, decls, stmts)``
``ForGenerate(label, var, range, decls, stmts)``; ``IfGenerate(label, branches: [GenBranch(label, cond,
decls, stmts)], else_branch: GenBranch|None)``; ``CaseGenerate(label, selector, alts: [GenAlt(label,
choices, decls, stmts)])``
``IfStmt(label, branches: [IfBranch(cond, body)], else_body|None)``;
``CaseStmt(label, selector, matching: bool, alts: [CaseAlt(choices, body)])``;
``ForLoop(label, var, range, body)``, ``WhileLoop(label, cond, body)``, ``LoopStmt(label, body)``,
``NextStmt``/``ExitStmt(label, loop_label, cond)``, ``ReturnStmt(label, value)``, ``NullStmt(label)``,
``WaitStmt(label, on: [expr]|None, until, timeout)``; ``Skipped("assert"|"report"|"psl"|...)``.

Diagnostics (stable codes)
~~~~~~~~~~~~~~~~~~~~~~~~~~
``PARSE_ERROR`` (error), ``UNSUPPORTED_CONSTRUCT`` (warning, affects cost), ``LIMIT_REACHED``,
``MISSING_UNIT`` (use clause naming a package found neither in sources nor in the IEEE model),
``AMBIGUOUS_ARCHITECTURE``, ``DUPLICATE_MODULE``, and at ``info`` level ``IGNORED_REGION`` (pragma
regions, assert/report, ``wait for``, ``after``, file/access/protected declarations, textio use, PSL,
configuration bodies) and ``SIMULATION_ONLY_PROCESS``.

IEEE model: see ``ieee`` (signatures as data, ``resolve``, ``result_width``, ``WIDTH_RULE_TABLE``).
"""
from __future__ import annotations

from . import ieee, nodes
from .hashing import node_hash
from .lexer import lex
from .nodes import walk
from .project import ParsedVhdl, parse_files, parse_source, parse_text, resolve_library
from .reader import SourceFile, read_source

__all__ = ["ParsedVhdl", "parse_files", "parse_text", "parse_source", "resolve_library", "node_hash", "lex",
           "read_source", "SourceFile", "ieee", "nodes", "walk"]
