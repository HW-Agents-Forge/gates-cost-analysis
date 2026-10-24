"""Cost sanity audit: small reference designs with analytically known gate counts.

Each design in ``REFERENCE`` carries a textbook estimate (GE, from the ``generic-asic``
primitive table: ripple adders, XOR trees, MUX2 trees, DFF banks, EDFF register files, ...).
The typ cost must stay within a factor of 1.5 of it. A deviation beyond that is a formula
bug, not a modelling choice. The regression tests below pin the individual fixes made by the
audit (narrow-operand adders, fused sums of narrow terms, sync-read memory register, muxes
whose arms share bits).
"""
import json
import unittest

from tests.e2e_support import Project

FACTOR = 1.5

# (name, Verilog, textbook estimate in GE)
REFERENCE = [
    ('add32', 'module t(input [31:0] a,b, output [31:0] y); assign y=a+b; endmodule', 170),
    ('sub32', 'module t(input [31:0] a,b, output [31:0] y); assign y=a-b; endmodule', 190),
    ('eq32', 'module t(input [31:0] a,b, output y); assign y=(a==b); endmodule', 95),
    ('lt32', 'module t(input [31:0] a,b, output y); assign y=(a<b); endmodule', 96),
    ('mux8x8', 'module t(input [7:0] a0,a1,a2,a3,a4,a5,a6,a7, input [2:0] s, output [7:0] y); wire [7:0] v[0:7]; assign v[0]=a0;assign v[1]=a1;assign v[2]=a2;assign v[3]=a3;assign v[4]=a4;assign v[5]=a5;assign v[6]=a6;assign v[7]=a7; assign y=v[s]; endmodule', 130.5),
    ('counter8en', 'module t(input clk,rst,en, output reg [7:0] c); always @(posedge clk) if(rst) c<=0; else if(en) c<=c+1; endmodule', 101.3),
    ('counter16', 'module t(input clk, output reg [15:0] c); always @(posedge clk) c<=c+1; endmodule', 144.0),
    ('shreg16x8', 'module t(input clk, input [7:0] d, output [7:0] q); reg [7:0] r[0:15]; integer i; always @(posedge clk) begin r[0]<=d; for(i=1;i<16;i=i+1) r[i]<=r[i-1]; end assign q=r[15]; endmodule', 725.8),
    ('shreg64', 'module t(input clk, d, output q); reg [63:0] r; always @(posedge clk) r<={r[62:0],d}; assign q=r[63]; endmodule', 362.9),
    ('dec4to16', "module t(input [3:0] a, output [15:0] y); assign y=16'b1<<a; endmodule", 37.4),
    ('barrel32', 'module t(input [31:0] a, input [4:0] s, output [31:0] y); assign y=a<<s; endmodule', 372.8),
    ('parity32', 'module t(input [31:0] a, output y); assign y=^a; endmodule', 62),
    ('regar8', 'module t(input clk,rst_n, input [7:0] d, output reg [7:0] q); always @(posedge clk or negedge rst_n) if(!rst_n) q<=0; else q<=d; endmodule', 53.4),
    ('acc16', 'module t(input clk, input [15:0] d, output reg [15:0] q); always @(posedge clk) q<=q+d; endmodule', 175.7),
    ('and_red16', 'module t(input [15:0] a, output y); assign y=&a; endmodule', 9.0),
    ('bitand32', 'module t(input [31:0] a,b, output [31:0] y); assign y=a&b; endmodule', 42.6),
    ('rf32x32_2r1w', 'module t(input clk, we, input [4:0] wa,ra1,ra2, input [31:0] wd, output [31:0] rd1,rd2); reg [31:0] m[0:31]; always @(posedge clk) if(we) m[wa]<=wd; assign rd1=m[ra1]; assign rd2=m[ra2]; endmodule', 10581.9),
    ('ram16x8_async', 'module t(input clk, we, input [3:0] wa,ra, input [7:0] wd, output [7:0] rd); reg [7:0] m[0:15]; always @(posedge clk) if(we) m[wa]<=wd; assign rd=m[ra]; endmodule', 1158.2),
    ('fifo16x8', 'module t(input clk,rst,push,pop,input [7:0] din,output [7:0] dout,output full,empty);\nreg [7:0] m[0:15]; reg [4:0] wp,rp;\nassign full=(wp[3:0]==rp[3:0])&&(wp[4]!=rp[4]); assign empty=(wp==rp);\nalways @(posedge clk) begin if(rst) begin wp<=0; rp<=0; end else begin if(push&&!full) begin m[wp[3:0]]<=din; wp<=wp+1; end if(pop&&!empty) rp<=rp+1; end end\nassign dout=m[rp[3:0]]; endmodule', 1308.2),
    ('mul16', 'module t(input [15:0] a,b, output [31:0] y); assign y=a*b; endmodule', 1534.4),
    ('mul8', 'module t(input [7:0] a,b, output [15:0] y); assign y=a*b; endmodule', 367.6),
    ('mul32', 'module t(input [31:0] a,b, output [63:0] y); assign y=a*b; endmodule', 6478.7),
    ('bitsel64', 'module t(input [63:0] a, input [5:0] i, output y); assign y=a[i]; endmodule', 146.8),
    ('popcount32', 'module t(input [31:0] a, output [5:0] y); integer i; reg [5:0] s; always @* begin s=0; for(i=0;i<32;i=i+1) s=s+a[i]; end assign y=s; endmodule', 195.2),
    ('lfsr32', "module t(input clk,rst, output reg [31:0] q); always @(posedge clk) if(rst) q<=32'h1; else q<={q[30:0], q[31]^q[21]^q[1]^q[0]}; endmodule", 230.0),
    ('crc32_8b', "module t(input clk,rst, input [7:0] d, output reg [31:0] c);\n integer i; reg [31:0] n; reg x;\n always @* begin n=c; for(i=0;i<8;i=i+1) begin x=n[31]^d[i]; n={n[30:0],1'b0}; if(x) n=n^32'h04C11DB7; end end\n always @(posedge clk) if(rst) c<=32'hffffffff; else c<=n; endmodule", 352.0),
    ('fsm4', 'module t(input clk,rst,go,done, output reg busy, output reg ld);\n reg [1:0] s; always @(posedge clk) if(rst) s<=0; else case(s) 0: if(go) s<=1; 1: s<=2; 2: if(done) s<=3; 3: s<=0; endcase\n always @* begin busy=(s!=0); ld=(s==1); end endmodule', 36.0),
    ('prienc32', 'module t(input [31:0] a, output reg [4:0] y, output v); integer i; always @* begin y=0; for(i=0;i<32;i=i+1) if(a[i]) y=i; end assign v=|a; endmodule', 127),
    ('satadd8', "module t(input [7:0] a,b, output [7:0] y); wire [8:0] s=a+b; assign y=s[8]?8'hff:s[7:0]; endmodule", 51.3),
    ('sgnlt32', 'module t(input signed [31:0] a,b, output y); assign y=a<b; endmodule', 98),
    ('gray16', 'module t(input [15:0] a, output [15:0] y); assign y=a^(a>>1); endmodule', 30),
    ('mac16', 'module t(input clk, input [15:0] a,b, output reg [39:0] acc); always @(posedge clk) acc<=acc+a*b; endmodule', 1974.4),
    ('add3_32', 'module t(input [31:0] a,b,c, output [33:0] y); assign y=a+b+c; endmodule', 341.1),
    ('clock_div_cnt_cmp', 'module t(input clk, input [15:0] lim, output reg tick); reg [15:0] c; always @(posedge clk) begin if(c==lim) begin c<=0; tick<=1; end else begin c<=c+1; tick<=0; end end endmodule', 219.0),
    ('fifo64x32', 'module t(input clk,rst,push,pop,input [31:0] din,output [31:0] dout,output full,empty);\nreg [31:0] m[0:63]; reg [6:0] wp,rp;\nassign full=(wp[5:0]==rp[5:0])&&(wp[6]!=rp[6]); assign empty=(wp==rp);\nalways @(posedge clk) begin if(rst) begin wp<=0; rp<=0; end else begin if(push&&!full) begin m[wp[5:0]]<=din; wp<=wp+1; end if(pop&&!empty) rp<=rp+1; end end\nassign dout=m[rp[5:0]]; endmodule', 18315.2),
]


