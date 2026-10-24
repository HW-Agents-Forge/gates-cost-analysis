"""Regressions found by running the tool on real open-source RTL (common_cells, ...)."""
import tempfile
import unittest
from pathlib import Path

from gatecost.elab import elaborate_project


def run(text, name="m.sv", **kw):
    with tempfile.TemporaryDirectory() as td:
        Path(td, name).write_text(text)
        return elaborate_project([Path(name)], root=Path(td), **kw)


class HeaderImport(unittest.TestCase):
    SRC = """package p;
  typedef enum logic {A=1'b0, B=1'b1} mode_e;
  function automatic int unsigned pw(int unsigned w); return w + 3; endfunction
endpackage
module m import p::*; #(
  parameter mode_e Mode = B,
  parameter int W = 8,
  localparam type par_t = logic [pw(W)-1:0]
)(input logic [W-1:0] a, output par_t y);
  assign y = Mode == B ? par_t'(a) : '0;
endmodule
"""

    def test_module_header_import_is_visible_in_the_parameter_list(self):
        res = run(self.SRC)
        bad = [d for d in res.diagnostics if d.code in ("UNRESOLVED_NAME", "UNRESOLVED_PARAM", "INTERNAL")]
        self.assertEqual(bad, [])


class ErrorTaskInFunction(unittest.TestCase):
    SRC = """module m #(parameter CFG = "A") (input [3:0] a, output [3:0] y);
  function [3:0] f(input [3:0] x);
    begin
      if (CFG == "A") f = x ^ 4'b0101;
      else if (CFG == "B") f = ~x;
      else begin
        $error("Error: unknown configuration setting!");
        $finish;
      end
    end
  endfunction
  assign y = f(a);
endmodule
"""

    def test_error_and_finish_do_not_make_a_function_nonsynthesizable(self):
        res = run(self.SRC)
        self.assertFalse([d for d in res.diagnostics if "non-synthesizable" in d.message])
        from gatecost.ir.dump import dump
        (m,) = res.design.modules.values()
        self.assertNotIn("4'hx", dump(m, analysis=False))


class FunctionStepBudget(unittest.TestCase):
    SRC = """module m(output [7:0] y);
  function [7:0] f(input [7:0] n);
    integer i;
    begin f = 0; for (i = 0; i < 8; i = i + 1) f = f + n; end
  endfunction
  genvar g;
  generate for (g = 0; g < 20; g = g + 1) begin : b
    wire [7:0] w = f(g);
  end endgenerate
  assign y = b[3].w;
endmodule
"""

    def test_total_steps_of_one_function_are_bounded(self):
        full = run(self.SRC)
        self.assertFalse([d for d in full.diagnostics if d.code == "LIMIT_REACHED"])
        cut = run(self.SRC, limits={"const_func_total_steps": 50})
        self.assertTrue([d for d in cut.diagnostics if d.code == "LIMIT_REACHED"])


class VhdlConcurrentEdge(unittest.TestCase):
    HDR = "library ieee; use ieee.std_logic_1164.all;\nentity e is port (q : out std_logic; d, clk, rst : in std_logic); end;\n"

    def dump(self, body):
        from gatecost.ir.dump import dump
        res = run(self.HDR + "architecture b of e is begin\n" + body + "\nend;", "m.vhd")
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        return dump(m, analysis=False)

    def test_conditional_assignment_with_edge_is_a_register(self):
        text = self.dump("q <= d when rising_edge(clk);")
        self.assertIn("DFF(D=d[0], CLK=clk[0]", text)
        self.assertIn("clk_edge=pos", text)

    def test_conditional_assignment_with_reset_and_edge(self):
        text = self.dump("q <= '0' when rst = '1' else d when rising_edge(clk);")
        self.assertIn("ARST=rst[0]", text)

    def test_event_and_not_equal_zero_is_a_rising_edge(self):
        text = self.dump("process (clk) begin if clk'event and clk /= '0' then q <= d; end if; end process;")
        self.assertIn("clk_edge=pos", text)


class VhdlStdLogicMisc(unittest.TestCase):
    SRC = """library ieee; use ieee.std_logic_1164.all; use ieee.std_logic_misc.all;
entity e is port (a : in std_logic_vector(7 downto 0); o, x, n : out std_logic); end;
architecture b of e is begin
  o <= or_reduce(a);
  x <= xor_reduce(a(3 downto 0));
  n <= nor_reduce(a) and and_reduce(a(1 downto 0));
end;
"""

    def test_reduce_functions_are_built_in(self):
        from gatecost.ir.dump import dump
        res = run(self.SRC, "m.vhd")
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        text = dump(m, analysis=False)
        self.assertTrue("REDUCE" in text.upper() or "RED_" in text.upper(), text)


class VhdlDynamicSliceWidth(unittest.TestCase):
    SRC = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity e is
  generic (G : natural := 4);
  port (a : in std_logic_vector(4*G-1 downto 0); i : in unsigned(1 downto 0);
        y : out std_logic_vector(G-1 downto 0); z : out std_logic_vector(G-1 downto 0));
end;
architecture b of e is
  signal k : natural range 0 to 3;
begin
  k <= to_integer(i);
  y <= a((k+1)*G-1 downto k*G);
  z <= a(k*G+G-1 downto (k+1)*G-G);
