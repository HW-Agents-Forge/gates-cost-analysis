"""Normalized AST hashes: invariant under formatting, comments, case and independent reordering."""
import unittest

from gatecost.frontend.vhdl import parse_text

BASE = """
library ieee; use ieee.std_logic_1164.all;
entity e is port (a, b : in std_logic; y : out std_logic); end entity;
architecture rtl of e is
  signal s : std_logic;
  constant k : integer := 3;
begin
  s <= a and b;
  y <= s or a;
  process (a) begin
    if a = '1' then s <= b; end if;
  end process;
end architecture rtl;
"""


def h(src, kind="architecture"):
    r = parse_text(src)
    return r.hashes[("architecture", "work", "e", "rtl")] if kind == "architecture" else r.hashes[("entity", "work", "e")]


class HashTests(unittest.TestCase):
    def test_whitespace_comments_and_case_do_not_matter(self):
        alt = BASE.replace("  ", "\t").replace("\n", "\n\n") + "-- trailing"
        alt = alt.replace("signal s", "SIGNAL S").replace("begin", "BEGIN -- go\n") .replace("s <= a and b;", "S <= A /* c */ AND B;")
        self.assertEqual(h(BASE), h(alt))
        self.assertEqual(h(BASE, "entity"), h(alt, "entity"))

    def test_reordering_independent_declarations_and_statements(self):
        alt = BASE.replace("  signal s : std_logic;\n  constant k : integer := 3;",
                           "  constant k : integer := 3;\n  signal s : std_logic;")
        alt = alt.replace("  s <= a and b;\n  y <= s or a;", "  y <= s or a;\n  s <= a and b;")
        self.assertNotEqual(alt, BASE)
        self.assertEqual(h(BASE), h(alt))

    def test_semantic_changes_change_the_hash(self):
        for old, new in (("a and b", "a or b"), ("integer := 3", "integer := 4"), ("s <= b;", "s <= a;"),
                         ("y <= s or a", "y <= s or b"), ("std_logic;\n  constant", "std_ulogic;\n  constant")):
            self.assertNotEqual(h(BASE), h(BASE.replace(old, new)), old)

    def test_sequential_order_matters(self):
        a = "entity e is end; architecture rtl of e is begin process begin x := 1; y := 2; end process; end;"
        b = "entity e is end; architecture rtl of e is begin process begin y := 2; x := 1; end process; end;"
        self.assertNotEqual(h(a), h(b))

    def test_port_order_matters(self):
        a = "entity e is port (a : in bit; b : in bit); end;"
        b = "entity e is port (b : in bit; a : in bit); end;"
        self.assertNotEqual(h(a, "entity"), h(b, "entity"))

    def test_locations_and_skipped_contents_do_not_matter(self):
        a = "entity e is end; architecture rtl of e is begin assert a = b report \"x\"; end;"
        b = "\n\n\nentity e is end; architecture rtl of e is begin assert c /= d report \"yy\"; end;"
        self.assertEqual(h(a), h(b))

    def test_pragma_regions_do_not_matter(self):
        a = "entity e is end; architecture rtl of e is begin x <= a; end;"
        b = "entity e is end; architecture rtl of e is begin\n-- synopsys translate_off\nz <= q;\n-- synopsys translate_on\nx <= a; end;"
        self.assertEqual(h(a), h(b))

    def test_entity_hash_independent_of_architecture(self):
        self.assertEqual(h(BASE, "entity"), h(BASE.replace("a and b", "a xor b"), "entity"))

    def test_hash_is_deterministic_across_parses(self):
        self.assertEqual(h(BASE), h(BASE))

    def test_context_is_part_of_the_hash(self):
        a = "use ieee.numeric_std.all; entity e is end;"
        b = "use ieee.std_logic_1164.all; entity e is end;"
        self.assertNotEqual(h(a, "entity"), h(b, "entity"))

    def test_very_deep_left_chain_hashes_without_recursion_error(self):
        src = "entity e is end; architecture rtl of e is begin x <= %s; end;" % " + ".join(["a"] * 20000)
        self.assertTrue(h(src))


if __name__ == "__main__":
    unittest.main()
