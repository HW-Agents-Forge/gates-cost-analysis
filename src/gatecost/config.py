"""Project file `gatecost.toml` and settings resolution (CLI.md sections 1 and 4)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import GatecostError
from .result import Diagnostic

FORMATS = ("agent", "json", "text", "markdown")

# section -> key -> accepted python type(s)
_SCHEMA: dict[str, dict[str, tuple[type, ...]]] = {
    "project": {
        "top": (str,),
        "sources": (list,),
        "filelists": (list,),
        "incdirs": (list,),
        "exclude": (list,),
        "defines": (dict,),
        "languages": (list,),
        "libraries": (dict,),
        "architectures": (dict,),
    },
    "cost": {"profile": (str,), "override": (dict,)},
    "budget": {"max_ge": (int, float), "max_delta_ge": (int, float), "basis": (str,)},
    "report": {"max_lines": (int,), "threshold_ge": (int, float), "format": (str,)},
    "limits": {
        "generate_iterations": (int,),
        "loop_unroll": (int,),
        "inline_depth": (int,),
        "max_instances": (int,),
        "macro_depth": (int,),
        "include_depth": (int,),
    },
    "cache": {"enabled": (bool,), "dir": (str,)},
}

DEFAULT_EXCLUDES = ("build", "sim", "work", "obj_dir", "xcelium.d", ".venv")


@dataclass(slots=True)
class ProjectConfig:
    path: Path | None = None
    data: dict = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    def get(self, section: str, key: str, default=None):
        return self.data.get(section, {}).get(key, default)


def load_project_file(path: Path | None) -> ProjectConfig:
    if path is None:
        return ProjectConfig()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise GatecostError("BAD_CONFIG", f"cannot read {path}: {exc.strerror}") from None
    except tomllib.TOMLDecodeError as exc:
        raise GatecostError("BAD_CONFIG", f"invalid TOML in {path.name}: {exc}") from None
    cfg = ProjectConfig(path=path, data=data)
    for section, body in data.items():
        spec = _SCHEMA.get(section)
        if spec is None or not isinstance(body, dict):
            if spec is None:
                cfg.diagnostics.append(_unknown(section))
                continue
            raise GatecostError("BAD_CONFIG", f"[{section}] must be a table")
        for key, value in body.items():
            types = spec.get(key)
            if types is None:
                cfg.diagnostics.append(_unknown(f"{section}.{key}"))
            elif isinstance(value, bool) and bool not in types or not isinstance(value, types):
                names = "/".join(t.__name__ for t in types)
                raise GatecostError("BAD_CONFIG", f"{section}.{key} must be of type {names}")
    fmt = cfg.get("report", "format")
    if fmt is not None and fmt not in FORMATS:
        raise GatecostError("BAD_CONFIG", f"report.format must be one of {', '.join(FORMATS)}")
    basis = cfg.get("budget", "basis")
    if basis is not None and basis not in ("typ", "max"):
        raise GatecostError("BAD_CONFIG", "budget.basis must be 'typ' or 'max'")
    return cfg


def _unknown(name: str) -> Diagnostic:
    return Diagnostic("UNKNOWN_CONFIG_KEY", f"unknown key {name} ignored", severity="info")


@dataclass(slots=True)
class Settings:
    root: Path
    format: str = "agent"
    profile: str = "generic-asic"
    max_lines: int = 10
    threshold_ge: float = 5
    top: str | None = None
    defines: dict[str, str] = field(default_factory=dict)
    incdirs: list[str] = field(default_factory=list)
    filelists: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    budget: float | None = None
    fail_on_increase: float | None = None
    basis: str = "typ"
    no_cache: bool = False
    verbose: bool = False
    quiet: bool = False
    no_meta: bool = False
    config_path: Path | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)


def find_project_root(cwd: Path, skill_dir: Path) -> Path:
    """Nearest ancestor with gatecost.toml, else with .git outside the skill dir, else cwd."""
    cwd = cwd.resolve()
    chain = [cwd, *cwd.parents]
    for d in chain:
        if (d / "gatecost.toml").is_file():
            return d
    for d in chain:
        if (d / ".git").exists() and not _inside(d, skill_dir):
            return d
    return cwd


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _parse_define(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not name.isidentifier():
            raise GatecostError("BAD_ARGUMENT", f"--define expects NAME[=VALUE], got {item!r}")
        out[name] = value if sep else "1"
    return out


def resolve_settings(args, cwd: Path, skill_dir: Path, env: dict[str, str] | None = None) -> tuple[Settings, ProjectConfig]:
    """Precedence: command line > gatecost.toml > environment > defaults."""
    env = os.environ if env is None else env
    g = lambda name: getattr(args, name, None)  # noqa: E731 - options differ per command

    root = Path(g("root")).resolve() if g("root") else find_project_root(cwd, skill_dir)
    if g("config"):
        cfg_path = Path(g("config"))
        if not cfg_path.is_file():
            raise GatecostError("BAD_CONFIG", f"config file not found: {cfg_path}")
    else:
        cfg_path = root / "gatecost.toml"
        cfg_path = cfg_path if cfg_path.is_file() else None
    cfg = load_project_file(cfg_path)
    base = cfg_path.parent if cfg_path else root

    s = Settings(root=root, config_path=cfg_path, diagnostics=list(cfg.diagnostics))

    def pick(cli, toml, envv, default):
        for v in (cli, toml, envv):
            if v is not None:
                return v
        return default

    s.format = pick(g("format"), cfg.get("report", "format"), env.get("GATECOST_FORMAT") or None, "agent")
    if s.format not in FORMATS:
        raise GatecostError("BAD_ARGUMENT", f"GATECOST_FORMAT must be one of {', '.join(FORMATS)}")
    s.profile = pick(g("profile"), cfg.get("cost", "profile"), env.get("GATECOST_PROFILE") or None, "generic-asic")
    s.max_lines = pick(g("max_lines"), cfg.get("report", "max_lines"), None, 10)
    s.threshold_ge = pick(g("threshold"), cfg.get("report", "threshold_ge"), None, 5)
    s.top = pick(g("top"), cfg.get("project", "top"), None, None)
    s.defines = {**{k: str(v) for k, v in cfg.get("project", "defines", {}).items()}, **_parse_define(g("define") or [])}
    s.incdirs = list(g("incdir") or []) + [str(base / p) for p in cfg.get("project", "incdirs", [])]
    s.filelists = list(g("filelist") or []) + [str(base / p) for p in cfg.get("project", "filelists", [])]
    s.sources = list(cfg.get("project", "sources", []))
    s.exclude = list(DEFAULT_EXCLUDES) + list(cfg.get("project", "exclude", []))
    s.budget = pick(g("budget"), cfg.get("budget", "max_ge"), None, None)
    s.fail_on_increase = pick(g("fail_on_increase"), cfg.get("budget", "max_delta_ge"), None, None)
    s.basis = pick(g("basis"), cfg.get("budget", "basis"), None, "typ")
    s.no_cache = bool(g("no_cache")) or env.get("GATECOST_NO_CACHE") == "1" or cfg.get("cache", "enabled") is False
    s.verbose = bool(g("verbose"))
    s.quiet = bool(g("quiet"))
    s.no_meta = bool(g("no_meta"))
    return s, cfg
