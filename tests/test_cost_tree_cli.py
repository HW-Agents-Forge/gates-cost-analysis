"""`gatecost cost` end to end with a stubbed elaborator, filelists, budgets, errors."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gatecost.cli import cmd_cost, main
from gatecost.cost import cost_op, load_profile
from gatecost.ir import Builder, Design, analyze_ir
from gatecost.report.validate import validate_json
from tests.ir_dsl import Mod

P = load_profile()
CALLS = []


def fake_design():
    c = Mod("child")
    a, b = c.inp("a", 16), c.inp("b", 16)
    c.out("y", a.mul(b, 32))
    child = c.done()
    t = Builder("top")
    x, y = t.port("x", "in", 16), t.port("y", "in", 16)
    o = t.port("o", "out", 32)
    r = t.inst(child.key, "u0", {"a": x.sig, "b": y.sig}, {"y": 32}, count=2)
    t.connect(o, r["y"])
    top = t.finalize()
    analyze_ir(top)
    d = Design(top.key)
    d.add(top)
    d.add(child)
    return d


def fake_elab(files, *, root, top, defines, incdirs, limits, side):
    CALLS.append(dict(files=files, root=root, top=top, defines=defines, incdirs=incdirs))
    d = fake_design()
    if top not in (None, "top"):
        return SimpleNamespace(design=d, top_key=None, tops=[], diagnostics=[], assumptions=[], parsed_hashes={})
    return SimpleNamespace(design=d, top_key="top", tops=["top"], diagnostics=[], assumptions=["parameter X unresolved"],
                           parsed_hashes={})


def run(*argv, cwd):
    out = io.StringIO()
    old = os.getcwd()
    try:
        os.chdir(cwd)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(argv))
    finally:
        os.chdir(old)
    return code, out.getvalue()


class CostCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        (self.dir / ".git").mkdir()
        (self.dir / "rtl").mkdir()
        (self.dir / "rtl" / "a.v").write_text("module top; endmodule\n")
        (self.dir / "rtl" / "b.sv").write_text("module child; endmodule\n")
        CALLS.clear()
        p = mock.patch.object(cmd_cost, "get_elaborator", lambda: fake_elab)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.mul = cost_op("MUL", {"n": 16, "m": 16}, P)

    def test_agent(self):
        code, out = run("cost", cwd=self.dir)
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith(f"COST {round(2 * self.mul.typ / 1000)} GE"), out)
        self.assertIn("TOP top", lines)
        self.assertTrue(any(l.endswith("child x2 50%") for l in lines), out)
        self.assertIn("ASSUMED parameter X unresolved", lines)
        self.assertEqual(len(CALLS[0]["files"]), 2)

    def test_json_valid_and_ops(self):
        code, out = run("cost", "--format", "json", "--detail", "ops", "--no-meta", cwd=self.dir)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(validate_json(doc), [])
        r = doc["result"]
        self.assertEqual(r["total"]["ge"]["typ"], round(2 * self.mul.typ / 1000, 3))
        self.assertEqual(r["operations"][0]["description"], "MUL 16x16")
        self.assertEqual(r["operations"][0]["instances"], 2)
        self.assertEqual(doc["config"]["sources"], 2)

    def test_formats_and_by(self):
        for fmt in ("text", "markdown"):
            code, out = run("cost", "--format", fmt, cwd=self.dir)
            self.assertEqual(code, 0)
            self.assertIn("child", out)
        code, out = run("cost", "--by", "file", cwd=self.dir)
        self.assertEqual(code, 0)
        code, out = run("cost", "--by", "category", cwd=self.dir)
        self.assertNotIn("child x2", out)

    def test_budget(self):
        code, out = run("cost", "--budget", "100", cwd=self.dir)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("COST "))
        self.assertIn("BUDGET_EXCEEDED", out)
        code, _ = run("cost", "--budget", "1000000", cwd=self.dir)
        self.assertEqual(code, 0)

    def test_no_sources(self):
        empty = self.dir / "empty"
        empty.mkdir()
        (empty / "gatecost.toml").write_text("")
        code, out = run("cost", cwd=empty)
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("ERROR NO_SOURCES"))

    def test_top_not_found_and_top_passed(self):
        code, out = run("cost", "--top", "nope", cwd=self.dir)
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("ERROR TOP_NOT_FOUND"))
        code, _ = run("cost", "--top", "top", "--define", "W=8", "--incdir", "inc", cwd=self.dir)
        self.assertEqual(CALLS[-1]["top"], "top")
        self.assertEqual(CALLS[-1]["defines"]["W"], "8")

    def test_paths_and_filelist(self):
        (self.dir / "rtl" / "list.f").write_text(
            "// c\n+incdir+inc +define+A=2+B\n-f sub.f\nb.sv # trailing\n")
        (self.dir / "rtl" / "sub.f").write_text("a.v\n")
        code, _ = run("cost", "--filelist", "rtl/list.f", cwd=self.dir)
        self.assertEqual(code, 0)
        c = CALLS[-1]
        self.assertEqual(sorted(f.name for f in c["files"]), ["a.v", "b.sv"])
        self.assertEqual(c["defines"], {"A": "2", "B": "1"})
        self.assertTrue(c["incdirs"][0].endswith("inc"))
        run("cost", "rtl/a.v", cwd=self.dir)
        self.assertEqual([f.name for f in CALLS[-1]["files"]], ["a.v"])

    def test_config_sources_and_defines(self):
        (self.dir / "gatecost.toml").write_text('[project]\nsources=["rtl/*.v"]\ndefines={X="3"}\ntop="top"\n')
        run("cost", cwd=self.dir)
        self.assertEqual([f.name for f in CALLS[-1]["files"]], ["a.v"])
        self.assertEqual(CALLS[-1]["defines"], {"X": "3"})

    def test_ambiguous_tops(self):
        def two(files, *, root, top, **kw):
            r = fake_elab(files, root=root, top=top if top == "top" else None, **kw)
            r.tops = ["top", "alt"]
            r.top_key = None if top is None else "top"
            if top == "alt":
                r.top_key = "top"
            return r
        with mock.patch.object(cmd_cost, "get_elaborator", lambda: two):
            code, out = run("cost", cwd=self.dir)
        self.assertEqual(code, 0, out)
        self.assertIn("multiple top candidates", out)

    def test_diag_makes_partial(self):
        from gatecost.result import Diagnostic

        def withdiag(files, **kw):
            r = fake_elab(files, **kw)
            r.diagnostics = [Diagnostic("UNSUPPORTED_CONSTRUCT", "while loop", "warning", True, "after", "rtl/a.v", 3)]
            return r
        with mock.patch.object(cmd_cost, "get_elaborator", lambda: withdiag):
            code, out = run("cost", cwd=self.dir)
        self.assertIn("WARN UNSUPPORTED_CONSTRUCT rtl/a.v:3", out)
        self.assertIn("confidence=low", out)


if __name__ == "__main__":
    unittest.main()


class RtlWrap(unittest.TestCase):
    def test_verilog_wrap(self):
        from gatecost.rtl_estimate import wrap_verilog
        w = wrap_verilog("assign y = a*b + c;", {"a": 16, "b": 16, "c": 32})
        self.assertEqual((w.inputs, w.outputs), ({"a": 16, "b": 16, "c": 32}, {"y": 32}))
        w = wrap_verilog("always @(posedge clk) q <= en ? d : q;", {"d": 32})
        self.assertEqual(w.inputs["clk"], 1)
        self.assertIn("output logic [31:0] q", w.source)
        self.assertTrue(any("input en width not given" in a for a in w.assumed))
        w = wrap_verilog("q <= d;", {"d": 4})
        self.assertIn("always @(posedge clk)", w.source)
        w = wrap_verilog("y = a & b;", {"a": 8, "b": 8, "y": 8})
        self.assertIn("assign y = a & b;", w.source)

    def test_vhdl_wrap(self):
        from gatecost.rtl_estimate import wrap_vhdl
        w = wrap_vhdl("y <= a + b;", {"a": 16, "b": 16})
        self.assertIn("y : out unsigned(15 downto 0)", w.source)
        w = wrap_vhdl("process(clk) begin if rising_edge(clk) then q <= d; end if; end process;", {"d": 8})
        self.assertIn("clk : in std_logic", w.source)

    def test_estimate_rtl_with_stub(self):
        seen = {}

        def elab(files, **kw):
            seen["text"] = files[0].read_text()
            return fake_elab(files, **{**kw, "top": None})
        from gatecost.rtl_estimate import estimate_fragment
        r = estimate_fragment("assign y = a*b;", {"a": 16, "b": 16}, "verilog", P, elaborate=elab)
        self.assertIn("module gatecost_fragment", seen["text"])
        self.assertEqual(r.kind, "rtl")
        self.assertEqual(r.ge.typ, 2 * cost_op("MUL", {"n": 16, "m": 16}, P).typ)
