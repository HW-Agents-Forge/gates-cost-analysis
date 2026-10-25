"""Snapshots: a stored copy of the relevant sources, for use without git (CLI.md section 2.4)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .baseline import _DirTree, restrict_prefixes, select_sources
from .cache import inside_skill_dir, project_key, user_state_dir, writable_state_dir
from .discovery import language_of
from .errors import GatecostError
from .params import canon as canon_params
from .params import to_text
from .paths import state_dir
from .result import Diagnostic

DEFAULT_KEEP = 10
_ID_RE = re.compile(r"[0-9a-f]{4,64}\Z")


@dataclass
class SnapshotInfo:
    id: str
    dir: Path
    files: list[str]
    defines: dict[str, str]
    profile: str
    modules: int
    created_ns: int
    params: dict = field(default_factory=dict)    # name -> value text (see gatecost.params)

    @property
    def files_dir(self) -> Path:
        return self.dir / "files"


class SnapshotTree(_DirTree):
    def __init__(self, root: Path, files: list[str]) -> None:
        super().__init__(root)
        self._files = set(files)

    def exists(self, rel: str) -> bool:
        return rel in self._files

    def walk_dir(self, rel: str) -> list[str]:
        prefix = rel.rstrip("/") + "/" if rel not in ("", ".") else ""
        return sorted(f for f in self._files if f.startswith(prefix))

    def rtl_candidates(self, settings):
        return sorted(self._files), []


_COMMENT_V = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_COMMENT_VHDL = re.compile(r"--[^\n]*")
_MOD_V = re.compile(r"(?<![\w$])(?:macro)?module\s+(?:automatic\s+|static\s+)?[A-Za-z_\\]")
_MOD_VHDL = re.compile(r"(?<![\w])entity\s+\w+\s+is\b", re.I)


def count_modules(text: str, lang: str | None) -> int:
    """Cheap count of `module`/`entity` declarations (comments stripped)."""
    if lang == "vhdl":
        return len(_MOD_VHDL.findall(_COMMENT_VHDL.sub("", text)))
    if lang == "verilog":
        return len(_MOD_V.findall(_COMMENT_V.sub("", text)))
    return 0


def _dirs(root: Path) -> list[Path]:
    out = []
    st = state_dir(root)
    if not inside_skill_dir(st):
        out.append(st / "snapshots")
    fb = user_state_dir(root)
    if not inside_skill_dir(fb):
        out.append(fb / "snapshots")
    return out


def _load(d: Path) -> SnapshotInfo | None:
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        return SnapshotInfo(
            id=str(meta["id"]), dir=d, files=list(meta["files"]), defines=dict(meta.get("defines", {})),
            profile=str(meta.get("profile", "")), modules=int(meta.get("modules", 0)), created_ns=int(meta["created_ns"]),
            params=dict(meta.get("params", {})),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def list_snapshots(root: Path) -> list[SnapshotInfo]:
    """All snapshots of the project, newest first."""
    found: dict[str, SnapshotInfo] = {}
    for base in _dirs(root):
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for d in children:
            info = _load(d) if d.is_dir() and _ID_RE.match(d.name) else None
            if info and (info.id not in found or info.created_ns > found[info.id].created_ns):
                found[info.id] = info
    return sorted(found.values(), key=lambda i: (i.created_ns, i.id), reverse=True)


def find(root: Path, which: str = "latest") -> SnapshotInfo | None:
    snaps = list_snapshots(root)
    if not snaps:
        return None
    if which in ("latest", ""):
        return snaps[0]
    hits = [s for s in snaps if s.id == which] or [s for s in snaps if s.id.startswith(which)]
    return hits[0] if len(hits) == 1 else None


def _rmtree(d: Path) -> None:
    def onerr(fn, path, _e):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            fn(path)
        except OSError:
            pass

    shutil.rmtree(d, onerror=onerr)


def create(settings, paths=(), keep: int | None = None) -> tuple[SnapshotInfo, list[Diagnostic]]:
    root = Path(settings.root).resolve()
    keep = DEFAULT_KEEP if keep is None else keep
    if keep < 1:
        raise GatecostError("BAD_ARGUMENT", "--keep must be >= 1")
    tree = _DirTree(root)
    rels = select_sources(tree, settings, restrict=restrict_prefixes(root, paths) or None)
    rtl_rels = [r for r in rels if language_of(r)]
    if not rtl_rels:
        raise GatecostError("NO_SOURCES", "no RTL sources found to snapshot")
    h = hashlib.blake2b(digest_size=16)
    extra = [json.dumps(canon_params(settings.params))] if getattr(settings, "params", None) else []
    for part in (json.dumps(sorted(settings.defines.items())), settings.profile, *extra):
        h.update(part.encode() + b"\0")
    contents: dict[str, bytes] = {}
    modules = 0
    for r in rels:
        data = tree.read(r)
        contents[r] = data
        h.update(r.encode("utf-8", "surrogateescape") + b"\0" + hashlib.blake2b(data, digest_size=16).digest())
        lang = language_of(r)
        if lang:
            modules += count_modules(data.decode("utf-8", "replace"), lang)
    sid = h.hexdigest()[:8]

    diags: list[Diagnostic] = []
    st, fallback = writable_state_dir(root)
    if st is None:
        raise GatecostError("INTERNAL", "no writable location for snapshots")
    if fallback:
        diags.append(Diagnostic("SNAPSHOT_LOCATION", "project not writable; snapshot stored in the user cache", severity="info"))
    snaps = st / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".new-", dir=snaps))
    try:
        for r, data in contents.items():
            p = tmp / "files" / r
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            p.chmod(0o444)
        meta = {"id": sid, "created_ns": time.time_ns(), "files": sorted(contents), "defines": dict(settings.defines),
                "params": {k: to_text(v) for k, v in getattr(settings, "params", {}).items()},
                "profile": settings.profile, "modules": modules, "root": str(root)}
        (tmp / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        final = snaps / sid
        if final.exists():
            _rmtree(final)
        os.replace(tmp, final)
    except BaseException:
        _rmtree(tmp)
        raise
    # rotation: oldest removed first
    here = [s for s in list_snapshots(root) if s.dir.parent == snaps]
    for old in here[keep:]:
        _rmtree(old.dir)
    info = _load(final)
    assert info is not None
    return info, diags


def delete(root: Path, which: str | None, all_: bool = False) -> list[str]:
    root = Path(root).resolve()
    if all_ or which == "all":
        targets = list_snapshots(root)
        for base in _dirs(root):  # also stale or unreadable entries
            if base.is_dir():
                for d in base.iterdir():
                    if d.is_dir() and d.name not in {t.dir.name for t in targets} and _ID_RE.match(d.name):
                        _rmtree(d)
    else:
        info = find(root, which or "")
        if info is None:
            raise GatecostError("BAD_ARGUMENT", f"snapshot {which} not found")
        targets = [info]
    for t in targets:
        _rmtree(t.dir)
    return [t.id for t in targets]
