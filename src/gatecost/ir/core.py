"""Core IR data structures: nets, nodes, connections, Module IR (IR.md sections 1-3)."""
from __future__ import annotations

from typing import NamedTuple

from .sigspec import Const, SigSpec, Slice, mask


class Origin(NamedTuple):
    """Source location.  ``def_*`` fields hold the macro definition site, if any."""

    file: str
    line: int
    column: int = 0
    def_file: str | None = None
    def_line: int = 0
    def_column: int = 0

    def short(self) -> str:
        s = f"{self.file}:{self.line}"
        if self.def_file is not None:
            s += f"<{self.def_file}:{self.def_line}"
        return s


class IRDiagnostic:
    """Diagnostic produced by the IR layer (``code`` as in FRONTEND.md section 6)."""

    __slots__ = ("code", "message", "severity", "affects_cost", "origin")

    def __init__(self, code: str, message: str, severity: str = "warning",
                 affects_cost: bool = False, origin: Origin | None = None) -> None:
        self.code = code
        self.message = message
        self.severity = severity
        self.affects_cost = affects_cost
        self.origin = origin

    def __repr__(self) -> str:
        return f"IRDiagnostic({self.code}: {self.message})"

    def as_result_diagnostic(self, side: str = "after"):
        """Convert to ``gatecost.result.Diagnostic``."""
        from ..result import Diagnostic

        o = self.origin
        return Diagnostic(self.code, self.message, self.severity, self.affects_cost, side,
                          o.file if o else None, o.line if o else None, o.column if o else None)


# ---------------------------------------------------------------------------
# Operation catalogue
# ---------------------------------------------------------------------------
BINARY_ARITH = ("ADD", "SUB", "MUL", "DIV", "MOD")
SHIFTS = ("SHL", "SHR", "SAR", "ROL", "ROR")
BITWISE2 = ("AND", "OR", "XOR", "XNOR")
REDUCTIONS = ("RED_AND", "RED_OR", "RED_XOR")
COMPARES = ("EQ", "NE", "LT", "LE", "GT", "GE")
STATE_OPS = ("DFF", "LATCH", "MEM")
OPAQUE_OPS = ("INST", "BLACKBOX")

ALL_OPS = (
    "ADD", "SUB", "NEG", "MUL", "DIV", "MOD", "POW",
    "SHL", "SHR", "SAR", "ROL", "ROR",
    "AND", "OR", "XOR", "XNOR", "NOT",
    "RED_AND", "RED_OR", "RED_XOR",
    "EQ", "NE", "LT", "LE", "GT", "GE",
    "MUX", "PMUX", "PRIORITY", "DECODE", "ENCODE", "SUM",
    "DFF", "LATCH", "MEM", "ROM", "INST", "BLACKBOX",
)

COMMUTATIVE = frozenset({"ADD", "MUL", "AND", "OR", "XOR", "XNOR", "EQ", "NE"})

#: Operand names, in positional order, for fixed-arity operations.
INPUT_NAMES: dict[str, tuple[str, ...]] = {
    "ADD": ("A", "B"), "SUB": ("A", "B"), "MUL": ("A", "B"), "DIV": ("A", "B"),
    "MOD": ("A", "B"), "NEG": ("A",), "POW": ("A",),
    "SHL": ("A", "B"), "SHR": ("A", "B"), "SAR": ("A", "B"), "ROL": ("A", "B"),
    "ROR": ("A", "B"),
    "AND": ("A", "B"), "OR": ("A", "B"), "XOR": ("A", "B"), "XNOR": ("A", "B"),
    "NOT": ("A",),
    "RED_AND": ("A",), "RED_OR": ("A",), "RED_XOR": ("A",),
    "EQ": ("A", "B"), "NE": ("A", "B"), "LT": ("A", "B"), "LE": ("A", "B"),
    "GT": ("A", "B"), "GE": ("A", "B"),
    "MUX": ("A", "B", "S"), "PMUX": ("A", "B", "S"), "PRIORITY": ("A", "B", "S"),
    "DECODE": ("A",), "ENCODE": ("A",), "ROM": ("A",),
}

#: Attributes that do not take part in hash-consing / identity.
NON_IDENTITY_ATTRS = frozenset({"merge_count", "inst", "mem_name", "recursive"})

