"""VHDL null ranges (``p(2 to L)`` with L = 1, ``v(2 downto 3)``) denote no element: a slice target with a null
range assigns nothing and a null slice read yields an empty value.  They used to be normalized with min/max and
clamped onto a real element (``p(1) <= p(1)``), which silently erased the logic upstream of the pipeline.

Verilog has no null part-select: a reversed one (``f[31:32]``) is illegal and taken swapped like Yosys; it must
at least be reported."""
import tempfile
import unittest
from pathlib import Path

from gatecost.cost import build_cost_tree, load_profile
from gatecost.elab import elaborate_project
from gatecost.ir import dump

P = load_profile()

HDR = "library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;\n"

NULL_SLICE_ADDER = HDR + """
entity null_slice_adder is
  generic (Latency_g : positive := 1);
  port (clk : in std_logic; d, e : in std_logic_vector(31 downto 0); q : out std_logic_vector(31 downto 0));
end entity;
architecture rtl of null_slice_adder is
  type arr_t is array (natural range <>) of std_logic_vector(31 downto 0);
  signal p : arr_t(1 to Latency_g);
begin
  process (clk) begin
    if rising_edge(clk) then
      p(1) <= std_logic_vector(unsigned(d) + unsigned(e));
      p(2 to Latency_g) <= p(1 to Latency_g-1);
    end if;
  end process;
  q <= p(Latency_g);
end architecture;
"""

NULL_SLICE_PIPE = HDR + """
entity null_slice_pipe is
  generic (Latency_g : positive := 1);
  port (clk : in std_logic; d : in std_logic_vector(31 downto 0); q : out std_logic_vector(31 downto 0));
end entity;
architecture rtl of null_slice_pipe is
  type arr_t is array (natural range <>) of std_logic_vector(31 downto 0);
  signal p : arr_t(1 to Latency_g);
begin
  process (clk) begin
    if rising_edge(clk) then
      p(1) <= d;
      p(2 to Latency_g) <= p(1 to Latency_g-1);
    end if;
  end process;
  q <= p(Latency_g);
end architecture;
"""

RAM_RDPIPE = HDR + """
entity ram_rdpipe is
  generic (Depth_g : positive := 64; Width_g : positive := 16; RdLatency_g : positive := 1);
  port (Clk : in std_logic; Wr_Ena : in std_logic;
        Wr_Addr, Rd_Addr : in std_logic_vector(5 downto 0);
        Wr_Data : in std_logic_vector(Width_g-1 downto 0);
        Rd_Data : out std_logic_vector(Width_g-1 downto 0));
end entity;
architecture rtl of ram_rdpipe is
  type Data_t is array (natural range <>) of std_logic_vector(Width_g - 1 downto 0);
  shared variable Mem_v : Data_t(Depth_g - 1 downto 0) := (others => (others => '0'));
  signal RdPipe : Data_t(1 to RdLatency_g);
begin
  Rd_Data <= RdPipe(RdLatency_g);
  process (Clk) begin
    if rising_edge(Clk) then
      if Wr_Ena = '1' then Mem_v(to_integer(unsigned(Wr_Addr))) := Wr_Data; end if;
      RdPipe(1) <= Mem_v(to_integer(unsigned(Rd_Addr)));
      RdPipe(2 to RdLatency_g) <= RdPipe(1 to RdLatency_g-1);
    end if;
  end process;
end architecture;
"""


def run(text, name="m.vhd", params=None):
    with tempfile.TemporaryDirectory() as td:
        Path(td, name).write_text(text)
        return elaborate_project([Path(name)], root=Path(td), params=params)


def top(res):
    return res.design.modules[res.top_key]


def ops(m):
    out: dict = {}
    for n in m.nodes:
        out[n.op] = out.get(n.op, 0) + 1
    return out


def assumptions(res):
    out = list(res.assumptions)
    for m in res.design.modules.values():
        out += list(m.assumptions)
    return out


def typ(res):
    return build_cost_tree(res.design, P).total().typ


def unit(decls, body, ports="d : in std_logic_vector(7 downto 0); q : out std_logic_vector(7 downto 0)",
         generics=""):
    g = "generic (%s);\n" % generics if generics else ""
    return HDR + "entity t is\n%sport (%s);\nend entity;\narchitecture a of t is\n%s\nbegin\n%s\nend architecture;\n" \
        % (g, ports, decls, body)


