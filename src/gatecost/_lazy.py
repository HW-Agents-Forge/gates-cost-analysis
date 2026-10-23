"""A read-only mapping whose content is computed on first use (digests nobody asked for are never computed)."""
from __future__ import annotations

from collections.abc import Mapping


class LazyMap(Mapping):
    __slots__ = ("_thunk", "_data")

    def __init__(self, thunk) -> None:
        self._thunk = thunk
        self._data = None

    def _d(self) -> dict:
        d = self._data
        if d is None:
            d = self._data = dict(self._thunk())
            self._thunk = None
        return d

    def __getitem__(self, k):
        return self._d()[k]

    def __iter__(self):
        return iter(self._d())

    def __len__(self) -> int:
        return len(self._d())

    def __contains__(self, k) -> bool:
        return k in self._d()

    def __repr__(self) -> str:
        return repr(self._d())
