"""Shared state and helpers of the VHDL elaborator."""
from __future__ import annotations

import os

from ...ir import Const, Origin, SigSpec
from ...ir.sigspec import mask
from ...result import Diagnostic
from . import vtypes as T
from .vtypes import VType, minbits
from .values import Val

DEFAULT_LIMITS = {
    "generate_iterations": 4096, "loop_unroll": 1024, "inline_depth": 32, "max_instances": 100000,
    "expr_depth": 500, "const_steps": 100000, "max_hier_depth": 128,
}
#: names used by the Verilog elaborator / project file for the same limits
_LIMIT_ALIASES = {"call_depth": "inline_depth", "const_func_steps": "const_steps"}

_AFFECTS = {
    "UNSUPPORTED_CONSTRUCT": ("warning", True), "MISSING_MODULE": ("warning", True), "MISSING_UNIT": ("warning", True),
    "UNRESOLVED_PARAM": ("warning", True), "LIMIT_REACHED": ("warning", True), "UNRESOLVED_OVERLOAD": ("warning", True),
    "MULTIPLE_DRIVERS": ("warning", True), "AMBIGUOUS_ARCHITECTURE": ("warning", True),
    "SIMULATION_ONLY_PROCESS": ("info", False), "IGNORED_REGION": ("info", False), "INFERRED_LATCH": ("warning", False),
    "RECURSIVE_INSTANCE": ("warning", True), "DUPLICATE_MODULE": ("warning", True),
    "DUPLICATE_DECLARATION": ("warning", True), "NON_CONSTANT_BOUND": ("warning", True),
}


class Unsupported(Exception):
    """A construct the elaborator degrades on (reported as UNSUPPORTED_CONSTRUCT)."""


class NotConst(Exception):
    """A constant was required but the value is hardware."""


class StopLimit(Exception):
    """A limit was reached; the enclosing construct is abandoned."""


#: Exceptions by which the elaborator reports, by design, that a construct is outside the path being tried
#: (a value that is not constant, a shape it does not handle).  A *speculative probe* (a fast path tried
#: before the general one, an overload-resolution heuristic) catches exactly these and takes its documented
#: fallback; the general path reports the construct itself when it really is a problem.  Any other exception
#: is an elaborator bug: it is not caught by the probe and reaches the statement / unit boundary handler,
#: which reports it as a diagnostic (:func:`failure_text`).  Limits (:class:`StopLimit`, ``RecursionError``)
#: are never swallowed by a probe.
PROBE_MISS: tuple[type[Exception], ...] = (NotConst, Unsupported)


def failure_text(ctx: object, ex: BaseException) -> str:
    """Boundary handlers (statement, operator, unit, root entity) degrade an unexpected exception into a
    diagnostic that quotes it: this returns the ``'Type: message'`` text to quote, or re-raises ``ex`` when the
    debug switch of the run (``GATECOST_VHDL_DEBUG``) is set, so that tests see the traceback."""
    if getattr(ctx, "debug", False):
        raise ex
    return "%s: %s" % (type(ex).__name__, ex)


class Ctx:
    """State shared by every module elaborated for one run."""

    def __init__(self, parsed, design, side, limits, fsm_encoding, propagate, foreign_lookup, ir_limits,
                 root=None, only_modules=None):
        self.parsed = parsed
        self.design = design
        self.side = side
        self.limits = dict(DEFAULT_LIMITS)
        for k, v in (limits or {}).items():
            if isinstance(v, int) and not isinstance(v, bool):
                self.limits[_LIMIT_ALIASES.get(k, k)] = v
        if ir_limits is None and limits and any(k in limits for k in ("max_nodes", "max_nets", "max_net_width", "max_mem_depth")):
            from ...ir import Limits
            il = Limits()
            ir_limits = Limits(*(int(limits.get(k, getattr(il, k))) for k in ("max_nodes", "max_nets", "max_net_width", "max_mem_depth")))
        self.root = root
        self.only = None if only_modules is None else {str(x).lower() for x in only_modules}
        self._cone = {}
        self._disp = {}
        self.stubbed = set()
        self.fsm_encoding = fsm_encoding
        self.propagate = propagate
        self.foreign_lookup = foreign_lookup
        self.ir_limits = ir_limits
        self.diags = []
        self._seen = set()
        self.assumptions = []
        self._aseen = set()
        self.pkgs = {}
        self.building = []
        self.instances = 0
        self.unit_deps = {}      # module name -> set of unit hash keys
        self.entity_iface = {}
        self.top_reported = False

    def disp(self, f):
        d = self._disp.get(f)
        if d is None:
            d = f
            if self.root and f and os.path.isabs(f):
                try:
                    r = os.path.relpath(f, self.root)
                    if not r.startswith(".."):
                        d = r.replace(os.sep, "/")
                except ValueError:
                    pass
            self._disp[f] = d
        return d

    def in_cone(self, ent_name):
        """True when ``only_modules`` is unset, or the entity is (or instantiates) a changed module."""
        if self.only is None:
            return True
        name = ent_name.lower()
        if name in self._cone:
            return self._cone[name]
        self._cone[name] = False            # cycle guard
        res = name in self.only
        if not res:
            from ...frontend.vhdl import nodes as N
            for (lib, en), lst in self.parsed.architectures.items():
                if en != name:
                    continue
                for a in lst:
                    for n in N.walk(a):
                        if isinstance(n, N.Instance) and n.unit and self.in_cone(n.unit[-1]):
                            res = True
                            break
                    if res:
                        break
        self._cone[name] = res
        return res

    def diag(self, code, msg, file=None, line=None, col=None, severity=None, affects=None):
        file = self.disp(file) if file else file
        k = (code, file, line, col, msg)
        if k in self._seen:
            return
        self._seen.add(k)
        ds, da = _AFFECTS.get(code, ("warning", False))
        self.diags.append(Diagnostic(code, msg, severity or ds, da if affects is None else affects, self.side, file, line, col))

    def assume(self, msg):
        if msg not in self._aseen:
            self._aseen.add(msg)
            self.assumptions.append(msg)


def int_val(n):
    return Val(T.T_UINT, None, int(n))


def real_val(x):
    return Val(T.T_REAL, None, float(x))


def bool_val(sig):
    return Val(T.T_BOOLEAN, sig)


def bool_const(b):
    return Val(T.T_BOOLEAN, SigSpec.const(1 if b else 0, 1), 1 if b else 0)


def range_int_type(lo, hi):
    w, s = minbits(lo, hi)
    return VType("int", "", "nat" if lo >= 0 else "int", low=lo, high=hi, width=w, signed=s)


def to_signed_int(const):
    return const.as_signed()


def const_of_sig(sig):
    """Constant (no x bits) value of a SigSpec or None."""
    c = sig.const_value()
    if c is None or c.xmask:
        return None
    return c
