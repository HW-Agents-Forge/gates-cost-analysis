-- Small multiply-accumulate unit: acc <= acc + a*b, with enable and synchronous clear.
-- VHDL twin of mac.sv.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity mac is
  generic (
    W   : positive := 8;    -- operand width
    ACC_W : positive := 24    -- accumulator width
  );
  port (
    clk   : in  std_logic;
    rst_n : in  std_logic;
    clear : in  std_logic;
    en    : in  std_logic;
    a     : in  unsigned(W-1 downto 0);
    b     : in  unsigned(W-1 downto 0);
    acc   : out unsigned(ACC_W-1 downto 0)
  );
end entity mac;

architecture rtl of mac is
  signal acc_q  : unsigned(ACC_W-1 downto 0);
  signal prod_q : unsigned(2*W-1 downto 0);  -- variant: registered product (pipeline stage)
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if en = '1' then
        prod_q <= a * b;
      end if;
    end if;
  end process;

  process (clk, rst_n)
  begin
    if rst_n = '0' then
      acc_q <= (others => '0');
    elsif rising_edge(clk) then
      if clear = '1' then
        acc_q <= (others => '0');
      elsif en = '1' then
        acc_q <= acc_q + resize(prod_q, ACC_W);
      end if;
    end if;
  end process;
  acc <= acc_q;
end architecture rtl;
