"""Very wide nets: lowering scales with the number of runs/chunks, not bits; limits degrade gracefully.

Timing bounds are deliberately generous (the pre-fix code needed minutes to hours on these inputs)."""
import contextlib
import io
import json
import os
import random
import tempfile
import time
import unittest
from pathlib import Path

from gatecost.cli import main
from gatecost.ir import Builder, Const, LowerBlock, SigSpec
from gatecost.ir.lowering import _cutter, _diff_ivals
from gatecost.ir.sigspec import Slice


def _ref_atoms(sig):
    return list(sig.bits())


def _ref_merge_value(lb, w, vals, dv, selects, exclusive, group, full=False):
    """The original bit-by-bit ``LowerBlock._merge_value`` (reference implementation)."""
    b = lb.b
    datoms = _ref_atoms(dv)
    aatoms = [_ref_atoms(v) for v in vals]
    if exclusive and not full and len(aatoms) > 2 and len(aatoms) * w <= 4_000_000:
        who = [None] * w
        for n_, a in enumerate(aatoms):
            for i in range(w):
                if a[i] != datoms[i]:
                    who[i] = (who[i] or ()) + (n_,)
        diff = who
    else:
        diff = [full or any(a[i] != datoms[i] for a in aatoms) for i in range(w)]
    pieces = []
    i = 0
    while i < w:
        j = i
        d = diff[i]
        while j < w and diff[j] == d:
            j += 1
        if not d:
            pieces.append(dv.slice(i, j - i))
        else:
            n = j - i
            dflt = dv.slice(i, n)
            arms = []
            for v, s in zip(vals, selects):
                sl = v.slice(i, n)
                if exclusive and sl == dflt:
                    continue
                arms.append((s, sl))
            if len(arms) == 1:
                pieces.append(b.op("MUX", dflt, arms[0][1], arms[0][0], width=n, group=group, raw=True))
            else:
                op = "PMUX" if exclusive else "PRIORITY"
                pieces.append(b.op(op, dflt, SigSpec.concat([a for _, a in arms]),
                                   SigSpec.concat([s for s, _ in arms]), width=n,
                                   k=len(arms), group=group, raw=True))
        i = j
    return SigSpec.concat(pieces)


def _rand_sig(rng, nets, w):
    """Random SigSpec of width ``w`` made of net slices and constants (with don't-cares)."""
    parts = []
    left = w
    while left:
        n = min(left, rng.choice([1, 1, 2, 3, 5, 8]))
        k = rng.random()
        if k < 0.6:
            net = rng.choice(nets)
            lo = rng.randrange(0, net.width - n + 1) if net.width >= n else 0
            parts.append(Slice(net, lo, min(n, net.width)))
            n = min(n, net.width)
        else:
            parts.append(Const(rng.getrandbits(n), n, rng.getrandbits(n) if rng.random() < 0.3 else 0))
        left -= n
    return SigSpec(parts)


