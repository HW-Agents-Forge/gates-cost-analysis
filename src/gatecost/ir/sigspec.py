"""Constants, net slices and signal specifications (IR.md section 2).

A ``SigSpec`` is an ordered tuple of chunks, least significant first.  A chunk is
either a :class:`Slice` ``(net, lo, width)`` of a net or a :class:`Const`
``(value, width, xmask)``.  Wiring (slice, concat, replicate, extension) is pure
``SigSpec`` construction and never creates nodes.
"""
from __future__ import annotations

from typing import Iterable, Iterator

_CONST_CACHE: dict[tuple[int, int, int], "Const"] = {}
_CACHE_LIMIT = 200_000


#: no legitimate value is anywhere near this wide (nets are limited to 65 536 bits); a larger request
#: comes from a runaway generic / array bound and would allocate gigabytes as one Python integer
MAX_CONST_WIDTH = 1 << 24


def mask(width: int) -> int:
    """All-ones mask of ``width`` bits."""
    if width > MAX_CONST_WIDTH:
        raise ValueError("value of %d bits is too wide" % width)
    return (1 << width) - 1 if width > 0 else 0


class Const:
    """Interned constant.  ``xmask`` bit i set means bit i is don't-care (x/z/-/?).

    ``value`` bits under ``xmask`` are always stored as 0 (canonical form).
    """

    __slots__ = ("value", "width", "xmask")

    def __new__(cls, value: int, width: int, xmask: int = 0) -> "Const":
        if width < 0:
            raise ValueError("negative constant width")
        if width > MAX_CONST_WIDTH:
            raise ValueError("constant of %d bits is too wide" % width)
        m = (1 << width) - 1
        xmask &= m
        value = value & m & ~xmask
        key = (value, width, xmask)
        c = _CONST_CACHE.get(key)
        if c is None:
            if len(_CONST_CACHE) >= _CACHE_LIMIT:
                _CONST_CACHE.clear()
            c = object.__new__(cls)
            c.value = value
            c.width = width
            c.xmask = xmask
            _CONST_CACHE[key] = c
        return c

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Const)
            and self.value == other.value
            and self.width == other.width
            and self.xmask == other.xmask
        )

    def __hash__(self) -> int:
        return hash((self.value, self.width, self.xmask))

    def __repr__(self) -> str:
        return f"Const({self.value:#x}, {self.width}, xmask={self.xmask:#x})"

    def __reduce__(self):  # pickling/copy
        return (Const, (self.value, self.width, self.xmask))

    @property
    def kmask(self) -> int:
        """Known-bit mask (bits that are not don't-care)."""
        return mask(self.width) & ~self.xmask

    @property
    def has_x(self) -> bool:
        return self.xmask != 0

    def key(self) -> tuple:
        return ("c", self.value, self.width, self.xmask)

    def as_signed(self) -> int:
        v = self.value
        if self.width and (v >> (self.width - 1)) & 1:
            v -= 1 << self.width
        return v


class Slice:
    """A slice ``net[lo +: width]`` of a net."""

    __slots__ = ("net", "lo", "width")

    def __init__(self, net, lo: int, width: int) -> None:
        self.net = net
        self.lo = lo
        self.width = width

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Slice)
            and self.net is other.net
            and self.lo == other.lo
            and self.width == other.width
        )

    def __hash__(self) -> int:
        return hash((self.net.id, self.lo, self.width))

    def __repr__(self) -> str:
        return f"Slice({self.net.name or self.net.id}[{self.lo}+:{self.width}])"

    def key(self) -> tuple:
        return ("n", self.net.id, self.lo, self.width)


def _normalize(chunks: Iterable) -> tuple:
    out: list = []
    for ch in chunks:
        w = ch.width
        if w == 0:
            continue
        if out:
            last = out[-1]
            if isinstance(ch, Const) and isinstance(last, Const):
                out[-1] = Const(
                    last.value | (ch.value << last.width),
                    last.width + w,
                    last.xmask | (ch.xmask << last.width),
                )
                continue
            if (
                isinstance(ch, Slice)
                and isinstance(last, Slice)
                and ch.net is last.net
                and ch.lo == last.lo + last.width
            ):
                out[-1] = Slice(last.net, last.lo, last.width + w)
                continue
        out.append(ch)
    return tuple(out)


