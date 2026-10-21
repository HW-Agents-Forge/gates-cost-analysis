"""One fixture per construct of FRONTEND.md 10.5 (syntactic side)."""
import unittest

from gatecost.frontend.vhdl import nodes as N
from tests._vhdl_helpers import arch, codes, errors, expr, parse, proc, show, stmts


def one(src):
    r = parse(src)
    assert not errors(r), [(d.line, d.message) for d in errors(r)]
    return r


class DesignUnitTests(unittest.TestCase):
    def test_library_and_use_clauses(self):
        r = one("library ieee, work;\nuse ieee.std_logic_1164.all, ieee.numeric_std.all;\nuse work.pkg.c;\n"
                "entity e is end;")
        e = r.entities[("work", "e")]
        self.assertIsInstance(e.context[0], N.LibraryClause)
        self.assertEqual(e.context[0].names, ["ieee", "work"])
        self.assertEqual(e.context[1].paths, [("ieee", "std_logic_1164", "all"), ("ieee", "numeric_std", "all")])
        self.assertEqual(e.context[2].paths, [("work", "pkg", "c")])

    def test_entity_with_generics_and_ports(self):
        r = one("""entity E is
          generic (W : positive := 8; type T; constant N, M : natural := 4);
          port (clk, rst : in std_logic; d : in std_logic_vector(W-1 downto 0) := (others => '0');
                q : out unsigned(W-1 downto 0); io : inout bit; b : buffer integer range 0 to 9);
        begin
          assert true;
        end entity E;""")
        e = r.entities[("work", "e")]
        self.assertEqual([(g.name, g.kind) for g in e.generics], [("w", "constant"), ("t", "type"), ("n", "constant"),
                                                                  ("m", "constant")])
        self.assertEqual(show(e.generics[0].default), "8")
        self.assertEqual([(p.name, p.mode) for p in e.ports],
                         [("clk", "in"), ("rst", "in"), ("d", "in"), ("q", "out"), ("io", "inout"), ("b", "buffer")])
        self.assertEqual(show(e.ports[2].subtype.index_constraints[0]), "(w - 1) downto 0")
        self.assertEqual(show(e.ports[2].default), "agg(others=>'0')")
        self.assertEqual(show(e.ports[5].subtype.range), "0 to 9")
        self.assertEqual(len(e.stmts), 1)
        self.assertEqual(e.line, 1)

    def test_entity_without_ports_and_empty_clauses(self):
        r = one("entity e is end; entity f is port (); end f;")
        self.assertEqual(r.entities[("work", "f")].ports, [])

    def test_architecture_and_entity_name(self):
        r = one("architecture RTL of work.E is begin end architecture;")
        a = r.architectures[("work", "e")][0]
        self.assertEqual((a.name, a.entity), ("rtl", "e"))

    def test_package_and_package_body(self):
        r = one("""package p is
          constant c : integer := 3;
          type t is (a, b);
          function f (x : integer) return integer;
          procedure pr (signal s : out bit; variable v : inout integer);
          component k port (x : in bit); end component;
        end package p;
        package body p is
          function f (x : integer) return integer is begin return x + c; end function f;
        end package body p;""")
        p = r.packages[("work", "p")]
        self.assertEqual([type(d).__name__ for d in p.decls],
                         ["ObjectDecl", "TypeDecl", "Subprogram", "Subprogram", "ComponentDecl"])
        self.assertFalse(p.decls[2].has_body)
        self.assertEqual(p.decls[3].params[0].cls, "signal")
        self.assertEqual(p.decls[3].params[1].mode, "inout")
        b = r.package_bodies[("work", "p")]
        self.assertTrue(b.decls[0].has_body)
        self.assertEqual(show(b.decls[0].body[0].value), "(x + c)")

    def test_package_with_generics_2008(self):
        r = one("package g is generic (W : integer := 4); constant c : integer := W; end package;")
        self.assertEqual(r.packages[("work", "g")].generics[0].name, "w")

    def test_configuration_default_binding(self):
        r = one("""configuration cfg of e is
          for rtl
            for all : comp use entity work.other(beh); end for;
          end for;
        end configuration cfg;""")
        c = r.configurations[("work", "cfg")]
        self.assertEqual((c.entity, c.architecture), ("e", "rtl"))
        self.assertIn("IGNORED_REGION", codes(r))

    def test_context_declaration_and_reference(self):
        r = one("""context c is library ieee; use ieee.std_logic_1164.all; end context c;
        context work.c;
        entity e is end;""")
        self.assertEqual(len(r.contexts[("work", "c")].items), 2)
        self.assertIsInstance(r.entities[("work", "e")].context[0], N.ContextRef)

    def test_unit_locations(self):
        r = one("\n\nentity e is end;\narchitecture a of e is begin end;")
        self.assertEqual((r.entities[("work", "e")].line, r.entities[("work", "e")].col), (3, 1))
        self.assertEqual(r.architectures[("work", "e")][0].line, 4)

    def test_multiple_units_per_file_keep_own_context(self):
        r = one("library ieee; use ieee.numeric_std.all; entity a is end; entity b is end;")
        self.assertEqual(len(r.entities[("work", "a")].context), 2)
        self.assertEqual(r.entities[("work", "b")].context, [])


