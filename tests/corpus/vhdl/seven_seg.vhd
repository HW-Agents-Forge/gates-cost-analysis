-- BCD to seven-segment decoder (lookup table) with blanking and a registered digit multiplexer
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity seven_seg is
  port (clk, rst : in std_logic; digits : in std_logic_vector(15 downto 0); blank : in std_logic;
        seg : out std_logic_vector(6 downto 0); an : out std_logic_vector(3 downto 0));
end entity;

architecture rtl of seven_seg is
  signal sel : unsigned(1 downto 0);
  signal nib : std_logic_vector(3 downto 0);
  signal decoded : std_logic_vector(6 downto 0);
  signal div : unsigned(15 downto 0);
begin
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then div <= (others => '0'); sel <= (others => '0');
      else
        div <= div + 1;
        if div = 0 then sel <= sel + 1; end if;
      end if;
    end if;
  end process;
  with sel select nib <= digits(3 downto 0) when "00", digits(7 downto 4) when "01",
                         digits(11 downto 8) when "10", digits(15 downto 12) when others;
  with nib select decoded <=
    "0111111" when x"0", "0000110" when x"1", "1011011" when x"2", "1001111" when x"3",
    "1100110" when x"4", "1101101" when x"5", "1111101" when x"6", "0000111" when x"7",
    "1111111" when x"8", "1101111" when x"9", "1110111" when x"A", "1111100" when x"B",
    "0111001" when x"C", "1011110" when x"D", "1111001" when x"E", "1110001" when others;
  seg <= (others => '0') when blank = '1' else decoded;
  an <= "1110" when sel = 0 else "1101" when sel = 1 else "1011" when sel = 2 else "0111";
end architecture;
