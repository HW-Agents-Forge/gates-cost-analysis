"""`gatecost diff` end to end on real edits in throw-away git projects (SPEC.md section 14, agent acceptance).

Every output is parsed with the reference grammar of `tests/agent_grammar.py` (via `Project.diff`).
"""
import json
import unittest

from tests.e2e_support import GitCase, delta, verdict

ALU = """\
module alu #(parameter W = 16) (
  input clk, input [W-1:0] a, b, input [1:0] op, output reg [W-1:0] y);
  wire [W-1:0] s = a + b;
  wire [W-1:0] d = a - b;
  wire [W-1:0] x = a ^ b;
  always @(posedge clk) begin
    case (op)
      2'd0: y <= s;
      2'd1: y <= d;
      2'd2: y <= x;
      default: y <= a | b;
    endcase
  end
endmodule
"""

DP = """\
module dp (input clk, input [31:0] a, b, c, output [31:0] z);
  localparam DW = 16;
  reg [DW-1:0] r1, r2, r3;
  wire [DW-1:0] s1 = r1 + r2;
  wire [DW-1:0] s2 = r2 - r3;
`ifdef FAST
  wire [DW-1:0] f = r1 | r3;
`else
  wire [DW-1:0] f = r1 & r3;
`endif
  always @(posedge clk) begin
    r1 <= a[DW-1:0]; r2 <= b[DW-1:0]; r3 <= s1 + s2 + f;
  end
  assign z = {16'b0, r3};
endmodule
"""

TOP = """\
module top #(parameter AW = 16) (input clk, input [31:0] a, b, c, input [1:0] op, output [31:0] z, output [AW-1:0] q);
  dp u_dp (.clk(clk), .a(a), .b(b), .c(c), .z(z));
  alu #(.W(AW)) u_alu (.clk(clk), .a(a[AW-1:0]), .b(b[AW-1:0]), .op(op), .y(q));
endmodule
"""

FILES = {"rtl/alu.sv": ALU, "rtl/dp.sv": DP, "rtl/top.sv": TOP}


