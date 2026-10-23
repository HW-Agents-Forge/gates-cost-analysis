"""Elaborator: self/context-determined width and signedness (FRONTEND.md section 2.5.5)."""
import random
import unittest

from tests.elab_helpers import comb, elab, top_module, width_sign

DECLS = ("wire [7:0] a; wire [3:0] b; wire signed [7:0] sa; wire signed [3:0] sb; integer i; "
         "reg [15:0] r; wire [0:7] asc; logic [3:0][7:0] pk; byte by; shortint si;")

# expression -> (width, signed)
TABLE = [
    ("a+b", 8, False), ("b+b", 4, False), ("a+sa", 8, False), ("sa+sb", 8, True), ("sb+sb", 4, True),
    ("a*b", 8, False), ("sa*sb", 8, True), ("a/b", 8, False), ("a%b", 8, False),
    ("a&b", 8, False), ("a|sb", 8, False), ("a^~b", 8, False),
    ("a<b", 1, False), ("sa<sb", 1, False), ("a==b", 1, False), ("a!==b", 1, False), ("a==?b", 1, False),
    ("a&&b", 1, False), ("a||b", 1, False), ("!a", 1, False),
    ("&a", 1, False), ("|a", 1, False), ("^a", 1, False), ("~&a", 1, False), ("~|a", 1, False), ("~^a", 1, False),
    ("a<<b", 8, False), ("b<<a", 4, False), ("sa>>>1", 8, True), ("sa>>1", 8, True), ("sb<<<2", 4, True),
    ("a**2", 8, False), ("sa**2", 8, True),
    ("{a,b}", 12, False), ("{b,b,b}", 12, False), ("{3{b}}", 12, False), ("{2{a,b}}", 24, False),
    ("a?b:sa", 8, False), ("a?sb:sa", 8, True), ("a?b:b", 4, False),
    ("a[3:0]", 4, False), ("a[2]", 1, False), ("sa[3:0]", 4, False), ("sa[2]", 1, False),
    ("a[b+:2]", 2, False), ("r[a-:4]", 4, False), ("asc[0:3]", 4, False),
    ("pk[1]", 8, False), ("pk[1][3:0]", 4, False), ("pk[2][7]", 1, False),
    ("$signed(a)", 8, True), ("$unsigned(sa)", 8, False), ("$signed(b)+$signed(b)", 4, True),
    ("8'sd5", 8, True), ("4'd3", 4, False), ("4'sd3", 4, True), ("'hFF", 32, False), ("12", 32, True),
    ("64'h1", 64, False), ("40'sd1", 40, True), ("i", 32, True), ("by", 8, True), ("si", 16, True),
    ("-sa", 8, True), ("-a", 8, False), ("~b", 4, False), ("+sb", 4, True),
    ("$clog2(a)", 32, True), ("$bits(a)", 32, True), ("$bits(r)", 32, True),
    ("sa/sb", 8, True), ("(a+b)>>1", 8, False), ("a+1", 32, False), ("a+1'b1", 8, False), ("sa+1", 32, True),
    ("16'(a)", 16, False), ("signed'(a)", 8, True), ("unsigned'(sa)", 8, False), ("2'(a)", 2, False),
    ("a inside {1,2}", 1, False), ("$countones(a)", 32, True), ("$onehot(a)", 1, False),
    ("$size(a)", 32, True), ("$high(asc)", 32, True),
]


class WidthTable(unittest.TestCase):
    def test_table(self):
        bad = []
        for expr, w, s in TABLE:
            got = width_sign(DECLS, expr)
            if got != (w, s):
                bad.append((expr, got, (w, s)))
        self.assertEqual(bad, [])


