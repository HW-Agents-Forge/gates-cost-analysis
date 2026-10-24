"""Regression tests for bugs found by applying realistic edits to real open-source designs
(picorv32, serv, verilog-uart/axis, ibex, common_cells, neorv32): every test is a minimal reduction."""
import json
import unittest

from tests.e2e_support import GitCase, Project, delta, verdict

HDR_WRAP = """\
`include "wrap.vh"
module wrap (input clk, input [`WW-1:0] d, output [`WW-1:0] q);
  reg [`WW-1:0] acc;
  always @(posedge clk) acc <= acc + d;
  assign q = acc;
endmodule
"""
TOML = '[project]\ntop = "wrap"\nsources = ["rtl/**/*.v"]\n'


class HeaderEdit(GitCase):
    """An edit of a header found next to the including file is an edit of the design, whatever the
    `sources` globs of the project file select (picorv32 wrapper with `define WW in wrap.vh)."""

    FILES = {"rtl/wrap.v": HDR_WRAP, "rtl/wrap.vh": "`define WW 8\n", "gatecost.toml": TOML}

    def test_git_uncommitted_header_edit_is_seen(self):
        self.p.write("rtl/wrap.vh", "`define WW 16\n")
        code, j = self.p.diff_json()
        self.assertEqual(code, 0)
        r = j["result"]
        self.assertEqual(r["verdict"], "INCREASE")
        self.assertGreater(r["delta"]["ge"]["typ"], 0)
        # the before side must see the old header too: no spurious include diagnostics
        codes = {d["code"] for d in j.get("diagnostics", [])}
        self.assertNotIn("INCLUDE_NOT_FOUND", codes)
        self.assertEqual(r["diagnostics_delta"]["new"] + r["diagnostics_delta"]["resolved"], [])

    def test_base_ref_header_edit_is_seen_and_reverse_is_symmetric(self):
        self.p.write("rtl/wrap.vh", "`define WW 16\n")
        self.p.commit("widen")
        code, fwd = self.p.diff_json("--base-ref", "HEAD~1")
        self.assertEqual(fwd["result"]["verdict"], "INCREASE")
        self.p.write("rtl/wrap.vh", "`define WW 8\n")
        code, back = self.p.diff_json("--base-ref", "HEAD")
        self.assertEqual(back["result"]["verdict"], "DECREASE")
        self.assertAlmostEqual(back["result"]["delta"]["ge"]["typ"], -fwd["result"]["delta"]["ge"]["typ"], places=2)

    def test_cache_key_covers_headers_next_to_the_includer(self):
        """A warm cache must not answer for a tree whose adjacent header differs."""
        files = {"rtl/wrap.v": HDR_WRAP, "rtl/wrap.vh": "`define WW 8\n"}
        before = Project(files, git=False)
        after = Project({**files, "rtl/wrap.vh": "`define WW 16\n"}, git=False)
        self.addCleanup(before.close)
        self.addCleanup(after.close)
        tot = []
        for proj in (before, after):          # same cache directory, one after the other
            proj.cache = before.cache
            code, text = proj.run("cost", "--format", "json", "--no-meta", "--top", "wrap")
            tot.append(json.loads(text)["result"]["total"]["ge"]["typ"])
        self.assertGreater(tot[1], tot[0])
        code, text = before.run("diff", "--format", "json", "--no-meta", "--top", "wrap",
                                "--before", str(before.dir), "--after", str(after.dir))
        r = json.loads(text)["result"]
        self.assertEqual(r["verdict"], "INCREASE")
        self.assertAlmostEqual(r["delta"]["ge"]["typ"], tot[1] - tot[0], places=2)


class MatchOpsTieBreak(unittest.TestCase):
    """Operations sharing a role path carry a "#i" suffix ordered by label-dependent shape hashes; renaming an
    instance or signal permutes the suffixes of picorv32's control-state PRIORITY/MUX ops.  Identical operations
    must still pair with each other (a rename is an exact zero delta)."""

    @staticmethod
    def _op(i, width, typ, suffix):
        from gatecost.result import Ge3
        from gatecost.diff.engine import CostOp

        key = ("m", "tgt", "case0" + suffix, "PRIORITY")
        return CostOp(id=i, op="PRIORITY", category="select", quality="good", cost=Ge3(typ - 1, typ, typ + 1), sigma=1,
                      file="a.v", line=10, named_key=key, declared_widths=(width,), effective_widths=(width,))

    def test_permuted_suffixes_pair_identical_ops(self):
        from gatecost.diff.align import Aligner
        from gatecost.diff.engine import match_ops

        before = [self._op(1, 1, 2000, ""), self._op(2, 2, 4000, "#1")]
        after = [self._op(1, 2, 4000, ""), self._op(2, 1, 2000, "#1")]
        pairs, rem_b, rem_a = match_ops(before, after, Aligner(None, None))
        self.assertEqual((rem_b, rem_a), ([], []))
        for ob, oa, _tier in pairs:
            self.assertEqual(ob.cost, oa.cost)
            self.assertEqual(ob.effective_widths, oa.effective_widths)

    def test_real_change_in_a_group_still_pairs_by_key(self):
        from gatecost.diff.align import Aligner
        from gatecost.diff.engine import match_ops

        before = [self._op(1, 1, 2000, ""), self._op(2, 2, 4000, "#1")]
        after = [self._op(1, 1, 2000, ""), self._op(2, 3, 6000, "#1")]
        pairs, rem_b, rem_a = match_ops(before, after, Aligner(None, None))
        self.assertEqual((rem_b, rem_a), ([], []))
        self.assertEqual(sorted((ob.id, oa.id) for ob, oa, _ in pairs), [(1, 1), (2, 2)])


class BrokenEdit(GitCase):
    """An edit that breaks the sources must not be reported as a confident "no change"."""

    FILES = {"rtl/c.v": "module c(input clk, input [7:0] d, output reg [7:0] q);\n"
                        "  reg [7:0] t;\n  always @(posedge clk) begin t <= d; q <= t; end\nendmodule\n"}

    def test_parse_error_lowers_confidence(self):
        self.p.edit("rtl/c.v", "reg [7:0] t;", "reg [7:0] t")
        code, j = self.p.diff_json()
        r = j["result"]
        self.assertEqual(r["verdict"], "NO_CHANGE")
        self.assertEqual(r["confidence"]["level"], "low")
        self.assertTrue(any("PARSE_ERROR" in x for x in r["confidence"]["reasons"]))

    def test_repairing_a_parse_error_lowers_confidence(self):
        self.p.edit("rtl/c.v", "reg [7:0] t;", "reg [7:0] t")
        self.p.commit("broken")
        self.p.edit("rtl/c.v", "reg [7:0] t\n", "reg [7:0] t;\n")
        code, j = self.p.diff_json()
        self.assertEqual(j["result"]["confidence"]["level"], "low")

    def test_clean_edit_keeps_high_confidence(self):
        self.p.edit("rtl/c.v", "reg [7:0] t;", "reg [7:0] t;  // comment")
        code, j = self.p.diff_json()
        self.assertEqual(j["result"]["verdict"], "NO_CHANGE")
        self.assertEqual(j["result"]["confidence"]["level"], "high")


if __name__ == "__main__":
    unittest.main()
