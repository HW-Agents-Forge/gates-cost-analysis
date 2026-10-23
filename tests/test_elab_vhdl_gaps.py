"""VHDL elaborator gap closers: variable slices, unconstrained ports, port-map conversions, guarded blocks, byte enables."""
import unittest

from tests.elab_helpers import comb
from tests.test_elab_vhdl_util import codes, module, nodes, ops, unit

SUB_X = """
entity sub is port (x : in std_logic_vector; z : out std_logic_vector); end entity;
architecture a of sub is begin z <= not x; end architecture;
"""


class VariableSlices(unittest.TestCase):
    def test_read_slice_at_variable_position(self):
        m, r, _ = module(unit("a : in std_logic_vector(15 downto 0); i : in integer range 0 to 12; "
                              "y : out std_logic_vector(3 downto 0)", "", "y <= a(i+3 downto i);"))
        self.assertEqual(codes(r, m), [])
        a = 0xBEEF
        for i in range(13):
            self.assertEqual(comb(m, a=a, i=i)["y"], (a >> i) & 0xF, i)

    def test_read_slice_to_direction(self):
        m, r, _ = module(unit("a : in std_logic_vector(0 to 15); i : in integer range 0 to 12; "
                              "y : out std_logic_vector(0 to 3)", "", "y <= a(i to i+3);"))
        a = 0xBEEF           # a(0) is the most significant bit
        for i in range(13):
            self.assertEqual(comb(m, a=a, i=i)["y"], (a >> (12 - i)) & 0xF, i)

    def test_slice_of_unsigned_with_converted_index(self):
        m, r, _ = module(unit("a : in unsigned(15 downto 0); i : in unsigned(3 downto 0); y : out unsigned(3 downto 0)",
                              "", "y <= a(to_integer(i)+3 downto to_integer(i));"))
        self.assertEqual(comb(m, a=0x1234, i=4)["y"], 0x3)

    def test_non_constant_width_is_diagnosed(self):
        m, r, _ = module(unit("a : in std_logic_vector(15 downto 0); i : in integer range 0 to 12; "
                              "y : out std_logic_vector(3 downto 0)", "", "y <= a(i downto 0);"))
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, m))

    def test_write_slice_at_variable_position(self):
        m, r, _ = module(unit("d : in std_logic_vector(3 downto 0); i : in integer range 0 to 4; "
                              "y : out std_logic_vector(7 downto 0)", "",
                              "process(d, i) begin y <= (others => '0'); y(i+3 downto i) <= d; end process;"))
        self.assertEqual(comb(m, d=0xA, i=3)["y"], 0xA << 3)
        self.assertEqual(comb(m, d=0xF, i=0)["y"], 0xF)


class Ports(unittest.TestCase):
    def test_unconstrained_ports_take_the_width_of_the_association(self):
        top = unit("a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0)", "",
                   "u: entity work.sub port map (x => a, z => y);", extra=SUB_X)
        m, r, d = module(top)
        self.assertFalse(any("no constant width" in s for s in r.assumptions))
        key = nodes(m, "INST")[0].attrs["module_key"]
        child = d.modules[key]
        self.assertEqual(sorted((p.name, p.width) for p in child.ports), [("x", 8), ("z", 8)])
        self.assertEqual(comb(child, x=0x0F)["z"], 0xF0)

    def test_two_widths_give_two_modules(self):
        body = ("u1: entity work.sub port map (x => a, z => y);\n"
                "u2: entity work.sub port map (x => b, z => w);")
        top = unit("a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0);"
                   " b : in std_logic_vector(3 downto 0); w : out std_logic_vector(3 downto 0)", "", body, extra=SUB_X)
        m, r, d = module(top)
        keys = {n.attrs["module_key"] for n in nodes(m, "INST")}
        self.assertEqual(len(keys), 2)
        self.assertEqual(sorted(sorted(p.width for p in d.modules[k].ports) for k in keys), [[4, 4], [8, 8]])

    def test_conversion_function_on_output_formal(self):
        extra = """
entity sub2 is port (x : in std_logic_vector(7 downto 0); z : out unsigned(7 downto 0)); end entity;
architecture a of sub2 is begin z <= unsigned(x) + 1; end architecture;
"""
        top = unit("a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0)", "",
                   "u1: entity work.sub2 port map (x => a, std_logic_vector(z) => y);", extra=extra)
        m, r, _ = module(top)
        self.assertEqual(codes(r, m), [])

    def test_instance_output_to_indexed_targets(self):
        extra = """
entity sub3 is port (x : in std_logic_vector(7 downto 0); z : out std_logic); end entity;
architecture a of sub3 is begin z <= x(0) and x(1); end architecture;
"""
        top = unit("a : in std_logic_vector(7 downto 0); y : out std_logic_vector(1 downto 0)", "",
                   "u1: entity work.sub3 port map (x => a, z => y(0));\nu2: entity work.sub3 port map (x => a, z => y(1));",
                   extra=extra)
        m, r, _ = module(top)
        self.assertEqual(codes(r, m), [])
        self.assertEqual(len(nodes(m, "INST")), 2)

    def test_instance_output_to_aggregate_target(self):
        extra = """
entity sub4 is port (x : in std_logic_vector(7 downto 0); z : out std_logic_vector(1 downto 0)); end entity;
architecture a of sub4 is begin z <= x(1 downto 0); end architecture;
"""
        top = unit("a : in std_logic_vector(7 downto 0); y : out std_logic; w : out std_logic", "",
                   "u1: entity work.sub4 port map (x => a, z(1) => y, z(0) => w);", extra=extra)
        m, r, _ = module(top)
        self.assertEqual(codes(r, m), [])

    def test_instance_output_to_whole_aggregate(self):
        extra = """
entity sub4 is port (x : in std_logic_vector(7 downto 0); z : out std_logic_vector(1 downto 0)); end entity;
architecture a of sub4 is begin z <= x(1 downto 0); end architecture;
"""
        top = unit("a : in std_logic_vector(7 downto 0); y : out std_logic; w : out std_logic", "",
                   "u1: entity work.sub4 port map (x => a, z(1) => y, z(0) => w);", extra=extra)
        m, r, _ = module(top)
        self.assertEqual(ops(m), ["INST"])


