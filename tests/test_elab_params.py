"""Elaborator: parameters, constant expressions, generate, types, packages."""
import unittest

from tests.elab_helpers import codes, comb, elab, ops, top_module


class Params(unittest.TestCase):
    def test_override_named_and_positional(self):
        r = elab("""
module child #(parameter W = 4, parameter D = 2)(input [W-1:0] a, output [W-1:0] y);
  assign y = a + D;
endmodule
module top(input [7:0] a, output [7:0] y1, output [3:0] y2);
  child #(.W(8), .D(5)) c1(.a(a), .y(y1));
  child #(4) c2(.a(a[3:0]), .y(y2));
endmodule""", top="top")
        keys = r.design.keys()
        self.assertIn("child(D=5,W=8)", keys)
        self.assertIn("child(D=2,W=4)", keys)
        self.assertEqual(codes(r), [])
        self.assertEqual(r.design.modules["child(D=5,W=8)"].port("y").width, 8)
        self.assertEqual(r.design.modules["child(D=2,W=4)"].port("y").width, 4)

    def test_default_parameter_chain_and_localparam(self):
        r = elab("""
module top #(parameter A = 3, parameter B = A * 2)(output [B-1:0] y);
  localparam C = B + 1;
  assign y = C;
endmodule""")
        m = top_module(r)
        self.assertEqual(m.port("y").width, 6)
        self.assertEqual(comb(m)["y"], 7)

    def test_identical_instances_share_one_module(self):
        r = elab("""
module leaf #(parameter W=4)(input [W-1:0] a, output [W-1:0] y); assign y = ~a; endmodule
module top(input [3:0] a, output [3:0] y, output [3:0] z);
  leaf u0(.a(a), .y(y));
  leaf u1(.a(a), .y(z));
endmodule""", top="top")
        self.assertEqual(sorted(r.design.keys()), ["leaf(W=4)", "top"])
        self.assertEqual(r.design.instance_counts()["leaf(W=4)"], 2)

    def test_defparam_simple_path(self):
        r = elab("""
module leaf #(parameter W=4)(input [W-1:0] a, output [W-1:0] y); assign y = a; endmodule
module top(input [7:0] a, output [7:0] y);
  leaf u(.a(a), .y(y));
  defparam u.W = 8;
endmodule""", top="top")
        self.assertIn("leaf(W=8)", r.design.keys())

    def test_type_parameter(self):
        r = elab("""
module leaf #(parameter type T = logic [3:0])(input T a, output T y); assign y = a + 1; endmodule
module top(input [9:0] a, output [9:0] y);
  leaf #(.T(logic [9:0])) u(.a(a), .y(y));
endmodule""", top="top")
        key = [k for k in r.design.keys() if k.startswith("leaf")][0]
        self.assertEqual(r.design.modules[key].port("y").width, 10)

    def test_unresolved_parameter_defaults_to_32_bits_and_is_flagged(self):
        r = elab("""
module top #(parameter W)(output [W-1:0] y); assign y = 1; endmodule""")
        self.assertIn("UNRESOLVED_PARAM", codes(r, True))
        self.assertEqual(top_module(r).port("y").width, 32)
        self.assertTrue(any("without value" in a for a in r.assumptions))

    def test_constant_functions_and_system_functions(self):
        r = elab("""
module top(output [7:0] y0, output [7:0] y1, output [7:0] y2, output [7:0] y3);
  function automatic integer fact(input integer n);
    if (n <= 1) return 1;
    else return n * fact(n - 1);
  endfunction
  function integer lg(input integer v); integer i; begin lg = 0; for (i = v - 1; i > 0; i = i >> 1) lg = lg + 1; end endfunction
  localparam F = fact(5);
  localparam L = lg(100);
  assign y0 = F;
  assign y1 = L + $clog2(33) * 16;
  assign y2 = $bits(y0) + $max(3, 9) - $min(4, 2);
  assign y3 = $signed(8'hff) >>> 1;
endmodule""")
        self.assertEqual(codes(r), [])
        o = comb(top_module(r))
        self.assertEqual((o["y0"], o["y1"], o["y2"], o["y3"]), (120, 7 + 6 * 16, 8 + 9 - 2, 0xFF))

    def test_constant_function_step_limit(self):
        r = elab("""
module top(output [7:0] y);
  function integer spin(input integer n); integer i; begin spin = 0; for (i = 0; i < 100000000; i = i + 1) spin = spin + 1; end endfunction
  localparam P = spin(1);
  assign y = P;
endmodule""", limits={"const_func_steps": 500, "loop_unroll": 100000000})
        self.assertIn("LIMIT_REACHED", codes(r))

    def test_package_import_and_scoped_names(self):
        r = elab("""
package p;
  localparam int W = 6;
  parameter logic [3:0] K = 4'd9;
  function automatic int twice(int x); return 2 * x; endfunction
endpackage
module top import p::*; (output [W-1:0] y, output [7:0] z, output [3:0] k);
  assign y = 0;
  assign z = p::twice(21);
  assign k = p::K;
endmodule""")
        m = top_module(r)
        self.assertEqual(m.port("y").width, 6)
        o = comb(m)
        self.assertEqual((o["z"], o["k"]), (42, 9))

    def test_enum_struct_typedef(self):
        r = elab("""
module top(input [7:0] d, output [3:0] hi, output [3:0] lo, output [1:0] st);
  typedef enum logic [1:0] {A, B = 2, C} st_t;
  typedef struct packed { logic [3:0] hi; logic [3:0] lo; } byte_t;
  byte_t v;
  st_t s;
  always_comb begin v = d; s = C; end
  assign hi = v.hi; assign lo = v.lo; assign st = s;
endmodule""")
        o = comb(top_module(r), d=0xA5)
        self.assertEqual((o["hi"], o["lo"], o["st"]), (0xA, 0x5, 3))

    def test_enum_without_base_uses_minimum_width(self):
        r = elab("""
module top(output [7:0] y);
  typedef enum {IDLE, RUN, DONE, ERR, X5} st_t;
  st_t s;
  assign s = X5;
  assign y = $bits(s);
endmodule""")
        self.assertEqual(comb(top_module(r))["y"], 3)


