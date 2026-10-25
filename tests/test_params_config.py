"""Top-level parameters (--param, [project.params]), per-side project files in diff, per-side include
directories in --before/--after runs, and the array-port flattening diagnostic.

CLI tests run the launcher as a subprocess with an isolated cache directory (never the repository's)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gatecost.elab.project import verilog_param_value
from gatecost.params import Lit, canon, parse_assignment, parse_value

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "gatecost"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
HAVE_GIT = shutil.which("git") is not None

ADDER = """\
module top #(parameter W, parameter real R = 1.0, parameter S = "ab", parameter EN = 0)
  (input [W-1:0] a, b, output [W-1:0] y);
  assign y = a + b;
endmodule
"""

VHDL_ADDER = """\
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity ent is generic (Width : natural := 4; Fast : boolean := false; Name : string := "x");
  port (a, b : in unsigned(Width-1 downto 0); y : out unsigned(Width-1 downto 0));
end entity;
architecture rtl of ent is begin
  y <= a + b;
end architecture;
"""


class Sandbox:
    """A temporary directory tree plus an isolated cache directory."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="gc_params_")
        self.dir = Path(self._tmp.name).resolve()
        self.cache = self.dir.parent / (self.dir.name + "_cache")
        self.env = {**os.environ, **GIT_ENV, "GATECOST_CACHE_DIR": str(self.cache)}
        self.env.pop("GATECOST_FORMAT", None)
        self.env.pop("GATECOST_PROFILE", None)

    def close(self):
        self._tmp.cleanup()
        shutil.rmtree(self.cache, ignore_errors=True)

    def write(self, rel: str, text: str):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def run(self, *argv: str, cwd: Path | None = None):
        r = subprocess.run([sys.executable, str(LAUNCHER), *argv], cwd=cwd or self.dir, env=self.env,
                           capture_output=True, text=True, timeout=300)
        return r.returncode, r.stdout

    def json(self, *argv: str, cwd: Path | None = None):
        code, out = self.run(*argv, "--format", "json", "--no-meta", cwd=cwd)
        try:
            return code, json.loads(out)
        except ValueError:
            raise AssertionError(f"not JSON ({code}): {out!r}") from None

    def git(self, *args: str):
        r = subprocess.run(["git", *args], cwd=self.dir, env=self.env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout


def typ(doc) -> float:
    return doc["result"]["total"]["ge"]["typ"]


def diag_codes(doc) -> list[str]:
    return [d["code"] for d in doc["diagnostics"]]


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)


class ParamParsing(unittest.TestCase):
    def test_value_grammar(self):
        cases = {"8": 8, "-3": -3, "0x1f": 31, "0b1010": 10, "1_000": 1000, "007": 7, "true": True, "False": False,
                 "1.5": 1.5, "1e3": 1000.0, "abc": "abc", '"123"': "123", "'x y'": "x y"}
        for text, want in cases.items():
            got = parse_value(text)
            self.assertEqual(got, want, text)
            self.assertIs(type(got), type(want), text)
        lit = parse_value("8'h1f")
        self.assertEqual((lit.value, lit.width, lit.signed), (31, 8, False))
        lit = parse_value("4'sb1010")
        self.assertEqual((lit.value, lit.width, lit.signed), (10, 4, True))
        self.assertEqual(parse_value("'d5").width, 32)
        self.assertEqual(parse_value("4'hff").value, 15)         # truncated to the width

    def test_bad_values(self):
        for text in ("", "0x_", "8'h_"):
            with self.assertRaises(ValueError, msg=text):
                parse_value(text)
        for item in ("W", "1a=3", "=3", "a b=3", "W="):
            with self.assertRaises(ValueError, msg=item):
                parse_assignment(item)

    def test_canon_distinguishes_types(self):
        self.assertNotEqual(canon({"W": 1}), canon({"W": True}))
        self.assertNotEqual(canon({"W": 1}), canon({"W": 1.0}))
        self.assertNotEqual(canon({"W": "1"}), canon({"W": 1}))
        self.assertNotEqual(canon({"W": parse_value("8'h1")}), canon({"W": 1}))
        self.assertEqual(canon({"A": 1, "B": 2}), canon({"B": 2, "A": 1}))

    def test_elaboration_values_are_typed(self):
        v = verilog_param_value(5)
        self.assertEqual((v.sig.const_value().value, v.w, v.signed, v.real), (5, 32, True, None))
        v = verilog_param_value(2.5)
        self.assertEqual((v.real, v.sig.const_value().value), (2.5, 2))      # real values keep the real
        v = verilog_param_value("ab")
        self.assertEqual((v.sig.const_value().value, v.w), (0x6162, 16))
        v = verilog_param_value(Lit(0x1f, 8, False, "8'h1f"))
        self.assertEqual((v.sig.const_value().value, v.w, v.signed), (31, 8, False))
        v = verilog_param_value(True)
        self.assertEqual(v.sig.const_value().value, 1)
        self.assertEqual(verilog_param_value(-1).sig.const_value().as_signed(), -1)


