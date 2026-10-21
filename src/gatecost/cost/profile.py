"""Cost profiles: loading, ``extends``, overrides, validation, TOML dump (COST_MODEL.md section 6).

A profile is resolved in three steps: the ``extends`` chain is merged (the built-in
``generic-asic`` is the root), ``[cost.override]`` from the project file is merged on top,
and the result is validated into an immutable ``Profile`` whose numbers are integer milli
(GE values) or integer permille (dimensionless ratios).
"""
from __future__ import annotations

import copy
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..errors import GatecostError
from ..result import Diagnostic
from .units import rdiv, to_milli

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"
DEFAULT_PROFILE = "generic-asic"

PRIMITIVES = (
    "INV BUF NAND2 NOR2 NAND3 NOR3 NAND4 NOR4 AND2 OR2 AOI21 OAI21 AOI22 OAI22 AO22 XOR2 XNOR2 "
    "MUX2 MUX4 MAJ3 HA FA DLAT DFF DFFR DFFRS EDFF SDFF_ADD ICG"
).split()

SECTION_ORDER = ("meta", "primitives", "coefficients", "factors", "policy", "alpha", "arch", "memory", "blackbox")
_ENUMS = {
    "timing": ("relaxed", "moderate", "aggressive"),
    "enable_style": ("mux", "icg", "edff"),
    "fsm_encoding": ("binary", "one_hot"),
    "resource_sharing": ("off", "auto"),
}
ADD_ARCHS = ("ripple", "brent_kung", "kogge_stone")


@dataclass(slots=True, frozen=True)
class Profile:
    name: str
    description: str
    source: str
    nand2_area_um2: float
    P: dict[str, int]  # primitives, milli-GE
    C: dict[str, int]  # per-operation coefficients, milli-GE
    F: dict[str, int]  # dimensionless factors, permille
    timing: str
    enable_style: str
    fsm_encoding: str
    icg_min_width: int
    scan: bool
    resource_sharing: str
    datapath_fusion: bool
    propagate_port_constants: bool
    sigma_arith: int  # permille
    sigma_other: int
    overhead_typ: int
    overhead_max: int
    alpha_add: int  # permille
    alpha_mul: int
    add_arch: tuple[str, str, str]
    mul_arch: tuple[str, str, str]
    booth_min_width: int
    macro_enable: bool
    macro_min_bits: int
    macro: dict[str, int]  # c0, c_bit, c_row, c_col in milli-GE
    macro_table: tuple[tuple[int, int, int], ...]  # (d, w, milli-GE)
    blackbox: dict[str, tuple[int, int, int]]  # name -> (min, typ, max) milli-GE
    raw: dict = field(default_factory=dict, compare=False)
    diagnostics: tuple[Diagnostic, ...] = field(default=(), compare=False)

    def scaled(self, num: int, den: int = 1) -> "Profile":
        """Every GE value (primitives, coefficients, macro, black boxes) scaled by num/den."""
        s = lambda v: rdiv(v * num, den)  # noqa: E731
        return replace(
            self,
            P={k: s(v) for k, v in self.P.items()},
            C={k: s(v) for k, v in self.C.items()},
            macro={k: s(v) for k, v in self.macro.items()},
            macro_table=tuple((d, w, s(g)) for d, w, g in self.macro_table),
            blackbox={k: tuple(s(x) for x in v) for k, v in self.blackbox.items()},
        )

    def blackbox_cost(self, name: str):
        """(min, typ, max) milli-GE of a listed black box, or None."""
        return self.blackbox.get(name)


# ---------------------------------------------------------------------------------------
# resolution

