"""Elaboration speed on a generated design (FRONTEND.md section 8)."""
import random
import time
import unittest

from tests.elab_helpers import elab


def generate(n_modules: int, seed: int = 1) -> str:
    rnd = random.Random(seed)
    parts = []
    for k in range(n_modules):
        w = rnd.choice([8, 16, 32])
        pads = "\n".join(f"  wire [W-1:0] pad{j} = a + {j};" for j in range(12))
        parts.append(f"""
module blk{k} #(parameter W = {w}, parameter D = 4)(input clk, input rst_n, input [W-1:0] a, input [W-1:0] b,
    input [1:0] op, input en, output reg [W-1:0] q, output [W:0] sum, output flag);
  reg [W-1:0] r0, r1;
  reg [3:0] cnt;
  wire [W-1:0] mulx = a[W/2-1:0] * b[W/2-1:0];
  assign sum = a + b;
  assign flag = (r0 == r1) && (cnt == D);
  function automatic [W-1:0] pick(input [1:0] o, input [W-1:0] x, input [W-1:0] y);
    case (o)
      2'd0: pick = x & y;
      2'd1: pick = x | y;
      2'd2: pick = x ^ y;
      default: pick = x - y;
    endcase
  endfunction
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin r0 <= 0; r1 <= 0; cnt <= 0; q <= 0; end
    else begin
      if (en) begin
        r0 <= pick(op, a, b);
        r1 <= r0 + mulx;
        cnt <= (cnt == D) ? 4'd0 : cnt + 1;
      end
      q <= r1 ^ {{{{(W/2){{cnt[0]}}}}, r0[W/2-1:0]}};
    end
  end
  genvar i;
  generate for (i = 0; i < 4; i = i + 1) begin : g
    wire [W-1:0] t = a ^ (b << i);
  end endgenerate
{pads}
endmodule
""")
    parts.append("module top(input clk, input rst_n, input [31:0] a, b, input [1:0] op, input en, output [31:0] q);\n")
    for k in range(n_modules):
        parts.append(f"  wire [31:0] q{k}; wire [32:0] s{k}; wire f{k};\n"
                     f"  blk{k} #(.W({rnd.choice([8, 16, 32])})) u{k}(.clk(clk), .rst_n(rst_n), .a(a[7:0]+{k}), "
                     f".b(b), .op(op), .en(en), .q(q{k}), .sum(s{k}), .flag(f{k}));\n")
    parts.append("  assign q = " + " ^ ".join(f"q{k}" for k in range(n_modules)) + ";\nendmodule\n")
    return "".join(parts)


class Speed(unittest.TestCase):
    def test_generated_design(self):
        src = generate(120)
        lines = src.count("\n")
        t0 = time.perf_counter()
        r = elab(src)
        dt = time.perf_counter() - t0
        self.assertGreater(lines, 5000)
        self.assertEqual([d for d in r.diagnostics if d.affects_cost], [])
        self.assertEqual(len(r.design.modules), 121)
        self.assertLess(dt, 20.0, f"{lines} lines took {dt:.1f}s")


if __name__ == "__main__":
    unittest.main()
