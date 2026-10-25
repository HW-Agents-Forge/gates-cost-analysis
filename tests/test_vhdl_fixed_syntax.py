"""Regressions for VHDL limitations found on real projects (ghdl testsuite/synth, open-logic, hdl4fpga, PoC):

1. ``ieee.fixed_pkg`` / ``ieee.float_pkg`` (types, result ranges, resize styles, constants, approximate FP datapaths);
2. whole-memory aggregate assignment, null arrays, record-aggregate ``return`` on an unconstrained record, user
   ``"**"`` overloads that differ by result type only;
3. VHDL-2019/2008 syntax: mode views and port views, external names, generic subprograms, ``T : type``;
4. a ``.v``/``.sv`` file that contains VHDL.
"""
import contextlib
import io
import json
import os
import random
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from gatecost.cli import main
from gatecost.frontend.vhdl import ieee, nodes as N, parse_text
from tests.ir_sim import Sim
from tests.test_elab_vhdl_util import HDR, elab, module, probe

FX = "use ieee.fixed_pkg.all;\n"
FLT = "use ieee.fixed_float_types.all;\nuse ieee.float_pkg.all;\n"


def lint(r):
    """Diagnostic codes that point at an unsupported construct."""
    return [(d.code, d.message) for d in r.diagnostics
            if d.code in ("UNSUPPORTED_CONSTRUCT", "UNRESOLVED_OVERLOAD", "PARSE_ERROR", "MISSING_UNIT")]


def fx_value(bits, hi, lo, signed):
    w = hi - lo + 1
    n = bits & ((1 << w) - 1)
    if signed and n >> (w - 1):
        n -= 1 << w
    return Fraction(n) * Fraction(2) ** lo


def floor_to(x, lo):
    return Fraction((x / Fraction(2) ** lo).__floor__()) * Fraction(2) ** lo


def round_to(x, lo):
    return Fraction((x / Fraction(2) ** lo + Fraction(1, 2)).__floor__()) * Fraction(2) ** lo


class FixedHarness(unittest.TestCase):
    """Elaborates ``y <= to_slv(<expr>)`` over fixed-point input ports and compares with exact rationals."""

    def result_type(self, decls, expr):
        v, _ctx = probe(decls, expr, use=FX)
        return v.t.left, v.t.right, v.t.cls

    def check(self, ports, expr, ref, n=40, seed=1):
        decls = "\n".join("signal %s : %s(%d downto %d);" % (k, "sfixed" if sg else "ufixed", h, l)
                          for k, (h, l, sg) in ports.items())
        hi, lo, cls = self.result_type(decls, expr)
        w = hi - lo + 1
        pdecl = ";\n".join("%s : in %s(%d downto %d)" % (k, "sfixed" if sg else "ufixed", h, l)
                           for k, (h, l, sg) in ports.items())
        src = (HDR + FX + "entity t is port (\n%s;\n y : out std_logic_vector(%d downto 0));\nend entity;\n"
               "architecture a of t is begin\n y <= to_slv(%s);\nend architecture;\n" % (pdecl, w - 1, expr))
        m, r, _d = module(src)
        self.assertEqual(lint(r), [], expr)
        sim = Sim(m)
        rnd = random.Random(seed)
        for _ in range(n):
            ins, vals = {}, {}
            for k, (h, l, sg) in ports.items():
                bits = rnd.getrandbits(h - l + 1)
                ins[k] = bits
                vals[k] = fx_value(bits, h, l, sg)
            got = fx_value(sim.outputs(sim.comb(ins))["y"], hi, lo, cls == "sf")
            want = ref(lo=lo, **vals)
            if want is not None:
                self.assertEqual(got, want, "%s with %s" % (expr, vals))
        return hi, lo, cls


U = {"a": (3, -2, False), "b": (5, 0, False)}
S = {"a": (3, -2, True), "b": (1, -4, True)}


