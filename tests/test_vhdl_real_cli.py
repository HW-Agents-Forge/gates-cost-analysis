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


class UselibDirective(Base):
    def test_uselib_is_ignored(self):
        self.write("u.v", "`uselib lib = ecp3\nmodule u(input a, output y); assign y = ~a; endmodule\n")
        code, doc = run_cost("--top", "u", "u.v", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertEqual([d["code"] for d in doc["diagnostics"] if d["code"] in ("UNDEFINED_MACRO", "PARSE_ERROR")], [])


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


class BadConfigValues(Base):
    def check(self, toml, needle):
        self.write("t.v", "module t(input a, output y); assign y = ~a; endmodule\n")
        self.write("gatecost.toml", toml)
        code, doc = run_cost("--top", "t", cwd=self.dir)
        self.assertEqual(doc["error"]["code"], "BAD_CONFIG", doc)
        self.assertIn(needle, doc["error"]["message"])
        self.assertNotEqual(doc["error"]["code"], "INTERNAL")

    def test_non_string_list_items(self):
        self.check("[project]\nincdirs = [5]\n", "project.incdirs")
        self.check("[project]\nsources = [1, 2]\n", "project.sources")

    def test_unknown_language(self):
        self.check('[project]\nlanguages = ["cobol"]\n', "languages")
        self.check("[project]\nlanguages = []\n", "languages")

    def test_non_positive_limit(self):
        self.check("[limits]\nloop_unroll = -5\n", "limits.loop_unroll")
        self.check("[limits]\ngenerate_iterations = 0\n", "limits.generate_iterations")

    def test_capitalised_language_accepted(self):
        self.write("t.v", "module t(input a, output y); assign y = ~a; endmodule\n")
        self.write("gatecost.toml", '[project]\nlanguages = ["Verilog"]\n')
        code, doc = run_cost("--top", "t", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))


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


ARR_OF_UNCONSTRAINED = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
package arr is
  type StlvArray_t is array (natural range <>) of std_logic_vector;
  function unflat(a : in std_logic_vector; elementSize : positive) return StlvArray_t;
end package;
package body arr is
  function unflat(a : in std_logic_vector; elementSize : positive) return StlvArray_t is
    constant N : natural := a'length / elementSize;
    variable U : StlvArray_t(0 to N - 1)(elementSize - 1 downto 0);
  begin
    for i in 0 to N - 1 loop
      U(i) := a((i + 1) * elementSize - 1 downto i * elementSize);
    end loop;
    return U;
  end function;
end package body;
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all; use work.arr.all;
entity top is port (a : in std_logic_vector(31 downto 0); sel : in unsigned(1 downto 0); y : out std_logic_vector(7 downto 0)); end entity;
architecture r of top is
  signal u : StlvArray_t(0 to 3)(7 downto 0);
begin
  u <= unflat(a, 8);
  y <= u(to_integer(sel));
end architecture;
"""

STRING_GENERIC = """library ieee; use ieee.std_logic_1164.all;
entity top is generic (POLY : std_logic_vector; NAME : string); port (a : in std_logic_vector(7 downto 0); y : out std_logic); end entity;
architecture r of top is
begin
  y <= a(0) xor a(1) when NAME = "x" else a(2);
end architecture;
"""


class Vhdl2008Constraints(Base):
    def test_element_constraint_of_array_of_unconstrained(self):
        self.write("a.vhd", ARR_OF_UNCONSTRAINED)
        code, doc = run_cost("--top", "top", "a.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertEqual(doc["status"], "ok", doc["diagnostics"])
        self.assertGreater(doc["result"]["total"]["ge"]["typ"], 20)

    def test_unconstrained_generic_without_default_is_one_diagnostic(self):
        self.write("g.vhd", STRING_GENERIC)
        code, doc = run_cost("--top", "top", "g.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        cs = [d["code"] for d in doc["diagnostics"]]
        self.assertEqual(cs.count("UNRESOLVED_PARAM"), 2)
        self.assertNotIn("UNSUPPORTED_CONSTRUCT", cs)


NULL_PORT = """library ieee; use ieee.std_logic_1164.all;
entity ram is generic (W : positive := 8); port (clk : in std_logic; d : in std_logic_vector(W - 1 downto 0);
  be : in std_logic_vector(W / 8 - 1 downto 0) := (others => '1'); q : out std_logic_vector(W - 1 downto 0)); end entity;
