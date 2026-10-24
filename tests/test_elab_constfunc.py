"""Constant-argument function calls: the pure-integer fast path (``gatecost.elab.constfunc``) against the general
statement-lowering path (``limits["const_func_fast"] = 0``)."""
import os
import unittest

from gatecost.elab import constfunc
from gatecost.ir.dump import dump as ir_dump
from tests.elab_helpers import codes, comb, elab, top_module

_NOLIM = {"const_func_steps": 10**9, "const_func_total_steps": 10**9}

# (name, source of the function(s), [(call arguments, expected value or None)])
CASES = [
    ("sum", """
  function integer sum(input integer n); integer i; begin sum = 0; for (i = 0; i < n; i = i + 1) sum = sum + i; end endfunction""",
     [("10", 45), ("0", 0), ("100", 4950)]),
    ("fact", """
  function [63:0] fact(input integer n); begin fact = 1; while (n > 1) begin fact = fact * n; n = n - 1; end end endfunction""",
     [("5", 120), ("20", 2432902008176640000), ("25", None)]),
    ("gcd", """
  function [15:0] gcd(input [15:0] a, input [15:0] b); reg [15:0] t;
    begin while (b != 0) begin t = b; b = a % b; a = t; end gcd = a; end endfunction""",
     [("12, 18", 6), ("17, 5", 1), ("0, 9", 9), ("65535, 255", 255)]),
    ("popc", """
  function [5:0] popc(input [31:0] x); integer i; begin popc = 0; for (i = 0; i < 32; i = i + 1) popc = popc + x[i]; end endfunction""",
     [("32'hdeadbeef", 24), ("0", 0), ("32'hffffffff", 32)]),
    ("rev", """
  function [7:0] rev(input [7:0] x); integer i; reg [7:0] r;
    begin for (i = 0; i < 8; i = i + 1) r[i] = x[7 - i]; rev = r; end endfunction""",
     [("8'h01", 0x80), ("8'hb2", 0x4d)]),
    ("crc8", """
  function [7:0] crc8(input [7:0] d, input [7:0] crc); integer i; reg [7:0] c;
    begin c = crc ^ d; for (i = 0; i < 8; i = i + 1) c = c[7] ? ((c << 1) ^ 8'h07) : (c << 1); crc8 = c; end endfunction""",
     [("8'h31, 8'h00", None), ("8'hff, 8'h5a", None)]),
    ("smul", """
  function signed [15:0] smul(input signed [7:0] a, input signed [7:0] b);
    smul = a * b + (a >>> 1) - b / 3 + a % b; endfunction""",
     [("-7, 3", None), ("100, -100", None), ("-128, -1", None), ("5, 0", None)]),
    ("sel", """
  function [7:0] sel(input [2:0] s);
    case (s) 3'd0: sel = 8'h11; 3'd1, 3'd2: sel = 8'h22; 3'd7: sel = 8'h77; default: sel = 8'hdd; endcase endfunction""",
     [("0", 0x11), ("2", 0x22), ("5", 0xdd), ("7", 0x77)]),
    ("dow", """
  function integer dow(input integer n); integer k; begin k = 0; dow = 1; do begin dow = dow * 3; k = k + 1; end while (k < n); end endfunction""",
     [("1", 3), ("4", 81)]),
    ("rep", """
  function integer rep(input integer n); integer k; begin rep = 7; repeat (n) rep = rep + 5; end endfunction""",
     [("3", 22), ("0", 7)]),
    ("brk", """
  function integer brk(input integer n); integer i; begin brk = 0;
    for (i = 0; i < 100; i = i + 1) begin
      if (i % 3 == 0) continue;
      if (i > n) break;
      brk = brk + i;
    end end endfunction""",
     [("10", None), ("0", 0), ("50", None)]),
    ("wcont", """
  function integer wcont(input integer n); integer i; begin wcont = 0; i = 0;
    while (i < n) begin i = i + 1; if (i[0]) continue; wcont = wcont + i; end end endfunction""",
     [("10", 30)]),
    ("arr", """
  function [15:0] arr(input [7:0] seed); reg [7:0] mem [0:15]; integer i; reg [15:0] acc;
    begin for (i = 0; i < 16; i = i + 1) mem[i] = seed + i * 3;
      acc = 0; for (i = 15; i >= 0; i = i - 1) acc = acc + mem[i] * (i + 1); arr = acc; end endfunction""",
     [("0", None), ("200", None)]),
    ("arr2", """
  function [11:0] arr2(input [3:0] k); reg [3:0] m [0:3][0:3]; integer i, j;
    begin for (i = 0; i < 4; i = i + 1) for (j = 0; j < 4; j = j + 1) m[i][j] = i * 4 + j + k;
      arr2 = {m[3][1], m[0][2], m[2][3]}; end endfunction""",
     [("0", None), ("9", None)]),
    ("arrdesc", """
  function [7:0] arrdesc(input [7:0] k); reg [7:0] m [7:0]; integer i; begin
      for (i = 0; i < 8; i = i + 1) m[i] = k ^ i; arrdesc = m[2] + m[7] - m[0]; end endfunction""",
     [("3", None)]),
    ("psel", """
  function [31:0] psel(input [7:0] x); reg [31:0] v;
    begin v = 0; v[7:0] = x; v[8 +: 8] = ~x; v[31 -: 4] = 4'hA; v[20:16] = x[4:0]; v[15] = 1'b1; psel = v; end endfunction""",
     [("8'h5c", None), ("0", None)]),
    ("cat", """
  function [23:0] cat(input [7:0] a, input [3:0] b, input c);
    cat = {a, b[3:2], {3{c}}, b[0], a[7:5], 5'b10101}; endfunction""",
     [("8'ha5, 4'b1011, 1'b1", None), ("8'h00, 4'b0100, 1'b0", None)]),
    ("tern", """
  function [7:0] tern(input [7:0] a, input [7:0] b); begin
      tern = (a > b ? a - b : b - a) + ((a && b) ? 1 : 0) + (a || b) + (!a) + (&a) + (|b) + (^a) + (~^b) + (~&a) + (~|b); end endfunction""",
     [("5, 9", None), ("255, 0", None), ("0, 0", None), ("255, 255", None)]),
    ("shf", """
  function signed [31:0] shf(input signed [31:0] a, input [5:0] n);
    shf = (a << n) ^ (a >> n) ^ (a >>> n) ^ (a <<< (n / 2)); endfunction""",
     [("-1000, 3", None), ("12345, 0", None), ("-1, 40", None), ("77, 33", None)]),
    ("pw", """
  function [31:0] pw(input integer b, input integer e); pw = b ** e + 2 ** e + (-2) ** 3; endfunction""",
     [("3, 4", None), ("10, 9", None), ("2, 40", None)]),
    ("cl", """
  function [31:0] cl(input integer n); cl = $clog2(n) * 100 + $signed(8'hf0) + $unsigned(-3) + $countones(n); endfunction""",
     [("1", None), ("33", None), ("1024", None), ("1025", None)]),
    ("early", """
  function integer early(input integer n); integer i; begin
      early = -1;
      for (i = 0; i < 50; i = i + 1) begin if (i * i > n) begin early = i; return early; end end
      early = 99; end endfunction""",
     [("30", 6), ("0", 1), ("100000", 99)]),
    ("trunc", """
  function [3:0] trunc(input [3:0] a); reg [3:0] t; reg [4:0] u; integer j; begin
      t = 5'd31 + 3; u = t; u += a; u <<= 1; u ^= 5'h15; t -= u; t |= 4'b0001; j = t; j *= 7; j >>= 1; t = j + u;
      trunc = t; end endfunction""",
     [("0", None), ("9", None), ("15", None)]),
    ("mixcmp", """
  function [7:0] mixcmp(input signed [7:0] a, input [7:0] b, input signed [3:0] c); begin
      mixcmp = (a < b) + 2 * (a < 0) + 4 * (a < c) + 8 * ($signed(b) > a) + 16 * (c < 4'sd0) + 32 * (a == b) + 64 * (c != a); end endfunction""",
     [("-1, 255, -2", None), ("5, 5, 5", None), ("-100, 100, 7", None)]),
    ("wide", """
  function [99:0] wide(input [99:0] a, input [99:0] b); wide = (a * b) ^ ({a[49:0], b[99:50]} + 100'd12345678901234567890); endfunction""",
     [("100'h123456789abcdef0123456789, 100'h3", None), ("100'd1, 100'd1", None)]),
    ("asc", """
  function [7:0] asc(input [7:0] x); reg [0:7] v; reg [2:9] w; integer i; begin
      v = x; w = x; for (i = 0; i < 8; i = i + 1) asc[i] = v[i] ^ w[i + 2] ^ v[7 - i]; asc = asc ^ v[0:3] ^ w[2:5]; end endfunction""",
     [("8'h35", None)]),
    ("div0", """
  function [7:0] div0(input [7:0] a, input [7:0] b); div0 = a / b; endfunction""",
     [("7, 2", 3), ("7, 0", None)]),
    ("uninit", """
  function [7:0] uninit(input [7:0] a); reg [7:0] t; begin t[3:0] = a[3:0]; uninit = t & 8'h0f; end endfunction""",
     [("8'h1a", None)]),
    ("oob", """
  function [7:0] oob(input [7:0] a, input integer i); oob = a[i] + 1; endfunction""",
     [("8'hff, 3", 2), ("8'hff, 9", None)]),
    ("limit", """
  function integer limit(input integer n); integer i; begin limit = 0; for (i = 0; i < n; i = i + 1) limit = limit + 1; end endfunction""",
     [("500", 500), ("5000", None)]),
    ("nest", """
  function [15:0] g2(input [15:0] a, input [15:0] b); reg [15:0] t;
    begin while (b != 0) begin t = b; b = a % b; a = t; end g2 = a; end endfunction
  function [31:0] nest(input [15:0] a, input [15:0] b); nest = {g2(a, b), g2(a * 2, b + 2)} + g2(.b(b), .a(a)); endfunction""",
     [("12, 18", None), ("100, 75", None)]),
    ("par", """
  function [15:0] par(input [7:0] a); par = a * K + W; endfunction""",
     [("3", 3 * 7 + 5)]),
    ("lfsr", """
  function [23:0] lfsr(input [4:0] index);
    reg [15:0] ms [15:0]; reg [7:0] md [15:0]; reg [15:0] sv; reg [7:0] dv; reg [7:0] dm; integer i, j;
    begin
      for (i = 0; i < 16; i = i + 1) begin ms[i] = 0; ms[i][i] = 1'b1; md[i] = 0; end
      for (dm = 8'h80; dm != 0; dm = dm >> 1) begin
        sv = ms[15]; dv = md[15] ^ dm;
        for (j = 15; j > 0; j = j - 1) begin ms[j] = ms[j-1]; md[j] = md[j-1]; end
        ms[0] = sv; md[0] = dv;
        for (j = 1; j < 16; j = j + 1) if ((16'h1021 >> j) & 1) begin ms[j] = ms[j] ^ sv; md[j] = md[j] ^ dv; end
      end
      if (index < 16) begin sv = ms[index]; dv = md[index]; end else begin sv = 0; dv = 0; end
      lfsr = {dv, sv};
    end endfunction""",
     [("0", None), ("3", None), ("15", None), ("16", None)]),
    ("strcmp", """
  function [7:0] strcmp(input [7:0] a); strcmp = (S == "ab") ? a : ~a; endfunction""",
     [("5", None)]),
    ("noinit_ret", """
  function [7:0] noinit_ret(input [7:0] a); begin if (a == 1) noinit_ret = 5; end endfunction""",
     [("1", 5), ("2", None)]),
    ("fill", """
  function [15:0] fill(input [3:0] a); fill = {a, '1} & 16'hf0f0 | {'0, a}; endfunction""",
     [("5", None)]),
    ("cz", """
  function [2:0] cz(input [3:0] x);
    casez (x) 4'b1???: cz = 3; 4'b01??: cz = 2; 4'b001?: cz = 1; 4'bzz00, 4'b0x11: cz = 5; default: cz = 0; endcase endfunction""",
     [("4'b1010", 3), ("4'b0110", 2), ("4'b0011", 1), ("4'b0001", 0), ("4'b0000", 5), ("4'b0100", 2)]),
    ("localblk", """
  function integer localblk(input integer n); begin : outer
      integer s; s = n;
      begin : inner integer s2; s2 = s * 2; s = s2 + 1; end
      localblk = s; end endfunction""",
     [("5", 11)]),
    ("tasklike", """
  function integer tasklike(input integer n); begin
      $display("hello %d", n); tasklike = n + 1; end endfunction""",
     [("5", None)]),
]

