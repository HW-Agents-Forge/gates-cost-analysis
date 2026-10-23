"""Every limit of FRONTEND.md section 7 is reached cleanly (diagnostic, partial result, no crash)."""
import unittest

from gatecost.ir import Limits

from tests.test_elab_vhdl_util import codes, module, nodes, ops, unit

P = "a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0)"


class LimitTests(unittest.TestCase):
    def test_generate_iterations(self):
        body = "g : for i in 0 to 99 generate y(i mod 8) <= a(i mod 8); end generate;"
        m, r, d = module(unit(P, "", body), limits={"generate_iterations": 10})
        self.assertIn("LIMIT_REACHED", codes(r, m))
        self.assertTrue(any("generate" in x.message for x in r.diagnostics))

    def test_default_generate_limit_is_4096(self):
        body = "g : for i in 0 to 5000 generate end generate; y <= a;"
        m, r, d = module(unit(P, "", body))
        self.assertTrue(any(x.code == "LIMIT_REACHED" and "4096" in x.message for x in r.diagnostics))

    def test_loop_unroll(self):
        body = "process(a) variable p : std_logic; begin p := '0'; for i in 0 to 2000 loop p := p xor a(i mod 8); end loop; y <= (others => p); end process;"
        m, r, d = module(unit(P, "", body), limits={"loop_unroll": 8})
        self.assertIn("LIMIT_REACHED", codes(r, m))
        self.assertLessEqual(len(nodes(m, "XOR")), 8)

    def test_while_loop_with_constant_condition_is_bounded(self):
        body = "process(a) variable n : integer := 0; begin while n < 100000 loop n := n + 1; end loop; y <= a; end process;"
        m, r, d = module(unit(P, "", body))
        self.assertIn("LIMIT_REACHED", codes(r, m))

    def test_while_with_data_dependent_condition_is_reported(self):
        body = "process(a) variable n : unsigned(7 downto 0); begin n := unsigned(a); while n > 0 loop n := n - 1; end loop; y <= std_logic_vector(n); end process;"
        m, r, d = module(unit(P, "", body))
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, m))

    def test_inline_depth_for_recursive_functions(self):
        decl = "function f(n : natural) return natural is begin if n = 0 then return 0; else return f(n - 1) + 1; end if; end function;"
        body = "y <= std_logic_vector(to_unsigned(f(100), 8));"
        m, r, d = module(unit(P, decl, body), limits={"inline_depth": 8})
        self.assertIn("LIMIT_REACHED", codes(r, m))
        m, r, d = module(unit(P, decl, "y <= std_logic_vector(to_unsigned(f(10), 8));"))
        self.assertEqual(m.conns[0].src.const_value().value, 10)

    def test_constant_function_step_limit(self):
        decl = ("function g(n : natural) return natural is variable s : natural := 0; begin "
                "for i in 0 to n loop for j in 0 to n loop s := s + 1; end loop; end loop; return s; end function;")
        body = "y <= std_logic_vector(to_unsigned(g(500), 8));"
        m, r, d = module(unit(P, decl, body), limits={"const_steps": 1000, "loop_unroll": 100000})
        self.assertIn("LIMIT_REACHED", codes(r, m))

    def test_total_instances(self):
        ent = ("entity leaf is port (a : in std_logic; y : out std_logic); end entity;\narchitecture r of leaf is begin y <= not a; end architecture;\n")
        body = "g : for i in 0 to 20 generate u : entity work.leaf port map (a => a(0), y => open); end generate; y <= a;"
        m, r, d = module(unit(P, "", body, extra=ent), limits={"max_instances": 5})
        self.assertEqual(len(nodes(m, "INST")), 5)
        self.assertIn("LIMIT_REACHED", codes(r, m))

    def test_hierarchy_depth(self):
        src = ("library ieee; use ieee.std_logic_1164.all;\nentity r is generic (N : integer := 0); port (a : in std_logic; y : out std_logic); end entity;\n"
               "architecture x of r is begin u : entity work.r generic map (N => N + 1) port map (a => a, y => y); end architecture;\n")
        from tests.test_elab_vhdl_util import elab
        r, d = elab(src, "r")
        self.assertTrue(any(x.code == "LIMIT_REACHED" for x in r.diagnostics))
        self.assertLess(len(d.modules), 400)

    def test_very_long_operator_chain_does_not_overflow_the_stack(self):
        expr = " xor ".join("a(%d)" % (i % 8) for i in range(3000))
        m, r, d = module(unit("a : in std_logic_vector(7 downto 0); y : out std_logic", "", "y <= %s;" % expr))
        self.assertTrue(nodes(m, "XOR") or m.conns)

    def test_deeply_nested_parentheses(self):
        expr = "(" * 300 + "a(0)" + ")" * 300
        m, r, d = module(unit("a : in std_logic_vector(7 downto 0); y : out std_logic", "", "y <= %s;" % expr))
        self.assertEqual(ops(m), [])

    def test_ir_node_limit_turns_the_module_into_a_black_box(self):
        body = "y <= std_logic_vector(unsigned(a) + unsigned(a(3 downto 0) & a(7 downto 4)) + 3) xor a;"
        m, r, d = module(unit(P, "", body), ir_limits=Limits(max_nodes=2), analyze=False)
        self.assertTrue(m.is_blackbox)
        self.assertIn("LIMIT_REACHED", [x.code for x in m.diagnostics])

    def test_net_width_limit(self):
        body = "y <= a;"
        m, r, d = module(unit("a : in std_logic_vector(99999 downto 0); y : out std_logic_vector(99999 downto 0)", "", body), analyze=False)
        self.assertTrue(m.is_blackbox)

    def test_memory_depth_limit(self):
        decl = "type ram_t is array (0 to 2097151) of std_logic_vector(7 downto 0); signal ram : ram_t;"
        body = "process(clk) begin if rising_edge(clk) then ram(to_integer(unsigned(a(3 downto 0)))) <= a; end if; end process; y <= a;"
        m, r, d = module(unit("clk : in std_logic;\n" + P, decl, body), analyze=False)
        self.assertTrue(m.is_blackbox)
        self.assertIn("LIMIT_REACHED", [x.code for x in m.diagnostics])

    def test_huge_combinational_dynamic_write_is_bounded(self):
        decl = "type ram_t is array (0 to 2097151) of std_logic_vector(7 downto 0); signal ram : ram_t;"
        body = "process(all) begin ram(to_integer(unsigned(a(3 downto 0)))) <= a; end process; y <= a;"
        m, r, d = module(unit(P, decl, body), analyze=False)
        self.assertTrue(any(x.code == "LIMIT_REACHED" for x in r.diagnostics) or m.is_blackbox)


if __name__ == "__main__":
    unittest.main()
