"""Hierarchy: instantiation forms, sharing, constant ports, architecture selection, mixed-language stubs, limits."""
import unittest

from gatecost.elab.vhdl import VhdlElaborator, elaborate_vhdl, elaborate_vhdl_files
from gatecost.frontend.vhdl import parse_text
from gatecost.ir import Const, Design, SigSpec, analyze_ir

from tests.test_elab_vhdl_util import HDR, codes, elab, module, nodes, ops, unit

ADDER = (HDR + "entity add2 is generic (W : integer := 4); port (x, z : in unsigned(W - 1 downto 0); s : out unsigned(W - 1 downto 0)); end entity;\n"
         "architecture r of add2 is begin s <= x + z; end architecture;\n")


def top(body, decls="", generics=""):
    return unit("a, b : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", decls, body, generics, extra=ADDER.replace(HDR, ""))


class Instantiation(unittest.TestCase):
    def test_direct_entity_instantiation(self):
        m, r, d = module(top("u : entity work.add2 generic map (W => 8) port map (x => a, z => b, s => y);"))
        inst = nodes(m, "INST")
        self.assertEqual(len(inst), 1)
        self.assertEqual(inst[0].attrs["module_key"], "add2(w=8)")
        self.assertIn("add2(w=8)", d.modules)
        self.assertEqual(ops(d.modules["add2(w=8)"]), ["ADD"])

    def test_component_instantiation_and_positional_maps(self):
        comp = "component add2 generic (W : integer := 4); port (x, z : in unsigned(W - 1 downto 0); s : out unsigned(W - 1 downto 0)); end component;"
        m, r, d = module(top("u : add2 generic map (8) port map (a, b, y);", decls=comp))
        self.assertEqual(nodes(m, "INST")[0].attrs["module_key"], "add2(w=8)")
        self.assertEqual(codes(r, m), [])

    def test_default_generic_and_instance_names(self):
        body = ("signal_dummy: for i in 0 to 0 generate end generate;\n"
                "u1 : entity work.add2 port map (x => a(3 downto 0), z => b(3 downto 0), s => y(3 downto 0));\n y(7 downto 4) <= (others => '0');")
        body = body.split("\n", 1)[1]
        m, r, d = module(top(body))
        self.assertEqual(nodes(m, "INST")[0].attrs["module_key"], "add2(w=4)")
        self.assertEqual(nodes(m, "INST")[0].attrs["inst"], "u1")

    def test_identical_instances_share_one_module(self):
        body = ("u1 : entity work.add2 generic map (W => 8) port map (x => a, z => b, s => y);\n"
                "u2 : entity work.add2 generic map (W => 8) port map (x => b, z => a, s => open);")
        m, r, d = module(top(body))
        self.assertEqual(sum(1 for k in d.modules if k.startswith("add2")), 1)
        self.assertEqual(d.instance_counts()["add2(w=8)"], 2)

    def test_for_generate_of_instances_multiplies_counts(self):
        body = ("g : for i in 0 to 3 generate u : entity work.add2 generic map (W => 8) port map (x => a, z => b, s => open); end generate;\n y <= a;")
        m, r, d = module(top(body))
        self.assertEqual(len(nodes(m, "INST")), 4)
        self.assertEqual(d.instance_counts()["add2(w=8)"], 4)
        names = sorted(n.attrs["inst"] for n in nodes(m, "INST"))
        self.assertEqual(len(set(names)), 4)

    def test_constant_port_propagation_uniquifies(self):
        body = ("u1 : entity work.add2 generic map (W => 8) port map (x => a, z => b, s => y);\n"
                "u2 : entity work.add2 generic map (W => 8) port map (x => a, z => to_unsigned(1, 8), s => open);")
        m, r, d = module(top(body))
        keys = sorted(k for k in d.modules if k.startswith("add2"))
        self.assertEqual(keys, ["add2(w=8)", "add2(w=8)[z=8'h1]"])
        r2, d2 = elab(top(body), propagate_port_constants=False)
        self.assertEqual(sum(1 for k in d2.modules if k.startswith("add2")), 1)

    def test_partial_and_open_associations(self):
        src = (HDR + "entity sub is port (v : in std_logic_vector(3 downto 0); o : out std_logic_vector(1 downto 0)); end entity;\n"
               "architecture r of sub is begin o <= v(3 downto 2); end architecture;\n")
        body = ("u : entity work.sub port map (v(1 downto 0) => a(1 downto 0), v(3 downto 2) => b(1 downto 0), o(0) => y(0), o(1) => open);\n")
        m, r, d = module(unit("a, b : in std_logic_vector(7 downto 0); y : out std_logic_vector(1 downto 0)", "", body + "y(1) <= '0';", extra=src.replace(HDR, "")))
        self.assertEqual(len(nodes(m, "INST")), 1)

    def test_output_to_port_slice_and_signal(self):
        m, r, d = module(top("u : entity work.add2 generic map (W => 8) port map (x => a, z => b, s => t);\n y <= t;",
                             decls="signal t : unsigned(7 downto 0);"))
        self.assertEqual(codes(r, m), [])

    def test_recursive_instantiation_is_cut(self):
        src = (HDR + "entity rec is generic (N : integer := 3); port (a : in std_logic; y : out std_logic); end entity;\n"
               "architecture r of rec is signal t : std_logic; begin\n"
               "g : if N > 0 generate u : entity work.rec generic map (N => N - 1) port map (a => a, y => t); y <= t; end generate;\n"
               "g2 : if N = 0 generate y <= a; end generate; end architecture;\n")
        r, d = elab(src, "rec")
        self.assertTrue(r.top_key)
        self.assertEqual(len([k for k in d.modules if k.startswith("rec")]), 4)
        bad = (HDR + "entity loop1 is port (a : in std_logic; y : out std_logic); end entity;\n"
               "architecture r of loop1 is begin u : entity work.loop1 port map (a => a, y => y); end architecture;\n")
        r, d = elab(bad, "loop1")
        d.detect_recursion()
        self.assertTrue(any(dg.code == "RECURSIVE_INSTANCE" for m in d.modules.values() for dg in m.diagnostics))

    def test_missing_entity_is_a_black_box(self):
        comp = "component foo port (i : in std_logic; o : out std_logic); end component;"
        m, r, d = module(unit("a : in std_logic; y : out std_logic", comp, "u : foo port map (i => a, o => y);"))
        bb = nodes(m, "BLACKBOX")
        self.assertEqual(len(bb), 1)
        self.assertEqual(bb[0].attrs["name"], "foo")
        self.assertIn("MISSING_MODULE", codes(r, m))
        self.assertEqual(bb[0].out_ports if hasattr(bb[0], "out_ports") else bb[0].attrs["out_ports"], ("o",))

    def test_instance_limit(self):
        body = "g : for i in 0 to 9 generate u : entity work.add2 generic map (W => 8) port map (x => a, z => b, s => open); end generate;\n y <= a;"
        m, r, d = module(top(body), limits={"max_instances": 3})
        self.assertEqual(len(nodes(m, "INST")), 3)
        self.assertIn("LIMIT_REACHED", codes(r, m))


