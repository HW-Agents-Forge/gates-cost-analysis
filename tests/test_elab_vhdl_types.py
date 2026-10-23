"""Types, constants, generics, generate forms, records, enumerations, overload resolution."""
import unittest

from tests.test_elab_vhdl_util import codes, elab, module, nodes, ops, probe, unit

FSM = """
type st_t is (s0, s1, s2, s3, s4);
signal st : st_t;
"""
FSM_BODY = """
process(clk) begin if rising_edge(clk) then
  if rst = '1' then st <= s0; else
    case st is
      when s0 => if go = '1' then st <= s1; end if;
      when s1 => st <= s2;
      when s2 => st <= s3;
      when s3 => st <= s4;
      when s4 => st <= s0;
    end case;
  end if; end if;
end process;
y <= '1' when st = s4 else '0';
"""
FSM_PORTS = "clk, rst, go : in std_logic; y : out std_logic"


def reg_width(m, name):
    return [n for n in m.nodes if n.op == "DFF" and n.outputs[0].name == name][0].outputs[0].width


class Enumerations(unittest.TestCase):
    def test_binary_encoding_is_ceil_log2(self):
        m, r, d = module(unit(FSM_PORTS, FSM, FSM_BODY))
        self.assertEqual(reg_width(m, "st"), 3)
        m, r, d = module(unit(FSM_PORTS, "type t2 is (a, b, c, d); signal st : t2;",
                              "process(clk) begin if rising_edge(clk) then st <= c; end if; end process; y <= '1' when st = d else '0';"))
        self.assertEqual(reg_width(m, "st"), 2)

    def test_one_hot_policy(self):
        m, r, d = module(unit(FSM_PORTS, FSM, FSM_BODY), fsm_encoding="one_hot")
        self.assertEqual(reg_width(m, "st"), 5)
        # reset value is the one-hot code of the first literal
        f = [n for n in m.nodes if n.op == "DFF"][0]
        self.assertEqual(f.attrs["srst_value"].value, 1)
        # the output compares a single state bit
        self.assertEqual(nodes(m, "EQ"), [])

    def test_one_hot_applies_to_next_state_signals_too(self):
        body = """
        process(clk) begin if rising_edge(clk) then st <= nx; end if; end process;
        process(all) begin nx <= st; case st is when s0 => nx <= s1; when s1 => nx <= s2; when others => nx <= s0; end case; end process;
        y <= '1' when st = s2 else '0';"""
        m, r, d = module(unit(FSM_PORTS, FSM + "signal nx : st_t;", body), fsm_encoding="one_hot")
        self.assertEqual(reg_width(m, "st"), 5)
        self.assertEqual([n.width for n in m.nodes if n.op == "DFF"], [5])

    def test_enum_encoding_attribute(self):
        decl = ("type st_t is (s0, s1, s2, s3, s4);\nattribute enum_encoding : string;\n"
                "attribute enum_encoding of st_t : type is \"onehot\";\nsignal st : st_t;")
        m, r, d = module(unit(FSM_PORTS, decl, FSM_BODY))
        self.assertEqual(reg_width(m, "st"), 5)

    def test_non_state_enum_stays_binary_under_one_hot(self):
        decl = "type op_t is (add, sub, andd, orr); signal op : op_t;"
        body = "op <= add when go = '1' else sub; y <= '1' when op = sub else '0';"
        m, r, d = module(unit(FSM_PORTS, decl, body), fsm_encoding="one_hot")
        self.assertEqual([n for n in m.nodes if n.op == "DFF"], [])

    def test_boolean_and_bit_are_one_bit(self):
        v, _ = probe("signal b : boolean; signal t : bit;", "b")
        self.assertEqual(v.sig.width, 1)
        v, _ = probe("signal b : boolean; signal t : bit;", "t")
        self.assertEqual(v.sig.width, 1)


