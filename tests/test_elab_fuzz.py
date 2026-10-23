"""Robustness: mutated designs must elaborate without raising (only GatecostError for a missing top / no
sources), terminate quickly and return well-formed diagnostics (FRONTEND.md section 9)."""
import random
import time
import unittest

from gatecost.errors import GatecostError
from gatecost.result import Diagnostic
from tests import elab_designs as D
from tests.elab_helpers import elab
from tests.test_verilog_fuzz import CORPUS, mutate, spans
from gatecost.frontend.verilog.lexer import lex  # noqa: F401

HIER = """
package p; localparam int N = 3; typedef enum logic [1:0] {A, B, C} e_t; function automatic int f(int x); return x + N; endfunction endpackage
interface bi #(parameter W = 4)(input clk); logic [W-1:0] d; modport m(output d); endinterface
module leaf #(parameter W = 4)(input [W-1:0] a, output [W-1:0] y); assign y = a + p::f(1); endmodule
module top import p::*; (input clk, input [3:0] a, output reg [3:0] q, output [3:0] y);
  e_t st;
  reg [3:0] mem [0:7];
  genvar i;
  generate for (i = 0; i < N; i = i + 1) begin : g leaf #(.W(4)) u (.a(a), .y()); end endgenerate
  leaf l (.a(a), .y(y));
  always @(posedge clk) begin
    case (st) A: st <= B; B: st <= C; default: st <= A; endcase
    mem[a[2:0]] <= a; q <= mem[a[2:0]];
    for (int k = 0; k < 3; k++) q[k] <= a[k];
  end
endmodule
"""

SOURCES = {"hier": HIER}
for name, (src, _top) in D.DESIGNS.items():
    SOURCES[name] = src
SOURCES.update({k: v for k, v in CORPUS.items()})
MUTATIONS = 12


class Fuzz(unittest.TestCase):
    def check(self, text):
        t0 = time.perf_counter()
        try:
            r = elab(text)
        except GatecostError as exc:
            self.assertIn(exc.code, ("NO_SOURCES", "TOP_NOT_FOUND"))
            return None
        dt = time.perf_counter() - t0
        self.assertLess(dt, 10.0, "elaboration took too long")
        for d in r.diagnostics:
            self.assertIsInstance(d, Diagnostic)
            self.assertTrue(d.code and d.message)
            self.assertNotEqual(d.code, "INTERNAL", (d.message, d.line))
        for m in r.design.modules.values():
            self.assertTrue(m.analyzed)
        return r

    def test_unmutated_sources(self):
        for name, text in SOURCES.items():
            with self.subTest(name):
                self.check(text)

    def test_seeded_mutations(self):
        rng = random.Random(20240601)
        for name in sorted(SOURCES):
            text = SOURCES[name]
            sp = spans(text)
            for k in range(MUTATIONS):
                mt = mutate(rng, text, sp)
                with self.subTest(fixture=name, k=k):
                    self.check(mt)

    def test_truncation_at_every_line(self):
        lines = HIER.splitlines(True)
        for i in range(1, len(lines)):
            with self.subTest(line=i):
                self.check("".join(lines[:i]))


if __name__ == "__main__":
    unittest.main()