ARITH_FUSABLE = frozenset({"ADD", "SUB", "NEG", "MUL"})


class Net:
    """A vector of ``width`` bits (IR.md section 2)."""

    __slots__ = ("id", "width", "signed", "name", "port", "driver", "kmask", "kval",
                 "demand", "fanout", "attrs", "_sig")

    def __init__(self, id: int, width: int, signed: bool = False, name: str | None = None,
                 port: str | None = None) -> None:
        self.id = id
        self.width = width
        self.signed = signed
        self.name = name
        self.port = port          # None | 'in' | 'out' | 'inout'
        self.driver = None        # Node (node output), 'port' (input port) or None
        self.kmask = 0            # known-bit mask (kmask, kval)
        self.kval = 0
        self.demand = 0           # demanded-bit mask
        self.fanout = 0
        self.attrs: dict = {}
        self._sig = None

    def __repr__(self) -> str:
        return f"Net({self.id}, {self.name or '_'}[{self.width}])"

    @property
    def sig(self) -> SigSpec:
        s = self._sig
        if s is None or s.width != self.width:
            s = self._sig = SigSpec((Slice(self, 0, self.width),))
        return s

    def __getitem__(self, idx) -> SigSpec:
        return self.sig[idx]


class Conn:
    """Wiring assignment ``dst[lo +: w] <- src``: zero cost, partial drives allowed."""

    __slots__ = ("net", "lo", "src", "origin")

    def __init__(self, net: Net, lo: int, src: SigSpec, origin: Origin | None = None) -> None:
        self.net = net
        self.lo = lo
        self.src = src
        self.origin = origin

    @property
    def width(self) -> int:
        return self.src.width

    def __repr__(self) -> str:
        return f"Conn({self.net!r}[{self.lo}+:{self.width}] <- {self.src!r})"


class Node:
    """An operation (IR.md section 3)."""

    __slots__ = ("id", "op", "inputs", "outputs", "attrs", "origin", "tags", "members",
                 "shape", "named_key", "eff", "alive")

    def __init__(self, id: int, op: str, inputs: tuple, outputs: tuple, attrs: dict,
                 origin: Origin | None) -> None:
        self.id = id
        self.op = op
        self.inputs = inputs          # tuple of (name, SigSpec)
        self.outputs = outputs        # tuple of Net
        self.attrs = attrs
        self.origin = origin
        self.tags: dict = {}
        self.members: tuple = ()
        self.shape: bytes | None = None
        self.named_key: tuple | None = None
        self.eff: dict | None = None
        self.alive = True

    def __repr__(self) -> str:
        return f"Node({self.id}, {self.op})"

    def input(self, name: str) -> SigSpec | None:
        for n, s in self.inputs:
            if n == name:
                return s
        return None

    def has_input(self, name: str) -> bool:
        return any(n == name for n, _ in self.inputs)

    @property
    def out(self) -> Net:
        return self.outputs[0]

    @property
    def width(self) -> int:
        return self.outputs[0].width

    @property
    def is_state(self) -> bool:
        return self.op in STATE_OPS or self.op in OPAQUE_OPS

    @property
    def merge_count(self) -> int:
        return self.attrs.get("merge_count", 1)


