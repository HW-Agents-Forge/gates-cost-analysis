-- Iterative restoring divider, 8-bit operands, one quotient bit per clock
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity divider is
  port (clk, rst, start : in std_logic; num, den : in unsigned(7 downto 0);
        quo, remd : out unsigned(7 downto 0); done : out std_logic; busy : out std_logic);
end entity;

architecture rtl of divider is
  signal q, d, quo_r, rem_r : unsigned(7 downto 0);
  signal r, shifted, diff : unsigned(8 downto 0);
  signal cnt : unsigned(3 downto 0);
  signal run, done_r : std_logic;
begin
  shifted <= r(7 downto 0) & q(7);
  diff <= shifted - resize(d, 9);
  busy <= run;
  quo <= quo_r;
  remd <= rem_r;
  done <= done_r;
  process (clk) begin
    if rising_edge(clk) then
      done_r <= '0';
      if rst = '1' then
        run <= '0';
        q <= to_unsigned(0, 8);
        r <= to_unsigned(0, 9);
        d <= to_unsigned(0, 8);
        cnt <= to_unsigned(0, 4);
        quo_r <= to_unsigned(0, 8);
        rem_r <= to_unsigned(0, 8);
      elsif run = '0' then
        if start = '1' then
          run <= '1';
          q <= num;
          d <= den;
          r <= to_unsigned(0, 9);
          cnt <= to_unsigned(0, 4);
        end if;
      else
        if diff(8) = '1' then
          r <= shifted;
          q <= q(6 downto 0) & '0';
        else
          r <= diff;
          q <= q(6 downto 0) & '1';
        end if;
        cnt <= cnt + 1;
        if cnt = 7 then
          run <= '0';
          done_r <= '1';
          if diff(8) = '1' then
            quo_r <= q(6 downto 0) & '0';
            rem_r <= shifted(7 downto 0);
          else
            quo_r <= q(6 downto 0) & '1';
            rem_r <= diff(7 downto 0);
          end if;
        end if;
      end if;
    end if;
  end process;
end architecture;
