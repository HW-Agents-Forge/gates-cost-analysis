"""Error handling of the elaborator: no exception is swallowed silently (FRONTEND.md 1, 6; IMPLEMENTATION.md 1).

* A *speculative probe* (a fast path tried before the general one, an overload heuristic) catches only the
  exceptions by which the elaborator reports, by design, that a construct is outside that path
  (``core.PROBE_MISS``) and takes its fallback.  Any other exception is an elaborator bug: it reaches the
  statement boundary handler, which reports it as ``UNSUPPORTED_CONSTRUCT`` quoting the exception.
* Limits (``StopLimit``) are never swallowed by a probe.
* Fallbacks that degrade a value (a parameter type, a block generic) report it.
* The language boundary (Verilog instance of a VHDL entity) quotes the reason of a failure.
* The constant-function fast path counts unexpected exceptions and re-raises them in debug mode.

Internal failures are injected with ``unittest.mock``: valid designs never raise in the probes.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from typing import Any, Iterator
from unittest import mock

from gatecost.elab import constfunc
from gatecost.elab import elaborate_project
from gatecost.elab.vhdl import core
from gatecost.elab.vhdl.build import Elab, _Foreign
from gatecost.elab.vhdl.core import NotConst, StopLimit, Unsupported, failure_text

from tests.test_elab_vhdl_util import elab, unit


class _ProbeBug(RuntimeError):
    """An unexpected exception (an elaborator bug) injected into a probe."""


@contextmanager
def failing(method: str, caller: str, exc: BaseException, depth: int = 6) -> Iterator[None]:
    """Make ``Elab.<method>`` raise ``exc`` when it is called (directly or through a few helper frames,
    comprehensions included) from ``Elab.<caller>``; other calls are unaffected."""
    orig = getattr(Elab, method)

    def wrapper(self: Any, *a: Any, **k: Any) -> Any:
        f = sys._getframe(1)
        for _ in range(depth):
            if f is None:
                break
            if f.f_code.co_name == caller:
                raise exc
            f = f.f_back
        return orig(self, *a, **k)

    with mock.patch.object(Elab, method, wrapper):
        yield


def messages(r: Any, code: str | None = None) -> list[str]:
    return [d.message for d in r.diagnostics if code is None or d.code == code]


ROM = unit("sel : in std_logic_vector(2 downto 0); y : out std_logic_vector(7 downto 0)", "",
           "process(sel) begin case sel is when \"000\" => y <= x\"11\"; when \"001\" => y <= x\"22\"; "
           "when \"010\" => y <= x\"33\"; when \"011\" => y <= x\"44\"; when others => y <= x\"00\"; end case; end process;")

MEM = unit("clk, we : in std_logic; wa, ra : in unsigned(1 downto 0); d : in unsigned(7 downto 0); "
           "y : out unsigned(7 downto 0)",
           "type mem_t is array (0 to 3) of unsigned(7 downto 0);\nsignal mem : mem_t;",
           "process(clk) begin if rising_edge(clk) then if we = '1' then mem(to_integer(wa)) <= d; end if; "
           "mem(0 to 2) <= mem(1 to 3); end if; end process;\ny <= mem(to_integer(ra));")

ARST = unit("clk, rst : in std_logic; d : in unsigned(7 downto 0); q : out unsigned(7 downto 0)", "",
            "process(clk, rst) begin if rst = '1' then q <= (others => '0'); elsif rising_edge(clk) then q <= d; "
            "end if; end process;")

OVERLOAD = unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)",
                "function f(x : unsigned) return unsigned is begin return x + 1; end function;\n"
                "function f(x : integer) return integer is begin return x + 2; end function;",
                "y <= f(a);")

RESULT_OVERLOAD = unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)",
                       "function g(x : unsigned) return integer is begin return 1; end function;\n"
                       "function g(x : unsigned) return unsigned is begin return x; end function;",
                       "y <= g(a);")

USER_RESIZE = unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)",
                   "function resize(x : std_logic_vector; n : natural) return std_logic_vector is "
                   "begin return x; end function;",
                   "y <= resize(a, 8);")

CALL = unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)",
            "function inc(x : unsigned(7 downto 0)) return unsigned is begin return x + 1; end function;",
            "y <= inc(a);")

NO_BODY = unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)",
               "function nb(x : unsigned) return unsigned;",
               "y <= nb(a);")

SIGNAL_PARAM = unit("clk : in std_logic; a : in std_logic; y : out std_logic", "",
                    "process(clk)\n procedure cp(signal s : in std_logic) is begin y <= s; end procedure;\n"
                    "begin if rising_edge(clk) then cp(a); end if; end process;")

BLOCK = unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)", "",
             "blk: block generic (N : integer); generic map (N => 3);\nbegin y <= a + N; end block;")

#: (probe, design, method made to fail, caller that is the probe[, frames searched for the caller])
PROBES = [
    ("try_case_rom", ROM, "lvalue", "try_case_rom"),
    ("rom_from_alts", ROM, "fit", "rom_from_alts"),
    ("mem_slice_parts", MEM, "eval_range", "mem_slice_parts"),
    ("reset_term", ARST, "sig_of", "reset_term"),
    ("pick_overload", OVERLOAD, "eval", "pick_overload"),
    ("user_params_match", OVERLOAD, "resolve_subtype", "user_params_match"),
    ("ret_fits", RESULT_OVERLOAD, "resolve_subtype", "ret_fits"),
    ("user_overload_fits", USER_RESIZE, "eval", "user_overload_fits"),
    ("bind_params type", CALL, "resolve_subtype", "bind_params"),
    ("bind_params signal", SIGNAL_PARAM, "lv_static_sig", "bind_params"),
    ("unknown_ret", NO_BODY, "resolve_subtype", "unknown_ret"),
    ("block generic map", BLOCK, "eval", "elab_block", 1),     # not the statements of the block
]


class ProbeErrorTests(unittest.TestCase):
    def test_designs_are_clean_without_injection(self) -> None:
        for name, src, *_rest in PROBES:
            if name in ("unknown_ret", "user_overload_fits"):
                continue                    # these designs carry their own (unrelated) diagnostics
            with self.subTest(probe=name):
                r, _d = elab(src)
                self.assertIsNotNone(r.top_key)
                self.assertEqual(messages(r, "UNSUPPORTED_CONSTRUCT"), [])

    def test_unexpected_exception_in_a_probe_is_reported(self) -> None:
        # regression: every probe caught ``Exception`` and fell back silently, hiding elaborator bugs
        for name, src, method, caller, *depth in PROBES:
            with self.subTest(probe=name), failing(method, caller, _ProbeBug("probe bug"), *depth):
                r, _d = elab(src)
                self.assertTrue(any("_ProbeBug: probe bug" in m for m in messages(r, "UNSUPPORTED_CONSTRUCT")),
                                messages(r))

    def test_expected_miss_in_a_probe_keeps_the_fallback(self) -> None:
        # by design: NotConst / Unsupported only mean "not this path"; the result is the general path's
        for name, src, method, caller, *depth in PROBES:
            if name in ("bind_params type", "block generic map"):
                continue                    # these fallbacks degrade a value: reported (tests below)
            with self.subTest(probe=name):
                r0, d0 = elab(src)
                for exc in (NotConst(), Unsupported("not this shape")):
                    with failing(method, caller, exc, *depth):
                        r, d = elab(src)
                    self.assertEqual(sorted(d.modules), sorted(d0.modules))
                    self.assertFalse(any("not this shape" in m for m in messages(r)), messages(r))

    def test_step_limit_is_not_swallowed_by_a_probe(self) -> None:
        # regression: StopLimit raised inside pick_overload was swallowed and the call went on
        src = unit("a : in unsigned(7 downto 0); y : out unsigned(7 downto 0)",
                   "function f(x : unsigned) return unsigned is begin return x + 1; end function;\n"
                   "function f(x : integer) return integer is begin return x + 2; end function;\n"
                   "function h(x : unsigned) return unsigned is begin return f(x); end function;",
                   "y <= h(a);")
        r0, _ = elab(src)
        self.assertEqual(messages(r0, "LIMIT_REACHED"), [])
        with failing("eval", "pick_overload", StopLimit("interpreter steps")):
            r, _d = elab(src)
        self.assertTrue(messages(r, "LIMIT_REACHED"), messages(r))

    def test_unresolvable_parameter_type_is_reported(self) -> None:
        # regression: bind_params dropped the parameter type silently (the value was not converted)
        with failing("resolve_subtype", "bind_params", Unsupported("odd subtype")):
            r, _d = elab(CALL)
        self.assertTrue(any("parameter 'x' of 'inc'" in m and "odd subtype" in m
                            for m in messages(r, "UNSUPPORTED_CONSTRUCT")), messages(r))

    def test_unevaluable_block_generic_is_reported(self) -> None:
        # regression: a block generic whose actual could not be evaluated was left undefined silently
        with failing("eval", "elab_block", Unsupported("odd actual"), 1):
            r, _d = elab(BLOCK)
        self.assertTrue(any("generic 'n' of block 'blk'" in m and "odd actual" in m
                            for m in messages(r, "UNRESOLVED_PARAM")), messages(r))


class BoundaryHelperTests(unittest.TestCase):
    def test_failure_text_quotes_the_exception(self) -> None:
        self.assertEqual(failure_text(object(), ValueError("bad width")), "ValueError: bad width")

    def test_failure_text_reraises_in_debug_mode(self) -> None:
        class Dbg:
            debug = True
        with self.assertRaises(KeyError):
            failure_text(Dbg(), KeyError("k"))

    def test_probe_misses_are_the_designed_degradations(self) -> None:
        self.assertEqual(set(core.PROBE_MISS), {NotConst, Unsupported})
        self.assertFalse(issubclass(StopLimit, core.PROBE_MISS))
        self.assertFalse(issubclass(RecursionError, core.PROBE_MISS))


LEAF = ("library ieee;\nuse ieee.std_logic_1164.all;\n"
        "entity leaf is port (a : in std_logic_vector(3 downto 0); y : out std_logic_vector(3 downto 0)); end entity;\n"
        "architecture r of leaf is begin y <= not a; end architecture;\n")
TOP = "module t(input [3:0] a, output [3:0] y); leaf u(.a(a), .y(y)); endmodule\n"


class ForeignBoundaryTests(unittest.TestCase):
    def _run(self) -> Any:
        with tempfile.TemporaryDirectory() as td:
            for name, text in (("t.v", TOP), ("leaf.vhd", LEAF)):
                with open(os.path.join(td, name), "w") as fh:
                    fh.write(text)
            return elaborate_project(["t.v", "leaf.vhd"], root=td, top="t")

    def test_reason_of_a_foreign_failure_is_quoted(self) -> None:
        # regression: the exception was swallowed; MISSING_MODULE did not say why
        self.assertEqual([d.code for d in self._run().diagnostics if d.code == "MISSING_MODULE"], [])

        def boom(self: Any, params: Any, cp: Any) -> Any:
            raise _ProbeBug("vhdl side failed")
        with mock.patch.object(_Foreign, "elaborate", boom):
            r = self._run()
        msgs = [d.message for d in r.diagnostics if d.code == "MISSING_MODULE"]
        self.assertTrue(any("_ProbeBug: vhdl side failed" in m for m in msgs), msgs)


class FastFuncTests(unittest.TestCase):
    @staticmethod
    def _fast(fn: Any) -> constfunc.FastFn:
        return constfunc.FastFn(fn, (8,), "f", [0], 1 << 30)

    def test_designed_bail_out_is_not_an_internal_error(self) -> None:
        def bail(x: int) -> int:
            raise constfunc.Bail("uninitialised value")
        n = constfunc.INTERNAL_ERRORS["count"]
        for fn in (bail, lambda x: 1 // (x - x), lambda x: 1 << -x):
            self.assertIs(self._fast(fn).run((1,)), constfunc.FAIL)
        self.assertEqual(constfunc.INTERNAL_ERRORS["count"], n)

    def test_unexpected_exception_is_counted(self) -> None:
        # regression: a translator bug in generated code was swallowed without a trace
        def bug(x: int) -> int:
            raise TypeError("translator bug")
        n = constfunc.INTERNAL_ERRORS["count"]
        self.assertIs(self._fast(bug).run((1,)), constfunc.FAIL)      # the exact general path answers
        self.assertEqual(constfunc.INTERNAL_ERRORS["count"], n + 1)

    def test_unexpected_exception_is_raised_in_debug_mode(self) -> None:
        def bug(x: int) -> int:
            raise TypeError("translator bug")
        with mock.patch.object(constfunc, "_DEBUG", True), self.assertRaises(TypeError):
            self._fast(bug).run((1,))


if __name__ == "__main__":
    unittest.main()
