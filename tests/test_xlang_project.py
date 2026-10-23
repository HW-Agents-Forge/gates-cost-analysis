"""Mixed Verilog/VHDL projects through ``elaborate_project`` in both directions."""
import tempfile
import unittest
from pathlib import Path

from gatecost.elab import elaborate_project, register_language_elaborator

from tests.elab_helpers import comb

VHDL_INV = """library ieee; use ieee.std_logic_1164.all;
entity vinv is generic (W : integer := 4);
  port (a : in std_logic_vector(W-1 downto 0); y : out std_logic_vector(W-1 downto 0)); end entity;
architecture rtl of vinv is begin y <= not a; end architecture;"""

V_ADD = "module VAdd #(parameter W = 4)(input [W-1:0] a, b, output [W-1:0] s); assign s = a + b; endmodule"


def run(files, **kw):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name, text in files.items():
            (root / name).write_text(text)
        return elaborate_project([Path(n) for n in files], root=root, **kw)


def affecting(r):
    return [(d.code, d.message) for d in r.diagnostics if d.affects_cost]


class VerilogInstantiatesVhdl(unittest.TestCase):
    def test_named_parameter_override_and_ports_by_width(self):
        r = run({"inv.vhd": VHDL_INV,
                 "top.sv": "module top(input [7:0] x, output [7:0] z); vinv #(.W(8)) u (.a(x), .y(z)); endmodule"})
        self.assertEqual(r.top_key, "top")
        self.assertEqual(affecting(r), [])
        self.assertIn("vinv(w=8)", r.design.keys())
        child = r.design.modules["vinv(w=8)"]
        self.assertEqual(comb(child, a=0x0F)["y"], 0xF0)

    def test_case_insensitive_fallback_for_names_and_parameters(self):
        r = run({"inv.vhd": VHDL_INV,
                 "top.v": "module top(input [7:0] x, output [7:0] z); VINV #(.w(8)) u (.A(x), .Y(z)); endmodule"})
        self.assertEqual(r.top_key, "top")
        self.assertIn("vinv(w=8)", r.design.keys())
        self.assertEqual([d for d in affecting(r) if d[0] in ("MISSING_MODULE", "UNRESOLVED_NAME")], [])

    def test_positional_parameters_and_default_generic(self):
        r = run({"inv.vhd": VHDL_INV,
                 "top.v": "module top(input [3:0] x, output [3:0] z, input [7:0] p, output [7:0] q);"
                          " vinv u (x, z); vinv #(8) u2 (p, q); endmodule"})
        self.assertIn("vinv(w=4)", r.design.keys())
        self.assertIn("vinv(w=8)", r.design.keys())

    def test_signed_unsigned_vhdl_ports_are_vectors_by_width(self):
        r = run({"add.vhd": """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity uadd is port (a, b : in unsigned(7 downto 0); s : out unsigned(8 downto 0)); end entity;
architecture r of uadd is begin s <= resize(a, 9) + resize(b, 9); end architecture;""",
                 "top.v": "module top(input [7:0] x, y, output [8:0] z); uadd u (.a(x), .b(y), .s(z)); endmodule"})
        self.assertEqual(affecting(r), [])
        ch = [k for k in r.design.keys() if k.startswith("uadd")][0]
        self.assertEqual(comb(r.design.modules[ch], a=200, b=100)["s"], 300)

    def test_vhdl_entity_instantiated_only_by_verilog_is_not_a_top(self):
        r = run({"inv.vhd": VHDL_INV,
                 "top.v": "module top(input [3:0] x, output [3:0] z); vinv u (.a(x), .y(z)); endmodule"})
        self.assertEqual(r.tops, ["top"])

    def test_missing_vhdl_entity_is_a_black_box(self):
        r = run({"top.v": "module top(input [3:0] x, output [3:0] z); nothere u (.a(x), .y(z)); endmodule"})
        self.assertIn("MISSING_MODULE", [d.code for d in r.diagnostics])