end;
"""

    def test_slice_with_symbolic_but_constant_width(self):
        res = run(self.SRC, "m.vhd")
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info" and d.code != "ASSUMPTION"], [])


class VhdlPhysicalTypes(unittest.TestCase):
    SRC = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
package ph is
  type FREQ is range 0 to integer'high units
    Hz;
    kHz = 1000 Hz;
    MHz = 1000 kHz;
  end units;
  function to_cycles(f : FREQ; t : FREQ) return natural;
end package;
package body ph is
  function to_cycles(f : FREQ; t : FREQ) return natural is
  begin
    return natural(f / t);
  end function;
end package body;
library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all; use work.ph.all;
entity e is
  generic (CLK_FREQ : FREQ := 100 MHz; BAUD : FREQ := 1 MHz);
  port (clk : in std_logic; q : out std_logic);
end;
architecture b of e is
  constant DIV : positive := to_cycles(CLK_FREQ, BAUD);
  signal cnt : unsigned(7 downto 0);
begin
  process (clk) begin
    if rising_edge(clk) then
      if cnt = DIV - 1 then cnt <= (others => '0'); else cnt <= cnt + 1; end if;
    end if;
  end process;
  q <= '1' when cnt = 0 else '0';
end;
"""

    def test_user_physical_units_are_integers_in_the_base_unit(self):
        from gatecost.ir.dump import dump
        res = run(self.SRC, "m.vhd", top="e")
        self.assertEqual([d for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        self.assertIn("'h63", dump(m, analysis=False))     # DIV = 100 MHz / 1 MHz, compared with DIV - 1


class PatternInTernary(unittest.TestCase):
    SRC = """package p;
  typedef struct packed { logic irq_ext; logic irq_int; logic [5:0] lower_cause; } cause_t;
  localparam cause_t C0 = '{irq_ext: 1'b0, irq_int: 1'b1, lower_cause: 6'd3};
endpackage
module m import p::*; (input logic a, input logic [5:0] c, output cause_t y);
  always_comb y = a ? C0 : '{irq_ext: 1'b0, irq_int: 1'b1, lower_cause: c};
endmodule
"""

    def test_untyped_pattern_in_a_ternary_branch_takes_the_target_type(self):
        res = run(self.SRC)
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])


class SystemFunctionSizeCast(unittest.TestCase):
    SRC = """module m (output logic [3:0] q);
  typedef enum logic [1:0] {Idle, Run} st_e;
  localparam st_e RV = Run;
  localparam logic [3:0] R = $bits(st_e)'(RV);
  assign q = $clog2(16)'(R) + $bits(st_e)'(1);
endmodule
"""

    def test_size_cast_with_a_system_function_width(self):
        from gatecost.ir.dump import dump
        res = run(self.SRC)
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        self.assertIn("q[3:0] <- 4'h", dump(m, analysis=False))


class VhdlTimeArithmetic(unittest.TestCase):
    SRC = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
entity e is
  generic (CLK_PERIOD : time := 10 ns; TICK : time := 1 us);
  port (clk : in std_logic; q : out std_logic);
end;
architecture b of e is
  constant DIV : positive := TICK / CLK_PERIOD;
  constant DIV2 : positive := integer(real(2 ms / 1 us) * 0.05);
  signal cnt : unsigned(7 downto 0);
begin
  process (clk) begin
    if rising_edge(clk) then
      if cnt = DIV - 1 then cnt <= (others => '0'); else cnt <= cnt + DIV2 / 100; end if;
    end if;
  end process;
  q <= '1' when cnt = 0 else '0';
end;
"""

    def test_time_values_are_femtosecond_integers(self):
        from gatecost.ir.dump import dump
        res = run(self.SRC, "m.vhd")
        self.assertEqual([d for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        text = dump(m, analysis=False)
        self.assertIn("'h63", text)          # 1 us / 10 ns - 1
        self.assertIn("ADD(8'h1,", text)     # 2 ms / 1 us * 0.05 / 100 = 1


class PartiallyAssignedComb(unittest.TestCase):
    SRC = """module m(input [7:0] a, output reg [7:0] y);
  always @* begin
    y[0] = a[0]; y[1] = a[1]; y[4] = ~a[4]; y[6] = a[5] & a[6]; y[7] = a[7];
    y[2] = 1'b0; y[3] = 1'b1; y[5] = a[2] | a[3];
  end
endmodule
"""

    def test_runs_of_assigned_bits_are_connected(self):
        from gatecost.ir.dump import dump
        res = run(self.SRC)
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        text = dump(m, analysis=False)
        self.assertIn("y[7:0] <- {a[7], n3[0], n1[0], n2[0], 2'h2, a[1:0]}", text)

    def test_very_wide_vector_is_not_quadratic(self):
        import time
        src = "module m(input [499999:0] a, output reg [499999:0] y);\n  always @* y = ~a;\nendmodule\n"
        t0 = time.time()
        res = run(src, limits={"max_net_width": 1 << 20})
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])
        self.assertLess(time.time() - t0, 30)


class VhdlControlCharacters(unittest.TestCase):
    SRC = """library ieee; use ieee.std_logic_1164.all;
entity e is port (q, r : out std_logic); end;
architecture b of e is
  constant C : character := NUL;
begin
  q <= '1' when character'pos(LF) = 10 and character'pos(DEL) = 127 else '0';
  r <= '1' when C = NUL else '0';
end;
"""

    def test_nul_lf_del_are_character_literals(self):
        from gatecost.ir.dump import dump
        res = run(self.SRC, "m.vhd")
        self.assertEqual([d.code for d in res.diagnostics if d.severity != "info"], [])
        (m,) = res.design.modules.values()
        text = dump(m, analysis=False)
        self.assertIn("q[0] <- 1'h1", text)
        self.assertIn("r[0] <- 1'h1", text)


if __name__ == "__main__":
    unittest.main()
