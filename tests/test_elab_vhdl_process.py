"""Process lowering: clocked-process recognition, resets, enables, latches, priority/case, variables."""
import unittest

from tests.test_elab_vhdl_util import codes, module, nodes, ops, unit


def dffs(m):
    return nodes(m, "DFF")


PORTS = "clk, rst, en : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0)"


class ClockedRecognition(unittest.TestCase):
    def one(self, body, ports=PORTS, decls=""):
        m, r, d = module(unit(ports, decls, body))
        f = dffs(m)
        self.assertEqual(len(f), 1, ops(m))
        return f[0], m

    def test_rising_edge(self):
        f, m = self.one("process(clk) begin if rising_edge(clk) then q <= d; end if; end process;")
        self.assertEqual((f.attrs["clk_edge"], f.attrs["reset_kind"], f.attrs["enable_recognized"]), ("pos", "none", False))

    def test_falling_edge(self):
        f, m = self.one("process(clk) begin if falling_edge(clk) then q <= d; end if; end process;")
        self.assertEqual(f.attrs["clk_edge"], "neg")

    def test_event_styles(self):
        for cond, edge in [("clk'event and clk = '1'", "pos"), ("clk = '1' and clk'event", "pos"),
                           ("clk'event and clk = '0'", "neg"), ("not clk'stable and clk = '1'", "pos")]:
            with self.subTest(cond=cond):
                f, m = self.one("process(clk) begin if %s then q <= d; end if; end process;" % cond)
                self.assertEqual(f.attrs["clk_edge"], edge)

    def test_wait_until(self):
        f, m = self.one("process begin wait until rising_edge(clk); q <= d; end process;")
        self.assertEqual(f.attrs["clk_edge"], "pos")
        f, m = self.one("process begin wait until clk'event and clk = '1'; q <= d; end process;")
        self.assertEqual(f.attrs["clk_edge"], "pos")

    def test_synchronous_reset(self):
        f, m = self.one("process(clk) begin if rising_edge(clk) then if rst = '1' then q <= (others => '0'); else q <= d; end if; end if; end process;")
        self.assertEqual((f.attrs["reset_kind"], f.attrs["srst_polarity"]), ("sync", 1))
        self.assertEqual(f.attrs["srst_value"].value, 0)
        self.assertEqual(ops(m), ["DFF"])

    def test_active_low_synchronous_reset(self):
        f, m = self.one("process(clk) begin if rising_edge(clk) then if rst = '0' then q <= x\"a\"; else q <= d; end if; end if; end process;")
        self.assertEqual(f.attrs["reset_kind"], "sync")
        self.assertEqual(f.attrs["srst_value"].value, 10)
        self.assertEqual(ops(m), ["DFF", "NOT"])          # active-low reset: the condition is inverted

    def test_asynchronous_reset(self):
        f, m = self.one("process(clk, rst) begin if rst = '1' then q <= x\"5\"; elsif rising_edge(clk) then q <= d; end if; end process;")
        self.assertEqual((f.attrs["reset_kind"], f.attrs["arst_polarity"]), ("async", 1))
        self.assertEqual(f.attrs["reset_value"].value, 5)

    def test_asynchronous_reset_active_low_and_event_clock(self):
        f, m = self.one("process(clk, rst) begin if rst = '0' then q <= (others => '1'); elsif clk'event and clk = '1' then q <= d; end if; end process;")
        self.assertEqual((f.attrs["reset_kind"], f.attrs["arst_polarity"]), ("async", 0))
        self.assertEqual(f.attrs["reset_value"].value, 15)

    def test_async_and_sync_reset_together(self):
        ports = "clk, rst, srst : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0)"
        f, m = self.one("process(clk, rst) begin if rst = '1' then q <= x\"0\"; elsif rising_edge(clk) then "
                        "if srst = '1' then q <= x\"3\"; else q <= d; end if; end if; end process;", ports)
        self.assertEqual(f.attrs["reset_kind"], "both")

    def test_clock_enable(self):
        f, m = self.one("process(clk) begin if rising_edge(clk) then if en = '1' then q <= d; end if; end if; end process;")
        self.assertTrue(f.attrs["enable_recognized"])
        self.assertEqual(ops(m), ["DFF"])
        self.assertTrue(f.has_input("EN"))

    def test_elsif_reset_chain_keeps_sync_reset_and_enable(self):
        f, m = self.one("process(clk) begin if rising_edge(clk) then if rst = '1' then q <= (others => '0'); elsif en = '1' then q <= d; end if; end if; end process;")
        self.assertEqual(f.attrs["reset_kind"], "sync")
        self.assertTrue(f.attrs["enable_recognized"])
        self.assertEqual(ops(m), ["DFF"])

    def test_enable_in_the_edge_condition(self):
        f, m = self.one("process(clk) begin if rising_edge(clk) and en = '1' then q <= d; end if; end process;")
        self.assertTrue(f.attrs["enable_recognized"])

    def test_async_reset_with_enable(self):
        f, m = self.one("process(clk, rst) begin if rst = '1' then q <= (others => '0'); elsif rising_edge(clk) then "
                        "if en = '1' then q <= d; end if; end if; end process;")
        self.assertEqual((f.attrs["reset_kind"], f.attrs["enable_recognized"]), ("async", True))
        self.assertEqual(ops(m), ["DFF"])

    def test_reset_branch_and_data_through_mux(self):
        f, m = self.one("process(clk) begin if rising_edge(clk) then if en = '1' then q <= d; else q <= q; end if; end if; end process;")
        self.assertTrue(f.attrs["enable_recognized"])

    def test_two_registers_in_one_process_share_controls(self):
        ports = "clk, rst : in std_logic; d : in std_logic_vector(3 downto 0); q, r : out std_logic_vector(3 downto 0)"
        m, res, dd = module(unit(ports, "", "process(clk) begin if rising_edge(clk) then if rst = '1' then q <= x\"0\"; r <= x\"1\"; "
                                           "else q <= d; r <= not d; end if; end if; end process;"))
        f = dffs(m)
        self.assertEqual(len(f), 2)
        self.assertTrue(all(x.attrs["reset_kind"] == "sync" for x in f))

    def test_signal_assignment_is_deferred(self):
        ports = "clk, d : in std_logic; qa, qb : out std_logic"
        m, r, dd = module(unit(ports, "signal a, b : std_logic;",
                               "process(clk) begin if rising_edge(clk) then a <= d; b <= a; end if; end process; qa <= a; qb <= b;"))
        f = {x.outputs[0].name: x for x in dffs(m)}
        self.assertEqual(len(f), 2)
        self.assertEqual(f["b"].input("D").chunks[0].net.name, "a")      # b reads the old a: a shift register

    def test_variable_assignment_is_immediate(self):
        ports = "clk, d : in std_logic; qb : out std_logic"
        m, r, dd = module(unit(ports, "", "process(clk) variable v : std_logic; begin if rising_edge(clk) then v := d; qb <= not v; end if; end process;"))
        f = dffs(m)
        self.assertEqual(len(f), 1)
        self.assertEqual(ops(m), ["DFF", "NOT"])
        self.assertEqual(nodes(m, "NOT")[0].input("A").chunks[0].net.name, "d")

    def test_variable_read_before_write_is_a_register(self):
        ports = "clk : in std_logic; q : out unsigned(3 downto 0)"
        m, r, dd = module(unit(ports, "", "process(clk) variable c : unsigned(3 downto 0); begin if rising_edge(clk) then c := c + 1; q <= c; end if; end process;"))
        self.assertGreaterEqual(len(dffs(m)), 1)
        self.assertNotIn("COMBINATIONAL_LOOP", codes(r, m))

    def test_slice_assignments_merge_into_one_register(self):
        m, r, dd = module(unit("clk : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(7 downto 0)", "",
                               "process(clk) begin if rising_edge(clk) then q(7 downto 4) <= d; q(3 downto 0) <= not d; end if; end process;"))
        self.assertEqual(len(dffs(m)), 1)
        self.assertEqual(dffs(m)[0].outputs[0].width, 8)


