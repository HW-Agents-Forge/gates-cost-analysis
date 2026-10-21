"""Design: the table of Module IRs, instance counts and recursion detection (IR.md 1, 8)."""
from __future__ import annotations

from .analyze import analyze_ir
from .core import ModuleIR, Node, module_key


class Design:
    """Elaborated top plus Module IRs keyed by ``(module, parameters, constant ports)``.

    Keys are the canonical strings of :func:`gatecost.ir.module_key`.
    """

    def __init__(self, top: str | None = None) -> None:
        self.modules: dict[str, ModuleIR] = {}
        self.top = top
        self.diagnostics: list = []

    @staticmethod
    def make_key(name: str, params: dict | None = None, const_ports: dict | None = None) -> str:
        """Key of a (module, parameter set, constant-port signature)."""
        return module_key(name, params or {}, const_ports or {})

    @staticmethod
    def const_port_signature(inputs: dict) -> dict:
        """Ports of an instance tied to a constant (no don't-care bits): the signature used
        to uniquify the child (``propagate_port_constants``).  ``inputs`` maps port name ->
        SigSpec as passed to ``Builder.inst``."""
        out = {}
        for name, sig in inputs.items():
            c = sig.const_value() if hasattr(sig, "const_value") else None
            if c is not None and c.width > 0 and not c.xmask:
                out[name] = c
        return out

    def add(self, m: ModuleIR) -> ModuleIR:
        self.modules[m.key] = m
        return m

    def get(self, key: str) -> ModuleIR | None:
        return self.modules.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self.modules

    def keys(self) -> list[str]:
        return sorted(self.modules)

    # -- hierarchy ----------------------------------------------------------
    def instances(self, key: str) -> list[Node]:
        m = self.modules[key]
        return sorted((n for n in m.nodes if n.op == "INST"), key=lambda n: n.id)

    def detect_recursion(self) -> list:
        """Mark INST nodes that close a recursion (``tags['recursive']``); the cycle is
        cut there and ``RECURSIVE_INSTANCE`` is reported on the instantiating module."""
        found = []
        if self.top is None or self.top not in self.modules:
            return found
        done: set[str] = set()

        def visit(key: str, path: list[str]) -> None:
            m = self.modules[key]
            for inst in self.instances(key):
                ck = inst.attrs.get("module_key")
                if ck not in self.modules:
                    continue
                if ck in path or ck == key:
                    if not inst.tags.get("recursive"):
                        inst.tags["recursive"] = True
                        d = m.diag("RECURSIVE_INSTANCE",
                                   f"recursive instantiation of {ck} in {key}; cut at the "
                                   "second occurrence", "warning", True, inst.origin)
                        found.append(d)
                    continue
                if ck in done:
                    continue
                visit(ck, path + [ck])
            done.add(key)

        visit(self.top, [self.top])
        return found

    def instance_counts(self) -> dict[str, int]:
        """Instances of each module key, multiplied along the hierarchy (top = 1).
        Recursive instances are not counted."""
        if self.top is None or self.top not in self.modules:
            return {}
        self.detect_recursion()
        counts: dict[str, int] = {}
        # topological order of the hierarchy DAG (parents first)
        order: list[str] = []
        state: dict[str, int] = {}

        def dfs(key: str) -> None:
            state[key] = 1
            for inst in self.instances(key):
                ck = inst.attrs.get("module_key")
                if ck in self.modules and not inst.tags.get("recursive") and state.get(ck, 0) == 0:
                    dfs(ck)
            state[key] = 2
            order.append(key)

        dfs(self.top)
        order.reverse()
        counts[self.top] = 1
        for key in order:
            c = counts.get(key, 0)
            for inst in self.instances(key):
                ck = inst.attrs.get("module_key")
                if ck in self.modules and not inst.tags.get("recursive"):
                    counts[ck] = counts.get(ck, 0) + c * int(inst.attrs.get("count", 1))
        return counts

    def missing_modules(self) -> list[str]:
        out = set()
        for m in self.modules.values():
            for n in m.nodes:
                if n.op == "INST" and n.attrs.get("module_key") not in self.modules:
                    out.add(n.attrs.get("module_key"))
        return sorted(out)

    # -- analysis -----------------------------------------------------------
    def analyze(self, **kw) -> "Design":
        for key in self.keys():
            analyze_ir(self.modules[key], **kw)
        return self


def export_design(design: Design) -> dict:
    """Plain-data view of an analyzed design: modules, instance counts, hierarchy edges."""
    from .export import export_module

    counts = design.instance_counts()
    edges = []
    for key in design.keys():
        for inst in design.instances(key):
            edges.append({"parent": key, "child": inst.attrs.get("module_key"),
                          "name": inst.attrs.get("inst"), "count": inst.attrs.get("count", 1),
                          "recursive": bool(inst.tags.get("recursive"))})
    return {"top": design.top, "instance_counts": counts, "edges": edges,
            "missing_modules": design.missing_modules(),
            "modules": {key: export_module(design.modules[key]) for key in design.keys()}}


def analyze_design(design: Design, **kw) -> Design:
    return design.analyze(**kw)
