"""unique / priority / parallel_case / full_case in the Verilog elaborator."""
import unittest

from tests.elab_helpers import codes, elab, ops, top_module


def body(stmt, extra=""):
    return f"""
module t(input [1:0] s, input a, b, c, output reg y);
  {extra}
  always_comb begin
    {stmt}
  end
endmodule"""


class Qualifiers(unittest.TestCase):
    def m(self, stmt, extra=""):
        r = elab(body(stmt, extra), top="t")
        return r, ops(top_module(r))

    def test_plain_case_with_default_unaffected(self):
        r, o = self.m("case (s) 0: y = a; 1: y = b; default: y = c; endcase")
        self.assertIn("PMUX", o)

    def test_priority_case_is_priority(self):
        r, o = self.m("priority case (s) 0: y = a; 1: y = b; 2: y = c; default: y = a; endcase")
        self.assertIn("PRIORITY", o)
        self.assertNotIn("PMUX", o)

    def test_unique_case_without_default_has_no_latch(self):
        r, o = self.m("unique case (s) 0: y = a; 1: y = b; 2: y = c; endcase")
        self.assertNotIn("INFERRED_LATCH", codes(r))
        self.assertNotIn("LATCH", o)
        self.assertIn("PMUX", o)

    def test_plain_case_without_default_latches(self):
        r, o = self.m("case (s) 0: y = a; 1: y = b; 2: y = c; endcase")
        self.assertIn("INFERRED_LATCH", codes(r))

    def test_full_case_attribute_removes_latch(self):
        r, o = self.m("(* full_case *) case (s) 0: y = a; 1: y = b; 2: y = c; endcase")
        self.assertNotIn("INFERRED_LATCH", codes(r))

    def test_parallel_case_forces_pmux_on_overlapping_labels(self):
        stmt = "casez (s) 2'b1?: y = a; 2'b?1: y = b; default: y = c; endcase"
        _, o = self.m(stmt)
        self.assertIn("PRIORITY", o)
        _, o = self.m("(* parallel_case *) " + stmt)
        self.assertIn("PMUX", o)
        self.assertNotIn("PRIORITY", o)

    def test_synopsys_comment_pragmas(self):
        r, o = self.m("case (s) // synopsys full_case parallel_case\n 0: y = a; 1: y = b; 2: y = c; endcase")
        self.assertNotIn("INFERRED_LATCH", codes(r))

    def test_unique_if_chain_is_parallel_and_full(self):
        r, o = self.m("unique if (s == 0) y = a; else if (s == 1) y = b; else if (a & b) y = c;")
        self.assertNotIn("INFERRED_LATCH", codes(r))
        self.assertIn("PMUX", o)

    def test_priority_if_has_no_latch(self):
        r, o = self.m("priority if (a) y = b; else if (b) y = c;")
        self.assertNotIn("INFERRED_LATCH", codes(r))

    def test_unique0_if_keeps_latch(self):
        r, o = self.m("unique0 if (a) y = b; else if (b) y = c;")
        self.assertIn("INFERRED_LATCH", codes(r))

    def test_unique_case_in_clocked_block_has_no_hold(self):
        r = elab("""
module t(input clk, input [1:0] s, input a, b, output reg y);
  always_ff @(posedge clk) unique case (s) 0: y <= a; 1: y <= b; 2: y <= a; 3: y <= b; endcase
endmodule""", top="t")
        o = ops(top_module(r))
        self.assertNotIn("INFERRED_LATCH", codes(r))
        self.assertEqual(o.get("DFF"), 1)


if __name__ == "__main__":
    unittest.main()
