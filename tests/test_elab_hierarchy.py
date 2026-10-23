"""Elaborator: hierarchy, sharing, constant-port propagation, black boxes, tops, only_modules, limits."""
import unittest

from gatecost.errors import GatecostError
from tests.elab_helpers import codes, comb, elab, ops, top_module

ALU = """
module alu(input [7:0] a, b, input [1:0] op, output reg [7:0] y);
  always @* case (op)
    0: y = a + b;
    1: y = a - b;
    2: y = a & b;
    default: y = a | b;
  endcase
endmodule
"""


class Hierarchy(unittest.TestCase):
    def test_constant_port_propagation_uniquifies_and_folds(self):
        r = elab(ALU + """
module top(input [7:0] a, b, input [1:0] op, output [7:0] y0, y1, y2);
  alu u0(.a(a), .b(b), .op(2'd0), .y(y0));
  alu u1(.a(a), .b(b), .op(2'd1), .y(y1));
  alu u2(.a(a), .b(b), .op(op),   .y(y2));
endmodule""", top="top")
        keys = r.design.keys()
        self.assertEqual(sorted(keys), ["alu", "alu[op=2'h0]", "alu[op=2'h1]", "top"])
        self.assertEqual(ops(r.design.modules["alu[op=2'h0]"]), {"ADD": 1})
        self.assertEqual(ops(r.design.modules["alu[op=2'h1]"]), {"SUB": 1})
        self.assertEqual(ops(r.design.modules["alu"]).get("PMUX"), 1)

    def test_constant_port_propagation_can_be_disabled(self):
        r = elab(ALU + """
module top(input [7:0] a, b, output [7:0] y0, y1);
  alu u0(.a(a), .b(b), .op(2'd0), .y(y0));
  alu u1(.a(a), .b(b), .op(2'd1), .y(y1));
endmodule""", top="top", propagate_const_ports=False)
        self.assertEqual(sorted(r.design.keys()), ["alu", "top"])

    def test_constant_propagates_through_two_levels(self):
        r = elab("""
module leaf(input en, input [3:0] d, output [3:0] y); assign y = en ? d : 4'd0; endmodule
module mid(input en, input [3:0] d, output [3:0] y); leaf l(.en(en), .d(d), .y(y)); endmodule
module top(input [3:0] d, output [3:0] y); mid m(.en(1'b1), .d(d), .y(y)); endmodule""", top="top")
        self.assertIn("leaf[en=1'h1]", r.design.keys())
        self.assertEqual(ops(r.design.modules["leaf[en=1'h1]"]), {})

    def test_instance_counts_multiply_along_hierarchy_and_arrays(self):
        r = elab("""
module cell(input [3:0] d, output [3:0] q); assign q = ~d; endmodule
module row(input [3:0] d, output [3:0] q);
  wire [3:0] t [0:2];
  cell c [2:0] (.d(d), .q());
  cell last(.d(d), .q(q));
endmodule
module top(input [3:0] d, output [3:0] q, output [3:0] q2);
  row r0(.d(d), .q(q));
  row r1(.d(d), .q(q2));
endmodule""", top="top")
        self.assertEqual(r.design.instance_counts()["cell"], 8)
        self.assertEqual(r.design.instance_counts()["row"], 2)

    def test_instance_array_slicing_of_connections(self):
        r = elab("""
module inv(input a, output y); assign y = ~a; endmodule
module top(input [3:0] a, output [3:0] y);
  inv u [3:0] (.a(a), .y(y));
endmodule""", top="top")
        m = top_module(r)
        insts = sorted((n.attrs["inst"] for n in m.nodes if n.op == "INST"))
        self.assertEqual(insts, ["u[0]", "u[1]", "u[2]", "u[3]"])
        self.assertEqual(codes(r), [])

    def test_missing_module_becomes_black_box_with_diagnostic(self):
        r = elab("""
module top(input clk, input [7:0] d, output [7:0] q);
  vendor_ram #(.AW(4)) u (.clk(clk), .din(d), .dout(q));
endmodule""")
        m = top_module(r)
        bb = [n for n in m.nodes if n.op == "BLACKBOX"]
        self.assertEqual(len(bb), 1)
        self.assertEqual(bb[0].attrs["name"], "vendor_ram")
        miss = [d for d in r.diagnostics if d.code == "MISSING_MODULE"]
        self.assertEqual(len(miss), 1)
        self.assertTrue(miss[0].affects_cost)
        self.assertEqual(miss[0].line, 3)
        self.assertEqual(r.design.missing_modules(), [])
        # direction guess: dout drives q (undriven otherwise), clk and din are inputs
        self.assertEqual(sorted(dict(bb[0].inputs)), ["clk", "din"])
        self.assertEqual(bb[0].attrs["out_ports"], ("dout",))

    def test_udp_instance_is_black_box(self):
        r = elab("""
primitive my_udp(o, a, b); output o; input a, b;
  table 0 0 : 0; 1 1 : 1; endtable
endprimitive
module top(input a, b, output o); my_udp u(o, a, b); endmodule""")
        self.assertIn("MISSING_MODULE", codes(r))

    def test_gate_primitives_one_per_gate(self):
        r = elab("""
module top(input a, b, c, output y1, y2, y3, y4);
  and g1(y1, a, b);
  nor g2(y2, a, b, c);
  not g3(y3, a);
  xor g4(y4, a, b);
endmodule""")
        m = top_module(r)
        o = ops(m)
        self.assertEqual((o.get("AND"), o.get("OR"), o.get("XOR"), o.get("NOT")), (1, 2, 1, 2))
        res = comb(m, a=1, b=0, c=0)
        self.assertEqual((res["y1"], res["y2"], res["y3"], res["y4"]), (0, 0, 0, 1))

    def test_tristate_is_mux_with_enable_flagged(self):
        r = elab("""
module top(input en, input [3:0] d, output [3:0] y, inout [3:0] io, output [3:0] z);
  assign y = en ? d : 4'bz;
  assign io = en ? d : 4'bz;
  bufif1 b0 (z[0], d[0], en);
endmodule""")
        m = top_module(r)
        tri = [n for n in m.nodes if n.op == "MUX" and n.attrs.get("tristate")]
        self.assertEqual(len(tri), 2)      # y and io share one structurally hashed driver, plus the bufif1
        self.assertTrue(any("tri-state" in a for a in r.assumptions))

    def test_interface_and_modport_are_flattened(self):
        r = elab("""
interface bus_if #(parameter W = 8)(input clk);
  logic [W-1:0] data; logic valid;
  modport src(output data, valid);
  modport dst(input data, valid);
endinterface
module prod(bus_if.src b, input [7:0] x); assign b.data = x; assign b.valid = 1'b1; endmodule
module cons(bus_if.dst b, output [7:0] y); assign y = b.valid ? b.data : 8'h0; endmodule
module top(input clk, input [7:0] x, output [7:0] y);
  bus_if #(.W(8)) bi(.clk(clk));
  prod p(.b(bi), .x(x));
  cons c(.b(bi), .y(y));
endmodule""", top="top")
        self.assertEqual(codes(r), [])
        self.assertEqual(len([k for k in r.design.keys() if k.startswith(("prod", "cons"))]), 2)
        cons = [m for k, m in r.design.modules.items() if k.startswith("cons")][0]
        self.assertEqual(sorted(p.name for p in cons.ports), ["b.clk", "b.data", "b.valid", "y"])

    def test_hierarchical_reference_is_skipped_with_diagnostic(self):
        r = elab("""
module sub(output [3:0] q); assign q = 4'd3; endmodule
module top(output [3:0] y);
  sub u(.q());
  assign y = u.q;
endmodule""", top="top")
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r, True))

    def test_recursive_instantiation_is_cut(self):
        r = elab("""
module rec(input a, output y); rec u(.a(a), .y(y)); endmodule""", top="rec")
        self.assertIn("RECURSIVE_INSTANCE", codes(r))

    def test_parameterized_recursion_terminates_through_generate(self):
        r = elab("""
module tree #(parameter N = 8)(input [N-1:0] a, output y);
  generate if (N == 1) begin : leaf assign y = a[0]; end
  else begin : node
    wire l, r;
    tree #(.N(N/2)) lt(.a(a[N/2-1:0]), .y(l));
    tree #(.N(N/2)) rt(.a(a[N-1:N/2]), .y(r));
    assign y = l ^ r;
  end endgenerate
endmodule""", top="tree")
        self.assertEqual(sorted(r.design.keys()), ["tree(N=1)", "tree(N=2)", "tree(N=4)", "tree(N=8)"])
        self.assertEqual(r.design.instance_counts()["tree(N=1)"], 8)


