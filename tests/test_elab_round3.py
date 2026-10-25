"""Regressions found by stressing the estimator on larger designs (round 3)."""
import tempfile
import unittest
from pathlib import Path

from gatecost.elab import elaborate_project


def run(text, name="m.sv"):
    with tempfile.TemporaryDirectory() as td:
        Path(td, name).write_text(text)
        return elaborate_project([Path(name)], root=Path(td))


class Round3(unittest.TestCase):
    def codes(self, text, name="m.sv"):
        return [d.code for d in run(text, name).diagnostics]

    def test_case_item_colon_directly_before_line_comment(self):
        # fpnew/opene906 ``3'b000://round to nearest``: ``:/`` is not the ``:/`` dist operator here
        src = """module r(input [2:0] rm, input a, b, output reg y);
always @* begin
  case (rm)
  3'b000://round to nearest
  begin
    y = a;
  end
  3'b001:/* block comment */
    y = b;
  default: y = 1'b0;
  endcase
end
endmodule"""
        codes = self.codes(src, "r.v")
        self.assertNotIn("PARSE_ERROR", codes)

    def test_memory_write_enable_survives_discard_of_shared_logic(self):
        # cva6 store_buffer: array-of-struct written field by field under nested conditions; the
        # write-enable logic is shared with a register run that is garbage collected before the
        # memory node is emitted ("IR invariant violated ... MEM.w0_en")
        src = """module sb (
  input logic clk_i, rst_ni, commit_i, gnt_i, rvalid_i, stall_i,
  input logic [31:0] data_i,
  output logic [31:0] o
);
  struct packed {
    logic [31:0] data;
    logic [1:0] cbo_op;
    logic valid;
    logic wait_rvalid;
  } cq_n[3:0], cq_q[3:0];
  logic [1:0] crp_q, crp_n, cwp_q, cwp_n;
  always_comb begin
    cq_n = cq_q;
    crp_n = crp_q;
    cwp_n = cwp_q;
    if (cq_q[crp_q].valid && !stall_i && !cq_q[crp_q].wait_rvalid) begin
      if (gnt_i) begin
        if (cq_q[crp_q].cbo_op == 0 || rvalid_i) begin
          cq_n[crp_q].valid = 1'b0;
          crp_n = crp_q + 1'b1;
        end else if (cq_q[crp_q].cbo_op != 0) begin
          cq_n[crp_q].wait_rvalid = 1'b1;
        end
      end
    end
    if (cq_q[crp_q].valid && cq_q[crp_q].wait_rvalid) begin
      if (rvalid_i) begin
        cq_n[crp_q].valid = 1'b0;
        crp_n = crp_q + 1'b1;
      end
    end
    if (commit_i) begin
      cq_n[cwp_q].data = data_i;
      cq_n[cwp_q].valid = 1'b1;
      cwp_n = cwp_n + 1'b1;
    end
  end
  assign o = cq_q[crp_q].data;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      cq_q <= '{default: 0};
      cwp_q <= 0; crp_q <= 0;
    end else begin
      cq_q <= cq_n; cwp_q <= cwp_n; crp_q <= crp_n;
    end
  end
endmodule"""
        self.assertNotIn("INTERNAL", self.codes(src))

    def test_diff_runs_matches_bitwise_comparison(self):
        from gatecost.elab.expr import _diff_runs
        from gatecost.ir import SigSpec
        a = SigSpec.const(0b1011_0110, 8)
        b = SigSpec.const(0b1011_0011, 8)
        runs = _diff_runs(a, b, 8)
        bits = [x != y for x, y in zip(a.bits(), b.bits())]
        flat = [d for i, j, d in runs for _ in range(j - i)]
        self.assertEqual(flat, bits)


if __name__ == "__main__":
    unittest.main()
