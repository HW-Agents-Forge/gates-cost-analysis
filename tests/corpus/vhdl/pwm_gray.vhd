-- PWM generator with a gray-coded phase counter and a lookup-table duty ramp
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity pwm_gray is
  generic (N : natural := 8);
  port (clk, rst, en : in std_logic; duty : in unsigned(N-1 downto 0);
        pwm : out std_logic; phase_gray : out std_logic_vector(N-1 downto 0));
end entity;

architecture rtl of pwm_gray is
  signal cnt : unsigned(N-1 downto 0);
  signal gray : std_logic_vector(N-1 downto 0);
begin
  gray <= std_logic_vector(cnt xor ('0' & cnt(N-1 downto 1)));
  phase_gray <= gray;
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then cnt <= (others => '0'); pwm <= '0';
      elsif en = '1' then
        cnt <= cnt + 1;
        if cnt < duty then pwm <= '1'; else pwm <= '0'; end if;
      end if;
    end if;
  end process;
end architecture;
