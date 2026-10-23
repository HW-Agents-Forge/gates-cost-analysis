-- Multiply-accumulate (signed): acc <= acc + a*b with enable and synchronous clear
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity mac_s is
  generic (W : positive := 8; ACC_W : positive := 24);
  port (clk, rst_n, clear, en : in std_logic; a, b : in signed(W-1 downto 0);
        acc : out signed(ACC_W-1 downto 0));
end entity;

architecture rtl of mac_s is
  signal acc_q : signed(ACC_W-1 downto 0);
begin
  process (clk, rst_n) begin
    if rst_n = '0' then acc_q <= (others => '0');
    elsif rising_edge(clk) then
      if clear = '1' then acc_q <= (others => '0');
      elsif en = '1' then acc_q <= acc_q + resize(a * b, ACC_W);
      end if;
    end if;
  end process;
  acc <= acc_q;
end architecture;
