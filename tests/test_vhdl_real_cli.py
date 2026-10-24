"""Regressions found by running the tool on real VHDL projects and by CLI robustness probing."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from gatecost.cli import main


def run_cost(*argv, cwd):
    out = io.StringIO()
    old = os.getcwd()
    try:
        os.chdir(cwd)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["cost", "--format", "json", *argv])
    finally:
        os.chdir(old)
    return code, json.loads(out.getvalue())


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._old_cache = os.environ.get("GATECOST_CACHE_DIR")
        os.environ["GATECOST_CACHE_DIR"] = str(self.dir / "_cache")

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("GATECOST_CACHE_DIR", None)
        else:
            os.environ["GATECOST_CACHE_DIR"] = self._old_cache
        self._tmp.cleanup()

    def write(self, name, text):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p


def codes(doc):
    return [d["code"] for d in doc["diagnostics"]]


class UndefinedMacroWithArgs(Base):
    def test_one_diagnostic_no_cascade(self):
        self.write("t.sv", "module t(input logic [3:0] a, output logic [3:0] y);\n"
                           "  `DV_FCOV_SIGNAL(logic, foo, a[0] & a[1])\n"
                           "  `DV_FCOV_SIGNAL (int, bar, (a[2]))\n"
                           "  assign y = ~a;\nendmodule\n")
        code, doc = run_cost("--top", "t", "t.sv", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        cs = codes(doc)
        self.assertEqual(cs.count("UNDEFINED_MACRO"), 2, doc["diagnostics"])
        self.assertNotIn("PARSE_ERROR", cs)
        self.assertGreater(doc["result"]["total"]["ge"]["typ"], 0)

    def test_undefined_macro_without_args_still_one(self):
        self.write("t.sv", "module t(input a, output y);\n  `NOPE\n  assign y = a;\nendmodule\n")
        code, doc = run_cost("--top", "t", "t.sv", cwd=self.dir)
        self.assertEqual(codes(doc).count("UNDEFINED_MACRO"), 1)
        self.assertNotIn("PARSE_ERROR", codes(doc))


class NestedFilelist(Base):
    def test_missing_nested_filelist_warns(self):
        self.write("t.sv", "module t(input a, output y); assign y = ~a; endmodule\n")
        self.write("top.f", "-F missing/inner.f\nt.sv\n")
        code, doc = run_cost("--top", "t", "--filelist", "top.f", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertIn("SOURCE_NOT_FOUND", codes(doc))
        self.assertGreater(doc["result"]["total"]["ge"]["typ"], 0)

    def test_missing_top_level_filelist_still_error(self):
        self.write("t.sv", "module t(input a, output y); assign y = ~a; endmodule\n")
        code, doc = run_cost("--filelist", "nope.f", cwd=self.dir)
        self.assertNotEqual(code, 0)


class NoSourcesMessage(Base):
    def test_message_names_path(self):
        (self.dir / "empty").mkdir()
        code, doc = run_cost("empty", cwd=self.dir)
        self.assertEqual(doc["error"]["code"], "NO_SOURCES")
        self.assertIn("empty", doc["error"]["message"])


class Utf16Sources(Base):
    def test_utf16_verilog_and_vhdl(self):
        v = "module u(input a, output y); assign y = ~a; endmodule\n"
        (self.dir / "u.v").write_bytes(v.encode("utf-16"))
        h = ("library ieee; use ieee.std_logic_1164.all;\nentity w is port (a : in std_logic; y : out std_logic); end entity;\n"
             "architecture r of w is begin y <= not a; end architecture;\n")
        (self.dir / "w.vhd").write_bytes(h.encode("utf-16-be"))
        (self.dir / "w.vhd").write_bytes(b"\xfe\xff" + h.encode("utf-16-be"))
        for top, f in (("u", "u.v"), ("w", "w.vhd")):
            code, doc = run_cost("--top", top, f, cwd=self.dir)
            self.assertEqual(code, 0, doc.get("error"))
            self.assertGreater(doc["result"]["total"]["ge"]["typ"], 0, top)


if __name__ == "__main__":
    unittest.main()