class SigSpec:
    """Immutable ordered list of chunks, least significant first."""

    __slots__ = ("chunks", "width", "_key")

    def __init__(self, chunks: Iterable = ()) -> None:
        if chunks.__class__ is tuple and len(chunks) == 1 and chunks[0].width > 0:
            cs = chunks                       # a single non-empty chunk is already normal
            self.width = chunks[0].width
        else:
            cs = _normalize(chunks)
            self.width = sum([c.width for c in cs])
        self.chunks = cs
        self._key = None

    # -- construction -----------------------------------------------------
    @staticmethod
    def of(x, lo: int = 0, width: int | None = None) -> "SigSpec":
        """SigSpec of a net (or part of it), or pass through a SigSpec/Const."""
        if isinstance(x, SigSpec):
            return x if (lo == 0 and width is None) else x.slice(lo, width)
        if isinstance(x, Const):
            return SigSpec((x,))
        if isinstance(x, Slice):
            return SigSpec((x,))
        w = x.width - lo if width is None else width
        if lo < 0 or w < 0 or lo + w > x.width:
            raise ValueError(f"slice [{lo}+:{w}] out of range of net width {x.width}")
        return SigSpec((Slice(x, lo, w),))

    @staticmethod
    def const(value: int, width: int, xmask: int = 0) -> "SigSpec":
        return SigSpec((Const(value, width, xmask),))

    @staticmethod
    def x(width: int) -> "SigSpec":
        return SigSpec((Const(0, width, mask(width)),))

    @staticmethod
    def concat(parts: Iterable) -> "SigSpec":
        """Concatenate, LEAST significant part first."""
        cs: list = []
        for p in parts:
            cs.extend(SigSpec.of(p).chunks)
        return SigSpec(cs)

    # -- identity -----------------------------------------------------------
    def key(self) -> tuple:
        k = self._key
        if k is None:
            parts = []
            for c in self.chunks:
                if c.__class__ is Slice:
                    parts.append(("n", c.net.id, c.lo, c.width))
                else:
                    parts.append(("c", c.value, c.width, c.xmask))
            k = self._key = tuple(parts)
        return k

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SigSpec) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def __len__(self) -> int:
        return self.width

    def __repr__(self) -> str:
        return "SigSpec(" + ", ".join(repr(c) for c in self.chunks) + ")"

    # -- properties ---------------------------------------------------------
    @property
    def is_const(self) -> bool:
        return all(isinstance(c, Const) for c in self.chunks)

    def const_value(self) -> Const | None:
        """The merged Const when every chunk is constant, else None."""
        if not self.chunks:
            return Const(0, 0)
        if len(self.chunks) == 1 and isinstance(self.chunks[0], Const):
            return self.chunks[0]
        return None

    def nets(self) -> Iterator:
        for c in self.chunks:
            if isinstance(c, Slice):
                yield c.net

    def single_slice(self) -> Slice | None:
        if len(self.chunks) == 1 and isinstance(self.chunks[0], Slice):
            return self.chunks[0]
        return None

    # -- wiring -------------------------------------------------------------
    def slice(self, lo: int, width: int | None = None) -> "SigSpec":
        w = self.width - lo if width is None else width
        if lo < 0 or w < 0 or lo + w > self.width:
            raise ValueError(f"slice [{lo}+:{w}] out of range of width {self.width}")
        if lo == 0 and w == self.width:
            return self
        out: list = []
        pos = 0
        for c in self.chunks:
            cw = c.width
            if pos + cw <= lo:
                pos += cw
                continue
            if pos >= lo + w:
                break
            a = max(lo, pos) - pos
            b = min(lo + w, pos + cw) - pos
            if isinstance(c, Const):
                out.append(Const(c.value >> a, b - a, c.xmask >> a))
            else:
                out.append(Slice(c.net, c.lo + a, b - a))
            pos += cw
        return SigSpec(out)

    def split(self, n: int) -> list:
        """``[self.slice(i * n, n) for i in range(self.width // n)]`` in one pass over the chunks."""
        if n <= 0:
            raise ValueError("split width must be positive")
        total = self.width
        if total % n:
            raise ValueError(f"width {total} is not a multiple of {n}")
        if n == total:
            return [self]
        out: list = []
        cur: list = []
        need = n
        for c in self.chunks:
            cw = c.width
            off = 0
            is_const = isinstance(c, Const)
            while off < cw:
                take = cw - off if cw - off < need else need
                if off == 0 and take == cw:
                    cur.append(c)
                elif is_const:
                    cur.append(Const(c.value >> off, take, c.xmask >> off))
                else:
                    cur.append(Slice(c.net, c.lo + off, take))
                off += take
                need -= take
                if need == 0:
                    out.append(SigSpec(cur))
                    cur = []
                    need = n
        return out

    def __getitem__(self, idx) -> "SigSpec":
        """``sig[i]`` one bit; ``sig[lo:hi]`` Python half-open, LSB indexed."""
        if isinstance(idx, slice):
            lo = 0 if idx.start is None else idx.start
            hi = self.width if idx.stop is None else idx.stop
            return self.slice(lo, hi - lo)
        if idx < 0:
            idx += self.width
        return self.slice(idx, 1)

    def bit(self, i: int) -> "SigSpec":
        return self.slice(i, 1)

    def cat(self, *higher) -> "SigSpec":
        """``self`` is the lowest part; the arguments are placed above it."""
        return SigSpec.concat((self,) + higher)

    def repeat(self, n: int) -> "SigSpec":
        return SigSpec(self.chunks * n) if n > 0 else SigSpec(())

    def zext(self, width: int) -> "SigSpec":
        if width <= self.width:
            return self.slice(0, width)
        return SigSpec(self.chunks + (Const(0, width - self.width),))

    def sext(self, width: int) -> "SigSpec":
        if width <= self.width:
            return self.slice(0, width)
        if self.width == 0:
            return SigSpec((Const(0, width),))
        msb = self.slice(self.width - 1, 1)
        return SigSpec(self.chunks + msb.repeat(width - self.width).chunks)

    def trunc(self, width: int) -> "SigSpec":
        return self.slice(0, width)

    def resize(self, width: int, signed: bool = False) -> "SigSpec":
        if width == self.width:
            return self
        if width < self.width:
            return self.slice(0, width)
        return self.sext(width) if signed else self.zext(width)

    # -- per-bit view ---------------------------------------------------------
    def bits(self) -> Iterator[tuple]:
        """Yield one atom per bit: ``(net, index)`` or ``(None, 0|1|2)`` (2 = x)."""
        for c in self.chunks:
            if isinstance(c, Const):
                for i in range(c.width):
                    if (c.xmask >> i) & 1:
                        yield (None, 2)
                    else:
                        yield (None, (c.value >> i) & 1)
            else:
                for i in range(c.width):
                    yield (c.net, c.lo + i)

    @property
    def sign_rep(self) -> int:
        """Number of upper bits that are copies of the bit directly below them.

        A sign-extension by ``k`` bits has ``sign_rep >= k``.  Used by the
        demanded-bits and effective-width passes to avoid costing extension bits.
        A bit pair counts as a copy when both are the same net bit or the same
        known constant.  Don't-care bits never count as copies.
        """
        return top_rep(self)