class Combinational(unittest.TestCase):
    def test_latch_inference(self):
        m, r, d = module(unit("en, x : in std_logic; q : out std_logic", "", "process(en, x) begin if en = '1' then q <= x; end if; end process;"))
        la = nodes(m, "LATCH")
        self.assertEqual(len(la), 1)
        self.assertTrue(la[0].attrs["inferred"])
        self.assertIn("INFERRED_LATCH", codes(r, m))

    def test_no_latch_with_default_assignment(self):
        m, r, d = module(unit("en, x : in std_logic; q : out std_logic", "",
                              "process(all) begin q <= '0'; if en = '1' then q <= x; end if; end process;"))
        self.assertEqual(nodes(m, "LATCH"), [])
        self.assertNotIn("INFERRED_LATCH", codes(r, m))

    def test_no_latch_when_case_is_exhaustive(self):
        m, r, d = module(unit("s : in std_logic_vector(1 downto 0); x : in std_logic_vector(3 downto 0); q : out std_logic", "",
                              "process(all) begin case s is when \"00\" => q <= x(0); when \"01\" => q <= x(1); when \"10\" => q <= x(2); when others => q <= x(3); end case; end process;"))
        self.assertEqual(nodes(m, "LATCH"), [])

    def test_if_elsif_gives_priority(self):
        m, r, d = module(unit("a, b, c, e : in std_logic; y : out std_logic", "",
                              "process(all) begin if a = '1' then y <= b; elsif c = '1' then y <= e; else y <= '0'; end if; end process;"))
        p = nodes(m, "PRIORITY")
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0].attrs["k"], 2)

    def test_case_gives_pmux(self):
        m, r, d = module(unit("s : in std_logic_vector(1 downto 0); a, b, c : in std_logic; y : out std_logic", "",
                              "process(all) begin case s is when \"00\" => y <= a; when \"01\" => y <= b; when others => y <= c; end case; end process;"))
        self.assertEqual(len(nodes(m, "PMUX")), 1)
        self.assertEqual(nodes(m, "PRIORITY"), [])

    def test_if_on_equality_chain_is_exclusive(self):
        m, r, d = module(unit("s : in std_logic_vector(1 downto 0); a, b, c : in std_logic; y : out std_logic", "",
                              "process(all) begin if s = \"00\" then y <= a; elsif s = \"01\" then y <= b; else y <= c; end if; end process;"))
        self.assertEqual(len(nodes(m, "PMUX")), 1)

    def test_selected_and_conditional_assignments(self):
        m, r, d = module(unit("s : in std_logic_vector(1 downto 0); a, b, c : in std_logic; y, z : out std_logic", "",
                              "with s select y <= a when \"00\", b when \"01\", c when others;\n z <= a when s = \"11\" else b;"))
        self.assertEqual(len(nodes(m, "PMUX")), 1)
        self.assertEqual(len(nodes(m, "MUX")), 1)

    def test_matching_case_uses_wildcards(self):
        m, r, d = module(unit("a : in std_logic_vector(3 downto 0); y : out std_logic", "",
                              "process(all) begin case? a is when \"1---\" => y <= '1'; when others => y <= '0'; end case?; end process;"))
        self.assertEqual(ops(m), [])           # only a[3] is compared: y = a(3)

    def test_unsigned_priority_encoder_with_exit(self):
        m, r, d = module(unit("a : in std_logic_vector(7 downto 0); y : out integer range 0 to 8", "",
                              "process(a) variable c : integer range 0 to 8; begin c := 8; for i in 0 to 7 loop if a(i) = '1' then c := i; exit; end if; end loop; y <= c; end process;"))
        self.assertEqual(nodes(m, "LATCH"), [])
        self.assertTrue(len(nodes(m, "MUX")) >= 8)

    def test_for_loop_unrolls_a_parity_tree(self):
        m, r, d = module(unit("a : in std_logic_vector(7 downto 0); y : out std_logic", "",
                              "process(a) variable p : std_logic; begin p := '0'; for i in a'range loop p := p xor a(i); end loop; y <= p; end process;"))
        self.assertEqual(ops(m), ["XOR"] * 7)

    def test_next_statement_skips_the_rest_of_the_iteration(self):
        m, r, d = module(unit("a : in std_logic_vector(3 downto 0); y : out unsigned(2 downto 0)", "",
                              "process(a) variable c : unsigned(2 downto 0); begin c := (others => '0'); for i in 0 to 3 loop next when a(i) = '0'; c := c + 1; end loop; y <= c; end process;"))
        self.assertEqual(len(nodes(m, "ADD")), 3)     # the first increment of the constant 0 folds

    def test_functions_are_inlined(self):
        decl = "function mx(x, y : unsigned) return unsigned is begin if x > y then return x; else return y; end if; end function;"
        m, r, d = module(unit("a, b : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", decl, "y <= mx(a, b);"))
        self.assertEqual(ops(m), ["GT", "MUX"])

    def test_function_early_return_in_loop(self):
        decl = ("function first_one(x : std_logic_vector) return integer is begin for i in x'range loop if x(i) = '1' then return i; end if; end loop; return 99; end function;")
        m, r, d = module(unit("a : in std_logic_vector(3 downto 0); y : out integer range 0 to 127", decl, "y <= first_one(a);"))
        self.assertNotIn("UNSUPPORTED_CONSTRUCT", codes(r, m))
        self.assertTrue(len(nodes(m, "MUX")) >= 3)

    def test_procedure_with_out_parameters(self):
        decl = "procedure swap(x, w : in unsigned(3 downto 0); signal p, q : out unsigned(3 downto 0)) is begin p <= w; q <= x; end procedure;"
        m, r, d = module(unit("a, b : in unsigned(3 downto 0); y, z : out unsigned(3 downto 0)", decl,
                              "process(all) begin swap(a, b, y, z); end process;"))
        self.assertEqual(ops(m), [])
        self.assertEqual(codes(r, m), [])

    def test_procedure_with_variable_out_parameter(self):
        decl = "procedure inc(x : in unsigned(3 downto 0); r : out unsigned(3 downto 0)) is begin r := x + 1; end procedure;"
        m, r, d = module(unit("a : in unsigned(3 downto 0); y : out unsigned(3 downto 0)", decl,
                              "process(all) variable t : unsigned(3 downto 0); begin inc(a, t); y <= t; end process;"))
        self.assertEqual(ops(m), ["ADD"])
        self.assertEqual(nodes(m, "LATCH"), [])

    def test_dynamic_bit_write_in_comb_process(self):
        m, r, d = module(unit("i : in integer range 0 to 3; y : out std_logic_vector(3 downto 0)", "",
                              "process(all) begin y <= (others => '0'); y(i) <= '1'; end process;"))
        self.assertEqual(nodes(m, "LATCH"), [])

    def test_simulation_only_process_is_skipped(self):
        m, r, d = module(unit("a : in std_logic; y : out std_logic", "",
                              "y <= a;\n process begin wait for 10 ns; report \"x\"; end process;"))
        self.assertIn("SIMULATION_ONLY_PROCESS", codes(r, m))
        self.assertEqual(ops(m), [])

    def test_multiple_drivers_reported(self):
        m, r, d = module(unit("a, b : in std_logic; y : out std_logic", "", "y <= a;\n y <= b;"))
        self.assertIn("MULTIPLE_DRIVERS", codes(r, m))

    def test_tristate_conditional_assignment(self):
        m, r, d = module(unit("en : in std_logic; x : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)", "",
                              "y <= x when en = '1' else (others => 'Z');"))
        mx = nodes(m, "MUX")
        self.assertEqual(len(mx), 1)
        self.assertTrue(mx[0].attrs.get("tristate"))

    def test_tristate_in_process(self):
        m, r, d = module(unit("en, x : in std_logic; y : out std_logic", "",
                              "process(all) begin if en = '1' then y <= x; else y <= 'Z'; end if; end process;"))
        self.assertTrue(nodes(m, "MUX")[0].attrs.get("tristate"))


