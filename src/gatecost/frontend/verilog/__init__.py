"""Verilog / SystemVerilog syntactic front-end (FRONTEND.md section 2.1 - 2.4).

Pipeline: ``Reader -> Preprocessor -> Lexer -> Parser -> AST``.  The elaborator is a separate
component that consumes the AST described below.

Public API
----------
``parse_files(paths, defines=None, incdirs=(), limits=None) -> ParsedDesign``
``parse_text(text, path="<string>", defines=None, incdirs=(), limits=None) -> ParsedDesign``

Neither raises: every problem becomes a ``gatecost.result.Diagnostic`` in
``ParsedDesign.diagnostics`` (codes ``PARSE_ERROR``, ``UNSUPPORTED_CONSTRUCT``, ``UNDEFINED_MACRO``,
``MACRO_ERROR``, ``PREPROC_ERROR``, ``INCLUDE_NOT_FOUND``, ``INCLUDE_CYCLE``, ``LIMIT_REACHED``,
``DUPLICATE_MODULE``, ``SOURCE_NOT_FOUND``, ``IGNORED_REGION`` (info), ``ENCODING_FALLBACK`` (info)).

``ParsedDesign``
    ``modules``       tuple of :class:`Module`, command-line/file order, *duplicates retained*
                      (``module_map()`` gives a name -> last-definition dict; a ``DUPLICATE_MODULE``
                      diagnostic is already emitted for each redefinition)
    ``interfaces``    tuple of :class:`Interface` (a Module subclass)
    ``packages``      tuple of :class:`Package`; compilation-unit scope items (typedefs, parameters,
                      functions, imports declared outside any module/package) are collected in a
                      package named ``"$unit"``
    ``udps``          names of user-defined primitives (skipped, to be treated as black boxes)
    ``diagnostics``   tuple of Diagnostic, sorted by (file, line, column)
    ``file_hashes``   ``{path: sha256}`` of every source file and every file reached by `include
    ``macro_uses``    ``{(file, line): [(macro, definition file, definition line), ...]}``: for code
                      produced by macro expansion, the use-site line is the node location and this
                      gives the macro definition as secondary location.

Locations
---------
Every node has ``loc = (file, line, col)`` (1-based, original source; the use site for macro
expansions).  ``Module.digest()`` / ``Package.digest()`` return the normalized AST hash
(whitespace/comment/declaration-order insensitive) used for the change detection of FRONTEND.md
section 5.

AST overview (all classes are in :mod:`gatecost.frontend.verilog.ast`; see their docstrings)
-------------------------------------------------------------------------------------------
Containers: ``Module(name, kind, params, ports, ansi, items, incomplete, default_nettype, file,
end_line)``, ``Interface`` (same fields), ``Package(name, items, incomplete, file, end_line)``.

Module items (``Module.items``, in source order; ``generate`` regions are flattened; one node per
declared name):
    ``Port`` (non-ANSI body port declarations; the header ports of a non-ANSI module only carry names),
    ``NetDecl``, ``VarDecl``, ``ParamDecl`` (also header params, ``is_type`` for type parameters),
    ``Typedef``, ``Import`` (``name == "*"`` for wildcard), ``Genvar``, ``Defparam``,
    ``ContAssign``, ``Always(kind, event, body)``, ``Instance`` (module/interface instance, one per
    name, with ``ParamAssign``/``PortConn``), ``GateInst``, ``Function`` (also tasks),
    ``Modport``, and the generate constructs ``GenFor``, ``GenIf``, ``GenCase`` (bodies are
    ``GenBlock(label, items)``).  ``initial``/``final``/``specify``/classes/assertions/...
    are *not* in the AST (reported as ``IGNORED_REGION`` info).

Data types: ``IntType(kind, signed, dims)`` (logic/bit/reg/byte/shortint/int/longint/integer/time),
``ImplicitType(signed, dims)``, ``OtherType(kind)``, ``NamedType(name, dims)``,
``EnumType(base, members, dims)``, ``StructType(is_union, packed, signed, members, dims)``;
dimensions are ``Dim(kind, left, right)`` with kind ``range`` / ``size`` / ``unsized`` / ``assoc`` /
``queue``.

Statements: ``Block``, ``ProcAssign(lhs, op, rhs, blocking)`` (``i++`` is ``+= 1``), ``If``, ``Case``
/ ``CaseItem`` (``full_case``/``parallel_case`` flags, ``inside``), ``For``, ``While``, ``DoWhile``,
``Repeat``, ``Forever``, ``Foreach``, ``CallStmt``, ``Return``, ``Break``, ``Continue``, ``Null``,
``EventStmt``.  Delays (``#n``, ``<= #1``) are dropped; system tasks (``$display`` ...) become
``Null`` and are dropped from blocks.

Expressions: ``Number`` (size, base, signed, digits, value, based), ``Fill`` (``'0 '1 'x 'z``),
``Real``, ``String``, ``Ident``, ``ScopedName(scope, name)``, ``Member``, ``Index``, ``Select``
(``:``, ``+:``, ``-:``), ``Unary``, ``Binary`` (the operator is a string), ``Ternary``, ``Concat``,
``Replicate``, ``Streaming``, ``Call``/``NamedArg``, ``SysCall`` (``$clog2`` ...), ``Cast`` (``target`` is a
DataType or an Expr), ``AssignPattern``/``PatternItem``, ``Inside``/``ValueRange``.  Parentheses are
not represented.

Incomplete modules: ``Module.incomplete`` is True when a parse error removed or truncated content.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from gatecost.frontend.common.diagnostics import DiagSink
from gatecost.frontend.common.reader import SourceFile, read_source, source_from_text
from gatecost.result import Diagnostic

from . import ast
from .ast import Interface, Module, Package, walk
from .lexer import lex
from .limits import Limits
from .norm import node_hash
from .parser import ParseError, Parser
from .preproc import LineMap, Preprocessor, PreprocResult


@dataclass(slots=True)
class ParsedFile:
    path: str
    sha256: str
    included: dict
    modules: tuple
    interfaces: tuple
    packages: tuple


@dataclass(slots=True)
class ParsedDesign:
    modules: tuple = ()
    interfaces: tuple = ()
    packages: tuple = ()
    udps: tuple = ()
    diagnostics: tuple = ()
    file_hashes: dict = field(default_factory=dict)
    macro_uses: dict = field(default_factory=dict)
    files: tuple = ()

    def module_map(self) -> dict:
        return {m.name: m for m in self.modules}

    def interface_map(self) -> dict:
        return {m.name: m for m in self.interfaces}

    def package_map(self) -> dict:
        return {m.name: m for m in self.packages}


def _parse_source(src: SourceFile, pp: Preprocessor, sink: DiagSink, limits: Limits, macro_uses: dict):
    old_limit = sys.getrecursionlimit()
    if old_limit < 6000:
        sys.setrecursionlimit(6000)
    try:
        if src.encoding != "utf-8":
            sink.add("ENCODING_FALLBACK", "file is not valid UTF-8; decoded as Latin-1", src.path, 1, 1)
        pre = pp.process(src)
        toks = lex(pre.text)
        nlines = pre.text.count("\n") + 1
        lf, ll = pre.linemap.expand(nlines)
        for out_line, uses in pre.macro_uses.items():
            if 1 <= out_line <= nlines:
                macro_uses.setdefault((lf[out_line - 1], ll[out_line - 1]), []).extend(uses)
        parser = Parser(pre.text, toks, lf, ll, pre.nettype, sink, limits, src.path)
        unit = parser.parse_unit()
        return pre, unit
    except Exception as exc:  # defensive: the front-end never aborts the analysis
        sink.add("PARSE_ERROR", f"internal front-end error ({type(exc).__name__}: {exc}); file skipped",
                 src.path, 1, 1)
        return None, None
    finally:
        sys.setrecursionlimit(old_limit)


def _finish(sources: list, pp: Preprocessor, sink: DiagSink, limits: Limits) -> ParsedDesign:
    modules: list = []
    interfaces: list = []
    packages: list = []
    udps: list = []
    unit_items: list = []
    unit_loc = None
    files: list = []
    hashes: dict = {}
    macro_uses: dict = {}
    for src in sources:
        hashes[src.path] = src.sha256
        pre, unit = _parse_source(src, pp, sink, limits, macro_uses)
        if unit is None:
            files.append(ParsedFile(src.path, src.sha256, {}, (), (), ()))
            continue
        hashes.update(pre.included)
        modules.extend(unit.modules)
        interfaces.extend(unit.interfaces)
        packages.extend(unit.packages)
        udps.extend(unit.udps)
        if unit.unit_items:
            if unit_loc is None:
                unit_loc = unit.unit_items[0].loc
            unit_items.extend(unit.unit_items)
        files.append(ParsedFile(src.path, src.sha256, dict(pre.included), tuple(unit.modules),
                                tuple(unit.interfaces), tuple(unit.packages)))
    if unit_items:
        packages.append(Package(unit_loc, "$unit", tuple(unit_items), False, unit_loc[0], unit_loc[1], None))
    seen: dict = {}
    for m in modules:
        if m.name in seen:
            sink.add("DUPLICATE_MODULE", f"module '{m.name}' redefined (previous definition at "
                     f"{seen[m.name].loc[0]}:{seen[m.name].loc[1]}); the last definition wins",
                     m.loc[0], m.loc[1], m.loc[2])
        seen[m.name] = m
    diags = sorted(sink.items, key=lambda d: (d.file or "", d.line or 0, d.column or 0))
    return ParsedDesign(tuple(modules), tuple(interfaces), tuple(packages), tuple(udps), tuple(diags),
                        hashes, macro_uses, tuple(files))


def parse_files(paths, defines: dict | None = None, incdirs=(), limits: Limits | None = None) -> ParsedDesign:
    """Parse source files in the given order (macros persist across files, as on a tool command line)."""
    limits = limits or Limits()
    sink = DiagSink()
    pp = Preprocessor(defines, incdirs, limits, sink)
    sources = []
    for p in paths:
        disp = os.fspath(p)
        try:
            sources.append(read_source(p, disp))
        except OSError as exc:
            sink.add("SOURCE_NOT_FOUND", f"cannot read source file: {exc.strerror or exc}", disp, None, None)
    return _finish(sources, pp, sink, limits)


def parse_text(text: str, path: str = "<string>", defines: dict | None = None, incdirs=(),
               limits: Limits | None = None) -> ParsedDesign:
    limits = limits or Limits()
    sink = DiagSink()
    pp = Preprocessor(defines, incdirs, limits, sink)
    return _finish([source_from_text(text, path)], pp, sink, limits)


__all__ = [
    "ast", "parse_files", "parse_text", "ParsedDesign", "ParsedFile", "Limits", "Module", "Interface",
    "Package", "walk", "node_hash", "lex", "Preprocessor", "LineMap", "PreprocResult", "Parser", "ParseError",
]