class VerilogEdits(GitCase):
    FILES = FILES

    def test_clean_tree_is_no_change(self):
        code, out, raw = self.p.diff()
        self.assertEqual(code, 0)
        self.assertEqual(verdict(out), "NO_CHANGE")
        self.assertEqual(len(out.records), 2)
        self.assertTrue(raw.startswith("DELTA 0 GE (0..0) 0.0% verdict=NO_CHANGE confidence=high\nTOP top before="))

    def test_widen_a_bus(self):
        self.p.edit("rtl/dp.sv", "reg [DW-1:0] r1, r2, r3;", "reg [DW-1:0] r1, r2, r3;\n  wire [7:0] ex = r1[7:0] + r2[7:0];")
        self.p.edit("rtl/dp.sv", "assign z = {16'b0, r3};", "assign z = {16'b0, r3} ^ {24'b0, ex};")
        code, out, _ = self.p.diff()
        self.assertEqual(verdict(out), "INCREASE")
        self.assertGreater(delta(out), 0)
        self.assertTrue(any(c.groups[3] == "added" and c.groups[1] == "rtl/dp.sv" for c in out.contributors()))

    def test_widen_an_operation_is_a_width_change(self):
        self.p.edit("rtl/alu.sv", "wire [W-1:0] x = a ^ b;", "wire [7:0] x = a[7:0] ^ b[7:0];")
        self.p.edit("rtl/alu.sv", "2'd2: y <= x;", "2'd2: y <= {a[W-1:8], x};")
        self.p.commit()
        self.p.edit("rtl/alu.sv", "wire [7:0] x = a[7:0] ^ b[7:0];", "wire [11:0] x = a[11:0] ^ b[11:0];")
        self.p.edit("rtl/alu.sv", "{a[W-1:8], x}", "{a[W-1:12], x}")
        code, out, raw = self.p.diff()
        cs = out.contributors()
        self.assertTrue(cs and cs[0].groups[3] == "width", raw)
        self.assertIn("x 8->12", raw)   # traced to the changed declaration of x
        self.assertGreater(delta(out), 0)

    def test_parameter_default_overridden_everywhere_changes_nothing(self):
        self.p.edit("rtl/alu.sv", "parameter W = 16", "parameter W = 8")
        code, out, _ = self.p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE")

    def test_instance_override_change_is_one_grouped_cause(self):
        self.p.edit("rtl/top.sv", "parameter AW = 16", "parameter AW = 32")
        code, doc = self.p.diff_json()
        cs = doc["result"]["contributors"]
        self.assertEqual(len(cs), 1, [c["description"] for c in cs])
        c = cs[0]
        self.assertEqual(c["cause"]["kind"], "parameter")
        self.assertEqual((c["cause"]["from"], c["cause"]["to"]), ("16", "32"))
        self.assertGreaterEqual(len(c["children"]), 3)
        self.assertEqual(c["file"], "rtl/top.sv")
        self.assertEqual(c["line"], 1)
        code, out, raw = self.p.diff()
        self.assertIn("cause=parameter:AW", raw)

    def test_localparam_used_in_many_places_is_one_contributor(self):
        self.p.edit("rtl/dp.sv", "localparam DW = 16", "localparam DW = 24")
        code, doc = self.p.diff_json()
        cs = doc["result"]["contributors"]
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["cause"]["name"], "DW")
        self.assertEqual(cs[0]["line"], 2)
        self.assertGreaterEqual(len(cs[0]["children"]), 4)
        self.assertEqual(sum(round(c["delta_ge"]["typ"], 3) for c in cs[0]["children"]) > 0, True)

    def test_added_multiplier(self):
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")
        code, out, raw = self.p.diff()
        self.assertEqual(verdict(out), "INCREASE")
        top = out.contributors()[0]
        self.assertEqual(top.groups[3], "added")
        self.assertIn("MUL 16x16", top.groups[4])

    def test_added_pipeline_register_stage(self):
        self.p.edit("rtl/dp.sv", "assign z = {16'b0, r3};", "reg [DW-1:0] r4;\n  always @(posedge clk) r4 <= r3;\n  assign z = {16'b0, r4};")
        code, out, raw = self.p.diff()
        self.assertEqual(verdict(out), "INCREASE")
        c = out.contributors()[0]
        self.assertEqual(c.groups[3], "added")
        self.assertTrue(c.groups[4].startswith("DFF x16"), raw)

    def test_added_case_branch_is_a_structure_change(self):
        self.p.edit("rtl/alu.sv", "      default: y <= a | b;", "      2'd3: y <= a & b;\n      default: y <= a | b;")
        code, out, raw = self.p.diff()
        self.assertGreater(delta(out), 0)
        reasons = {c.groups[3] for c in out.contributors()}
        self.assertTrue(reasons & {"structure", "added"}, raw)

    def test_toggle_a_define_has_a_define_cause(self):
        self.p.edit("rtl/dp.sv", "`ifdef FAST\n  wire [DW-1:0] f = r1 | r3;", "`ifdef FAST\n  wire [DW-1:0] f = r1 + r3;")
        self.p.commit()
        code, out, raw = self.p.diff("--define", "FAST")
        self.assertEqual(verdict(out), "NO_CHANGE")      # same defines on both sides
        self.p.edit("rtl/dp.sv", "module dp", "`define FAST\nmodule dp")
        code, doc = self.p.diff_json()
        kinds = {(c["cause"] or {}).get("kind") for c in doc["result"]["contributors"]}
        self.assertIn("define", kinds, doc["result"]["contributors"])
        self.assertGreater(doc["result"]["delta"]["ge"]["typ"], 0)

    def test_reformat_only_is_no_change(self):
        self.p.edit("rtl/dp.sv", "  wire [DW-1:0] s1 = r1 + r2;",
                    "  // pipeline arithmetic\n\n  wire   [DW-1:0]  s1\n      =   r1 +\n   r2 ;   /* sum */")
        code, out, raw = self.p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE")
        self.assertEqual(raw.splitlines()[0], "DELTA 0 GE (0..0) 0.0% verdict=NO_CHANGE confidence=high")

    def test_rename_signal_and_reorder_declarations(self):
        s = self.p.read("rtl/dp.sv").replace("s1", "sum_one").replace("reg [DW-1:0] r1, r2, r3;", "reg [DW-1:0] r3, r2, r1;")
        self.p.write("rtl/dp.sv", s)
        code, out, _ = self.p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE")

    def test_module_rename_and_file_move(self):
        self.p.edit("rtl/dp.sv", "module dp", "module datapath")
        self.p.edit("rtl/top.sv", "  dp u_dp", "  datapath u_dp")
        code, out, _ = self.p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE")
        self.p.commit()
        (self.p.dir / "rtl" / "sub").mkdir()
        (self.p.dir / "rtl" / "alu.sv").rename(self.p.dir / "rtl" / "sub" / "alu_moved.sv")
        code, out, _ = self.p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE")

    def test_instance_count_change_is_multiplicity(self):
        self.p.edit("rtl/top.sv", "alu #(.W(AW)) u_alu (", "alu #(.W(AW)) u_alu [1:0] (")
        # both elements observed (one slice of q each): an unobserved element would be removed (D2)
        self.p.edit("rtl/top.sv", "output [AW-1:0] q)", "output [2*AW-1:0] q)")
        code, out, raw = self.p.diff("--verbose")
        reasons = {c.groups[3] for c in out.contributors()}
        self.assertIn("multiplicity", reasons, raw)
        self.assertGreater(delta(out), 0)
        self.assertTrue(any(r.kind == "MODULE" for r in out.records))

    def test_new_untracked_file_is_added(self):
        self.p.write("rtl/extra.sv", "module extra (input clk, input [15:0] a, b, output reg [15:0] y);\n"
                                     "  always @(posedge clk) y <= a * b;\nendmodule\n")
        self.p.edit("rtl/top.sv", "output [AW-1:0] q)", "output [AW-1:0] q, output [15:0] e)")
        self.p.edit("rtl/top.sv", "  dp u_dp", "  extra u_ex (.clk(clk), .a(a[15:0]), .b(b[15:0]), .y(e));\n  dp u_dp")
        code, out, raw = self.p.diff()
        self.assertEqual(verdict(out), "INCREASE")
        self.assertTrue(any(c.groups[1] == "rtl/extra.sv" for c in out.contributors()), raw)

    def test_new_instance_with_unconnected_output_costs_nothing(self):
        # D2: an instance whose outputs reach no output of the top is removed with its whole subtree
        self.p.write("rtl/extra.sv", "module extra (input clk, input [15:0] a, b, output reg [15:0] y);\n"
                                     "  always @(posedge clk) y <= a * b;\nendmodule\n")
        self.p.edit("rtl/top.sv", "  dp u_dp", "  extra u_ex (.clk(clk), .a(a[15:0]), .b(b[15:0]), .y());\n  dp u_dp")
        code, out, raw = self.p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE", raw)
        self.assertEqual(delta(out), 0)

    def test_instance_whose_output_is_no_longer_read_is_a_decrease(self):
        # D2: disconnecting the only observed output removes the unchanged child's whole cost
        self.p.edit("rtl/top.sv", ".op(op), .y(q));", ".op(op), .y());\n  assign q = 0;")
        code, out, raw = self.p.diff("--verbose")
        self.assertEqual(verdict(out), "DECREASE", raw)
        _, text = self.p.run("cost", "--format", "agent", "--no-cache")
        self.assertNotIn(" alu", text)
        self.p.checkout_clean()
        _, before = self.p.run("cost", "--format", "agent", "--no-cache")
        alu = [ln for ln in before.splitlines() if ln.split()[1:2] == ["alu"]]
        self.assertTrue(alu, before)
        self.assertAlmostEqual(-delta(out), float(alu[0].split()[0]), delta=1.0)

    def test_removed_logic_is_a_decrease_with_before_coordinates(self):
        self.p.edit("rtl/alu.sv", "      2'd2: y <= x;\n", "")
        code, out, raw = self.p.diff()
        self.assertEqual(verdict(out), "DECREASE")
        self.assertLess(delta(out), 0)

    def test_base_ref_and_committed_work(self):
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")
        self.p.commit("add mul")
        code, out, _ = self.p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE")
        code, out, _ = self.p.diff("--base-ref", "HEAD~1")
        self.assertEqual(verdict(out), "INCREASE")
        code, out, _ = self.p.diff("--base-ref", "merge-base")
        self.assertIn(verdict(out), ("INCREASE", "NO_CHANGE"))
        code, text = self.p.run("diff", "--base-ref", "nonexistent")
        self.assertEqual((code, text.split()[:2]), (2, ["ERROR", "NO_BASELINE"]))

    def test_path_restriction_reports_only_changes_inside(self):
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")
        self.p.edit("rtl/alu.sv", "      2'd2: y <= x;\n", "")
        code, full, _ = self.p.diff()
        code, only_alu, raw = self.p.diff("rtl/alu.sv")
        code, only_dp, raw2 = self.p.diff("rtl/dp.sv")
        self.assertEqual(verdict(only_alu), "DECREASE", raw)
        self.assertEqual(verdict(only_dp), "INCREASE", raw2)
        self.assertEqual(delta(only_alu) + delta(only_dp), delta(full))
        self.assertTrue(all(c.groups[1] == "rtl/alu.sv" for c in only_alu.contributors()))
        code, nothing, raw3 = self.p.diff("rtl/top.sv")
        self.assertEqual(verdict(nothing), "NO_CHANGE")

    def test_threshold_max_lines_and_others(self):
        self.p.edit("rtl/dp.sv", "localparam DW = 16", "localparam DW = 17")
        self.p.edit("rtl/alu.sv", "      2'd2: y <= x;\n", "")
        code, out, raw = self.p.diff("--threshold", "100000")
        self.assertEqual(out.contributors(), [])
        self.assertTrue(any(r.kind == "OTHERS" for r in out.records))
        code, out, raw = self.p.diff("--max-lines", "1")
        self.assertEqual(len(out.contributors()), 1)
        self.assertTrue(any(r.kind == "OTHERS" for r in out.records))
        code, out, raw = self.p.diff("--quiet")
        self.assertEqual(len(out.records), 1)

    def test_budget_and_fail_on_increase(self):
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")
        code, out, raw = self.p.diff("--budget", "10")
        self.assertEqual(code, 1)
        self.assertEqual(verdict(out), "INCREASE")              # the normal result is printed first
        self.assertTrue(any(w.groups[0] == "BUDGET_EXCEEDED" for w in out.of("WARN")), raw)
        code, out, raw = self.p.diff("--fail-on-increase", "50")
        self.assertEqual(code, 1)
        code, out, raw = self.p.diff("--fail-on-increase", "100000", "--budget", "1000000")
        self.assertEqual(code, 0)
        code, out, raw = self.p.diff("--fail-on-increase", "50", "--basis", "max")
        self.assertEqual(code, 1)
        code, doc = self.p.diff_json("--budget", "10")
        self.assertEqual(code, 1)
        self.assertEqual(doc["status"], "ok")
        self.assertTrue(any(d["code"] == "BUDGET_EXCEEDED" for d in doc["diagnostics"]))

    def test_json_limit_and_detail_ops(self):
        self.p.edit("rtl/dp.sv", "localparam DW = 16", "localparam DW = 24")
        self.p.edit("rtl/alu.sv", "      2'd2: y <= x;\n", "")
        code, doc = self.p.diff_json("--threshold", "0")
        n = len(doc["result"]["contributors"])
        self.assertGreaterEqual(n, 2)
        code, doc = self.p.diff_json("--threshold", "0", "--limit", "1")
        self.assertEqual(len(doc["result"]["contributors"]), 1)
        code, doc = self.p.diff_json("--detail", "ops")
        self.assertTrue(doc["result"]["operations"])
        from gatecost.report.validate import validate_json
        self.assertEqual(validate_json(doc), [])

    def test_formats_agree(self):
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")
        code, out, raw = self.p.diff()
        code, doc = self.p.diff_json()
        code, text = self.p.run("diff", "--format", "text")
        code, md = self.p.run("diff", "--format", "markdown")
        self.assertEqual(doc["result"]["verdict"], verdict(out))
        self.assertEqual(round(doc["result"]["delta"]["ge"]["typ"]), delta(out))
        self.assertIn(raw.splitlines()[0], text)
        self.assertIn("INCREASE", md)
        first = out.contributors()[0]
        self.assertEqual(doc["result"]["contributors"][0]["line"], int(first.groups[2]))
        self.assertLessEqual(len(raw), 2000)

    def test_additivity_in_json(self):
        self.p.edit("rtl/dp.sv", "localparam DW = 16", "localparam DW = 24")
        self.p.edit("rtl/alu.sv", "      2'd2: y <= x;\n", "")
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")
        code, doc = self.p.diff_json("--threshold", "0")
        r = doc["result"]
        tot = sum(c["delta_ge"]["typ"] for c in r["contributors"]) + r["others"]["net_ge"]["typ"]
        self.assertAlmostEqual(tot, r["delta"]["ge"]["typ"], places=2)
        self.assertAlmostEqual(r["after"]["ge"]["typ"] - r["before"]["ge"]["typ"], r["delta"]["ge"]["typ"], places=2)

    def test_symmetry(self):
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")
        code, fwd, _ = self.p.diff()
        self.p.commit()
        code, back, _ = self.p.diff("--base-ref", "HEAD~1")
        self.assertEqual(delta(back), delta(fwd))
        self.p.run("snapshot")
        self.p.checkout_clean()
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;")


