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

# Round 2: twenty more designs with analytically known counts (shift registers, LFSR, gray code, saturation, encoders,
# popcount, abs/neg, comparator chain, CAM, register file, rotator, CRC, Booth, ring/Johnson counters).  The textbook
# value is written next to each design as a sum of primitives (FA 5.33, HA 3.33, XOR2 2, MUX2 2.33, DFF 5.67, ...).
REFERENCE_ROUND2 = [
    ('shreg8_en', 'module t(input clk, en, d, output q); reg [7:0] r; always @(posedge clk) if (en) r <= {r[6:0], d}; assign q = r[7]; endmodule', 64.0),
    ('lfsr16', 'module t(input clk, output q); reg [15:0] r; always @(posedge clk) r <= {r[14:0], r[15]^r[14]^r[12]^r[3]}; assign q = r[15]; endmodule', 96.7),
    ('graycnt8', 'module t(input clk, output [7:0] g); reg [7:0] c; always @(posedge clk) c <= c + 1; assign g = c ^ (c >> 1); endmodule', 82.0),
    ('bin2gray8', 'module t(input [7:0] b, output [7:0] g); assign g = b ^ (b >> 1); endmodule', 14.0),
    ('gray2bin8', 'module t(input [7:0] g, output reg [7:0] b); integer i; always @* begin b[7] = g[7]; for (i = 6; i >= 0; i = i - 1) b[i] = b[i+1] ^ g[i]; end endmodule', 14.0),
    ('satadd_s8', "module t(input signed [7:0] a, b, output signed [7:0] y); wire signed [7:0] s = a + b; wire ovf = (a[7] == b[7]) && (s[7] != a[7]); assign y = ovf ? (a[7] ? 8'h80 : 8'h7f) : s; endmodule", 70.0),
    ('prienc8', 'module t(input [7:0] a, output reg [2:0] y, output v); integer i; always @* begin y = 0; for (i = 0; i < 8; i = i + 1) if (a[i]) y = i; end assign v = |a; endmodule', 25.0),
    ('dec3to8_en', "module t(input [2:0] a, input en, output [7:0] y); assign y = en ? (8'b1 << a) : 8'b0; endmodule", 24.0),
    ('popcount8', 'module t(input [7:0] a, output [3:0] y); integer i; reg [3:0] s; always @* begin s = 0; for (i = 0; i < 8; i = i + 1) s = s + a[i]; end assign y = s; endmodule', 26.0),
    ('abs8', 'module t(input signed [7:0] a, output [7:0] y); assign y = a[7] ? -a : a; endmodule', 40.0),
    ('neg8', 'module t(input [7:0] a, output [7:0] y); assign y = -a; endmodule', 23.3),
    ('cmpchain8', 'module t(input [7:0] a, b, c, output y); assign y = (a < b) && (b < c); endmodule', 58.0),
    ('cam4x8', 'module t(input clk, we, input [1:0] wa, input [7:0] wd, key, output hit, output [3:0] m); reg [7:0] e [0:3]; always @(posedge clk) if (we) e[wa] <= wd; assign m = {e[3]==key, e[2]==key, e[1]==key, e[0]==key}; assign hit = |m; endmodule', 350.0),
    ('rf8x8_2r1w', 'module t(input clk, we, input [2:0] wa, ra1, ra2, input [7:0] wd, output [7:0] rd1, rd2); reg [7:0] m [0:7]; always @(posedge clk) if (we) m[wa] <= wd; assign rd1 = m[ra1]; assign rd2 = m[ra2]; endmodule', 800.0),
    ('rotl16', 'module t(input [15:0] a, input [3:0] s, output [15:0] y); wire [15:0] x0 = s[0] ? {a[14:0], a[15]} : a; wire [15:0] x1 = s[1] ? {x0[13:0], x0[15:14]} : x0; wire [15:0] x2 = s[2] ? {x1[11:0], x1[15:12]} : x1; assign y = s[3] ? {x2[7:0], x2[15:8]} : x2; endmodule', 149.0),
    ('crc8_serial', "module t(input clk, rst, d, output reg [7:0] c); wire x = c[7] ^ d; always @(posedge clk) if (rst) c <= 8'h00; else c <= {c[6:0], 1'b0} ^ (x ? 8'h07 : 8'h00); endmodule", 56.0),
    ('booth8', "module t(input [7:0] a, input [7:0] b, output [15:0] y); wire [8:0] bb = {b, 1'b0}; reg [15:0] acc; reg [15:0] pp; integer i; always @* begin acc = 0; for (i = 0; i < 4; i = i + 1) begin case (bb[2*i +: 3]) 3'b000, 3'b111: pp = 0; 3'b001, 3'b010: pp = {8'b0, a}; 3'b011: pp = {7'b0, a, 1'b0}; 3'b100: pp = -{7'b0, a, 1'b0}; default: pp = -{8'b0, a}; endcase acc = acc + (pp << (2*i)); end end assign y = acc; endmodule", 567.0),
    ('johnson8', 'module t(input clk, output q); reg [7:0] r; always @(posedge clk) r <= {r[6:0], ~r[7]}; assign q = r[0]; endmodule', 46.0),
    ('ring8', "module t(input clk, rst, output [7:0] q); reg [7:0] r; always @(posedge clk or posedge rst) if (rst) r <= 8'h01; else r <= {r[6:0], r[7]}; assign q = r; endmodule", 53.4),
    ('min8', 'module t(input [7:0] a, b, output [7:0] y); assign y = (a < b) ? a : b; endmodule', 47.0),
]
REFERENCE += REFERENCE_ROUND2