class FixedResultRanges(FixedHarness):
    """VHDL-2008 fixed_pkg result ranges and values."""

    def test_range_rules_table(self):
        R = ieee.fixed_result_range
        self.assertEqual(R("+", (3, -2), (5, 0)), (6, -2))
        self.assertEqual(R("-", (3, -2), (1, -4)), (4, -4))
        self.assertEqual(R("*", (3, -2), (5, 0)), (9, -2))
        self.assertEqual(R("*", (3, -2), (1, -4), True), (5, -6))
        self.assertEqual(R("/", (3, -2), (5, 0)), (3, -8))                  # ufixed: ha-lb downto la-hb-1
        self.assertEqual(R("/", (3, -2), (1, -4), True), (8, -3))           # sfixed: ha-lb+1 downto la-hb
        self.assertEqual(R("rem", (3, -2), (5, 0)), (3, -2))
        self.assertEqual(R("mod", (3, -2), (5, 0)), (5, -2))
        self.assertEqual(R("neg", (3, -2)), (4, -2))
        self.assertEqual(R("abs", (3, -2)), (4, -2))

    def test_operator_ranges_through_the_elaborator(self):
        decl = "signal ua : ufixed(3 downto -2); signal ub : ufixed(5 downto 0);\n" \
               "signal sa : sfixed(3 downto -2); signal sb : sfixed(1 downto -4);"
        for expr, want in (("ua + ub", (6, -2, "uf")), ("sa - sb", (4, -4, "sf")), ("ua * ub", (9, -2, "uf")),
                           ("sa * sb", (5, -6, "sf")), ("ua / ub", (3, -8, "uf")), ("sa / sb", (8, -3, "sf")),
                           ("-sa", (4, -2, "sf")), ("abs sa", (4, -2, "sf")), ("ua + 1", (4, -2, "uf")),
                           ("sa * 0.5", (7, -4, "sf")), ("ub mod ua", (3, -2, "uf")),
                           ("resize(sa, 5, -6)", (5, -6, "sf")), ("maximum(ua, ub)", (5, -2, "uf"))):
            self.assertEqual(self.result_type(decl, expr), want, expr)

    def test_declared_ranges_and_attributes(self):
        v, _ = probe("signal x : sfixed(7 downto -8);", "x'low", use=FX)
        self.assertEqual(v.c, -8)
        v, _ = probe("signal x : sfixed(7 downto -8);", "x'length", use=FX)
        self.assertEqual(v.c, 16)
        v, _ = probe("signal x : ufixed(3 downto -4);", "x'high", use=FX)
        self.assertEqual(v.c, 3)


class FixedArithmetic(FixedHarness):
    def test_add_sub(self):
        self.check(U, "a + b", lambda lo, a, b: a + b)
        self.check(S, "a + b", lambda lo, a, b: a + b)
        self.check(S, "a - b", lambda lo, a, b: a - b)
        self.check(S, "b - a", lambda lo, a, b: b - a)

    def test_mul(self):
        self.check(U, "a * b", lambda lo, a, b: a * b)
        self.check(S, "a * b", lambda lo, a, b: a * b)

    def test_div_rem_mod(self):
        import math

        def tdiv(lo, a, b):
            if b == 0:
                return None
            q = a / b
            n = int(q / Fraction(2) ** lo) if q >= 0 else -int(-q / Fraction(2) ** lo)
            return Fraction(n) * Fraction(2) ** lo
        self.check(U, "a / b", lambda lo, a, b: None if b == 0 else floor_to(a / b, lo))
        self.check(S, "a / b", tdiv)
        self.check(U, "b mod a", lambda lo, a, b: None if a == 0 else b - a * math.floor(b / a))
        self.check(U, "b rem a", lambda lo, a, b: None if a == 0 else b - a * math.floor(b / a))

    def test_negation_abs(self):
        self.check(S, "-a", lambda lo, a, b: -a)
        self.check(S, "abs(a)", lambda lo, a, b: abs(a))

    def test_real_and_integer_operands(self):
        self.check(S, "a * 0.5", lambda lo, a, b: a / 2)
        self.check(S, "a + 1", lambda lo, a, b: a + 1)
        self.check(S, "a + 0.25", lambda lo, a, b: a + Fraction(1, 4))
        self.check(U, "2 * a" if False else "a * 2", lambda lo, a, b: a * 2)

    def test_min_max(self):
        self.check(U, "maximum(a, b)", lambda lo, a, b: max(a, b))
        self.check(S, "minimum(a, b)", lambda lo, a, b: min(a, b))

    def test_scalb_and_shifts(self):
        self.check(S, "scalb(a, 2)", lambda lo, a, b: None)
        self.check(S, "shift_right(a, 1)", lambda lo, a, b: None)

    def test_conversions_in(self):
        self.check(U, "to_ufixed(a, 5, -4)", lambda lo, a, b: a)
        self.check(S, "to_sfixed(b, 3, -2)",
                   lambda lo, a, b: max(-8, min(Fraction(31, 4), round_to(b, -2))))

    def test_comparisons_elaborate(self):
        src = HDR + FX + ("entity t is port (a : in sfixed(3 downto -2); b : in sfixed(1 downto -4); "
                          "lt, eq, ge : out std_logic);\nend entity;\narchitecture x of t is begin\n"
                          " lt <= '1' when a < b else '0';\n eq <= '1' when a = b else '0';\n"
                          " ge <= '1' when a >= 0.5 else '0';\nend architecture;\n")
        m, r, _d = module(src)
        self.assertEqual(lint(r), [])
        sim = Sim(m)
        rnd = random.Random(3)
        for _ in range(60):
            a, b = rnd.getrandbits(6), rnd.getrandbits(6)
            va, vb = fx_value(a, 3, -2, True), fx_value(b, 1, -4, True)
            out = sim.outputs(sim.comb({"a": a, "b": b}))
            self.assertEqual(out["lt"], int(va < vb))
            self.assertEqual(out["eq"], int(va == vb))
            self.assertEqual(out["ge"], int(va >= Fraction(1, 2)))