class Semantics(unittest.TestCase):
    """Context-determined evaluation checked against Python reference values."""

    @staticmethod
    def sg(x, w):
        return x - (1 << w) if (x >> (w - 1)) & 1 else x

    def test_context_width_carries(self):
        # the carry of a+b is kept because the target is wide enough (context-determined)
        r = elab("""
module t(input [7:0] a, b, output [8:0] y, output [7:0] z, output [8:0] c);
  assign y = a + b;
  assign z = a + b;
  assign c = (a + b) >> 1;
endmodule""")
        m = top_module(r)
        for a, b in ((255, 255), (200, 100), (1, 2)):
            o = comb(m, a=a, b=b)
            self.assertEqual(o["y"], a + b)
            self.assertEqual(o["z"], (a + b) & 255)
            self.assertEqual(o["c"], (a + b) >> 1)

    def test_sign_extension_rules(self):
        r = elab("""
module t(input signed [3:0] sa, input [3:0] ua, output [7:0] y0, output [7:0] y1, output [7:0] y2, output [7:0] y3, output y4, output y5);
  assign y0 = sa;                 // sign extended
  assign y1 = ua;                 // zero extended
  assign y2 = sa + ua;            // mixed: unsigned context, zero extension
  assign y3 = sa + 4'sd1;         // signed context
  assign y4 = sa < 0;
  assign y5 = ua < 0;
endmodule""")
        m = top_module(r)
        for sa in range(16):
            for ua in (0, 5, 15):
                o = comb(m, sa=sa, ua=ua)
                s = self.sg(sa, 4)
                self.assertEqual(o["y0"], s & 255)
                self.assertEqual(o["y1"], ua)
                self.assertEqual(o["y2"], (sa + ua) & 255)
                self.assertEqual(o["y3"], (s + 1) & 255)
                self.assertEqual(o["y4"], int(s < 0))
                self.assertEqual(o["y5"], 0)

    def test_signed_division_shift_and_compare(self):
        r = elab("""
module t(input signed [7:0] a, input signed [7:0] b, output signed [7:0] q, output signed [7:0] sh, output lt, output [7:0] ush);
  assign q = a / b;
  assign sh = a >>> 2;
  assign lt = a < b;
  assign ush = a >> 2;
endmodule""")
        m = top_module(r)
        rnd = random.Random(3)
        for _ in range(200):
            a, b = rnd.randrange(256), rnd.randrange(1, 256)
            sa, sb = self.sg(a, 8), self.sg(b, 8)
            o = comb(m, a=a, b=b)
            qq = abs(sa) // abs(sb) * (1 if (sa < 0) == (sb < 0) else -1)
            self.assertEqual(o["q"], qq & 255)
            self.assertEqual(o["sh"], (sa >> 2) & 255)
            self.assertEqual(o["lt"], int(sa < sb))
            self.assertEqual(o["ush"], a >> 2)

    def test_ternary_concat_replicate_and_reductions(self):
        r = elab("""
module t(input [3:0] a, input s, output [7:0] y0, output [7:0] y1, output y2, output y3, output y4);
  assign y0 = {a, s ? a : ~a};
  assign y1 = {2{a}};
  assign y2 = &a;
  assign y3 = ^a;
  assign y4 = ~|a;
endmodule""")
        m = top_module(r)
        for a in range(16):
            for s in (0, 1):
                o = comb(m, a=a, s=s)
                self.assertEqual(o["y0"], (a << 4) | (a if s else (~a & 15)))
                self.assertEqual(o["y1"], (a << 4) | a)
                self.assertEqual(o["y2"], int(a == 15))
                self.assertEqual(o["y3"], bin(a).count("1") & 1)
                self.assertEqual(o["y4"], int(a == 0))

    def test_part_selects_with_variable_base_and_ascending_ranges(self):
        r = elab("""
module t(input [15:0] x, input [3:0] i, output [3:0] up, output [3:0] dn, output b);
  wire [0:15] asc = x;
  assign up = x[i +: 4];
  assign dn = x[i + 3 -: 4];
  assign b = asc[i];
endmodule""")
        m = top_module(r)
        rnd = random.Random(1)
        for _ in range(100):
            x, i = rnd.randrange(1 << 16), rnd.randrange(12)
            o = comb(m, x=x, i=i)
            self.assertEqual(o["up"], (x >> i) & 15)
            self.assertEqual(o["dn"], (x >> i) & 15)
            self.assertEqual(o["b"], (x >> (15 - i)) & 1)

    def test_streaming_inside_and_patterns(self):
        r = elab("""
module t(input [7:0] a, output [7:0] rev, output [7:0] bytes, output in_set, output [7:0] pat);
  assign rev = {<<{a}};
  assign bytes = {<<4{a}};
  assign in_set = a inside {8'd3, [8'd10:8'd20], 8'hF?};
  typedef struct packed { logic [3:0] hi; logic [3:0] lo; } s_t;
  s_t s;
  assign s = '{hi: 4'd7, lo: 4'd1};
  assign pat = s;
endmodule""")
        m = top_module(r)
        for a in (0, 3, 15, 16, 21, 0xF3, 0xFF, 5):
            o = comb(m, a=a)
            self.assertEqual(o["rev"], int(f"{a:08b}"[::-1], 2))
            self.assertEqual(o["bytes"], ((a & 15) << 4) | (a >> 4))
            self.assertEqual(o["in_set"], int(a == 3 or 10 <= a <= 20 or (a >> 4) == 15))
            self.assertEqual(o["pat"], 0x71)

    def test_literals_with_x_and_z(self):
        r = elab("""
module t(input [3:0] a, output y, output [3:0] z);
  assign y = (a ==? 4'b1?0?);
  assign z = 4'b10_01;
endmodule""")
        m = top_module(r)
        for a in range(16):
            o = comb(m, a=a)
            self.assertEqual(o["y"], int((a >> 3) == 1 and ((a >> 1) & 1) == 0))
            self.assertEqual(o["z"], 9)


if __name__ == "__main__":
    unittest.main()
