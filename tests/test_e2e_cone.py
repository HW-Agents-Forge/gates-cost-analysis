"""The changed-cone analysis (only the edited modules are elaborated, unchanged ones come from the baseline
analysis) must give exactly the results of the full analysis."""
import unittest

from gatecost import pipeline
from tests.e2e_support import GitCase
from tests.test_e2e_diff import FILES


def e_param(p):
    p.edit("rtl/top.sv", "parameter AW = 16", "parameter AW = 24")


def e_localparam(p):
    p.edit("rtl/dp.sv", "localparam DW = 16", "localparam DW = 24")


def e_mul(p):
    p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")


def e_stage(p):
    p.edit("rtl/dp.sv", "assign z = {16'b0, r3};", "reg [DW-1:0] r4;\n  always @(posedge clk) r4 <= r3;\n  assign z = {16'b0, r4};")


def e_case(p):
    p.edit("rtl/alu.sv", "      default: y <= a | b;", "      2'd3: y <= a & b;\n      default: y <= a | b;")


def e_alu_removed(p):
    p.edit("rtl/alu.sv", "      2'd2: y <= x;\n", "")


def e_rename(p):
    p.edit("rtl/dp.sv", "module dp", "module datapath")
    p.edit("rtl/top.sv", "  dp u_dp", "  datapath u_dp")


def e_array(p):
    p.edit("rtl/top.sv", "alu #(.W(AW)) u_alu (", "alu #(.W(AW)) u_alu [1:0] (")


def e_new_param_set(p):
    p.edit("rtl/top.sv", "alu #(.W(AW)) u_alu (", "alu #(.W(8)) u_alu (")


def e_new_module(p):
    p.write("rtl/extra.sv", "module extra (input clk, input [15:0] a, b, output reg [15:0] y);\n"
                            "  always @(posedge clk) y <= a * b;\nendmodule\n")
    p.edit("rtl/top.sv", "  dp u_dp", "  extra u_ex (.clk(clk), .a(a[15:0]), .b(b[15:0]), .y());\n  dp u_dp")


def e_define(p):
    p.edit("rtl/dp.sv", "module dp", "`define FAST\nmodule dp")


def e_reformat(p):
    p.edit("rtl/dp.sv", "  wire [DW-1:0] s1 = r1 + r2;", "  wire   [DW-1:0] s1 =\n  r1 + r2 ;")


def e_two_files(p):
    e_mul(p)
    e_alu_removed(p)


EDITS = [e_param, e_localparam, e_mul, e_stage, e_case, e_alu_removed, e_rename, e_array, e_new_param_set,
         e_new_module, e_define, e_reformat, e_two_files]


class ConeEquivalence(GitCase):
    FILES = FILES

    def test_cone_equals_full_for_every_edit(self):
        for edit in EDITS:
            with self.subTest(edit=edit.__name__):
                self.p.checkout_clean()
                for junk in ("rtl/extra.sv",):
                    (self.p.dir / junk).unlink(missing_ok=True)
                edit(self.p)
                results = []
                for env in ({}, {"GATECOST_NO_CONE": "1"}):
                    for fmt in ("agent", "json"):
                        _, text = self.p.run("diff", "--format", fmt, "--no-meta", "--verbose", "--threshold", "0", "--no-cache", env=env)
                        results.append((fmt, text))
                self.assertEqual(results[0], results[2], edit.__name__)
                self.assertEqual(results[1], results[3], edit.__name__)

    def test_cone_is_used_and_cached(self):
        seen = []
        orig = pipeline.analyze_cone

        def spy(*a, **k):
            r = orig(*a, **k)
            seen.append(r is not None)
            return r
        e_mul(self.p)
        from unittest import mock
        with mock.patch.object(pipeline, "analyze_cone", spy):
            self.p.run("diff")
            self.p.run("diff")
        self.assertEqual(seen, [True, True])

    def test_unsafe_edits_fall_back_to_full(self):
        self.assertIsNone(pipeline.cone_names(["rtl/a.vh"], {}, {"rtl/a.vh": "`define X 1"}))
        self.assertIsNone(pipeline.cone_names(["rtl/a.sv"], {}, {"rtl/a.sv": "package p; endpackage"}))
        self.assertIsNone(pipeline.cone_names(["rtl/a.sv"], {}, {"rtl/a.sv": "`define X 1\nmodule a; endmodule"}))
        self.assertIsNone(pipeline.cone_names(["files.f"], {}, {}))
        self.assertEqual(pipeline.cone_names(["rtl/a.sv"], {"rtl/a.sv": "module a; endmodule"}, {"rtl/a.sv": "module b; endmodule"}), {"a", "b"})
        self.assertEqual(pipeline.cone_names(["rtl/a.vhd"], {}, {"rtl/a.vhd": "entity e is end; architecture r of e is begin end;"}), {"e"})


if __name__ == "__main__":
    unittest.main()
