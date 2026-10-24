"""VHDL gaps found on real RTL: unconstrained record fields, mixed / dual-edge processes, string functions in
constant context, and top-level unconstrained ports (ghdl testsuite/synth, open-logic)."""
import tempfile
import unittest
from pathlib import Path

from gatecost.elab import elaborate_project
from gatecost.ir.dump import dump


def run(text, top=None):
    with tempfile.TemporaryDirectory() as td:
        Path(td, "m.vhd").write_text(text)
        res = elaborate_project([Path("m.vhd")], root=Path(td), top=top) if top else \
            elaborate_project([Path("m.vhd")], root=Path(td))
        mods = {}
        for m in res.design.modules.values():
            mods.setdefault(m.name, dump(m, analysis=False))
            mods[dump(m, analysis=False).splitlines()[0][len("module "):]] = dump(m, analysis=False)
        return res, mods


def codes(res):
    return [d.code for d in res.diagnostics if d.severity != "info"]


class UnconstrainedRecordField(unittest.TestCase):
    PKG = """library ieee; use ieee.std_logic_1164.all;
package rp is
  type my_rec is record
    v : std_logic_vector;
    c : std_logic;
  end record;
end;
"""

    def test_field_width_from_constrained_subtype(self):
        res, mods = run(self.PKG + """library ieee; use ieee.std_logic_1164.all; use work.rp.all;
entity top is port (clk : std_logic; a : in std_logic_vector(7 downto 0); o : out std_logic_vector(7 downto 0)); end;
architecture b of top is
  subtype r8 is my_rec(v(7 downto 0));
  signal s : r8;
begin
  process(clk) begin
    if rising_edge(clk) then s.v <= a; s.c <= a(0); end if;
  end process;
  o <= s.v;
end;
""")
        self.assertEqual(codes(res), [])
        self.assertIn("DFF(D={a[0], a[7:0]}", mods["top"])

    def test_field_width_from_port_map_actual(self):
        res, mods = run(self.PKG + """library ieee; use ieee.std_logic_1164.all; use work.rp.all;
entity sub is port (clk : std_logic; inp : my_rec; o : out std_logic); end;
architecture b of sub is begin
  process(clk) begin if rising_edge(clk) then o <= inp.v(0) xor inp.c; end if; end process;
end;
library ieee; use ieee.std_logic_1164.all;
entity top is port (clk : std_logic; a : in std_logic; o : out std_logic); end;
architecture b of top is begin
  u : entity work.sub port map (clk => clk, inp.c => a, inp.v => "00", o => o);
end;
""", top="top")
        self.assertEqual([c for c in codes(res) if c == "UNSUPPORTED_CONSTRUCT"], [])
        self.assertIn("inp.v[1:0]", [v for k, v in mods.items() if k.startswith("sub") and "$len:inp.v=2" in k][0])
        self.assertEqual(res.assumptions, ())

    def test_top_level_unconstrained_field_defaults_with_assumption(self):
        res, mods = run(self.PKG + """library ieee; use ieee.std_logic_1164.all; use work.rp.all;
entity top is port (inp : in my_rec; o : out std_logic); end;
architecture b of top is begin o <= inp.v(0) xor inp.c; end;
""")
        self.assertEqual([c for c in codes(res) if c == "UNSUPPORTED_CONSTRUCT"], [])
        self.assertIn("inp.v[7:0]", mods["top"])
        self.assertTrue(any("inp.v" in a and "assumed" in a for a in res.assumptions))


class MixedAndDualEdgeProcesses(unittest.TestCase):
    HDR = "library ieee; use ieee.std_logic_1164.all;\n"

    def test_both_edges_in_one_process_make_two_register_sets(self):
        res, mods = run(self.HDR + """entity top is port (clk, d : in std_logic; q : out std_logic); end;
architecture b of top is signal r, f : std_logic; begin
  process(clk) begin
    if rising_edge(clk) then r <= d; end if;
    if falling_edge(clk) then f <= d; end if;
  end process;
  q <= r xor f;
end;
""")
        t = mods["top"]
        self.assertIn("clk_edge=pos", t)
        self.assertIn("clk_edge=neg", t)

    def test_if_elsif_with_both_edges(self):
        res, mods = run(self.HDR + """entity top is port (clk, d : in std_logic; q : out std_logic); end;
architecture b of top is signal r, f : std_logic; begin
  process(clk) begin
    if clk'event and clk = '1' then r <= d;
    elsif clk'event and clk = '0' then f <= d; end if;
  end process;
  q <= r xor f;
end;
""")
        self.assertIn("clk_edge=pos", mods["top"])
        self.assertIn("clk_edge=neg", mods["top"])

    def test_bare_clk_event_is_one_register_set_with_an_assumption(self):
        res, mods = run(self.HDR + """entity top is port (clk, d : in std_logic; q : out std_logic); end;
architecture b of top is begin
  process(clk) begin if clk'event then q <= d; end if; end process;
end;
""")
        self.assertIn("clk_edge=pos", mods["top"])
        self.assertTrue(any("clk'event" in a for a in res.assumptions))

    def test_comb_outputs_next_to_a_clocked_branch(self):
        res, mods = run(self.HDR + """entity top is port (clk, d : in std_logic; c, qm : out std_logic; x : out std_logic); end;
architecture b of top is signal m : std_logic; begin
  process(clk, d) begin
    c <= d;
    if rising_edge(clk) then m <= d; end if;
    x <= m xor d;
  end process;
  qm <= m;
end;
""")
        t = mods["top"]
        self.assertEqual(codes(res), [])
        self.assertIn("c[0] <- d[0]", t)
        self.assertIn("XOR(", t)
        self.assertEqual(t.count("DFF("), 1)

    def test_signal_in_both_parts_keeps_the_register(self):
        res, mods = run(self.HDR + """entity top is port (clk, d : in std_logic; q : out std_logic); end;
architecture b of top is begin
  process(clk) begin
    q <= '0';
    if rising_edge(clk) then q <= d; end if;
  end process;
end;
""")
        self.assertEqual(mods["top"].count("DFF("), 1)
        self.assertTrue(any("register wins" in a for a in res.assumptions))

    def test_plain_clocked_process_unchanged(self):
        res, mods = run(self.HDR + """entity top is port (clk, d : in std_logic; q : out std_logic); end;
architecture b of top is begin
  process(clk) begin if rising_edge(clk) then q <= d; end if; end process;
end;
""")
        self.assertEqual(mods["top"].count("DFF("), 1)
        self.assertEqual(res.assumptions, ())


