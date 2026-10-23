"""Baseline resolution for `diff` (DISTRIBUTION.md section 4.1, CLI.md section 2.2).

A baseline is a read-only copy of the sources at the "before" state (`before_root`), the tree
holding the "after" state (`after_root`), and the relative paths that differ.  Both trees use
identical relative paths so that diagnostics and line alignment work.  `git` is invoked only
with fixed argument lists and is never required.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from . import REPO_ROOT
from .errors import GatecostError
from .discovery import ALWAYS_EXCLUDED, RTL_EXT, discover
from .result import Diagnostic

CONFIG_NAME = "gatecost.toml"
_MAX_INCLUDE_FILES = 5000


# --------------------------------------------------------------------------- result type


@dataclass
class Baseline:
    before_root: Path
    after_root: Path
    changed_files: list[str]
    added: list[str]
    removed: list[str]
    modified: list[str]
    label: str
    kind: str = "git"  # git | snapshot | dirs
    defines: dict[str, str] | None = None  # recorded by a snapshot, else None
    profile: str | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    all_changed_files: list[str] = field(default_factory=list)  # before the PATH restriction
    _cleanups: list = field(default_factory=list, repr=False)

    def cleanup(self) -> None:
        for fn in reversed(self._cleanups):
            try:
                fn()
            except OSError:
                pass
        self._cleanups.clear()

    def __enter__(self) -> "Baseline":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


# --------------------------------------------------------------------------- source selection


def _skill_rel(root: Path) -> str | None:
    try:
        rel = REPO_ROOT.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return rel.as_posix() if rel.parts else None


def _excluded(rel: str, excludes, skill_rel: str | None) -> bool:
    """Mirror of discovery._skip_dir applied to every directory component of `rel`."""
    parts = rel.split("/")
    for i in range(len(parts) - 1):
        name = parts[i]
        reldir = "/".join(parts[: i + 1])
        if name in ALWAYS_EXCLUDED or reldir == ".github/skills" or (skill_rel and reldir == skill_rel):
            return True
        if any(fnmatch(reldir, p) or fnmatch(name, p) for p in excludes):
            return True
    return False


def _glob_re(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[" and "]" in pattern[i + 2:]:
            j = pattern.index("]", i + 2)
            body = pattern[i + 1 : j].replace("\\", "\\\\")
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body + "]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def parse_filelist(text: str, base: Path, root: Path, depth: int = 0) -> tuple[list[Path], list[Path], list[Path]]:
    """Return (source files, include dirs, nested filelists) referenced by a `.f` text."""
    files: list[Path] = []
    incdirs: list[Path] = []
    nested: list[Path] = []

    def res(tok: str) -> Path:
        p = Path(tok)
        if p.is_absolute():
            return p
        return base / p

    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    toks: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"(//|#).*", "", line)
        toks.extend(line.split())
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if t in ("-f", "-F") and i < len(toks):
            nested.append(res(toks[i]))
            i += 1
        elif t == "-I" and i < len(toks):
            incdirs.append(res(toks[i]))
            i += 1
        elif t.startswith("-I") and len(t) > 2:
            incdirs.append(res(t[2:]))
        elif t.startswith("+incdir+"):
            incdirs.extend(res(x) for x in t[8:].split("+") if x)
        elif t in ("-v", "-y") and i < len(toks):
            (incdirs if t == "-y" else files).append(res(toks[i]))
            i += 1
        elif t.startswith(("+", "-")):
            continue
        else:
            files.append(res(t))
    return files, incdirs, nested


class _Tree:
    """A set of project files: the working tree, a directory, or a git tree-ish."""

    root: Path

    def exists(self, rel: str) -> bool: ...
    def read(self, rel: str) -> bytes: ...
    def walk_dir(self, rel: str) -> list[str]: ...
    def rtl_candidates(self, settings) -> tuple[list[str], list[str]]: ...


class _DirTree(_Tree):
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def walk_dir(self, rel: str) -> list[str]:
        base = self.root / rel
        out: list[str] = []
        if not base.is_dir():
            return out
        for dp, dn, fn in os.walk(base):
            dn[:] = sorted(d for d in dn if d not in ALWAYS_EXCLUDED)
            for f in sorted(fn):
                out.append((Path(dp) / f).relative_to(self.root).as_posix())
                if len(out) >= _MAX_INCLUDE_FILES:
                    return out
        return out

    def rtl_candidates(self, settings):
        rtl, flists = discover(
            self.root, excludes=list(settings.exclude), skill_dir=REPO_ROOT, globs=list(settings.sources) or None
        )
        return [p.as_posix() for p in rtl], [p.as_posix() for p in flists]


class _GitTree(_Tree):
    def __init__(self, root: Path, ref: str, files: set[str]) -> None:
        self.root = root
        self.ref = ref
        self.files = files

    def exists(self, rel: str) -> bool:
        return rel in self.files

    def read(self, rel: str) -> bytes:
        cp = _git(self.root, "show", f"{self.ref}:./{rel}")
        if cp is None or cp.returncode != 0:
            raise GatecostError("NO_BASELINE", f"cannot read {rel} at {self.ref[:12]}")
        return cp.stdout

    def walk_dir(self, rel: str) -> list[str]:
        prefix = rel.rstrip("/") + "/" if rel not in ("", ".") else ""
        return sorted(f for f in self.files if f.startswith(prefix) and not _excluded(f, [], None))[:_MAX_INCLUDE_FILES]

    def rtl_candidates(self, settings):
        skill_rel = _skill_rel(self.root)
        rtl: list[str] = []
        flists: list[str] = []
        if settings.sources:
            regs = [_glob_re(g) for g in settings.sources]
            for f in sorted(self.files):
                if Path(f).suffix.lower() in RTL_EXT and any(r.match(f) for r in regs):
                    rtl.append(f)
            return rtl, flists
        for f in sorted(self.files):
            if _excluded(f, settings.exclude, skill_rel):
                continue
            if Path(f).suffix.lower() in RTL_EXT:
                rtl.append(f)
            elif f.endswith(".f"):
                flists.append(f)
        return rtl, flists


def _to_rel(root: Path, spec: str) -> str | None:
    p = Path(spec)
    if not p.is_absolute():
        cand = Path.cwd() / p
        p = cand if cand.exists() else root / p
    try:
        rel = Path(os.path.abspath(p)).relative_to(Path(os.path.abspath(root)))
    except ValueError:
        try:
            rel = p.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return None
    s = rel.as_posix()
    return "" if s == "." else s


def restrict_prefixes(root: Path, paths) -> list[str]:
    out = []
    for p in paths or ():
        rel = _to_rel(root, str(p))
        if rel is None:
            raise GatecostError("BAD_ARGUMENT", f"path is outside the project: {p}")
        out.append(rel)
    return out


def _under(rel: str, prefixes: list[str]) -> bool:
    return any(p == "" or rel == p or rel.startswith(p.rstrip("/") + "/") for p in prefixes)


def select_sources(tree: _Tree, settings, restrict: list[str] | None = None) -> list[str]:
    """Relative posix paths of everything that can influence the analysis: RTL sources,
    filelists, files in include directories, files named by filelists, and the project file."""
    root = Path(settings.root)
    rtl, flists = tree.rtl_candidates(settings)
    if restrict:
        rtl = [f for f in rtl if _under(f, restrict)]
    sel: set[str] = set(rtl) | set(flists)

    inc_dirs: list[str] = []
    for spec in settings.incdirs:
        rel = _to_rel(root, spec)
        if rel is not None:
            inc_dirs.append(rel)

    pending: list[str] = list(flists)
    for spec in settings.filelists:
        rel = _to_rel(root, spec)
        if rel is not None:
            pending.append(rel)
    seen: set[str] = set()
    while pending:
        fl = pending.pop()
        if fl in seen:
            continue
        seen.add(fl)
        if not tree.exists(fl):
            continue
        sel.add(fl)
        try:
            text = tree.read(fl).decode("utf-8", "replace")
        except OSError:
            continue
        base = root / Path(fl).parent
        files, incs, nested = parse_filelist(text, base, root)
        for p in files:
            rel = _to_rel(root, str(p))
            if rel is not None and tree.exists(rel):
                sel.add(rel)
        for p in incs:
            rel = _to_rel(root, str(p))
            if rel is not None:
                inc_dirs.append(rel)
        for p in nested:
            rel = _to_rel(root, str(p))
            if rel is not None:
                pending.append(rel)

    for d in inc_dirs:
        for f in tree.walk_dir(d):
            sel.add(f)
    cfg = _config_rel(settings)
    if cfg and tree.exists(cfg):
        sel.add(cfg)
    return sorted(sel)


def _config_rel(settings) -> str | None:
    path = getattr(settings, "config_path", None)
    root = Path(settings.root)
    if path is None:
        return CONFIG_NAME
    rel = _to_rel(root, str(path))
    return rel


# --------------------------------------------------------------------------- git


def _git(root: Path, *args: str):
    exe = shutil.which("git")
    if exe is None:
        return None
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            [exe, "-c", "core.quotepath=off", *args], cwd=root, capture_output=True, env=env, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git_available() -> bool:
    return shutil.which("git") is not None


def _git_ok(root: Path, *args: str) -> bytes | None:
    cp = _git(root, *args)
    if cp is None or cp.returncode != 0:
        return None
    return cp.stdout


def in_git_repo(root: Path) -> bool:
    out = _git_ok(root, "rev-parse", "--is-inside-work-tree")
    return out is not None and out.strip() == b"true"


def _names(out: bytes | None) -> list[str]:
    if not out:
        return []
    return [n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n]


def resolve_commit(root: Path, ref: str) -> str | None:
    if not ref or ref.startswith("-") or "\0" in ref or "\n" in ref:
        return None
    out = _git_ok(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.decode().strip() if out else None


def default_branch_ref(root: Path) -> str | None:
    """The repository's default branch: origin/HEAD, then main, master (local, then origin)."""
    out = _git_ok(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if out:
        target = out.decode().strip()
        if resolve_commit(root, target):
            return target
    for cand in ("main", "master", "origin/main", "origin/master"):
        if resolve_commit(root, cand):
            return cand
    return None


# --------------------------------------------------------------------------- helpers


def _digest(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=16).digest()


def _make_tempdir(cleanups: list) -> Path:
    d = Path(tempfile.mkdtemp(prefix="gatecost-base-"))

    def rm() -> None:
        def onerr(fn, path, _exc):
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                fn(path)
            except OSError:
                pass

        shutil.rmtree(d, onerror=onerr)

    cleanups.append(rm)
    return d


def _write_ro(dest_root: Path, rel: str, data: bytes) -> None:
    p = dest_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    try:
        p.chmod(0o444)
    except OSError:
        pass


def _filter(files: list[str], restrict: list[str]) -> list[str]:
    return [f for f in files if _under(f, restrict)] if restrict else list(files)


def _compare_trees(before: _Tree, after: _Tree, sel_b: list[str], sel_a: list[str]):
    sb, sa = set(sel_b), set(sel_a)
    added = sorted(sa - sb)
    removed = sorted(sb - sa)
    modified = []
    for f in sorted(sa & sb):
        try:
            if _digest(before.read(f)) != _digest(after.read(f)):
                modified.append(f)
        except OSError:
            modified.append(f)
    return added, removed, modified


def _finish(bl: Baseline, restrict: list[str]) -> Baseline:
    bl.all_changed_files = sorted(set(bl.added) | set(bl.removed) | set(bl.modified))
    bl.changed_files = _filter(bl.all_changed_files, restrict)
    return bl


# --------------------------------------------------------------------------- modes


def _git_baseline(settings, root: Path, ref_name: str, sha: str | None, label: str, restrict: list[str]) -> Baseline:
    """Working tree against commit `sha` (None: the empty tree of a repository without commits)."""
    after = _DirTree(root)
    sel_a = select_sources(after, settings)
    cleanups: list = []
    ignored: set[str] = set()
    ig = _git_ok(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    ignored = set(_names(ig))

    if sha is None:
        tracked: set[str] = set()
        changed: set[str] = set()
        before_tree: _Tree = _GitTree(root, "", set())
    else:
        out = _git_ok(root, "ls-tree", "-r", "-z", "--name-only", sha)
        tracked = set(_names(out))
        # --no-renames: a move shows as one removed and one added path.
        diff = _git_ok(root, "diff", "--name-only", "--no-renames", "-z", sha, "--")
        changed = set(_names(diff))
        before_tree = _GitTree(root, sha, tracked)
    # Ignored untracked files exist at both states: they are never part of the edit.
    keep = {f for f in sel_a if f in ignored and f not in tracked}
    files_b = tracked | keep
    sel_b = select_sources(_ViewTree(before_tree, keep, after), settings)

    tmp = _make_tempdir(cleanups)
    for rel in sel_b:
        if rel in keep or (rel not in changed and (root / rel).is_file()):
            data = after.read(rel)
        else:
            data = before_tree.read(rel)
        _write_ro(tmp, rel, data)
    del files_b

    added, removed, modified = [], [], []
    sb, sa = set(sel_b), set(sel_a)
    added = sorted(sa - sb)
    removed = sorted(sb - sa)
    for f in sorted(sa & sb):
        if f in changed:
            if _digest((tmp / f).read_bytes()) != _digest(after.read(f)):
                modified.append(f)
    bl = Baseline(tmp, root, [], added, removed, modified, label, "git", _cleanups=cleanups)
    return _finish(bl, restrict)


class _ViewTree(_Tree):
    """Git tree plus extra files read from the working tree (ignored, untracked files)."""

    def __init__(self, git_tree: _Tree, extra: set[str], work: _Tree) -> None:
        self.git, self.extra, self.work = git_tree, extra, work
        self.root = git_tree.root

    def exists(self, rel: str) -> bool:
        return rel in self.extra or self.git.exists(rel)

    def read(self, rel: str) -> bytes:
        return self.work.read(rel) if rel in self.extra else self.git.read(rel)

    def walk_dir(self, rel: str) -> list[str]:
        prefix = rel.rstrip("/") + "/" if rel not in ("", ".") else ""
        extra = [f for f in self.extra if f.startswith(prefix)]
        return sorted(set(self.git.walk_dir(rel)) | set(extra))

    def rtl_candidates(self, settings):
        rtl, fl = self.git.rtl_candidates(settings)
        skill_rel = _skill_rel(self.root)
        regs = [_glob_re(g) for g in settings.sources]
        for f in sorted(self.extra):
            if settings.sources:
                if Path(f).suffix.lower() in RTL_EXT and any(r.match(f) for r in regs):
                    rtl.append(f)
            elif not _excluded(f, settings.exclude, skill_rel):
                if Path(f).suffix.lower() in RTL_EXT:
                    rtl.append(f)
                elif f.endswith(".f"):
                    fl.append(f)
        return sorted(set(rtl)), sorted(set(fl))


def _snapshot_baseline(settings, root: Path, which: str, restrict: list[str]) -> Baseline:
    from . import snapshot as snap

    info = snap.find(root, which)
    if info is None:
        what = "no snapshot recorded" if which in ("latest", "") else f"snapshot {which} not found"
        raise GatecostError("NO_BASELINE", f"{what}; run `gatecost snapshot` before editing")
    before = snap.SnapshotTree(info.files_dir, info.files)
    after = _DirTree(root)
    sel_a = select_sources(after, settings, restrict=None)
    added, removed, modified = _compare_trees(before, after, info.files, sel_a)
    bl = Baseline(info.files_dir, root, [], added, removed, modified, f"snapshot {info.id}", "snapshot",
                  defines=dict(info.defines), profile=info.profile)
    return _finish(bl, restrict)


def _dirs_baseline(settings, before_dir: str, after_dir: str, restrict_raw) -> Baseline:
    b, a = Path(before_dir), Path(after_dir)
    for d, flag in ((b, "--before"), (a, "--after")):
        if not d.is_dir():
            raise GatecostError("BAD_ARGUMENT", f"{flag} is not a directory: {d}")
    tb, ta = _DirTree(b.resolve()), _DirTree(a.resolve())
    sel_b = select_sources(tb, _with_root(settings, b.resolve()))
    sel_a = select_sources(ta, _with_root(settings, a.resolve()))
    added, removed, modified = _compare_trees(tb, ta, sel_b, sel_a)
    restrict = [str(Path(p).as_posix()).strip("/") for p in restrict_raw or () if not Path(p).is_absolute()]
    bl = Baseline(b.resolve(), a.resolve(), [], added, removed, modified, f"{before_dir} -> {after_dir}", "dirs")
    return _finish(bl, restrict)


def _with_root(settings, root: Path):
    import copy

    s = copy.copy(settings)
    s.root = root
    s.config_path = None
    return s


def resolve_baseline(settings, args, project_root: Path | None = None) -> Baseline:
    """Pick the baseline by the precedence of DISTRIBUTION.md section 4.1."""
    root = Path(project_root if project_root is not None else settings.root).resolve()
    g = lambda n: getattr(args, n, None)  # noqa: E731
    restrict = restrict_prefixes(root, g("paths"))

    if g("before") or g("after"):
        if not (g("before") and g("after")):
            raise GatecostError("BAD_ARGUMENT", "--before and --after must be given together")
        return _dirs_baseline(settings, g("before"), g("after"), g("paths"))

    if g("since_snapshot") is not None:
        return _snapshot_baseline(settings, root, g("since_snapshot") or "latest", restrict)

    if g("base_ref"):
        ref = g("base_ref")
        if not git_available():
            raise GatecostError("NO_BASELINE", "git is not available; use --since-snapshot or --before/--after")
        if not in_git_repo(root):
            raise GatecostError("NO_BASELINE", "the project is not a git repository")
        if ref == "merge-base":
            default = default_branch_ref(root)
            if default is None:
                raise GatecostError("NO_BASELINE", "cannot determine the default branch (tried origin/HEAD, main, master)")
            out = _git_ok(root, "merge-base", "HEAD", default)
            if not out:
                raise GatecostError("NO_BASELINE", f"no merge base between HEAD and {default}")
            sha = out.decode().strip()
            return _git_baseline(settings, root, ref, sha, f"merge-base with {default} ({sha[:8]})", restrict)
        sha = resolve_commit(root, ref)
        if sha is None:
            raise GatecostError("NO_BASELINE", f"git reference {ref!r} not found")
        return _git_baseline(settings, root, ref, sha, f"{ref} ({sha[:8]})", restrict)

    if git_available() and in_git_repo(root):
        sha = resolve_commit(root, "HEAD")
        if sha is not None:
            return _git_baseline(settings, root, "HEAD", sha, f"HEAD ({sha[:8]})", restrict)
        return _git_baseline(settings, root, "HEAD", None, "empty repository (no commits)", restrict)

    from . import snapshot as snap

    if snap.find(root, "latest") is not None:
        return _snapshot_baseline(settings, root, "latest", restrict)
    why = "git repository not usable" if (root / ".git").exists() else "not a git repository"
    raise GatecostError("NO_BASELINE", f"{why} and no snapshot exists; run `gatecost snapshot` before editing")