class DeclarationTests(unittest.TestCase):
    def decls(self, text):
        r, a = arch("", "", text)
        self.assertEqual(errors(r), [], [(d.line, d.message) for d in errors(r)])
        return a.decls

    def test_enumeration_type(self):
        d = self.decls("type st is (idle, run, \\Done\\, 'a');")[0]
        self.assertEqual(d.definition.literals, ["idle", "run", "\\Done\\", "'a'"])

    def test_integer_range_type_and_subtype(self):
        d = self.decls("type byte is range 0 to 255; subtype nib is byte range 0 to 15;")
        self.assertEqual(show(d[0].definition.range), "0 to 255")
        self.assertEqual(show(d[1].subtype.range), "0 to 15")
        self.assertEqual(d[1].subtype.mark.name, "byte")

    def test_array_types_constrained_unconstrained_multidim(self):
        d = self.decls("""type m is array (0 to 15) of std_logic_vector(7 downto 0);
                          type u is array (natural range <>) of bit;
                          type g is array (0 to 3, integer range 0 to 7) of bit;
                          type e is array (st_t) of bit;""")
        self.assertFalse(d[0].definition.unconstrained)
        self.assertEqual(show(d[0].definition.indexes[0]), "0 to 15")
        self.assertEqual(show(d[0].definition.element.index_constraints[0]), "7 downto 0")
        self.assertTrue(d[1].definition.unconstrained)
        self.assertEqual(len(d[2].definition.indexes), 2)
        self.assertEqual(d[3].definition.indexes[0].name, "st_t")

    def test_record_type(self):
        d = self.decls("type r is record a, b : std_logic; c : unsigned(3 downto 0); end record r;")[0]
        self.assertEqual([f.name for f in d.definition.fields], ["a", "b", "c"])

    def test_objects(self):
        d = self.decls("""constant c : integer := 2 ** 3;
                          signal s, t : std_logic := '0';
                          signal b : bit_vector(3 downto 0) register;
                          shared variable sv : integer;""")
        self.assertEqual([x.name for x in d], ["c", "s", "t", "b", "sv"])
        self.assertEqual(d[0].cls, "constant")
        self.assertTrue(d[4].shared)
        self.assertEqual(show(d[1].init), "'0'")

    def test_deferred_constant(self):
        d = self.decls("constant c : integer;")[0]
        self.assertIsNone(d.init)

    def test_alias_attribute_and_component(self):
        d = self.decls("""alias hi : std_logic_vector(3 downto 0) is data(7 downto 4);
                          attribute keep : string;
                          attribute keep of s : signal is "true";
                          attribute enum_encoding of st_t : type is "one-hot";
                          component c is generic (n : integer := 1); port (a : in bit; b : out bit); end component c;""")
        self.assertIsInstance(d[0], N.AliasDecl)
        self.assertEqual(show(d[0].target), "data{7 downto 4}")
        self.assertIsInstance(d[1], N.AttributeDecl)
        self.assertEqual((d[2].attr, d[2].targets, d[2].entity_class), ("keep", ["s"], "signal"))
        self.assertEqual(d[3].value.value, "one-hot")
        self.assertEqual([p.name for p in d[4].ports], ["a", "b"])
        self.assertEqual(d[4].generics[0].name, "n")

    def test_resolution_function_in_subtype(self):
        d = self.decls("signal s : resolved std_ulogic;")[0]
        self.assertEqual((d.subtype.resolution.name, d.subtype.mark.name), ("resolved", "std_ulogic"))

    def test_functions_and_procedures(self):
        d = self.decls("""pure function log2 (n : positive) return natural is
                            variable r : natural := 0; variable v : natural := n - 1;
                          begin
                            while v > 0 loop v := v / 2; r := r + 1; end loop;
                            return r;
                          end function log2;
                          impure function g return integer;
                          function "+" (a, b : t) return t is begin return a; end;
                          procedure p (signal clk : in bit; x : out integer) is begin x := 1; end procedure;""")
        self.assertEqual([(s.kind, s.name, s.pure, s.has_body) for s in d],
                         [("function", "log2", True, True), ("function", "g", False, False),
                          ("function", '"+"', True, True), ("procedure", "p", True, True)])
        self.assertEqual(len(d[0].decls), 2)
        self.assertIsInstance(d[0].body[0], N.WhileLoop)
        self.assertEqual([p.name for p in d[2].params], ["a", "b"])
        self.assertEqual(d[3].params[0].cls, "signal")

    def test_package_instantiation(self):
        d = self.decls("package ip is new work.gp generic map (W => 8);")[0]
        self.assertIsInstance(d, N.PackageInst)
        self.assertEqual(d.target, ("work", "gp"))

    def test_use_clause_in_declarative_part(self):
        d = self.decls("use work.pkg.all;")[0]
        self.assertIsInstance(d, N.UseClause)