architecture r of ram is begin
  process (clk) begin if rising_edge(clk) then q <= d; end if; end process;
end architecture;
library ieee; use ieee.std_logic_1164.all;
entity top is port (clk : in std_logic; d : in std_logic_vector(0 downto 0); q : out std_logic_vector(0 downto 0)); end entity;
architecture r of top is begin
  u : entity work.ram generic map (W => 1) port map (clk => clk, d => d, q => q);
end architecture;
"""


class NullWidthPortDefault(Base):
    def test_others_default_on_null_width_port(self):
        self.write("n.vhd", NULL_PORT)
        code, doc = run_cost("--top", "top", "n.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertNotIn("UNSUPPORTED_CONSTRUCT", [d["code"] for d in doc["diagnostics"]])


DEFAULT_OVERLOAD = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
package base is
  function rev (constant arg : std_logic_vector; constant keep : boolean := true) return std_logic_vector;
  function rev (constant arg : unsigned) return unsigned;
end package;
package body base is
  function rev (constant arg : std_logic_vector; constant keep : boolean := true) return std_logic_vector is
    variable r : std_logic_vector(arg'range);
  begin
    for i in arg'range loop r(i) := arg(arg'right + arg'left - i); end loop;
    return r;
  end;
  function rev (constant arg : unsigned) return unsigned is
  begin
    return unsigned(rev(std_logic_vector(arg)));
  end;
end package body;
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all; use work.base.all;
entity top is port (a : in std_logic_vector(15 downto 0); u : in unsigned(15 downto 0);
  y : out std_logic_vector(15 downto 0); z : out unsigned(15 downto 0)); end entity;
architecture r of top is begin
  y <= rev(a);
  z <= rev(u);
end architecture;
"""


