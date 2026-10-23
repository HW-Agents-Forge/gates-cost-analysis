"""Robustness: mutated VHDL elaborates to diagnostics and partial IR, never raises and never hangs."""
import random
import time
import unittest

from gatecost.elab.vhdl import elaborate_vhdl
from gatecost.frontend.vhdl import parse_text
from gatecost.ir import Design, analyze_ir, dump

from tests.test_vhdl_fuzz import mutate

FIXTURES = [
    """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
package pk is
  constant K : natural := 4;
  type state_t is (IDLE, RUN, DONE);
  function clog2(n : natural) return natural;
end package;
package body pk is
  function clog2(n : natural) return natural is variable r : natural := 0; variable v : natural := n - 1;
  begin while v > 0 loop v := v / 2; r := r + 1; end loop; return r; end function;
end package body;
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all; use work.pk.all;
entity sub is generic (N : natural := 2); port (a : in std_logic_vector(N - 1 downto 0); y : out std_logic); end entity;
architecture r of sub is begin y <= '1' when unsigned(a) = 0 else '0'; end architecture;
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all; use work.pk.all;
entity top is
  generic (W : positive := 8);
  port (clk, rst, en, go : in std_logic; d : in std_logic_vector(W - 1 downto 0); q : out std_logic_vector(W - 1 downto 0);
        z : out std_logic; sel : in std_logic_vector(1 downto 0));
end entity;
architecture rtl of top is
  signal st : state_t;
  signal cnt : unsigned(W - 1 downto 0);
  type ram_t is array (0 to 15) of std_logic_vector(7 downto 0);
  signal ram : ram_t;
  type rec is record a : std_logic; b : unsigned(3 downto 0); end record;
  signal r : rec;
  function f(x : unsigned) return unsigned is begin return x + 1; end function;
  component sub is generic (N : natural := 2); port (a : in std_logic_vector(N - 1 downto 0); y : out std_logic); end component;
begin
  q <= std_logic_vector(cnt);
  process (clk, rst) begin
    if rst = '1' then cnt <= (others => '0'); st <= IDLE;
    elsif rising_edge(clk) then
      if en = '1' then cnt <= f(cnt); end if;
      case st is when IDLE => if go = '1' then st <= RUN; end if; when RUN => st <= DONE; when others => st <= IDLE; end case;
      ram(to_integer(cnt(3 downto 0))) <= d(7 downto 0);
    end if;
  end process;
  process (all) variable p : std_logic; begin
    p := '0';
    for i in d'range loop p := p xor d(i); end loop;
    case sel is when "00" => z <= p; when "01" => z <= not p; when others => z <= 'Z'; end case;
  end process;
  u1 : sub generic map (N => W) port map (a => d, y => open);
  g : for i in 0 to 1 generate signal t : std_logic; begin t <= d(i) and d(i + 1); end generate;
  g2 : if W > 4 generate r.a <= '1'; else generate r.a <= '0'; end generate;
end architecture;
""",
    """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity alu is port (a, b : in signed(7 downto 0); op : in std_logic_vector(2 downto 0); y : out signed(15 downto 0); c : out std_logic); end;
architecture a of alu is
begin
  process (all) begin
    c <= '0';
    case op is
      when "000" => y <= resize(a + b, 16);
      when "001" => y <= a * b;
      when "010" => y <= resize(a - b, 16);
      when "011" => y <= shift_left(resize(a, 16), to_integer(unsigned(b(2 downto 0))));
      when "100" => y <= (others => '0'); c <= '1';
      when others => y <= resize(abs(a), 16);
    end case;
  end process;
end;
""",
]


class ElabFuzz(unittest.TestCase):
    def run_one(self, text, top=None):
        t0 = time.time()
        parsed = parse_text(text)
        d = Design()
        r = elaborate_vhdl(parsed, d, top=top)
        for m in d.modules.values():
            analyze_ir(m)
            dump(m)
        dt = time.time() - t0
        self.assertLess(dt, 10.0, "slow elaboration: %r" % text[:200])
        for x in r.diagnostics:
            self.assertTrue(x.code and x.message and x.severity in ("info", "warning", "error"))
        return r, d

    def test_fixtures_elaborate_cleanly(self):
        for src in FIXTURES:
            r, d = self.run_one(src)
            self.assertTrue(r.tops)
            self.assertEqual([x for x in r.diagnostics if x.severity == "error"], [])
            self.assertEqual([x.code for x in r.diagnostics if x.code in ("UNSUPPORTED_CONSTRUCT", "UNRESOLVED_OVERLOAD")], [])

    def test_seeded_mutations(self):
        rnd = random.Random(424242)
        for _ in range(250):
            src = rnd.choice(FIXTURES)
            self.run_one(mutate(src, rnd))

    def test_mutations_with_a_chosen_top(self):
        rnd = random.Random(99)
        for _ in range(60):
            self.run_one(mutate(FIXTURES[0], rnd), top="top")

    def test_every_prefix(self):
        src = FIXTURES[1]
        step = max(1, len(src) // 80)
        for n in range(0, len(src), step):
            self.run_one(src[:n])

    def test_token_soup(self):
        rnd = random.Random(5)
        words = ["entity", "architecture", "is", "begin", "end", "process", "if", "then", "else", "case", "when", "loop", "for",
                 "generate", "(", ")", ";", ":", ":=", "<=", "=>", "a", "b", "1", "'1'", '"01"', "port", "map", "signal", "std_logic",
                 "unsigned", "to_integer", "rising_edge", "clk", "others", "&", "+", "of", "type", "array"]
        for _ in range(150):
            self.run_one(" ".join(rnd.choice(words) for _ in range(rnd.randint(1, 100))))


if __name__ == "__main__":
    unittest.main()