class IntegersAndSubtypes(unittest.TestCase):
    def test_integer_range_width(self):
        for decl, w, sg in [("integer range 0 to 15", 4, False), ("integer range -8 to 7", 4, True), ("integer range 0 to 16", 5, False),
                            ("integer range -9 to 7", 5, True), ("natural range 0 to 1", 1, False), ("integer", 32, True),
                            ("natural", 31, False), ("positive", 31, False)]:
            with self.subTest(decl=decl):
                v, _ = probe("signal x : %s;" % decl, "x")
                self.assertEqual((v.sig.width, bool(v.t.signed)), (w, sg))

    def test_user_integer_type_and_subtype(self):
        v, _ = probe("type byte_i is range 0 to 255; subtype nib is byte_i range 0 to 15; signal x : byte_i; signal y : nib;", "y")
        self.assertEqual(v.sig.width, 4)
        v, _ = probe("type byte_i is range 0 to 255; signal x : byte_i;", "x")
        self.assertEqual(v.sig.width, 8)

    def test_subtype_of_vector_and_alias(self):
        v, _ = probe("subtype word is std_logic_vector(31 downto 0); signal w : word;", "w")
        self.assertEqual(v.sig.width, 32)
        v, _ = probe("signal a : std_logic_vector(7 downto 0); alias hi : std_logic_vector(3 downto 0) is a(7 downto 4);", "hi")
        self.assertEqual(v.sig.width, 4)
        self.assertEqual([c.lo for c in v.sig.chunks], [4])

    def test_slice_keeps_the_source_index_range(self):
        v, _ = probe("signal a : std_logic_vector(15 downto 0);", "a(15 downto 8)")
        self.assertEqual((v.t.left, v.t.right), (15, 8))
        v, _ = probe("signal a : std_logic_vector(15 downto 0);", "a(15 downto 8)(9)")
        self.assertEqual(v.sig.chunks[0].lo, 9)

    def test_ascending_vectors_put_the_left_element_at_the_msb(self):
        v, _ = probe("signal a : std_logic_vector(0 to 7);", "a(0)")
        self.assertEqual(v.sig.chunks[0].lo, 7)
        v, _ = probe("signal a : std_logic_vector(0 to 7);", "a(0 to 3)")
        self.assertEqual((v.sig.chunks[0].lo, v.sig.width), (4, 4))
        v, _ = probe("signal a : std_logic_vector(0 to 7);", "to_integer(unsigned(a(0 to 1)))")
        self.assertEqual(v.sig.width, 2)

    def test_dynamic_index_into_ascending_vector(self):
        m, r, d = module(unit("a : in std_logic_vector(0 to 3); i : in unsigned(1 downto 0); y : out std_logic", "",
                              "y <= a(to_integer(i));"))
        self.assertEqual(len(nodes(m, "MUX")), 3)


