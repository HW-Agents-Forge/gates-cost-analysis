-- 32-bit logical right shifter / rotator built from five mux stages
library ieee;
use ieee.std_logic_1164.all;

entity barrel is
  port (a : in std_logic_vector(31 downto 0); sh : in std_logic_vector(4 downto 0); rot : in std_logic;
        y : out std_logic_vector(31 downto 0));
end entity;

architecture rtl of barrel is
  signal s0, s1, s2, s3, s4 : std_logic_vector(31 downto 0);
begin
  s0 <= (a(0) and rot) & a(31 downto 1) when sh(0) = '1' else a;
  s1 <= (s0(1 downto 0) and (1 downto 0 => rot)) & s0(31 downto 2) when sh(1) = '1' else s0;
  s2 <= (s1(3 downto 0) and (3 downto 0 => rot)) & s1(31 downto 4) when sh(2) = '1' else s1;
  s3 <= (s2(7 downto 0) and (7 downto 0 => rot)) & s2(31 downto 8) when sh(3) = '1' else s2;
  s4 <= (s3(15 downto 0) and (15 downto 0 => rot)) & s3(31 downto 16) when sh(4) = '1' else s3;
  y <= s4;
end architecture;