class FixedResize(FixedHarness):
    def test_wrap_truncate(self):
        self.check(S, "resize(a, 3, -1, fixed_wrap, fixed_truncate)", lambda lo, a, b: None)
        self.check(S, "resize(a, 4, -2)", lambda lo, a, b: a)

    def test_saturate(self):
        self.check(U, "resize(a, 1, -2)", lambda lo, a, b: min(Fraction(15, 4), a))
        self.check(S, "resize(a, 1, -2)", lambda lo, a, b: max(Fraction(-2), min(Fraction(7, 4), a)))

    def test_round_and_truncate(self):
        self.check(U, "resize(a, 3, 0)", lambda lo, a, b: min(Fraction(15), round_to(a, 0)))
        self.check(U, "resize(a, 3, 0, fixed_saturate, fixed_truncate)", lambda lo, a, b: floor_to(a, 0))
        self.check(S, "resize(a, 3, 0, fixed_saturate, fixed_truncate)", lambda lo, a, b: floor_to(a, 0))
        self.check(S, "resize(a, 2, 0)", lambda lo, a, b: max(-4, min(3, round_to(a, 0))))

    def test_size_res_form(self):
        decls = "signal a : sfixed(3 downto -2); signal t : sfixed(6 downto -5);"
        self.assertEqual(self.result_type(decls, "resize(a, t)"), (6, -5, "sf"))

    def test_resize_costs_something_when_saturating(self):
        def ge(expr):
            src = HDR + FX + ("entity t is port (a : in sfixed(7 downto -8); y : out sfixed(3 downto -4));\nend entity;\n"
                              "architecture x of t is begin y <= %s; end architecture;\n" % expr)
            m, r, _d = module(src)
            self.assertEqual(lint(r), [])
            return len(m.nodes)
        wrap = ge("resize(a, 3, -4, fixed_wrap, fixed_truncate)")
        sat = ge("resize(a, 3, -4, fixed_saturate, fixed_truncate)")
        rnd = ge("resize(a, 3, -4, fixed_wrap, fixed_round)")
        self.assertEqual(wrap, 0)                       # pure wiring
        self.assertGreater(sat, wrap)                   # overflow detector + clamp mux
        self.assertGreater(rnd, wrap)                   # round incrementer


class FixedConstants(unittest.TestCase):
    def const(self, decls, expr):
        v, _ = probe(decls, expr, use=FX)
        c = v.sig.const_value()
        self.assertIsNotNone(c, expr)
        return c.value, v.t

    def test_to_sfixed_constants(self):
        bits, t = self.const("", "to_sfixed(0.5, 7, -8)")
        self.assertEqual((bits, t.left, t.right), (0x0080, 7, -8))
        self.assertEqual(self.const("", "to_sfixed(-0.5, 7, -8)")[0], 0xFF80)
        self.assertEqual(self.const("", "to_sfixed(1000.0, 7, -8)")[0], 0x7FFF)          # saturates
        self.assertEqual(self.const("", "to_ufixed(3.14159, 3, -4)")[0], 50)             # round to nearest
        self.assertEqual(self.const("", "to_ufixed(5, 4, 0)")[0], 5)
        self.assertEqual(self.const("", "to_sfixed(-3, 4, 0)")[0], 0b11101)

    def test_to_real_and_to_integer_of_constants(self):
        v, _ = probe("constant k : sfixed(7 downto -8) := to_sfixed(-1.25, 7, -8);", "to_real(k)", use=FX)
        self.assertEqual(v.c, -1.25)
        v, _ = probe("constant k : sfixed(7 downto -8) := to_sfixed(-1.25, 7, -8);", "to_integer(k)", use=FX)
        self.assertEqual(v.c, -2)                                                          # floor, like fixed_truncate

    def test_constant_folding_of_operators(self):
        bits, t = self.const("constant a : ufixed(3 downto -2) := to_ufixed(2.25, 3, -2);\n"
                             "constant b : ufixed(3 downto 0) := to_ufixed(3, 3, 0);", "a * b")
        self.assertEqual((t.left, t.right), (7, -2))
        self.assertEqual(bits, int(2.25 * 3 * 4))

    def test_pure_helpers(self):
        self.assertEqual(ieee.fixed_quantize(0.5, 7, -8, True), 0x80)
        self.assertEqual(ieee.fixed_quantize(-100, 3, 0, True), 0b1000)                    # saturate low
        self.assertEqual(ieee.fixed_quantize(-100, 3, 0, True, saturate=False), (-100) & 15)
        self.assertEqual(ieee.fixed_quantize(2.5, 3, 0, False, rounding=False), 2)
        self.assertEqual(ieee.fixed_to_real(0xFF80, 7, -8, True), -0.5)
        self.assertIsNone(ieee.fixed_quantize(float("inf"), 3, 0, True))


