-- 4-bit S-box lookup table with a registered output (case-based ROM)
library ieee;
use ieee.std_logic_1164.all;

entity sbox is
  port (clk, en : in std_logic; x : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0);
        y_comb : out std_logic_vector(3 downto 0));
end entity;

architecture rtl of sbox is
  signal t, yr : std_logic_vector(3 downto 0);
begin
  process (x) begin
    case x is
      when x"0" => t <= x"C";  when x"1" => t <= x"5";  when x"2" => t <= x"6";  when x"3" => t <= x"B";
      when x"4" => t <= x"9";  when x"5" => t <= x"0";  when x"6" => t <= x"A";  when x"7" => t <= x"D";
      when x"8" => t <= x"3";  when x"9" => t <= x"E";  when x"A" => t <= x"F";  when x"B" => t <= x"8";
      when x"C" => t <= x"4";  when x"D" => t <= x"7";  when x"E" => t <= x"1";  when others => t <= x"2";
    end case;
  end process;
  y_comb <= t;
  process (clk) begin
    if rising_edge(clk) then
      if en = '1' then yr <= t; end if;
    end if;
  end process;
  y <= yr;
end architecture;
