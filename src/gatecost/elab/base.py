"""Evaluator base: shared state, diagnostics and origins."""
from __future__ import annotations

from .decls import DeclMixin
from .expr import ExprMixin
from .stmts import StmtMixin

#: code -> (severity, affects_cost)
DIAG_DEFAULTS = {
    "UNRESOLVED_NAME": ("warning", True),
    "UNRESOLVED_PARAM": ("warning", True),
    "MISSING_MODULE": ("warning", True),
    "MISSING_UNIT": ("warning", True),
    "UNSUPPORTED_CONSTRUCT": ("warning", True),
    "LIMIT_REACHED": ("warning", True),
    "TRISTATE": ("info", False),
    "INFERRED_LATCH": ("warning", False),
    "TOP_AMBIGUOUS": ("info", False),
    "INTERNAL": ("warning", True),
    "STUBBED_MODULE": ("info", False),
    "DUPLICATE_MODULE": ("warning", True),
}


class BaseEv(ExprMixin, DeclMixin, StmtMixin):
    def __init__(self, ctx, scope, b, modb=None):
        self.ctx = ctx
        self.scope = scope
        self.b = b
        self.modb = modb
        self.blk = None
        self._tm = {}
        self._unres = set()
        self._dseen = set()
        self.tmp_cands = None
        self.rbw = set()
        self.ctl = []
        self.created = []
        self.block_locals = []
        self.depth = 0
        self.fsteps = 0
        self.fkey = 0
        self._ctrl_memo = {}
        self._reset_memo = {}
        self.mem_ports = {}
        self.forced_vars = {}
        self.assign_counts = {}
        self.sync_read_done = set()
        self._cur = None
        self.path = ""

    # ------------------------------------------------------------------ diagnostics
    def org(self, loc):
        if loc is None:
            return None
        return self.ctx.origin(loc)

    def diag(self, code, message, node=None, affects=None, severity=None):
        loc = None
        if node is not None:
            loc = node if isinstance(node, tuple) else getattr(node, "loc", None)
        if loc is None and self._cur is not None:
            loc = getattr(self._cur, "loc", None)
        sev, aff = DIAG_DEFAULTS.get(code, ("warning", True))
        if severity is not None:
            sev = severity
        if affects is not None:
            aff = bool(affects)
        key = (code, loc, message)
        if key in self._dseen:
            return
        self._dseen.add(key)
        o = self.ctx.origin(loc) if loc is not None else None
        if self.modb is not None:
            self.modb.module.diag(code, message, sev, aff, o)
        else:
            self.ctx.add_diag(code, message, sev, aff, o)

    def assume(self, text):
        if self.modb is not None:
            if text not in self.modb.module.assumptions:
                self.modb.module.assumptions.append(text)
        else:
            self.ctx.assumptions.add(text)