class ParamsCli(SandboxCase):
    def setUp(self):
        super().setUp()
        self.sb.write("rtl/top.v", ADDER)

    def cost(self, *extra):
        return self.sb.json("cost", "--top", "top", *extra)

    def test_param_sets_the_top_parameter(self):
        _, d0 = self.cost()
        self.assertIn("UNRESOLVED_PARAM", diag_codes(d0))               # no default: 32 assumed
        _, d8 = self.cost("--param", "W=8")
        _, d16 = self.cost("--param", "W=16")
        self.assertNotIn("UNRESOLVED_PARAM", diag_codes(d8))
        self.assertLess(typ(d8), typ(d16))
        self.assertLess(typ(d16), typ(d0))
        self.assertEqual(d8["config"]["params"], {"W": 8})
        self.assertEqual(d0["config"]["params"], {})

    def test_value_forms_agree(self):
        base = typ(self.cost("--param", "W=16")[1])
        for form in ("0x10", "0b10000", "1_6", "16'd16", "'d16"):
            self.assertEqual(typ(self.cost("--param", f"W={form}")[1]), base, form)
        self.assertEqual(typ(self.cost("--param", "W=8'h08")[1]), typ(self.cost("--param", "W=8")[1]))
        # a sized literal is shown as written
        self.assertEqual(self.cost("--param", "W=8'h08")[1]["config"]["params"], {"W": "8'h08"})

    def test_real_string_and_boolean_values(self):
        code, doc = self.cost("--param", "W=8", "--param", "R=2.5", "--param", "S=hello", "--param", "EN=true")
        self.assertEqual(code, 0)
        self.assertEqual(doc["config"]["params"], {"W": 8, "R": 2.5, "S": "hello", "EN": True})
        self.assertEqual(diag_codes(doc), [])
        code, doc = self.cost("--param", "W=8", "--param", 'S="123"')
        self.assertEqual(doc["config"]["params"]["S"], "123")

    def test_unknown_parameter_is_reported_and_ignored(self):
        _, ok = self.cost("--param", "W=8")
        code, doc = self.cost("--param", "W=8", "--param", "bogus=1")
        self.assertEqual(code, 0)
        self.assertEqual(typ(doc), typ(ok))
        msgs = [d["message"] for d in doc["diagnostics"] if d["code"] == "UNKNOWN_PARAMETER"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("unknown parameter 'bogus' of top 'top'", msgs[0])
        for known in ("W", "R", "S", "EN"):
            self.assertIn(known, msgs[0])
        self.assertEqual(doc["status"], "partial")
        code, text = self.sb.run("cost", "--top", "top", "--param", "W=8", "--param", "bogus=1")
        self.assertIn("WARN UNKNOWN_PARAMETER", text)

    def test_bad_arguments(self):
        for item in ("W", "1a=3", "=3", "W="):
            code, text = self.sb.run("cost", "--param", item)
            self.assertEqual((code, text.split()[:2]), (2, ["ERROR", "BAD_ARGUMENT"]), item)

    def test_toml_params_and_command_line_wins(self):
        self.sb.write("gatecost.toml", '[project]\ntop = "top"\n[project.params]\nW = 8\nS = "xy"\nR = 0.5\nEN = true\n')
        _, d = self.sb.json("cost")
        self.assertEqual(d["config"]["params"], {"EN": True, "R": 0.5, "S": "xy", "W": 8})
        self.assertEqual(typ(d), typ(self.cost("--param", "W=8")[1]))
        _, d16 = self.sb.json("cost", "--param", "W=16")
        self.assertEqual(d16["config"]["params"]["W"], 16)
        self.assertEqual(d16["config"]["params"]["S"], "xy")            # merged, not replaced
        self.assertEqual(typ(d16), typ(self.cost("--param", "W=16")[1]))

    def test_bad_toml_params(self):
        for body in ('W = [1, 2]', '"1a" = 3', 'W = { x = 1 }', 'W = ""\nbad = 2024-01-01'):
            self.sb.write("gatecost.toml", f"[project.params]\n{body}\n")
            code, text = self.sb.run("cost")
            self.assertEqual((code, text.split()[:2]), (2, ["ERROR", "BAD_CONFIG"]), body)
        self.sb.write("gatecost.toml", "[project]\nparams = 3\n")
        code, text = self.sb.run("cost")
        self.assertEqual((code, text.split()[:2]), (2, ["ERROR", "BAD_CONFIG"]))

    def test_every_cache_key_includes_the_parameters(self):
        # same cache directory, no --no-cache: results of different values must never be shared
        totals = [typ(self.cost("--param", f"W={w}")[1]) for w in (8, 16, 8, 16, 24)]
        self.assertEqual(totals[0], totals[2])
        self.assertEqual(totals[1], totals[3])
        self.assertEqual(len({totals[0], totals[1], totals[4]}), 3)
        # type matters too: the integer 1 and the boolean true are different values
        a = self.cost("--param", "W=8", "--param", "EN=1")[1]
        b = self.cost("--param", "W=8", "--param", "EN=true")[1]
        self.assertEqual(a["config"]["params"]["EN"], 1)
        self.assertIs(b["config"]["params"]["EN"], True)

    def test_vhdl_generics_case_insensitive(self):
        sb = self.sb
        sb.write("rtl/ent.vhd", VHDL_ADDER)
        sb.run("clean")
        _, d4 = sb.json("cost", "--top", "ent")
        _, d8 = sb.json("cost", "--top", "ent", "--param", "width=8")
        _, d16 = sb.json("cost", "--top", "ent", "--param", "WIDTH=16", "--param", "fast=true", "--param", "NAME=abc")
        self.assertLess(typ(d4), typ(d8))
        self.assertLess(typ(d8), typ(d16))
        self.assertEqual(diag_codes(d16), [])
        _, d = sb.json("cost", "--top", "ent", "--param", "Nope=1")
        msg = next(x["message"] for x in d["diagnostics"] if x["code"] == "UNKNOWN_PARAMETER")
        self.assertIn("unknown parameter 'Nope' of top 'ent'", msg)
        self.assertIn("width", msg)
        self.assertEqual(typ(d), typ(d4))

    def test_params_apply_without_top_to_the_single_root(self):
        self.sb.write("gatecost.toml", "[project.params]\nW = 8\n")
        _, d = self.sb.json("cost")
        self.assertEqual(typ(d), typ(self.cost("--param", "W=8")[1]))


@unittest.skipUnless(HAVE_GIT, "git is required")
class DiffUsesTheBaselineProjectFile(SandboxCase):
    TOML = '[project]\ntop = "top"\n[project.params]\nW = {w}\n'

    def setUp(self):
        super().setUp()
        sb = self.sb
        sb.write("rtl/top.v", ADDER)
        sb.write("gatecost.toml", self.TOML.format(w=8))
        sb.git("init", "-q", "-b", "main")
        sb.git("add", "-A")
        sb.git("commit", "-qm", "init")

    def mismatch(self, doc):
        return [d for d in doc["diagnostics"] if d["code"] == "CONFIG_MISMATCH"] + \
               [d for d in doc["result"].get("diagnostics_delta", {}).get("new", []) if d["code"] == "CONFIG_MISMATCH"]

    def test_unchanged_file_is_not_a_mismatch(self):
        code, doc = self.sb.json("diff")
        self.assertEqual(doc["result"]["verdict"], "NO_CHANGE")
        self.assertEqual(self.mismatch(doc), [])
        self.sb.write("rtl/top.v", ADDER.replace("a + b", "a - b"))
        code, doc = self.sb.json("diff")
        self.assertEqual(self.mismatch(doc), [])
        self.assertEqual(doc["config"]["params"], {"W": 8})

    def test_edited_params_between_base_and_working_tree(self):
        self.sb.write("gatecost.toml", self.TOML.format(w=16))
        code, doc = self.sb.json("diff")
        self.assertEqual(doc["result"]["verdict"], "INCREASE")          # not NO_CHANGE: the base used W=8
        mm = self.mismatch(doc)
        self.assertEqual(len(mm), 1, doc["diagnostics"])
        self.assertIn("params.W", mm[0]["message"])
        self.assertNotIn("defines", mm[0]["message"])
        self.assertEqual(doc["result"]["confidence"]["level"], "low")
        self.assertEqual(doc["config"]["params"], {"W": 16})            # the current configuration is reported
        code, text = self.sb.run("diff")
        self.assertIn("CONFIG_MISMATCH", text)

    def test_edited_defines_top_and_sources(self):
        sb = self.sb
        sb.write("rtl/top.v", "module top #(parameter W = 8)(input [W-1:0] a, b, output [W-1:0] y);\n"
                              "`ifdef WIDE\n  wire [2*W-1:0] t = a * b;\n  assign y = t[W-1:0];\n`else\n  assign y = a + b;\n`endif\n"
                              "endmodule\n")
        sb.write("gatecost.toml", '[project]\ntop = "top"\n')
        sb.git("add", "-A")
        sb.git("commit", "-qm", "ifdef")
        sb.write("gatecost.toml", '[project]\ntop = "top"\n[project.defines]\nWIDE = "1"\n')
        code, doc = sb.json("diff")
        self.assertEqual(doc["result"]["verdict"], "INCREASE")
        mm = self.mismatch(doc)
        self.assertEqual(len(mm), 1)
        self.assertIn("defines.WIDE", mm[0]["message"])
        self.assertEqual(doc["config"]["defines"], {"WIDE": "1"})
        # sources: a file the base did not compile
        sb.git("checkout", "-q", "gatecost.toml")
        sb.write("rtl/extra.v", "module extra(input a, b, output y); assign y = a & b; endmodule\n")
        sb.write("rtl/more.v", "module more(input [7:0] a, b, output [7:0] y); extra e(.a(a[0]), .b(b[0]), .y(y[0])); endmodule\n")
        sb.write("gatecost.toml", '[project]\ntop = "top"\nsources = ["rtl/top.v"]\n')
        sb.git("add", "-A")
        sb.git("commit", "-qm", "sources")
        sb.write("gatecost.toml", '[project]\ntop = "top"\nsources = ["rtl/*.v"]\n')
        code, doc = sb.json("diff")
        self.assertIn("sources", " ".join(d["message"] for d in self.mismatch(doc)))

    def test_new_project_file_counts_as_a_difference(self):
        sb = self.sb
        sb.git("rm", "-q", "gatecost.toml")
        sb.git("commit", "-qm", "no config")
        sb.write("gatecost.toml", self.TOML.format(w=16))
        code, doc = sb.json("diff")
        mm = self.mismatch(doc)
        self.assertEqual(len(mm), 1)
        self.assertIn("params.W", mm[0]["message"])

    def test_invalid_baseline_file_falls_back_with_a_warning(self):
        sb = self.sb
        sb.write("gatecost.toml", "[project\n")
        sb.git("add", "-A")
        sb.git("commit", "-qm", "broken")
        sb.write("gatecost.toml", self.TOML.format(w=8))
        code, doc = sb.json("diff")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.mismatch(doc)), 1)
        self.assertIn("cannot read the project file of the baseline", self.mismatch(doc)[0]["message"])

    def test_snapshot_mode_reads_the_snapshot_file(self):
        sb = self.sb
        code, out = sb.run("snapshot")
        self.assertEqual(code, 0, out)
        sb.write("gatecost.toml", self.TOML.format(w=16))
        code, doc = sb.json("diff", "--since-snapshot")
        self.assertEqual(doc["result"]["verdict"], "INCREASE")
        mm = self.mismatch(doc)
        self.assertEqual(len(mm), 1)
        self.assertIn("params.W", mm[0]["message"])
        sb.write("gatecost.toml", self.TOML.format(w=8))
        code, doc = sb.json("diff", "--since-snapshot")
        self.assertEqual(doc["result"]["verdict"], "NO_CHANGE")
        self.assertEqual(self.mismatch(doc), [])

    def test_snapshot_remembers_command_line_params(self):
        sb = self.sb
        sb.write("gatecost.toml", '[project]\ntop = "top"\n')
        code, out = sb.run("snapshot", "--param", "W=8")
        self.assertEqual(code, 0, out)
        sb.write("rtl/top.v", ADDER.replace("a + b", "a - b"))
        code, doc = sb.json("diff", "--since-snapshot")
        self.assertEqual(doc["config"]["params"], {"W": 8})
        self.assertEqual(self.mismatch(doc), [])
        self.assertNotIn("UNRESOLVED_PARAM", diag_codes(doc))
        code, doc = sb.json("diff", "--since-snapshot", "--param", "W=16")      # explicit: both sides
        self.assertEqual(doc["config"]["params"], {"W": 16})
        self.assertEqual(self.mismatch(doc), [])

    def test_param_given_on_the_command_line_applies_to_both_sides(self):
        code, doc = self.sb.json("diff", "--param", "W=24")
        self.assertEqual(self.mismatch(doc), [])
        self.assertEqual(doc["result"]["verdict"], "NO_CHANGE")


