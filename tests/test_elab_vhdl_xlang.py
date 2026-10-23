"""Paired Verilog / VHDL descriptions of the same circuit yield the same normalized IR dump (FRONTEND.md 10.7).

The Verilog side goes through the real ``gatecost.elab.elaborate_project``; the VHDL side through the hook
``gatecost.elab.vhdl.elaborate_vhdl`` of the same function (files with the ``.vhd`` extension).
"""
import os
import tempfile
import unittest

from gatecost.elab import elaborate_project
from gatecost.ir import dump

from tests.test_elab_vhdl_util import unit


def both(vsrc, vhsrc, top="t"):
    with tempfile.TemporaryDirectory() as td:
        for name, text in (("a.v", vsrc), ("a.vhd", vhsrc)):
            with open(os.path.join(td, name), "w") as fh:
                fh.write(text)
        rv = elaborate_project(["a.v"], root=td, top=top)
        rh = elaborate_project(["a.vhd"], root=td, top=top)
    return rv, rh


def dumps(rv, rh):
    return dump(rv.design.modules[rv.top_key], normalize=True), dump(rh.design.modules[rh.top_key], normalize=True)


LEAF = ("entity leaf is generic (W : natural := 4); port (a : in std_logic_vector(W - 1 downto 0); y : out std_logic_vector(W - 1 downto 0)); end entity;\n"
        "architecture r of leaf is begin y <= not a; end architecture;\n")

