"""Environment variables in filelists (D5): $VAR, ${VAR}, $(VAR); undefined variables are one clear diagnostic."""
import os
import unittest
from unittest import mock

from gatecost.envsub import expand_env
from tests.test_vhdl_real_cli import Base, codes, run_cost

SRC = "module t(input a, output y); assign y = ~a; endmodule\n"


class ExpandEnv(unittest.TestCase):
    ENV = {"HOME_X": "/p", "A_1": "q"}

    def test_three_forms(self):
        for t in ("$HOME_X/r.v", "${HOME_X}/r.v", "$(HOME_X)/r.v"):
            self.assertEqual(expand_env(t, self.ENV), ("/p/r.v", ()))
        self.assertEqual(expand_env("$A_1$A_1.v", self.ENV), ("qq.v", ()))

    def test_undefined_is_reported_and_left_literal(self):
        self.assertEqual(expand_env("$NOPE/x ${NOPE}/y $(OTHER)", self.ENV),
                         ("$NOPE/x ${NOPE}/y $(OTHER)", ("NOPE", "OTHER")))

    def test_no_command_substitution(self):
        self.assertEqual(expand_env("$(rm -rf x)/a.v", self.ENV), ("$(rm -rf x)/a.v", ()))
        self.assertEqual(expand_env("$(echo $HOME_X)", self.ENV)[0], "$(echo /p)")


class BaselineFilelistEnv(unittest.TestCase):
    def test_baseline_parser_expands(self):
        from pathlib import Path

        from gatecost.baseline import parse_filelist
        with mock.patch.dict(os.environ, {"PRJ_X": "/abs"}):
            files, incs, nested = parse_filelist("$PRJ_X/a.v\n+incdir+${PRJ_X}/inc\n-f $(PRJ_X)/n.f\n", Path("/b"), Path("/b"))
        self.assertEqual((files, incs, nested), ([Path("/abs/a.v")], [Path("/abs/inc")], [Path("/abs/n.f")]))


class FilelistEnv(Base):
    def test_defined_variable_forms(self):
        self.write("rtl/t.sv", SRC)
        for i, form in enumerate(("$PRJ_X/rtl/t.sv", "${PRJ_X}/rtl/t.sv", "$(PRJ_X)/rtl/t.sv")):
            self.write(f"f{i}.f", form + "\n")
            with mock.patch.dict(os.environ, {"PRJ_X": str(self.dir)}):
                code, doc = run_cost("--top", "t", "--filelist", f"f{i}.f", cwd=self.dir)
            self.assertEqual(code, 0, doc.get("error"))
            self.assertNotIn("FILE_NOT_FOUND", codes(doc))
            self.assertGreater(doc["result"]["total"]["ge"]["typ"], 0)

    def test_variable_in_incdir_and_nested_filelist(self):
        self.write("rtl/t.sv", '`include "h.vh"\n' + SRC)
        self.write("rtl/h.vh", "`define X 1\n")
        self.write("rtl/inner.f", "t.sv\n")
        self.write("top.f", "+incdir+$PRJ_X/rtl\n-f $PRJ_X/rtl/inner.f\n")
        with mock.patch.dict(os.environ, {"PRJ_X": str(self.dir)}):
            code, doc = run_cost("--top", "t", "--filelist", "top.f", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertEqual([c for c in codes(doc) if c in ("FILE_NOT_FOUND", "SOURCE_NOT_FOUND")], [])

    def test_undefined_variable_is_one_diagnostic_not_one_per_file(self):
        self.write("t.sv", SRC)
        self.write("top.f", "$NO_SUCH_PRJ_VAR/a.sv\n$NO_SUCH_PRJ_VAR/b.sv\n${NO_SUCH_PRJ_VAR}/c.sv\nt.sv\n")
        env = {k: v for k, v in os.environ.items() if k != "NO_SUCH_PRJ_VAR"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, doc = run_cost("--top", "t", "--filelist", "top.f", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        msgs = [d["message"] for d in doc["diagnostics"] if d["code"] == "FILE_NOT_FOUND"]
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("NO_SUCH_PRJ_VAR", msgs[0])
        self.assertIn("3 entries", msgs[0])
        self.assertGreater(doc["result"]["total"]["ge"]["typ"], 0)


if __name__ == "__main__":
    unittest.main()
