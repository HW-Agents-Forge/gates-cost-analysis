"""Verilog gap closers: defparam forms, streaming targets, ROM inference, loops, powers."""
import unittest

from tests.elab_helpers import codes, comb, elab, ops, top_module

SUB = "module sub #(parameter W=4)(input [W-1:0] a, output [W-1:0] y); assign y=~a; endmodule\n"


class Defparam(unittest.TestCase):
    def test_hierarchical_defparam_through_child(self):
        r = elab(SUB + """
module mid(input [7:0] a, output [7:0] y); sub u(.a(a),.y(y)); endmodule
module t(input [7:0] a, output [7:0] y); mid m(.a(a), .y(y)); defparam m.u.W = 8; endmodule""", top="t")
        self.assertTrue(any(k.startswith("sub(W=8") for k in r.design.modules))
        self.assertNotIn("UNSUPPORTED_CONSTRUCT", codes(r))

    def test_defparam_with_own_module_prefix(self):
        r = elab(SUB + """
module mid(input [7:0] a, output [7:0] y); sub u(.a(a),.y(y)); endmodule
module t(input [7:0] a, output [7:0] y); mid m(.a(a), .y(y)); defparam t.m.u.W = 8; endmodule""", top="t")
        self.assertTrue(any(k.startswith("sub(W=8") for k in r.design.modules))

    def test_defparam_selects_generate_instance(self):
        r = elab(SUB + """
module t(input [7:0] a, output [15:0] y);
  genvar i;
  generate for (i = 0; i < 2; i = i + 1) begin: g sub u(.a(a), .y(y[i*8 +: 8])); end endgenerate
  defparam g[1].u.W = 8;
endmodule""", top="t")
        keys = {n.attrs["inst"]: n.attrs["module_key"] for n in top_module(r).nodes if n.op == "INST"}
        self.assertEqual(keys["g[0].u"], "sub(W=4)")
        self.assertEqual(keys["g[1].u"], "sub(W=8)")


class Streaming(unittest.TestCase):
    def test_left_to_right_target(self):
        r = elab("module t(input [15:0] a, output [7:0] y, output [7:0] z); assign {>>{y,z}} = a; endmodule")
        self.assertEqual(codes(r, True), [])
        self.assertEqual(comb(top_module(r), a=0x1234), {"y": 0x12, "z": 0x34})

    def test_byte_reversing_target(self):
        r = elab("""module t(input [15:0] a, output reg [7:0] y, output reg [7:0] z);
  always_comb {<<8{y,z}} = a; endmodule""")
        self.assertEqual(comb(top_module(r), a=0x1234), {"y": 0x34, "z": 0x12})

    def test_nibble_reversing_target_over_three_parts(self):
        r = elab("""module t(input [11:0] a, output reg [3:0] y, output reg [3:0] z, output reg [3:0] w);
  always_comb {<<4{y,z,w}} = a; endmodule""")
        self.assertEqual(comb(top_module(r), a=0x123), {"y": 3, "z": 2, "w": 1})

    def test_bit_reversing_target_splits_parts(self):
        r = elab("""module t(input [3:0] a, output reg [1:0] y, output reg [1:0] z);
  always_comb {<<{y,z}} = a; endmodule""")
        self.assertEqual(comb(top_module(r), a=0b0011), {"y": 0b11, "z": 0b00})
        self.assertEqual(comb(top_module(r), a=0b0001), {"y": 0b10, "z": 0b00})


