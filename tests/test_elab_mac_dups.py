"""Regressions: MAC fusion across Verilog/VHDL (resize/extension of a product), VHDL duplicate declarations."""
import contextlib
import io
import os
import tempfile
import unittest

from gatecost.cli import main
from gatecost.cost import build_cost_tree, load_profile
from gatecost.elab import elaborate_project

P = load_profile()

VHD_HEAD = """library ieee; use ieee.std_logic_1164.all; use ieee.numeric_std.all;
"""


def elab(src, name, top=None):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, name), "w") as f:
        f.write(src)
    return d, elaborate_project([name], root=d, top=top)


def ops(res):
    return sorted(n.op for n in res.design.modules[res.top_key].nodes)


def cost(res):
    return build_cost_tree(res.design, P).total().typ


class MacFusion(unittest.TestCase):
    def pair(self, sign):
        s = "signed" if sign else "unsigned"
        v = ("module m(input clk, input %s [7:0] a, input %s [7:0] b, output reg %s [23:0] acc);\n"
             "always @(posedge clk) acc <= acc + a * b;\nendmodule\n" % (("signed",) * 3 if sign else ("",) * 3))
        h = VHD_HEAD + ("entity m is port(clk: in std_logic; a,b: in %s(7 downto 0); acc: out %s(23 downto 0)); end;\n"
                        "architecture r of m is signal q: %s(23 downto 0);\nbegin\n"
                        " process(clk) begin if rising_edge(clk) then q <= q + resize(a*b, 24); end if; end process;\n"
                        " acc <= q;\nend;\n" % (s, s, s))
        return elab(v, "m.v")[1], elab(h, "m.vhd")[1]

    def test_unsigned_and_signed_agree_and_fuse(self):
        for sign in (False, True):
            with self.subTest(signed=sign):
                rv, rh = self.pair(sign)
                self.assertIn("SUM", ops(rh))
                self.assertNotIn("MUL", ops(rh))
                self.assertEqual(ops(rv), ops(rh))
                self.assertEqual(cost(rv), cost(rh))

    def test_truncating_product_is_not_fused(self):
        # 16x16 product truncated to 16 bits then zero-extended is not the 24-bit product
        h = VHD_HEAD + ("entity m is port(clk: in std_logic; a,b: in unsigned(15 downto 0); acc: out unsigned(23 downto 0)); end;\n"
                        "architecture r of m is signal q: unsigned(23 downto 0);\nbegin\n"
                        " process(clk) begin if rising_edge(clk) then q <= q + resize(a*b, 24); end if; end process;\n"
                        " acc <= q;\nend;\n")
        # a*b in numeric_std is 32 bits; resize to 24 truncates, so build the 16-bit one explicitly
        h = h.replace("resize(a*b, 24)", "resize(resize(a*b, 16), 24)")
        r = elab(h, "m.vhd")[1]
        self.assertIn("MUL", ops(r))


class DuplicateDecl(unittest.TestCase):
    SRC = VHD_HEAD + """entity d is
  generic (ACC : positive := 24);
  port (clk : in std_logic; a : in unsigned(7 downto 0); acc : out unsigned(ACC-1 downto 0));
end entity;
architecture rtl of d is
begin
  acc <= resize(a, ACC);
end architecture;
"""

    def test_generic_port_clash(self):
        _d, r = elab(self.SRC, "d.vhd")
        ds = [x for x in r.diagnostics if x.code == "DUPLICATE_DECLARATION"]
        self.assertEqual(len(ds), 1, r.diagnostics)
        self.assertTrue(ds[0].affects_cost)
        self.assertEqual(ds[0].line, 4)
        self.assertIn("acc", ds[0].message.lower())

    def test_signal_vs_port_and_no_false_positive(self):
        src = self.SRC.replace("generic (ACC : positive := 24);", "generic (W : positive := 24);").replace(
            "ACC", "W").replace("acc <=", "q <=").replace("begin\n  q", "signal Q : unsigned(7 downto 0);\nbegin\n  q")
        src = src.replace("acc : out unsigned(W-1", "Q : out unsigned(W-1")
        _d, r = elab(src, "d.vhd")
        self.assertTrue([x for x in r.diagnostics if x.code == "DUPLICATE_DECLARATION"], r.diagnostics)
        ok = self.SRC.replace("generic (ACC", "generic (ACC_W").replace("-1 downto 0));", "-1 downto 0));").replace(
            "unsigned(ACC-1", "unsigned(ACC_W-1").replace("resize(a, ACC)", "resize(a, ACC_W)")
        _d, r = elab(ok, "d.vhd")
        self.assertEqual([x for x in r.diagnostics if x.code == "DUPLICATE_DECLARATION"], [])

    def test_cost_prints_warn(self):
        d, _r = elab(self.SRC, "d.vhd")
        out = io.StringIO()
        old = os.getcwd()
        try:
            os.chdir(d)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                main(["cost", "d.vhd"])
        finally:
            os.chdir(old)
        self.assertIn("WARN DUPLICATE_DECLARATION d.vhd:4", out.getvalue())


class NonConstBound(unittest.TestCase):
    def test_non_constant_bound_is_cost_diagnostic(self):
        src = VHD_HEAD + """entity n is port (clk : in std_logic; i : in unsigned(3 downto 0); y : out std_logic); end;
architecture r of n is
  signal u : std_logic_vector(to_integer(i) downto 0);
begin
  y <= u(0);
end;
"""
        _d, r = elab(src, "n.vhd")
        ds = [x for x in r.diagnostics if x.code == "NON_CONSTANT_BOUND"]
        self.assertTrue(ds, [(x.code, x.message) for x in r.diagnostics])
        self.assertTrue(all(x.affects_cost for x in ds))


if __name__ == "__main__":
    unittest.main()