def cost(src: str) -> dict:
    p = Project({"t.v": src}, git=False)
    try:
        code, text = p.run("cost", "--format", "json", "--no-meta", "--no-cache")
    finally:
        p.close()
    assert code == 0, text
    return json.loads(text)["result"]


def typ(src: str) -> float:
    return cost(src)["total"]["ge"]["typ"]


def category(src: str, cat: str) -> float:
    return cost(src)["by_category"][cat]


class ReferenceDesigns(unittest.TestCase):
    def test_within_factor_of_textbook(self):
        bad = []
        for name, src, want in REFERENCE:
            got = typ(src)
            if not (want / FACTOR <= got <= want * FACTOR):
                bad.append(f"{name}: got {got:.1f} GE, textbook {want:.1f} GE")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_scenarios_ordered(self):
        for name, src, _ in REFERENCE:
            ge = cost(src)["total"]["ge"]
            self.assertLessEqual(ge["min"], ge["typ"] + 0.01, name)
            self.assertLessEqual(ge["typ"], ge["max"] + 0.01, name)


class NarrowOperandAdd(unittest.TestCase):
    """A sum with a 1-bit operand is an incrementer chain, not a full-width adder."""

    SRC = "module t(input [31:0] a, input b, output [31:0] y); assign y = a + b; endmodule"

    def test_cheaper_than_full_adder(self):
        full = typ("module t(input [31:0] a, b, output [31:0] y); assign y = a + b; endmodule")
        self.assertLess(typ(self.SRC), 0.7 * full)

    def test_not_cheaper_than_half_adder_chain(self):
        self.assertGreaterEqual(typ(self.SRC), 31 * 3.33)

    def test_popcount_chain_is_a_compressor_tree(self):
        src = ("module t(input [31:0] a, output [5:0] y); integer i; reg [5:0] s; "
               "always @* begin s = 0; for (i = 0; i < 32; i = i + 1) s = s + a[i]; end assign y = s; endmodule")
        # 32 one-bit inputs need about 26 FA (180 GE); the old model charged 30 x 6-bit FA rows
        self.assertLess(typ(src), 300)
        self.assertGreater(typ(src), 140)