class GhdlFixedPointExample(unittest.TestCase):
    """The shape of ghdl testsuite/synth/issue1417: many ``to_sfixed`` calls must not be UNRESOLVED_OVERLOAD."""

    def test_no_unresolved_overload(self):
        body = "\n".join("constant c%d : sfixed(7 downto -8) := to_sfixed(%d.5, 7, -8);" % (i, i) for i in range(20))
        src = ("library ieee;\nuse ieee.std_logic_1164.all;\nuse ieee.fixed_pkg.all;\nuse ieee.fixed_float_types.all;\n"
               "entity e is end entity;\narchitecture a of e is\n%s\n"
               "constant r1 : sfixed(0 downto -8) := reciprocal(c3);\nbegin\nend architecture;\n" % body)
        r, _d = elab(src, top="e")
        self.assertEqual([c for c, _m in lint(r)], [])
        parsed = parse_text(src)
        self.assertEqual([d.code for d in parsed.diagnostics if d.code == "MISSING_UNIT"], [])

    def test_realistic_filter_has_no_diagnostics_and_cost(self):
        src = HDR + FX + """
entity fir is port (clk : in std_logic; x : in sfixed(7 downto -8); y : out sfixed(9 downto -8));
end entity;
architecture a of fir is
  type taps_t is array (0 to 3) of sfixed(7 downto -8);
  signal taps : taps_t;
  type coef_t is array (0 to 3) of sfixed(1 downto -14);
  constant K : coef_t := (to_sfixed(0.5, 1, -14), to_sfixed(-0.25, 1, -14), to_sfixed(0.125, 1, -14), to_sfixed(1.0, 1, -14));
  signal acc : sfixed(9 downto -8);
begin
  process (clk)
    variable s : sfixed(9 downto -8);
  begin
    if rising_edge(clk) then
      taps <= x & taps(0 to 2);
      s := (others => '0');
      for i in 0 to 3 loop
        s := s + resize(taps(i) * K(i), s'high, s'low);
      end loop;
      acc <= s;
    end if;
  end process;
  y <= acc;
end architecture;
"""
        m, r, _d = module(src, top="fir")
        self.assertEqual(lint(r), [])
        ops = {n.op for n in m.nodes}
        self.assertIn("MUL", ops)
        self.assertIn("DFF", ops)


class FloatPackage(unittest.TestCase):
    def test_types_and_widths(self):
        for name, w in (("float16", 16), ("float32", 32), ("float64", 64), ("float128", 128)):
            v, _ = probe("signal f : %s;" % name, "f", use=FLT)
            self.assertEqual(v.sig.width, w, name)
        v, _ = probe("signal f : float(8 downto -23);", "f", use=FLT)
        self.assertEqual(v.sig.width, 32)
        v, _ = probe("signal f : float32;", "f'low", use=FLT)
        self.assertEqual(v.c, -23)

    def test_constants_are_exact(self):
        v, _ = probe("", "to_float(1.5)", use=FLT)
        self.assertEqual(v.sig.const_value().value, 0x3FC00000)
        v, _ = probe("", "to_float(-0.1, 11, 52)", use=FLT)
        import struct
        self.assertEqual(v.sig.const_value().value, struct.unpack(">Q", struct.pack(">d", -0.1))[0])
        v, _ = probe("constant a : float32 := to_float(1.5); constant b : float32 := to_float(2.25);", "a * b", use=FLT)
        self.assertEqual(v.sig.const_value().value, ieee.float_encode(3.375, 8, 23))

    def test_encoder_matches_ieee_754(self):
        import struct
        for x in (0.0, 1.0, -2.5, 3.14159, 1e-40, 1e30, 65504.0, 2.0 ** -126):
            self.assertEqual(ieee.float_encode(x, 8, 23), struct.unpack(">I", struct.pack(">f", x))[0], x)
            self.assertEqual(ieee.float_decode(ieee.float_encode(x, 8, 23), 8, 23), struct.unpack(">f", struct.pack(">f", x))[0])
        self.assertEqual(ieee.float_encode(1.0, 5, 10), 0x3C00)

    def test_arithmetic_is_modelled_and_documented(self):
        src = HDR + FLT + """
entity tf is port (a, b : in float32; i : in signed(15 downto 0); s, p, q, c : out float32;
                   t : out float32; n : out signed(15 downto 0); lt : out boolean; h : out float16);
end entity;
architecture r of tf is
begin
  s <= a + b;
  p <= a * b;
  q <= a / b;
  c <= to_float(i, 8, 23);
  t <= -a;
  n <= to_signed(a, 16);
  lt <= a < b;
  h <= resize(a, 5, 10);
end architecture;
"""
        m, r, _d = module(src, top="tf")
        self.assertEqual(lint(r), [])
        ops = {n.op for n in m.nodes}
        self.assertTrue({"ADD", "SUB", "MUL", "DIV", "SHL", "SHR"} <= ops, ops)
        widths = {p.name: p.width for p in m.ports}
        self.assertEqual((widths["s"], widths["h"]), (32, 16))
        self.assertTrue(any("floating-point" in a for a in m.assumptions), m.assumptions)


