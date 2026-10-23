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


if __name__ == "__main__":
    unittest.main()
