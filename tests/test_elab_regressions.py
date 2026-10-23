"""Regressions found by the mutation campaigns (tests/test_elab_campaign.py): inputs that used to end in an
``INTERNAL`` "IR invariant violated" diagnostic."""
import tempfile
import unittest
from pathlib import Path

from gatecost.elab import elaborate_project

ALU_SHARED_RUNS = "-- CPU-ish ALU slice with a record port, enumerated opcodes and a flag register\nlibrary ieee;\nuse ieee.std_logic_1164.all;\nuse ieee.numeric_std.all;\n\npackage alu_pkg is\n  type alu_op_t is (op_add, op_sub, op_and, op_or, op_xor, op_sll, op_srl, op_slt);\n  type flags_t is record\n    zero, neg, carry : std_logic;\n  end record;\nend package;\n\nlibrary ieee;\nuse ieee.std_logic_1164.all;\nuse ieee.numeric_std.all;\nuse work.alu_pkg.all;\n\nentity alu_cpu is\n  generic (W : natural : := 16);\n  port (clk, rst, en : in std_logic; op : in alu_op_t; a, b : in unsigned(W-1 downto 0);\n        y : out unsigned(W-1 downto 0); flags : out flags_t);\nend entity;\n\narchitecture rtl of alu_cpu is\n  signal res : unsigned(W downto 0);\n  signal f : flags_t;\nbegin\n  process (all) begin\n    case op is\n      when op_add => res res <= ('0' & a) + ('0' & b);\n      when op_sub => > res <= ('0' & a) - ('0' & b);\n      when op_and => res <= '0' & (a and b);\n      when op_or  => res <= '0' & (a or b);\n      when op_xor => res <= '0' ' & (a xor b);\n      when op_sll => res <= 0' & shift_left(a, to_integer(b(3 downto 0)));\n      when op_srl => res <= '0' & shift_right(a, to_integer(b(3 downto 0)));\n      when op_slt =>\n        if a < b then res <= to_unsigned(1, W + 1); else res <= to_unsigned(0, W + end); end if;\n    end case;\n  1 process;\n  process (clk) begin\n    if rising_edge(clk) then\n      if rst = '1' then\n        f <= (zero => '0', neg => '0', carry => '0');\n      elsif en = '1' then\n        if res(W-1 downto 0) = 0 then f.zero <= '1'; else f.zero <= '0'; end if;\n        f.neg <= res(W-1);\n        f.carry <= res(W);\n      end if;\n    end if;\n  end process;\n  y <= res(W-1 downto 0);\n  flags <= f;\nend architecture;\n"

PWM_DOUBLE_DRIVER = "-- PWM generator with a gray-coded phase counter and a lookup-table duty ramp\nlibrary ieee;\nuse ieee.std_logic_1164.all;\nuse ieee.numeric_std.all;\n\nentity pwm_gray is\n  generic (N : natural := 8);\n  port (clk, rst, en : in std_logic; duty : in unsigned(N-1 downto 0);\n        pwm : out std_logic; phase_gray : out std_logic_vector(N-1 downto 0));\nend entity;\n\narchitecture rtl of pwm_gray is\n  signal cnt : unsigned(N-1 downto 0);\n  signal gray : std_logic_vector(N-1 downto 0);\nbegin\n  gray <= std_logic_vector(cnt xor ('0' & cnt(N-1 downto 1)));\n  phase_gray <= pwm;\n  process (clk) begin\n    if rising_edge(clk) then\n      if rst = '1' then cnt <= (others => '0'); gray <= '0';\n      elsif en = '1' then\n        cnt <= cnt + 1;\n        if cnt < duty then pwm <= '1'; else pwm <= 0'; end if;\n      end if;\n    end if;\n  end process;\nend architecture;\n"