class RecordsAndArrays(unittest.TestCase):
    PKG = ("package pk is type rec is record a : std_logic_vector(3 downto 0); v : std_logic; end record; "
           "type rec_arr is array (0 to 1) of rec; end package;\n")

    def test_record_signal_is_flattened(self):
        decls = "type rec is record a : std_logic_vector(3 downto 0); v : std_logic; end record; signal r : rec;"
        m, r, d = module(unit("clk : in std_logic; x : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0); z : out std_logic", decls,
                              "process(clk) begin if rising_edge(clk) then r.a <= x; r.v <= x(0); end if; end process; y <= r.a; z <= r.v;"))
        self.assertEqual(sum(n.width for n in m.nodes if n.op == "DFF"), 5)

    def test_record_ports_are_flattened_at_the_boundary(self):
        src = (self.PKG + "library ieee; use ieee.std_logic_1164.all; use work.pk.all;\n"
               "entity t is port (i : in rec; o : out rec); end entity;\n"
               "architecture a of t is begin o.a <= not i.a; o.v <= i.v; end architecture;\n")
        m, r, d = module(src)
        self.assertEqual([(p.name, p.width) for p in m.ports], [("i.a", 4), ("i.v", 1), ("o.a", 4), ("o.v", 1)])

    def test_record_aggregates_and_comparison(self):
        decls = "type rec is record a : std_logic_vector(3 downto 0); v : std_logic; end record; signal r, s : rec;"
        m, res, d = module(unit("x, z : in std_logic_vector(3 downto 0); c : in std_logic; y : out std_logic", decls,
                                "r <= (a => x, v => c); s <= (z, c); y <= '1' when r = s else '0';"))
        self.assertEqual(ops(m), ["EQ"])

    def test_array_of_arrays_two_dimensional(self):
        decl = "type m_t is array (0 to 3, 0 to 3) of std_logic; signal s : m_t;"
        v, _ = probe(decl, "s")
        self.assertEqual(v.sig.width, 16)
        m, r, d = module(unit("clk : in std_logic; i, j : in integer range 0 to 3; y : out std_logic", decl, "y <= s(i, j);"))
        self.assertEqual(len(nodes(m, "MUX")), 6)       # 3 row muxes (4 bits wide) + 3 bit muxes

    def test_aggregates(self):
        for expr, val in [("(others => '1')", 15), ("(3 => '1', others => '0')", 8), ("(3 downto 2 => '1', 1 downto 0 => '0')", 12),
                          ("('1', '0', '0', '1')", 9), ("(0 => '1', 1 => '1', others => '0')", 3)]:
            with self.subTest(expr=expr):
                v, _ = probe("", expr, hint=probe("signal x : std_logic_vector(3 downto 0);", "x")[0].t)
                self.assertEqual(v.sig.const_value().value, val)

    def test_bit_string_literals(self):
        for expr, val, w in [('x"A5"', 0xA5, 8), ('b"101"', 5, 3), ('o"17"', 0o17, 6), ('12x"FF"', 0xFF, 12), ('8d"10"', 10, 8),
                             ('12SX"F0"', 0xFF0, 12), ('"0110"', 6, 4)]:
            with self.subTest(expr=expr):
                v, _ = probe("", expr)
                self.assertEqual((v.sig.const_value().value, v.sig.width), (val, w))

    def test_std_logic_literals(self):
        v, _ = probe("", "'Z'")
        self.assertTrue(v.z)
        for ch, xm in [("'0'", 0), ("'1'", 0), ("'-'", 1), ("'X'", 1), ("'L'", 0), ("'H'", 0)]:
            v, _ = probe("", ch)
            self.assertEqual(v.sig.const_value().xmask, xm, ch)


class GenericsAndConstants(unittest.TestCase):
    def test_generic_default_and_override(self):
        src = unit("y : out std_logic_vector(W - 1 downto 0)", "", "y <= (others => '1');", generics="W : natural := 6")
        m, r, d = module(src)
        self.assertEqual(m.port("y").width, 6)
        self.assertEqual(m.key, "t(w=6)")
        m, r, d = module(src, generics={"w": 3})
        self.assertEqual(m.port("y").width, 3)
        self.assertEqual(m.key, "t(w=3)")

    def test_unresolved_generic_is_flagged(self):
        src = unit("y : out std_logic_vector(W - 1 downto 0)", "", "y <= (others => '1');", generics="W : natural")
        m, r, d = module(src)
        self.assertEqual(m.port("y").width, 32)
        self.assertIn("UNRESOLVED_PARAM", codes(r, m))
        self.assertTrue(r.assumptions)

    def test_boolean_and_string_generics(self):
        src = unit("a : in std_logic; y : out std_logic", "", "g1: if FAST generate y <= a; end generate; g2: if not FAST generate y <= not a; end generate;",
                   generics="FAST : boolean := true; MODE : string := \"abc\"")
        m, r, d = module(src)
        self.assertEqual(ops(m), [])
        m, r, d = module(src, generics={"FAST": False})
        self.assertEqual(ops(m), ["NOT"])

    def test_constants_depend_on_generics_and_functions(self):
        decl = ("function clog2(n : natural) return natural is variable r : natural := 0; variable v : natural := n - 1; "
                "begin while v > 0 loop v := v / 2; r := r + 1; end loop; return r; end function;\n"
                "constant AW : natural := clog2(DEPTH);\nsignal t : std_logic_vector(AW - 1 downto 0);")
        src = unit("a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0)", decl, "t <= a(AW - 1 downto 0); y <= (others => t(0));",
                   generics="DEPTH : natural := 100")
        m, r, d = module(src)
        self.assertEqual([n.width for n in m.nets if n.name == "t"], [7])

    def test_package_constants_functions_and_deferred_constants(self):
        pkg = ("package cfg is constant DEF : natural; function twice(n : natural) return natural; type mode_t is (fast, slow); end package;\n"
               "package body cfg is constant DEF : natural := 5; function twice(n : natural) return natural is begin return 2 * n; end function; end package body;\n"
               "library ieee; use ieee.std_logic_1164.all; use work.cfg.all;\n")
        src = (pkg + "entity t is port (y : out std_logic_vector(twice(DEF) - 1 downto 0)); end entity;\n"
               "architecture a of t is begin y <= (others => '0'); end architecture;\n")
        m, r, d = module(src)
        self.assertEqual(m.port("y").width, 10)
        self.assertNotIn("MISSING_UNIT", codes(r, m))

    def test_math_real_constant(self):
        src = unit("y : out std_logic_vector(integer(ceil(log2(real(DEPTH)))) - 1 downto 0)", "", "y <= (others => '1');",
                   generics="DEPTH : natural := 1000", use="use ieee.math_real.all;\n")
        m, r, d = module(src)
        self.assertEqual(m.port("y").width, 10)

    def test_attributes_in_port_types(self):
        src = unit("a : in std_logic_vector(7 downto 0); y : out std_logic_vector(a'range)", "", "y <= a;")
        m, r, d = module(src)
        self.assertEqual(m.port("y").width, 8)

    def test_constant_array_and_indexed_read_with_constant_index(self):
        decl = "type t_a is array (0 to 2) of natural; constant K : t_a := (3, 5, 7);"
        v, _ = probe(decl, "K(1)")
        self.assertEqual(v.sig.const_value().value, 5)


