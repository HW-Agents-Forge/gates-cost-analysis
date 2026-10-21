"""Forward known-bits propagation (IR.md section 6.1)."""
from __future__ import annotations

from .core import ModuleIR, Node
from .evalops import eval_const
from .graph import topo_order
from .sigspec import Const, SigSpec, mask


def sig_known(sig: SigSpec) -> tuple[int, int]:
    """(kmask, kval) of a SigSpec from constants and the known facts of its nets."""
    km = kv = pos = 0
    for c in sig.chunks:
        if isinstance(c, Const):
            km |= c.kmask << pos
            kv |= c.value << pos
        else:
            n = c.net
            mm = (1 << c.width) - 1
            km |= ((n.kmask >> c.lo) & mm) << pos
            kv |= ((n.kval >> c.lo) & mm) << pos
        pos += c.width
    return km, kv & km


def _tz(v: int, w: int) -> int:
    """Number of trailing set bits of ``v`` (limited to w)."""
    n = (~v & (v + 1)).bit_length() - 1
    return min(n, w)


def _ub(km: int, kv: int, w: int) -> int:
    """Upper bound on the bit length of the value: bits above are known zero."""
    return (~(km & ~kv) & mask(w)).bit_length()


def _pattern(j: int, width: int) -> int:
    """Mask over output indices 0..width-1 of those whose bit ``j`` is 1."""
    p = 1 << (j + 1)
    half = 1 << j
    block = ((1 << half) - 1) << half
    reps = (width + p - 1) // p
    full = block * (((1 << (reps * p)) - 1) // ((1 << p) - 1))
    return full & mask(width)


_FOLDABLE_OPS = frozenset({
    "ADD", "SUB", "MUL", "NEG", "AND", "OR", "XOR", "XNOR", "NOT", "EQ", "NE", "LT", "LE", "GT",
    "GE", "MUX", "RED_AND", "RED_OR", "RED_XOR", "SHL", "SHR", "SAR", "ROL", "ROR", "DIV", "MOD",
    "POW", "DECODE", "ENCODE", "ROM"})


def node_known(nd: Node) -> list[tuple[int, int]]:
    """Known (kmask, kval) for each output of ``nd`` given the facts of its input nets."""
    op = nd.op
    if not nd.outputs:
        return []
    w = nd.outputs[0].width
    m = mask(w)
    if op in ("LATCH", "MEM", "INST", "BLACKBOX"):
        return [(0, 0)] * len(nd.outputs)
    ins = {k: s for k, s in nd.inputs}
    kn = {k: sig_known(s) for k, s in ins.items()}

    def full(k):
        return kn[k][0] == mask(ins[k].width)

    if op == "DFF":
        return [_dff_known(nd, ins, kn, w)]
    # fully known inputs: evaluate exactly (the builder usually folded already)
    if op in _FOLDABLE_OPS and ins and all(full(k) for k in ins):
        try:
            cs = [Const(kn[k][1], ins[k].width) for k in ("A", "B", "S") if k in ins]
            c = eval_const(op, cs, nd.attrs, w)
            return [(c.kmask, c.value)]
        except Exception:
            pass
    ak, av = kn.get("A", (0, 0))
    bk, bv = kn.get("B", (0, 0))
    km = kv = 0
    if op == "AND":
        zero = (ak & ~av) | (bk & ~bv)
        one = ak & bk & av & bv
        km, kv = (zero | one) & m, one & m
    elif op == "OR":
        one = (ak & av) | (bk & bv)
        zero = ak & bk & ~av & ~bv
        km, kv = (zero | one) & m, one & m
    elif op in ("XOR", "XNOR"):
        km = ak & bk & m
        kv = (av ^ bv) & km
        if op == "XNOR":
            kv = ~kv & km
    elif op == "NOT":
        km, kv = ak & m, ~av & ak & m
    elif op in ("ADD", "SUB"):
        r = _tz(ak & bk, w)
        if r:
            km = mask(r)
            kv = ((av + bv) if op == "ADD" else (av - bv)) & mask(r)
        # trailing known zeros of both operands: result low bits known zero too
        z = min(_tz(ak & ~av, w), _tz(bk & ~bv, w))
        if z > r:
            km |= mask(z)
            kv &= mask(r)
        if op == "ADD":
            ub = max(_ub(ak, av, w), _ub(bk, bv, w)) + 1
            if ub < w:
                km |= m & ~mask(ub)
    elif op == "NEG":
        r = _tz(ak, w)
        if r:
            km = mask(r)
            kv = (-av) & mask(r)
        z = _tz(ak & ~av, w)
        if z > r:
            km |= mask(z)
    elif op == "MUL":
        r = min(_tz(ak, w), _tz(bk, w))
        if r:
            km = mask(r)
            kv = (av * bv) & mask(r)
        z = _tz(ak & ~av, w) + _tz(bk & ~bv, w)
        z = min(z, w)
        if z > r:
            km |= mask(z)
            kv &= mask(r)
        ub = _ub(ak, av, w) + _ub(bk, bv, w)
        if ub < w:
            km |= m & ~mask(ub)
    elif op == "SHL":
        z = _tz(ak & ~av, w)
        km = mask(z)
    elif op in ("SHR",):
        ub = _ub(ak, av, w)
        km = m & ~mask(ub)
    elif op == "SAR":
        if (ak >> (w - 1)) & 1 and not (av >> (w - 1)) & 1:
            km = m & ~mask(_ub(ak, av, w))
    elif op in ("ROL", "ROR"):
        if ak == m and av in (0, m):
            km, kv = m, av
    elif op == "DIV":
        sg = nd.attrs.get("sa") and nd.attrs.get("sb")
        if not sg:
            ub = min(_ub(ak, av, ins["A"].width), w)
            km = m & ~mask(ub)
    elif op == "MOD":
        sg = nd.attrs.get("sa") and nd.attrs.get("sb")
        if not sg:
            ub = min(_ub(ak, av, ins["A"].width), _ub(bk, bv, ins["B"].width), w)
            km = m & ~mask(ub)
    elif op in ("EQ", "NE"):
        cw = max(ins["A"].width, ins["B"].width)
        both = ak & bk
        if (av ^ bv) & both:
            km, kv = 1, 1 if op == "NE" else 0
    elif op in ("LT", "LE", "GT", "GE"):
        pass
    elif op in ("RED_AND",):
        n = ins["A"].width
        if ak & ~av:
            km, kv = 1, 0
        elif ak == mask(n):
            km, kv = 1, 1
    elif op == "RED_OR":
        n = ins["A"].width
        if ak & av:
            km, kv = 1, 1
        elif ak == mask(n):
            km, kv = 1, 0
    elif op == "RED_XOR":
        pass
    elif op == "MUX":
        sk, sv = kn["S"]
        if sk & 1:
            km, kv = (bk, bv) if sv & 1 else (ak, av)
        else:
            km = ak & bk & ~(av ^ bv) & m
            kv = av & km
    elif op in ("PMUX", "PRIORITY"):
        k = nd.attrs["k"]
        sk, sv = kn["S"]
        Bsig = ins["B"]
        cands = []
        dflt_possible = True
        for i in range(k):
            if (sk >> i) & 1:
                if not (sv >> i) & 1:
                    continue
                cands.append(i)
                dflt_possible = False
                if op == "PRIORITY":
                    break
                continue
            cands.append(i)
        bkk, bvv = sig_known(Bsig)
        arms = [((bkk >> (i * w)) & m, (bvv >> (i * w)) & m) for i in cands]
        if dflt_possible:
            arms.append((ak, av))
        if arms:
            ref_k, ref_v = arms[0]
            km = ref_k
            for (xk, xv) in arms[1:]:
                km &= xk & ~(xv ^ ref_v)
            kv = ref_v & km
    elif op == "DECODE":
        if w <= (1 << 16):
            km = 0
            n = ins["A"].width
            for j in range(n):
                if (ak >> j) & 1:
                    pat = _pattern(j, w)
                    zero = (~pat & m) if (av >> j) & 1 else pat
                    km |= zero
            kv = 0
    elif op == "ROM":
        contents = nd.attrs["contents"]
        if contents:
            same1 = mask(w)
            same0 = mask(w)
            for c in contents:
                same1 &= c
                same0 &= ~c
            km = (same1 | same0) & m
            kv = same1 & m
            if nd.attrs.get("depth", len(contents)) < (1 << ins["A"].width):
                pass
    elif op == "SUM":
        pass
    return [(km & m, kv & km & m)]


def _dff_known(nd: Node, ins, kn, w: int) -> tuple[int, int]:
    dk, dv = kn["D"]
    a = nd.attrs
    vals = []
    kind = a.get("reset_kind", "none")
    if kind in ("async", "both"):
        vals.append(a["reset_value"])
    if kind == "sync":
        vals.append(a["reset_value"])
    if kind == "both":
        vals.append(a["srst_value"])
    if not vals:
        init = a.get("init")
        if init is None:
            return (0, 0)
        vals.append(Const(init, w))
    km = mask(w)
    ref = vals[0]
    km &= ref.kmask
    for v in vals[1:]:
        km &= v.kmask & ~(v.value ^ ref.value)
    km &= dk & ~(dv ^ ref.value)
    return (km, ref.value & km)


def known_bits(m: ModuleIR, order=None, rounds: int = 4) -> None:
    """Fill ``Net.kmask/kval`` for every net (forward pass, IR.md section 6.1)."""
    for n in m.nets:
        c = n.attrs.get("const")
        if c is not None and n.port == "in":
            n.kmask, n.kval = c.kmask, c.value
        else:
            n.kmask = n.kval = 0
    if order is None:
        order, _ = topo_order(m)
    for _ in range(rounds):
        changed = False
        for it in order:
            if isinstance(it, Node):
                for o, (km, kv) in zip(it.outputs, node_known(it)):
                    if km != o.kmask or kv != o.kval:
                        o.kmask, o.kval = km, kv
                        changed = True
            else:
                km, kv = sig_known(it.src)
                net = it.net
                mm = mask(it.src.width) << it.lo
                km2 = (net.kmask & ~mm) | (km << it.lo)
                kv2 = (net.kval & ~mm) | (kv << it.lo)
                if km2 != net.kmask or kv2 != net.kval:
                    net.kmask, net.kval = km2, kv2
                    changed = True
        if not changed:
            break