class Errors(GitCase):
    FILES = FILES

    def test_no_sources(self):
        from tests.e2e_support import Project
        p = Project({"README.md": "x"})
        self.addCleanup(p.close)
        code, out = p.run("diff")
        self.assertEqual((code, out.split()[:2]), (3, ["ERROR", "NO_SOURCES"]))
        self.assertEqual(out.count("\n"), 1)
        code, doc = p.run("diff", "--format", "json")[0], json.loads(p.run("diff", "--format", "json")[1])
        self.assertEqual(doc["status"], "error")
        self.assertEqual(doc["error"]["code"], "NO_SOURCES")

    def test_no_baseline_without_git_or_snapshot(self):
        from tests.e2e_support import Project
        p = Project(FILES, git=False)
        self.addCleanup(p.close)
        code, out = p.run("diff")
        self.assertEqual((code, out.split()[:2]), (2, ["ERROR", "NO_BASELINE"]))

    def test_top_not_found(self):
        code, out = self.p.run("diff", "--top", "nope")
        self.assertEqual((code, out.split()[:2]), (3, ["ERROR", "TOP_NOT_FOUND"]))

    def test_bad_arguments(self):
        for argv in (["--base-ref", "x", "--since-snapshot"], ["--before", "a"], ["--nope"]):
            code, out = self.p.run("diff", *argv)
            self.assertEqual((code, out.split()[:2]), (2, ["ERROR", "BAD_ARGUMENT"]), argv)

    def test_top_changed_when_top_is_renamed(self):
        self.p.edit("rtl/top.sv", "module top ", "module chip ")
        code, out, raw = self.p.diff()
        self.assertTrue(any(w.groups[0] == "TOP_CHANGED" for w in out.of("WARN")), raw)
        self.assertFalse(any(w.groups[0] == "CONFIG_MISMATCH" for w in out.of("WARN")), raw)
        self.assertIn("TOP chip before=", raw)

    def test_explicit_top_missing_in_baseline_is_not_comparable(self):
        self.p.edit("rtl/top.sv", "module top ", "module chip ")
        self.p.commit()
        self.p.write("rtl/new_top.sv", "module newtop (input clk, input [31:0] a, b, c, input [1:0] op, output [31:0] z, output [15:0] q);\n"
                                      "  chip c0 (.clk(clk), .a(a), .b(b), .c(c), .op(op), .z(z), .q(q));\nendmodule\n")
        code, out, raw = self.p.diff("--top", "newtop")
        self.assertEqual(verdict(out), "NOT_COMPARABLE", raw)
        self.assertTrue(raw.startswith("DELTA n/a verdict=NOT_COMPARABLE"))
        self.assertIn("WHY low: baseline has no module named newtop", raw)


