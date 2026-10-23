-- Radix-4 Booth multiplier, 8x8 signed, combinational partial-product array
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity booth is
  port (a, b : in signed(7 downto 0); p : out signed(15 downto 0));
end entity;

architecture rtl of booth is
begin
  process (a, b)
    variable acc, pp, ax : signed(15 downto 0);
    variable grp : std_logic_vector(2 downto 0);
  begin
    acc := to_signed(0, 16);
    ax := resize(a, 16);
    for i in 0 to 3 loop
      if i = 0 then grp := std_logic_vector(b(1 downto 0)) & '0';
      else grp := std_logic_vector(b(2*i+1 downto 2*i-1));
      end if;
      case grp is
        when "001" | "010" => pp := ax;
        when "011" => pp := shift_left(ax, 1);
        when "100" => pp := -shift_left(ax, 1);
        when "101" | "110" => pp := -ax;
        when others => pp := to_signed(0, 16);
      end case;
      acc := acc + shift_left(pp, 2*i);
    end loop;
    p <= acc;
  end process;
end architecture;