_PRELUDE = "  localparam [7:0] K = 7; localparam W = 5; localparam S = \"ab\";\n"


def _source():
    funcs = "".join(c[1] + "\n" for c in CASES)
    params = []
    outs = []
    for i, (name, _f, calls) in enumerate(CASES):
        for j, (args, _e) in enumerate(calls):
            params.append(f"  localparam P_{i}_{j} = {name}({args});\n  assign o_{i}_{j} = P_{i}_{j};")
            outs.append(f"output [127:0] o_{i}_{j}")
    return "module top(" + ", ".join(outs) + ");\n" + _PRELUDE + funcs + "\n".join(params) + "\nendmodule\n"


def _run(src, **limits):
    constfunc.STATS.update(calls=0, fallbacks=0)
    r = elab(src, limits=limits)
    m = top_module(r)
    return r, m, dict(constfunc.STATS)


class FastConstFuncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = _source()
        cls.fast = _run(src)
        cls.slow = _run(src, const_func_fast=0)
        cls.fast_nolim = _run(src, **_NOLIM)

    def test_fast_path_is_used_and_general_path_is_not_affected(self):
        self.assertGreater(self.fast[2]["calls"], 60)
        self.assertEqual(self.slow[2], {"calls": 0, "fallbacks": 0})

    def test_ir_identical_to_general_path(self):
        a = ir_dump(self.fast[1], normalize=True)
        b = ir_dump(self.slow[1], normalize=True)
        self.assertEqual(a, b)
        self.assertEqual(codes(self.fast[0]), codes(self.slow[0]))
        self.assertEqual(sorted(d.message for d in self.fast[0].diagnostics),
                         sorted(d.message for d in self.slow[0].diagnostics))

    def test_values_per_call(self):
        """Every call, one by one: the fast and the general path give the same bits (including unknown bits)."""
        fo, so = comb(self.fast[1]), comb(self.slow[1])
        self.assertEqual(fo, so)

    def test_reference_values(self):
        fo = comb(self.fast[1])
        for i, (name, _f, calls) in enumerate(CASES):
            for j, (args, exp) in enumerate(calls):
                if exp is None:
                    continue
                got = fo[f"o_{i}_{j}"]
                self.assertEqual(got, exp & ((1 << 128) - 1), f"{name}({args})")

    def test_loop_limit_still_reported(self):
        self.assertIn("LIMIT_REACHED", codes(self.fast[0]))

    def test_unknown_results_fall_back(self):
        fb = self.fast[2]["fallbacks"]
        self.assertGreaterEqual(fb, 4)          # div0(7,0), uninit, oob(.., 9), limit(5000), noinit_ret(2)

    def test_no_loop_limit_means_full_result_where_general_path_gives_up(self):
        # with the unroll limit raised both paths agree on the long loop too
        lim = {"loop_unroll": 100000, **_NOLIM}
        src = "module t(output [31:0] y); function integer f(input integer n); integer i; begin f = 0; for (i = 0; i < n; i = i + 1) f = f + 3; end endfunction localparam P = f(5000); assign y = P; endmodule"
        for fast in (1, 0):
            _, m, _ = _run(src, const_func_fast=fast, **lim)
            self.assertEqual(comb(m)["y"], 15000)

    def test_memoisation_and_step_budget(self):
        # 150 calls of a loop-heavy function: far above the general path's per-function step budget (600000)
        calls = "\n".join(f"  assign y{i} = f({i});" for i in range(150))
        outs = ", ".join(f"output [31:0] y{i}" for i in range(150))
        src = f"""module t({outs});
  function integer f(input integer n); integer i, j; begin f = 0;
    for (i = 0; i < 50; i = i + 1) for (j = 0; j < 20; j = j + 1) f = f + (i ^ j) + n; end endfunction
{calls}
endmodule"""
        r, m, st = _run(src)
        self.assertEqual(codes(r, True), [])
        self.assertEqual(st["fallbacks"], 0)
        o = comb(m)
        exp = lambda n: sum((i ^ j) + n for i in range(50) for j in range(20))
        self.assertEqual([o[f"y{i}"] for i in range(150)], [exp(i) for i in range(150)])

    def test_non_constant_arguments_use_the_general_path(self):
        src = """module t(input [7:0] a, output [7:0] y);
  function [7:0] f(input [7:0] x); integer i; begin f = 0; for (i = 0; i < 8; i = i + 1) f = f + x[i]; end endfunction
  assign y = f(a); endmodule"""
        r, m, st = _run(src)
        self.assertEqual(st, {"calls": 0, "fallbacks": 0})
        self.assertEqual(comb(m, a=0xf3)["y"], 6)

    def test_same_function_in_two_instances(self):
        src = """module leaf #(parameter W = 4)(output [W-1:0] y);
  function [W-1:0] f(input [W-1:0] x); integer i; begin f = 0; for (i = 0; i < W; i = i + 1) f = f ^ (x << i); end endfunction
  assign y = f(5); endmodule
module top(output [3:0] a, output [3:0] b, output [5:0] c);
  leaf #(4) u0(.y(a)); leaf #(4) u1(.y(b)); leaf #(6) u2(.y(c)); endmodule"""
        r, m, st = _run(src)
        self.assertEqual(st["fallbacks"], 0)
        got = sorted(comb(mod)["y"] for mod in r.design.modules.values() if mod.name.startswith("leaf"))
        self.assertEqual(got, [3, 3])


