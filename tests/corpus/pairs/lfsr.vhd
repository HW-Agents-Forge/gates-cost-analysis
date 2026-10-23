-- 16-bit Fibonacci LFSR with load and enable (corpus design, written for gatecost tests)
library ieee;
use ieee.std_logic_1164.all;

entity lfsr is
  generic (W : natural := 16);
  port (clk, rst, en, load : in std_logic; seed : in std_logic_vector(W-1 downto 0);
        q : out std_logic_vector(W-1 downto 0); bit_out : out std_logic);
end entity;

architecture rtl of lfsr is
  signal r : std_logic_vector(W-1 downto 0);
  signal fb : std_logic;
begin
  fb <= r(15) xor r(13) xor r(12) xor r(10);
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then r <= x"ACE1";
      elsif load = '1' then r <= seed;
      elsif en = '1' then r <= r(W-2 downto 0) & fb;
      end if;
    end if;
  end process;
  q <= r;
  bit_out <= r(W-1);
end architecture;