class ConcurrentTests(unittest.TestCase):
    def test_simple_assignment_and_label(self):
        s = stmts("l1: x <= a and b;")[0]
        self.assertEqual((s.label, show(s.target), len(s.alts)), ("l1", "x", 1))
        self.assertIsNone(s.alts[0].cond)
        self.assertEqual(s.kind, "signal")

    def test_target_forms(self):
        s = stmts("x(3 downto 0) <= y; (a, b) <= z; r.f(2) <= w;")
        self.assertEqual(show(s[0].target), "x{3 downto 0}")
        self.assertIsInstance(s[1].target, N.Aggregate)
        self.assertEqual(show(s[2].target), "r.f[2]")

    def test_conditional_assignment(self):
        s = stmts("x <= a when c1 else b when c2 else (others => '0');")[0]
        self.assertEqual(len(s.alts), 3)
        self.assertEqual(show(s.alts[0].cond), "c1")
        self.assertIsNone(s.alts[2].cond)

    def test_selected_assignment_and_matching(self):
        s = stmts("""with sel select x <= a when "00" | "01", b when "1-", c when others;
                     with sel select? y <= a when "1-", b when others;""")
        self.assertEqual(len(s[0].alts), 3)
        self.assertEqual(len(s[0].alts[0].choices), 2)
        self.assertIsInstance(s[0].alts[2].choices[0], N.Others)
        self.assertFalse(s[0].matching)
        self.assertTrue(s[1].matching)

    def test_guarded_transport_and_after_are_tolerated(self):
        s = stmts("x <= guarded transport a after 5 ns; y <= a after 1 ns, b after 2 ns;")
        self.assertTrue(s[0].guarded)
        self.assertEqual(show(s[0].alts[0].values[0]), "a")
        self.assertEqual(len(s[1].alts[0].values), 2)

    def test_unaffected(self):
        s = stmts("x <= unaffected when c else a;")[0]
        self.assertIsInstance(s.alts[0].values[0], N.Unaffected)

    def test_process_forms(self):
        s = stmts("""process (clk, rst) begin null; end process;
                     p2: process (all) variable v : integer; begin v := 1; end process p2;
                     process begin wait until rising_edge(clk); end process;
                     postponed process is begin wait; end postponed process;""")
        self.assertEqual([show(x) for x in s[0].sensitivity], ["clk", "rst"])
        self.assertEqual((s[1].label, s[1].sensitivity, len(s[1].decls)), ("p2", "all", 1))
        self.assertIsNone(s[2].sensitivity)
        self.assertTrue(s[3].postponed)

    def test_component_instantiation_with_maps(self):
        i = stmts("u1: comp generic map (W => 8, 4) port map (a => x, b => open, c(3) => y(0), z);")[0]
        self.assertIsInstance(i, N.Instance)
        self.assertEqual((i.kind, i.unit, i.arch), ("component", ("comp",), None))
        self.assertEqual(show(i.generic_map[0].formal), "w")
        self.assertIsNone(i.generic_map[1].formal)
        self.assertIsInstance(i.port_map[1].actual, N.Open)
        self.assertEqual(show(i.port_map[2].formal), "c[3]")
        self.assertIsNone(i.port_map[3].formal)

    def test_entity_instantiation(self):
        i = stmts("u1: entity work.foo(rtl) port map (a => b); u2: entity lib.bar port map (x => y); "
                  "u3: component comp port map (p => q); u4: configuration work.cfg;")
        self.assertEqual((i[0].kind, i[0].unit, i[0].arch), ("entity", ("work", "foo"), "rtl"))
        self.assertEqual((i[1].unit, i[1].arch), (("lib", "bar"), None))
        self.assertEqual(i[2].kind, "component")
        self.assertEqual((i[3].kind, i[3].unit), ("configuration", ("work", "cfg")))

    def test_instance_with_selected_component_name_and_no_maps(self):
        i = stmts("u1: work.comp port map (a => b);")[0]
        self.assertEqual(i.unit, ("work", "comp"))

    def test_concurrent_procedure_call(self):
        s = stmts("proc(a, b); l: work.p.q(c);")
        self.assertIsInstance(s[0], N.ProcCall)
        self.assertEqual(show(s[0].call), "proc[a, b]")

    def test_for_generate(self):
        g = stmts("g: for i in 0 to N - 1 generate\n signal t : bit; begin\n x(i) <= y(i);\n u: comp port map (a => t);\n end generate g;")[0]
        self.assertIsInstance(g, N.ForGenerate)
        self.assertEqual((g.label, g.var, show(g.range)), ("g", "i", "0 to (n - 1)"))
        self.assertEqual(len(g.decls), 1)
        self.assertEqual(len(g.stmts), 2)

    def test_for_generate_over_range_attribute(self):
        g = stmts("g: for i in x'range generate x(i) <= '0'; end generate;")[0]
        self.assertEqual(show(g.range), "x'range")

    def test_if_generate_with_elsif_else_2008(self):
        g = stmts("""g: if A = 1 generate x <= '1';
                     elsif B = 2 generate x <= '0';
                     else generate x <= 'Z';
                     end generate g;""")[0]
        self.assertIsInstance(g, N.IfGenerate)
        self.assertEqual(len(g.branches), 2)
        self.assertEqual(show(g.branches[1].cond), "(b = 2)")
        self.assertEqual(len(g.else_branch.stmts), 1)

    def test_if_generate_with_branch_labels_and_ends(self):
        g = stmts("""g: if a1: A = 1 generate x <= '1'; end a1;
                     elsif a2: B = 2 generate x <= '0'; end a2;
                     else a3: generate x <= 'Z'; end a3;
                     end generate g;""")[0]
        self.assertEqual([b.label for b in g.branches], ["a1", "a2"])
        self.assertEqual(g.else_branch.label, "a3")

    def test_case_generate(self):
        g = stmts("""g: case MODE generate
                      when 0 | 1 => x <= '1';
                      when A2: 2 => x <= '0';
                      when others => x <= 'Z';
                     end generate;""")[0]
        self.assertIsInstance(g, N.CaseGenerate)
        self.assertEqual(len(g.alts), 3)
        self.assertEqual(g.alts[1].label, "a2")
        self.assertIsInstance(g.alts[2].choices[0], N.Others)

    def test_nested_generate(self):
        g = stmts("""a: for i in 0 to 1 generate b: for j in 0 to 1 generate c: if i = j generate x(i,j) <= '1';
                     end generate c; end generate b; end generate a;""")[0]
        self.assertIsInstance(g.stmts[0].stmts[0], N.IfGenerate)

    def test_block(self):
        b = stmts("""b1: block (en = '1') is
                       generic (g : integer := 1); generic map (g => 2);
                       port (p : in bit); port map (p => clk);
                       signal t : bit;
                     begin
                       t <= p;
                     end block b1;""")[0]
        self.assertIsInstance(b, N.BlockStmt)
        self.assertEqual(show(b.guard), "(en = '1')")
        self.assertEqual((len(b.generics), len(b.ports), len(b.generic_map), len(b.port_map)), (1, 1, 1, 1))
        self.assertEqual((len(b.decls), len(b.stmts)), (1, 1))


