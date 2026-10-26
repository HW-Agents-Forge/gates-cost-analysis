"""Elaborator: symbol resolution, declaration styles, procedural odds and ends, degradation."""
import unittest

from gatecost.errors import GatecostError
from tests.elab_helpers import codes, comb, elab, ops, top_module


class Symbols(unittest.TestCase):
    def test_implicit_nets_follow_default_nettype(self):
        r = elab("""
module sub(input a, output y); assign y = ~a; endmodule
module top(input a, output y, output y2);
  assign w = a;
  sub u(.a(w), .y(y));
  sub u2(.a(a), .y(w2));
  assign y2 = w2;
endmodule""", top="top")
        self.assertEqual(codes(r, True), [])
        m = top_module(r)
        self.assertEqual(sorted(n.attrs["inst"] for n in m.nodes if n.op == "INST"), ["u", "u2"])
        r2 = elab("""
`default_nettype none
module top(input a, output y);
  assign w = a;
  assign y = w;
endmodule
`default_nettype wire""")
        self.assertIn("UNRESOLVED_NAME", codes(r2, True))

    def test_undeclared_identifier_in_expression_is_flagged_and_assumed_32_bits(self):
        r = elab("module t(output [31:0] y); assign y = nope + 1; endmodule")
        self.assertIn("UNRESOLVED_NAME", codes(r, True))
        self.assertTrue(any("nope" in a for a in r.assumptions))

    def test_non_ansi_ports_with_separate_reg_declarations(self):
        r = elab("""
module t(a, b, q, y);
  parameter W = 6;
  input [W-1:0] a, b;
  input signed [3:0] s;
  output q;
  output [W-1:0] y;
  reg q;
  reg [W-1:0] y;
  always @* begin q = &a; y = a ^ b; end
endmodule""")
        m = top_module(r)
        self.assertEqual([(p.name, p.port, p.width) for p in m.ports],
                         [("a", "in", 6), ("b", "in", 6), ("q", "out", 1), ("y", "out", 6)])
        self.assertEqual(comb(m, a=0b110011, b=0b000111)["y"], 0b110100)

    def test_hierarchical_reference_into_generate_block(self):
        r = elab("""
module t(input [3:0] a, output [3:0] y);
  genvar i;
  generate for (i = 0; i < 4; i = i + 1) begin : g
    wire w = ~a[i];
  end endgenerate
  assign y = {g[3].w, g[2].w, g[1].w, g[0].w};
endmodule""")
        self.assertEqual(comb(top_module(r), a=0b1010)["y"], 0b0101)

    def test_wait_style_always_block(self):
        r = elab("""
module t(input clk, input [3:0] d, output reg [3:0] q);
  always begin @(posedge clk); q <= d; end
endmodule""")
        self.assertEqual(ops(top_module(r)), {"DFF": 1})

    def test_always_without_event_is_ignored(self):
        r = elab("""
module t(input a, output reg y);
  always #5 y = ~y;
  initial y = 0;
endmodule""")
        self.assertEqual(ops(top_module(r)), {})
        self.assertNotIn("UNSUPPORTED_CONSTRUCT", codes(r, True))

    def test_concatenation_lvalue_and_compound_assignments(self):
        r = elab("""
module t(input [3:0] a, b, output reg c, output reg [3:0] s, output reg [3:0] t);
  always @* begin
    {c, s} = a + b;
    t = a; t += b; t -= 1; t *= 2; t ^= 4'hF; t <<= 1; t >>= 1;
  end
endmodule""")
        m = top_module(r)
        for a in range(16):
            for b in (0, 3, 9):
                o = comb(m, a=a, b=b)
                self.assertEqual((o["c"], o["s"]), ((a + b) >> 4, (a + b) & 15))
                t = ((((a + b - 1) * 2) & 15) ^ 15)
                t = ((t << 1) & 15) >> 1
                self.assertEqual(o["t"], t)

    def test_real_numbers_are_flagged(self):
        r = elab("module t(output [7:0] y); assign y = 1.5; endmodule")
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, True))

    def test_errors_never_abort_the_analysis(self):
        r = elab("""
module good(input a, output y); assign y = ~a; endmodule
module bad(input a, output y);
  assign y = a +;
  always @(posedge ) y <= a
endmodule
module top(input a, output y, z);
  good g(.a(a), .y(y));
  bad  b(.a(a), .y(z));
endmodule""", top="top")
        self.assertIn("PARSE_ERROR", codes(r))
        self.assertEqual(sorted(r.design.keys()), ["bad", "good", "top"])
        self.assertEqual(ops(r.design.modules["good"]), {"NOT": 1})

    def test_macro_and_include_limits(self):
        r = elab("`define A1 `A2\n`define A2 `A3\n`define A3 `A4\n`define A4 1\n"
                 "module t(output y); assign y = `A1; endmodule", limits={"macro_depth": 2})
        self.assertIn("LIMIT_REACHED", codes(r))
        r = elab({"t.sv": '`include "a.vh"\nmodule t(output y); assign y = 1; endmodule',
                  "a.vh": '`include "b.vh"\n', "b.vh": '`include "c.vh"\n', "c.vh": "`define X 1\n"},
                 limits={"include_depth": 2})
        self.assertIn("LIMIT_REACHED", codes(r))

    def test_parser_expression_depth_limit(self):
        e = "(" * 300 + "a" + ")" * 300
        r = elab(f"module t(input a, output y); assign y = {e}; endmodule", limits={"expr_depth": 100})
        self.assertIn("PARSE_ERROR", codes(r))
        self.assertTrue(r.design.keys())

    def test_diagnostics_carry_side_and_relative_locations(self):
        r = elab({"rtl/a.sv": "module t(output y); assign y = nope; endmodule"}, side="before")
        d = [x for x in r.diagnostics if x.code == "UNRESOLVED_NAME"][0]
        self.assertEqual((d.side, d.file, d.line), ("before", "rtl/a.sv", 1))
        m = top_module(r)
        self.assertEqual(m.nodes[0].origin.file if m.nodes else "rtl/a.sv", "rtl/a.sv")

    def test_macro_use_site_is_the_origin(self):
        r = elab("`define ADD(x, y) ((x) + (y))\nmodule t(input [3:0] a, b, output [3:0] y);\n"
                 "  assign y = `ADD(a, b);\nendmodule")
        add = [n for n in top_module(r).nodes if n.op == "ADD"][0]
        self.assertEqual(add.origin.line, 3)
        self.assertEqual(add.origin.def_line, 1)

    def test_missing_include_and_package_are_diagnostics_not_failures(self):
        r = elab('`include "nope.vh"\nmodule t import nopkg::*; (output y); assign y = nopkg::X; endmodule')
        self.assertIn("INCLUDE_NOT_FOUND", codes(r))
        self.assertIn("MISSING_UNIT", codes(r, True))

    def test_do_while_case_inside_pattern_default_and_inout_task(self):
        r = elab("""
module t(input [7:0] a, input [2:0] s, output reg [7:0] y, output reg [3:0] z, output reg [7:0] w, output reg [7:0] u, output [7:0] pa);
  const int K = 3;
  logic [3:0][7:0] arr;
  assign arr = '{default: 8'h11};
  assign pa = arr[s[1:0]];
  integer i;
  task automatic swap(inout [7:0] p, inout [7:0] q); reg [7:0] t; begin t = p; p = q; q = t; end endtask
  always_comb begin
    y = 0; i = 0;
    do begin y = y + a; i = i + 1; end while (i < K);
    case (s) inside
      [0:1]: z = 1;
      3'd5, 3'd6: z = 2;
      default: z = 3;
    endcase
    w = a; u = 8'hF0;
    swap(w, u);
  end
endmodule""")
        m = top_module(r)
        self.assertEqual(codes(r, True), [])
        for a in (0, 1, 17, 200):
            for s in range(8):
                o = comb(m, a=a, s=s)
                self.assertEqual((o["y"], o["z"], o["w"], o["u"], o["pa"]),
                                 ((3 * a) & 255, 1 if s <= 1 else 2 if s in (5, 6) else 3, 0xF0, a, 0x11))

    def test_struct_arrays_enums_and_functions_returning_structs(self):
        r = elab("""
package p;
  typedef enum logic [1:0] {RED, GREEN = 2, BLUE} color_t;
  typedef struct packed { logic [3:0] a; color_t c; } rec_t;
endpackage
module t(input [3:0] x, input [1:0] i, output [3:0] y, output [1:0] cc, output [7:0] bits, output [5:0] y2, output [3:0] y3);
  import p::*;
  rec_t arr [0:3];
  rec_t [1:0] pa;
  function rec_t mk(input [3:0] a, input color_t c); mk.a = a; mk.c = c; endfunction
  always_comb begin
    for (int k = 0; k < 4; k++) arr[k] = mk(x + k, k == 2 ? BLUE : GREEN);
    pa[0] = mk(x, RED);
    pa[1] = '{a: ~x, c: color_t'(2'd1)};
  end
  assign y = arr[i].a;
  assign cc = arr[i].c;
  assign bits = $bits(rec_t) + $bits(color_t) * 16 + p::BLUE * 64;
  assign y2 = pa[1];
  assign y3 = pa[0].a;
endmodule""")
        m = top_module(r)
        self.assertNotIn("MEM", ops(m))
        for x in range(16):
            for i in range(4):
                o = comb(m, x=x, i=i)
                self.assertEqual((o["y"], o["cc"], o["bits"], o["y2"], o["y3"]),
                                 ((x + i) & 15, 3 if i == 2 else 2, (6 + 2 * 16 + 3 * 64) & 255,
                                  ((~x & 15) << 2) | 1, x))


if __name__ == "__main__":
    unittest.main()
