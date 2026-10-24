"""Reader (FRONTEND.md section 2.1): bytes to normalized text plus a content hash.

Never raises on bad bytes: UTF-8 is tried first, Latin-1 is the fallback (it cannot fail).
Line endings are normalized to ``\\n``; a UTF-8 byte-order mark is dropped.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


def content_hash(data: bytes) -> str:
    """SHA-256 hex digest of raw file bytes (the cache key of SPEC.md section 12)."""
    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True, frozen=True)
class SourceFile:
    path: str          # as displayed in diagnostics / locations
    text: str          # normalized text
    sha256: str        # hash of the raw bytes
    encoding: str      # "utf-8" or "latin-1"


def _decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    elif data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            data = data.decode("utf-16").encode("utf-8")      # UTF-16 with a byte-order mark
        except UnicodeDecodeError:
            pass
    try:
        text = data.decode("utf-8")
        enc = "utf-8"
    except UnicodeDecodeError:
        text = data.decode("latin-1")
        enc = "latin-1"
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, enc


def source_from_text(text: str, path: str = "<string>") -> SourceFile:
    data = text.encode("utf-8", "surrogatepass")
    norm = text.replace("\r\n", "\n").replace("\r", "\n") if "\r" in text else text
    return SourceFile(path, norm, content_hash(data), "utf-8")


def read_source(path: str | os.PathLike, display: str | None = None) -> SourceFile:
    """Read a file. Raises only ``OSError`` (missing or unreadable file)."""
    with open(path, "rb") as fh:
        data = fh.read()
    text, enc = _decode(data)
    return SourceFile(display if display is not None else os.fspath(path), text, content_hash(data), enc)


def resolve_include(name: str, including_dir: str | None, incdirs: tuple[str, ...] | list[str]) -> str | None:
    """Resolve an include: the including file's directory first, then the include directories.

    Only these locations are searched (FRONTEND.md section 1, rule 6); absolute names are
    accepted as given.
    """
    if os.path.isabs(name):
        return name if os.path.isfile(name) else None
    cands = []
    if including_dir is not None:
        cands.append(including_dir)
    cands.extend(incdirs)
    for d in cands:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None
