"""The changed-cone analysis parses only the sources it needs; the result must still equal the full analysis,
including diagnostics and macro facts of the files it did not read."""
import unittest

from gatecost import pipeline
from tests.e2e_support import GitCase

FILES = {
    "rtl/top.sv": """\
module top (input clk, input [15:0] a, b, output [15:0] z);
  wire [15:0] za, zb, zc;
  mid_a u_a (.clk(clk), .a(a), .b(b), .z(za));
  mid_b u_b (.clk(clk), .a(a), .b(b), .z(zb));
  mid_c u_c (.clk(clk), .a(a), .b(b), .z(zc));
  assign z = za ^ zb ^ zc;
endmodule
""",
    "rtl/mid_a.sv": """\
module mid_a (input clk, input [15:0] a, b, output [15:0] z);
  wire [15:0] y1, y2;
  leaf_a1 #(.W(16)) u1 (.clk(clk), .a(a), .b(b), .y(y1));
  leaf_a2 u2 (.clk(clk), .a(a), .b(b), .y(y2));
  assign z = y1 + y2;
endmodule
""",
    "rtl/mid_b.sv": """\
module mid_b (input clk, input [15:0] a, b, output [15:0] z);
  leaf_b u1 (.clk(clk), .a(a), .b(b), .y(z));
endmodule
""",
    "rtl/mid_c.sv": """\
module mid_c (input clk, input [15:0] a, b, output [15:0] z);
  leaf_c u1 (.clk(clk), .a(a), .b(b), .y(z));
endmodule
""",
    "rtl/leaf_a1.sv": """\
module leaf_a1 #(parameter W = 8) (input clk, input [W-1:0] a, b, output reg [W-1:0] y);
  always @(posedge clk) y <= a + b;
endmodule
""",
    "rtl/leaf_a2.sv": """\
module leaf_a2 (input clk, input [15:0] a, b, output reg [15:0] y);
  always @(posedge clk) y <= a - b;
endmodule
""",
    "rtl/leaf_b.sv": """\
`ifdef WIDE
`define LW 16
`else
`define LW 8
`endif
module leaf_b (input clk, input [15:0] a, b, output reg [15:0] y);
  always @(posedge clk) y <= a[`LW-1:0] * b[`LW-1:0];
endmodule
""",
    # a leaf with a syntax error and an ifdef: neither is touched by the edits below
    "rtl/leaf_c.sv": """\
module leaf_c (input clk, input [15:0] a, b, output reg [15:0] y);
`ifdef FAST_C
  wire [15:0] t = a | b;
`else
  wire [15:0] t = a & b;
`endif
  always @(posedge clk) y <= t + ;
endmodule
""",
}


def e_leaf(p):
    p.edit("rtl/leaf_a1.sv", "y <= a + b;", "y <= a + b + 1;")


def e_leaf_param(p):
    p.edit("rtl/mid_a.sv", "#(.W(16))", "#(.W(12))")


def e_mid_instance(p):
    p.edit("rtl/mid_a.sv", "  assign z = y1 + y2;", "  wire [15:0] y3;\n  leaf_a2 u3 (.clk(clk), .a(a), .b(b), .y(y3));\n  assign z = y1 + y2 + y3;")


def e_top(p):
    p.edit("rtl/top.sv", "assign z = za ^ zb ^ zc;", "assign z = (za ^ zb) + zc;")


def e_new_ifdef(p):
    p.edit("rtl/leaf_a2.sv", "  always @(posedge clk) y <= a - b;", "`ifdef NEWFLAG\n  always @(posedge clk) y <= a + b;\n`else\n  always @(posedge clk) y <= a - b;\n`endif")


def e_remove_instance(p):
    p.edit("rtl/mid_a.sv", "  leaf_a2 u2 (.clk(clk), .a(a), .b(b), .y(y2));\n", "  assign y2 = 16'd0;\n")


def e_new_module(p):
    p.write("rtl/leaf_a3.sv", "module leaf_a3 (input a, output b); assign b = ~a; endmodule\n")


def e_new_module_used(p):
    p.write("rtl/leaf_a3.sv", "module leaf_a3 (input [15:0] a, output [15:0] b); assign b = ~a; endmodule\n")
    p.edit("rtl/mid_a.sv", "  assign z = y1 + y2;", "  wire [15:0] y3;\n  leaf_a3 u3 (.a(a), .b(y3));\n  assign z = y1 + y2 + y3;")


def e_delete_file(p):
    (p.dir / "rtl/leaf_a2.sv").unlink()


def e_define(p):
    p.edit("rtl/leaf_a1.sv", "module leaf_a1", "`define X 1\nmodule leaf_a1")


EDITS = [e_leaf, e_leaf_param, e_mid_instance, e_top, e_new_ifdef, e_remove_instance, e_new_module,
         e_new_module_used, e_delete_file, e_define]


class ConeSubset(GitCase):
    FILES = FILES

    def run_diff(self, env):
        out = []
        for fmt in ("agent", "json"):
            _, text = self.p.run("diff", "--format", fmt, "--no-meta", "--verbose", "--threshold", "0", env=env)
            out.append(text)
        return out

    def test_subset_equals_full_for_every_edit(self):
        used = 0
        for edit in EDITS:
            with self.subTest(edit=edit.__name__):
                self.p.checkout_clean()
                (self.p.dir / "rtl/leaf_a3.sv").unlink(missing_ok=True)
                self.p.run("diff")                        # baseline analysis (and its per-file index) into the cache
                edit(self.p)
                pipeline.cone_stats.clear()
                a = self.run_diff({})
                if pipeline.cone_stats and pipeline.cone_stats["parsed"] < pipeline.cone_stats["total"]:
                    used += 1
                b = self.run_diff({"GATECOST_NO_SUBSET": "1"})
                c = self.run_diff({"GATECOST_NO_CONE": "1"})
                self.assertEqual(a, b, edit.__name__)
                self.assertEqual(a, c, edit.__name__)
        self.assertGreaterEqual(used, 3, "the subset analysis should have been exercised")

    def test_unread_file_keeps_its_diagnostics(self):
        e_leaf(self.p)
        pipeline.cone_stats.clear()
        self.p.run("diff")
        self.p.run("diff")
        _, text = self.p.run("diff", "--format", "json", "--no-meta", "--verbose")
        self.assertLess(pipeline.cone_stats["parsed"], pipeline.cone_stats["total"])
        full = self.p.run("diff", "--format", "json", "--no-meta", "--verbose", env={"GATECOST_NO_CONE": "1"})[1]
        self.assertEqual(text, full)


if __name__ == "__main__":
    unittest.main()
