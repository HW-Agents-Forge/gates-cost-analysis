-- Bytewise CRC-8 (poly 0x07), serial-in-byte update through a function
library ieee;
use ieee.std_logic_1164.all;

entity crc8 is
  port (clk, rst, init, valid : in std_logic; data : in std_logic_vector(7 downto 0);
        crc : out std_logic_vector(7 downto 0));
end entity;

architecture rtl of crc8 is
  signal c : std_logic_vector(7 downto 0);
  function step(cur : std_logic_vector(7 downto 0); d : std_logic_vector(7 downto 0)) return std_logic_vector is
    variable x : std_logic_vector(7 downto 0);
  begin
    x := cur xor d;
    for i in 0 to 7 loop
      if x(7) = '1' then x := x(6 downto 0) & '0' xor x"07";
      else x := x(6 downto 0) & '0';
      end if;
    end loop;
    return x;
  end function;
begin
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' or init = '1' then c <= x"00";
      elsif valid = '1' then c <= step(c, data);
      end if;
    end if;
  end process;
  crc <= c;
end architecture;
