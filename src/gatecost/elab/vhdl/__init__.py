"""VHDL elaborator (FRONTEND.md section 10.3-10.5): parsed VHDL units -> Module IRs in a shared Design.

API
---
``elaborate_vhdl(parsed, design, *, top=None, generics=None, limits=None, side="after", fsm_encoding="binary",
propagate_port_constants=True, foreign_lookup=None, ir_limits=None, only_modules=None, foreign=None, root=None)
-> VhdlElabResult``
    This is also the hook called by ``gatecost.elab.elaborate_project`` for ``.vhd``/``.vhdl`` files (``foreign`` is
    an alias of ``foreign_lookup``; ``only_modules`` stubs the entities outside the changed cone and lists their keys
    in ``stubbed``; ``root`` makes file names relative in diagnostics and origins).
    Adds a Module IR for the top entity (or for every root entity when ``top`` is None) and for every entity
    instantiated below it to ``design`` (keys from ``Design.make_key(entity, generics, constant ports)``, so the
    Verilog elaborator can share the same Design).  ``generics`` overrides top-level generics (python values).
    ``limits`` keys: generate_iterations, loop_unroll, inline_depth (alias call_depth), max_instances, expr_depth,
    const_steps (alias const_func_steps), max_hier_depth, and the IR limits max_nodes, max_nets, max_net_width,
    max_mem_depth.
    ``fsm_encoding`` is the profile policy (``binary`` | ``one_hot``) for enumerations used as state registers.
    ``VhdlElabResult``: ``tops`` (root entity names), ``top_keys`` ({name: Module IR key}), ``top_key`` (key of the
    selected top, or the only root; None when ambiguous), ``stubbed``, ``parsed_hashes`` (= ``unit_hashes``),
    ``diagnostics``
    (tuple of ``result.Diagnostic`` raised by the elaborator; IR-level ones such as INFERRED_LATCH and
    MULTIPLE_DRIVERS stay in ``ModuleIR.diagnostics``), ``assumptions`` (strings), ``unit_hashes``
    ({module name: hash of entity + architecture + the user packages they use}, for the differ).
``elaborate_vhdl_files(paths, *, top=None, generics=None, design=None, library_map=None, **kw) -> (VhdlElabResult, Design)``
    Parses and elaborates files with a fresh Design (used by tests).
``VhdlElaborator(parsed, design, ...)`` exposes ``.elaborate(top, generics)`` and ``.lookup(name)``.

Mixed-language hooks
--------------------
``foreign_lookup(name) -> module | None`` is called for an instantiated name that is not a VHDL entity (exact
name, then lower case).  The returned object provides ``name``; ``parameters`` ({name: default int/str});
``ports(params) -> [(port name, 'in'|'out'|'inout', width, signed)]``; ``elaborate(params, const_ports) -> str | None``
(elaborates the module into the shared Design and returns its key; None gives a black box).  Without a
lookup (or when it returns None) the instance is a black box with ``MISSING_MODULE``.  Conversely
``VhdlElaborator.lookup(name)`` returns an object with the same protocol for a VHDL entity, so the Verilog
elaborator can instantiate VHDL entities.

Behaviour notes and known gaps
------------------------------
* Clocked processes are recognised from ``rising_edge``/``falling_edge``, ``clk'event and clk = '1'`` (either order,
  also ``not clk'stable``) and ``wait until <edge>`` as the first statement; leading ``if``/``elsif`` branches before
  the clock branch become asynchronous resets, extra ``and`` terms next to the edge become an enable.  Reset-like
  (constant-only) first arms inside the clocked branch are peeled so that SRST/EN stay recognisable, as in the
  Verilog elaborator.  Signals assigned in a clocked process are registers read at their old value; variables
  are immediate and become registers only when read before being written.
* Enumerations: binary ``ceil(log2 N)`` bits; ``fsm_encoding="one_hot"`` re-encodes (all signals of) enumeration
  types that are assigned in clocked processes; an ``enum_encoding`` attribute (``onehot`` or explicit code words)
  overrides the policy.  Comparisons with one-hot states read a single bit.
* Arrays of vectors/records written with a non-constant index in a clocked process become ``MEM`` (read ports are
  asynchronous; a registered read is a DFF after the port); constant arrays with an indexed read become ``ROM``.
* ``inout``/``buffer`` ports are ``out`` ports when the architecture drives them, ``in`` otherwise.  Record ports
  are flattened to ``port.field`` ports; the instance output of a whole plain signal drives that net directly.
* ``if/else`` chains assigning ``'Z'`` in a branch become tri-state ``MUX`` nodes (``tristate=True``).
* Variable slice position with a constant width (``a(i+3 downto i)``, ``to`` or ``downto``) is a multiplexer for
  reads and a decoded write for targets (the two bounds must differ by a constant).  An entity port that is
  unconstrained takes its length from the port-map actual (the length is part of the module key as the pseudo
  generic ``$len:<port>``).  A conversion function / type conversion on an output formal
  (``std_logic_vector(z) => y``) is wiring.  Instance outputs to indexed, sliced or aggregate targets go through a
  small combinational block.  ``generic (type T)`` entities are bound from the generic map (``T =>
  std_logic_vector(7 downto 0)``).  Constant byte-lane writes into array-of-vector memories are lane writes (see the
  Verilog notes: one write port with per-lane enables).  ``case`` statements and selected assignments mapping
  constant choices to constants (at least 6 choices, selector 3..12 bits, ``others`` or full coverage) are ROMs.
  Guarded blocks keep their statements unguarded with a diagnostic.
* ``ieee.fixed_pkg`` (``ufixed``/``sfixed``) has the package result ranges and quantisation/overflow styles
  (``fixed.py``); ``ieee.float_pkg`` has exact types/constants and an approximate datapath model (``floatp.py``;
  ``ctx.assume`` records it).  An array that is assigned as a whole in a clocked process (a reset of
  ``mem <= (others => (others => '0'))``) is a register file, not a RAM.  Overloads that differ only by result type
  (hdl4fpga ``"**"``) are chosen from the expected type.  Ports declared with a VHDL-2019 mode view get a direction
  per flattened record leaf.
* Gaps (degrade with a diagnostic): non-constant slice widths, ``wait`` other than the leading
  ``wait until <edge>``, ``while`` loops with data-dependent conditions, configuration bodies, generic
  subprograms and design-unit-level generic package instantiation (the parser skips them with one diagnostic; a
  package instantiated in a declarative region works), external names (parsed; value unknown), arrays of records
  behind view ports, physical/real hardware values.
"""
from __future__ import annotations

from .build import Elab, VhdlElabResult, VhdlElaborator

__all__ = ["elaborate_vhdl", "elaborate_vhdl_files", "VhdlElabResult", "VhdlElaborator"]


def elaborate_vhdl(parsed, design, *, top=None, generics=None, limits=None, side="after", fsm_encoding="binary",
                   propagate_port_constants=True, foreign_lookup=None, ir_limits=None, only_modules=None,
                   foreign=None, root=None, **_unused):
    el = VhdlElaborator(parsed, design, limits=limits, side=side, fsm_encoding=fsm_encoding,
                        propagate_port_constants=propagate_port_constants,
                        foreign_lookup=foreign_lookup if foreign_lookup is not None else foreign,
                        ir_limits=ir_limits, root=root, only_modules=only_modules)
    return el.elaborate(top, generics)


def elaborate_vhdl_files(paths, *, top=None, generics=None, design=None, library_map=None, architectures=None, **kw):
    from ...frontend.vhdl import parse_files
    from ...ir import Design

    parsed = parse_files(paths, library_map, architectures=architectures, side=kw.get("side", "after"))
    design = design if design is not None else Design()
    res = elaborate_vhdl(parsed, design, top=top, generics=generics, **kw)
    return res, design
