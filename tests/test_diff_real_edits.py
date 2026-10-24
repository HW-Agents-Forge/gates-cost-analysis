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


if __name__ == "__main__":
    unittest.main()