class SequentialTests(unittest.TestCase):
    def test_if_elsif_else(self):
        s = proc("if a then x <= 1; elsif b then x <= 2; elsif c then null; else x <= 3; end if;")[0]
        self.assertEqual(len(s.branches), 3)
        self.assertEqual(len(s.else_body), 1)

    def test_if_with_label(self):
        s = proc("l: if a then null; end if l;")[0]
        self.assertEqual(s.label, "l")

    def test_case_and_matching_case(self):
        s = proc("""case s is when "00" => x <= 1; when "01" | "10" => x <= 2; when others => null; end case;
                    case? v is when "1--" => y <= 1; when others => y <= 0; end case?;""")
        self.assertFalse(s[0].matching)
        self.assertTrue(s[1].matching)
        self.assertEqual(len(s[0].alts), 3)
        self.assertEqual(len(s[0].alts[1].choices), 2)

    def test_loops(self):
        s = proc("""for i in 0 to 7 loop x := x + i; end loop;
                    for i in a'range loop null; end loop;
                    l: while c loop exit l when d; end loop l;
                    loop next when e; exit; end loop;""")
        self.assertIsInstance(s[0], N.ForLoop)
        self.assertEqual((s[0].var, show(s[0].range)), ("i", "0 to 7"))
        self.assertIsInstance(s[2], N.WhileLoop)
        self.assertEqual(s[2].label, "l")
        self.assertIsInstance(s[2].body[0], N.ExitStmt)
        self.assertEqual((s[2].body[0].loop_label, show(s[2].body[0].cond)), ("l", "d"))
        self.assertIsInstance(s[3], N.LoopStmt)
        self.assertIsInstance(s[3].body[0], N.NextStmt)
        self.assertIsInstance(s[3].body[1], N.ExitStmt)

    def test_return_null_and_calls(self):
        s = proc("return; return x + 1; null; pr(a, b); pkg.pr(c); work.pkg.pr;")
        self.assertIsNone(s[0].value)
        self.assertEqual(show(s[1].value), "(x + 1)")
        self.assertIsInstance(s[2], N.NullStmt)
        self.assertEqual(show(s[3].call), "pr[a, b]")
        self.assertEqual(show(s[4].call), "pkg.pr[c]")

    def test_assignments(self):
        s = proc("""x <= y; v := w; a(1) := b; r.f <= (others => '0');
                    z <= '1' when c else '0'; v := 1 when c else 2;
                    with sel select q <= a when '0', b when others;""")
        self.assertEqual([x.kind for x in s[:6]], ["signal", "variable", "variable", "signal", "signal", "variable"])
        self.assertEqual(len(s[4].alts), 2)
        self.assertIsInstance(s[6], N.SelectAssign)

    def test_wait_forms(self):
        s = proc("wait until rising_edge(clk); wait on a, b; wait on a until c; wait;")
        self.assertEqual(show(s[0].until), "rising_edge[clk]")
        self.assertEqual([show(x) for x in s[1].on], ["a", "b"])
        self.assertEqual((len(s[2].on), show(s[2].until)), (1, "c"))
        self.assertEqual((s[3].on, s[3].until, s[3].timeout), (None, None, None))

    def test_clock_patterns_parse(self):
        s = proc("""if rst = '1' then q <= (others => '0'); elsif clk'event and clk = '1' then q <= d; end if;
                    if falling_edge(clk) then q <= d; end if;""")
        self.assertEqual(show(s[0].branches[1].cond), "(clk'event and (clk = '1'))")

    def test_variable_declarations_in_process(self):
        r, a = arch("", "process variable v : integer range 0 to 3 := 1; constant k : integer := 2; begin null; end process;")
        self.assertEqual([d.name for d in a.stmts[0].decls], ["v", "k"])


