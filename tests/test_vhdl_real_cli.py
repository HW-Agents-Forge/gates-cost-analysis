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


OVERLOAD_PKG = """library ieee; use ieee.std_logic_1164.all;
package comp is
  function ffdre(q : std_logic; d : std_logic; rst : std_logic := '0'; en : std_logic := '1'; constant INIT : std_logic := '0') return std_logic;
  function ffdre(q : std_logic_vector; d : std_logic_vector; rst : std_logic := '0'; en : std_logic := '1'; constant INIT : std_logic_vector := (0 to 0 => '0')) return std_logic_vector;
end package;
package body comp is
  function ffdre(q : std_logic; d : std_logic; rst : std_logic := '0'; en : std_logic := '1'; constant INIT : std_logic := '0') return std_logic is
  begin
    return ((d and en) or (q and not en)) and not rst;
  end function;
  function ffdre(q : std_logic_vector; d : std_logic_vector; rst : std_logic := '0'; en : std_logic := '1'; constant INIT : std_logic_vector := (0 to 0 => '0')) return std_logic_vector is
    variable Result : std_logic_vector(q'range);
  begin
    for i in q'range loop
      Result(i) := ffdre(q => q(i), d => d(i), rst => rst, en => en, INIT => INIT(0));
    end loop;
    return Result;
  end function;
end package body;
library ieee; use ieee.std_logic_1164.all; use work.comp.all;
entity top is port (rst, en : in std_logic; q, d : in std_logic_vector(%d downto 0); y : out std_logic_vector(%d downto 0)); end entity;
architecture r of top is begin
  y <= ffdre(q => q, d => d, rst => rst, en => en);
end architecture;
"""


class VhdlNamedOverload(Base):
    def test_named_association_picks_scalar_overload(self):
        # the vector overload calls the scalar one with named actuals; it used to resolve to itself
        # (recursing to the inline-depth limit with exponential work)
        self.write("o.vhd", OVERLOAD_PKG % (15, 15))
        code, doc = run_cost("--top", "top", "o.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        cs = [d["message"] for d in doc["diagnostics"] if d["code"] in ("LIMIT_REACHED", "UNSUPPORTED_CONSTRUCT")]
        self.assertEqual(cs, [])
        self.assertGreater(doc["result"]["total"]["ge"]["typ"], 16)


RESIZE_SRC = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
package vec is
  type T_SLM is array (natural range <>, natural range <>) of std_logic;
  function resize(slm : T_SLM; size : positive) return T_SLM;
end package;
package body vec is
  function resize(slm : T_SLM; size : positive) return T_SLM is
    variable Result : T_SLM(size - 1 downto 0, slm'high(2) downto slm'low(2)) := (others => (others => '0'));
  begin
    for i in slm'range(1) loop
      for j in slm'high(2) downto slm'low(2) loop
        Result(i, j) := slm(i, j);
      end loop;
    end loop;
    return Result;
  end function;
end package body;
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all; use work.vec.all;
entity top is port (a : in unsigned(7 downto 0); b : in std_logic_vector(7 downto 0); y : out unsigned(15 downto 0); z : out std_logic_vector(11 downto 0)); end entity;
architecture r of top is begin
  y <= resize(a, 16) + 1;
  z <= std_logic_vector(resize(unsigned(b), 12) + 3);
end architecture;
"""


class UserOverloadDoesNotHideBuiltin(Base):
    def test_user_resize_on_matrix_type_leaves_numeric_std_resize(self):
        self.write("r.vhd", RESIZE_SRC)
        code, doc = run_cost("--top", "top", "r.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        bad = [d for d in doc["diagnostics"] if d["code"] in ("UNSUPPORTED_CONSTRUCT", "UNRESOLVED_OVERLOAD")]
        self.assertEqual(bad, [])
        self.assertGreater(doc["result"]["total"]["ge"]["typ"], 10)


AGG_CONCAT = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity top is port (a, b : in unsigned(15 downto 0); c : in unsigned(16 downto 0); y : out unsigned(16 downto 0)); end entity;
architecture r of top is
  signal s : unsigned(16 downto 0);
begin
  s <= ('0' & a) + ('0' & b) + ((a'range => '0') & c(c'high));
  y <= s;
end architecture;
"""


class AggregateInConcatenation(Base):
    def test_range_choice_aggregate_takes_type_from_context(self):
        self.write("ag.vhd", AGG_CONCAT)
        code, doc = run_cost("--top", "top", "ag.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        bad = [d for d in doc["diagnostics"] if d["code"] in ("UNSUPPORTED_CONSTRUCT", "UNRESOLVED_OVERLOAD")]
        self.assertEqual(bad, [])
        self.assertEqual(doc["status"], "ok")


TB_PROC = """library ieee; use ieee.std_logic_1164.all;
entity tb is generic (N : natural := 4000); port (clk, en, ack : in std_logic; v : out std_logic; o : out std_logic_vector(7 downto 0)); end entity;
architecture r of tb is begin
  process
    variable w : natural := 0;
  begin
    v <= '0';
    wait until en = '1';
    wait until rising_edge(clk);
    for t in 0 to 1000 loop
      w := 0;
      loop
        wait until rising_edge(clk);
        v <= '1';
        o <= std_logic_vector(to_unsigned(w, 8));
        wait until falling_edge(clk);
        if ack = '1' then w := w + 1; end if;
        exit when w > 1000;
      end loop;
    end loop;
  end process;
end architecture;
"""


class BehaviouralProcess(Base):
    def test_multi_wait_process_skipped_fast(self):
        import time
        self.write("tb.vhd", TB_PROC)
        t0 = time.time()
        code, doc = run_cost("--top", "tb", "tb.vhd", cwd=self.dir)
        self.assertLess(time.time() - t0, 20)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertTrue(any("wait statements" in d["message"] for d in doc["diagnostics"]), doc["diagnostics"])


NESTED_LOOPS = """library ieee; use ieee.std_logic_1164.all;
entity nl is port (a : in std_logic_vector(1023 downto 0); y : out std_logic); end entity;
architecture r of nl is begin
  process (a)
    variable f : std_logic;
  begin
    f := '0';
    for i in 0 to 1023 loop
      for j in 0 to 1023 loop
        if a(j) = '1' and a(i) = '1' then f := '1'; exit; end if;
      end loop;
    end loop;
    y <= f;
  end process;
end architecture;
"""


class LoopBudget(Base):
    def test_nested_exit_loops_bounded(self):
        import time
        self.write("nl.vhd", NESTED_LOOPS)
        t0 = time.time()
        code, doc = run_cost("--top", "nl", "nl.vhd", cwd=self.dir)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertTrue(any(d["code"] == "LIMIT_REACHED" for d in doc["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
