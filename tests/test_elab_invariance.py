"""Elaborator: presentation invariance (reformatting, renaming locals, reordering items)."""
import re
import unittest

from tests.elab_helpers import elab

BASE = """
module alu #(parameter W = 8)(input [W-1:0] a, b, input [1:0] op, output reg [W-1:0] y, output zero);
  wire [W-1:0] sum = a + b;
  wire [W-1:0] dif = a - b;
  always @* begin
    case (op)
      2'd0: y = sum;
      2'd1: y = dif;
      2'd2: y = a & b;
      default: y = a ^ b;
    endcase
  end
  assign zero = (y == 0);
endmodule

module top(input clk, input rst, input [7:0] x, input [7:0] z, input [1:0] op, output reg [7:0] acc, output zf);
  wire [7:0] r;
  alu #(.W(8)) u_alu (.a(acc), .b(x), .op(op), .y(r), .zero(zf));
  reg [7:0] hold;
  always @(posedge clk) begin
    if (rst) acc <= 8'd0;
    else acc <= r + z;
    hold <= acc;
  end
endmodule
"""

REFORMATTED = """
// a comment
module alu
  #(parameter W = 8)
  (input  [W-1:0] a,
   input  [W-1:0] b, /* block comment */
   input  [1:0] op,
   output reg [W-1:0] y,
   output zero);

  wire [W-1:0] sum = a +
                     b;
  wire [W-1:0] dif = a - b;

  always @*
  begin
      case (op)
        2'd0 : y = sum;
        2'd1 : y = dif;
        2'd2 : y = a & b;
        default : y = a ^ b;
      endcase
  end
  assign zero = (y == 0);
endmodule
module top(input clk,input rst,input [7:0] x,input [7:0] z,input [1:0] op,output reg [7:0] acc,output zf);
wire [7:0] r;alu #(.W(8)) u_alu(.a(acc),.b(x),.op(op),.y(r),.zero(zf));reg [7:0] hold;
always @(posedge clk) begin if (rst) acc <= 8'd0; else acc <= r + z; hold <= acc; end
endmodule
"""

RENAMED = (BASE.replace("sum", "total").replace("dif", "delta").replace("hold", "shadow")
           .replace("wire [7:0] r;", "wire [7:0] res;")
           .replace(".y(r)", ".y(res)").replace("r + z", "res + z"))

REORDERED = """
module top(input clk, input rst, input [7:0] x, input [7:0] z, input [1:0] op, output reg [7:0] acc, output zf);
  reg [7:0] hold;
  wire [7:0] r;
  always @(posedge clk) begin
    hold <= acc;
    if (rst) acc <= 8'd0;
    else acc <= r + z;
  end
  alu #(.W(8)) u_alu (.a(acc), .b(x), .op(op), .y(r), .zero(zf));
endmodule

module alu #(parameter W = 8)(input [W-1:0] a, b, input [1:0] op, output reg [W-1:0] y, output zero);
  assign zero = (y == 0);
  wire [W-1:0] dif = a - b;
  wire [W-1:0] sum = a + b;
  always @* begin
    case (op)
      2'd0: y = sum;
      2'd1: y = dif;
      2'd2: y = a & b;
      default: y = a ^ b;
    endcase
  end
endmodule
"""


def hashes(res):
    return {k: m.canonical_hash for k, m in res.design.modules.items()}


class Invariance(unittest.TestCase):
    def setUp(self):
        self.base = hashes(elab(BASE, top="top"))

    def test_reformatting(self):
        self.assertEqual(hashes(elab(REFORMATTED, top="top")), self.base)

    def test_renaming_internal_names(self):
        self.assertEqual(hashes(elab(RENAMED, top="top")), self.base)

    def test_reordering_independent_items(self):
        self.assertEqual(hashes(elab(REORDERED, top="top")), self.base)

    def test_a_real_change_is_detected(self):
        changed = BASE.replace("a - b", "a + b")
        self.assertNotEqual(hashes(elab(changed, top="top")), self.base)

    def test_module_digests_ignore_presentation(self):
        a = elab(BASE, top="top").parsed_hashes
        b = elab(REFORMATTED, top="top").parsed_hashes
        self.assertEqual(a, b)

    def test_generate_and_function_forms_are_stable(self):
        a = """
module t #(parameter N = 4)(input [N-1:0] x, output [N-1:0] y);
  function automatic [N-1:0] rev(input [N-1:0] v); integer i; begin for (i = 0; i < N; i = i + 1) rev[i] = v[N-1-i]; end endfunction
  genvar g;
  generate for (g = 0; g < N; g = g + 1) begin : b assign y[g] = rev(x)[g] ^ x[g]; end endgenerate
endmodule"""
        b = a.replace("rev", "flip").replace("genvar g", "genvar q").replace("g = 0", "q = 0").replace("g < N", "q < N") \
             .replace("g = g + 1", "q = q + 1").replace("y[g]", "y[q]").replace("x[g]", "x[q]").replace(")[g]", ")[q]")
        self.assertEqual(hashes(elab(a)), hashes(elab(b)))


if __name__ == "__main__":
    unittest.main()