def builtin_names() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.toml"))


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise GatecostError("BAD_CONFIG", f"cannot read profile {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise GatecostError("BAD_CONFIG", f"invalid TOML in profile {path.name}: {exc}") from None


def _locate(spec: str, base_dir: Path | None) -> Path:
    names = builtin_names()
    looks_like_path = any(c in spec for c in "/\\") or spec.endswith(".toml")
    if spec in names and not looks_like_path:
        return PROFILE_DIR / f"{spec}.toml"
    p = Path(spec)
    if not p.is_absolute() and base_dir is not None:
        p = base_dir / p
    if p.is_file():
        return p
    raise GatecostError(
        "BAD_CONFIG", f"unknown profile {spec!r} (built-in: {', '.join(names)}; or give a path to a .toml file)"
    )


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _resolve(spec: str, base_dir: Path | None, chain: tuple[Path, ...]) -> dict:
    path = _locate(spec, base_dir).resolve()
    if path in chain:
        raise GatecostError("BAD_CONFIG", f"profile extends cycle through {path.name}")
    data = _read_toml(path)
    parent = data.pop("extends", None)
    meta = data.get("meta", {})
    if parent is None and "extends" in meta:
        parent = meta.pop("extends")
    elif "extends" in meta:
        meta.pop("extends")
    if parent is not None:
        if not isinstance(parent, str):
            raise GatecostError("BAD_CONFIG", "extends must be a profile name or path")
        merged = _merge(_resolve(parent, path.parent, chain + (path,)), data)
    else:
        merged = data
    if "name" not in data.get("meta", {}):
        merged.setdefault("meta", {})["name"] = path.stem
    return merged


def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise GatecostError("BAD_CONFIG", f"profile key {where} must be a number")
    if v < 0:
        raise GatecostError("BAD_CONFIG", f"profile key {where} must be >= 0")
    return v


def _int(v, where: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise GatecostError("BAD_CONFIG", f"profile key {where} must be a non-negative integer")
    return v


def _bool(v, where: str) -> bool:
    if not isinstance(v, bool):
        raise GatecostError("BAD_CONFIG", f"profile key {where} must be true or false")
    return v


def _enum(v, key: str) -> str:
    if v not in _ENUMS[key]:
        raise GatecostError("BAD_CONFIG", f"policy.{key} must be one of {', '.join(_ENUMS[key])}")
    return v


def _table(raw: dict, name: str) -> dict:
    t = raw.get(name, {})
    if not isinstance(t, dict):
        raise GatecostError("BAD_CONFIG", f"profile [{name}] must be a table")
    return t


def _fill_defaults(raw: dict, default: dict) -> dict:
    """Missing keys of every table except [primitives] and [meta] come from generic-asic."""
    out = copy.deepcopy(raw)
    for sec in ("coefficients", "factors", "policy", "alpha", "arch", "memory"):
        out[sec] = _merge(default.get(sec, {}), out.get(sec, {}) if isinstance(out.get(sec, {}), dict) else {})
    out.setdefault("blackbox", {})
    return out


def _known_keys(default: dict) -> dict[str, set[str]]:
    return {sec: set(default.get(sec, {})) for sec in SECTION_ORDER if sec not in ("blackbox", "meta")}


def build_profile(raw: dict, default: dict | None = None) -> Profile:
    """Validate a merged profile dictionary into a ``Profile``."""
    if default is None:
        default = _resolve(DEFAULT_PROFILE, None, ())
    raw = _fill_defaults(raw, default)
    diags: list[Diagnostic] = []
    known = _known_keys(default)
    for sec in raw:
        if sec not in SECTION_ORDER:
            diags.append(Diagnostic("UNKNOWN_PROFILE_KEY", f"unknown profile section [{sec}] ignored", severity="info"))
    for sec, keys in known.items():
        for k in raw.get(sec, {}):
            if k not in keys:
                diags.append(Diagnostic("UNKNOWN_PROFILE_KEY", f"unknown profile key {sec}.{k} ignored", severity="info"))

    meta = _table(raw, "meta")
    if "nand2_area_um2" not in meta:
        raise GatecostError("BAD_CONFIG", "profile must provide meta.nand2_area_um2")
    area = float(_num(meta["nand2_area_um2"], "meta.nand2_area_um2"))
    if area <= 0:
        raise GatecostError("BAD_CONFIG", "profile key meta.nand2_area_um2 must be > 0")

    prim = _table(raw, "primitives")
    missing = [p for p in PRIMITIVES if p not in prim]
    if missing:
        raise GatecostError("BAD_CONFIG", f"profile [primitives] lacks: {', '.join(missing)}")
    for k in prim:
        if k not in PRIMITIVES:
            diags.append(Diagnostic("UNKNOWN_PROFILE_KEY", f"unknown primitive {k} ignored", severity="info"))
    P = {k: to_milli(_num(prim[k], f"primitives.{k}")) for k in PRIMITIVES}
    C = {k: to_milli(_num(v, f"coefficients.{k}")) for k, v in _table(raw, "coefficients").items()}
    F = {k: to_milli(_num(v, f"factors.{k}")) for k, v in _table(raw, "factors").items()}

    pol = _table(raw, "policy")
    sizing = pol["sizing_factor"]
    over = pol["overhead"]
    for tbl, keys, nm in ((sizing, ("arith", "other"), "sizing_factor"), (over, ("typ", "max"), "overhead")):
        if not isinstance(tbl, dict) or any(k not in tbl for k in keys):
            raise GatecostError("BAD_CONFIG", f"policy.{nm} must be a table with keys {', '.join(keys)}")
    sig_a, sig_o = to_milli(_num(sizing["arith"], "policy.sizing_factor.arith")), to_milli(_num(sizing["other"], "policy.sizing_factor.other"))
    ov_t, ov_m = to_milli(_num(over["typ"], "policy.overhead.typ")), to_milli(_num(over["max"], "policy.overhead.max"))
    if min(sig_a, sig_o, ov_t, ov_m) < 1000:
        raise GatecostError("BAD_CONFIG", "sizing and overhead factors must be >= 1.0")

    alpha_t = {k: to_milli(_num(v, f"alpha.{k}")) for k, v in _table(raw, "alpha").items()}
    timing = _enum(pol["timing"], "timing")
    if timing not in alpha_t:
        raise GatecostError("BAD_CONFIG", f"[alpha] has no entry for timing {timing!r}")
    arch = _table(raw, "arch")
    add_t, mul_t = arch["add"], arch["mul"]
    if not isinstance(add_t, dict) or not isinstance(mul_t, dict):
        raise GatecostError("BAD_CONFIG", "arch.add and arch.mul must be tables")
    add_arch = tuple(add_t[k] for k in ("min", "mid", "max"))
    for a in add_arch:
        if a not in ADD_ARCHS:
            raise GatecostError("BAD_CONFIG", f"arch.add architecture must be one of {', '.join(ADD_ARCHS)}")
    mul_arch = tuple(str(mul_t[k]) for k in ("min", "mid", "max"))
    alphas = []
    for t in (add_t, mul_t):
        a = to_milli(_num(t["alpha"], "arch.alpha")) if "alpha" in t else alpha_t[timing]
        if a > 1000:
            raise GatecostError("BAD_CONFIG", "arch alpha must be within [0, 1]")
        alphas.append(a)

    mem = _table(raw, "memory")
    macro = mem["macro"]
    if not isinstance(macro, dict) or any(k not in macro for k in ("c0", "c_bit", "c_row", "c_col")):
        raise GatecostError("BAD_CONFIG", "memory.macro must define c0, c_bit, c_row, c_col")
    mtab = []
    for i, e in enumerate(mem.get("macro_table", [])):
        if not isinstance(e, dict) or any(k not in e for k in ("d", "w", "ge")):
            raise GatecostError("BAD_CONFIG", f"memory.macro_table[{i}] needs d, w, ge")
        mtab.append((_int(e["d"], "macro_table.d"), _int(e["w"], "macro_table.w"), to_milli(_num(e["ge"], "macro_table.ge"))))

    bb: dict[str, tuple[int, int, int]] = {}
    for name, v in _table(raw, "blackbox").items():
        if isinstance(v, dict):
            typ = to_milli(_num(v.get("typ", 0), f"blackbox.{name}.typ"))
            lo = to_milli(_num(v["min"], f"blackbox.{name}.min")) if "min" in v else typ
            hi = to_milli(_num(v["max"], f"blackbox.{name}.max")) if "max" in v else typ
            if not lo <= typ <= hi:
                raise GatecostError("BAD_CONFIG", f"blackbox {name!r} needs min <= typ <= max")
        else:
            typ = to_milli(_num(v, f"blackbox.{name}"))
            lo = hi = typ
        bb[name] = (lo, typ, hi)

    return Profile(
        name=str(meta.get("name", "custom")),
        description=str(meta.get("description", "")),
        source=str(meta.get("source", "")),
        nand2_area_um2=area,
        P=P,
        C=C,
        F=F,
        timing=timing,
        enable_style=_enum(pol["enable_style"], "enable_style"),
        fsm_encoding=_enum(pol["fsm_encoding"], "fsm_encoding"),
        icg_min_width=_int(pol["icg_min_width"], "policy.icg_min_width"),
        scan=_bool(pol["scan"], "policy.scan"),
        resource_sharing=_enum(pol["resource_sharing"], "resource_sharing"),
        datapath_fusion=_bool(pol["datapath_fusion"], "policy.datapath_fusion"),
        propagate_port_constants=_bool(pol["propagate_port_constants"], "policy.propagate_port_constants"),
        sigma_arith=sig_a,
        sigma_other=sig_o,
        overhead_typ=ov_t,
        overhead_max=ov_m,
        alpha_add=alphas[0],
        alpha_mul=alphas[1],
        add_arch=add_arch,  # type: ignore[arg-type]
        mul_arch=mul_arch,  # type: ignore[arg-type]
        booth_min_width=_int(mul_t.get("booth_min_width", 2), "arch.mul.booth_min_width"),
        macro_enable=_bool(mem["macro_enable"], "memory.macro_enable"),
        macro_min_bits=_int(mem["macro_min_bits"], "memory.macro_min_bits"),
        macro={k: to_milli(_num(macro[k], f"memory.macro.{k}")) for k in ("c0", "c_bit", "c_row", "c_col")},
        macro_table=tuple(sorted(mtab)),
        blackbox=bb,
        raw=raw,
        diagnostics=tuple(diags),
    )


def load_profile(spec: str = DEFAULT_PROFILE, overrides: dict | None = None, base_dir: Path | None = None) -> Profile:
    """Load a built-in profile by name or a TOML file by path; ``overrides`` is merged on top.

    ``overrides`` takes the same tables as a profile file (``[cost.override]`` of the
    project file). Raises ``GatecostError(BAD_CONFIG)`` on any problem.
    """
    raw = _resolve(spec, base_dir, ())
    if overrides:
        raw = _merge(raw, overrides)
    default = raw if spec == DEFAULT_PROFILE and not overrides else _resolve(DEFAULT_PROFILE, None, ())
    return build_profile(raw, default)


def list_profiles() -> list[tuple[str, str]]:
    out = []
    for n in builtin_names():
        meta = _read_toml(PROFILE_DIR / f"{n}.toml").get("meta", {})
        out.append((n, str(meta.get("description", ""))))
    return out


# ---------------------------------------------------------------------------------------
# TOML dump of a resolved profile (deterministic)

def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_val(x)}" for k, x in v.items()) + " }" if v else "{}"
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    raise GatecostError("INTERNAL", f"cannot dump {type(v).__name__} to TOML")


def _key(k: str) -> str:
    return k if k and all(c.isalnum() or c in "_-" for c in k) else _val(str(k))


def dump_profile(profile: Profile) -> str:
    """The resolved profile (after ``extends`` and overrides) as TOML text."""
    raw = profile.raw
    lines: list[str] = []
    for sec in SECTION_ORDER:
        body = raw.get(sec)
        if body is None:
            continue
        if lines:
            lines.append("")
        lines.append(f"[{sec}]")
        for k, v in body.items():
            lines.append(f"{_key(k)} = {_val(v)}")
    return "\n".join(lines) + "\n"