PAIRS = {
    "adder_tree": (
        "module t(input [7:0] a,b,c,d, output [7:0] y); assign y = (a + b) + (c + d); endmodule",
        unit("a, b, c, d : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", "", "y <= (a + b) + (c + d);")),
    "multiplier": (
        "module t(input [7:0] a, input [7:0] b, output [15:0] p); assign p = a * b; endmodule",
        unit("a, b : in unsigned(7 downto 0); p : out unsigned(15 downto 0)", "", "p <= a * b;")),
    "ternary_mux": (
        "module t(input [7:0] a,b, input s, output [7:0] y); assign y = s ? a : b; endmodule",
        unit("a, b : in std_logic_vector(7 downto 0); s : in std_logic; y : out std_logic_vector(7 downto 0)", "", "y <= a when s = '1' else b;")),
    "logic_and_compare": (
        "module t(input [7:0] a,b, output lt, output [7:0] y); assign lt = a < b; assign y = (a & b) | (a ^ b); endmodule",
        unit("a, b : in unsigned(7 downto 0); lt : out std_logic; y : out unsigned(7 downto 0)", "",
             "lt <= '1' when a < b else '0'; y <= (a and b) or (a xor b);")),
    "alu_case": (
        "module t(input [7:0] a, input [7:0] b, input [1:0] op, output reg [7:0] y);\n"
        " always @* case(op) 2'b00: y = a & b; 2'b01: y = a | b; 2'b10: y = a ^ b; default: y = a + b; endcase\nendmodule",
        unit("a, b : in std_logic_vector(7 downto 0); op : in std_logic_vector(1 downto 0); y : out std_logic_vector(7 downto 0)", "",
             "process(all) begin case op is when \"00\" => y <= a and b; when \"01\" => y <= a or b; when \"10\" => y <= a xor b; "
             "when others => y <= std_logic_vector(unsigned(a) + unsigned(b)); end case; end process;")),
    "priority_if": (
        "module t(input a, c, input [3:0] b, d, output reg [3:0] y); always @* if (a) y = b; else if (c) y = d; else y = 4'b0; endmodule",
        unit("a, c : in std_logic; b, d : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)", "",
             "process(all) begin if a = '1' then y <= b; elsif c = '1' then y <= d; else y <= (others => '0'); end if; end process;")),
    "latch": (
        "module t(input en, input [3:0] d, output reg [3:0] q); always @* if (en) q = d; endmodule",
        unit("en : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0)", "",
             "process(all) begin if en = '1' then q <= d; end if; end process;")),
    "tristate": (
        "module t(input en, input [3:0] d, output [3:0] q); assign q = en ? d : 4'bzzzz; endmodule",
        unit("en : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0)", "",
             "q <= d when en = '1' else (others => 'Z');")),
    "counter_sync_reset_enable": (
        "module t #(parameter w=8)(input clk, input rst, input en, output [w-1:0] q);\n reg [w-1:0] c;\n"
        " always @(posedge clk) if (rst) c <= 0; else if (en) c <= c + {{(w-1){1'b0}},1'b1};\n assign q = c;\nendmodule",
        unit("clk, rst, en : in std_logic; q : out std_logic_vector(W - 1 downto 0)", "signal c : unsigned(W - 1 downto 0);",
             "process(clk) begin if rising_edge(clk) then if rst = '1' then c <= (others => '0'); elsif en = '1' then c <= c + 1; end if; end if; end process;"
             " q <= std_logic_vector(c);", generics="W : natural := 8")),
    "async_reset_register": (
        "module t(input clk, rstn, input [3:0] d, output reg [3:0] q); always @(posedge clk or negedge rstn) if (!rstn) q <= 4'h5; else q <= d; endmodule",
        unit("clk, rstn : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0)", "",
             "process(clk, rstn) begin if rstn = '0' then q <= x\"5\"; elsif rising_edge(clk) then q <= d; end if; end process;")),
    "shift_register": (
        "module t(input clk, input d, output [3:0] q); reg [3:0] sr; always @(posedge clk) sr <= {sr[2:0], d}; assign q = sr; endmodule",
        unit("clk, d : in std_logic; q : out std_logic_vector(3 downto 0)", "signal sr : std_logic_vector(3 downto 0);",
             "process(clk) begin if rising_edge(clk) then sr <= sr(2 downto 0) & d; end if; end process; q <= sr;")),
    "fsm": (
        "module t(input clk, input rst, input go, output busy);\n localparam [1:0] IDLE=2'd0, RUN=2'd1, DONE=2'd2; reg [1:0] st;\n"
        " always @(posedge clk) if (rst) st <= IDLE; else case (st) IDLE: if (go) st <= RUN; RUN: st <= DONE; DONE: st <= IDLE; endcase\n"
        " assign busy = st == RUN;\nendmodule",
        unit("clk, rst, go : in std_logic; busy : out std_logic", "type st_t is (idle, run, done); signal st : st_t;",
             "process(clk) begin if rising_edge(clk) then if rst = '1' then st <= idle; else case st is when idle => if go = '1' then st <= run; end if; "
             "when run => st <= done; when done => st <= idle; end case; end if; end if; end process; busy <= '1' when st = run else '0';")),
    "register_file": (
        "module t(input clk, input we, input [3:0] wa, input [3:0] ra, input [7:0] di, output [7:0] dout);\n reg [7:0] ram [0:15];\n"
        " always @(posedge clk) if (we) ram[wa] <= di;\n assign dout = ram[ra];\nendmodule",
        unit("clk, we : in std_logic; wa, ra : in unsigned(3 downto 0); di : in std_logic_vector(7 downto 0); dout : out std_logic_vector(7 downto 0)",
             "type ram_t is array (0 to 15) of std_logic_vector(7 downto 0); signal ram : ram_t;",
             "process(clk) begin if rising_edge(clk) then if we = '1' then ram(to_integer(wa)) <= di; end if; end if; end process; dout <= ram(to_integer(ra));")),
    "hierarchy": (
        "module leaf #(parameter w=4)(input [w-1:0] a, output [w-1:0] y); assign y = ~a; endmodule\n"
        "module t(input [7:0] x, output [7:0] z); wire [7:0] m; leaf #(.w(8)) u1(.a(x), .y(m)); leaf #(.w(8)) u2(.a(m), .y(z)); endmodule",
        unit("x : in std_logic_vector(7 downto 0); z : out std_logic_vector(7 downto 0)", "signal m : std_logic_vector(7 downto 0);",
             "u1 : entity work.leaf generic map (W => 8) port map (a => x, y => m); u2 : entity work.leaf generic map (W => 8) port map (a => m, y => z);",
             extra=LEAF)),
}


class CrossLanguage(unittest.TestCase):
    def test_pairs_have_identical_normalized_dumps(self):
        for name, (v, vh) in PAIRS.items():
            with self.subTest(pair=name):
                rv, rh = both(v, vh)
                a, b = dumps(rv, rh)
                self.assertEqual(a, b)

    def test_pairs_have_equal_canonical_hashes_and_instance_counts(self):
        for name in ("alu_case", "register_file", "hierarchy", "fsm"):
            with self.subTest(pair=name):
                rv, rh = both(*PAIRS[name])
                self.assertEqual(rv.design.modules[rv.top_key].canonical_hash, rh.design.modules[rh.top_key].canonical_hash)
                self.assertEqual(sorted(rv.design.instance_counts().values()), sorted(rh.design.instance_counts().values()))


if __name__ == "__main__":
    unittest.main()