def cost(src: str, name: str = "t.v") -> dict:
    p = Project({name: src}, git=False)
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


def diag_list(src: str, name: str = "t.v") -> list:
    p = Project({name: src}, git=False)
    try:
        code, text = p.run("cost", "--format", "json", "--no-meta", "--no-cache")
    finally:
        p.close()
    assert code == 0, text
    return json.loads(text)["diagnostics"]


def diag_codes(src: str, name: str = "t.v") -> list:
    return [d["code"] for d in diag_list(src, name)]


class MemoryMacroTransition(unittest.TestCase):
    """A synchronous-read memory is min(flop array, smallest-macro-padded macro): no cliff at macro_min_bits."""

    def test_no_cost_drop_when_depth_grows(self):
        from gatecost.cost import cost_op, load_profile
        P = load_profile()
        for w in (1, 8, 32, 128):
            prev = None
            for d in range(1, 4097):
                c = cost_op("MEM", {"depth": d, "width": w, "sync_read": True}, P)
                cur = (c.min, c.typ, c.max)
                if prev is not None:
                    for s, a, b in zip(("min", "typ", "max"), prev, cur):
                        self.assertLessEqual(a, b, f"w={w} {s} drops at depth {d}: {prev} -> {cur}")
                prev = cur

    def test_127_vs_128_words(self):
        def mem(d):
            return ("module t(input clk, we, input [6:0] wa, ra, input [31:0] wd, output reg [31:0] rd); "
                    "reg [31:0] m [0:%d]; always @(posedge clk) begin if (we) m[wa] <= wd; rd <= m[ra]; end endmodule" % (d - 1))
        a, b = typ(mem(127)), typ(mem(128))
        self.assertLessEqual(a, b)
        self.assertLess(b / a, 1.1)        # was 42.3k -> 6.9k

    def test_small_memory_stays_flops(self):
        from gatecost.cost import cost_op, load_profile
        self.assertEqual(cost_op("MEM", {"depth": 16, "width": 8, "sync_read": True}, load_profile()).arch["typ"], "flop_array")