class GenerateForms(unittest.TestCase):
    def test_for_generate_replicates(self):
        m, r, d = module(unit("a : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)", "",
                              "g: for i in 0 to 3 generate y(i) <= not a(i); end generate;"))
        self.assertEqual(ops(m), ["NOT"] * 4)

    def test_for_generate_descending_and_nested(self):
        m, r, d = module(unit("a : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)", "",
                              "g: for i in 3 downto 0 generate g2: for j in 0 to 0 generate y(i) <= a(i) and a(j); end generate; end generate;"))
        self.assertEqual(len(nodes(m, "AND")), 3)

    def test_if_generate_with_elsif_else(self):
        for n, expect in [(1, []), (2, ["NOT"]), (3, ["AND"])]:
            with self.subTest(n=n):
                body = ("g: if N = 1 generate y <= a; elsif N = 2 generate y <= not a; else generate y <= a and b; end generate;")
                m, r, d = module(unit("a, b : in std_logic; y : out std_logic", "", body, generics="N : integer := %d" % n))
                self.assertEqual(ops(m), expect)

    def test_case_generate(self):
        body = ("g: case N generate when 0 => y <= a; when 1 | 2 => y <= not a; when others => y <= '0'; end generate;")
        for n, expect in [(0, []), (1, ["NOT"]), (2, ["NOT"]), (5, [])]:
            with self.subTest(n=n):
                m, r, d = module(unit("a : in std_logic; y : out std_logic", "", body, generics="N : integer := %d" % n))
                self.assertEqual(ops(m), expect)

    def test_generate_with_local_signals_and_blocks(self):
        body = ("g: for i in 0 to 1 generate signal t : std_logic; begin t <= a(i) xor b(i); y(i) <= t; end generate;\n"
                "blk: block signal u : std_logic; begin u <= a(0) and b(0); z <= u; end block;")
        m, r, d = module(unit("a, b : in std_logic_vector(1 downto 0); y : out std_logic_vector(1 downto 0); z : out std_logic", "", body))
        self.assertEqual(ops(m), ["AND", "XOR", "XOR"])