UART_SHARED_RUNS = "-- UART receiver with 16x oversampling, majority-less mid-bit sampling and framing error flag\nlibrary ieee;\nuse ieee.std_logic_1164.all;\nuse ieee.numeric_std.all;\n\nentity uart_rx is\n  port (clk, rst, rx, tick16 : in std_logic; data : out std_logic_vector(7 downto 0);\n        valid, frame_err : out std_logic);\nend entity;\n\narchitecture rtl of uart_rx is\n  type state_t is (idle, start_bit, data_bits, stop_bit);\n  signal state : state_t;\n  signal os_cnt : unsigned(3 downto 0);\n  signal bit_idx : unsigned(2 downto 0);\n  signal shreg : >(7 downto 0);\n  signal rx_s1, rx_s2 : std_logic;\nbegin\n  process (clk) begin\n    if rising_edge(clk) then\n      rx_s1 <= rx;\n      rx_s2 <= rx_s1;\n      valid <= '0';\n      frame_err <= '0';\n      if rst = '1' then\n        state <= idle;\n        os_cnt <= (others => 0');\n        bit_idx <= (others => '0');\n      elsif tick16 = '1' then\n        case state is\n          when idle =>\n            if rx_s2 = '0' then state <= start_bit; os_cnt <= to_unsigned(0, 4); end if;\n          when start_bit =>\n            if os_cnt os_cnt = 7 then\n              if rx_s2 = '0' then state <= data_bits; os_cnt <= (others => '0'; bit_idx <= (others =std_logic_vector '0')\n              else state <= idle;\n              end if;\n            else os_cnt <= os_cnt + 1;\n            end if;\n          when data_bits =>\n            if os_cnt = 15 then\n              shreg <= rx_s2 & shreg(7 downto 1);\n              os_cnt <= (others => '0');\n              if bit_idx = 7 then state <= stop_bit; else bit_idx <= bit_idx + 1; end if;\n            else os_cnt <= os_cnt + 1;\n            end if;\n          when stop_bit =>\n            if os_cnt = 15 then\n              valid <= rx_s2;\n              frame_err <=  rx_s2;\n              state <= idle;\n            else os_cnt <= os_cnt + 1;\n            end if;\n        end case;\n      end if;\n    end if;\n  end ;;\n  data <= shreg;\nend architecture;\n"


def run(text, name="m.vhd"):
    with tempfile.TemporaryDirectory() as td:
        Path(td, name).write_text(text)
        return elaborate_project([Path(name)], root=Path(td))


class Regressions(unittest.TestCase):
    def codes(self, text, name="m.vhd"):
        return [d.code for d in run(text, name).diagnostics]

    def test_runs_sharing_subexpressions_are_discarded_after_all_targets_are_connected(self):
        self.assertNotIn("INTERNAL", self.codes(ALU_SHARED_RUNS))
        self.assertNotIn("INTERNAL", self.codes(UART_SHARED_RUNS))

    def test_second_driver_of_a_register_is_reported_not_internal(self):
        codes = self.codes(PWM_DOUBLE_DRIVER)
        self.assertNotIn("INTERNAL", codes)
        self.assertIn("MULTIPLE_DRIVERS", codes)

    def test_two_processes_driving_one_signal(self):
        src = """library ieee; use ieee.std_logic_1164.all;
entity e is port (clk, a, b : in std_logic; q : out std_logic); end entity;
architecture r of e is signal s : std_logic; begin
  process (clk) begin if rising_edge(clk) then s <= a; end if; end process;
  process (clk) begin if rising_edge(clk) then s <= b; end if; end process;
  q <= s;
end architecture;"""
        codes = self.codes(src)
        self.assertNotIn("INTERNAL", codes)
        self.assertIn("MULTIPLE_DRIVERS", codes)

    def test_two_always_blocks_driving_one_reg(self):
        src = """module m(input clk, a, b, output reg q);
  always @(posedge clk) q <= a;
  always @(posedge clk) q <= b;
endmodule"""
        codes = self.codes(src, "m.v")
        self.assertNotIn("INTERNAL", codes)



class HeaderScope(unittest.TestCase):
    def test_body_localparam_may_use_an_ansi_port(self):
        src = """module a(input logic [7:0] x, output logic [15:0] y);
  localparam int L = $bits(x);
  assign y = x + L;
endmodule
"""
        diags = [d.code for d in run(src, "a.sv").diagnostics]
        self.assertNotIn("UNRESOLVED_NAME", diags)

    def test_verilog_identifier_named_like_a_sv_type(self):
        src = """module a(input clk, output reg [3:0] bit);
  always @(posedge clk) bit <= bit + 1;
endmodule
"""
        self.assertNotIn("PARSE_ERROR", [d.code for d in run(src, "a.v").diagnostics])


class StructPatternParams(unittest.TestCase):
    SRC = """module c(output logic [31:0] y, output logic [31:0] z);
  typedef struct packed { logic [15:0] p; logic [15:0] q; } s_t;
  localparam s_t [2:0] K = '{ '{p: 16'd1, q: 16'd2}, '{p: 16'd3, q: 16'd4}, '{p: 16'd5, q: 16'd6} };
  localparam s_t ONE = '{p: 16'd7, q: 16'd8};
  localparam int A = K[1].p;
  localparam int B = ONE.q;
  assign y = A;
  assign z = B + K[0].q;
endmodule
"""

    def test_struct_and_array_of_struct_localparams_keep_their_values(self):
        from gatecost.ir.dump import dump
        res = run(self.SRC, "c.sv")
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        text = dump(m, analysis=False)
        self.assertIn("y[31:0] <- 32'h3", text)
        self.assertIn("z[31:0] <- 32'he", text)