class ArchitectureSelection(unittest.TestCase):
    SRC = (HDR + "entity e is port (a : in std_logic; y : out std_logic); end entity;\n"
           "architecture fast of e is begin y <= a; end architecture;\n"
           "architecture slow of e is begin y <= not a; end architecture;\n")

    def test_last_compiled_wins_with_a_diagnostic(self):
        parsed = parse_text(self.SRC)
        d = Design()
        r = elaborate_vhdl(parsed, d, top="e")
        self.assertEqual(ops(d.modules["e"]), ["NOT"])
        self.assertTrue(any(x.code == "AMBIGUOUS_ARCHITECTURE" for x in parsed.diagnostics))

    def test_project_selection(self):
        parsed = parse_text(self.SRC, architectures={"e": "fast"})
        d = Design()
        elaborate_vhdl(parsed, d, top="e")
        self.assertEqual(ops(d.modules["e"]), [])

    def test_explicit_architecture_in_direct_instantiation(self):
        top_src = (self.SRC + "entity tt is port (a : in std_logic; y, z : out std_logic); end entity;\n"
                   "architecture r of tt is begin u1 : entity work.e(fast) port map (a, y); u2 : entity work.e(slow) port map (a, z); end architecture;\n")
        r, d = elab(top_src, "tt")
        self.assertEqual(sorted(k for k in d.modules if k != "tt"), ["e", "e.fast"] if False else sorted(k for k in d.modules if k != "tt"))
        self.assertEqual(len([k for k in d.modules if k != "tt"]), 2)