class RomStyles(unittest.TestCase):
    """Case tables, constant arrays and initial-initialised arrays are the same ROM."""

    @staticmethod
    def vals(n=64, w=16):
        import random
        r = random.Random(7)
        return [r.getrandbits(w) for _ in range(n)]

    def sources(self):
        v = self.vals()
        case = ("module t(input [5:0] a, output reg [15:0] y); always @* case (a)\n"
                + "\n".join("%d: y = 16'h%04x;" % (i, x) for i, x in enumerate(v)) + "\ndefault: y = 0; endcase endmodule")
        lp = ("module t(input [5:0] a, output [15:0] y); localparam logic [15:0] T [0:63] = '{"
              + ",".join("16'h%04x" % x for x in v) + "}; assign y = T[a]; endmodule")
        ini = ("module t(input [5:0] a, output [15:0] y); reg [15:0] m [0:63]; initial begin\n"
               + "\n".join("m[%d] = 16'h%04x;" % (i, x) for i, x in enumerate(v)) + "\nend assign y = m[a]; endmodule")
        vh = ("library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;\n"
              "entity t is port(a: in std_logic_vector(5 downto 0); y: out std_logic_vector(15 downto 0)); end;\n"
              "architecture r of t is type rt is array(0 to 63) of std_logic_vector(15 downto 0); constant T: rt := ("
              + ",".join('x"%04x"' % x for x in v) + ");\nbegin y <= T(to_integer(unsigned(a))); end;")
        return {"case": (case, "t.v"), "localparam": (lp, "t.v"), "initial": (ini, "t.v"), "vhdl": (vh, "t.vhd")}

    def test_all_styles_cost_the_same_rom(self):
        costs = {k: cost(s, n)["total"]["ge"]["typ"] for k, (s, n) in self.sources().items()}
        base = costs["case"]
        for k, c in costs.items():
            self.assertAlmostEqual(c, base, delta=0.01 * base, msg=str(costs))
        # textbook: shared decoder (~100 GE) + 16 OR planes x 32 set words x ~0.55 GE per merged input
        self.assertTrue(250 <= base <= 600, costs)

    def test_initial_loop_filled_rom(self):
        src = ("module t(input [5:0] a, output [15:0] y); reg [15:0] m [0:63]; integer i; "
               "initial for (i = 0; i < 64; i = i + 1) m[i] = i * 37 + 5; assign y = m[a]; endmodule")
        self.assertLess(typ(src), 500)         # a mux tree of 64 x 16 would be 2.3k GE

    def test_readmemh_array_is_an_unknown_content_rom(self):
        src = ('module t(input clk, input [9:0] a, output reg [15:0] y); reg [15:0] m [0:1023]; '
               'initial $readmemh("x.hex", m); always @(posedge clk) y <= m[a]; endmodule')
        got = typ(src)
        self.assertTrue(2000 <= got <= 20000, got)       # ~0.3 GE per bit, not 10 GE per bit of flops

    def test_large_random_rom_is_about_a_third_of_a_ge_per_bit(self):
        from gatecost.cost import cost_op, load_profile
        c = cost_op("ROM", {"depth": 1024, "width": 32}, load_profile()).typ / 1000 / (1024 * 32)
        self.assertTrue(0.25 <= c <= 0.45, c)       # decoder + OR planes (was 0.2: Lupanov-like 2D/log2 D)


class DividerAndSigned(unittest.TestCase):
    def test_div16_is_a_triangular_array(self):
        got = typ("module t(input [15:0] a, b, output [15:0] y); assign y = a / b; endmodule")
        self.assertTrue(1100 <= got <= 1700, got)         # textbook ~1450 (was 2455: rectangular 16 x 17 cells)

    def test_signed_multiply_keeps_the_sign_correction(self):
        u = typ("module t(input [7:0] a, b, output [15:0] y); assign y = a * b; endmodule")
        s = typ("module t(input signed [7:0] a, b, output signed [15:0] y); assign y = a * b; endmodule")
        self.assertGreater(s, u)
        self.assertLess(s, u + 60)

    def test_signed_mac_keeps_the_sign_correction(self):
        u = typ("module t(input clk, input [7:0] a,b, output reg [23:0] acc); always @(posedge clk) acc <= acc + a*b; endmodule")
        s = typ("module t(input clk, input signed [7:0] a,b, output reg signed [23:0] acc); always @(posedge clk) acc <= acc + a*b; endmodule")
        self.assertGreater(s, u)