class SkippedRegionTests(unittest.TestCase):
    def infos(self, r):
        return [d for d in r.diagnostics if d.code == "IGNORED_REGION"]

    def test_assert_and_report_skipped_at_info(self):
        r, a = arch("", "assert a = b report \"x\" severity error;\nprocess begin assert x; report \"hi\"; y <= 1; end process;")
        self.assertEqual(errors(r), [])
        self.assertEqual(len(self.infos(r)), 3)
        self.assertTrue(all(d.severity == "info" and not d.affects_cost for d in self.infos(r)))
        self.assertIsInstance(a.stmts[0], N.Skipped)
        self.assertEqual([type(x).__name__ for x in a.stmts[1].body], ["Skipped", "Skipped", "SignalAssign"])

    def test_wait_for_and_after_keep_the_statement(self):
        r, a = arch("", "process begin x <= '1' after 2 ns; wait for 10 ns; end process;")
        self.assertEqual(len(self.infos(r)), 2)
        self.assertIsInstance(a.stmts[0].body[0], N.SignalAssign)
        self.assertIsInstance(a.stmts[0].body[1], N.WaitStmt)

    def test_simulation_only_process_detected(self):
        r, a = arch("", "stim: process begin clk <= '0'; wait for 5 ns; clk <= '1'; wait for 5 ns; end process;"
                        "process begin report \"x\"; wait; end process;"
                        "process begin wait until rising_edge(clk); q <= d; end process;"
                        "process (clk) begin if rising_edge(clk) then q <= d; end if; end process;")
        self.assertEqual([p.sim_only for p in a.stmts], [True, True, False, False])
        self.assertEqual(codes(r).count("SIMULATION_ONLY_PROCESS"), 2)
        sim = [d for d in r.diagnostics if d.code == "SIMULATION_ONLY_PROCESS"]
        self.assertTrue(all(d.severity == "info" and not d.affects_cost for d in sim))

    def test_file_declarations_textio_and_access_types(self):
        r, a = arch("library std; use std.textio.all;\n", "process\n"
                    "file f : text open read_mode is \"in.txt\";\n"
                    "variable l : line;\n"
                    "type ptr is access integer;\n"
                    "variable p : ptr;\n"
                    "begin readline(f, l); p := new integer; deallocate(p); wait; end process;",
                    "type t is protected procedure inc; end protected;\n"
                    "type body_t is record a : integer; end record;")
        self.assertEqual(errors(r), [])
        msgs = " ".join(d.message for d in self.infos(r))
        for w in ("std.textio", "file declaration", "access type", "allocator", "file I/O"):
            self.assertIn(w, msgs)
        self.assertIn("protected type", msgs)
        self.assertTrue(a.stmts[0].sim_only)

    def test_nonsynthesis_attribute_specs_reported_at_info(self):
        r, a = arch("", "", "attribute foo : string;\nattribute foo of s : signal is \"x\";\n"
                            "attribute keep : string;\nattribute keep of s : signal is \"true\";\nsignal s : bit;")
        self.assertEqual(errors(r), [])
        msgs = [d.message for d in self.infos(r)]
        self.assertEqual(len(msgs), 1)
        self.assertIn("'foo'", msgs[0])
        self.assertEqual(len([d for d in a.decls if isinstance(d, N.AttributeSpec)]), 2)

    def test_shared_variable_of_protected_type(self):
        r, a = arch("", "", "type counter_t is protected procedure inc; end protected;\n"
                            "type counter_t is protected body variable n : integer; procedure inc is begin n := n + 1; end; end protected body;\n"
                            "shared variable c : counter_t;")
        self.assertEqual(errors(r), [])
        self.assertTrue(any("shared variable of protected type" in d.message for d in self.infos(r)))

    def test_physical_type_skipped(self):
        r, a = arch("", "", "type d is range 0 to 1000 units um; mm = 1000 um; end units d;\nsignal s : bit;")
        self.assertEqual(errors(r), [])
        self.assertIsInstance(a.decls[0].definition, N.Skipped)
        self.assertEqual(a.decls[1].name, "s")

    def test_psl_skipped(self):
        r, a = arch("", "default clock is rising_edge(clk);\nassert always a -> next b;\nx <= y;",
                    "property p is always a;\nsequence s is {a; b};")
        self.assertEqual(errors(r), [])
        self.assertEqual(len(a.stmts), 3)
        self.assertEqual(len(self.infos(r)), 4)

    def test_external_names_are_unsupported(self):
        r, a = arch("", "x <= << signal .tb.dut.s : bit >>;")
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r))
        self.assertEqual(len(a.stmts), 1)

    def test_pragma_region_reported_and_excluded(self):
        r = one("entity e is end;\narchitecture a of e is begin\n-- synopsys translate_off\nx <= ;\n"
                "-- synopsys translate_on\ny <= '1';\nend;")
        self.assertEqual(len(r.architectures[("work", "e")][0].stmts), 1)
        self.assertEqual(len(self.infos(r)), 1)
        self.assertEqual(self.infos(r)[0].line, 3)


if __name__ == "__main__":
    unittest.main()
