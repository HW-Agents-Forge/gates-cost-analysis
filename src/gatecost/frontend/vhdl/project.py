"""Parse a set of VHDL files into design units, with library handling and diagnostics."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from ...result import Diagnostic
from .hashing import node_hash
from .lexer import lex
from .nodes import (Architecture, ContextDecl, Configuration, Entity, Package, PackageBody, UseClause)
from .parser import ParseError, Parser
from .reader import SourceFile, read_source, source_from_text

STD_LIBS = frozenset(("std", "ieee", "work"))


@dataclass
class ParsedVhdl:
    """Result of :func:`parse_files`.  Keys are ``(library, lower-case name)``."""

    entities: dict = field(default_factory=dict)             # (lib, name) -> Entity
    architectures: dict = field(default_factory=dict)        # (lib, entity) -> [Architecture] in compile order
    packages: dict = field(default_factory=dict)             # (lib, name) -> Package
    package_bodies: dict = field(default_factory=dict)       # (lib, name) -> PackageBody
    configurations: dict = field(default_factory=dict)       # (lib, name) -> Configuration
    contexts: dict = field(default_factory=dict)             # (lib, name) -> ContextDecl
    units: list = field(default_factory=list)                # every unit, compile order
    diagnostics: list = field(default_factory=list)          # gatecost.result.Diagnostic
    hashes: dict = field(default_factory=dict)               # unit key -> normalized AST hash
    file_hashes: dict = field(default_factory=dict)          # path -> content hash
    selected: dict = field(default_factory=dict)             # (lib, entity) -> chosen Architecture

    # ------------------------------------------------------------ lookup helpers
    def find_entity(self, name, lib=None):
        """Entity by name; ``lib`` None searches every library (``work`` first)."""
        name = name.lower()
        if lib is not None:
            return self.entities.get((lib.lower(), name))
        for key in (("work", name),):
            if key in self.entities:
                return self.entities[key]
        for (l, n), e in self.entities.items():
            if n == name:
                return e
        return None

    def find_package(self, name, lib=None):
        name = name.lower()
        if lib is not None:
            return self.packages.get((lib.lower(), name))
        if ("work", name) in self.packages:
            return self.packages[("work", name)]
        for (l, n), p in self.packages.items():
            if n == name:
                return p
        return None

    def architecture_of(self, entity, lib="work"):
        """Selected architecture of ``entity`` (see :func:`select_architectures`) or None."""
        return self.selected.get((lib.lower(), entity.lower()))


def _norm(p):
    return os.path.normcase(os.path.abspath(str(p)))


def resolve_library(path, library_map):
    """Library of ``path``: exact path entry, else longest directory-prefix entry, else ``work``."""
    if not library_map:
        return "work"
    ap = _norm(path)
    best = None
    blen = -1
    for k, v in library_map.items():
        nk = _norm(k)
        if ap == nk:
            return str(v).lower()
        pref = nk if nk.endswith(os.sep) else nk + os.sep
        if ap.startswith(pref) and len(pref) > blen:
            best, blen = str(v).lower(), len(pref)
    return best or "work"


def parse_source(src: SourceFile, library="work", side="after", max_depth=500):
    """Parse one source. Returns ``(units, diagnostics)``; never raises."""
    diags: list[Diagnostic] = []
    if src.error:
        diags.append(Diagnostic("PARSE_ERROR", "cannot read file: %s" % src.error, "error", True, side, src.path, None, None))
        return [], diags
    tokens, ignored, lex_errors = lex(src.text)
    for (a, b) in ignored:
        diags.append(Diagnostic("IGNORED_REGION", "synthesis pragma region (translate_off) lines %d-%d excluded" % (a, b),
                                "info", False, side, src.path, a, None))
    p = Parser(tokens, src.path, lex_errors, max_depth=max_depth * 3, side=side)
    old = sys.getrecursionlimit()
    if old < 30000:
        sys.setrecursionlimit(30000)
    try:
        try:
            units = p.parse_design_file()
        except RecursionError:
            p._add("LIMIT_REACHED", "nesting too deep; rest of file skipped", "warning", True,
                   p.tok.line, p.tok.col)
            units = p.units
            if p.cur_unit is not None:
                p.cur_unit.incomplete = True
                if p.cur_unit not in units:
                    units.append(p.cur_unit)
        except ParseError as e:  # defensive: should be handled inside
            p.record(e)
            units = p.units
    finally:
        if old < 30000:
            sys.setrecursionlimit(old)
    for u in units:
        u.file = src.path
        u.library = library
        u.hash = node_hash(u)
    diags.extend(p.diags)
    return units, diags


def _unit_key(u):
    lib = u.library
    if isinstance(u, Architecture):
        return ("architecture", lib, u.entity, u.name)
    kind = {Entity: "entity", Package: "package", PackageBody: "package_body", Configuration: "configuration",
            ContextDecl: "context"}[type(u)]
    return (kind, lib, u.name)


def parse_files(paths, library_map=None, *, architectures=None, side="after", max_depth=500) -> ParsedVhdl:
    """Parse VHDL files in order (compile order = file order, then position in file).

    ``library_map`` maps a file or directory path to a library name (default ``work``).
    ``architectures`` optionally maps entity name -> architecture name (project-file selection).
    """
    out = ParsedVhdl()
    for path in paths:
        src = read_source(path)
        lib = resolve_library(path, library_map)
        units, diags = parse_source(src, lib, side, max_depth)
        out.diagnostics.extend(diags)
        out.file_hashes[str(path)] = src.content_hash
        _register(out, units, side)
    _finish(out, architectures, side)
    return out


def parse_text(text, path="<string>", library="work", *, architectures=None, side="after", max_depth=500) -> ParsedVhdl:
    """Parse VHDL source given as a string (used for ``estimate --rtl``)."""
    out = ParsedVhdl()
    src = source_from_text(text, path)
    units, diags = parse_source(src, library, side, max_depth)
    out.diagnostics.extend(diags)
    out.file_hashes[path] = src.content_hash
    _register(out, units, side)
    _finish(out, architectures, side)
    return out


def _dup(out, u, what, side):
    out.diagnostics.append(Diagnostic("DUPLICATE_MODULE", "%s '%s' defined more than once; the last definition wins"
                                      % (what, u.name), "warning", True, side, u.file, u.line, u.col))


def _register(out: ParsedVhdl, units, side):
    for u in units:
        out.units.append(u)
        lib = u.library
        key = (lib, u.name.lower())
        out.hashes[_unit_key(u)] = u.hash
        if isinstance(u, Entity):
            if key in out.entities:
                _dup(out, u, "entity", side)
            out.entities[key] = u
        elif isinstance(u, Architecture):
            lst = out.architectures.setdefault((lib, u.entity.lower()), [])
            for i, a in enumerate(lst):
                if a.name == u.name:
                    _dup(out, u, "architecture", side)
                    del lst[i]
                    break
            lst.append(u)
        elif isinstance(u, Package):
            if key in out.packages:
                _dup(out, u, "package", side)
            out.packages[key] = u
        elif isinstance(u, PackageBody):
            out.package_bodies[key] = u
        elif isinstance(u, Configuration):
            out.configurations[key] = u
        elif isinstance(u, ContextDecl):
            out.contexts[key] = u


def _finish(out: ParsedVhdl, architectures, side):
    from . import ieee

    explicit = {k.lower(): v.lower() for k, v in (architectures or {}).items()}
    cfg_arch = {}
    for (lib, _), c in out.configurations.items():
        if c.architecture:
            cfg_arch[(lib, c.entity.lower())] = (c.architecture.lower(), c)
    for (lib, ent), lst in out.architectures.items():
        chosen = None
        how = None
        want = explicit.get(ent)
        if want:
            chosen = next((a for a in lst if a.name == want), None)
            how = "project file"
        if chosen is None and (lib, ent) in cfg_arch:
            want, c = cfg_arch[(lib, ent)]
            chosen = next((a for a in lst if a.name == want), None)
            how = "configuration '%s'" % c.name
        if chosen is None:
            chosen = lst[-1]
            if len(lst) > 1:
                out.diagnostics.append(Diagnostic(
                    "AMBIGUOUS_ARCHITECTURE",
                    "entity '%s' has %d architectures (%s) and none is selected; the last compiled ('%s') is used"
                    % (ent, len(lst), ", ".join(a.name for a in lst), chosen.name),
                    "warning", True, side, chosen.file, chosen.line, chosen.col))
        elif len(lst) > 1:
            for a in lst:
                if a is not chosen:
                    out.diagnostics.append(Diagnostic(
                        "AMBIGUOUS_ARCHITECTURE", "architecture '%s' of '%s' not selected (%s selects '%s')"
                        % (a.name, ent, how, chosen.name), "info", False, side, a.file, a.line, a.col))
        out.selected[(lib, ent)] = chosen
    # MISSING_UNIT: use clauses naming packages that are neither modelled nor present
    libs_known = set(STD_LIBS) | {u.library for u in out.units}
    seen = set()
    for u in out.units:
        ctx = list(u.context or [])
        if isinstance(u, ContextDecl):
            ctx.extend(u.items or [])
        ctx.extend(d for d in (getattr(u, "decls", None) or []) if isinstance(d, UseClause))
        for c in ctx:
            if not isinstance(c, UseClause):
                continue
            for p in c.paths:
                if len(p) < 2:
                    continue
                lib, pkg = p[0], p[1]
                if pkg == "all":
                    continue
                if (lib, pkg) in ieee.KNOWN_PACKAGES or (lib in ("std", "ieee") and lib + "." + pkg in ieee.IGNORED_PACKAGES):
                    continue
                if lib == "work":
                    if (u.library, pkg) in out.packages or out.find_package(pkg) is not None \
                            or (u.library, pkg) in out.contexts:
                        continue
                elif (lib, pkg) in out.packages or (lib, pkg) in out.contexts:
                    continue
                key = (u.file, lib, pkg)
                if key in seen:
                    continue
                seen.add(key)
                out.diagnostics.append(Diagnostic(
                    "MISSING_UNIT", "package '%s.%s' not found in the sources or the built-in IEEE model" % (lib, pkg),
                    "warning", True, side, u.file, c.line, c.col))
