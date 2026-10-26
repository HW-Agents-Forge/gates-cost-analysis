"""`cost` wording and diagnostics scope (D6a/b)."""
import unittest

from gatecost.cli.notes import cone_filter, cost_wording
from gatecost.result import Confidence, Diagnostic
from tests.test_diff_support import mod, op, tree
from tests.test_vhdl_real_cli import Base, codes, run_cost


class CostWording(Base):
    def test_cost_does_not_speak_of_a_changed_cone(self):
        self.write("t.v", "module t(input [3:0] a, output [3:0] y); assign y = $foo(a); endmodule\n")
        code, doc = run_cost("--top", "t", "t.v", cwd=self.dir)
        reasons = doc["result"]["confidence"]["reasons"]
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(doc))
        self.assertTrue(any("unsupported construct" in r and "in the analyzed design" in r for r in reasons), reasons)
        self.assertFalse(any("changed cone" in r for r in reasons), reasons)

    def test_unit(self):
        c = cost_wording(Confidence("low", ("1 unsupported construct in the changed cone", "other in the changed cone")))
        self.assertEqual(c.reasons, ("1 unsupported construct in the analyzed design", "other in the changed cone"))


class ConeFilter(Base):
    def test_out_of_cone_notes_are_dropped_in_cost(self):
        self.write("top.v", "module top(input a, output y); initial begin end\n assign y = ~a; endmodule\n")
        self.write("other.v", "module other(input a, output y); initial begin end\n assign y = a; endmodule\n"
                              "module other2(input a, output y); initial begin end\n assign y = a; endmodule\n")
        code, doc = run_cost("--top", "top", "top.v", "other.v", cwd=self.dir)
        files = {d["file"] for d in doc["diagnostics"] if d["code"] == "IGNORED_REGION"}
        self.assertEqual(files, {"top.v"}, doc["diagnostics"])
        self.assertGreaterEqual(doc["meta"]["diagnostics_out_of_cone"], 1)

    def test_cost_affecting_and_header_diagnostics_are_kept(self):
        t = tree([mod("top", [op("AND", 10, tgt="o")], file="top.v")])
        d = [Diagnostic("UNSUPPORTED_CONSTRUCT", "x", "warning", True, "after", "other.v", 3),
             Diagnostic("IGNORED_REGION", "x", "info", False, "after", "defs.vh", 3),
             Diagnostic("IGNORED_REGION", "x", "info", False, "after", None, None),
             Diagnostic("IGNORED_REGION", "x", "info", False, "after", "top.v", 1),
             Diagnostic("IGNORED_REGION", "x", "info", False, "after", "other.v", 1)]
        kept, dropped = cone_filter(t, d)
        self.assertEqual((len(kept), dropped), (4, 1))
        self.assertNotIn(("IGNORED_REGION", "other.v"), [(x.code, x.file) for x in kept])


class StringGenericHint(Base):
    VHD = ("library ieee; use ieee.std_logic_1164.all;\n"
           "entity s is generic (StopBits_g : string := \"1\"); port (a : in std_logic; y : out std_logic);\n"
           "end entity;\narchitecture rtl of s is begin\n"
           "  g1 : if StopBits_g = \"1\" generate y <= a; end generate;\n"
           "  g2 : if StopBits_g = \"2\" generate y <= not a; end generate;\nend architecture;\n")

    def test_unquoted_value_for_a_string_generic_suggests_the_quoted_form(self):
        self.write("s.vhd", self.VHD)
        code, doc = run_cost("--top", "s", "--param", "StopBits_g=2", "s.vhd", cwd=self.dir)
        msgs = [d["message"] for d in doc["diagnostics"] if "scalar to an array" in d["message"]]
        self.assertEqual(len(msgs), 1, doc["diagnostics"])
        self.assertIn("quote the value", msgs[0])
        self.assertIn("--param StopBits_g='\"2\"'", msgs[0])

    def test_quoted_value_is_fine(self):
        self.write("s.vhd", self.VHD)
        code, doc = run_cost("--top", "s", "--param", 'StopBits_g="2"', "s.vhd", cwd=self.dir)
        self.assertNotIn("UNSUPPORTED_CONSTRUCT", codes(doc))


if __name__ == "__main__":
    unittest.main()