class Roms(unittest.TestCase):
    def test_array_parameter_indexed_by_signal_is_a_rom(self):
        r = elab("""module t #(parameter int T [0:3] = '{1,2,3,4})(input [1:0] a, output [7:0] y);
  assign y = T[a]; endmodule""")
        self.assertEqual(ops(top_module(r)).get("ROM"), 1)
        self.assertEqual([comb(top_module(r), a=i)["y"] for i in range(4)], [1, 2, 3, 4])

    def test_localparam_array_constant_index_is_constant(self):
        r = elab("""module t(input [1:0] a, output [7:0] y, output [7:0] z);
  localparam logic [7:0] T [0:3] = '{8'h10, 8'h20, 8'h30, 8'h40};
  assign y = T[a]; assign z = T[3]; endmodule""")
        m = top_module(r)
        self.assertEqual(comb(m, a=2), {"y": 0x30, "z": 0x40})

    def test_case_table_is_a_rom(self):
        r = elab("""module t(input [2:0] a, output reg [7:0] y);
  always_comb case (a) 0: y = 8'h10; 1: y = 8'h21; 2: y = 8'h33; 3: y = 5; 4: y = 6; 5: y = 7; 6: y = 8;
    default: y = 8'h44; endcase
endmodule""")
        m = top_module(r)
        self.assertEqual(ops(m), {"ROM": 1})
        rom = [n for n in m.nodes if n.op == "ROM"][0]
        self.assertEqual(rom.attrs["contents"], (0x10, 0x21, 0x33, 5, 6, 7, 8, 0x44))

    def test_registered_case_table_is_rom_plus_register(self):
        r = elab("""module t(input clk, input [2:0] a, output reg [7:0] y);
  always @(posedge clk) case (a) 0: y <= 1; 1: y <= 2; 2: y <= 3; 3: y <= 4; 4: y <= 5; 5: y <= 6; 6: y <= 7; 7: y <= 9;
  endcase
endmodule""")
        self.assertEqual(ops(top_module(r)), {"ROM": 1, "DFF": 1})

    def test_small_or_computed_case_stays_a_mux(self):
        r = elab("""module t(input [2:0] a, input b, output reg [7:0] y);
  always_comb case (a) 0: y = 1; 1: y = b; default: y = 3; endcase
endmodule""")
        self.assertNotIn("ROM", ops(top_module(r)))


class LoopsAndPowers(unittest.TestCase):
    def test_non_constant_bound_is_unrolled_with_guards(self):
        r = elab("""module t(input [3:0] n, input [7:0] a, output reg [11:0] y);
  integer i;
  always_comb begin y = 0; for (i = 0; i < n; i = i + 1) y = y + a; end
endmodule""")
        m = top_module(r)
        self.assertEqual([comb(m, a=3, n=k)["y"] for k in (0, 1, 5, 15)], [0, 3, 15, 45])
        self.assertTrue(any("non-constant bound" in a for a in r.assumptions))

    def test_unbounded_dynamic_loop_is_capped_with_a_diagnostic(self):
        r = elab("""module t(input [31:0] n, input [7:0] a, output reg [7:0] y);
  integer i;
  always_comb begin y = 0; for (i = 0; i < n; i = i + 1) y = y + a; end
endmodule""")
        self.assertIn("LIMIT_REACHED", codes(r, True))

    def test_power_with_variable_exponent(self):
        r = elab("""module t(input [2:0] n, input [7:0] a, output [15:0] y); assign y = a ** n; endmodule""")
        m = top_module(r)
        self.assertEqual([comb(m, a=3, n=k)["y"] for k in range(8)], [1, 3, 9, 27, 81, 243, 729, 2187])

    def test_two_to_the_n_is_a_shift(self):
        r = elab("module t(input [3:0] n, output [15:0] y); assign y = 2 ** n; endmodule")
        self.assertEqual(ops(top_module(r)), {"SHL": 1})

    def test_forever_is_reported(self):
        r = elab("module t(input clk, output reg y); always @(posedge clk) begin forever y <= 1; end endmodule")
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, True))

    def test_interface_array_is_one_clear_diagnostic(self):
        r = elab("""interface bus_if; logic [7:0] d; endinterface
module sub(bus_if b, output [7:0] y); assign y = b.d; endmodule
module t(input [7:0] x, output [7:0] y0);
  bus_if b[2]();
  sub s0(.b(b[0]), .y(y0));
endmodule""", top="t")
        self.assertTrue(any("interface array" in d.message for d in r.diagnostics))

    def test_hierarchical_reference_into_other_module_is_diagnosed(self):
        r = elab("""module sub(input a, output y); assign y = ~a; wire w = a; endmodule
module t(input a, output y); wire q; sub u(.a(a), .y(q)); assign y = u.w; endmodule""", top="t")
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, True))


if __name__ == "__main__":
    unittest.main()
