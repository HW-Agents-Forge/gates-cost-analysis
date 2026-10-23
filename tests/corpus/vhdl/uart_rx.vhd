-- UART receiver with 16x oversampling, majority-less mid-bit sampling and framing error flag
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity uart_rx is
  port (clk, rst, rx, tick16 : in std_logic; data : out std_logic_vector(7 downto 0);
        valid, frame_err : out std_logic);
end entity;

architecture rtl of uart_rx is
  type state_t is (idle, start_bit, data_bits, stop_bit);
  signal state : state_t;
  signal os_cnt : unsigned(3 downto 0);
  signal bit_idx : unsigned(2 downto 0);
  signal shreg : std_logic_vector(7 downto 0);
  signal rx_s1, rx_s2 : std_logic;
begin
  process (clk) begin
    if rising_edge(clk) then
      rx_s1 <= rx;
      rx_s2 <= rx_s1;
      valid <= '0';
      frame_err <= '0';
      if rst = '1' then
        state <= idle;
        os_cnt <= (others => '0');
        bit_idx <= (others => '0');
      elsif tick16 = '1' then
        case state is
          when idle =>
            if rx_s2 = '0' then state <= start_bit; os_cnt <= to_unsigned(0, 4); end if;
          when start_bit =>
            if os_cnt = 7 then
              if rx_s2 = '0' then state <= data_bits; os_cnt <= (others => '0'); bit_idx <= (others => '0');
              else state <= idle;
              end if;
            else os_cnt <= os_cnt + 1;
            end if;
          when data_bits =>
            if os_cnt = 15 then
              shreg <= rx_s2 & shreg(7 downto 1);
              os_cnt <= (others => '0');
              if bit_idx = 7 then state <= stop_bit; else bit_idx <= bit_idx + 1; end if;
            else os_cnt <= os_cnt + 1;
            end if;
          when stop_bit =>
            if os_cnt = 15 then
              valid <= rx_s2;
              frame_err <= not rx_s2;
              state <= idle;
            else os_cnt <= os_cnt + 1;
            end if;
        end case;
      end if;
    end if;
  end process;
  data <= shreg;
end architecture;
