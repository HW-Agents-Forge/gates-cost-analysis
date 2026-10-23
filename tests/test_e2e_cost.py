"""`cost` and `estimate --rtl` end to end through the real front-ends (CLI.md 2.3 and 3.2, COST_MODEL.md 7)."""
import json
import tempfile
import unittest
from pathlib import Path

from tests.agent_grammar import parse
from tests.e2e_support import GitCase, Project
from tests.test_e2e_diff import FILES


def estimate(*argv):
    p = Project({}, git=False)
    try:
        code, text = p.run("estimate", *argv)
    finally:
        p.close()
    return code, text


def typ(*argv) -> int:
    code, text = estimate(*argv)
    assert code == 0, text
    return int(parse(text).head.groups[0])


class CostCommand(GitCase):
    FILES = FILES

    def test_agent_output_parses_and_lists_modules(self):
        code, text = self.p.run("cost")
        self.assertEqual(code, 0)
        out = parse(text)
        self.assertEqual(out.head.kind, "COST")
        names = [r.groups[1] for r in out.of("COST_MODULE")]
        self.assertEqual(set(names), {"top", "alu", "dp"})
        self.assertTrue(any(r.kind == "CATEGORIES" for r in out.records))

    def test_total_is_sum_of_instances_and_by_options(self):
        code, text = self.p.run("cost", "--format", "json", "--no-meta")
        doc = json.loads(text)
        r = doc["result"]
        self.assertEqual(r["top"], "top")
        top = [m for m in r["modules"] if m["module"] == "top"][0]
        self.assertAlmostEqual(top["total_ge"]["typ"], r["total"]["ge"]["typ"], places=2)
        self.assertAlmostEqual(sum(r["by_category"].values()), r["total"]["ge"]["typ"], delta=0.5)
        for by in ("module", "category", "file"):
            code, text = self.p.run("cost", "--by", by)
            self.assertEqual(code, 0)
            parse(text)

    def test_budget_exit_code(self):
        code, text = self.p.run("cost", "--budget", "10")
        self.assertEqual(code, 1)
        self.assertTrue(text.startswith("COST "))
        self.assertIn("WARN BUDGET_EXCEEDED", text)
        self.assertEqual(self.p.run("cost", "--budget", "1000000")[0], 0)

    def test_detail_ops_validates_against_the_schema(self):
        from gatecost.report.validate import validate_json
        code, text = self.p.run("cost", "--format", "json", "--detail", "ops")
        doc = json.loads(text)
        self.assertEqual(validate_json(doc), [])
        self.assertTrue(doc["result"]["operations"])
        self.assertNotIn(str(self.p.dir), text)       # no absolute paths anywhere

    def test_vhdl_and_mixed_cost(self):
        p = Project({
            "rtl/a.vhd": "library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;\n"
                         "entity a is port (x, y : in unsigned(15 downto 0); z : out unsigned(15 downto 0)); end entity;\n"
                         "architecture r of a is begin z <= x + y; end architecture;\n",
            "rtl/b.sv": "module b (input [15:0] x, y, output [15:0] z);\n  a u (.x(x), .y(y), .z(z));\nendmodule\n"})
        self.addCleanup(p.close)
        code, text = p.run("cost")
        out = parse(text)
        self.assertEqual(code, 0, text)
        self.assertEqual(out.of("TOP")[0].groups[0], "b")
        self.assertGreater(int(out.head.groups[0]), 50)