class _Gen:
    """Random constant functions (integer / reg / signed / part-select / loops / case)."""

    BIN = ["+", "-", "*", "/", "%", "&", "|", "^", "~^", "<<", ">>", "<<<", ">>>", "==", "!=", "<", "<=", ">", ">=",
           "&&", "||", "===", "!=="]
    UN = ["-", "~", "!", "&", "|", "^", "~&", "~|", "~^", "+"]

    def __init__(self, rnd):
        self.r = rnd
        self.has_arr = False

    def width(self):
        return self.r.choice([1, 2, 3, 4, 5, 7, 8, 8, 12, 16, 16, 24, 31, 32, 33, 40, 64])

    def lit(self):
        r = self.r
        k = r.random()
        if k < 0.3:
            return str(r.randint(0, 40))
        if k < 0.5:
            w = self.width()
            return f"{w}'d{r.randrange(1 << w)}"
        if k < 0.65:
            w = self.width()
            return f"{w}'h{r.randrange(1 << w):x}"
        if k < 0.75:
            w = max(2, self.width())
            return f"{w}'sd{r.randrange(1 << (w - 1))}"
        if k < 0.85:
            return f"-{r.randint(1, 20)}"
        return f"{self.width()}'b{r.choice(['0', '1'])}"

    def pick_idx(self, v):
        n, w, sg, l, rr = v
        return self.r.randint(min(l, rr), max(l, rr))

    def part(self, v):
        n, w, sg, l, rr = v
        a, b = self.pick_idx(v), self.pick_idx(v)
        if (l >= rr) != (a >= b):
            a, b = b, a
        return f"{n}[{a}:{b}]"

    def arr_idx(self, vs):
        ports = [v for v in vs if v[0].startswith("p") and v[1] >= 2]
        if ports and self.r.random() < 0.6:
            return f"{self.r.choice(ports)[0]}[1:0]"
        return str(self.r.randint(0, 3))

    def atom(self, vs):
        r = self.r
        k = r.random()
        if k < 0.1 and self.has_arr:
            a = f"ar[{self.arr_idx(vs)}]"
            return f"{a}[{r.randrange(8)}]" if r.random() < 0.2 else a
        if k < 0.55 and vs:
            v = r.choice(vs)
            n, w, sg, l, rr = v
            if w > 1 and r.random() < 0.3:
                if r.random() < 0.5:
                    return f"{n}[{self.pick_idx(v)}]"
                return self.part(v)
            if w > 3 and r.random() < 0.1:
                base = r.randint(min(l, rr), max(l, rr) - 1)
                return f"{n}[{base} +: 2]"
            return n
        return self.lit()

    def expr(self, vs, d=0):
        r = self.r
        if d >= 3 or r.random() < 0.25:
            return self.atom(vs)
        k = r.random()
        if k < 0.45:
            return f"({self.expr(vs, d + 1)} {r.choice(self.BIN)} {self.expr(vs, d + 1)})"
        if k < 0.58:
            return f"({r.choice(self.UN)}{self.expr(vs, d + 1)})"
        if k < 0.68:
            return f"({self.expr(vs, d + 1)} ? {self.expr(vs, d + 1)} : {self.expr(vs, d + 1)})"
        if k < 0.76:
            return "{" + ", ".join(self.expr(vs, d + 1) for _ in range(r.randint(1, 3))) + "}"
        if k < 0.80:
            return "{" + f"{r.randint(1, 3)}" + "{" + self.expr(vs, d + 1) + "}}"
        if k < 0.86:
            return f"$signed({self.expr(vs, d + 1)})"
        if k < 0.90:
            return f"$unsigned({self.expr(vs, d + 1)})"
        if k < 0.93:
            return f"$clog2({self.expr(vs, d + 1)})"
        if k < 0.96:
            return f"({self.expr(vs, d + 1)} ** {r.randint(0, 3)})"
        if k < 0.98:
            return f"{r.choice([4, 9, 16, 33])}'({self.expr(vs, d + 1)})"
        return f"$countones({self.expr(vs, d + 1)})"

    def stmts(self, vs, d, out, ind):
        r = self.r
        for _ in range(r.randint(1, 4)):
            k = r.random()
            pad = "  " * ind
            v = r.choice([x for x in vs if not x[0].startswith("lv")])
            n, w, sg, l, rr = v
            if k < 0.5 or d >= 2:
                if self.has_arr and r.random() < 0.15:
                    a = f"ar[{self.arr_idx(vs)}]"
                    if r.random() < 0.3:
                        a += f"[{r.randrange(8)}]"
                    out.append(f"{pad}{a} = {self.expr(vs)};")
                elif w > 2 and r.random() < 0.25:
                    out.append(f"{pad}{self.part(v)} = {self.expr(vs)};")
                elif w > 1 and r.random() < 0.15:
                    out.append(f"{pad}{n}[{self.pick_idx(v)}] = {self.expr(vs)};")
                elif r.random() < 0.2:
                    out.append(f"{pad}{n} {r.choice(['+=', '-=', '^=', '|=', '&=', '<<=', '>>=', '*='])} {self.expr(vs)};")
                else:
                    out.append(f"{pad}{n} = {self.expr(vs)};")
            elif k < 0.68:
                out.append(f"{pad}if ({self.expr(vs)}) begin")
                self.stmts(vs, d + 1, out, ind + 1)
                if r.random() < 0.5:
                    out.append(f"{pad}end else begin")
                    self.stmts(vs, d + 1, out, ind + 1)
                out.append(f"{pad}end")
            elif k < 0.85:
                lv = f"lv{d}"
                out.append(f"{pad}for ({lv} = 0; {lv} < {r.randint(1, 5)}; {lv} = {lv} + 1) begin")
                self.stmts(vs + [(lv, 32, True, 31, 0)], d + 1, out, ind + 1)
                if r.random() < 0.2:
                    out.append(f"{pad}  if ({self.expr(vs)}) {r.choice(['break', 'continue'])};")
                out.append(f"{pad}end")
            else:
                out.append(f"{pad}case ({self.expr(vs)})")
                for _ in range(r.randint(1, 3)):
                    out.append(f"{pad}  {r.randint(0, 9)}, {r.randint(0, 9)}: begin")
                    self.stmts(vs, d + 1, out, ind + 2)
                    out.append(f"{pad}  end")
                out.append(f"{pad}  default: begin")
                self.stmts(vs, d + 1, out, ind + 2)
                out.append(f"{pad}  end")
                out.append(f"{pad}endcase")

    def function(self, name):
        r = self.r
        ports = [(f"p{i}", self.width(), r.random() < 0.4, 0, 0) for i in range(r.randint(1, 3))]
        ports = [(n, w, sg, w - 1, 0) for n, w, sg, _l, _r in ports]
        rw, rsg = self.width(), r.random() < 0.4
        locs = []
        for i in range(r.randint(0, 3)):
            if r.random() < 0.3:
                locs.append((f"l{i}", 32, True, 31, 0, "integer"))
            else:
                w, sg = self.width(), r.random() < 0.3
                form = r.choice(["desc", "desc", "asc", "off"])
                l, rr = {"desc": (w - 1, 0), "asc": (0, w - 1), "off": (w + 1, 2)}[form]
                locs.append((f"l{i}", w, sg, l, rr, f"reg {'signed ' if sg else ''}[{l}:{rr}]"))
        vs = list(ports) + [(n, w, sg, l, rr) for n, w, sg, l, rr, _t in locs] + [(name, rw, rsg, rw - 1, 0)]
        self.has_arr = r.random() < 0.5
        lines = [f"  function {'signed ' if rsg else ''}[{rw - 1}:0] {name}("
                 + ", ".join(f"input {'signed ' if sg else ''}[{w - 1}:0] {n}" for n, w, sg, _l, _r in ports) + ");"]
        for n, w, sg, l, rr, t in locs:
            lines.append(f"    {t} {n};")
        if self.has_arr:
            lines.append(r.choice(["    reg [7:0] ar [0:3];", "    reg [7:0] ar [3:0];"]))
        lines.append("    integer lv0, lv1, lv2;")
        lines.append("    begin")
        if self.has_arr:
            lines.append(f"      for (lv2 = 0; lv2 < 4; lv2 = lv2 + 1) ar[lv2] = {self.expr(vs[:len(ports)])} + lv2;")
        for n, w, sg, l, rr, t in locs:
            if r.random() < 0.95:
                lines.append(f"      {n} = {self.expr(vs[:len(ports)])};")
        if r.random() < 0.95:
            lines.append(f"      {name} = {self.expr(vs[:len(ports)])};")
        self.stmts(vs, 0, lines, 3)
        if r.random() < 0.9:
            lines.append(f"      {name} = {self.expr(vs)};")
        lines.append("    end")
        lines.append("  endfunction")
        return "\n".join(lines), ports


