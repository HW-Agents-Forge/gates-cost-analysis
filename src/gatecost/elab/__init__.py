"""Elaborator: parsed RTL to word-level IR (FRONTEND.md section 2.5, IMPLEMENTATION.md stages 4 and 6).

Public API
==========
::

    from gatecost.elab import elaborate_project, ElabResult

    res = elaborate_project(files, root=root, top=None, defines=None, incdirs=(), limits=None,
                            side="after", only_modules=None, propagate_const_ports=True)

``files``       paths of ``.v .vh .sv .svh`` (Verilog/SystemVerilog, handled here) and ``.vhd .vhdl``
                (routed to the VHDL elaborator, see below), absolute or relative to ``root``; command-line
                order matters (macros persist, the last of several definitions of a module wins).
``root``        project root; locations in diagnostics and IR origins are shown relative to it.
``top``         module/entity name, or ``None`` to auto-detect (modules instantiated by no other module).
                An unknown name raises ``GatecostError("TOP_NOT_FOUND")``; no module at all raises
                ``GatecostError("NO_SOURCES")``.  Several roots: every root is elaborated into the same
                design (identical sub-elaborations are shared), ``tops`` lists them, ``top_keys`` maps each
                to its Module IR key, and ``top_key`` is the first one (alphabetical) with a ``TOP_AMBIGUOUS``
                info diagnostic; ``--top`` selects one.  A pure instantiation cycle selects the first module.
``defines``     macro definitions (``SYNTHESIS`` is defined by the parser by default).
``incdirs``     include directories.
``limits``      dict overriding the FRONTEND.md section 7 limits.  Keys and defaults: ``macro_depth`` 64,
                ``include_depth`` 32, ``generate_iterations`` 4096, ``loop_unroll`` 1024, ``call_depth`` 32
                (alias ``inline_depth``), ``max_instances`` 100000, ``expr_depth`` 500, ``const_func_steps`` 100000, plus
                ``max_hier_depth`` 128 and ``const_port_variants`` 256 (maximum distinct constant-port
                specializations per ``(module, parameters)`` before propagation is switched off for it),
                and the IR limits ``max_nodes``, ``max_nets``, ``max_net_width``, ``max_mem_depth``.
                Reaching a limit is a ``LIMIT_REACHED`` diagnostic, never an exception.
``side``        ``"before"``/``"after"`` stamped on every diagnostic.
``only_modules``  set of module names whose bodies must be elaborated ("changed cone", FRONTEND.md section 5).
                Those modules and all their ancestors up to the top are elaborated for every parameterization
                reached; the other modules are *stubbed*: their key is computed (header, parameters and
                constant ports are evaluated) and an ``INST`` refers to it, but no Module IR is built.  The keys
                are returned in ``ElabResult.stubbed`` so the differ can substitute cached costs.  ``None``
                (default) elaborates everything reachable from the top.
``propagate_const_ports``  constant-port propagation (COST_MODEL.md 4.7); when False identical
                ``(module, parameters)`` instances share one Module IR regardless of constant inputs.

``ElabResult`` (dataclass)
    ``design``         :class:`gatecost.ir.Design`, already ``analyze()``d, ``design.top`` set to ``top_key``
    ``top_key``        Module IR key of the selected top or ``None``
    ``tops``           candidate roots (names), sorted
    ``top_keys``       ``{name: key}`` for every elaborated root
    ``diagnostics``    tuple of :class:`gatecost.result.Diagnostic` (parser, elaborator, IR; deduplicated)
    ``assumptions``    tuple of strings (sorted, unique) describing every approximation applied
    ``parsed_hashes``  ``{module name: normalized AST digest}``
    ``stubbed``        keys of modules not built because of ``only_modules``

Language routing and the VHDL hook contract
===========================================
``.vhd``/``.vhdl`` files are parsed with ``gatecost.frontend.vhdl.parse_files(paths, side=side)`` and
elaborated by a *lazily imported, optional* function::

    gatecost.elab.vhdl.elaborate_vhdl(parsed, design, *, top, limits, side, only_modules, foreign, root)

* ``parsed``  the ``ParsedVhdl`` object, ``design`` the shared :class:`gatecost.ir.Design`: the function
  adds its (not yet analyzed) ``ModuleIR`` objects with ``design.add(m)`` and must not call ``analyze``;
* ``top``     requested top entity name or ``None`` (auto-detect), ``limits`` the limits dict above,
  ``only_modules`` as above, ``root`` the project root (for relative locations);
* ``foreign`` cross-language instantiation: a callable ``foreign(name) -> module | None`` giving the
  Verilog/SystemVerilog modules to the VHDL elaborator (``name``, ``parameters``, ``ports(params)``,
  ``elaborate(params, const_ports) -> key``; the protocol documented in ``elab/vhdl/__init__.py``).  In the
  other direction the Verilog elaborator instantiates VHDL entities through ``VhdlElaborator.lookup`` when the
  real ``gatecost.elab.vhdl`` is used.  A registered override that also wants to serve Verilog -> VHDL
  instantiation is an object with a ``bind(parsed, design, *, top, limits, side, only_modules, foreign, root)``
  method (called before any Verilog module is elaborated) returning a ``lookup(name) -> module | None``
  with the same protocol; a plain registered function gets only the VHDL -> Verilog direction.  An unresolved
  foreign module is a black box plus ``MISSING_MODULE``.  With roots in both languages the alphabetically first
  one is the default top (``TOP_AMBIGUOUS``).  VHDL entities instantiated only from Verilog (and vice versa) are not
  reported as extra tops;
* returns an object with attributes ``top_key`` (str | None), ``tops`` (list[str]), ``diagnostics``
  (iterable of ``Diagnostic``), ``assumptions`` (iterable of str) and ``parsed_hashes``
  (``{unit name: digest}``); optional ``top_keys`` (dict) and ``stubbed`` (iterable of keys).

If the module or the function does not exist the VHDL files are skipped with an ``UNSUPPORTED_CONSTRUCT``
diagnostic.  Tests or other front-ends can install an implementation with
:func:`register_language_elaborator` (``register_language_elaborator("vhdl", fn)``) without touching
this package.  No file of this package needs to change when ``elab/vhdl/`` appears.

Verilog/SystemVerilog notes (approximations and gaps)
====================================================
* Enums without explicit base type use the minimum binary width (like VHDL), not 32 bits.
* Unpacked structs are flattened like packed ones; unpacked array ports are flattened into one vector.
* ``unique`` / ``unique0`` / ``priority`` and the ``full_case`` / ``parallel_case`` attributes or ``// synopsys``
  pragmas are honoured through ``LowerBlock.case_``/``if_chain(parallel=, full=)``: ``parallel`` forces a ``PMUX``
  (``priority`` forces ``PRIORITY``), ``full`` makes the last arm the default so no feedback/latch is created.
* ``inout`` ports are ``in`` ports unless the module drives them, then ``out`` (tri-state mux costed).
* Black-box port directions are guessed: a connection to an otherwise undriven net is an output.
* An unpacked array is a memory when it is indexed with a signal-dependent index and written in a procedural
  block, and not written with loop-variable indices (reset loops give registers); otherwise its elements are
  separate registers/wires.  A synchronous read (``q <= mem[a]`` with ``q`` assigned nowhere else in the block)
  becomes a synchronous read port; other reads are asynchronous.  Constant part-select writes of a memory word
  (``mem[a][8*i +: 8] <= ...``, byte enables) are lane writes: the IR merges disjoint lanes of one address into a
  single write port with a per-lane enable input ``w<i>_be`` (attribute ``wlanes``); a part-select at a variable
  offset is approximated by a full-word write (flagged).
* A blocking assignment to a variable in a clocked block makes it a register only when it is read before it is
  written (or used outside the block); otherwise it is a combinational temporary.
* Procedural ``for``/``while``/``repeat`` loops are unrolled when their condition is constant at each step.  A
  ``for`` loop whose bound is not constant is unrolled with a guard per iteration (``condition`` AND-ed along the
  way) up to ``2**k + 1`` iterations for the widest ``k``-bit signal in the condition, at most
  ``limits["dyn_loop_unroll"]`` (default 32), with an ``UNSUPPORTED_CONSTRUCT`` warning; other non-constant loops
  and ``forever`` are reported and skipped.
* ROM inference: array parameters/localparams (``parameter int T [0:3] = '{...}``, the element count is taken from
  the pattern because the parser drops the unpacked dimension) indexed by a signal, and ``case`` statements that
  map constant labels (at least 6, selector 3..12 bits, a default or full coverage) to constants of one target,
  become a ``ROM`` node with its contents.
* ``a ** n`` with a variable exponent is a square-and-multiply ladder over (at most) 6 exponent bits (``2 ** n`` is
  a shift).  A wire declared with a constant initializer reads as that constant.  Streaming concatenations are
  supported as assignment targets (``{<< 8 {a, b}} = x`` reverses the slices of the source).
* ``defparam`` supports ``inst.param``, ``gen[3].inst.param`` and paths through child instances
  (``mid.u.W``, forwarded to the child as the pseudo parameter ``$dp:u.W`` of its module key); a leading
  own-module name is accepted.  Hierarchical references into other modules and interface arrays / virtual
  interfaces are diagnosed (``UNSUPPORTED_CONSTRUCT``) and skipped.
* Assignment to an undeclared identifier is skipped with ``UNRESOLVED_NAME``.
* Set the environment variable ``GATECOST_ELAB_DEBUG=1`` to let internal errors propagate (tests, debugging).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace

from gatecost._lazy import LazyMap
from gatecost.errors import GatecostError
from gatecost.frontend.verilog import ast as A
from gatecost.result import Diagnostic

_VERILOG_EXT = {".v", ".vh", ".sv", ".svh"}
_VHDL_EXT = {".vhd", ".vhdl"}

_REGISTRY: dict = {}


def register_language_elaborator(language: str, fn) -> None:
    """Install (or, with ``fn=None``, remove) the elaborator function of a language (``"vhdl"``)."""
    if fn is None:
        _REGISTRY.pop(language, None)
    else:
        _REGISTRY[language] = fn


@dataclass(slots=True)
class ElabResult:
    design: object
    top_key: str | None
    tops: list
    diagnostics: tuple
    assumptions: tuple
    _hashes: object
    top_keys: dict = field(default_factory=dict)
    stubbed: tuple = ()
    _facts: object = None

    @property
    def file_facts(self) -> dict | None:
        """Per parsed Verilog file (path as given): modules with the module names they instantiate, whether the
        file must always be parsed (compilation-unit items, packages, interfaces), and its parse diagnostics.
        ``None`` when the project mixes languages.  Computed on first access."""
        f = self._facts
        if callable(f):
            f = self._facts = f()
        return f

    @property
    def parsed_hashes(self) -> dict:
        """``{module name: digest}``; computed on first access (nothing in the pipeline needs it)."""
        h = self._hashes
        if callable(h):
            h = self._hashes = h()
        return h


def _vhdl_elaborator():
    fn = _REGISTRY.get("vhdl")
    if fn is not None:
        return fn
    try:
        from .vhdl import elaborate_vhdl      # type: ignore[attr-defined]
        return elaborate_vhdl
    except Exception:
        return None


def _abs(p, root):
    s = os.fspath(p)
    if os.path.isabs(s):
        return s
    if root is not None:
        cand = os.path.join(os.fspath(root), s)
        if os.path.exists(cand):
            return cand
    return s


def _file_facts(parsed, ctx) -> dict:
    by_file: dict = {}
    nofile = []
    for d in parsed.diagnostics:
        if d.file:
            by_file.setdefault(d.file, []).append(replace(d, side="after", file=ctx.disp(d.file)))
        else:
            nofile.append(replace(d, side="after"))
    out = {}
    if nofile:
        out[""] = {"mods": [], "unit": True, "diags": nofile}
    for pf in parsed.files:
        mods = [[m.name, "module", sorted(ctx.scan(m).insts)] for m in pf.modules]
        mods += [[m.name, "interface", sorted(ctx.scan(m).insts)] for m in pf.interfaces]
        out[pf.path] = {"mods": mods, "unit": bool(pf.unit_items or pf.packages or pf.interfaces),
                        "diags": by_file.get(pf.path, [])}
    return out


def _merge_maps(maps):
    out = {}
    for m in maps:
        out.update(m)
    return out


def _dedupe(diags):
    seen = set()
    out = []
    for d in diags:
        k = (d.code, d.message, d.file, d.line, d.column, d.side)
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


def quick_facts(files, *, root, defines: dict | None = None, incdirs=(), limits: dict | None = None) -> dict:
    """Parse ``files`` (Verilog/SystemVerilog only) and return their ``ElabResult.file_facts`` without elaborating."""
    from gatecost.frontend.verilog import Limits as PLimits
    from gatecost.frontend.verilog import parse_files as vparse

    from .project import DEFAULT_LIMITS, Ctx

    alias = {"inline_depth": "call_depth", "const_steps": "const_func_steps"}
    limits = {alias.get(k, k): v for k, v in (limits or {}).items() if isinstance(v, int) and not isinstance(v, bool)}
    lim = dict(DEFAULT_LIMITS)
    lim.update(limits)
    old_rec = sys.getrecursionlimit()
    if old_rec < 30000:
        sys.setrecursionlimit(30000)
    try:
        parsed = vparse([_abs(f, root) for f in files], defines=defines, incdirs=tuple(os.fspath(i) for i in incdirs),
                        limits=PLimits(macro_depth=lim["macro_depth"], include_depth=lim["include_depth"],
                                       expr_depth=lim["expr_depth"]))
        return _file_facts(parsed, Ctx(parsed, root, limits, "after", None, True))
    finally:
        if sys.getrecursionlimit() != old_rec:
            sys.setrecursionlimit(old_rec)


def elaborate_project(files, *, root, top: str | None = None, defines: dict | None = None, incdirs=(),
                      limits: dict | None = None, side: str = "after", only_modules=None,
                      propagate_const_ports: bool = True, roots_hint=None, needed_hint=None, extra_parse_diags=()) -> ElabResult:
    """Parse and elaborate a set of RTL files into an analyzed :class:`gatecost.ir.Design`."""
    from gatecost.frontend.verilog import Limits as PLimits
    from gatecost.frontend.verilog import parse_files as vparse

    from .project import DEFAULT_LIMITS, Ctx, elaborate_verilog

    files = list(files)
    if not files:
        raise GatecostError("NO_SOURCES", "no RTL source files to analyze")
    v_files = [_abs(f, root) for f in files if os.path.splitext(os.fspath(f))[1].lower() in _VERILOG_EXT]
    h_files = [_abs(f, root) for f in files if os.path.splitext(os.fspath(f))[1].lower() in _VHDL_EXT]
    alias = {"inline_depth": "call_depth", "const_steps": "const_func_steps"}
    limits = {alias.get(k, k): v for k, v in (limits or {}).items() if isinstance(v, int) and not isinstance(v, bool)}
    lim = dict(DEFAULT_LIMITS)
    lim.update(limits)
    old_rec = sys.getrecursionlimit()
    if old_rec < 30000:
        sys.setrecursionlimit(30000)
    from .expr import _PN
    _PN.clear()
    try:
        parsed = vparse(v_files, defines=defines, incdirs=tuple(os.fspath(i) for i in incdirs),
                        limits=PLimits(macro_depth=lim["macro_depth"], include_depth=lim["include_depth"],
                                       expr_depth=lim["expr_depth"]))
        ctx = Ctx(parsed, root, limits, side, only_modules, propagate_const_ports)
        ctx.roots_hint = list(roots_hint) if roots_hint is not None else None
        ctx.needed_hint = set(needed_hint) if needed_hint is not None else None
        diags = [replace(d, side=side, file=ctx.disp(d.file) if d.file else d.file) for d in parsed.diagnostics]
        if extra_parse_diags:      # parse diagnostics of files the caller left out (changed-cone analysis)
            diags = sorted(diags + [replace(d, side=side) for d in extra_parse_diags],
                           key=lambda d: (d.file or "", d.line or 0, d.column or 0))
        v_units = list(parsed.modules) + list(parsed.interfaces)
        tops: list = []
        top_keys: dict = {}
        top_key = None
        vh = _VhdlSide(ctx, h_files, top, lim, side, only_modules, root, propagate_const_ports) if h_files else None
        if vh is not None:
            diags.extend(vh.diags)
        v_names_low = {n.lower() for n in ctx.modmap}
        h_names_low = vh.entity_names_low() if vh is not None else set()
        in_v = top is not None and top in ctx.modmap
        in_h = top is not None and not in_v and top.lower() in h_names_low
        if top is not None and not in_v and not in_h:
            raise GatecostError("TOP_NOT_FOUND", f"top module '{top}' not found in the sources")
        run_v = bool(parsed.modules) and (top is None or in_v)
        run_h = vh is not None and vh.ok and (top is None or in_h)
        if not parsed.modules and (vh is None or not h_names_low):
            raise GatecostError("NO_SOURCES", "no module or entity found in the sources")
        hres = None
        h_hashes = None
        if run_v:
            exclude = vh.instantiated_low() if vh is not None else set()
            name, top_keys, tops = elaborate_verilog(parsed, ctx, top if in_v else None, exclude,
                                                    have_other_language=vh is not None)
            if name is not None:
                top_key = top_keys.get(name)
        if run_h:
            v_inst_low = {x.lower() for m in list(parsed.modules) + list(parsed.interfaces)
                          for x in ctx.scan(m).insts}
            hres = vh.run(top if in_h else None, v_inst_low)
            if hres is not None:
                diags.extend(hres["diags"])
                for t in hres["tops"]:
                    if t not in tops:
                        tops.append(t)
                for k, v in hres["top_keys"].items():
                    top_keys.setdefault(k, v)
                if top_key is None:
                    top_key = hres["top_key"]
                ctx.stubbed.update(hres["stubbed"])
                ctx.assumptions.update(hres["assumptions"])
                h_hashes = hres["hashes"]
        if top is None and vh is not None and len(tops) > 1:
            # roots of both languages: the alphabetically first one is the default top
            first = next((t for t in sorted(tops) if t in top_keys), None)
            if first is not None:
                top_key = top_keys[first]
            if not any(d.code == "TOP_AMBIGUOUS" for d in ctx.sink):
                ctx.add_diag("TOP_AMBIGUOUS", "several top-level units (" + ", ".join(sorted(tops)) + "); each root is "
                             "elaborated separately, use --top to select one", "info", False, None)
        design = ctx.design
        design.top = top_key
        if top_key is not None and top_key not in design.modules and top_key in ctx.stubbed:
            pass
        try:
            design.analyze()
        except Exception as exc:       # analysis problems never abort
            ctx.add_diag("INTERNAL", f"analysis error ({type(exc).__name__}: {exc})", "error", True, None)
        # effective instance count check
        try:
            if design.top in design.modules:
                tot = sum(design.instance_counts().values())
                if tot > lim["max_instances"]:
                    ctx.add_diag("LIMIT_REACHED", f"elaborated hierarchy has {tot} instances "
                                 f"(limit {lim['max_instances']})", "warning", True, None)
        except Exception:
            pass
        diags.extend(replace(d, side=side) for d in ctx.sink)
        asm = set(ctx.assumptions)
        for key in sorted(design.modules):
            m = design.modules[key]
            for d in m.diagnostics:
                diags.append(d.as_result_diagnostic(side))
            asm.update(m.assumptions)
        if tops == [] and top_key is not None:
            tops = [top_key.split("(")[0]]
        def hashes():
            out = {}
            for m in v_units:
                try:
                    out[m.name] = m.digest()
                except Exception:
                    pass
            if h_hashes is not None:
                out.update(h_hashes)
            if vh is not None:
                out.update(vh.hashes)
            return out

        def facts():
            if vh is not None:
                return None
            return _file_facts(parsed, ctx)

        return ElabResult(design, top_key, sorted(tops), tuple(_dedupe(diags)), tuple(sorted(asm)), hashes,
                          top_keys, tuple(sorted(ctx.stubbed)), facts)
    finally:
        _PN.clear()
        if sys.getrecursionlimit() != old_rec:
            sys.setrecursionlimit(old_rec)


class _VhdlSide:
    """VHDL half of a project: parsing, the hook (or the elaborator class) and cross-language wiring."""

    def __init__(self, ctx, h_files, top, lim, side, only, root, propagate):
        self.ctx = ctx
        self.diags: list = []
        self.hashes: dict = {}
        self.ok = False
        self.vel = None
        self.fn = _REGISTRY.get("vhdl")
        self.lim, self.side, self.only, self.root, self.propagate = lim, side, only, root, propagate
        self.parsed = None
        try:
            from gatecost.frontend.vhdl import parse_files as hparse
        except Exception:
            hparse = None
        if self.fn is None:
            try:
                from .vhdl import VhdlElaborator
            except Exception:
                VhdlElaborator = None
                self.fn = _vhdl_elaborator()
        else:
            VhdlElaborator = None
        if hparse is None or (self.fn is None and VhdlElaborator is None):
            self.diags.append(Diagnostic("UNSUPPORTED_CONSTRUCT",
                                         "VHDL elaboration is not available; VHDL files skipped", "warning", True,
                                         side, None, None, None))
            return
        try:
            self.parsed = hparse(h_files, side=side)
        except Exception as exc:
            self.diags.append(Diagnostic("PARSE_ERROR", f"VHDL front-end failure ({type(exc).__name__}: {exc})",
                                         "error", True, side, None, None, None))
            return
        self.diags.extend(replace(d, side=side, file=ctx.disp(d.file) if d.file else d.file)
                          for d in self.parsed.diagnostics)
        self.ok = True
        bind = getattr(self.fn, "bind", None) if self.fn is not None and VhdlElaborator is None else None
        if callable(bind):
            # a registered elaborator object may serve Verilog -> VHDL instantiation through ``bind``
            try:
                ctx.foreign = bind(self.parsed, ctx.design, top=top, limits=dict(lim), side=side,
                                   only_modules=set(only) if only is not None else None,
                                   foreign=ctx.verilog_lookup, root=root)
            except Exception as exc:
                self.diags.append(Diagnostic("UNSUPPORTED_CONSTRUCT",
                                             f"VHDL elaborator bind failed ({type(exc).__name__}: {exc})",
                                             "warning", True, side, None, None, None))
        if VhdlElaborator is not None:
            try:
                self.vel = VhdlElaborator(self.parsed, ctx.design, limits=dict(lim), side=side,
                                          propagate_port_constants=propagate, foreign_lookup=ctx.verilog_lookup,
                                          ir_limits=ctx.ir_limits, root=root, only_modules=only)
                ctx.foreign = self.vel.lookup
            except Exception as exc:
                self.vel = None
                self.fn = _vhdl_elaborator()
                self.diags.append(Diagnostic("UNSUPPORTED_CONSTRUCT",
                                             f"VHDL elaborator failed to start ({type(exc).__name__}: {exc})",
                                             "warning", True, side, None, None, None))

    def entity_names_low(self):
        if self.parsed is None:
            return set()
        return {n for (_l, n) in self.parsed.entities}

    def instantiated_low(self):
        out = set()
        if self.parsed is None:
            return out
        try:
            from gatecost.frontend.vhdl import nodes as N
            for u in self.parsed.units:
                for n in N.walk(u):
                    if isinstance(n, N.Instance) and getattr(n, "unit", None):
                        out.add(str(n.unit[-1]).lower())
        except Exception:
            pass
        return out

    def run(self, top, v_inst_low):
        side = self.side
        if self.vel is not None:
            vel = self.vel
            try:
                if top is not None:
                    names = [top]
                else:
                    names = [e.name for e in vel.roots(None) if e.name.lower() not in v_inst_low]
                tops, top_keys, stubbed, asm, hashes, diags = [], {}, set(), set(), [], []
                for nm in names:
                    r = vel.elaborate(nm)
                    for t in r.tops:
                        if t not in tops:
                            tops.append(t)
                    top_keys.update(r.top_keys)
                    stubbed.update(r.stubbed)
                    asm.update(r.assumptions)
                    hashes.append(r.parsed_hashes)
                    diags = list(r.diagnostics)
                tk = None
                if top is not None and tops:
                    tk = top_keys.get(tops[0])
                elif len(tops) == 1:
                    tk = top_keys.get(tops[0])
                return {"tops": tops, "top_keys": top_keys, "top_key": tk, "stubbed": stubbed, "assumptions": asm,
                        "hashes": LazyMap(lambda: _merge_maps(hashes)),
                        "diags": [replace(d, side=side) for d in diags]}
            except GatecostError:
                raise
            except Exception as exc:
                return {"tops": [], "top_keys": {}, "top_key": None, "stubbed": set(), "assumptions": set(),
                        "hashes": {}, "diags": [Diagnostic("UNSUPPORTED_CONSTRUCT",
                                                           f"VHDL elaboration failed ({type(exc).__name__}: {exc})",
                                                           "warning", True, side, None, None, None)]}
        try:
            res = self.fn(self.parsed, self.ctx.design, top=top, limits=dict(self.lim), side=side,
                          only_modules=set(self.only) if self.only is not None else None,
                          foreign=self.ctx.verilog_lookup, root=self.root)
        except GatecostError:
            raise
        except Exception as exc:
            return {"tops": [], "top_keys": {}, "top_key": None, "stubbed": set(), "assumptions": set(),
                    "hashes": {}, "diags": [Diagnostic("UNSUPPORTED_CONSTRUCT",
                                                       f"VHDL elaboration failed ({type(exc).__name__}: {exc})",
                                                       "warning", True, side, None, None, None)]}
        tops = list(getattr(res, "tops", ()) or ())
        top_keys = dict(getattr(res, "top_keys", None) or {})
        tk = getattr(res, "top_key", None)
        if tk and not top_keys and tops:
            top_keys = {tops[0]: tk}
        return {"tops": tops, "top_keys": top_keys, "top_key": tk,
                "stubbed": set(getattr(res, "stubbed", ()) or ()),
                "assumptions": set(getattr(res, "assumptions", ()) or ()),
                "hashes": getattr(res, "parsed_hashes", None) or {},
                "diags": [replace(d, side=side) for d in (getattr(res, "diagnostics", ()) or ())]}


__all__ = ["elaborate_project", "quick_facts", "ElabResult", "register_language_elaborator"]