class Tops(unittest.TestCase):
    SRC = """
module leaf(input a, output y); assign y = a; endmodule
module top1(input a, output y); leaf l(.a(a), .y(y)); endmodule
module top2(input a, output y); assign y = ~a; endmodule
"""

    def test_auto_detect_single_and_ambiguous(self):
        r = elab(self.SRC)
        self.assertEqual(r.tops, ["top1", "top2"])
        self.assertIn("TOP_AMBIGUOUS", codes(r))
        self.assertEqual(sorted(r.top_keys), ["top1", "top2"])
        r2 = elab(self.SRC.replace("module top2", "module _unused_top2"), top="top1")
        self.assertEqual(r2.top_key, "top1")
        self.assertEqual(sorted(r2.design.keys()), ["leaf", "top1"])

    def test_explicit_top_and_not_found(self):
        r = elab(self.SRC, top="top2")
        self.assertEqual((r.top_key, sorted(r.design.keys())), ("top2", ["top2"]))
        with self.assertRaises(GatecostError) as cm:
            elab(self.SRC, top="nope")
        self.assertEqual(cm.exception.code, "TOP_NOT_FOUND")

    def test_no_sources(self):
        with self.assertRaises(GatecostError) as cm:
            elab({"a.sv": "// nothing\n"})
        self.assertEqual(cm.exception.code, "NO_SOURCES")

    def test_duplicate_module_last_wins(self):
        r = elab("""
module m(output [3:0] y); assign y = 1; endmodule
module m(output [3:0] y); assign y = 2; endmodule""")
        self.assertIn("DUPLICATE_MODULE", codes(r))
        self.assertEqual(comb(top_module(r))["y"], 2)

    def test_parsed_hashes_and_routing_of_vhdl_without_elaborator(self):
        r = elab({"a.sv": "module m(input a, output y); assign y = a; endmodule",
                  "b.vhd": "entity e is end entity;"})
        self.assertIn("m", r.parsed_hashes)
        self.assertTrue(len(r.parsed_hashes["m"]) > 8)
        self.assertEqual(r.top_key, "m")


