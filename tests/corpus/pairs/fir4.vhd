-- 4-tap FIR filter: delay line and per-tap products built in a generate loop
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity fir4 is
  generic (W : natural := 8; C0 : integer := 3; C1 : integer := -5; C2 : integer := 7; C3 : integer := 2);
  port (clk, rst, en : in std_logic; x : in signed(W-1 downto 0); y : out signed(2*W+1 downto 0));
end entity;

architecture rtl of fir4 is
  type tap_t is array (0 to 3) of signed(W-1 downto 0);
  type prod_t is array (0 to 3) of signed(2*W-1 downto 0);
  signal taps : tap_t;
  signal prod : prod_t;
  type coef_t is array (0 to 3) of integer;
  constant COEF : coef_t := (C0, C1, C2, C3);
begin
  g_tap : for i in 0 to 3 generate
    prod(i) <= taps(i) * to_signed(COEF(i), 8);
    g_shift : if i > 0 generate
      process (clk) begin
        if rising_edge(clk) then
          if rst = '1' then taps(i) <= (others => '0');
          elsif en = '1' then taps(i) <= taps(i-1);
          end if;
        end if;
      end process;
    end generate;
  end generate;
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then taps(0) <= (others => '0');
      elsif en = '1' then taps(0) <= x;
      end if;
    end if;
  end process;
  y <= resize(prod(0), 2*W+2) + resize(prod(1), 2*W+2) + resize(prod(2), 2*W+2) + resize(prod(3), 2*W+2);
end architecture;
