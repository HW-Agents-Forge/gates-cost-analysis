"""Memories written through constant addresses (COST_MODEL.md 3.8).

Regression: an unpacked array of more than 4096 elements is lowered to a ``MEM`` node even
when it is only a bundle of nets (``assign w[0] = x``, instance outputs bound to ``w[i]``).
Each constant-address drive used to be costed as a full write port (address decoder plus a
``W``-bit select on every word), so a chain of N instances through such an array cost
``O(N * D * W)`` MUX2: 9.3e5 GE at N=4000 (array of 4001 words, scalarized) jumped to
2.5e9 GE at N=8000 (8001 words, a ``MEM``).
"""
import unittest

from gatecost.cost import build_cost_tree, cost_op, load_profile
from tests import cost_ref as ref
from tests.elab_helpers import elab, top_module

P = load_profile()

LEAF = """module leaf(input logic clk, input logic [15:0] a, input logic [15:0] b, output logic [15:0] y);
  logic [15:0] r;
  always_ff @(posedge clk) r <= a + b;
  assign y = r ^ a;
endmodule
"""


def chain(n: int, depth: int) -> dict:
    body = ["module net(input logic clk, input logic [15:0] x, output logic [15:0] y);",
            f"  logic [15:0] w [0:{depth - 1}];", "  assign w[0] = x;"]
    body += [f"  leaf u{i}(.clk(clk), .a(w[{i}]), .b(x), .y(w[{i + 1}]));" for i in range(n)]
    body += [f"  assign y = w[{n}];", "endmodule"]
    return {"leaf.sv": LEAF, "net.sv": "\n".join(body) + "\n"}


def total(src: dict):
    res = elab(src, top="net")
    return build_cost_tree(res.design, P).total(), res


class NetArrayChain(unittest.TestCase):
    def test_large_net_array_costs_like_scalarized_one(self):
        small, _ = total(chain(40, 41))            # 41 words: scalarized nets
        big, res = total(chain(40, 4201))          # > 4096 words, constant indices only: nets too (no MEM)
        self.assertEqual([n.op for n in top_module(res).nodes].count("MEM"), 0)
        self.assertEqual((big.min, big.typ, big.max), (small.min, small.typ, small.max))

    def test_cost_is_linear_in_instances(self):
        a, _ = total(chain(20, 4201))
        b, _ = total(chain(40, 4201))
        self.assertEqual(b.typ, 2 * a.typ)


class ConstPortFormula(unittest.TestCase):
    def test_no_const_ports_unchanged(self):
        c = cost_op("MEM", {"depth": 16, "width": 8, "rports": 2, "wports": 2}, P)
        lo, ty, hi = ref.mem_flop(16, 8, 2, 2)
        self.assertAlmostEqual(c.typ / 1000, ty, delta=0.01)
        self.assertAlmostEqual(c.min / 1000, lo, delta=0.01)

    def test_net_array_has_no_storage(self):
        c = cost_op("MEM", {"depth": 4201, "width": 16, "rports": 3, "wports": 3, "const_reads": 3,
                            "const_writes": 3, "const_write_words": 3, "wires": True}, P)
        self.assertEqual((c.min, c.typ, c.max), (0, 0, 0))
        self.assertEqual(c.arch["typ"], "net_array")
        dyn = cost_op("MEM", {"depth": 64, "width": 8, "rports": 1, "wports": 2,
                              "const_writes": 2, "const_write_words": 2, "wires": True}, P)
        self.assertAlmostEqual(dyn.typ / 1000, 63 * 8 * ref.MUX2, delta=0.01)
        self.assertNotIn("DFF", dyn.breakdown)

    def test_clocked_const_writes_skip_decoder_and_word_selects(self):
        D, W = 256, 8
        var = cost_op("MEM", {"depth": D, "width": W, "wports": 4}, P)
        cst = cost_op("MEM", {"depth": D, "width": W, "wports": 4, "const_writes": 4, "const_write_words": 4}, P)
        one = cost_op("MEM", {"depth": D, "width": W, "wports": 1}, P)
        dec = ref.decode(8)[1]
        # storage, hold mux and read tree of a 1-port memory, without its decoder
        self.assertAlmostEqual(cst.typ / 1000, one.typ / 1000 - dec, delta=0.01)
        self.assertLess(cst.typ, var.typ)
        # two constant ports aimed at the same word: one extra W-bit select
        dup = cost_op("MEM", {"depth": D, "width": W, "wports": 4, "const_writes": 4, "const_write_words": 3}, P)
        self.assertAlmostEqual((dup.typ - cst.typ) / 1000, W * ref.MUX2, delta=0.01)
        # mixed: 1 variable port + 3 constant ones = 1-port memory + 3 W-bit selects
        mix = cost_op("MEM", {"depth": D, "width": W, "wports": 4, "const_writes": 3, "const_write_words": 3}, P)
        self.assertAlmostEqual((mix.typ - one.typ) / 1000, 3 * W * ref.MUX2, delta=0.01)

    def test_bounds_and_monotone_in_depth(self):
        prev = None
        for d in (2, 3, 16, 100, 1024, 5000):
            c = cost_op("MEM", {"depth": d, "width": 8, "wports": 3, "const_writes": 2, "const_write_words": 2}, P)
            self.assertTrue(c.min <= c.typ <= c.max)
            if prev is not None:
                self.assertLessEqual(prev, c.typ)
            prev = c.typ


if __name__ == "__main__":
    unittest.main()