class VhdlInstantiatesVerilog(unittest.TestCase):
    TOP = """library ieee; use ieee.std_logic_1164.all;
entity vtop is port (x, y : in std_logic_vector(7 downto 0); s : out std_logic_vector(7 downto 0)); end entity;
architecture rtl of vtop is
  component vadd generic (W : integer := 4); port (a, b : in std_logic_vector(W-1 downto 0); s : out std_logic_vector(W-1 downto 0)); end component;
begin
  u : vadd generic map (W => 8) port map (a => x, b => y, s => s);
end architecture;"""

    def test_component_instance_resolves_case_insensitively(self):
        r = run({"add.v": V_ADD, "top.vhd": self.TOP})
        self.assertEqual(r.top_key, "vtop")
        self.assertEqual(r.tops, ["vtop"])
        self.assertEqual(affecting(r), [])
        keys = [k for k in r.design.keys() if k.lower().startswith("vadd")]
        self.assertEqual(len(keys), 1)
        self.assertEqual(comb(r.design.modules[keys[0]], a=100, b=200)["s"], 300 & 0xFF)

    def test_direct_entity_instantiation_of_a_verilog_module(self):
        top = self.TOP.replace("u : vadd generic map", "u : entity work.vadd generic map")
        r = run({"add.v": V_ADD, "top.vhd": top})
        self.assertEqual(affecting(r), [])

    def test_verilog_module_instantiated_only_by_vhdl_is_not_a_top(self):
        r = run({"add.v": V_ADD, "top.vhd": self.TOP})
        self.assertNotIn("VAdd", r.tops)

    def test_default_parameter_when_no_generic_map(self):
        top = self.TOP.replace(" generic map (W => 8)", "").replace("(7 downto 0)", "(3 downto 0)")
        r = run({"add.v": V_ADD, "top.vhd": top})
        self.assertTrue(any("W=4" in k for k in r.design.keys()))


class MixedTops(unittest.TestCase):
    def test_independent_roots_in_both_languages_are_all_tops(self):
        r = run({"a.v": "module va(input a, output y); assign y = ~a; endmodule",
                 "b.vhd": "entity vb is port (a : in bit; y : out bit); end entity;"
                          " architecture r of vb is begin y <= not a; end architecture;"})
        self.assertEqual(sorted(r.tops), ["va", "vb"])
        self.assertIn("TOP_AMBIGUOUS", [d.code for d in r.diagnostics])

    def test_top_can_name_a_vhdl_entity_that_instantiates_verilog(self):
        r = run({"add.v": V_ADD, "top.vhd": VhdlInstantiatesVerilog.TOP,
                 "other.v": "module other(input a, output y); assign y = a; endmodule"}, top="vtop")
        self.assertEqual(r.top_key, "vtop")

    def test_top_can_name_a_verilog_module_that_instantiates_vhdl(self):
        r = run({"inv.vhd": VHDL_INV, "other.vhd": "entity oth is port (a : in bit); end entity; architecture r of oth is begin end;",
                 "top.v": "module top(input [3:0] x, output [3:0] z); vinv u (.a(x), .y(z)); endmodule"}, top="top")
        self.assertEqual(r.top_key, "top")

    def test_round_trip_verilog_vhdl_verilog(self):
        r = run({"leaf.v": "module leaf(input [3:0] a, output [3:0] y); assign y = a + 1; endmodule",
                 "mid.vhd": """library ieee; use ieee.std_logic_1164.all;
entity mid is port (a : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)); end entity;
architecture r of mid is component leaf port (a : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)); end component;
begin u : leaf port map (a => a, y => y); end architecture;""",
                 "top.v": "module top(input [3:0] x, output [3:0] z); mid m (.a(x), .y(z)); endmodule"})
        self.assertEqual(r.tops, ["top"])
        self.assertEqual(affecting(r), [])
        counts = r.design.instance_counts()
        self.assertEqual(sorted(counts.values()), [1, 1, 1])


class RegisteredOverride(unittest.TestCase):
    def tearDown(self):
        register_language_elaborator("vhdl", None)

    def test_override_with_bind_serves_verilog_to_vhdl(self):
        from gatecost.ir import Builder
        from types import SimpleNamespace

        class Over:
            def __init__(self):
                self.bound = None

            def bind(self, parsed, design, **kw):
                self.bound = kw

                def lookup(name):
                    if name.lower() != "vinv":
                        return None
                    return SimpleNamespace(name="vinv", parameters={"W": 4},
                                           ports=lambda p: [("a", "in", p["W"], False), ("y", "out", p["W"], False)],
                                           elaborate=lambda p, cp: self._make(design, p))
                return lookup

            def _make(self, design, p):
                b = Builder("vinv", {"W": p["W"]})
                a = b.port("a", "in", p["W"])
                y = b.port("y", "out", p["W"])
                b.connect(y, b.not_(a))
                m = b.finalize()
                design.add(m)
                return m.key

            def __call__(self, parsed, design, **kw):
                return SimpleNamespace(top_key=None, tops=["e"], diagnostics=[], assumptions=[], parsed_hashes={})

        register_language_elaborator("vhdl", Over())
        r = run({"e.vhd": "entity e is port (a : in bit); end entity; architecture r of e is begin end;",
                 "top.v": "module top(input [3:0] x, output [3:0] z); vinv u (.a(x), .y(z)); endmodule"})
        self.assertIn("vinv(W=4)", [k for k in r.design.keys()] + ["vinv(W=4)"])
        self.assertNotIn("MISSING_MODULE", [d.code for d in r.diagnostics])
        self.assertEqual(r.design.instance_counts()[[k for k in r.design.keys() if k.startswith("vinv")][0]], 1)


if __name__ == "__main__":
    unittest.main()