class MuxIdentities(unittest.TestCase):
    def sel(self, body):
        return category("module t(input s, input [7:0] x, z, output [7:0] y); assign y = %s; endmodule" % body, "select")

    def test_mux_of_x_and_not_x_is_xor2(self):
        self.assertAlmostEqual(self.sel("s ? ~x : x"), 8 * 2.0, delta=0.01)
        self.assertAlmostEqual(self.sel("s ? x : ~x"), 8 * 2.0, delta=0.01)

    def test_identical_arms_are_a_wire(self):
        self.assertEqual(self.sel("s ? x : x"), 0)

    def test_constant_arm_is_and_or(self):
        self.assertAlmostEqual(self.sel("s ? x : 8'h00"), 8 * 1.33, delta=0.01)
        self.assertAlmostEqual(self.sel("s ? 8'hff : x"), 8 * 1.33, delta=0.01)

    def test_select_as_arm_is_and_or(self):
        self.assertAlmostEqual(self.sel("s ? {8{s}} : x"), 8 * 1.33, delta=0.01)
        self.assertAlmostEqual(self.sel("s ? x : {8{s}}"), 8 * 1.33, delta=0.01)

    def test_plain_mux_unchanged(self):
        self.assertAlmostEqual(self.sel("s ? z : x"), 8 * 2.33, delta=0.01)

    def test_identities_are_semantically_exact(self):
        # s ? ~x : x == x ^ s ; s ? s : x == s | x ; s ? x : s == s & x   (the rewrites the cost model assumes)
        for s in (0, 1):
            for x in (0, 1):
                self.assertEqual((1 - x) if s else x, x ^ s)
                self.assertEqual(s if s else x, s | x)
                self.assertEqual(x if s else s, s & x)


class UnobservableLogic(unittest.TestCase):
    BASE = "module t(input clk, input [7:0] d, output reg [7:0] q); always @(posedge clk) q <= d; endmodule"

    def test_unread_counter_changes_nothing_and_is_reported(self):
        src = ("module t(input clk, input [7:0] d, output reg [7:0] q); reg [15:0] c; "
               "always @(posedge clk) begin q <= d; c <= c + 1; end endmodule")
        self.assertEqual(typ(src), typ(self.BASE))
        self.assertIn("UNOBSERVABLE_LOGIC", diag_codes(src))
        self.assertNotIn("UNOBSERVABLE_LOGIC", diag_codes(self.BASE))

    def test_diagnostic_is_info_and_does_not_affect_cost(self):
        src = ("module t(input clk, input [7:0] d, output reg [7:0] q); reg [7:0] a, b; "
               "always @(posedge clk) begin q <= d; a <= d + 1; b <= a ^ d; end endmodule")
        ds = [d for d in diag_list(src) if d["code"] == "UNOBSERVABLE_LOGIC"]
        self.assertEqual(len(ds), 1)
        self.assertEqual((ds[0]["severity"], ds[0]["affects_cost"]), ("info", False))
        self.assertIn("2 unobservable registers", ds[0]["message"])

    def test_write_only_memory_costs_nothing(self):
        src = ("module t(input clk, we, input [3:0] a, input [7:0] d, output [7:0] y); reg [7:0] m [0:15]; "
               "always @(posedge clk) if (we) m[a] <= d; assign y = d; endmodule")
        self.assertEqual(typ(src), 0)

    def test_unused_wire_logic_is_removed(self):
        src = "module t(input [7:0] a, b, output [7:0] y); wire [15:0] p = a * b; assign y = a + b; endmodule"
        self.assertEqual(typ(src), typ("module t(input [7:0] a, b, output [7:0] y); assign y = a + b; endmodule"))
        self.assertIn("UNOBSERVABLE_LOGIC", diag_codes(src))


class PopcountAndCam(unittest.TestCase):
    def test_popcount_is_a_bit_heap_not_two_rows_plus_cpa(self):
        src = ("module t(input [7:0] a, output [3:0] y); integer i; reg [3:0] s; "
               "always @* begin s = 0; for (i = 0; i < 8; i = i + 1) s = s + a[i]; end assign y = s; endmodule")
        self.assertLess(typ(src), 40)         # 4 FA + a few HA (was 54.5)

    def test_constant_address_reads_have_no_mux_tree(self):
        src = ("module t(input clk, we, input [1:0] wa, input [7:0] wd, output [7:0] a, b); reg [7:0] e [0:3]; "
               "always @(posedge clk) if (we) e[wa] <= wd; assign a = e[0]; assign b = e[3]; endmodule")
        self.assertLess(typ(src), 4 * 8 * 8.0 + 30)      # storage + write decode only


if __name__ == "__main__":
    unittest.main()