# ---------------------------------------------------------------------------------------------- 2. aggregates
class WholeMemoryAggregate(unittest.TestCase):
    SRC = HDR + """
entity t is port (clk, rst, we : in std_logic; adr : in std_logic_vector(1 downto 0);
                  din : in std_logic_vector(7 downto 0); dout : out std_logic_vector(7 downto 0); rdout : out std_logic);
end entity;
architecture a of t is
  type mem_t is array (0 to 3) of std_logic_vector(7 downto 0);
  signal key_mem : mem_t;
  type r2 is record p : std_logic_vector(3 downto 0); q : std_logic; end record;
  type rmem_t is array (0 to 1) of r2;
  signal rm : rmem_t;
begin
  process (clk) begin
    if rising_edge(clk) then
      if rst = '1' then
        key_mem <= (others => (others => '0'));
        rm <= (others => (p => (others => '0'), q => '0'));
      elsif we = '1' then
        key_mem(to_integer(unsigned(adr))) <= din;
        rm(0).q <= din(0);
      end if;
    end if;
  end process;
  dout <= key_mem(to_integer(unsigned(adr)));
  rdout <= rm(0).q;
end architecture;
"""

    def test_reset_of_the_whole_array_is_a_register_file(self):
        m, r, _d = module(self.SRC)
        self.assertEqual(lint(r), [])
        ops = {n.op for n in m.nodes}
        self.assertIn("DFF", ops)
        self.assertNotIn("MEM", ops)                  # a RAM cannot be reset as a whole

    def test_behaviour(self):
        m, r, _d = module(self.SRC)
        sim = Sim(m)
        sim.step({"rst": 1, "we": 0, "adr": 0, "din": 0})
        for a in range(4):
            self.assertEqual(sim.outputs(sim.comb({"rst": 0, "we": 0, "adr": a, "din": 0}))["dout"], 0)
        sim.step({"rst": 0, "we": 1, "adr": 2, "din": 0xA5})
        self.assertEqual(sim.outputs(sim.comb({"rst": 0, "we": 0, "adr": 2, "din": 0}))["dout"], 0xA5)
        self.assertEqual(sim.outputs(sim.comb({"rst": 0, "we": 0, "adr": 1, "din": 0}))["dout"], 0)
        sim.step({"rst": 1, "we": 0, "adr": 0, "din": 0})
        self.assertEqual(sim.outputs(sim.comb({"rst": 0, "we": 0, "adr": 2, "din": 0}))["dout"], 0)

    def test_named_and_positional_memory_aggregates(self):
        src = HDR + """
entity t is port (clk : in std_logic; sel : in std_logic; din : in std_logic_vector(3 downto 0);
                  a : in std_logic_vector(1 downto 0); y : out std_logic_vector(3 downto 0));
end entity;
architecture a of t is
  type mem_t is array (0 to 3) of std_logic_vector(3 downto 0);
  signal m : mem_t;
begin
  process (clk) begin
    if rising_edge(clk) then
      if sel = '1' then
        m <= (0 => x"1", 1 => x"2", others => x"F");
      else
        m(to_integer(unsigned(a))) <= din;
      end if;
    end if;
  end process;
  y <= m(to_integer(unsigned(a)));
end architecture;
"""
        m, r, _d = module(src)
        self.assertEqual(lint(r), [])
        sim = Sim(m)
        sim.step({"sel": 1, "a": 0, "din": 0})
        got = [sim.outputs(sim.comb({"sel": 0, "a": a, "din": 0}))["y"] for a in range(4)]
        self.assertEqual(got, [1, 2, 15, 15])


class NullSliceAndRecordReturn(unittest.TestCase):
    def test_null_slice_concatenation(self):
        src = HDR + """
entity t is port (inp : in std_logic_vector(7 downto 0); y : out std_logic_vector(15 downto 0);
                  z : out std_logic_vector(7 downto 0));
end entity;
architecture a of t is
  signal null_vector : std_logic_vector(-1 downto 0) := (others => '0');
begin
  null_vector <= inp(null_vector'range);
  y <= null_vector & (7 downto 0 => '0') & inp;
  z <= inp(-1 downto 0) & (7 downto 0 => '0');
end architecture;
"""
        m, r, _d = module(src)
        self.assertEqual(lint(r), [])
        sim = Sim(m)
        for v in (0, 0x5A, 0xFF):
            out = sim.outputs(sim.comb({"inp": v}))
            self.assertEqual(out["y"], v)
            self.assertEqual(out["z"], 0)

    def test_null_array_signal_is_not_a_one_bit_stand_in(self):
        v, _ = probe("signal n : std_logic_vector(-1 downto 0);", "n & \"101\"", use="")
        self.assertEqual(v.sig.width, 3)

    def test_record_aggregate_return_on_unconstrained_record(self):
        src = HDR + """
package pk is
  type rec_t is record a : std_logic_vector; n : integer; end record;
  function mk(x : std_logic_vector) return rec_t;
end package;
package body pk is
  function mk(x : std_logic_vector) return rec_t is
  begin
    return (a => x, n => x'length);
  end function;
end package body;
library ieee;
use ieee.std_logic_1164.all;
use work.pk.all;
entity t is port (inp : in std_logic_vector(7 downto 0); y : out std_logic_vector(3 downto 0);
                  n : out integer range 0 to 255);
end entity;
architecture a of t is
  signal rr : rec_t(a(3 downto 0));
begin
  rr <= mk(inp(3 downto 0));
  y <= rr.a;
  n <= rr.n;
end architecture;
"""
        m, r, _d = module(src)
        self.assertEqual(lint(r), [])
        out = Sim(m).outputs(Sim(m).comb({"inp": 0xB6}))
        self.assertEqual((out["y"], out["n"]), (0x6, 4))


