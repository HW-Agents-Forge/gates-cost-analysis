-- UART transmitter: 8N1, programmable baud divider, start/stop framing
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity uart_tx is
  port (clk, rst, start : in std_logic; data : in std_logic_vector(7 downto 0);
        div : in unsigned(15 downto 0); tx : out std_logic; busy : out std_logic);
end entity;

architecture rtl of uart_tx is
  type state_t is (idle, start_s, data_s, stop_s);
  signal state : state_t;
  signal baud_cnt : unsigned(15 downto 0);
  signal bit_cnt : unsigned(2 downto 0);
  signal shreg : std_logic_vector(7 downto 0);
  signal tx_r : std_logic;
  signal tick : std_logic;
begin
  tick <= '1' when baud_cnt = 0 else '0';
  busy <= '1' when state /= idle else '0';
  tx <= tx_r;
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then
        state <= idle;
        tx_r <= '1';
        baud_cnt <= to_unsigned(0, 16);
        bit_cnt <= to_unsigned(0, 3);
        shreg <= x"00";
      else
        case state is
          when idle =>
            tx_r <= '1';
            if start = '1' then
              shreg <= data;
              baud_cnt <= div;
              state <= start_s;
            end if;
          when start_s =>
            tx_r <= '0';
            if tick = '1' then
              baud_cnt <= div;
              bit_cnt <= to_unsigned(0, 3);
              state <= data_s;
            else baud_cnt <= baud_cnt - 1;
            end if;
          when data_s =>
            tx_r <= shreg(0);
            if tick = '1' then
              baud_cnt <= div;
              shreg <= '0' & shreg(7 downto 1);
              bit_cnt <= bit_cnt + 1;
              if bit_cnt = 7 then state <= stop_s; end if;
            else baud_cnt <= baud_cnt - 1;
            end if;
          when others =>
            tx_r <= '1';
            if tick = '1' then state <= idle;
            else baud_cnt <= baud_cnt - 1;
            end if;
        end case;
      end if;
    end if;
  end process;
end architecture;
