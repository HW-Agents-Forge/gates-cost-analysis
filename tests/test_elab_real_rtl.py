"""Regressions found by running the tool on real open-source RTL (common_cells, ...)."""
import tempfile
import unittest
from pathlib import Path

from gatecost.elab import elaborate_project


def run(text, name="m.sv", **kw):
    with tempfile.TemporaryDirectory() as td:
        Path(td, name).write_text(text)
        return elaborate_project([Path(name)], root=Path(td), **kw)


class HeaderImport(unittest.TestCase):
    SRC = """package p;
  typedef enum logic {A=1'b0, B=1'b1} mode_e;
  function automatic int unsigned pw(int unsigned w); return w + 3; endfunction
endpackage
module m import p::*; #(
  parameter mode_e Mode = B,
  parameter int W = 8,
  localparam type par_t = logic [pw(W)-1:0]
)(input logic [W-1:0] a, output par_t y);
  assign y = Mode == B ? par_t'(a) : '0;
endmodule
"""

    def test_module_header_import_is_visible_in_the_parameter_list(self):
        res = run(self.SRC)
        bad = [d for d in res.diagnostics if d.code in ("UNRESOLVED_NAME", "UNRESOLVED_PARAM", "INTERNAL")]
        self.assertEqual(bad, [])


class ErrorTaskInFunction(unittest.TestCase):
    SRC = """module m #(parameter CFG = "A") (input [3:0] a, output [3:0] y);
  function [3:0] f(input [3:0] x);
    begin
      if (CFG == "A") f = x ^ 4'b0101;
      else if (CFG == "B") f = ~x;
      else begin
        $error("Error: unknown configuration setting!");
        $finish;
      end
    end
  endfunction
  assign y = f(a);
endmodule
"""

    def test_error_and_finish_do_not_make_a_function_nonsynthesizable(self):
        res = run(self.SRC)
        self.assertFalse([d for d in res.diagnostics if "non-synthesizable" in d.message])
        from gatecost.ir.dump import dump
        (m,) = res.design.modules.values()
        self.assertNotIn("4'hx", dump(m, analysis=False))


class FunctionStepBudget(unittest.TestCase):
    SRC = """module m(output [7:0] y);
  function [7:0] f(input [7:0] n);
    integer i;
    begin f = 0; for (i = 0; i < 8; i = i + 1) f = f + n; end
  endfunction
  genvar g;
  generate for (g = 0; g < 20; g = g + 1) begin : b
    wire [7:0] w = f(g);
  end endgenerate
  assign y = b[3].w;
endmodule
"""

    def test_total_steps_of_one_function_are_bounded(self):
        full = run(self.SRC)
        self.assertFalse([d for d in full.diagnostics if d.code == "LIMIT_REACHED"])
        cut = run(self.SRC, limits={"const_func_total_steps": 50})
        self.assertTrue([d for d in cut.diagnostics if d.code == "LIMIT_REACHED"])


if __name__ == "__main__":
    unittest.main()