class NullSliceTarget(unittest.TestCase):
    def test_null_slice_target_keeps_the_adder(self):
        r = run(NULL_SLICE_ADDER)
        m = top(r)
        self.assertEqual(ops(m), {"ADD": 1, "DFF": 1})
        self.assertFalse([a for a in assumptions(r) if "slice" in a or "width mismatch" in a], assumptions(r))
        self.assertIn("DFF(D=", dump(m))
        self.assertGreater(typ(r), 350)                  # was 181 (registers only): Yosys 410 GE

    def test_null_slice_target_keeps_the_memory_read(self):
        r = run(RAM_RDPIPE)
        m = top(r)
        self.assertEqual(ops(m), {"DFF": 1, "MEM": 1})
        self.assertFalse([a for a in assumptions(r) if "slice" in a or "width mismatch" in a], assumptions(r))
        self.assertGreater(typ(r), 9000)                 # was 91 (mem=0): ~10.6k GE without the null slice

    def test_null_slice_target_feeds_the_register_from_the_input(self):
        r = run(NULL_SLICE_PIPE)
        m = top(r)
        self.assertIn("DFF(D=d[31:0]", dump(m))
        self.assertEqual(assumptions(r), [])

    def test_latency_two_is_a_two_stage_pipeline(self):
        r = run(NULL_SLICE_ADDER, params={"latency_g": 2})
        m = top(r)
        self.assertEqual(ops(m).get("ADD"), 1)
        self.assertEqual(sum(n.width for n in m.nodes if n.op == "DFF"), 64)
        self.assertEqual(assumptions(r), [])
        r1 = run(NULL_SLICE_ADDER)
        self.assertGreater(typ(r), typ(r1))

    def test_null_downto_target_assigns_nothing(self):
        src = unit("signal v : std_logic_vector(7 downto 0);",
                   "process (d) begin v <= d; v(2 downto 3) <= x\"00\"(2 downto 3); end process; q <= v;")
        r = run(src)
        self.assertIn("v[7:0] <- d[7:0]\n", dump(top(r)))       # was {d[7:4], 2'h0, d[1:0]}
        self.assertEqual(assumptions(r), [])


    def test_null_formal_slice_in_a_port_map_maps_no_bit(self):
        sub = HDR + ("entity s is port (x : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0));"
                     " end entity;\narchitecture a of s is begin y <= x; end architecture;\n")
        src = sub + unit("signal v : std_logic_vector(7 downto 0);",
                         "u: entity work.s port map (x(7 downto 0) => d, x(3 downto 4) => v(1 downto 2), "
                         "y(7 downto 0) => q, y(0 downto 1) => v(1 downto 2));")
        r = run(src)
        self.assertIn("INST(x=d[7:0])", dump(top(r)))   # was x={d[7:5], 2'h0, d[2:0]}
        self.assertEqual(assumptions(r), [])

    def test_null_memory_slice_shift_writes_no_word(self):
        src = unit("constant L : natural := 1;\n"
                   "type m_t is array (0 to 15) of std_logic_vector(7 downto 0);\nsignal m : m_t;",
                   "process (clk) begin if rising_edge(clk) then m(0) <= d; m(1 to L-1) <= m(0 to L-2); "
                   "end if; end process; q <= m(0);",
                   ports="clk : in std_logic; d : in std_logic_vector(7 downto 0); "
                         "q : out std_logic_vector(7 downto 0)")
        r = run(src)
        self.assertEqual(assumptions(r), [])
        self.assertNotIn("INTERNAL", [d.code for d in r.diagnostics])


class NullSliceRead(unittest.TestCase):
    def test_null_read_contributes_no_bit(self):
        src = unit("constant L : natural := 0;",
                   "q <= d(7 downto 0) & d(L-1 downto 0);")
        r = run(src)
        self.assertIn("q[7:0] <- d[7:0]", dump(top(r)))
        self.assertEqual(assumptions(r), [])

    def test_null_ascending_read_in_a_concatenation(self):
        src = unit("type a_t is array (natural range <>) of std_logic_vector(7 downto 0);\n"
                   "signal p : a_t(1 to 1);",
                   "p(1) <= d; q <= p(1) when p(1 to 0)'length = 0 else (others => '0');")
        r = run(src)
        self.assertEqual(assumptions(r), [])

    def test_real_out_of_range_slice_is_still_reported(self):
        src = unit("signal w : std_logic_vector(9 downto 0);",
                   "w <= \"00\" & d; q <= w(11 downto 4);",
                   ports="d : in std_logic_vector(7 downto 0); q : out std_logic_vector(7 downto 0)")
        r = run(src)
        self.assertTrue(any("outside" in a for a in assumptions(r)), assumptions(r))

    def test_real_out_of_range_target_is_reported(self):
        src = unit("signal w : std_logic_vector(7 downto 0);",
                   "process (d) begin w <= d; w(9 downto 6) <= \"0000\"; end process; q <= w;")
        r = run(src)
        self.assertTrue(any("target slice" in a and "outside" in a for a in assumptions(r)), assumptions(r))


class NullChoiceAndAttributes(unittest.TestCase):
    def test_null_range_choice_in_an_aggregate_names_no_element(self):
        src = unit("constant L : natural := 1;",
                   "q <= (7 downto L+7 => '1', others => '0');")
        r = run(src)
        self.assertIn("q[7:0] <- 8'h0\n", dump(top(r)))

    def test_high_and_low_of_a_null_range_follow_the_direction(self):
        src = unit("type a_t is array (natural range <>) of std_logic;\nsignal n : a_t(1 to 0);",
                   "q <= std_logic_vector(to_unsigned(n'high * 16 + n'low, 8));")
        r = run(src)
        self.assertIn("q[7:0] <- 8'h1\n", dump(top(r)))     # 'high = 0, 'low = 1


class VerilogReversedPartSelect(unittest.TestCase):
    SRC = """module b #(parameter LAT=1) (input clk, input [31:0] d, e, output [31:0] q);
  reg [32*LAT-1:0] f;
  always @(posedge clk) begin
    f[31:0] <= d + e;
    f[32*LAT-1:32] <= f[32*LAT-33:0];
  end
  assign q = f[32*LAT-1 -: 32];
endmodule
"""

    def test_reversed_part_select_is_reported(self):
        r = run(self.SRC, "b.v")
        self.assertTrue(any("reversed" in a for a in assumptions(r)), assumptions(r))

    def test_in_range_part_select_is_silent(self):
        r = run(self.SRC, "b.v", params={"LAT": 2})
        self.assertFalse([a for a in assumptions(r) if "part-select" in a], assumptions(r))


if __name__ == "__main__":
    unittest.main()
