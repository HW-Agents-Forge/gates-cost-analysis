-- AXI4-Lite style register block: four 32-bit registers with byte strobes and a read mux
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity axil_regs is
  port (clk, rstn : in std_logic;
        awaddr : in std_logic_vector(3 downto 0); awvalid : in std_logic;
        wdata : in std_logic_vector(31 downto 0); wstrb : in std_logic_vector(3 downto 0); wvalid : in std_logic;
        araddr : in std_logic_vector(3 downto 0); arvalid : in std_logic;
        rdata : out std_logic_vector(31 downto 0); rvalid : out std_logic;
        ctrl, thresh : out std_logic_vector(31 downto 0));
end entity;

architecture rtl of axil_regs is
  signal r0, r1, r2, r3, rd : std_logic_vector(31 downto 0);
  signal rv, wr : std_logic;
begin
  wr <= awvalid and wvalid;
  process (clk) begin
    if rising_edge(clk) then
      if rstn = '0' then
        r0 <= x"00000000";
        r1 <= x"00000000";
        r2 <= x"0000FFFF";
        r3 <= x"00000000";
      elsif wr = '1' then
        for i in 0 to 3 loop
          if wstrb(i) = '1' then
            case awaddr(3 downto 2) is
              when "00" => r0(8*i+7 downto 8*i) <= wdata(8*i+7 downto 8*i);
              when "01" => r1(8*i+7 downto 8*i) <= wdata(8*i+7 downto 8*i);
              when "10" => r2(8*i+7 downto 8*i) <= wdata(8*i+7 downto 8*i);
              when others => r3(8*i+7 downto 8*i) <= wdata(8*i+7 downto 8*i);
            end case;
          end if;
        end loop;
      end if;
    end if;
  end process;
  process (clk) begin
    if rising_edge(clk) then
      if rstn = '0' then
        rd <= x"00000000";
        rv <= '0';
      else
        rv <= arvalid;
        if arvalid = '1' then
          case araddr(3 downto 2) is
            when "00" => rd <= r0;
            when "01" => rd <= r1;
            when "10" => rd <= r2;
            when others => rd <= r3;
          end case;
        end if;
      end if;
    end if;
  end process;
  rdata <= rd;
  rvalid <= rv;
  ctrl <= r0;
  thresh <= r2;
end architecture;