class OverloadResolution(unittest.TestCase):
    def test_mixed_unsigned_and_integer(self):
        decl = "signal a : unsigned(7 downto 0); signal n : natural; signal i : integer range -4 to 3;"
        for expr, cls, w in [("a + n", "u", 8), ("n + a", "u", 8), ("a - 1", "u", 8), ("a * n", "u", 16), ("a < n", "bool", 1),
                             ("a = 255", "bool", 1), ("to_integer(a) + n", "nat", None), ("to_unsigned(n, 8) + a", "u", 8),
                             ("resize(a, 4) + to_unsigned(i + 4, 4)", "u", 4)]:
            with self.subTest(expr=expr):
                v, _ = probe(decl, expr)
                self.assertEqual(v.t.cls, cls)
                if w is not None:
                    self.assertEqual(v.sig.width, w)

    def test_unresolved_overload_is_a_diagnostic_not_a_crash(self):
        m, r, d = module(unit("a : in std_logic; y : out std_logic", "", "y <= mystery(a);"))
        self.assertIn("UNRESOLVED_OVERLOAD", codes(r, m))
        m, r, d = module(unit("a : in std_logic; y : out std_logic", "", "y <= a + 'Z';"))
        self.assertTrue(any(x.code in ("UNRESOLVED_OVERLOAD", "UNSUPPORTED_CONSTRUCT") for x in r.diagnostics))

    def test_user_defined_overloads(self):
        decl = ("function f(x : unsigned) return unsigned is begin return x + 1; end function;\n"
                "function f(x : std_logic_vector) return std_logic_vector is begin return not x; end function;")
        m, r, d = module(unit("a : in unsigned(3 downto 0); b : in std_logic_vector(3 downto 0); y : out unsigned(3 downto 0); z : out std_logic_vector(3 downto 0)",
                              decl, "y <= f(a); z <= f(b);"))
        self.assertEqual(ops(m), ["ADD", "NOT"])

    def test_user_defined_operator(self):
        decl = "function \"+\" (l, r : std_logic_vector) return std_logic_vector is begin return l xor r; end function;"
        m, r, d = module(unit("a, b : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)", decl, "y <= a + b;"))
        self.assertEqual(ops(m), ["XOR"])

    def test_numeric_std_unsigned_package(self):
        m, r, d = module(unit("a, b : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0)", "", "y <= a + b;",
                              use="use ieee.numeric_std_unsigned.all;\n"))
        self.assertEqual(ops(m), ["ADD"])

    def test_variable_shift_and_rotate(self):
        m, r, d = module(unit("a : in unsigned(7 downto 0); n : in integer range 0 to 7; y, z : out unsigned(7 downto 0)", "",
                              "y <= shift_left(a, n); z <= rotate_right(a, n);"))
        self.assertEqual(ops(m), ["ROR", "SHL"])