class Memories(unittest.TestCase):
    RAM = ("type ram_t is array (0 to 15) of std_logic_vector(7 downto 0); signal ram : ram_t;")

    def test_ram_with_write_port_and_read_port(self):
        m, r, d = module(unit("clk, we : in std_logic; a : in unsigned(3 downto 0); di : in std_logic_vector(7 downto 0); dout : out std_logic_vector(7 downto 0)",
                              self.RAM, "process(clk) begin if rising_edge(clk) then if we = '1' then ram(to_integer(a)) <= di; end if; dout <= ram(to_integer(a)); end if; end process;"))
        mem = nodes(m, "MEM")
        self.assertEqual(len(mem), 1)
        self.assertEqual((mem[0].attrs["depth"], mem[0].attrs["width"], mem[0].attrs["wports"], mem[0].attrs["rports"]), (16, 8, 1, 1))
        self.assertTrue(mem[0].has_input("w0_en"))

    def test_rom_from_constant_array(self):
        rom = ("type rom_t is array (0 to 3) of std_logic_vector(7 downto 0); constant ROM : rom_t := (x\"01\", x\"02\", x\"04\", x\"08\");")
        m, r, d = module(unit("a : in unsigned(1 downto 0); y : out std_logic_vector(7 downto 0)", rom, "y <= ROM(to_integer(a));"))
        rm = nodes(m, "ROM")
        self.assertEqual(len(rm), 1)
        self.assertEqual(rm[0].attrs["contents"], (1, 2, 4, 8))

    def test_bit_vector_with_dynamic_index_is_not_a_memory(self):
        m, r, d = module(unit("clk, d : in std_logic; i : in integer range 0 to 7; q : out std_logic_vector(7 downto 0)", "signal sr : std_logic_vector(7 downto 0);",
                              "process(clk) begin if rising_edge(clk) then sr(i) <= d; end if; end process; q <= sr;"))
        self.assertEqual(nodes(m, "MEM"), [])
        self.assertEqual(sum(x.outputs[0].width for x in dffs(m)), 8)

    def test_variable_ram_in_process(self):
        m, r, d = module(unit("clk, we : in std_logic; a : in unsigned(2 downto 0); di : in std_logic_vector(3 downto 0); dout : out std_logic_vector(3 downto 0)", "",
                              "process(clk) type ram_t is array (0 to 7) of std_logic_vector(3 downto 0); variable ram : ram_t; begin if rising_edge(clk) then if we = '1' then ram(to_integer(a)) := di; end if; dout <= ram(to_integer(a)); end if; end process;"))
        self.assertEqual(len(nodes(m, "MEM")), 1)

    def test_shift_register_signal_is_flops(self):
        m, r, d = module(unit("clk, d : in std_logic; q : out std_logic_vector(3 downto 0)", "signal sr : std_logic_vector(3 downto 0);",
                              "process(clk) begin if rising_edge(clk) then sr <= sr(2 downto 0) & d; end if; end process; q <= sr;"))
        self.assertEqual(ops(m), ["DFF"])


if __name__ == "__main__":
    unittest.main()