def _rev_atoms(sig: SigSpec) -> Iterator[tuple]:
    """Bit atoms from the most significant bit downward."""
    for c in reversed(sig.chunks):
        if isinstance(c, Const):
            for i in range(c.width - 1, -1, -1):
                if (c.xmask >> i) & 1:
                    yield (None, 2)
                else:
                    yield (None, (c.value >> i) & 1)
        else:
            for i in range(c.width - 1, -1, -1):
                yield (c.net, c.lo + i)


def top_rep(sig: SigSpec) -> int:
    """Count of top bits equal to the bit directly below them."""
    chunks = sig.chunks
    if len(chunks) == 1 and chunks[0].__class__ is Slice:
        return 0           # consecutive bits of one slice are never equal atoms
    count = 0
    prev = None
    it = _rev_atoms(sig)
    first = True
    for atom in it:
        if first:
            prev = atom
            first = False
            continue
        # compare bit (k) with bit (k-1): both walking down from the top
        if atom == prev and atom[1] != 2:
            count += 1
            continue
        break
    return count


def known_of(sig: SigSpec) -> tuple[int, int]:
    """Known mask/value contributed by Const chunks only (no net facts)."""
    km = kv = pos = 0
    for c in sig.chunks:
        if isinstance(c, Const):
            km |= c.kmask << pos
            kv |= c.value << pos
        pos += c.width
    return km, kv