class Workflows(GitCase):
    FILES = FILES

    def test_snapshot_then_diff_restores_defines_and_profile(self):
        code, text = self.p.run("snapshot", "--define", "FAST")
        self.assertEqual(code, 0)
        self.assertRegex(text, r"^SNAPSHOT saved id=[0-9a-f]{8} files=3 modules=3\n$")
        self.p.edit("rtl/dp.sv", "r3 <= s1 + s2 + f;", "r3 <= s1 + s2 + f;\n    r2 <= b[DW-1:0] * c[DW-1:0];")
        code, doc = self.p.diff_json("--since-snapshot")
        self.assertEqual(doc["config"]["defines"], {"FAST": "1"})
        self.assertEqual(doc["result"]["verdict"], "INCREASE")
        code, doc = self.p.diff_json("--since-snapshot", "--define", "FAST", "--define", "X=2")
        self.assertEqual(doc["config"]["defines"], {"FAST": "1", "X": "2"})
        code, text = self.p.run("snapshot", "--list")
        self.assertIn("SNAPSHOT ", text)

    def test_non_git_snapshot_workflow(self):
        from tests.e2e_support import Project
        p = Project(FILES, git=False)
        self.addCleanup(p.close)
        code, text = p.run("snapshot")
        self.assertEqual(code, 0, text)
        code, out, _ = p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE")
        p.edit("rtl/alu.sv", "      2'd2: y <= x;\n", "")
        code, out, raw = p.diff()
        self.assertEqual(verdict(out), "DECREASE", raw)
        code, out, raw = p.diff("--since-snapshot")
        self.assertEqual(verdict(out), "DECREASE", raw)
        code, out, raw = p.diff("--since-snapshot", "nosuchid")
        self.assertEqual(code, 2)

    def test_before_after_directories(self):
        from tests.e2e_support import Project
        a = Project(FILES, git=False)
        b = Project(FILES, git=False)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.edit("rtl/dp.sv", "localparam DW = 16", "localparam DW = 20")
        code, out = self.p.run("diff", "--before", str(a.dir), "--after", str(b.dir))
        from tests.agent_grammar import parse
        self.assertIn(verdict(parse(out)), ("INCREASE", "UNCERTAIN"), out)
        self.assertIn("cause=parameter:DW", out)
        same = self.p.run("diff", "--before", str(a.dir), "--after", str(a.dir))[1]
        self.assertTrue(same.startswith("DELTA 0 GE (0..0) 0.0% verdict=NO_CHANGE"), same)