class StringPowerOperator(unittest.TestCase):
    """hdl4fpga ``hdo``: ``function "**" (object : hdo; path : string) return <T>`` for several T."""

    PKG = """
package hp is
  subtype hdo is string;
  function "**" (constant object : hdo; constant path : string) return boolean;
  function "**" (constant object : hdo; constant path : string) return integer;
  function "**" (constant object : hdo; constant path : string) return hdo;
  function "**" (constant object : hdo; constant position : natural) return hdo;
end package;
package body hp is
  function "**" (constant object : hdo; constant path : string) return boolean is
  begin
    return object = path;
  end function;
  function "**" (constant object : hdo; constant path : string) return integer is
  begin
    return object'length + path'length;
  end function;
  function "**" (constant object : hdo; constant path : string) return hdo is
  begin
    return object & path;
  end function;
  function "**" (constant object : hdo; constant position : natural) return hdo is
  begin
    return object(1 to position);
  end function;
end package body;
"""

    def test_overloads_are_selected_by_the_expected_type(self):
        src = self.PKG + """
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.hp.all;
entity tp is port (a : in std_logic; n_o, s_o, t_o : out unsigned(7 downto 0); b_o : out std_logic);
end entity;
architecture rtl of tp is
  constant obj : hdo := "abc";
  constant n : natural := obj ** ".xy";
  constant s : string := obj ** ".xy";
  constant t : string := hdo(obj) ** 2;
  constant b : boolean := obj ** "abc";
begin
  n_o <= to_unsigned(n, 8);
  s_o <= to_unsigned(s'length, 8);
  t_o <= to_unsigned(t'length, 8);
  b_o <= '1' when b else '0';
end architecture;
"""
        m, r, _d = module(src, top="tp")
        self.assertEqual(lint(r), [])
        out = Sim(m).outputs(Sim(m).comb({"a": 0}))
        self.assertEqual((out["n_o"], out["s_o"], out["t_o"], out["b_o"]), (6, 6, 2, 1))


# ---------------------------------------------------------------------------------------------- 3. syntax
VIEW_PKG = """
library ieee;
use ieee.std_logic_1164.all;
package vp is
  type chan_t is record
    valid : std_logic;
    data  : std_logic_vector(7 downto 0);
    ready : std_logic;
  end record;
  view chan_master of chan_t is
    valid : out;
    data  : out;
    ready : in;
  end view chan_master;
  alias chan_slave is chan_master'converse;
  type chan_array_t is array (natural range <>) of chan_t;
end package;
"""


class ViewsAndPortViews(unittest.TestCase):
    def test_view_declaration_parses_without_errors(self):
        r = parse_text(VIEW_PKG)
        self.assertEqual([d for d in r.diagnostics if d.severity == "error"], [])
        pkg = r.packages[("work", "vp")]
        views = [d for d in pkg.decls if isinstance(d, N.ViewDecl)]
        self.assertEqual([v.name for v in views], ["chan_master"])
        self.assertEqual([(e.name, e.mode) for e in views[0].elements],
                         [("valid", "out"), ("data", "out"), ("ready", "in")])

    def test_port_view_forms_parse(self):
        src = VIEW_PKG + """
library ieee;
use ieee.std_logic_1164.all;
use work.vp.all;
entity e is
  port (
    clk : in std_logic;
    s : view chan_slave;
    m : view chan_master of chan_t;
    mm : view (chan_master) of chan_array_t(0 to 1)
  );
end entity;
"""
        r = parse_text(src)
        self.assertEqual([d for d in r.diagnostics if d.severity == "error"], [], [d.message for d in r.diagnostics])
        ports = r.entities[("work", "e")].ports
        self.assertEqual([p.mode for p in ports], ["in", "view", "view", "view"])
        self.assertTrue(isinstance(ports[1].subtype, N.ViewInd) and ports[3].subtype.array)
        self.assertIsInstance(ports[1].subtype.mark, N.Ident)

    def test_nested_sub_views_parse(self):
        src = """
package p is
  type c_t is record d : bit; r : bit; end record;
  view c_m of c_t is d : out; r : in; end view;
  alias c_s is c_m'converse;
  type b_t is record tx : c_t; rx : c_t; end record;
  view b_m of b_t is
    tx : view c_m;
    rx : view c_s;
  end view;
end package;
"""
        r = parse_text(src)
        self.assertEqual([d for d in r.diagnostics if d.severity == "error"], [])

    def test_directions_follow_the_view(self):
        src = VIEW_PKG + """
library ieee;
use ieee.std_logic_1164.all;
use work.vp.all;
entity src is port (clk : in std_logic; m : view chan_master); end entity;
architecture a of src is begin
  m.valid <= clk;
  m.data <= (others => clk);
end architecture;
library ieee;
use ieee.std_logic_1164.all;
use work.vp.all;
entity snk is port (s : view chan_slave); end entity;
architecture a of snk is begin
  s.ready <= s.valid and s.data(0);
end architecture;
library ieee;
use ieee.std_logic_1164.all;
use work.vp.all;
entity vtop is port (clk : in std_logic; o : out std_logic); end entity;
architecture a of vtop is
  signal c : chan_t;
begin
  u1 : entity work.src port map (clk => clk, m => c);
  u2 : entity work.snk port map (s => c);
  o <= c.ready;
end architecture;
"""
        r, d = elab(src, top="vtop")
        self.assertEqual(lint(r), [])
        dirs = {}
        for m in d.modules.values():
            dirs[m.name.split("[")[0]] = {p.name: p.port for p in m.ports}
        self.assertEqual(dirs["src"], {"clk": "in", "m.valid": "out", "m.data": "out", "m.ready": "in"})
        self.assertEqual(dirs["snk"], {"s.valid": "in", "s.data": "in", "s.ready": "out"})
        top = d.modules[r.top_key]
        insts = [n for n in top.nodes if n.op == "INST"]
        self.assertEqual(len(insts), 2)


