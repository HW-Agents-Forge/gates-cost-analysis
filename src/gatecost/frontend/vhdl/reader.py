"""VHDL source reader: tolerant decoding, line-ending normalization, content hash."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SourceFile:
    path: str
    text: str
    content_hash: str
    error: str | None = None


def decode(raw: bytes) -> str:
    """UTF-8 with a Latin-1 fallback; CRLF and CR become LF. Never raises."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    if text.startswith("﻿"):
        text = text[1:]
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_source(path: str | Path) -> SourceFile:
    """Read a file. On I/O failure the result has empty text and ``error`` set."""
    p = str(path)
    try:
        raw = Path(p).read_bytes()
    except OSError as exc:
        return SourceFile(p, "", content_hash(b""), error=str(exc))
    return SourceFile(p, decode(raw), content_hash(raw))


def source_from_text(text: str, path: str = "<string>") -> SourceFile:
    raw = text.encode("utf-8", "surrogatepass") if False else text.encode("utf-8", "replace")
    return SourceFile(path, decode(raw), content_hash(raw))
