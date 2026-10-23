"""Content-addressed on-disk analysis cache (SPEC.md section 12, DISTRIBUTION.md section 5).

The cache is purely an optimization: entries are JSON (never pickle), each protected by a
checksum, written atomically, and any unreadable entry is treated as a miss.  Dataclasses of
`result.py` and `cost/tree.py` travel through a registry-based typed codec, so decoding can
only ever build the registered classes.  The operations of module costs are kept apart from the
tree entry, in checksummed *packs* (``.gcp``) that trees of later analyses share and that are read
per module on demand (``Cache.put``).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import REPO_ROOT, version
from .paths import state_dir, user_cache_dir

DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_MAGIC = b"GCC1"
_SUFFIX = ".gce"
_PACK_SUFFIX = ".gcp"


# --------------------------------------------------------------------------- locations


def inside_skill_dir(path: Path) -> bool:
    """True when `path` is the skill directory or lies within it (nothing is ever written there)."""
    try:
        Path(os.path.abspath(path)).resolve().relative_to(REPO_ROOT.resolve())
        return True
    except (ValueError, OSError):
        return False


def _writable_dir(d: Path) -> bool:
    """Create `d` if needed and verify that a file can be written in it."""
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".probe-", dir=d)
        os.close(fd)
        os.unlink(name)
        return True
    except OSError:
        return False


def ensure_state_dir(root: Path) -> Path | None:
    """Return `<root>/.gatecost` (created, with its `.gitignore` of `*`) or None if unusable."""
    st = state_dir(root)
    if inside_skill_dir(st):
        return None
    try:
        st.mkdir(parents=True, exist_ok=True)
        gi = st / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
    except OSError:
        return None
    return st if _writable_dir(st) else None


def project_key(root: Path) -> str:
    return hashlib.blake2b(str(Path(root).resolve()).encode("utf-8", "surrogateescape"), digest_size=6).hexdigest()


def user_state_dir(root: Path) -> Path:
    """Per-project state in the user cache (fallback when the project is not writable)."""
    return user_cache_dir() / "projects" / project_key(root)


def writable_state_dir(root: Path) -> tuple[Path | None, bool]:
    """(directory for snapshots, used_fallback). Project first, then the user cache, never the skill dir."""
    st = ensure_state_dir(root)
    if st is not None:
        return st, False
    fb = user_state_dir(root)
    if not inside_skill_dir(fb) and _writable_dir(fb):
        return fb, True
    return None, True


# --------------------------------------------------------------------------- keys


def make_key(*parts: Any) -> str:
    """blake2b over length-prefixed parts (bytes, str, int, or JSON-able values)."""
    h = hashlib.blake2b(digest_size=20)
    for p in parts:
        if isinstance(p, bytes):
            data = b"b" + p
        elif isinstance(p, str):
            data = b"s" + p.encode("utf-8", "surrogateescape")
        else:
            data = b"j" + json.dumps(p, sort_keys=True, default=str).encode("utf-8")
        h.update(len(data).to_bytes(8, "little"))
        h.update(data)
    return h.hexdigest()


def analysis_key(
    files: Iterable[tuple[str, bytes]],
    defines: dict[str, str] | None = None,
    includes: Iterable[tuple[str, bytes]] = (),
    profile: str | bytes = "",
    tool_version: str | None = None,
    extra: Any = None,
) -> str:
    """Key of SPEC section 12: file contents, defines, include contents, profile, tool VERSION."""
    return make_key(
        "files", [[n, hashlib.blake2b(c, digest_size=16).hexdigest()] for n, c in sorted(files)],
        "defines", dict(defines or {}),
        "includes", [[n, hashlib.blake2b(c, digest_size=16).hexdigest()] for n, c in sorted(includes)],
        "profile", profile,
        "version", version() if tool_version is None else tool_version,
        "extra", extra,
    )


# --------------------------------------------------------------------------- the cache


class Cache:
    """Content-addressed store: `<dir>/<key[:2]>/<key>.gce`.  `Cache(None)` is a disabled cache."""

    def __init__(self, directory: Path | str | None, max_bytes: int = DEFAULT_MAX_BYTES, note: str | None = None) -> None:
        self.dir = Path(directory) if directory is not None else None
        self.max_bytes = max_bytes
        self.note = note  # human-readable reason when disabled or degraded
        self.hits = 0
        self.misses = 0
        self._puts = 0
        self._need: list = []
        self._fds: dict = {}

    def close(self) -> None:
        for fd in self._fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def enabled(self) -> bool:
        return self.dir is not None

    @property
    def state(self) -> str:
        """`off`, `cold` (no hit yet) or `warm`: for the `meta.cache` field."""
        if not self.enabled:
            return "off"
        return "warm" if self.hits else "cold"

    def _path(self, key: str) -> Path | None:
        if self.dir is None or not key or not all(c in "0123456789abcdef" for c in key):
            return None
        return self.dir / key[:2] / (key + _SUFFIX)

    # bytes ---------------------------------------------------------------

    def get_bytes(self, key: str, count: bool = True) -> bytes | None:
        p = self._path(key)
        if p is None:
            return None
        try:
            raw = p.read_bytes()
        except OSError:
            self.misses += count
            return None
        payload = self._unwrap(raw)
        if payload is None:
            self.misses += count
            try:
                p.unlink()
            except OSError:
                pass
            return None
        self.hits += count
        try:
            os.utime(p, None)  # LRU: most recently used
        except OSError:
            pass
        return payload

    def put_bytes(self, key: str, data: bytes) -> bool:
        p = self._path(key)
        if p is None:
            return False
        blob = _MAGIC + hashlib.blake2b(data, digest_size=16).digest() + data
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=p.parent)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(blob)
                os.replace(tmp, p)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return False
        self._puts += 1
        if self._puts >= 64:
            self._puts = 0
            self.prune()
        return True

    @staticmethod
    def _unwrap(raw: bytes) -> bytes | None:
        if len(raw) < 20 or raw[:4] != _MAGIC:
            return None
        digest, payload = raw[4:20], raw[20:]
        if hashlib.blake2b(payload, digest_size=16).digest() != digest:
            return None
        return payload

    # typed values --------------------------------------------------------

    def get(self, key: str) -> Any:
        """Decoded value stored by `put`, or None (miss, corrupt, or undecodable entry)."""
        data = self.get_bytes(key)
        if data is None:
            return None
        try:
            self._need = []
            value = decode(json.loads(data.decode("utf-8")), None, self)
            for pk in set(self._need):           # every pack of module operations must still be there (and counts as used)
                if not self._touch(self._pack_path(pk)):
                    raise KeyError("operations pack missing")
            return value
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            self.hits -= 1
            self.misses += 1
            p = self._path(key)
            if p is not None:
                try:
                    p.unlink()
                except OSError:
                    pass
            return None

    def put(self, key: str, value: Any) -> bool:
        """Store ``value`` (JSON).  The operations of module costs go to one pack file per call (``.gcp``): the
        entry itself keeps only a header per module and the location of its operations in a pack, so two
        trees that share modules share the packs and a reader fetches just the modules it looks at."""
        sink = _Sink()
        try:
            doc = encode(value, sink)
            if sink.blobs:
                pack = b"".join(sink.blobs)
                pk = hashlib.blake2b(pack, digest_size=20).hexdigest()
                for ref in sink.pending:
                    ref[0] = pk
                if not self._put_pack(pk, pack):
                    return False
            data = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            return False
        return self.put_bytes(key, data)

    def _pack_path(self, pk: str) -> Path | None:
        if self.dir is None or not pk or not all(c in "0123456789abcdef" for c in pk):
            return None
        return self.dir / pk[:2] / (pk + _PACK_SUFFIX)

    def _put_pack(self, pk: str, data: bytes) -> bool:
        p = self._pack_path(pk)
        if p is None:
            return False
        if self._touch(p):
            return True
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=p.parent)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, p)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return False
        self._puts += 1
        return True

    @staticmethod
    def _touch(p: Path | None) -> bool:
        """Mark a stored file as recently used; False when it does not exist."""
        if p is None:
            return False
        try:
            os.utime(p, None)
        except OSError:
            return False
        return True

    def _read_ops(self, loc: list) -> tuple:
        pk, off, n, bid = loc
        data = None
        try:
            if hasattr(os, "pread"):
                fd = self._fds.get(pk)
                if fd is None:
                    fd = self._fds[pk] = os.open(self._pack_path(pk), os.O_RDONLY)
                data = os.pread(fd, n, off)
            else:                                   # no positional read (Windows): open, seek, read
                with open(self._pack_path(pk), "rb") as fh:
                    fh.seek(off)
                    data = fh.read(n)
        except (OSError, TypeError, ValueError):
            data = None
        if data is None or len(data) != n or hashlib.blake2b(data, digest_size=12).hexdigest() != bid:
            raise RuntimeError(f"cache pack {pk} (module operations) is unreadable or vanished while in use; "
                               "rerun with --no-cache")
        return blob_to_ops(data)

    # maintenance ---------------------------------------------------------

    def _entries(self) -> list[tuple[float, int, Path]]:
        out = []
        if self.dir is None:
            return out
        try:
            for sub in self.dir.iterdir():
                if not sub.is_dir():
                    continue
                for f in sub.iterdir():
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    out.append((st.st_mtime, st.st_size, f))
        except OSError:
            pass
        return out

    def size(self) -> int:
        return sum(s for _, s, _ in self._entries())

    def prune(self, max_bytes: int | None = None) -> int:
        """Delete least recently used entries until the total is within the bound; return bytes freed."""
        limit = self.max_bytes if max_bytes is None else max_bytes
        entries = sorted(self._entries(), key=lambda e: (e[0], e[2].name))
        total = sum(s for _, s, _ in entries)
        freed = 0
        for _, size, f in entries:
            if total <= limit:
                break
            try:
                f.unlink()
            except OSError:
                continue
            total -= size
            freed += size
        return freed


def resolve_cache(settings, cache_dir: str | None = None) -> Cache:
    """Cache location rules: disabled by --no-cache; else `<project>/.gatecost/cache`; else the
    user cache; else disabled. Never inside the skill directory."""
    if getattr(settings, "no_cache", False) or os.environ.get("GATECOST_NO_CACHE") == "1":
        return Cache(None, note="cache disabled")
    candidates: list[Path] = []
    if cache_dir:
        candidates.append(Path(cache_dir))
    root = Path(settings.root)
    st = ensure_state_dir(root)
    if st is not None:
        candidates.append(st / "cache")
    candidates.append(user_cache_dir() / "analysis")
    for c in candidates:
        if not inside_skill_dir(c) and _writable_dir(c):
            return Cache(c)
    return Cache(None, note="no writable cache location; running without cache")


# --------------------------------------------------------------------------- typed codec

_EXTRA: dict[str, type] = {}


def register(cls: type) -> type:
    """Allow `cls` (a dataclass) to be encoded/decoded."""
    _EXTRA[f"{cls.__module__}.{cls.__qualname__}"] = cls
    return cls


_REG: dict[str, type] | None = None
_REG_N = -1
_CLS: dict[type, tuple] = {}          # dataclass -> (registered name, field names)


def _registry() -> dict[str, type]:
    global _REG, _REG_N
    if _REG is not None and _REG_N == len(_EXTRA):
        return _REG
    from . import result
    from .cost import tree

    reg: dict[str, type] = dict(_EXTRA)
    for mod in (result, tree):
        for v in vars(mod).values():
            if isinstance(v, type) and dataclasses.is_dataclass(v) and v.__module__ == mod.__name__:
                reg[f"{v.__module__}.{v.__qualname__}"] = v
    _REG, _REG_N = reg, len(_EXTRA)
    _CLS.clear()
    return reg


def _cls_info(cls: type) -> tuple:
    info = _CLS.get(cls)
    if info is None:
        name = f"{cls.__module__}.{cls.__qualname__}"
        if name not in _registry():
            raise TypeError(f"unregistered dataclass {name}")
        info = _CLS[cls] = (name, tuple(f.name for f in dataclasses.fields(cls)), frozenset(f.name for f in dataclasses.fields(cls)))
    return info


_TAGS = frozenset(("__t", "__mc", "__tuple", "__set", "__fset", "__dict"))   # keys the decoder interprets
_SCALARS = frozenset((str, int, float, bool, type(None)))


class _Sink:
    """Collects the operation blobs of the module costs met while encoding (see :meth:`Cache.put`)."""

    __slots__ = ("blobs", "pending", "by_bid", "off")

    def __init__(self) -> None:
        self.blobs: list = []
        self.pending: list = []
        self.by_bid: dict = {}
        self.off = 0

    def add(self, blob: bytes) -> list:
        bid = hashlib.blake2b(blob, digest_size=12).hexdigest()
        ref = self.by_bid.get(bid)
        if ref is None:
            ref = self.by_bid[bid] = [None, self.off, len(blob), bid]    # pack key patched in once the pack is known
            self.blobs.append(blob)
            self.pending.append(ref)
            self.off += len(blob)
        return ref


def encode(obj: Any, sink: "_Sink | None" = None) -> Any:
    """Turn dataclasses, tuples, sets and non-string-keyed dicts into plain JSON values.

    With a ``sink``, module costs are split: the value keeps a header and the location of the operations,
    which are collected in the sink (see :class:`Cache`)."""
    c = obj.__class__
    if c is str or c is int or c is bool or obj is None or c is float:
        return obj
    if c is tuple:
        for x in obj:
            if x.__class__ not in _SCALARS:
                return {"__tuple": [encode(x, sink) for x in obj]}
        return {"__tuple": list(obj)}
    if c is list:
        for x in obj:
            if x.__class__ not in _SCALARS:
                return [encode(x, sink) for x in obj]
        return list(obj)
    if c is dict:
        flat = True
        for k, v in obj.items():
            if k.__class__ is not str or k in _TAGS:
                return {"__dict": [[encode(k, sink), encode(v, sink)] for k, v in obj.items()]}
            if v.__class__ not in _SCALARS:
                flat = False
        if flat:
            return dict(obj)
        return {k: encode(v, sink) for k, v in obj.items()}
    if isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, tuple) and not hasattr(obj, "_fields"):
        return {"__tuple": [encode(x, sink) for x in obj]}
    if isinstance(obj, list):
        return [encode(x, sink) for x in obj]
    if isinstance(obj, (set, frozenset)):
        items = [encode(x, sink) for x in obj]
        items.sort(key=lambda v: json.dumps(v, sort_keys=True))
        return {"__fset" if isinstance(obj, frozenset) else "__set": items}
    if isinstance(obj, dict):
        if all(isinstance(k, str) and not k.startswith("__") for k in obj):
            return {k: encode(v, sink) for k, v in obj.items()}
        return {"__dict": [[encode(k, sink), encode(v, sink)] for k, v in obj.items()]}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if sink is not None and _is_module_cost(obj):
            return _enc_module(obj, sink)
        name, fnames, _ = _cls_info(c)
        return {"__t": name, "f": {n: encode(getattr(obj, n), sink) for n in fnames}}
    raise TypeError(f"cannot encode {type(obj).__name__}")


def _is_module_cost(obj) -> bool:
    from .cost.tree import ModuleCost

    return isinstance(obj, ModuleCost)


def _enc_module(m, sink: _Sink) -> dict:
    """Header of a module cost (cheap to read) plus the location of its operations."""
    loc = getattr(m, "_loc", None)
    if loc is None or getattr(m, "_load", None) is None:
        loc = sink.add(ops_to_blob(m.ops))
    return {"__mc": [m.key, m.name, encode(m.parameters), m.canonical_hash, encode(m.instances),
                     encode(m.diagnostics), m.file, m.line, list(m.ops_total()), loc]}


# operations travel as positional rows (the dominant part of a cache entry)
_OP_FIELDS = ("id", "op", "category", "quality", "cost", "sigma", "file", "line", "column", "def_file", "def_line",
              "named_key", "shape_key", "declared_widths", "effective_widths", "tags", "breakdown", "arch", "facts",
              "description", "members")


def ops_to_blob(ops) -> bytes:
    from .cost.tree import CostOp

    if tuple(f.name for f in dataclasses.fields(CostOp)) != _OP_FIELDS:
        return json.dumps({"g": encode(tuple(ops))}, separators=(",", ":")).encode("utf-8")
    rows = []
    for o in ops:
        c = o.cost
        rows.append([o.id, o.op, o.category, o.quality, c.min, c.typ, c.max, o.sigma, o.file, o.line, o.column,
                     o.def_file, o.def_line, encode(o.named_key), o.shape_key, list(o.declared_widths),
                     list(o.effective_widths), list(o.tags), encode(o.breakdown), encode(o.arch), encode(o.facts),
                     o.description, list(o.members)])
    return json.dumps(rows, separators=(",", ":")).encode("utf-8")


def blob_to_ops(data: bytes) -> tuple:
    from .cost.tree import CostOp
    from .result import Ge3

    obj = json.loads(data.decode("utf-8"))
    if isinstance(obj, dict):
        return decode(obj["g"])
    out = []
    add = out.append
    for r in obj:
        add(CostOp(r[0], r[1], r[2], r[3], Ge3(r[4], r[5], r[6]), r[7], r[8], r[9], r[10], r[11], r[12],
                   decode(r[13]), r[14], tuple(r[15]), tuple(r[16]), tuple(r[17]), decode(r[18]), decode(r[19]),
                   decode(r[20]), r[21], tuple(r[22])))
    return tuple(out)


def decode(data: Any, _reg: dict[str, type] | None = None, _mods=None) -> Any:
    """Inverse of :func:`encode`; ``_mods`` is the :class:`Cache` that holds the module blobs (if any)."""
    c = data.__class__
    if c is str or c is int or data is None or c is float or c is bool:
        return data
    reg = _registry() if _reg is None else _reg
    if c is list:
        return [decode(x, reg, _mods) for x in data]
    if c is not dict:
        if isinstance(data, list):
            return [decode(x, reg, _mods) for x in data]
        if not isinstance(data, dict):
            return data
    if "__mc" in data:
        return _dec_module(data["__mc"], reg, _mods)
    if "__t" in data:
        cls = reg.get(data["__t"])
        if cls is None:
            raise KeyError(f"unknown type {data['__t']!r}")
        info = _CLS.get(cls)
        names = info[2] if info is not None else {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: decode(v, reg, _mods) for k, v in data["f"].items()}
        if not kwargs.keys() <= names:
            raise KeyError("unexpected fields")
        return cls(**kwargs)
    if "__tuple" in data:
        return tuple(decode(x, reg, _mods) for x in data["__tuple"])
    if "__set" in data:
        return {decode(x, reg, _mods) for x in data["__set"]}
    if "__fset" in data:
        return frozenset(decode(x, reg, _mods) for x in data["__fset"])
    if "__dict" in data:
        return {decode(k, reg, _mods): decode(v, reg, _mods) for k, v in data["__dict"]}
    return {k: decode(v, reg, _mods) for k, v in data.items()}


def _dec_module(h: list, reg, cache):
    from .cost.tree import LazyModuleCost

    if cache is None:
        raise KeyError("module blobs need a cache")
    key, name, params, chash, insts, diags, file, line, sums, loc = h
    if not (isinstance(loc, list) and len(loc) == 4):
        raise KeyError("bad module location")
    cache._need.append(loc[0])
    return LazyModuleCost(key, name, decode(params, reg), chash, None, decode(insts, reg), decode(diags, reg), file,
                          line, _loc=loc, _load=lambda: cache._read_ops(loc), _sums=tuple(sums))


def to_json(obj: Any) -> str:
    return json.dumps(encode(obj), separators=(",", ":"))


def from_json(text: str) -> Any:
    return decode(json.loads(text))


def cost_tree_to_json(tree) -> str:
    """Serialize a `DesignCostTree` (cost/tree.py); the inverse is `cost_tree_from_json`."""
    return to_json(tree)


def cost_tree_from_json(text: str):
    from .cost.tree import DesignCostTree

    obj = from_json(text)
    if not isinstance(obj, DesignCostTree):
        raise ValueError("not a DesignCostTree")
    return obj


def module_cost_to_json(mc) -> str:
    """Serialize a `cost.tree.ModuleCost` (the per-module cache unit)."""
    return to_json(mc)


def module_cost_from_json(text: str):
    from .cost.tree import ModuleCost

    obj = from_json(text)
    if not isinstance(obj, ModuleCost):
        raise ValueError("not a ModuleCost")
    return obj