class ExternalNames(unittest.TestCase):
    def test_external_names_parse_in_all_positions(self):
        src = """
library ieee;
use ieee.std_logic_1164.all;
entity e is port (clk : in std_logic; o : out std_logic); end entity;
architecture a of e is
  alias t is << signal ^.dut.t : std_logic >>;
begin
  << signal .tb.dut.x : std_logic >> <= clk;
  o <= t and << signal @work.pkg.sig : std_logic >>;
end architecture;
"""
        r = parse_text(src)
        self.assertEqual([d.message for d in r.diagnostics if d.code == "PARSE_ERROR"], [])
        a = r.architectures[("work", "e")][-1]
        asg = [s for s in a.stmts if isinstance(s, N.SignalAssign)]
        self.assertIsInstance(asg[0].target, N.ExternalName)
        self.assertEqual(asg[0].target.path, ".tb.dut.x")
        self.assertEqual(asg[0].target.cls, "signal")
        alias = [d for d in a.decls if isinstance(d, N.AliasDecl)][0]
        self.assertIsInstance(alias.target, N.ExternalName)
        self.assertEqual(alias.target.path, "^.dut.t")

    def test_one_precise_diagnostic_per_external_name(self):
        src = """
library ieee;
use ieee.std_logic_1164.all;
entity e is port (clk : in std_logic; o : out std_logic); end entity;
architecture a of e is
begin
  << signal .tb.dut.x : std_logic >> <= clk;
  o <= << signal .tb.dut.y : std_logic >>;
end architecture;
"""
        r, _d = elab(src, top="e")
        msgs = [d.message for d in r.diagnostics if d.code == "UNSUPPORTED_CONSTRUCT"]
        self.assertEqual(len(msgs), 2, msgs)
        self.assertTrue(all("external name" in m for m in msgs), msgs)
        self.assertEqual([d for d in r.diagnostics if d.code in ("PARSE_ERROR", "UNRESOLVED_OVERLOAD")], [])


class GenericSubprogramsAndTypes(unittest.TestCase):
    def test_generic_function_in_entity_is_one_diagnostic(self):
        src = """
library ieee;
use ieee.std_logic_1164.all;
entity gen_delay is
  generic (function id(a : bit) return bit;
           W : natural := 4);
  port (i : in bit; o : out bit);
end gen_delay;
architecture arch of gen_delay is
begin
  o <= i;
end arch;
"""
        r = parse_text(src)
        self.assertEqual([d.code for d in r.diagnostics], ["UNSUPPORTED_CONSTRUCT"])
        self.assertIn("generic function 'id'", r.diagnostics[0].message)
        ent = r.entities[("work", "gen_delay")]
        self.assertEqual([(g.name, g.kind) for g in ent.generics], [("id", "subprogram"), ("w", "constant")])
        self.assertEqual([p.name for p in ent.ports], ["i", "o"])           # no cascade into the port list
        self.assertIn(("work", "gen_delay"), r.architectures)

    def test_generic_function_in_package_header(self):
        src = """
library ieee;
use ieee.std_logic_1164.all;
package func04_pkg is
  generic (function key (a: std_logic_vector) return std_logic_vector);
  function key2 (q : std_logic_vector) return std_logic_vector;
end func04_pkg;
package body func04_pkg is
  function key2 (q : std_logic_vector) return std_logic_vector is
  begin
    return q;
  end function;
end package body;
"""
        r = parse_text(src)
        self.assertEqual([d.code for d in r.diagnostics], ["UNSUPPORTED_CONSTRUCT"])

    def test_several_kinds_of_generic_subprogram_and_package(self):
        src = """
entity e is
  generic (
    pure function f (a : integer) return integer is <>;
    procedure p (signal s : out bit);
    package pk is new work.base generic map (<>);
    N : natural := 2);
  port (x : in bit);
end entity;
"""
        r = parse_text(src)
        self.assertEqual([d.code for d in r.diagnostics], ["UNSUPPORTED_CONSTRUCT"] * 3)
        self.assertEqual([p.name for p in r.entities[("work", "e")].ports], ["x"])
        self.assertEqual(r.entities[("work", "e")].generics[-1].name, "n")

    def test_legacy_generic_type(self):
        src = """
library ieee;
use ieee.std_logic_1164.all;
entity fifo is
  generic (T : type; MIN_DEPTH : natural);
  port (clk : in std_ulogic);
end entity;
"""
        r = parse_text(src)
        self.assertEqual([d.code for d in r.diagnostics if d.severity == "error"], [])
        gs = r.entities[("work", "fifo")].generics
        self.assertEqual([(g.name, g.kind) for g in gs], [("t", "type"), ("min_depth", "constant")])