class EstimateFragments(unittest.TestCase):
    def test_sanity_bands_through_real_rtl(self):
        W = "a=32,b=32,c=32,d=32,s=2"
        add32 = typ("--rtl", "assign y = a + b;", "--widths", "a=32,b=32,y=32")
        mul16 = typ("--rtl", "assign y = a * b;", "--widths", "a=16,b=16,y=32")
        mul32 = typ("--rtl", "assign y = a * b;", "--widths", "a=32,b=32,y=64")
        dff32 = typ("--rtl", "always @(posedge clk) q <= d;", "--widths", "d=32,q=32")
        mux4 = typ("--rtl", "assign y = s[1] ? (s[0] ? d : c) : (s[0] ? b : a);", "--widths", W + ",y=32")
        shl32 = typ("--rtl", "assign y = a << s;", "--widths", "a=32,s=5,y=32")
        self.assertTrue(170 <= add32 <= 300, add32)
        self.assertTrue(1200 <= mul16 <= 2000, mul16)
        self.assertTrue(4500 <= mul32 <= 8000, mul32)
        self.assertTrue(180 <= dff32 <= 260, dff32)
        self.assertTrue(200 <= mux4 <= 260, mux4)
        self.assertTrue(330 <= shl32 <= 420, shl32)

    def test_verilog_and_vhdl_forms_agree(self):
        pairs = [
            (("assign y = a + b;", "y <= a + b;"), "a=16,b=16,y=16"),
            (("assign y = a * b + c;", "y <= resize(a * b, 32) + c;"), "a=16,b=16,c=32,y=32"),
            (("assign y = a & b;", "y <= a and b;"), "a=8,b=8,y=8"),
            (("assign y = (a < b) ? a : b;", "y <= a when a < b else b;"), "a=16,b=16,y=16"),
            (("always @(posedge clk) q <= d;", "process (clk) begin if rising_edge(clk) then q <= d; end if; end process;"), "d=12,q=12"),
        ]
        for (v, h), widths in pairs:
            with self.subTest(v=v):
                self.assertEqual(typ("--rtl", v, "--widths", widths), typ("--rtl", h, "--lang", "vhdl", "--widths", widths))

    def test_inference_rules_and_assumed_lines(self):
        code, text = estimate("--rtl", "assign y = a + b;")
        out = parse(text)
        assumed = [r.line for r in out.of("ASSUMED")]
        self.assertTrue(any("input a width not given, 32 used" in l for l in assumed), assumed)
        self.assertTrue(any("output y width inferred as 32" in l for l in assumed), assumed)
        code, text = estimate("--rtl", "assign y = a + b;", "--widths", "a=8,b=8,y=8")
        self.assertEqual([r for r in parse(text).of("ASSUMED")], [])
        # declarations in the fragment take precedence over --widths
        c1 = typ("--rtl", "wire [7:0] a; wire [7:0] b; assign y = a + b;", "--widths", "y=8")
        c2 = typ("--rtl", "assign y = a + b;", "--widths", "a=8,b=8,y=8")
        self.assertEqual(c1, c2)
        # a truncated multiplier is cheaper than a full one
        self.assertLess(typ("--rtl", "assign y = a * b;", "--widths", "a=16,b=16,y=16"),
                        typ("--rtl", "assign y = a * b;", "--widths", "a=16,b=16,y=32"))

    def test_count_multiplies(self):
        one = typ("--rtl", "assign y = a * b + c;", "--widths", "a=16,b=16,c=32")
        four = typ("--rtl", "assign y = a * b + c;", "--widths", "a=16,b=16,c=32", "--count", "4")
        self.assertEqual(four, 4 * one)
        code, text = estimate("--rtl", "assign y = a + b;", "--count", "3", "--format", "json", "--no-meta")
        doc = json.loads(text)
        self.assertEqual(doc["status"], "ok")
        self.assertTrue(doc["result"]["normalized"].startswith("3 * ("))
        code, text = estimate("--rtl", "assign y = a;", "--count", "0")
        self.assertEqual((code, text.split()[1]), (2, "BAD_ARGUMENT"))

    def test_rtl_file_and_errors(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "f.v"
            f.write_text("assign y = a * b;\n// comment\n", encoding="utf-8")
            code, text = estimate("--rtl-file", str(f), "--widths", "a=16,b=16,y=32")
            self.assertEqual(code, 0, text)
            self.assertEqual(typ("--rtl-file", str(f), "--widths", "a=16,b=16,y=32"), typ("--rtl", "assign y = a * b;", "--widths", "a=16,b=16,y=32"))
        code, text = estimate("--rtl", "assign y = a +;", "--widths", "a=8")
        self.assertEqual(code, 2)
        self.assertEqual(text.count("\n"), 1)
        self.assertTrue(text.startswith("ERROR BAD_ARGUMENT"))
        code, text = estimate("--rtl", "assign y = a;", "--lang", "cobol")
        self.assertEqual(code, 2)

    def test_clocked_fragments_make_registers(self):
        with_en = typ("--rtl", "always @(posedge clk) q <= en ? d : q;", "--widths", "d=32,q=32,en=1")
        plain = typ("--rtl", "always @(posedge clk) q <= d;", "--widths", "d=32,q=32")
        self.assertGreater(with_en, plain)
        rst = typ("--rtl", "always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else q <= d;", "--widths", "d=32,q=32")
        self.assertGreater(rst, plain)

    def test_compare_and_json_envelope(self):
        code, text = estimate("--compare", "mul 32x32", "mul 32 const=10")
        self.assertEqual(code, 0)
        parse(text)


if __name__ == "__main__":
    unittest.main()