class VhdlEdits(GitCase):
    FILES = {
        "rtl/acc.vhd": """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity acc is
  generic (W : integer := 8);
  port (clk : in std_logic; a, b : in unsigned(W-1 downto 0); y : out unsigned(W-1 downto 0));
end entity;
architecture rtl of acc is
  constant K : integer := W;
  signal r : unsigned(K-1 downto 0);
begin
  process (clk) begin
    if rising_edge(clk) then r <= r + a + b; end if;
  end process;
  y <= r;
end architecture;
""",
        "rtl/top.vhd": """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity top is
  port (clk : in std_logic; a, b : in unsigned(15 downto 0); y : out unsigned(15 downto 0));
end entity;
architecture rtl of top is
begin
  u0 : entity work.acc generic map (W => 16) port map (clk => clk, a => a, b => b, y => y);
end architecture;
""",
    }

    def test_generic_override_is_the_cause(self):
        self.p.edit("rtl/top.vhd", "W => 16", "W => 24")
        self.p.edit("rtl/top.vhd", "unsigned(15 downto 0)", "unsigned(23 downto 0)")
        code, doc = self.p.diff_json()
        self.assertEqual(doc["result"]["verdict"], "INCREASE")
        causes = [c["cause"] for c in doc["result"]["contributors"] if c["cause"]]
        self.assertTrue(any(c["name"] == "W" and c["kind"] == "parameter" for c in causes), causes)

    def test_added_multiplier_vhdl(self):
        self.p.edit("rtl/acc.vhd", "r <= r + a + b;", "r <= r + a + b + resize(a * b, K);")
        code, out, raw = self.p.diff()
        self.assertEqual(verdict(out), "INCREASE", raw)
        self.assertGreater(delta(out), 100)

    def test_reformat_and_rename_vhdl_are_no_change(self):
        s = self.p.read("rtl/acc.vhd").replace(" r ", " acc_reg ").replace("r <=", "acc_reg <=").replace("y <= r;", "y <=   acc_reg ; -- out")
        s = s.replace("signal r :", "signal acc_reg :")
        self.p.write("rtl/acc.vhd", s)
        code, out, raw = self.p.diff()
        self.assertEqual(verdict(out), "NO_CHANGE", raw)

    def test_mixed_language_project(self):
        self.p.write("rtl/wrap.sv", "module wrap (input clk, input [15:0] a, b, output [15:0] y);\n"
                                    "  acc #(.W(16)) u (.clk(clk), .a(a), .b(b), .y(y));\nendmodule\n")
        self.p.commit()
        self.p.edit("rtl/acc.vhd", "r <= r + a + b;", "r <= r + a - b;")
        code, out, raw = self.p.diff("--top", "wrap")
        self.assertIn(verdict(out), ("NEGLIGIBLE", "NO_CHANGE", "INCREASE", "DECREASE", "UNCERTAIN"))


if __name__ == "__main__":
    unittest.main()