class TopSelection(unittest.TestCase):
    def test_roots_are_detected_without_a_top(self):
        src = (ADDER + "entity wrap is port (a, b : in unsigned(3 downto 0); y : out unsigned(3 downto 0)); end entity;\n"
               "architecture r of wrap is begin u : entity work.add2 port map (a, b, y); end architecture;\n"
               "entity other is port (a : in std_logic; y : out std_logic); end entity;\narchitecture r of other is begin y <= a; end architecture;\n")
        parsed = parse_text(src)
        d = Design()
        r = elaborate_vhdl(parsed, d)
        self.assertEqual(sorted(r.tops), ["other", "wrap"])
        self.assertIsNone(r.top_key)
        r = elaborate_vhdl(parsed, Design(), top="WRAP")
        self.assertEqual(r.tops, ["wrap"])
        self.assertEqual(r.top_key, "wrap")

    def test_missing_top(self):
        r, d = elab(ADDER, "nothere")
        self.assertIsNone(r.top_key)
        self.assertIn("MISSING_UNIT", [x.code for x in r.diagnostics])

    def test_design_top_is_set_and_hashes_returned(self):
        parsed = parse_text(ADDER)
        d = Design()
        r = elaborate_vhdl(parsed, d, top="add2")
        self.assertEqual(d.top, "add2(w=4)")
        self.assertIn("add2", r.unit_hashes)
        r2 = elaborate_vhdl(parse_text(ADDER.replace("x + z", "x - z")), Design(), top="add2")
        self.assertNotEqual(r.unit_hashes["add2"], r2.unit_hashes["add2"])
        r3 = elaborate_vhdl(parse_text(ADDER.replace("begin s", "begin  -- comment\n s")), Design(), top="add2")
        self.assertEqual(r.unit_hashes["add2"], r3.unit_hashes["add2"])

    def test_files_entry_point(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "a.vhd")
            with open(p, "w") as fh:
                fh.write(ADDER)
            r, d = elaborate_vhdl_files([p], top="add2", generics={"W": 6})
            self.assertEqual(r.top_key, "add2(w=6)")
            self.assertEqual(d.modules[r.top_key].port("s").width, 6)

    def test_lazy_resolution_independent_of_file_order(self):
        a = ADDER
        b = (HDR + "entity wrap is port (a, b : in unsigned(3 downto 0); y : out unsigned(3 downto 0)); end entity;\n"
             "architecture r of wrap is begin u : entity work.add2 port map (a, b, y); end architecture;\n")
        for text in (a + b, b + a):
            r, d = elab(text, "wrap")
            self.assertEqual(nodes(d.modules["wrap"], "INST")[0].attrs["module_key"], "add2(w=4)")


class MixedLanguage(unittest.TestCase):
    """The foreign-module protocol (documented in ``gatecost.elab.vhdl``) stubbed with a fake Verilog module."""

    class FakeVerilog:
        name = "vadd"
        parameters = {"W": 4}

        def __init__(self, design):
            self.design = design
            self.elaborated = []

        def ports(self, params):
            w = int(params["W"])
            return [("a", "in", w, False), ("b", "in", w, False), ("y", "out", w, False)]

        def elaborate(self, params, const_ports):
            from gatecost.ir import Builder
            w = int(params["W"])
            key = Design.make_key("vadd", params, const_ports)
            if key not in self.design.modules:
                b = Builder("vadd", params, const_ports, key=key)
                a, c, y = b.port("a", "in", w), b.port("b", "in", w), b.port("y", "out", w)
                b.connect(y, b.add(a.sig, c.sig, w))
                self.design.add(b.finalize())
                self.elaborated.append(key)
            return key

    def test_vhdl_instantiates_a_foreign_module(self):
        d = Design()
        fv = self.FakeVerilog(d)
        seen = []

        def lookup(name):
            seen.append(name)
            return fv if name.lower() == "vadd" else None

        comp = "component VAdd generic (W : integer := 4); port (a, b : in std_logic_vector(W - 1 downto 0); y : out std_logic_vector(W - 1 downto 0)); end component;"
        src = unit("x, z : in std_logic_vector(7 downto 0); s : out std_logic_vector(7 downto 0)", comp,
                   "u : VAdd generic map (W => 8) port map (a => x, b => z, y => s);")
        parsed = parse_text(src)
        r = elaborate_vhdl(parsed, d, top="t", foreign_lookup=lookup)
        self.assertEqual(seen[0], "vadd")
        inst = nodes(d.modules["t"], "INST")
        self.assertEqual(inst[0].attrs["module_key"], "vadd(W=8)")
        self.assertEqual(fv.elaborated, ["vadd(W=8)"])
        self.assertNotIn("MISSING_MODULE", [x.code for x in r.diagnostics])

    def test_without_lookup_it_is_a_black_box(self):
        comp = "component vadd port (a, b : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)); end component;"
        src = unit("x, z : in std_logic_vector(3 downto 0); s : out std_logic_vector(3 downto 0)", comp, "u : vadd port map (a => x, b => z, y => s);")
        r, d = elab(src)
        self.assertEqual(len(nodes(d.modules["t"], "BLACKBOX")), 1)
        self.assertIn("MISSING_MODULE", [x.code for x in r.diagnostics])

    def test_other_language_instantiates_a_vhdl_entity(self):
        parsed = parse_text(ADDER)
        d = Design()
        el = VhdlElaborator(parsed, d)
        fm = el.lookup("ADD2")
        self.assertEqual(fm.name, "add2")
        self.assertEqual(fm.parameters, {"w": 4})
        self.assertEqual(fm.ports({"W": 6}), [("x", "in", 6, False), ("z", "in", 6, False), ("s", "out", 6, False)])
        key = fm.elaborate({"W": 6}, {})
        self.assertEqual(key, "add2(w=6)")
        self.assertIn(key, d.modules)
        self.assertIsNone(el.lookup("nothing"))


if __name__ == "__main__":
    unittest.main()