class Blocks(unittest.TestCase):
    def test_guarded_block_is_diagnosed(self):
        m, r, _ = module(unit("a : in std_logic; en : in std_logic; y : out std_logic", "",
                              "b1: block (en = '1') begin y <= guarded a; end block;"))
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, m))

    def test_unguarded_block_is_fine(self):
        m, r, _ = module(unit("a : in std_logic; y : out std_logic", "", "b1: block begin y <= a; end block;"))
        self.assertEqual(codes(r, m), [])


class Memories(unittest.TestCase):
    def test_byte_enable_writes_are_one_lane_port(self):
        m, r, _ = module(unit(
            "clk : in std_logic; we : in std_logic_vector(3 downto 0); ad : in unsigned(3 downto 0); "
            "d : in std_logic_vector(31 downto 0); q : out std_logic_vector(31 downto 0)",
            "type ram_t is array (0 to 15) of std_logic_vector(31 downto 0); signal ram : ram_t;",
            """process(clk) begin if rising_edge(clk) then
 for i in 0 to 3 loop if we(i) = '1' then ram(to_integer(ad))(8*i+7 downto 8*i) <= d(8*i+7 downto 8*i); end if; end loop;
 q <= ram(to_integer(ad)); end if; end process;"""))
        mem = nodes(m, "MEM")[0]
        self.assertEqual(mem.attrs["wports"], 1)
        self.assertEqual(len(mem.attrs["wlanes"][0]), 4)
        self.assertEqual(dict(mem.inputs)["w0_be"].width, 4)

    def test_full_word_write_has_no_lanes(self):
        m, r, _ = module(unit(
            "clk : in std_logic; we : in std_logic; ad : in unsigned(3 downto 0); d : in std_logic_vector(7 downto 0); "
            "q : out std_logic_vector(7 downto 0)",
            "type ram_t is array (0 to 15) of std_logic_vector(7 downto 0); signal ram : ram_t;",
            "process(clk) begin if rising_edge(clk) then if we = '1' then ram(to_integer(ad)) <= d; end if; end if; end process;\n"
            "q <= ram(to_integer(ad));"))
        self.assertNotIn("wlanes", nodes(m, "MEM")[0].attrs)


class Generics(unittest.TestCase):
    def test_generic_type_is_bound_from_the_generic_map(self):
        extra = """
entity gt is generic (type T); port (d : in T; q : out T); end entity;
architecture a of gt is begin q <= d; end architecture;
"""
        top = unit("a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0)", "",
                   "u: entity work.gt generic map (T => std_logic_vector(7 downto 0)) port map (d => a, q => y);", extra=extra)
        m, r, d = module(top)
        self.assertEqual(codes(r, m), [])
        child = d.modules[nodes(m, "INST")[0].attrs["module_key"]]
        self.assertEqual(sorted((p.name, p.width) for p in child.ports), [("d", 8), ("q", 8)])

    def test_two_generic_type_actuals_give_two_modules(self):
        extra = """
entity gt is generic (type T); port (d : in T; q : out T); end entity;
architecture a of gt is begin q <= d; end architecture;
"""
        body = ("u1: entity work.gt generic map (T => std_logic_vector(7 downto 0)) port map (d => a, q => y);\n"
                "u2: entity work.gt generic map (T => std_logic_vector(3 downto 0)) port map (d => b, q => w);")
        top = unit("a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0);"
                   " b : in std_logic_vector(3 downto 0); w : out std_logic_vector(3 downto 0)", "", body, extra=extra)
        m, r, d = module(top)
        self.assertEqual(len({n.attrs["module_key"] for n in nodes(m, "INST")}), 2)

    def test_package_instantiated_in_a_declarative_region(self):
        extra = """
package gp is generic (K : integer := 3); function f(x : unsigned) return unsigned; end package;
package body gp is function f(x : unsigned) return unsigned is begin return x + K; end function; end package body;
"""
        m, r, _ = module(unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)",
                              "package gp5 is new work.gp generic map (K => 5);", "y <= gp5.f(a);", extra=extra))
        self.assertEqual(comb(m, a=1)["y"], 6)


class Loops(unittest.TestCase):
    def test_while_loop_with_constant_condition_is_unrolled(self):
        m, r, _ = module(unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", "",
                              """process(a) variable n : integer; variable acc : unsigned(7 downto 0); begin
 acc := a; n := 1; while n < W loop acc := acc + n; n := n * 2; end loop; y <= acc; end process;""",
                              generics="W : integer := 16"))
        self.assertEqual(comb(m, a=0)["y"], 1 + 2 + 4 + 8)
        self.assertEqual(codes(r, m), [])

    def test_while_loop_with_data_dependent_condition_is_diagnosed(self):
        m, r, _ = module(unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", "",
                              """process(a) variable i : unsigned(7 downto 0); begin
 i := a; while i /= 0 loop i := i - 1; end loop; y <= i; end process;"""))
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, m))


if __name__ == "__main__":
    unittest.main()