# ---------------------------------------------------------------------------------------------- 4. VHDL in .v
def run_cost(*argv, cwd):
    out = io.StringIO()
    old = os.getcwd()
    try:
        os.chdir(cwd)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(["cost", "--format", "json", "--no-cache", *argv])
    finally:
        os.chdir(old)
    return code, json.loads(out.getvalue())


VHDL_TEXT = """-- a VHDL design that lives in a .v file
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
entity cnt is
  port (clk : in std_logic; q : out unsigned(7 downto 0));
end entity;
architecture rtl of cnt is
  signal r : unsigned(7 downto 0);
begin
  process (clk) begin
    if rising_edge(clk) then r <= r + 1; end if;
  end process;
  q <= r;
end architecture;
"""


class VhdlInVerilogFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._old = os.environ.get("GATECOST_CACHE_DIR")
        os.environ["GATECOST_CACHE_DIR"] = str(self.dir / "_cache")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("GATECOST_CACHE_DIR", None)
        else:
            os.environ["GATECOST_CACHE_DIR"] = self._old
        self._tmp.cleanup()

    def cost(self, name, text, *extra):
        (self.dir / name).write_text(text)
        code, doc = run_cost("--top", "cnt", name, *extra, cwd=self.dir)
        self.assertEqual(code, 0, doc.get("error"))
        return doc

    def test_dot_v_with_vhdl_is_parsed_as_vhdl_with_one_diagnostic(self):
        doc = self.cost("cnt.v", VHDL_TEXT)
        cs = [d["code"] for d in doc["diagnostics"]]
        self.assertEqual(cs.count("LANGUAGE_MISMATCH"), 1, doc["diagnostics"])
        self.assertNotIn("PARSE_ERROR", cs)
        self.assertGreater(doc["result"]["total"]["ge"]["typ"], 0)
        mism = [d for d in doc["diagnostics"] if d["code"] == "LANGUAGE_MISMATCH"][0]
        self.assertEqual(mism["file"], "cnt.v")
        self.assertFalse(mism["affects_cost"])

    def test_same_cost_as_the_vhd_file(self):
        a = self.cost("cnt.sv", VHDL_TEXT)["result"]["total"]["ge"]["typ"]
        b = self.cost("cnt.vhd", VHDL_TEXT)["result"]["total"]["ge"]["typ"]
        self.assertEqual(a, b)

    def test_license_header_and_package_first(self):
        text = "/* long\n * licence\n */\n// more\n-- and a VHDL comment\npackage pk is\n constant c : integer := 1;\nend package;\n" \
               + VHDL_TEXT
        doc = self.cost("cnt.v", text)
        self.assertEqual([d["code"] for d in doc["diagnostics"]].count("LANGUAGE_MISMATCH"), 1)
        self.assertNotIn("PARSE_ERROR", [d["code"] for d in doc["diagnostics"]])

    def test_real_verilog_is_untouched(self):
        for name, text in (("cnt.v", "module cnt(input clk, output reg [7:0] q);\n always @(posedge clk) q <= q + 1;\nendmodule\n"),
                           ("cnt.sv", "// header\n`timescale 1ns/1ps\nmodule cnt(input logic clk, output logic [7:0] q);\n"
                                      " always_ff @(posedge clk) q <= q + 1;\nendmodule\n"),
                           ("cnt.sv", "package p;\n localparam int W = 8;\nendpackage\nmodule cnt(input logic clk, "
                                      "output logic [p::W-1:0] q);\n always_ff @(posedge clk) q <= q + 1;\nendmodule\n")):
            doc = self.cost(name, text)
            self.assertNotIn("LANGUAGE_MISMATCH", [d["code"] for d in doc["diagnostics"]], text)

    def test_sniffer_unit(self):
        from gatecost.discovery import looks_like_vhdl
        yes = ["library ieee;", "-- c\nuse ieee.std_logic_1164.all;", "ENTITY x IS\nEND;", "architecture a of e is begin end;",
               "package p is end;", "package body p is end;", "/* c */ context c is end;", "configuration c of e is end;"]
        no = ["module m; endmodule", "package p; endpackage", "`define A 1\nmodule m; endmodule", "// entity x is\nmodule m;",
              "interface i; endinterface", "", "library lib1 \"*.v\";", "entity", "use"]
        for t in yes:
            self.assertTrue(looks_like_vhdl(t), t)
        for t in no:
            self.assertFalse(looks_like_vhdl(t), t)


if __name__ == "__main__":
    unittest.main()
