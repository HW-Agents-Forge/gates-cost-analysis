-- Direct-mapped cache tag array: 64 sets, valid bit, tag compare and fill
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity cache_tag is
  port (clk, rst : in std_logic; addr : in std_logic_vector(25 downto 0); fill, invalidate : in std_logic;
        hit : out std_logic; victim_tag : out std_logic_vector(19 downto 0); victim_valid : out std_logic);
end entity;

architecture rtl of cache_tag is
  type tag_t is array (0 to 63) of std_logic_vector(19 downto 0);
  signal tags : tag_t;
  signal valid : std_logic_vector(63 downto 0);
  signal index : unsigned(5 downto 0);
  signal tag, stored : std_logic_vector(19 downto 0);
  signal same : std_logic;
begin
  index <= unsigned(addr(5 downto 0));
  tag <= addr(25 downto 6);
  stored <= tags(to_integer(index));
  same <= '1' when stored = tag else '0';
  hit <= valid(to_integer(index)) and same;
  victim_tag <= stored;
  victim_valid <= valid(to_integer(index));
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then valid <= (others => '0');
      else
        if fill = '1' then
          tags(to_integer(index)) <= tag;
          valid(to_integer(index)) <= '1';
        elsif invalidate = '1' then valid(to_integer(index)) <= '0';
        end if;
      end if;
    end if;
  end process;
end architecture;
