"""Seeded robustness fuzzing: mutated VHDL must terminate quickly, never raise, and return diagnostics."""
import random
import re
import time
import unittest

from gatecost.frontend.vhdl import parse_text

FIXTURES = [
    """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity counter is
  generic (W : positive := 8);
  port (clk, rst, en : in std_logic; q : out std_logic_vector(W-1 downto 0));
end entity counter;
architecture rtl of counter is
  type state_t is (IDLE, RUN, DONE);
  signal st : state_t;
  signal cnt : unsigned(W-1 downto 0) := (others => '0');
  type rec is record a : std_logic; b : unsigned(3 downto 0); end record;
  function f(x : unsigned) return unsigned is begin return x + 1; end function f;
  component sub is generic (N : integer); port (a : in std_logic; b : out std_logic); end component;
begin
  q <= std_logic_vector(cnt);
  y <= a when s = '1' else b when t = '0' else (others => 'Z');
  with sel select z <= "00" when "01" | "10", x"F" when others;
  process (clk, rst)
    variable v : integer range 0 to 7;
  begin
    if rst = '1' then cnt <= (others => '0');
    elsif rising_edge(clk) then
      if en = '1' then cnt <= cnt + 1; end if;
      case st is when IDLE => st <= RUN; when others => null; end case;
      for i in 0 to 3 loop v := v + i; end loop;
      assert false report "x" severity note;
    end if;
  end process;
  u1 : sub generic map (N => 4) port map (a => clk, b => open);
  u2 : entity work.counter(rtl) port map (clk => clk, rst => rst, en => en, q => open);
  g : for i in 0 to 3 generate signal t : std_logic; begin t <= a(i) and b(i); end generate g;
  g2 : if W > 4 generate x <= '1'; elsif W > 2 generate x <= '0'; else generate x <= 'Z'; end generate;
  b1 : block (en = '1') is begin t <= p; end block b1;
  tb: process begin clk <= '0'; wait for 5 ns; clk <= '1' after 2 ns; end process;
end architecture rtl;
""",
    """package p is
  constant c : integer := 2 ** 4 - 1;
  type t is array (natural range <>) of bit;
  subtype nib is integer range 0 to 15;
  function "+" (a, b : t) return t;
  procedure pr (signal s : out bit; variable v : inout integer);
  alias al : bit_vector(3 downto 0) is data(7 downto 4);
  attribute keep : string;
  attribute keep of s : signal is "true";
end package p;
package body p is
  function "+" (a, b : t) return t is
    variable r : t(a'range);
  begin
    for i in a'range loop r(i) := a(i) xor b(i); end loop;
    return r;
  end function;
end package body p;
configuration cfg of e is for rtl end for; end configuration;
context ctx is library ieee; use ieee.std_logic_1164.all; end context;
""",
    """entity e is port (a : in bit_vector(3 downto 0); y : out bit); end;
architecture a of e is
begin
  process (all) begin
    case? a is when "1---" => y <= '1'; when others => y <= '0'; end case?;
    l: while a(0) = '1' loop exit l when a(1) = '1'; next; end loop l;
    loop wait on a; exit; end loop;
    y <= ?? (a and b) when a ?= b else '0';
  end process;
  g: case MODE generate when 0 | 1 => x <= '1'; when others => x <= '0'; end generate;
end;
""",
]

PIECE = re.compile(r"\s+|[A-Za-z_][A-Za-z0-9_]*|\d+|.", re.S)


def mutate(text, rnd):
    pieces = PIECE.findall(text)
    toks = [i for i, p in enumerate(pieces) if not p.isspace()]
    out = list(pieces)
    for _ in range(rnd.randint(1, 4)):
        kind = rnd.choice(("delete", "swap", "dup", "trunc", "garbage", "kw"))
        i = rnd.choice(toks)
        if kind == "delete":
            out[i] = ""
        elif kind == "swap":
            j = rnd.choice(toks)
            out[i], out[j] = out[j], out[i]
        elif kind == "dup":
            out[i] = out[i] + " " + out[i]
        elif kind == "trunc":
            out = out[:i]
            break
        elif kind == "garbage":
            out[i] = rnd.choice(["`", ")", "(", ";", "'", '"', "\\", "=>", "<=", "end", "begin", "is", "#", "/*"])
        else:
            out[i] = rnd.choice(["entity", "architecture", "process", "if", "case", "end", "generate", "block",
                                 "function", "package", "then", "loop", "when", "else", "port", "generic"])
    return "".join(out)


class FuzzTests(unittest.TestCase):
    def run_one(self, text):
        t0 = time.time()
        r = parse_text(text)
        dt = time.time() - t0
        self.assertLess(dt, 5.0, "slow parse: %r" % text[:200])
        self.assertTrue(hasattr(r, "diagnostics"))
        for d in r.diagnostics:
            self.assertTrue(d.code and d.message and d.severity in ("info", "warning", "error"))
        return r

    def test_valid_fixtures_parse_without_errors(self):
        for src in FIXTURES:
            r = self.run_one(src)
            self.assertEqual([d for d in r.diagnostics if d.severity == "error"], [])

    def test_seeded_mutations(self):
        rnd = random.Random(20240607)
        n_err = 0
        for _ in range(600):
            src = rnd.choice(FIXTURES)
            text = mutate(src, rnd)
            r = self.run_one(text)
            if any(d.severity == "error" for d in r.diagnostics):
                n_err += 1
        self.assertGreater(n_err, 100)

    def test_every_prefix_of_a_fixture(self):
        src = FIXTURES[0]
        step = max(1, len(src) // 150)
        for n in range(0, len(src), step):
            self.run_one(src[:n])

    def test_random_token_soup(self):
        rnd = random.Random(7)
        words = ["entity", "architecture", "is", "begin", "end", "process", "if", "then", "else", "elsif", "case",
                 "when", "loop", "for", "generate", "block", "(", ")", ";", ":", ":=", "<=", "=>", "x", "y", "1",
                 "'1'", '"01"', "port", "map", "generic", "function", "return", "package", "body", "signal",
                 "type", "record", "array", "of", ",", ".", "'", "&", "+", "others", "all", "wait", "until"]
        for _ in range(300):
            self.run_one(" ".join(rnd.choice(words) for _ in range(rnd.randint(1, 120))))

    def test_deep_nesting_terminates(self):
        self.run_one("architecture a of e is begin " + "g: if c generate " * 2000 + "x <= y; " + "end generate; " * 2000 + "end;")
        self.run_one("architecture a of e is begin process begin " + "if a then " * 2000 + "x <= 1;" + " end if;" * 2000
                     + " end process; end;")
        self.run_one("architecture a of e is begin x <= " + "f(" * 5000 + "1" + ")" * 5000 + "; end;")


if __name__ == "__main__":
    unittest.main()