class OnlyModules(unittest.TestCase):
    SRC = """
module a1(input [3:0] x, output [3:0] y); assign y = x + 1; endmodule
module a2(input [3:0] x, output [3:0] y); assign y = x + 2; endmodule
module mid(input [3:0] x, output [3:0] y, z); a1 u1(.x(x), .y(y)); a2 u2(.x(x), .y(z)); endmodule
module top(input [3:0] x, output [3:0] y, z); mid m(.x(x), .y(y), .z(z)); endmodule
"""

    def test_changed_cone_only_builds_changed_modules_and_ancestors(self):
        full = elab(self.SRC, top="top")
        part = elab(self.SRC, top="top", only_modules={"a1"})
        self.assertEqual(sorted(full.design.keys()), ["a1", "a2", "mid", "top"])
        self.assertEqual(sorted(part.design.keys()), ["a1", "mid", "top"])
        self.assertEqual(part.stubbed, ("a2",))
        # the modules that were built are identical to the full elaboration
        for k in part.design.keys():
            self.assertEqual(part.design.modules[k].canonical_hash, full.design.modules[k].canonical_hash)


class Limits(unittest.TestCase):
    def test_instance_array_exceeding_limit_is_clamped(self):
        r = elab("""
module inv(input a, output y); assign y = ~a; endmodule
module top(input a, output y); inv u [99999:0] (.a(a), .y()); endmodule""", top="top",
                 limits={"max_instances": 8})
        self.assertIn("LIMIT_REACHED", codes(r))
        self.assertLessEqual(r.design.instance_counts()["inv"], 8)

    def test_hierarchy_depth_limit_replaces_module_by_black_box(self):
        r = elab("""
module l3(input a, output y); assign y = a; endmodule
module l2(input a, output y); l3 u(.a(a), .y(y)); endmodule
module l1(input a, output y); l2 u(.a(a), .y(y)); endmodule
module l0(input a, output y); l1 u(.a(a), .y(y)); endmodule""", top="l0", limits={"max_hier_depth": 1})
        self.assertIn("LIMIT_REACHED", codes(r))

    def test_loop_unroll_limit(self):
        r = elab("""
module t(input [7:0] a, output reg [7:0] y);
  integer i;
  always @* begin y = 0; for (i = 0; i < 1000; i = i + 1) y = y + a; end
endmodule""", limits={"loop_unroll": 8})
        self.assertIn("LIMIT_REACHED", codes(r))

    def test_non_constant_loop_is_reported_and_bounded(self):
        r = elab("""
module t(input [3:0] n, input [7:0] a, output reg [7:0] y);
  integer i;
  always @* begin y = 0; for (i = 0; i < n; i = i + 1) y = y + a; end
endmodule""")
        self.assertIn("UNSUPPORTED_CONSTRUCT", codes(r))

    def test_function_call_depth_limit(self):
        r = elab("""
module t(input [7:0] a, output [7:0] y);
  function automatic [7:0] f(input [7:0] x); f = f(x + 1); endfunction
  assign y = f(a);
endmodule""", limits={"call_depth": 4})
        self.assertIn("LIMIT_REACHED", codes(r))

    def test_expression_depth_does_not_crash(self):
        expr = " + ".join(["a"] * 3000)
        r = elab(f"module t(input [7:0] a, output [7:0] y); assign y = {expr}; endmodule")
        self.assertTrue(r.design.keys())

    def test_net_width_limit_gives_black_box_module(self):
        r = elab("module t(input a, output [99999:0] y); assign y = {100000{a}}; endmodule")
        self.assertIn("LIMIT_REACHED", codes(r))
        self.assertTrue(top_module(r).is_blackbox)


if __name__ == "__main__":
    unittest.main()