class Generate(unittest.TestCase):
    def test_nested_generate_for_if_case_and_names(self):
        r = elab("""
module leaf(input a, output y); assign y = ~a; endmodule
module top #(parameter N = 3)(input [N*2-1:0] a, output [N*2-1:0] y);
  genvar i, j;
  generate
    for (i = 0; i < N; i = i + 1) begin : outer
      for (j = 0; j < 2; j = j + 1) begin : inner
        if ((i + j) % 2 == 0) begin : even
          leaf u (.a(a[i*2+j]), .y(y[i*2+j]));
        end else begin : odd
          assign y[i*2+j] = a[i*2+j];
        end
      end
    end
  endgenerate
endmodule""", top="top")
        m = top_module(r)
        names = sorted(n.attrs["inst"] for n in m.nodes if n.op == "INST")
        self.assertEqual(names, ["outer[0].inner[0].even.u", "outer[1].inner[1].even.u", "outer[2].inner[0].even.u"])
        o = comb(m, a=0b101101)
        # leaf inverts bits 0, 3, 4 ; others pass through
        self.assertEqual(r.design.instance_counts()["leaf"], 3)

    def test_generate_case_selects_branch(self):
        for mode, expect in ((0, 5), (1, 6), (7, 9)):
            r = elab(f"""
module top #(parameter M = {mode})(output [7:0] y);
  generate case (M)
    0: assign y = 5;
    1: assign y = 6;
    default: assign y = 9;
  endcase endgenerate
endmodule""")
            self.assertEqual(comb(top_module(r))["y"], expect)

    def test_generate_for_builds_adder_chain(self):
        r = elab("""
module top #(parameter N = 4)(input [N-1:0] a, b, output [N:0] s);
  wire [N:0] c;
  assign c[0] = 1'b0;
  genvar i;
  generate for (i = 0; i < N; i = i + 1) begin : g
    assign s[i] = a[i] ^ b[i] ^ c[i];
    assign c[i+1] = (a[i] & b[i]) | (c[i] & (a[i] ^ b[i]));
  end endgenerate
  assign s[N] = c[N];
endmodule""")
        m = top_module(r)
        for a in range(16):
            for b in range(16):
                self.assertEqual(comb(m, a=a, b=b)["s"], a + b)

    def test_generate_iteration_limit(self):
        r = elab("""
module top(output [7:0] y);
  genvar i;
  generate for (i = 0; i < 100; i = i + 1) begin : g
    wire w = 1'b1;
  end endgenerate
  assign y = 0;
endmodule""", limits={"generate_iterations": 10})
        self.assertIn("LIMIT_REACHED", codes(r, True))


if __name__ == "__main__":
    unittest.main()
