"""Elaborator: language routing and the VHDL elaborator hook contract."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gatecost.elab import elaborate_project, register_language_elaborator
from gatecost.ir import Builder
from gatecost.result import Diagnostic

VHDL = "entity e is port (a : in bit; y : out bit); end entity; architecture r of e is begin y <= a; end architecture;"
VERILOG = "module m(input a, output y); assign y = ~a; endmodule"


def run(files, **kw):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name, text in files.items():
            (root / name).write_text(text)
        return elaborate_project([Path(n) for n in files], root=root, **kw)


class Hook(unittest.TestCase):
    def tearDown(self):
        register_language_elaborator("vhdl", None)

    def test_vhdl_files_are_routed_to_the_registered_elaborator(self):
        seen = {}

        def fake(parsed, design, *, top, limits, side, only_modules, foreign, root):
            seen.update(top=top, side=side, only=only_modules, foreign=foreign, entities=sorted(parsed.entities),
                        call_depth=limits["call_depth"])
            b = Builder("e")
            b.port("a", "in", 1)
            b.port("y", "out", 1)
            b.connect(b.module.ports[1], b.module.ports[0].sig)
            design.add(b.finalize())
            return SimpleNamespace(top_key="e", tops=["e"], diagnostics=[Diagnostic("MISSING_UNIT", "x", side=side)],
                                   assumptions=["vhdl assumption"], parsed_hashes={"e": "h"})

        register_language_elaborator("vhdl", fake)
        r = run({"e.vhd": VHDL}, side="before")
        self.assertEqual((seen["top"], seen["side"]), (None, "before"))
        self.assertIsNone(seen["foreign"]("nothing"))
        self.assertEqual(seen["entities"], [("work", "e")])
        self.assertEqual(r.top_key, "e")
        self.assertEqual(r.tops, ["e"])
        self.assertIn("vhdl assumption", r.assumptions)
        self.assertEqual(r.parsed_hashes["e"], "h")
        self.assertIn("MISSING_UNIT", [d.code for d in r.diagnostics])
        self.assertTrue(r.design.modules["e"].analyzed)

    def test_mixed_project_elaborates_verilog_and_passes_top_to_vhdl(self):
        calls = []

        def fake(parsed, design, **kw):
            calls.append(kw["top"])
            return SimpleNamespace(top_key=None, tops=["e"], diagnostics=[], assumptions=[], parsed_hashes={})

        register_language_elaborator("vhdl", fake)
        r = run({"m.v": VERILOG, "e.vhd": VHDL})
        self.assertEqual(r.top_key, "m")
        self.assertEqual(sorted(r.tops), ["e", "m"])
        r = run({"m.v": VERILOG, "e.vhd": VHDL}, top="e")
        self.assertEqual(calls, [None, "e"])
        self.assertIsNone(r.top_key)

    def test_default_path_uses_the_real_vhdl_elaborator(self):
        r = run({"e.vhd": VHDL})
        self.assertEqual(r.top_key, "e")
        self.assertIn("e", r.design.keys())

    def test_failing_vhdl_elaborator_does_not_abort(self):
        def boom(parsed, design, **kw):
            raise RuntimeError("bug")

        register_language_elaborator("vhdl", boom)
        r = run({"m.v": VERILOG, "e.vhd": VHDL})
        self.assertEqual(r.top_key, "m")
        self.assertTrue(any("VHDL elaboration failed" in d.message for d in r.diagnostics))


if __name__ == "__main__":
    unittest.main()


class MixedLanguage(unittest.TestCase):
    def test_verilog_instantiates_vhdl_entity(self):
        r = run({
            "inv.vhd": """library ieee; use ieee.std_logic_1164.all;
entity vinv is generic (W : integer := 4); port (a : in std_logic_vector(W-1 downto 0); y : out std_logic_vector(W-1 downto 0)); end entity;
architecture rtl of vinv is begin y <= not a; end architecture;""",
            "top.sv": "module top(input [7:0] x, output [7:0] z); vinv #(.W(8)) u (.a(x), .y(z)); endmodule"})
        self.assertEqual(r.top_key, "top")
        self.assertEqual(r.tops, ["top"])
        self.assertIn("vinv(w=8)", r.design.keys())
        self.assertEqual([d.code for d in r.diagnostics if d.affects_cost], [])
        self.assertEqual(r.design.instance_counts()["vinv(w=8)"], 1)

    def test_vhdl_instantiates_verilog_module(self):
        r = run({
            "add.v": "module vadd #(parameter W = 4)(input [W-1:0] a, b, output [W-1:0] s); assign s = a + b; endmodule",
            "top.vhd": """library ieee; use ieee.std_logic_1164.all;
entity vtop is port (x, y : in std_logic_vector(7 downto 0); s : out std_logic_vector(7 downto 0)); end entity;
architecture rtl of vtop is
  component vadd generic (W : integer := 4); port (a, b : in std_logic_vector(W-1 downto 0); s : out std_logic_vector(W-1 downto 0)); end component;
begin
  u : vadd generic map (W => 8) port map (a => x, b => y, s => s);
end architecture;"""})
        self.assertEqual(r.top_key, "vtop")
        self.assertEqual(r.tops, ["vtop"])
        self.assertIn("vadd(W=8)", r.design.keys())
        self.assertEqual([d.code for d in r.diagnostics if d.affects_cost], [])