class StringFunctionsInConstantContext(unittest.TestCase):
    PKG = """package sp is
  function toUpper(s : string) return string;
  function trim(s : string) return string;
  function compareNoCase(a : string; b : string) return boolean;
end;
package body sp is
  function toUpper(s : string) return string is
    variable r : string(s'range);
    variable ci : natural;
  begin
    for i in s'range loop
      ci := character'pos(s(i));
      if ci >= character'pos('a') and ci <= character'pos('z') then ci := ci - 32; end if;
      r(i) := character'val(ci);
    end loop;
    return r;
  end;
  function trim(s : string) return string is
    variable a : natural := s'left;
    variable b : natural := s'right;
  begin
    while a < s'right and s(a) = ' ' loop a := a + 1; end loop;
    while b > s'left and s(b) = ' ' loop b := b - 1; end loop;
    return s(a to b);
  end;
  function compareNoCase(a : string; b : string) return boolean is
  begin return toUpper(trim(a)) = toUpper(trim(b)); end;
end;
"""

    def test_string_function_results_fold_and_record_type_survives(self):
        res, mods = run(self.PKG + """library ieee; use ieee.std_logic_1164.all; use work.sp.all;
entity top is
  generic (Parity_g : string := "Even"; DataBits_g : positive := 8);
  port (clk : in std_logic; a : in std_logic_vector(7 downto 0); o : out std_logic_vector(7 downto 0);
        flags : out std_logic_vector(5 downto 0));
end;
architecture b of top is
  function txBits return natural is
    variable n : natural := DataBits_g + 1;
  begin
    if not compareNoCase(Parity_g, "none") then n := n + 1; end if;
    return n;
  end;
  constant hasP : boolean := toUpper(trim(Parity_g)) /= toUpper(trim("none"));
  type rec_t is record
    cnt : natural range 0 to txBits*2 - 1;
    d : std_logic_vector(DataBits_g-1 downto 0);
  end record;
  signal r : rec_t;
begin
  process(clk) begin
    if rising_edge(clk) then r.d <= a; r.cnt <= r.cnt + 1; end if;
  end process;
  o <= r.d;
  flags(0) <= '1' when hasP else '0';
  flags(1) <= '1' when compareNoCase("  abc ", "ABC") else '0';
  flags(2) <= '1' when trim("  abc ")'length = 3 else '0';
  flags(3) <= '1' when toUpper("xyz") = "XYZ" else '0';
  flags(4) <= '1' when compareNoCase(Parity_g, " EVEN ") else '0';
  flags(5) <= '1' when txBits = 10 else '0';
end;
""")
        t = mods[[k for k in mods if k.startswith("top")][0]]
        self.assertEqual(codes(res), [])
        self.assertEqual(res.assumptions, ())
        for i in range(6):
            self.assertIn("flags[%d] <- 1'h1" % i, t)
        # cnt is 5 bits (0..19) next to the 8-bit data field
        self.assertIn("r[12:0]", t)

    def test_z_character_literal_under_pos_is_a_character(self):
        res, mods = run("""library ieee; use ieee.std_logic_1164.all;
entity top is port (o : out std_logic); end;
architecture b of top is
  constant c : natural := character'pos('z');
begin o <= '1' when c = 122 else '0'; end;
""")
        self.assertIn("o[0] <- 1'h1", mods["top"])


class TopLevelUnconstrainedPorts(unittest.TestCase):
    def test_default_width_with_assumption(self):
        res, mods = run("""library ieee; use ieee.std_logic_1164.all;
entity top is port (a, b : in std_logic_vector; o : out std_logic_vector); end;
architecture x of top is begin o <= a and b; end;
""")
        t = mods["top"]
        self.assertIn("port in  a[7:0]", t)
        self.assertIn("port out o[7:0]", t)
        self.assertTrue(any("unconstrained port 'a'" in a and "assumed" in a for a in res.assumptions))

    def test_instantiated_unconstrained_port_still_takes_the_actual(self):
        res, mods = run("""library ieee; use ieee.std_logic_1164.all;
entity sub is port (a : in std_logic_vector; o : out std_logic_vector); end;
architecture x of sub is begin o <= not a; end;
library ieee; use ieee.std_logic_1164.all;
entity top is port (a : in std_logic_vector(3 downto 0); o : out std_logic_vector(3 downto 0)); end;
architecture x of top is begin u : entity work.sub port map (a => a, o => o); end;
""", top="top")
        self.assertTrue(any("port in  a[3:0]" in v for v in mods.values()))
        self.assertEqual(res.assumptions, ())


if __name__ == "__main__":
    unittest.main()
