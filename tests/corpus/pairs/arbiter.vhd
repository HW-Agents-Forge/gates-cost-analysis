-- Four-requester round-robin arbiter using a rotating mask
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity arbiter is
  port (clk, rst : in std_logic; req : in std_logic_vector(3 downto 0);
        grant : out std_logic_vector(3 downto 0); any_req : out std_logic);
end entity;

architecture rtl of arbiter is
  signal mask : std_logic_vector(3 downto 0);
  signal mreq, sel, g : std_logic_vector(3 downto 0);
begin
  mreq <= req and mask;
  sel <= mreq when mreq /= "0000" else req;
  g <= sel and std_logic_vector(unsigned(not sel) + 1);
  grant <= g;
  any_req <= '1' when req /= "0000" else '0';
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then mask <= "1111";
      elsif g /= "0000" then
        mask <= not std_logic_vector(unsigned(g(2 downto 0) & '0') - 1);
      end if;
    end if;
  end process;
end architecture;
