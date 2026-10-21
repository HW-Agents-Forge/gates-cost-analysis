"""Float reference implementation of COST_MODEL.md formulas, written from the spec text only.

Used by the exact-value tests (tolerance 0.01 GE). It deliberately shares no code with
``gatecost.cost``. Defaults are the generic-asic primitives and sigma = 1.3 / 1.15,
overhead.max = 1.15 (applied only where no sigma appears in the max column).
"""
INV, BUF, AND2, OR2, XOR2, MUX2, AO22 = 0.67, 1.0, 1.33, 1.33, 2.0, 2.33, 2.0
HA, FA, DLAT, DFF, DFFR, DFFRS, EDFF, ICG, SDFF = 3.33, 5.33, 3.33, 5.67, 6.67, 7.67, 7.33, 5.0, 1.0
SA, SO, OVM, ALPHA = 1.3, 1.15, 1.15, 0.4


def clog2(n):
    return (n - 1).bit_length()


def tri(lo, mid, hi):
    return lo, lo + ALPHA * (mid - lo), hi


def add(n):
    if n == 1:
        return 2.0, 2.0, 2.0 * OVM
    L, G = clog2(n), 2.0
    lo = (n - 1) * FA + HA
    mid = n * HA + (2 * n - 2 - L) * G + (n - 1) * XOR2
    hi = SA * (n * HA + (n * L - n + 1) * G + (n - 1) * XOR2)
    return tri(lo, mid, hi)


def sub(n):
    lo, mid, hi = add(n)[0], None, None
    L, G = clog2(n), 2.0
    mid = n * HA + (2 * n - 2 - L) * G + (n - 1) * XOR2 + 0.33 * n
    hi = SA * (n * HA + (n * L - n + 1) * G + (n - 1) * XOR2) + n * INV
    return tri(lo, mid, hi)


def eq(n):
    return n * XOR2 + (n - 1) * 0.55, n * XOR2 + (n - 1) * 0.75, 3.33 * n * OVM


def lt(n, signed=False):
    x = XOR2 if signed else 0.0
    return 2.7 * n + x, 3.5 * n + x, SA * 5.0 * n + x


def mux(k, w):
    if k == 1:
        return 0.0, 0.0, 0.0
    typ = (k - 1) * w * MUX2
    lo = (k - 1) * w * 1.17 if k >= 3 else typ
    return lo, typ, SO * typ + clog2(k) * (-(-w // 8)) * BUF


def pmux(k, w):
    h = -(-k // 2)
    return (w * (h * AO22 + (h - 1) * 0.75), w * (h * AO22 + (h - 1) * OR2), k * w * MUX2 * OVM)


def decode(s):
    pre = ((1 << (s // 2)) + (1 << (s - s // 2))) * 1.33
    return (1 << s) * 1.0 + pre, (1 << s) * AND2 + pre, (1 << s) * (s - 1) * AND2 * OVM


def shl(n, s):
    L = clog2(n)
    Ls = min(s, L)
    typ = sum((n - (1 << j)) * MUX2 + (1 << j) * AND2 for j in range(Ls))
    if s > L:
        typ += (s - L - 1) * OR2 + n * AND2
    return 0.85 * typ, typ, SO * typ + Ls * (-(-n // 8)) * BUF


def rot(n, s):
    Ls = min(s, clog2(n))
    typ = n * Ls * MUX2
    return 0.85 * typ, typ, SO * typ + Ls * (-(-n // 8)) * BUF


def array_mul(n, m):
    return n * m * AND2 + (n * m - n - m) * FA + n * HA


def booth_tree(n, m):
    R = -(-(m + 1) // 2)
    rows = R * (n + 2)
    return rows * 3.5 + (-(-m // 2)) * 6 + (rows - 2 * (n + m)) * FA


def cpa(w):
    L, G = clog2(w), 2.0
    lo = (w - 1) * FA + HA
    mid = w * HA + (2 * w - 2 - L) * G + (w - 1) * XOR2
    hi = SA * (w * HA + (w * L - w + 1) * G + (w - 1) * XOR2)
    return lo, mid, hi


def mul(n, m, signed=False):
    n, m = max(n, m), min(n, m)
    ex = (n + m) * INV + 2 * HA if signed else 0.0
    if m == 1:
        v = n * AND2 + ex
        return v, v, v * OVM
    lo_c, mid_c, hi_c = cpa(n + m)
    arr = array_mul(n, m)
    bt = booth_tree(n, m)
    lo = min(arr, bt + lo_c)
    typ = min(arr, bt + lo_c + ALPHA * (mid_c - lo_c))
    wal = n * m * AND2 + max(0, n * m - 2 * (n + m)) * FA
    hi = SA * (wal + hi_c / SA)
    return lo + ex, typ + ex, hi + ex


def div(n, m):
    lo = n * (m + 1) * (FA + XOR2)
    typ = 1.2 * n * ((m + 1) * FA + m * MUX2)
    return lo, typ, 2.5 * typ


def reg(n, async_reset=False, sync_reset=False, enable=False, scan=False, style="mux", icg_min=3):
    base = DFFR if async_reset else DFF
    if not enable:
        lo = typ = hi = n * base
    else:
        mux_ = n * (base + MUX2)
        edff = n * (EDFF + base - DFF)
        icg = n * base + ICG
        opts_min = [edff, mux_] + ([icg] if n >= icg_min else [])
        lo = min(opts_min)
        typ = {"mux": mux_, "edff": edff, "icg": min([edff] + ([icg] if n >= icg_min else []))}[style]
        hi = mux_
    if sync_reset:
        lo += 0.67 * n
        typ += AND2 * n
        hi += MUX2 * n
    if scan:
        lo, typ, hi = lo + n * SDFF, typ + n * SDFF, hi + n * SDFF
    return lo, typ, hi * OVM


def mem_flop(D, W, R=1, P=1):
    dec = decode(clog2(D)) if clog2(D) >= 2 else (0, 0, 0)
    typ = D * W * (DFF + MUX2) + R * (D - 1) * W * MUX2 + P * dec[1] + (P - 1) * D * W * MUX2
    lo = D * W * DLAT + D * ICG + R * (D - 1) * W * 1.17 + P * dec[0]
    return lo, typ, SO * typ
