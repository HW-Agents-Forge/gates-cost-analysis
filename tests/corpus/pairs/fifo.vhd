-- Synchronous FIFO, 16 x 8, binary pointers with a wrap bit, asynchronous read
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity fifo is
  port (clk, rst, push, pop : in std_logic; din : in std_logic_vector(7 downto 0);
        dout : out std_logic_vector(7 downto 0); full, empty : out std_logic; level : out unsigned(4 downto 0));
end entity;

architecture rtl of fifo is
  type mem_t is array (0 to 15) of std_logic_vector(7 downto 0);
  signal mem : mem_t;
  signal wp, rp : unsigned(4 downto 0);
  signal full_i, empty_i : std_logic;
begin
  empty_i <= '1' when wp = rp else '0';
  full_i <= '1' when (wp(4) /= rp(4)) and (wp(3 downto 0) = rp(3 downto 0)) else '0';
  empty <= empty_i;
  full <= full_i;
  level <= wp - rp;
  dout <= mem(to_integer(rp(3 downto 0)));
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then
        wp <= to_unsigned(0, 5);
        rp <= to_unsigned(0, 5);
      else
        if push = '1' and full_i = '0' then
          mem(to_integer(wp(3 downto 0))) <= din;
          wp <= wp + 1;
        end if;
        if pop = '1' and empty_i = '0' then rp <= rp + 1; end if;
      end if;
    end if;
  end process;
end architecture;
