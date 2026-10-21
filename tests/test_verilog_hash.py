"""Normalized-AST hash: presentation invariance (FRONTEND.md section 5)."""
import unittest

from gatecost.frontend.verilog import parse_text

BASE = """
module top #(parameter W = 8) (input clk, input [W-1:0] a, output reg [W-1:0] q);
  wire [W-1:0] t;
  reg [W-1:0] r;
  localparam K = 3;
  assign t = a & r;
  always @(posedge clk) q <= t + K;
  sub #(.W(W), .D(2)) u (.clk(clk), .a(a), .y(r));
endmodule
"""


def h(text, name="top"):
    d = parse_text(text)
    assert not [x for x in d.diagnostics if x.severity != "info"], d.diagnostics
    return d.module_map()[name].digest()


class ModuleHash(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(h(BASE), h(BASE))
        self.assertEqual(len(h(BASE)), 64)

    def test_whitespace_and_comments(self):
        reformatted = """
// header comment
module top #( parameter W = 8 )
(
    input clk ,
    input [ W - 1 : 0 ] a , /* inline */
    output reg [W-1:0] q
);

    wire [W-1:0]   t ;   // temp
    reg  [W-1:0] r;
    localparam   K   =   3 ;
    assign t =
        a & r;
    always @( posedge clk )
        q <=
           t + K ;
    sub #( .W( W ), .D( 2 ) ) u ( .clk( clk ), .a( a ), .y( r ) );
endmodule
"""
        self.assertEqual(h(BASE), h(reformatted))

    def test_independent_declaration_order(self):
        reordered = BASE.replace("  wire [W-1:0] t;\n  reg [W-1:0] r;\n  localparam K = 3;\n",
                                 "  localparam K = 3;\n  reg [W-1:0] r;\n  wire [W-1:0] t;\n")
        self.assertNotEqual(reordered, BASE)
        self.assertEqual(h(BASE), h(reordered))

    def test_named_connection_and_override_order(self):
        reordered = BASE.replace(".W(W), .D(2)", ".D(2), .W(W)").replace(".clk(clk), .a(a), .y(r)", ".y(r), .a(a), .clk(clk)")
        self.assertEqual(h(BASE), h(reordered))

    def test_number_spelling(self):
        a = "module top; localparam A = 8'hFF; endmodule"
        b = "module top; localparam A = 8 'hff; endmodule"
        c = "module top; localparam A = 8'hFE; endmodule"
        self.assertEqual(h(a), h(b))
        self.assertNotEqual(h(a), h(c))

    def test_macro_expansion_is_hashed_not_text(self):
        a = "`define X 3\nmodule top; localparam A = `X; endmodule"
        b = "module top; localparam A = 3; endmodule"
        self.assertEqual(h(a), h(b))

    def test_semantic_changes_alter_hash(self):
        base = h(BASE)
        for old, new in (("t + K", "t - K"), ("K = 3", "K = 4"), ("[W-1:0] r", "[W:0] r"),
                         ("posedge clk", "negedge clk"), ("a & r", "a | r"), (".D(2)", ".D(3)"),
                         ("output reg", "output"), ("parameter W = 8", "parameter W = 16"), ("sub #", "sub2 #")):
            self.assertNotEqual(base, h(BASE.replace(old, new)), old)

    def test_concurrent_statement_order_is_preserved(self):
        a = "module top; assign x = 1; assign y = 2; endmodule"
        b = "module top; assign y = 2; assign x = 1; endmodule"
        self.assertNotEqual(h(a), h(b))

    def test_rename_changes_hash(self):
        self.assertNotEqual(h(BASE), h(BASE.replace("module top", "module top2"), "top2"))

    def test_hash_independent_of_file_position_and_other_modules(self):
        d = parse_text("module pad; endmodule\n\n\n" + BASE + "\nmodule tail; wire z; endmodule\n")
        self.assertEqual(d.module_map()["top"].digest(), h(BASE))

    def test_per_module_hash_isolation(self):
        d1 = parse_text(BASE + "module other; wire a; endmodule")
        d2 = parse_text(BASE + "module other; wire b; endmodule")
        m1, m2 = d1.module_map(), d2.module_map()
        self.assertEqual(m1["top"].digest(), m2["top"].digest())
        self.assertNotEqual(m1["other"].digest(), m2["other"].digest())

    def test_incomplete_module_differs_from_fixed(self):
        broken = BASE.replace("q <= t + K;", "q <= ;")
        d = parse_text(broken)
        self.assertTrue(d.modules[0].incomplete)
        self.assertNotEqual(d.modules[0].digest(), h(BASE))

    def test_packages_and_interfaces_have_digests(self):
        d = parse_text("package p; localparam A = 1; localparam B = 2; endpackage\n"
                       "interface i; logic a; endinterface\n")
        self.assertEqual(len(d.packages[0].digest()), 64)
        self.assertEqual(len(d.interfaces[0].digest()), 64)
        d2 = parse_text("package p; localparam B = 2; localparam A = 1; endpackage\n")
        self.assertEqual(d.packages[0].digest(), d2.packages[0].digest())

    def test_generate_block_declaration_order(self):
        a = "module top; for (genvar i=0;i<2;i++) begin : g wire a; wire b; assign a = b; end endmodule"
        b = "module top; for (genvar i=0;i<2;i++) begin : g wire b; wire a; assign a = b; end endmodule"
        self.assertEqual(h(a), h(b))


if __name__ == "__main__":
    unittest.main()