def _mutate(rng, base, nets):
    """A copy of ``base`` with a few random runs replaced."""
    sig = base
    for _ in range(rng.randrange(0, 4)):
        n = rng.randrange(1, max(2, sig.width // 2))
        lo = rng.randrange(0, sig.width - n + 1)
        piece = _rand_sig(rng, nets, n)
        parts = [sig.slice(0, lo), piece, sig.slice(lo + n)]
        sig = SigSpec.concat(parts)
    return sig


class MergeValueEquivalence(unittest.TestCase):
    def test_diff_ivals_and_cutter_match_bitwise(self):
        rng = random.Random(1)
        b = Builder("t")
        nets = [b.net(rng.randrange(1, 40), f"n{i}") for i in range(4)]
        for _ in range(300):
            w = rng.randrange(1, 70)
            a = _rand_sig(rng, nets, w)
            c = _mutate(rng, a, nets) if rng.random() < 0.8 else _rand_sig(rng, nets, w)
            want = [x != y for x, y in zip(a.bits(), c.bits())]
            got = [False] * w
            for lo, hi in _diff_ivals(a, c):
                for i in range(lo, hi):
                    got[i] = True
            self.assertEqual(want, got)
            lo = rng.randrange(0, w)
            n = rng.randrange(1, w - lo + 1)
            self.assertEqual(_cutter(a)(lo, n), a.slice(lo, n))

    def test_merge_value_equals_reference(self):
        rng = random.Random(7)
        for trial in range(250):
            outs = []
            for which in (0, 1):
                r = random.Random(1000 + trial)
                b = Builder("t")
                nets = [b.net(r.randrange(1, 40), f"n{i}") for i in range(4)]
                tgt = b.net(r.randrange(8, 80), "tgt")
                narms = r.randrange(1, 6)
                sels = [b.net(1, f"s{i}").sig for i in range(narms)]
                dv = _rand_sig(r, nets, tgt.width) if r.random() < 0.5 else SigSpec.of(tgt)
                vals = [_mutate(r, dv, nets) for _ in range(narms)]
                exclusive = r.random() < 0.6
                full = r.random() < 0.2
                lb = LowerBlock(b, "comb")
                g = b.new_group()
                if which == 0:
                    res = _ref_merge_value(lb, tgt.width, vals, dv, sels, exclusive, g, full)
                else:
                    res = lb._merge_value(tgt.width, vals, dv, sels, exclusive, g, full)
                outs.append((res.key(), len(b.module.nodes)))
            self.assertEqual(outs[0], outs[1], f"trial {trial}")
        del rng


class GiantLowering(unittest.TestCase):
    def test_wide_net_partial_writes_nested_ifs(self):
        """A 60k-bit net with narrow writes under nested ifs: linear in the number of runs."""
        b = Builder("t", limits=None)
        n = b.net(60_000, "mem")
        cs = [b.net(1, f"c{i}").sig for i in range(6)]
        lb = LowerBlock(b, "comb")
        t0 = time.time()
        for rep in range(40):
            def inner(rep=rep):
                lb.assign(SigSpec.of(n, rep * 1000, 8), SigSpec.const(rep & 255, 8))
                lb.if_(cs[2], lambda: lb.assign(SigSpec.of(n, 59_000 - rep * 7, 4), SigSpec.const(3, 4)),
                       lambda: lb.assign(SigSpec.of(n, 17 + rep * 64, 16), SigSpec.const(1, 16)))
            lb.if_(cs[0], lambda: lb.if_(cs[1], inner, lambda: lb.assign(SigSpec.of(n, 5, 3), SigSpec.const(1, 3))))
        lb.finish()
        self.assertLess(time.time() - t0, 60)

    def test_many_constant_chunks(self):
        t0 = time.time()
        s = SigSpec((Const(1, 1),) * 300_000)
        self.assertEqual(s.width, 300_000)
        self.assertEqual(len(s.chunks), 1)
        t = SigSpec((Const(0, 1), Const(0, 1, 1)) * 100_000)
        self.assertEqual(t.width, 200_000)
        self.assertLess(time.time() - t0, 20)


SRC_STRESS = """
module stress #(parameter int N = 1024, parameter int W = 24) (
  input  logic clk, input logic [$clog2(N)-1:0] a, input logic [$clog2(N)-1:0] b,
  input logic [W-1:0] d, input logic en0, en1, en2, en3,
  output logic [W-1:0] q);
  typedef struct packed { logic v; logic [W-1:0] data; logic [3:0] tag; } ent_t;
  ent_t [N-1:0] tab_q, tab_d;
  always_comb begin
    tab_d = tab_q;
    if (en0) begin
      tab_d[a].data = d;
      if (en1) begin
        tab_d[b].v = 1'b1;
        if (en2) tab_d[3].tag = 4'hf;
        else if (en3) tab_d[N-1].data = ~d;
      end
    end else if (en3) begin
      tab_d[0].v = 1'b0;
      for (int i = 0; i < 8; i++) if (en2) tab_d[i*7].tag = i[3:0];
    end
  end
  always_ff @(posedge clk) tab_q <= tab_d;
  assign q = tab_q[b].data;
endmodule
"""

SRC_PATTERN = """
module pat (input logic clk, input logic [3:0] a, output logic [7:0] q);
  localparam longint N = 64'd1 << 40;
  logic [N-1:0][3:0] big;
  logic [7:0] r;
  assign big = '{default: 4'h5};
  always_ff @(posedge clk) r <= {4'h0, a};
  assign q = r;
endmodule
"""

SRC_UNRESOLVED = """
module giant #(parameter int P) (input logic clk, input logic [3:0] a, input logic en,
                                 input logic [7:0] d, output logic [7:0] q);
  typedef logic [P-1:0][P-1:0][P-1:0][P-1:0][7:0] huge_t;
  huge_t h_q, h_d;
  always_comb begin
    h_d = h_q;
    if (en) h_d[a][1][2][3] = d;
  end
  always_ff @(posedge clk) h_q <= h_d;
  assign q = h_q[a][1][2][3];
endmodule
"""


def _cost(src, top):
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cache:
        f = Path(td) / "d.sv"
        f.write_text(src)
        old = os.environ.get("GATECOST_CACHE_DIR")
        os.environ["GATECOST_CACHE_DIR"] = cache
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                main(["cost", "--top", top, str(f), "--format", "json"])
        finally:
            if old is None:
                os.environ.pop("GATECOST_CACHE_DIR", None)
            else:
                os.environ["GATECOST_CACHE_DIR"] = old
        return json.loads(out.getvalue())


class GiantDesigns(unittest.TestCase):
    def codes(self, d):
        return [x["code"] for x in d.get("diagnostics", [])]

    def test_stress_struct_array_partial_writes(self):
        t0 = time.time()
        d = _cost(SRC_STRESS, "stress")
        el = time.time() - t0
        self.assertIn(d["status"], ("ok", "partial"))
        self.assertNotIn("INTERNAL", self.codes(d))
        self.assertLess(el, 120)

    def test_pattern_wider_than_limit_is_limit_reached(self):
        d = _cost(SRC_PATTERN, "pat")
        self.assertNotIn("INTERNAL", self.codes(d))
        self.assertIn("LIMIT_REACHED", self.codes(d))

    def test_unresolved_param_giant_type(self):
        t0 = time.time()
        d = _cost(SRC_UNRESOLVED, "giant")
        self.assertNotIn("INTERNAL", self.codes(d))
        self.assertIn("LIMIT_REACHED", self.codes(d))
        self.assertLess(time.time() - t0, 60)


if __name__ == "__main__":
    unittest.main()