class SyncReadMemory(unittest.TestCase):
    ASYNC = ("module t(input clk, we, input [3:0] a, input [7:0] wd, output [7:0] rd); reg [7:0] m [0:15]; "
             "always @(posedge clk) if (we) m[a] <= wd; assign rd = m[a]; endmodule")
    SYNC = ("module t(input clk, we, input [3:0] a, input [7:0] wd, output reg [7:0] rd); reg [7:0] m [0:15]; "
            "always @(posedge clk) begin if (we) m[a] <= wd; rd <= m[a]; end endmodule")

    def test_registered_read_adds_one_flop_per_bit(self):
        self.assertAlmostEqual(typ(self.SYNC) - typ(self.ASYNC), 8 * 5.67, delta=0.5)

    def test_per_bit_cost_is_reasonable_and_linear(self):
        # flop + write mux + read mux tree, with no quadratic term in the depth
        def mem(d, w):
            return ("module t(input clk, we, input [%d:0] wa, ra, input [%d:0] wd, output [%d:0] rd); "
                    "reg [%d:0] m [0:%d]; always @(posedge clk) if (we) m[wa] <= wd; assign rd = m[ra]; endmodule"
                    % (max(1, (d - 1).bit_length()) - 1, w - 1, w - 1, w - 1, d - 1))
        per_bit = [typ(mem(d, 8)) / (d * 8) for d in (4, 16, 64, 256)]
        for v in per_bit:
            self.assertTrue(7.5 <= v <= 12.5, per_bit)
        self.assertLess(per_bit[-1] / per_bit[0], 1.3, per_bit)


class MuxWithSharedBits(unittest.TestCase):
    """``if (x) n = n ^ K``: only the bits where K is 1 need a selector."""

    def test_crc_style_conditional_xor(self):
        src = ("module t(input clk, rst, input [7:0] d, output reg [31:0] c); integer i; reg [31:0] n; reg x; "
               "always @* begin n = c; for (i = 0; i < 8; i = i + 1) begin x = n[31] ^ d[i]; n = {n[30:0], 1'b0}; "
               "if (x) n = n ^ 32'h04C11DB7; end end "
               "always @(posedge clk) if (rst) c <= 32'hffffffff; else c <= n; endmodule")
        r = cost(src)
        # 8 steps x 15 polynomial bits of selector; the old model charged all 32 bits per step
        self.assertLess(r["by_category"]["select"], 8 * 15 * 2.33 + 20)
        self.assertLess(r["total"]["ge"]["typ"], 600)

    def test_equal_bits_of_the_arms_are_free(self):
        src = ("module t(input s, input [15:0] a, output [15:0] y); "
               "assign y = s ? a : (a ^ 16'h00ff); endmodule")
        # the upper byte passes through a ^ 0 on both arms: only the low byte needs selectors
        self.assertLess(category(src, "select"), 8 * 2.33 + 0.5)


if __name__ == "__main__":
    unittest.main()
