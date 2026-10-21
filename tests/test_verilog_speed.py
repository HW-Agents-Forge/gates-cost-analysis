"""Speed check: FRONTEND.md section 8 targets 1 MB/s of lexing + parsing in CPython.

The assertion floor is deliberately lower than the target so the test is not flaky on loaded CI
machines; the measured figures are printed (run with ``-v`` to see them).
"""
import sys
import time
import unittest

from gatecost.frontend.common import source_from_text
from gatecost.frontend.verilog import parse_text
from gatecost.frontend.verilog.lexer import lex
from gatecost.frontend.verilog.preproc import Preprocessor

BLOCK = """
// block {i}
module blk{i} #(parameter W = 16, parameter D = 4) (
  input  wire clk, input wire rst_n,
  input  wire [W-1:0] a, input wire [W-1:0] b,
  output reg  [W-1:0] q, output wire [W:0] s
);
  reg [W-1:0] r0, r1;
  wire [W-1:0] t = (a & b) | (a ^ {{W{{1'b0}}}});
  assign s = {{1'b0, a}} + {{1'b0, b}};
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      r0 <= {{W{{1'b0}}}};
      r1 <= 'd0;
    end else begin
      r0 <= a + b;
      r1 <= r0 > b ? r0 - b : b - r0;
      case (a[1:0])
        2'b00: q <= r0;
        2'b01: q <= r1;
        2'b10: q <= t;
        default: q <= {{W{{1'bx}}}};
      endcase
    end
  end
  genvar g;
  generate
    for (g = 0; g < D; g = g + 1) begin : lanes
      sub #(.W(W)) u (.a(a[g]), .b(b[g]), .y());
    end
  endgenerate
endmodule
"""

SV_BLOCK = """
`define MASK(x) ((x) & 8'hFF)
package pkg{i};
  typedef enum logic [1:0] {{IDLE, RUN, DONE}} st_t;
endpackage
module sv{i} import pkg{i}::*; (input logic clk, input logic [7:0] d, output logic [7:0] q);
  st_t st;
  logic [7:0] mem [0:15];
  always_ff @(posedge clk) begin
    unique case (st)
      IDLE: st <= RUN;
      RUN: begin mem[d[3:0]] <= `MASK(d); st <= DONE; end
      default: st <= IDLE;
    endcase
    q <= mem[d[7:4]];
  end
endmodule
"""


class Speed(unittest.TestCase):
    def measure(self, template, n):
        text = "".join(template.format(i=i) for i in range(n))
        mb = len(text) / 1e6
        src = source_from_text(text)
        t = time.perf_counter()
        pre = Preprocessor().process(src)
        t_pre = time.perf_counter() - t
        t = time.perf_counter()
        toks = lex(pre.text)
        t_lex = time.perf_counter() - t
        t = time.perf_counter()
        d = parse_text(text)
        t_all = time.perf_counter() - t
        self.assertEqual(len(d.modules), n)
        self.assertEqual([x for x in d.diagnostics if x.severity != "info"], [])
        sys.stderr.write(f"\n[speed] {mb:.2f} MB, {len(toks)} tokens: preprocess {t_pre:.3f}s, lex {t_lex:.3f}s "
                         f"({mb / max(t_lex, 1e-9):.2f} MB/s), lex+preprocess+parse {t_all:.3f}s "
                         f"({mb / t_all:.2f} MB/s)\n")
        return mb / t_all

    def test_plain_verilog(self):
        self.assertGreater(self.measure(BLOCK, 800), 0.5)

    def test_systemverilog_with_macros(self):
        self.assertGreater(self.measure(SV_BLOCK, 1500), 0.4)


if __name__ == "__main__":
    unittest.main()
