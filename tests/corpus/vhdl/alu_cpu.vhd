-- CPU-ish ALU slice with a record port, enumerated opcodes and a flag register
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package alu_pkg is
  type alu_op_t is (op_add, op_sub, op_and, op_or, op_xor, op_sll, op_srl, op_slt);
  type flags_t is record
    zero, neg, carry : std_logic;
  end record;
end package;

library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.alu_pkg.all;

entity alu_cpu is
  generic (W : natural := 16);
  port (clk, rst, en : in std_logic; op : in alu_op_t; a, b : in unsigned(W-1 downto 0);
        y : out unsigned(W-1 downto 0); flags : out flags_t);
end entity;

architecture rtl of alu_cpu is
  signal res : unsigned(W downto 0);
  signal f : flags_t;
begin
  process (all) begin
    case op is
      when op_add => res <= ('0' & a) + ('0' & b);
      when op_sub => res <= ('0' & a) - ('0' & b);
      when op_and => res <= '0' & (a and b);
      when op_or  => res <= '0' & (a or b);
      when op_xor => res <= '0' & (a xor b);
      when op_sll => res <= '0' & shift_left(a, to_integer(b(3 downto 0)));
      when op_srl => res <= '0' & shift_right(a, to_integer(b(3 downto 0)));
      when op_slt =>
        if a < b then res <= to_unsigned(1, W + 1); else res <= to_unsigned(0, W + 1); end if;
    end case;
  end process;
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then
        f <= (zero => '0', neg => '0', carry => '0');
      elsif en = '1' then
        if res(W-1 downto 0) = 0 then f.zero <= '1'; else f.zero <= '0'; end if;
        f.neg <= res(W-1);
        f.carry <= res(W);
      end if;
    end if;
  end process;
  y <= res(W-1 downto 0);
  flags <= f;
end architecture;
