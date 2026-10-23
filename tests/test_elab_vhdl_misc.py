"""Determinism, presentation invariance, origins, diagnostics metadata of the VHDL elaborator."""
import unittest

from gatecost.ir import analyze_ir, dump

from tests.test_elab_vhdl_util import codes, elab, module, nodes, ops, unit

BASE = ("clk, rst, en : in std_logic; a, b : in unsigned(7 downto 0); q : out unsigned(7 downto 0); s : out unsigned(7 downto 0)")


def design_src(tmp="tmp", decl_order=False, spacing="  ", sig="acc"):
    decls = ("signal %s : unsigned(7 downto 0);\nsignal %s : unsigned(7 downto 0);" % (sig, tmp))
    if decl_order:
        decls = ("signal %s : unsigned(7 downto 0);\nsignal %s : unsigned(7 downto 0);" % (tmp, sig))
    body = ("%s <= a + b;\n"
            "process(clk) begin%sif rising_edge(clk) then%sif rst = '1' then %s <= (others => '0'); elsif en = '1' then %s <= %s + %s; end if; end if; end process;\n"
            "q <= %s; s <= %s;" % (tmp, spacing, spacing, sig, sig, sig, tmp, sig, tmp))
    return unit(BASE, decls, body)


class Invariance(unittest.TestCase):
    def test_renaming_reordering_and_reformatting_keep_the_canonical_hash(self):
        ref, _, _ = module(design_src())
        for kw in ({"tmp": "sum_i"}, {"decl_order": True}, {"spacing": "\n\n    "}, {"tmp": "x_y", "decl_order": True, "spacing": "\t"}):
            with self.subTest(**kw):
                m, _, _ = module(design_src(**kw))
                self.assertEqual(m.canonical_hash, ref.canonical_hash)

    def test_changing_the_function_changes_the_hash(self):
        ref, _, _ = module(design_src())
        m, _, _ = module(design_src().replace("tmp <= a + b;", "tmp <= a - b;"))
        self.assertNotEqual(m.canonical_hash, ref.canonical_hash)
        m, _, _ = module(design_src(sig="renamed_register"))
        self.assertNotEqual(m.canonical_hash, ref.canonical_hash)

    def test_runs_are_deterministic(self):
        outs = []
        for _ in range(2):
            m, r, d = module(design_src())
            outs.append((dump(m), [(x.code, x.message, x.line) for x in r.diagnostics], r.unit_hashes, list(d.keys())))
        self.assertEqual(outs[0], outs[1])


class Metadata(unittest.TestCase):
    def test_origins_point_to_the_statement(self):
        src = unit("a, b : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", "", "y <= a\n  + b;")
        m, r, d = module(src)
        add = nodes(m, "ADD")[0]
        self.assertEqual((add.origin.file, add.origin.line), ("<string>", src.split("\n").index("y <= a") + 1))

    def test_diagnostics_have_location_and_side(self):
        src = unit("a : in std_logic; y : out std_logic", "", "y <= mystery(a);")
        r, d = elab(src, side="before")
        dg = [x for x in r.diagnostics if x.code == "UNRESOLVED_OVERLOAD"][0]
        self.assertEqual(dg.side, "before")
        self.assertEqual(dg.line, src.split("\n").index("y <= mystery(a);") + 1)
        self.assertTrue(dg.affects_cost)
        self.assertEqual(dg.severity, "warning")

    def test_severity_and_cost_flags(self):
        src = unit("a : in std_logic; y : out std_logic", "", "y <= a;\n process begin wait for 1 ns; end process;")
        r, d = elab(src)
        sim = [x for x in r.diagnostics if x.code == "SIMULATION_ONLY_PROCESS"][0]
        self.assertEqual((sim.severity, sim.affects_cost), ("info", False))

    def test_assumptions_are_recorded_on_the_module(self):
        m, r, d = module(unit("a : in std_logic_vector(3 downto 0); y : out std_logic_vector(7 downto 0)", "", "y <= a;"))
        self.assertTrue(m.assumptions)

    def test_result_fields(self):
        m, r, d = module(unit("a : in std_logic; y : out std_logic", "", "y <= a;"))
        self.assertEqual((r.tops, r.top_key, r.top_keys), (["t"], "t", {"t": "t"}))
        self.assertEqual(r.stubbed, ())
        self.assertEqual(set(r.parsed_hashes), {"t"})
        self.assertEqual(d.top, "t")

    def test_shared_design_with_two_languages_can_coexist(self):
        from gatecost.ir import Builder
        r, d = elab(unit("a : in std_logic; y : out std_logic", "", "y <= a;"))
        b = Builder("other")
        x = b.port("x", "in", 1)
        d.add(b.finalize())
        self.assertEqual(sorted(d.modules), ["other", "t"])


if __name__ == "__main__":
    unittest.main()