class OverloadWithDefaults(Base):
    def test_defaulted_parameter_overload_not_mistaken_for_last_candidate(self):
        self.write("d.vhd", DEFAULT_OVERLOAD)
        code, doc = run_cost("--top", "top", "d.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        bad = [d for d in doc["diagnostics"] if d["code"] in ("LIMIT_REACHED", "UNSUPPORTED_CONSTRUCT", "UNRESOLVED_OVERLOAD")]
        self.assertEqual(bad, [])


HUGE_ARRAY = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
package p is
  type nat_vec is array (natural range <>) of natural;
  function pick (constant word : nat_vec; constant addr : std_logic_vector) return natural;
end package;
package body p is
  function pick (constant word : nat_vec; constant addr : std_logic_vector) return natural is
    alias arg : nat_vec(0 to word'length - 1) is word;
    variable retval : nat_vec(0 to 2 ** addr'length - 1);
  begin
    retval := (others => 0);
    if retval'length < arg'length then retval := arg(retval'range); else retval(arg'range) := arg; end if;
    return retval(to_integer(unsigned(addr)));
  end;
end package body;
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all; use work.p.all;
entity top is port (addr : in std_logic_vector(31 downto 0); y : out natural range 0 to 255); end entity;
architecture r of top is
  constant tbl : nat_vec(0 to 3) := (1, 2, 3, 4);
begin
  y <= pick(tbl, addr);
end architecture;
"""


class RunawayWidth(Base):
    def test_giant_array_bound_does_not_allocate_gigabytes(self):
        import resource
        import subprocess
        import sys
        from gatecost import REPO_ROOT
        self.write("h.vhd", HUGE_ARRAY)

        def limit():
            resource.setrlimit(resource.RLIMIT_AS, (3 << 30, 3 << 30))
        env = {**os.environ, "GATECOST_CACHE_DIR": str(self.dir / "_c")}
        p = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "gatecost"), "cost", "--format", "json",
                            "--top", "top", "h.vhd"], cwd=self.dir, env=env, capture_output=True, text=True,
                           preexec_fn=limit, timeout=120)
        doc = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, doc.get("error"))
        self.assertFalse([d for d in doc["diagnostics"] if "MemoryError" in d["message"]], doc["diagnostics"])


RET_AGG = """library ieee; use ieee.std_logic_1164.all;
package p is
  function zeros (constant arg : std_logic_vector) return std_logic_vector;
end package;
package body p is
  function zeros (constant arg : std_logic_vector) return std_logic_vector is
  begin
    return (arg'range => '0');
  end;
end package body;
library ieee; use ieee.std_logic_1164.all; use work.p.all;
entity top is port (a : in std_logic_vector(7 downto 0); y : out std_logic_vector(7 downto 0)); end entity;
architecture r of top is begin
  y <= a or zeros(a);
end architecture;
"""


class ReturnAggregateUnconstrained(Base):
    def test_return_range_aggregate_in_unconstrained_function(self):
        self.write("r.vhd", RET_AGG)
        code, doc = run_cost("--top", "top", "r.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertEqual([d for d in doc["diagnostics"] if d["code"] == "UNSUPPORTED_CONSTRUCT"], [])


PSL_SRC = """library ieee; use ieee.std_logic_1164.all;
entity top is port (clk, rst, a : in std_logic; y : out std_logic); end entity;
architecture r of top is
  signal q : std_logic;
  sequence seq (boolean d0, d1) is {d0; d1};
  default clock is rising_edge(clk);
begin
  process (clk) begin if rising_edge(clk) then q <= a; end if; end process;
  y <= q;
  a_1 : assume always stable(a);
  f_1 : assert always {a; q};
  c_1 : cover {a = '1'; q = '1'}
    report "hit";
  r_1 : restrict {rst = '0'; rst = '1'}[*1];
  cover {a; q};
end architecture;

vunit top_vu (top(r)) {
  default clock is rising_edge(clk);
  assert always (a -> next q);
  sequence s2 is {a; {q; q}};
}
"""


class PslIsSkipped(Base):
    def test_psl_directives_and_vunit_are_ignored_not_errors(self):
        self.write("psl.vhd", PSL_SRC)
        code, doc = run_cost("--top", "top", "psl.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertEqual([d for d in doc["diagnostics"] if d["code"] == "PARSE_ERROR"], [])
        self.assertGreater(doc["result"]["total"]["ge"]["typ"], 0)


NESTED_EDGE = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity top is port (clk, rst, cen : in std_logic; d : in unsigned(7 downto 0); q : out unsigned(7 downto 0)); end entity;
architecture r of top is
  signal c : unsigned(7 downto 0);
begin
  process (clk, rst)
  begin
    if rst = '0' then
      c <= (others => '0');
    %s
      if clk'event and clk = '1' and cen = '1' then
        c <= c + d;
      end if;
    %s
  end process;
  q <= c;
end architecture;
"""


class ElseIfClockEdge(Base):
    def test_edge_in_nested_if_after_async_reset(self):
        self.write("a.vhd", NESTED_EDGE % ("else", "end if;"))
        code, a = run_cost("--top", "top", "a.vhd", cwd=self.dir)
        self.assertEqual(code, 0, a.get("error"))
        self.assertEqual([d for d in a["diagnostics"] if "outside an edge" in d["message"]], [])
        self.assertGreater(a["result"]["total"]["ge"]["typ"], 80)


SIGNAL_AS_GENERIC = """library ieee; use ieee.std_logic_1164.all;
entity leaf is generic (G : std_logic_vector(3 downto 0) := "0000"); port (clk : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0)); end entity;
architecture r of leaf is
  signal r1 : std_logic_vector(3 downto 0);
begin
  process (clk) begin if rising_edge(clk) then if d = G then r1 <= d; else r1 <= G; end if; end if; end process;
  q <= r1;
end architecture;
library ieee; use ieee.std_logic_1164.all;
entity mid is generic (G : std_logic_vector(3 downto 0)); port (clk : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0)); end entity;
architecture r of mid is begin
  u : entity work.leaf generic map (G => G) port map (clk => clk, d => d, q => q);
end architecture;
library ieee; use ieee.std_logic_1164.all;
entity top is port (clk : in std_logic; d : in std_logic_vector(3 downto 0); q : out std_logic_vector(3 downto 0)); end entity;
architecture r of top is
  signal s : std_logic_vector(3 downto 0);
begin
  s <= d;
  u : entity work.mid generic map (G => s) port map (clk => clk, d => d, q => q);
end architecture;
"""


class SignalAsGenericActual(Base):
    def test_signal_actual_does_not_corrupt_child_module(self):
        self.write("s.vhd", SIGNAL_AS_GENERIC)
        code, doc = run_cost("--top", "top", "s.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertEqual([d for d in doc["diagnostics"] if d["code"] == "INTERNAL"], [])
        self.assertTrue(any("not a constant" in d["message"] for d in doc["diagnostics"]))


STR_CONCAT_AND_ASSERT_IF = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity top is generic (N : natural := 4); port (clk : in std_logic; a, b : in unsigned(7 downto 0); q : out unsigned(8 downto 0); r : out std_logic_vector(7 downto 0)); end entity;
architecture r of top is
  signal rr : std_logic_vector(7 downto 0);
begin
  process (clk) begin
    if N > 100 then assert false report "N out of range" severity error; end if;
    if rising_edge(clk) then
      q <= ("0" & a) + b;
      rr <= std_logic_vector(a);
    end if;
  end process;
  r <= rr;
end architecture;
"""


class StringConcatAndLeadingIf(Base):
    def test_string_literal_concat_takes_unsigned_and_clock_after_assert_if(self):
        self.write("s.vhd", STR_CONCAT_AND_ASSERT_IF)
        code, doc = run_cost("--top", "top", "s.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        bad = [d["message"] for d in doc["diagnostics"] if d["code"] in ("UNRESOLVED_OVERLOAD", "UNSUPPORTED_CONSTRUCT")]
        self.assertEqual(bad, [])
        self.assertEqual(doc["status"], "ok")


WORD_SHIFT = """library ieee; use ieee.std_logic_1164.all;
entity top is port (clk, v : in std_logic; d : in std_logic_vector(7 downto 0); q : out std_logic_vector(8 downto 0)); end entity;
architecture r of top is
  type a_t is array (0 to 20) of std_logic_vector(8 downto 0);
  signal dd : a_t;
begin
  process (clk) begin
    if rising_edge(clk) then
      dd(0 to dd'high - 1) <= dd(1 to dd'high);
      if v = '1' then dd(dd'high) <= '1' & d; else dd(dd'high) <= (others => '0'); end if;
    end if;
  end process;
  q <= dd(0);
end architecture;
"""

MEM_SLICE = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity top is port (clk, we : in std_logic; a : in unsigned(3 downto 0); d : in std_logic_vector(7 downto 0); q : out std_logic_vector(7 downto 0)); end entity;
architecture r of top is
  type m_t is array (0 to 15) of std_logic_vector(7 downto 0);
  signal m : m_t;
begin
  process (clk) begin
    if rising_edge(clk) then
      if we = '1' then m(to_integer(a)) <= d; end if;
      m(8 to 11) <= m(4 to 7);
      q <= m(to_integer(a));
    end if;
  end process;
end architecture;
"""


class WordShiftRegister(Base):
    def test_constant_slice_shift_of_word_array_is_a_register_chain(self):
        self.write("s.vhd", WORD_SHIFT)
        code, doc = run_cost("--top", "top", "s.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        self.assertEqual(doc["status"], "ok", doc["diagnostics"])
        ge = doc["result"]["total"]["ge"]["typ"]
        self.assertTrue(400 < ge < 3000, ge)        # ~189 flip-flops; it used to be 20 000 (a 21-port RAM)

    def test_slice_copy_inside_a_real_memory(self):
        self.write("m.vhd", MEM_SLICE)
        code, doc = run_cost("--top", "top", "m.vhd", cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        bad = [d["message"] for d in doc["diagnostics"] if "whole memory" in d["message"]]
        self.assertEqual(bad, [])


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
