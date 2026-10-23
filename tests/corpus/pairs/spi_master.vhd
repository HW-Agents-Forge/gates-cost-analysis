-- SPI master, mode 0, 8-bit frames, clock divided by an even divisor
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity spi_master is
  port (clk, rst, start : in std_logic; tx_data : in std_logic_vector(7 downto 0); div : in unsigned(7 downto 0);
        miso : in std_logic; mosi : out std_logic; sclk : out std_logic; cs_n : out std_logic;
        rx_data : out std_logic_vector(7 downto 0); done : out std_logic);
end entity;

architecture rtl of spi_master is
  signal sh_out, sh_in : std_logic_vector(7 downto 0);
  signal cnt : unsigned(7 downto 0);
  signal bits : unsigned(3 downto 0);
  signal active, sclk_r, cs_r, done_r : std_logic;
  signal rx_r : std_logic_vector(7 downto 0);
begin
  mosi <= sh_out(7);
  sclk <= sclk_r;
  cs_n <= cs_r;
  rx_data <= rx_r;
  done <= done_r;
  process (clk) begin
    if rising_edge(clk) then
      done_r <= '0';
      if rst = '1' then
        active <= '0';
        sclk_r <= '0';
        cs_r <= '1';
        cnt <= to_unsigned(0, 8);
        bits <= to_unsigned(0, 4);
        sh_out <= x"00";
        sh_in <= x"00";
        rx_r <= x"00";
      elsif active = '0' then
        if start = '1' then
          active <= '1';
          cs_r <= '0';
          sh_out <= tx_data;
          cnt <= div;
          bits <= to_unsigned(0, 4);
        end if;
      else
        if cnt = 0 then
          cnt <= div;
          sclk_r <= not sclk_r;
          if sclk_r = '0' then
            sh_in <= sh_in(6 downto 0) & miso;
          else
            sh_out <= sh_out(6 downto 0) & '0';
            bits <= bits + 1;
            if bits = 7 then
              active <= '0';
              cs_r <= '1';
              rx_r <= sh_in;
              done_r <= '1';
            end if;
          end if;
        else cnt <= cnt - 1;
        end if;
      end if;
    end if;
  end process;
end architecture;
