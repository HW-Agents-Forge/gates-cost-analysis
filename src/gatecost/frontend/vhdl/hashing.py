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


def _feed(h, root) -> None:
    stack = [root]
    pop = stack.pop
    push = stack.append
    update = h.update
    while stack:
        x = pop()
        if x.__class__ is bytes:
            update(x)
        elif isinstance(x, Node):
            cls = type(x)
            if cls.__name__ == "Skipped":
                update(b"(Skipped:" + str(x.kind).encode() + b")")
                continue
            tags = _TAGS.get(cls)
            if tags is None:
                tags = _TAGS[cls] = (b"(" + cls.__name__.encode(),
                                     tuple((f, b":" + f.encode() + b"=", b":" + f.encode() + b"{")
                                           for f in reversed(cls._fields)))
            update(tags[0])
            push(b")")
            unordered = cls._unordered
            for f, eq, br in tags[1]:
                v = getattr(x, f, None)
                if unordered and f in unordered and isinstance(v, list):
                    ds = sorted(_digest(i) if isinstance(i, Node) else repr(i).encode() for i in v)
                    push(br + b"".join(ds) + b"}")
                else:
                    push(v)
                    push(eq)
        elif isinstance(x, (list, tuple)):
            update(b"[")
            push(b"]")
            for i in reversed(x):
                push(i)
        else:
            update(b"<" + repr(x).encode() + b">")


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
