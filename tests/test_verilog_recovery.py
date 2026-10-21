"""Error recovery (FRONTEND.md section 2.4): exact diagnostics, neighbours stay analyzed."""
import unittest

from gatecost.frontend.verilog import ast as A
from gatecost.frontend.verilog import parse_text


def diags(d, severity=None):
    return [(x.code, x.line, x.column, x.message) for x in d.diagnostics if severity is None or x.severity == severity]


def names(m):
    return [getattr(i, "name", type(i).__name__) for i in m.items]


class Recovery(unittest.TestCase):
    def test_missing_semicolon_keeps_both_assigns(self):
        d = parse_text("module a(input x, output y);\n assign y = x\n assign z = x;\n wire w;\nendmodule\nmodule b; endmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 3, 2, "expected ';', found 'assign'")])
        a, b = d.modules
        self.assertEqual(names(a), ["ContAssign", "ContAssign", "w"])
        self.assertTrue(a.incomplete)
        self.assertFalse(b.incomplete)

    def test_statement_error_in_block(self):
        d = parse_text("module a(input clk, output reg y);\n always @(posedge clk) begin\n  y <= ;\n  y <= 1;\n"
                       "  if (y y) y <= 0; else y <= 1;\n  y <= 2;\n end\n assign q = 1;\nendmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 3, 8, "expected expression, found ';'"),
                                    ("PARSE_ERROR", 5, 9, "expected ')', found 'y'")])
        m = d.modules[0]
        body = m.items[0].body
        self.assertEqual([s.rhs.value for s in body.items], [1, 2])
        self.assertIsInstance(m.items[1], A.ContAssign)
        self.assertTrue(m.incomplete)

    def test_expression_error_in_declaration(self):
        d = parse_text("module a;\n wire x = (1 + ;\n wire y = 2;\nendmodule\nmodule b; wire z; endmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 2, 16, "expected expression, found ';'")])
        self.assertEqual(names(d.modules[0]), ["y"])
        self.assertEqual(names(d.modules[1]), ["z"])
        self.assertFalse(d.modules[1].incomplete)

    def test_module_header_error_skips_to_endmodule(self):
        d = parse_text("module a(input x,, output y);\n wire q;\nendmodule\nmodule b; wire z; endmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 1, 18, "expected identifier, found ','")])
        a, b = d.modules
        self.assertTrue(a.incomplete)
        self.assertEqual(a.items, ())
        self.assertEqual([p.name for p in a.ports], ["x"])            # what was parsed is retained
        self.assertEqual(names(b), ["z"])

    def test_missing_endmodule(self):
        d = parse_text("module a;\n wire q;\nmodule b; wire z; endmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 3, 1, "expected 'endmodule', found 'module'")])
        self.assertEqual([m.name for m in d.modules], ["a", "b"])
        self.assertTrue(d.modules[0].incomplete)
        self.assertEqual(names(d.modules[0]), ["q"])
        self.assertFalse(d.modules[1].incomplete)

    def test_eof_inside_block(self):
        d = parse_text("module a;\n always @(*) begin\n x = 1;\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 4, 1, "expected 'end', found end of file")])
        self.assertTrue(d.modules[0].incomplete)
        self.assertEqual(len(d.modules[0].items[0].body.items), 1)

    def test_case_item_error(self):
        d = parse_text("module a;\n always @(*) case (x)\n 1: y = ;\n 2: y = 3;\n endcase\n assign w = 1;\nendmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 3, 9, "expected expression, found ';'")])
        case = d.modules[0].items[0].body
        self.assertEqual([i.labels[0].value for i in case.items], [2])
        self.assertIsInstance(d.modules[0].items[1], A.ContAssign)

    def test_stray_end(self):
        d = parse_text("module a;\n wire q;\n end\n wire r;\nendmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 3, 2, "unexpected 'end' at module item level")])
        self.assertEqual(names(d.modules[0]), ["q", "r"])

    def test_header_error_of_always_skips_body(self):
        d = parse_text("module a;\n always @(posedge clk begin\n x <= 1;\n y <= 2;\n end\n wire ok;\nendmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 2, 23, "expected ')', found 'begin'")])
        self.assertEqual(names(d.modules[0]), ["ok"])

    def test_generate_header_error(self):
        d = parse_text("module a;\n for (i = 0; i < ; i++) begin : g\n wire w;\n end\n wire ok;\nendmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 2, 18, "expected expression, found ';'")])
        self.assertEqual(names(d.modules[0]), ["ok"])

    def test_illegal_character_reported_once(self):
        d = parse_text("module a;\n wire q; `\n wire r;\nendmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 2, 10, "illegal character '`'")])
        self.assertEqual(names(d.modules[0]), ["q", "r"])

    def test_errors_in_several_modules_are_independent(self):
        d = parse_text("module a; assign x = ; endmodule\nmodule b; wire ok; endmodule\nmodule c; assign = 1; endmodule\n")
        self.assertEqual([(x[1], x[3]) for x in diags(d)],
                         [(1, "expected expression, found ';'"), (3, "expected expression, found '='")])
        self.assertEqual([m.incomplete for m in d.modules], [True, False, True])
        self.assertEqual(names(d.modules[1]), ["ok"])

    def test_function_body_error(self):
        d = parse_text("module a;\n function f(input x);\n  f = ;\n  f = x;\n endfunction\n wire after;\nendmodule\n")
        self.assertEqual(diags(d), [("PARSE_ERROR", 3, 7, "expected expression, found ';'")])
        self.assertEqual(names(d.modules[0]), ["f", "after"])
        self.assertEqual(len(d.modules[0].items[0].body), 1)

    def test_unknown_item_keyword(self):
        d = parse_text("module a;\n 12 here;\n wire ok;\nendmodule\n")
        self.assertEqual(len(diags(d)), 1)
        self.assertEqual(names(d.modules[0])[-1], "ok")

    def test_undefined_macro_is_not_a_parse_error(self):
        d = parse_text("module a;\n wire [`W-1:0] x;\nendmodule\n")
        self.assertEqual([(x.code, x.line) for x in d.diagnostics], [("UNDEFINED_MACRO", 2)])
        self.assertEqual(names(d.modules[0]), ["x"])

    def test_nested_begin_end_resync(self):
        d = parse_text("module a;\n always @(*) begin\n  if (x y) begin\n   a = 1;\n   b = 2;\n  end else begin\n   c = 3;\n  end\n  d = 4;\n end\nendmodule\n")
        self.assertEqual(len(diags(d)), 1)
        body = d.modules[0].items[0].body
        self.assertEqual([s.lhs.name for s in body.items], ["d"])

    def test_empty_and_garbage_input(self):
        for text in ("", "   \n", "endmodule", "module", "module ;", ")))", "`define", "module m(;"):
            d = parse_text(text)
            self.assertIsNotNone(d)

    def test_info_regions_do_not_mark_incomplete(self):
        d = parse_text("module a;\n initial x = 0;\n wire w;\nendmodule\n")
        self.assertFalse(d.modules[0].incomplete)
        self.assertEqual([x.severity for x in d.diagnostics], ["info"])


if __name__ == "__main__":
    unittest.main()