class MoreSemantics(unittest.TestCase):
    def test_vector_and_string_generics(self):
        src = unit("a : in std_logic; y : out std_logic_vector(7 downto 0); z : out std_logic", "",
                   "y <= INIT; g1 : if MODE = \"fast\" generate z <= a; end generate; g2 : if MODE /= \"fast\" generate z <= not a; end generate;",
                   generics="INIT : std_logic_vector(7 downto 0) := x\"a5\"; MODE : string := \"fast\"")
        m, r, d = module(src)
        self.assertEqual(m.key, "t(init=8'ha5,mode=fast)")
        self.assertEqual(m.conns[0].src.const_value().value if m.conns[0].net.name == "y" else None, 0xA5)
        m, r, d = module(src, generics={"MODE": "slow"})
        self.assertEqual(ops(m), ["NOT"])

    def test_negative_integer_constants_and_conversions(self):
        m, r, d = module(unit("y : out std_logic_vector(7 downto 0); z : out integer range -8 to 7", "signal x : integer range -8 to 7;",
                              "x <= -3; y <= std_logic_vector(to_signed(-1, 8)); z <= x;"))
        vals = {c.net.name: c.src.const_value() for c in m.conns if c.src.const_value() is not None}
        self.assertEqual(vals["y"].value, 0xFF)
        self.assertEqual(vals["x"].value, 0b1101)

    def test_qualified_expression_and_boolean_signal(self):
        m, r, d = module(unit("a, b : in unsigned(3 downto 0); y : out std_logic; w : out unsigned(3 downto 0)", "signal flag : boolean;",
                              "flag <= a > b; process(all) begin if flag then y <= '1'; else y <= '0'; end if; end process; w <= unsigned'(\"0101\") + a;"))
        self.assertEqual(ops(m), ["ADD", "GT"])

    def test_nested_aggregate_constant_rom(self):
        decl = ("type rom_t is array (0 to 3) of std_logic_vector(7 downto 0); constant Q : rom_t := (0 => x\"ff\", others => x\"01\");")
        m, r, d = module(unit("a : in std_logic_vector(1 downto 0); y : out std_logic_vector(7 downto 0)", decl, "y <= Q(to_integer(unsigned(a)));"))
        self.assertEqual(nodes(m, "ROM")[0].attrs["contents"], (255, 1, 1, 1))

    def test_integer_case_selector(self):
        m, r, d = module(unit("i : in integer range 0 to 3; y : out std_logic_vector(3 downto 0)", "",
                              "process(all) begin case i is when 0 => y <= \"0001\"; when 1 => y <= \"0010\"; when 2 => y <= \"0100\"; when others => y <= \"1000\"; end case; end process;"))
        self.assertEqual(len(nodes(m, "PMUX")), 1)

    def test_sizes_checked_by_assumption_not_crash(self):
        m, r, d = module(unit("a : in std_logic_vector(3 downto 0); y : out std_logic_vector(7 downto 0)", "", "y <= a;"))
        self.assertTrue(any("width mismatch" in x for x in r.assumptions))
        self.assertEqual(m.conns[0].src.width, 8)

    def test_ports_and_reads_of_out_ports(self):
        m, r, d = module(unit("a : in std_logic; y : out std_logic; z : out std_logic", "", "y <= not a; z <= y;"))
        self.assertEqual(ops(m), ["NOT"])

    def test_inout_and_buffer_ports(self):
        m, r, d = module(unit("en, a : in std_logic; io : inout std_logic; b : buffer std_logic", "", "io <= a when en = '1' else 'Z'; b <= a;"))
        self.assertEqual(sorted((p.name, p.port) for p in m.ports), [("a", "in"), ("b", "out"), ("en", "in"), ("io", "out")])


class ProjectHook(unittest.TestCase):
    """The hook contract of gatecost.elab.elaborate_project (limits dict, only_modules, root, parsed_hashes)."""

    def test_elaborate_project_routes_vhdl_files(self):
        import os
        import tempfile
        from gatecost.elab import elaborate_project
        src = ("library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;\n"
               "entity leaf is generic (W : natural := 4); port (a, b : in unsigned(W-1 downto 0); y : out unsigned(W-1 downto 0)); end;\n"
               "architecture r of leaf is begin y <= a + b; end;\n"
               "library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;\n"
               "entity top is port (a, b : in unsigned(7 downto 0); y : out unsigned(7 downto 0)); end;\n"
               "architecture r of top is begin u: entity work.leaf generic map (W => 8) port map (a, b, y); end;\n")
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "a.vhd"), "w") as fh:
                fh.write(src)
            res = elaborate_project(["a.vhd"], root=td)
            self.assertEqual((res.top_key, res.tops), ("top", ["top"]))
            self.assertEqual(sorted(res.design.modules), ["leaf(w=8)", "top"])
            self.assertEqual(res.design.modules["top"].nodes[0].origin.file, "a.vhd")
            self.assertEqual(sorted(res.parsed_hashes), ["leaf", "top"])
            res = elaborate_project(["a.vhd"], root=td, only_modules={"top"})
            self.assertEqual(sorted(res.design.modules), ["top"])
            self.assertEqual(res.stubbed, ("leaf(w=8)",))
            res = elaborate_project(["a.vhd"], root=td, only_modules={"leaf"})
            self.assertEqual(sorted(res.design.modules), ["leaf(w=8)", "top"])
            res = elaborate_project(["a.vhd"], root=td, top="leaf")
            self.assertEqual(res.top_key, "leaf(w=4)")


if __name__ == "__main__":
    unittest.main()
