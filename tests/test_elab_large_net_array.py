"""Unpacked arrays above 4096 elements (module.py ``declare_signal``).

Regression: every unpacked array of more than 4096 elements used to become a ``MEM`` node, even when
it was only a bundle of nets reached through constant indices (``assign w[0] = x``, instance outputs
bound to ``w[i]``): thousands of constant-address ports on a fictitious memory, ``mem`` category
instead of nets.  Above the threshold an array is now a ``MEM`` only if it is a memory candidate
(variable-index procedural write, ``initial``/``$readmem`` init) or is indexed by a signal anywhere;
otherwise it is scalarised into per-element nets created on demand.
"""
import unittest

from gatecost.cost import build_cost_tree, load_profile
from tests.elab_helpers import codes, elab, top_module
from tests.test_cost_mem_const_ports import chain

P = load_profile()


def mems(res) -> list:
    return [n for n in top_module(res).nodes if n.op == "MEM"]


def cost(res):
    t = build_cost_tree(res.design, P).total()
    return (t.min, t.typ, t.max)


def dyn_read(depth: int) -> str:
    return f"""module m(input logic clk, input logic [12:0] wa, ra, input logic [7:0] d, input logic we,
         output logic [7:0] q);
  logic [7:0] mem [0:{depth - 1}];
  always_ff @(posedge clk) if (we) mem[wa] <= d;
  assign q = mem[ra];
endmodule
"""


class LargeNetArray(unittest.TestCase):
    def test_constant_index_array_above_threshold_is_nets(self):
        big = elab(chain(40, 4201), top="net")
        small = elab(chain(40, 41), top="net")
        self.assertEqual(mems(big), [])
        self.assertEqual(cost(big), cost(small))
        self.assertEqual(codes(big), codes(small))
        # only the elements actually used become nets (41 of 4201)
        names = [n.name for n in top_module(big).nets if "w[" in (n.name or "")]
        self.assertLessEqual(len(names), 41)

    def test_constant_out_of_range_index_still_reported(self):
        src = chain(4, 5000)["net.sv"].replace("assign y = w[4];", "assign y = w[6000];")
        res = elab({"leaf.sv": chain(1, 2)["leaf.sv"], "net.sv": src}, top="net")
        self.assertEqual(mems(res), [])
        ref = elab({"leaf.sv": chain(1, 2)["leaf.sv"],
                    "net.sv": chain(4, 5)["net.sv"].replace("assign y = w[4];", "assign y = w[6000];")},
                   top="net")
        self.assertEqual(codes(res), codes(ref))

    def test_variable_read_index_above_threshold_stays_mem(self):
        src = """module m(input logic [12:0] s, input logic [7:0] x, output logic [7:0] y);
  logic [7:0] w [0:4999];
  assign w[0] = x;
  assign w[1] = ~x;
  assign y = w[s];
endmodule
"""
        res = elab(src)
        self.assertEqual(len(mems(res)), 1)
        self.assertNotIn("LIMIT_REACHED", codes(res))

    def test_variable_write_index_above_threshold_stays_mem(self):
        res = elab(dyn_read(8192))
        self.assertEqual(len(mems(res)), 1)
        self.assertEqual(mems(res)[0].attrs["depth"], 8192)

    def test_ordinary_memory_unchanged(self):
        res = elab(dyn_read(16))
        self.assertEqual(len(mems(res)), 1)
        self.assertEqual(mems(res)[0].attrs["depth"], 16)


if __name__ == "__main__":
    unittest.main()
