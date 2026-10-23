"""Differ: compares two cost trees and explains the difference (DIFF.md, SPEC.md 5.6).

API
    ``diff_trees(before, after, *, before_sources=None, after_sources=None, threshold_ge=5,
    config_mismatch=None) -> DiffResult``

``before_sources`` / ``after_sources`` map file path -> source text (used only for the line
alignment: tier 3 matching, removed-line translation, cause tracing). Without them the
alignment degrades to identical line numbers and removed operations keep their before
location. ``config_mismatch`` is None (auto-detect from ``tree.config`` keys ``profile``,
``defines``, ``top``), a bool, or the list of differing keys. A ``profile`` mismatch yields
``NOT_COMPARABLE`` (the delta is still filled in); other mismatches emit ``CONFIG_MISMATCH``
and cap confidence at low. ``before``/``after`` may be None (side not analyzable) and give
``NOT_COMPARABLE``.

Result conventions (additive extras on ``DiffResult``/``Contributor``)
    ``delta_scenarios`` is the exact per-scenario total delta; ``delta`` is the same with the
    residual range of COST_MODEL 5.1 (``typ`` identical). Every ``Contributor`` has
    ``scenario`` (exact, additive) and ``delta`` (with its own residual range).
    ``sum(c.scenario for c in all_c) == delta_scenarios`` per scenario, also for ``by_line``,
    ``by_cause``, ``category_scenarios`` and by_module intrinsic + multiplicity.
    ``contributors`` is ``all_contributors`` minus those folded into ``others_*`` (|typ| below
    ``threshold_ge``); ``others_net`` is the exact scenario sum of the folded ones.

Provenance facts convention (what the cost-tree builder should emit in ``CostOp.facts``)
    Facts are compared by the differ in the fixed order of DIFF.md section 5; all keys are
    optional. File/line origins are ``(file, line)`` in the coordinates of the op's own side.

    ``params``          {name: value}   parameters/generics/localparams that determined the op
    ``param_origins``   {name: (file, line)}  line defining or overriding the value that was used
    ``consts``          {name: value}   constant operands (e.g. {"rhs": 5}); ``const_origins`` likewise
    ``widths``          {name: int}     operand/result widths; ``width_origins`` likewise (a port
                        width or localparam line). ``declared_widths``/``effective_widths`` of
                        the op are also compared for the ``width`` reason.
    ``struct``          any comparable value describing composition (case branches, PMUX inputs)
    ``sharing`` / ``fusion``   optional extra comparable values; the tags ``shared``/``fused`` and
                        the length of ``members`` are compared as well
    ``reg``             {reset, enable, ...} register attributes (also: seq breakdown key set)
    ``defines``         {NAME: value}   macros whose value/definedness gates this op or its width
    ``define_origins``  {NAME: (file, line)}  the ``define / ``ifdef line

    Reason order: parameter, constant, width, structure, sharing, fusion, register,
    architecture (BLACKBOX ops always give ``blackbox``). A cause is attached when the origin of
    a changed fact lies on a changed line (line alignment) -> ``{"kind","name","file","line",
    "from","to"}`` with kind parameter | constant | define; ``cause=define:NAME`` also when a
    define differs between ``before.config["defines"]`` and ``after.config["defines"]`` (or its
    origin line changed) for an added/removed op carrying ``defines`` facts.

Tree contract relied upon: ``canonical_hash`` covers ops (shape keys), parameters, ports and the
instance list (child keys and counts); equal hash and equal instance count means contributes
zero. An empty hash disables the short-circuit.
"""
from .engine import diff_trees, match_ops

__all__ = ["diff_trees", "match_ops"]