class ModuleIR:
    """Intermediate representation of one (module, parameters, constant ports)."""

    def __init__(self, name: str, params: dict | None = None,
                 const_ports: dict | None = None, key: str | None = None) -> None:
        self.name = name
        self.params: tuple = tuple(sorted((params or {}).items()))
        self.const_ports: tuple = tuple(sorted(
            (k, (v.value, v.width, v.xmask)) for k, v in (const_ports or {}).items()))
        self.key = key or module_key(name, params or {}, const_ports or {})
        self.nets: list[Net] = []
        self.nodes: list[Node] = []
        self.conns: list[Conn] = []
        self.ports: list[Net] = []
        self.diagnostics: list[IRDiagnostic] = []
        self.assumptions: list[str] = []
        self.origin: Origin | None = None
        # driver bookkeeping: net.id -> driven bit mask
        self.driven: dict[int, int] = {}
        self.next_net_id = 1
        self.next_node_id = 1
        self.next_group = 1
        self.is_blackbox = False          # replaced by a black box (limits)
        self.limit_reached: str | None = None
        self.analyzed = False
        self.share_sets: list[dict] = []
        self.canonical_hash: bytes | None = None
        self.conns_by_net: dict[int, list[Conn]] = {}
        self.conn_keys: dict[int, bytes] = {}
        # state elements / operations that reach no output and were removed (cost 0); see ``UNOBSERVABLE_LOGIC``
        self.unobservable = {"regs": 0, "ops": 0}

    # -- lookups ---------------------------------------------------------
    def port(self, name: str) -> Net:
        for p in self.ports:
            if p.name == name:
                return p
        raise KeyError(name)

    def input_ports(self) -> list[Net]:
        return [p for p in self.ports if p.port in ("in", "inout")]

    def output_ports(self) -> list[Net]:
        return [p for p in self.ports if p.port in ("out", "inout")]

    def diag(self, code: str, message: str, severity: str = "warning",
             affects_cost: bool = False, origin: Origin | None = None) -> IRDiagnostic:
        d = IRDiagnostic(code, message, severity, affects_cost, origin)
        self.diagnostics.append(d)
        return d

    def recount_fanout(self) -> None:
        """Recompute ``Net.fanout``: chunk references from node inputs and connection
        sources, plus one for every output/inout port (consumed by the environment)."""
        for n in self.nets:
            n.fanout = 0
        for nd in self.nodes:
            for _, s in nd.inputs:
                for c in s.chunks:
                    if c.__class__ is Slice:
                        c.net.fanout += 1
        for cn in self.conns:
            for c in cn.src.chunks:
                if c.__class__ is Slice:
                    c.net.fanout += 1
        for p in self.ports:
            if p.port in ("out", "inout"):
                p.fanout += 1

    def node_by_id(self) -> dict[int, Node]:
        return {n.id: n for n in self.nodes}

    def __repr__(self) -> str:
        return f"ModuleIR({self.key}, {len(self.nodes)} nodes, {len(self.nets)} nets)"


def _fmt_param(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, Const):
        return f"{v.width}'h{v.value:x}"
    return str(v)


def module_key(name: str, params: dict, const_ports: dict) -> str:
    """Canonical readable key ``name(P=v,...)[port=w'hV,...]`` (IR.md section 1)."""
    s = name
    if params:
        s += "(" + ",".join(f"{k}={_fmt_param(params[k])}" for k in sorted(params)) + ")"
    if const_ports:
        parts = []
        for k in sorted(const_ports):
            v = const_ports[k]
            if isinstance(v, Const):
                if v.xmask:
                    parts.append(f"{k}={v.width}'b" + "".join(
                        "x" if (v.xmask >> i) & 1 else str((v.value >> i) & 1)
                        for i in range(v.width - 1, -1, -1)))
                else:
                    parts.append(f"{k}={v.width}'h{v.value:x}")
            else:
                parts.append(f"{k}={v}")
        s += "[" + ",".join(parts) + "]"
    return s


def freeze(v):
    """Make an attribute value hashable and deterministic."""
    if isinstance(v, (list, tuple)):
        return tuple(freeze(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, freeze(x)) for k, x in v.items()))
    if isinstance(v, Const):
        return ("c", v.value, v.width, v.xmask)
    return v


def identity_attrs(attrs: dict) -> tuple:
    if not attrs:
        return ()
    items = []
    for k, v in attrs.items():
        if k in NON_IDENTITY_ATTRS:
            continue
        c = v.__class__
        items.append((k, v if (c is bool or c is int or c is str or v is None) else freeze(v)))
    items.sort()          # keys are unique: values are never compared
    return tuple(items)


__all__ = [
    "Origin", "IRDiagnostic", "Net", "Conn", "Node", "ModuleIR", "module_key",
    "ALL_OPS", "COMMUTATIVE", "INPUT_NAMES", "BINARY_ARITH", "SHIFTS", "BITWISE2",
    "REDUCTIONS", "COMPARES", "STATE_OPS", "OPAQUE_OPS", "ARITH_FUSABLE", "freeze",
    "identity_attrs", "NON_IDENTITY_ATTRS", "mask", "Const", "SigSpec", "Slice",
]