class FuzzDifferentialTests(unittest.TestCase):
    def test_random_functions_agree(self):
        import random
        total_calls = 0
        for seed in range(int(os.environ.get("GATECOST_FUZZ_SEEDS", "40"))):
            rnd = random.Random(seed)
            g = _Gen(rnd)
            funcs, params, outs = [], [], []
            for fi in range(8):
                src, ports = g.function(f"f{fi}")
                funcs.append(src)
                for ci in range(3):
                    args = []
                    for _n, w, sg, _l, _r in ports:
                        v = rnd.choice([0, 1, rnd.randrange(1 << w), (1 << w) - 1, rnd.randrange(1 << w)])
                        args.append(f"{w}'d{v}")
                    params.append(f"  localparam P_{fi}_{ci} = f{fi}({', '.join(args)});")
                    params.append(f"  assign o_{fi}_{ci} = P_{fi}_{ci};")
                    outs.append(f"output [127:0] o_{fi}_{ci}")
            text = ("module top(" + ", ".join(outs) + ");\n" + "\n".join(funcs) + "\n" + "\n".join(params)
                    + "\nendmodule\n")
            fast = _run(text)
            slow = _run(text, const_func_fast=0)
            a, b = ir_dump(fast[1], normalize=True), ir_dump(slow[1], normalize=True)
            if a != b:
                self.fail(f"seed {seed}: fast and general path differ\n{text}\n--- fast\n{a}\n--- general\n{b}")
            # the general path also evaluates the unselected arm of a ternary, which can add a spurious diagnostic
            extra = {d.message for d in fast[0].diagnostics} - {d.message for d in slow[0].diagnostics}
            self.assertFalse(extra, f"seed {seed}")
            total_calls += fast[2]["calls"]
        self.assertGreater(total_calls, 300)


if __name__ == "__main__":
    unittest.main()