class DirectoryMode(SandboxCase):
    TOML = '[project]\ntop = "top"\n[project.params]\nW = {w}\n'

    def test_each_side_reads_its_own_project_file(self):
        sb = self.sb
        for side, w in (("old", 8), ("new", 16)):
            sb.write(f"{side}/rtl/top.v", ADDER)
            sb.write(f"{side}/gatecost.toml", self.TOML.format(w=w))
        code, doc = sb.json("diff", "--before", "old", "--after", "new")
        self.assertEqual(doc["result"]["verdict"], "INCREASE")
        msgs = [d["message"] for d in doc["result"]["diagnostics_delta"]["new"] if d["code"] == "CONFIG_MISMATCH"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("params.W", msgs[0])
        self.assertEqual(doc["config"]["params"], {"W": 16})

    def test_explicit_config_serves_both_sides(self):
        sb = self.sb
        for side, w in (("old", 8), ("new", 16)):
            sb.write(f"{side}/rtl/top.v", ADDER)
            sb.write(f"{side}/gatecost.toml", self.TOML.format(w=w))
        sb.write("shared.toml", self.TOML.format(w=12))
        code, doc = sb.json("diff", "--before", "old", "--after", "new", "--config", "shared.toml")
        self.assertEqual(doc["result"]["verdict"], "NO_CHANGE")
        self.assertEqual(doc["config"]["params"], {"W": 12})
        self.assertNotIn("CONFIG_MISMATCH", json.dumps(doc))

    def test_trees_without_project_file_use_the_working_directory_file(self):
        sb = self.sb
        for side in ("old", "new"):
            sb.write(f"{side}/rtl/top.v", ADDER)
        sb.write("old/rtl/top.v", ADDER.replace("a + b", "a - b"))
        sb.write("gatecost.toml", self.TOML.format(w=8))
        code, doc = sb.json("diff", "--before", "old", "--after", "new")
        self.assertEqual(doc["config"]["params"], {"W": 8})
        self.assertNotIn("CONFIG_MISMATCH", json.dumps(doc))

    def test_relative_incdir_is_resolved_per_side(self):
        sb = self.sb
        for side, w in (("old", 8), ("new", 16)):
            sb.write(f"{side}/rtl/t.v", '`include "defs.vh"\nmodule t(input [`W-1:0] a, b, output [`W-1:0] y);\n'
                                       "  assign y = a + b;\nendmodule\n")
            sb.write(f"{side}/inc/defs.vh", f"`define W {w}\n")
        # --root is the after tree: the header used to be read from it on both sides
        code, doc = sb.json("diff", "--before", "old", "--after", "new", "--root", "new", "--incdir", "inc")
        self.assertEqual(doc["result"]["verdict"], "INCREASE", doc)
        self.assertNotIn("UNDEFINED_MACRO", diag_codes(doc))
        self.assertNotIn("INCLUDE_NOT_FOUND", diag_codes(doc))
        # the same run from a directory that contains neither tree
        (sb.dir / "elsewhere").mkdir()
        code, doc = sb.json("diff", "--before", str(sb.dir / "old"), "--after", str(sb.dir / "new"),
                            "--root", str(sb.dir / "new"), "--incdir", "inc", cwd=sb.dir / "elsewhere")
        self.assertEqual(doc["result"]["verdict"], "INCREASE", doc)

    def test_relative_incdir_under_the_after_tree_is_still_rebased(self):
        sb = self.sb
        for side, w in (("old", 8), ("new", 16)):
            sb.write(f"{side}/rtl/t.v", '`include "defs.vh"\nmodule t(input [`W-1:0] a, b, output [`W-1:0] y);\n'
                                       "  assign y = a + b;\nendmodule\n")
            sb.write(f"{side}/inc/defs.vh", f"`define W {w}\n")
        code, doc = sb.json("diff", "--before", "old", "--after", "new", "--incdir", "new/inc")
        self.assertEqual(doc["result"]["verdict"], "INCREASE", doc)


class ArrayPortFlattening(SandboxCase):
    def test_flattening_is_information_only(self):
        src = (REPO / "tests" / "corpus" / "verilog" / "mac_array.sv").read_text(encoding="utf-8")
        self.sb.write("rtl/mac_array.sv", src)
        code, doc = self.sb.json("cost")
        self.assertEqual(code, 0)
        flat = [d for d in doc["diagnostics"] if "unpacked array port" in d["message"]]
        self.assertTrue(flat)
        for d in flat:
            self.assertEqual((d["code"], d["severity"], d["affects_cost"]), ("ARRAY_PORT_FLATTENED", "info", False))
        self.assertEqual([d for d in doc["diagnostics"] if d["code"] == "UNSUPPORTED_CONSTRUCT"], [])
        self.assertEqual(doc["status"], "ok")
        code, text = self.sb.run("cost")
        self.assertNotIn("WARN", text)

    def test_status_stays_partial_for_other_reasons(self):
        self.sb.write("rtl/t.v", "module t(input [3:0] a [2], output [3:0] y);\n  assign y = a[0] + a[1] + nothere(a[0]);\nendmodule\n")
        code, doc = self.sb.json("cost")
        self.assertEqual(doc["status"], "partial")
        self.assertIn("ARRAY_PORT_FLATTENED", diag_codes(doc))


if __name__ == "__main__":
    unittest.main()
