"""Regressions found by running the cost estimator over more real open-source RTL (round 2)."""
import tempfile
import unittest
from pathlib import Path

from gatecost.elab import elaborate_project


def run(text, name="m.sv"):
    with tempfile.TemporaryDirectory() as td:
        Path(td, name).write_text(text)
        return elaborate_project([Path(name)], root=Path(td))


class Round2(unittest.TestCase):
    def codes(self, text, name="m.sv"):
        return [d.code for d in run(text, name).diagnostics]

    def test_whitespace_between_size_and_cast_tick(self):
        # basejump_stl writes ``width_p ' (x)`` and ``n ' {a, b}``
        src = """module b #(parameter w=8, parameter n=4)(input [3:0] x, input [n-1:0] z, output [w-1:0] y,
  output [2*n-1:0] q, output [w-1:0] u);
  assign y = w ' (x);
  assign q = { 2 { n ' (z) } } << x[1:0];
  assign u = (w) ' (x);
endmodule"""
        codes = self.codes(src)
        self.assertNotIn("PARSE_ERROR", codes)
        self.assertEqual(codes, [])

    def test_constant_mask_and_does_not_create_false_combinational_loop(self):
        # verilog-axi axi_crossbar_addr: bit n only looks at lower bits of the same vector through a constant mask
        src = """module a #(parameter N=4)(input clk, input [N-1:0] act, input go, output [N-1:0] y);
wire [N-1:0] ts;
genvar n;
generate for (n=0;n<N;n=n+1) begin
  assign ts[n] = (act[n] || (!act[n] && !(ts & ({N{1'b1}} >> (N-n))))) && go;
end endgenerate
assign y = ts;
endmodule"""
        self.assertNotIn("COMBINATIONAL_LOOP", self.codes(src, "a.v"))

    def test_function_argument_with_typedef_packed_type(self):
        # opentitan aes_ctr: ``function automatic sp2v_e [N-1:0] f(sp2v_e [N-1:0] in);``
        src = """package p;
  typedef enum logic [1:0] {A=0, B=1} sp2v_e;
  localparam int N = 4;
endpackage
module c import p::*; (input sp2v_e [N-1:0] a, output sp2v_e [N-1:0] y);
  function automatic sp2v_e [N-1:0] rev(sp2v_e [N-1:0] in);
    sp2v_e [N-1:0] out;
    for (int i = 0; i < N; i++) begin
      out[i] = in[N-1-i];
    end
    return out;
  endfunction
  assign y = rev(a);
endmodule"""
        codes = self.codes(src)
        self.assertNotIn("PARSE_ERROR", codes)
        self.assertNotIn("UNRESOLVED_NAME", codes)

    def test_prose_comment_mentioning_translate_off_is_not_a_pragma(self):
        # opentitan hmac: "// To pass FPV, this shouldn't add pragma translate_off even ..."
        src = """module m(input a, output y);
  // this shouldn't add pragma translate_off even for these signals
  assign y = ~a;
endmodule"""
        codes = self.codes(src)
        self.assertNotIn("IGNORED_REGION", codes)
        self.assertNotIn("PARSE_ERROR", codes)

    def test_real_translate_off_region_still_excluded(self):
        src = """module m(input a, output y);
  // synopsys translate_off
  initial $display("x");
  // synopsys translate_on
  assign y = ~a;
endmodule"""
        self.assertIn("IGNORED_REGION", self.codes(src))

    def test_void_cast_statement(self):
        # ibex_tracer: ``void'($value$plusargs(...));`` / ``void'(f(x));`` as a statement
        src = """module d(input clk, input [3:0] a, output logic [3:0] y);
  function automatic logic [3:0] f(input logic [3:0] x);
    return x + 1;
  endfunction
  always_ff @(posedge clk) begin
    void'(f(a));
    void'($urandom);
    y <= f(a);
  end
endmodule"""
        self.assertNotIn("PARSE_ERROR", self.codes(src))

    def test_bits_of_port_struct_member_in_localparam(self):
        # opentitan tlul_err / uart_core: ``localparam int IW = $bits(tl_i.a_source);``
        src = """package pk;
  typedef struct packed { logic [5:0] a_source; logic [31:0] a_data; logic v; } h_t;
  typedef struct packed { logic [3:0] q; logic qe; } f_t;
  typedef struct packed { f_t ctrl; } r2h_t;
endpackage
module e import pk::*; (input h_t tl_i, input r2h_t reg2hw, output logic [11:0] y);
  localparam int IW = $bits(tl_i.a_source);
  localparam int NW = $bits(reg2hw.ctrl.q);
  logic [IW-1:0] s;
  assign s = tl_i.a_source;
  assign y = {s & {IW{tl_i.v}}, reg2hw.ctrl.q ^ NW[3:0] , 2'b0};
endmodule"""
        res = run(src)
        self.assertEqual([d.code for d in res.diagnostics], [])

    def test_dynamic_write_into_packed_2d_array_is_linear_in_depth(self):
        # opentitan prim_fifo_sync: ``logic [Depth-1:0][Width-1:0] storage; storage[wptr] <= wdata;``
        # every case arm used to span the whole net, making the cost quadratic (237k GE for 128x8)
        from gatecost.cost import build_cost_tree, load_profile
        src = """module f #(parameter D=4, parameter W=8, parameter P=2)(input clk, input we, input [P-1:0] wp,
  input [P-1:0] rp, input [W-1:0] wd, output [W-1:0] rd);
  logic [D-1:0][W-1:0] storage;
  always_ff @(posedge clk) if (we) storage[wp] <= wd;
  assign rd = storage[rp];
endmodule"""
        costs = {}
        for d, pw in ((16, 4), (64, 6)):
            res = run(src.replace("D=4", f"D={d}").replace("P=2", f"P={pw}"))
            self.assertEqual([x.code for x in res.diagnostics], [])
            costs[d] = build_cost_tree(res.design, load_profile()).total().typ
        self.assertLess(costs[64], 5 * costs[16])      # ~4x for linear, ~16x for quadratic


if __name__ == "__main__":
    unittest.main()
