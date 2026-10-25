-- Registered ALU with a two-bit opcode.
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity alu is generic (W : integer := 16);
  port (clk : in std_logic; a, b : in unsigned(W-1 downto 0); op : in std_logic_vector(1 downto 0);
        y : out unsigned(W-1 downto 0));
end entity;
architecture rtl of alu is
  signal r : unsigned(W-1 downto 0);
begin
  process (clk) begin
    if rising_edge(clk) then
      case op is
        when "00"   => r <= a + b;
        when "01"   => r <= a - b;
        when "10"   => r <= a and b;
        when others => r <= resize(a * b, W);
      end case;
    end if;
  end process;
  y <= r;
end architecture;
