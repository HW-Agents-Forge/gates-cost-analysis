"""Normalized AST hashes of design units.

The hash covers the semantic fields of a node only: not locations, not comments or whitespace
(which never reach the AST), not skipped-region contents.  Lists named in a node's ``_unordered``
(independent declarations and concurrent statements) are hashed as multisets, so reordering them
does not change the hash.  The walk is iterative: long left-leaning operator chains are safe.
"""
from __future__ import annotations

import hashlib
import sys

from .nodes import Node, walk


_TAGS: dict = {}


def _digest(node) -> bytes:
    h = hashlib.sha256()
    _feed(h, node)
    return h.digest()


_SCALAR = frozenset({type(None), str, int, bool, float})


def _feed(h, root) -> None:
    """Stream the canonical serialization of ``root`` into ``h``.

    Stack items: ``str`` = literal text, anything else = still to be serialized.  Scalars are
    rendered inline, so only child nodes and sequences go through the stack.
    """
    stack = [root]
    pop = stack.pop
    update = h.update
    scalar = _SCALAR
    out: list[str] = []           # pending text, encoded in one go
    emit = out.append
    while stack:
        x = pop()
        c = x.__class__
        if c is str:
            emit(x)
        elif c is bytes:
            if out:
                update("".join(out).encode())
                out.clear()
            update(x)
        elif isinstance(x, Node):
            cls = c
            tags = _TAGS.get(cls)
            if tags is None:
                name = cls.__name__
                tags = _TAGS[cls] = ("(" + name, tuple((f, ":" + f + "=", ":" + f + "{") for f in cls._fields),
                                     name == "Skipped")
            if tags[2]:
                emit("(Skipped:" + str(x.kind) + ")")
                continue
            unordered = cls._unordered
            seq: list = []
            buf = tags[0]
            for f, eq, br in tags[1]:
                v = getattr(x, f, None)
                if unordered and f in unordered and isinstance(v, list):
                    ds = sorted(_digest(i) if isinstance(i, Node) else repr(i).encode() for i in v)
                    seq.append(buf + br)
                    seq.append(b"".join(ds) + b"}")
                    buf = ""
                    continue
                buf += eq
                if v.__class__ in scalar:
                    buf += "<" + repr(v) + ">"
                else:
                    seq.append(buf)
                    seq.append(v)
                    buf = ""
            seq.append(buf + ")")
            seq.reverse()
            stack.extend(seq)
        elif isinstance(x, (list, tuple)):
            seq = []
            buf = "["
            for i in x:
                if i.__class__ in scalar:
                    buf += "<" + repr(i) + ">"
                else:
                    seq.append(buf)
                    seq.append(i)
                    buf = ""
            seq.append(buf + "]")
            seq.reverse()
            stack.extend(seq)
        else:
            emit("<" + repr(x) + ">")
    if out:
        update("".join(out).encode())


def node_hash(node) -> str:
    """Hex digest of the normalized AST of ``node`` (a design unit or any node)."""
    old = sys.getrecursionlimit()
    if old < 30000:
        sys.setrecursionlimit(30000)
    try:
        h = hashlib.sha256()
        _feed(h, node)
        return h.hexdigest()
    except RecursionError:  # absurdly nested declarations: fall back to a flat, order-sensitive walk
        h = hashlib.sha256(b"flat")
        for n in walk(node):
            h.update(type(n).__name__.encode())
            for f in n._fields:
                v = getattr(n, f, None)
                if not isinstance(v, (Node, list, tuple)):
                    h.update(repr(v).encode())
        return h.hexdigest()
    finally:
        if old < 30000:
            sys.setrecursionlimit(old)