class VhdlLibraryName(unittest.TestCase):
    """A project compiled in a single library and referring to it by its real name (``neorv32``)."""
    PKG = """package my_pkg is
  constant K : natural := 5;
  function twice(x : natural) return natural;
end package;
package body my_pkg is
  function twice(x : natural) return natural is begin return 2 * x; end function;
end package body;
"""
    TOP = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
library mylib; use mylib.my_pkg.all;
entity t is port (a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)); end entity;
architecture r of t is begin y <= a + K + twice(1); end architecture;
"""

    def test_unknown_library_falls_back_to_lookup_by_unit_name(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "pkg.vhd").write_text(self.PKG)
            Path(td, "t.vhd").write_text(self.TOP)
            res = elaborate_project([Path("pkg.vhd"), Path("t.vhd")], root=Path(td))
        bad = [(d.code, d.message) for d in res.diagnostics if d.severity != "info"]
        self.assertEqual(bad, [])

    def test_vhdl_diagnostic_paths_are_relative(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "t.vhd").write_text(self.TOP)         # the package is missing: MISSING_UNIT
            res = elaborate_project([Path("t.vhd")], root=Path(td))
        files = [d.file for d in res.diagnostics if d.code == "MISSING_UNIT"]
        self.assertTrue(files)
        self.assertTrue(all(f and not f.startswith("/") for f in files), files)


class GenericWithoutDefault(unittest.TestCase):
    TEMPLATE = """library ieee; use ieee.std_logic_1164.all;
entity e is generic (N : %s);
  port (clk, rstn : in std_ulogic; d : in std_ulogic_vector(N-1 downto 0); q : out std_ulogic_vector(N-1 downto 0));
end entity;
architecture r of e is signal s : std_ulogic_vector(N-1 downto 0); begin
  process (rstn, clk) begin
    if rstn = '0' then s <= (others => '0'); elsif rising_edge(clk) then s <= d; end if;
  end process;
  q <= s;
end architecture;
"""

    def diags(self, subtype):
        return run(self.TEMPLATE % subtype, "e.vhd").diagnostics

    def test_assumed_value_stays_inside_the_subtype_range(self):
        for sub, want in (("natural range 1 to 8", "assumed 8"), ("integer range 4 to 6", "assumed 6"),
                          ("positive range 40 to 50", "assumed 40"), ("natural", "assumed 32")):
            msgs = [d.message for d in self.diags(sub) if d.code == "UNRESOLVED_PARAM"]
            self.assertEqual(len(msgs), 1, sub)
            self.assertTrue(msgs[0].endswith(want), (sub, msgs))

    def test_no_aggregate_failure_with_a_ranged_generic(self):
        bad = [d.message for d in self.diags("natural range 1 to 8") if d.code == "UNSUPPORTED_CONSTRUCT"]
        self.assertEqual(bad, [])


class BlackBoxConnections(unittest.TestCase):
    def test_one_net_on_two_ports_of_a_black_box_is_not_driven_twice(self):
        src = """module pll(input clki, output clko);
  wire fb, op;
  VENDOR_PLL u (.CLKI(clki), .CLKFB(fb), .CLKINTFB(fb), .CLKOP(op));
  assign clko = op;
endmodule
"""
        codes = [d.code for d in run(src, "pll.v").diagnostics]
        self.assertNotIn("MULTIPLE_DRIVERS", codes)
        self.assertIn("MISSING_MODULE", codes)


class UnpackedStructArrayParams(unittest.TestCase):
    SRC = """module a(input logic [1:0] sel, output logic [7:0] y, output logic z, output logic [1:0] w, output logic r);
  typedef enum logic [1:0] {OFF=0, TOR=1, NA4=2} mode_e;
  typedef struct packed { logic lock; mode_e mode; logic [3:0] val; } cfg_t;
  parameter cfg_t Rst[3] = '{ '{lock: 1'b0, mode: OFF, val: 4'd1}, '{lock: 1'b1, mode: TOR, val: 4'd2},
                              '{lock: 1'b1, mode: NA4, val: 4'd9} };
  localparam int V1 = Rst[1].val;
  localparam int V2 = Rst[2].val;
  assign y = V1 + V2;
  assign z = Rst[1].lock;
  assign w = Rst[2].mode;
  assign r = Rst[sel].lock;
endmodule
"""

    def test_elements_and_fields_of_an_array_parameter_of_structs(self):
        from gatecost.ir.dump import dump
        res = run(self.SRC, "a.sv")
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        text = dump(m, analysis=False)
        self.assertIn("y[7:0] <- 8'hb", text)       # 2 + 9
        self.assertIn("z[0] <- 1'h1", text)
        self.assertIn("w[1:0] <- 2'h2", text)


if __name__ == "__main__":
    unittest.main()
