"""Structural guards for the performance work: algorithmic bounds (counted, not timed), laziness of the
digests, the fused module scan, the split of signals, the operation packs of the analysis cache."""
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gatecost import cache as cache_mod
from gatecost.cache import Cache
from gatecost.cost.tree import CostOp, DesignCostTree, InstanceEdge, LazyModuleCost, ModuleCost
from gatecost.elab import elaborate_project
from gatecost.elab.scan import mem_names_of, scan_module
from gatecost.frontend.verilog import ast as A
from gatecost.frontend.verilog import parse_files
from gatecost.ir import SigSpec
from gatecost.ir import lowering
from gatecost.pipeline import SideResult
from gatecost.result import Diagnostic, Ge3
from tests.elab_helpers import elab

WIDE = """\
module m(input clk, input [9:0] index, input set, input clr, output [{m}:0] q);
  reg [{m}:0] valid;
  always @(posedge clk) begin
    if (set) valid[index] <= 1'b1;
    if (clr) valid[index] <= 1'b0;
  end
  assign q = valid;
endmodule
"""


class WideRegister(unittest.TestCase):
    """A wide register written through a variable index (two writes in one block) used to cost O(width^3)."""

    def count_mux1(self, width: int) -> int:
        calls = [0]
        orig = lowering._mux1

        def counting(*a, **k):
            calls[0] += 1
            return orig(*a, **k)
        with mock.patch.object(lowering, "_mux1", counting), tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.v"
            p.write_text(WIDE.format(m=width - 1))
            res = elaborate_project([str(p)], root=td)
        self.assertEqual(len([n for n in res.design.modules[res.top_key].nodes if n.op == "DFF"]), width)
        return calls[0]

    def test_hold_analysis_is_quadratic_not_cubic(self):
        small, large = self.count_mux1(40), self.count_mux1(160)
        self.assertLess(large, 4 * 160 * 160)          # was about 162 * 160 * 160
        self.assertLess(large / small, 20)             # 16 for a quadratic law, 64 for a cubic one


class Laziness(unittest.TestCase):
    def test_digests_are_computed_only_on_demand(self):
        src = "module a(input x, output y); assign y = ~x; endmodule\nmodule top(input x, output y); a u(.x(x), .y(y)); endmodule\n"
        with mock.patch("gatecost.frontend.verilog.norm.node_hash", side_effect=AssertionError("eager digest")):
            res = elab(src, top="top")
            self.assertIsNotNone(res.design)
        res = elab(src, top="top")
        self.assertEqual(set(res.parsed_hashes), {"a", "top"})
        self.assertTrue(all(len(v) > 8 for v in res.parsed_hashes.values()))

    def test_vhdl_unit_hash_is_lazy(self):
        from gatecost.frontend.vhdl import parse_text

        r = parse_text("entity e is end; architecture a of e is begin end;")
        ent = r.entities[("work", "e")]
        self.assertIsNone(ent._hash)
        self.assertEqual(r.hashes[("entity", "work", "e")], ent.hash)
        self.assertIsNotNone(ent._hash)


class FusedScan(unittest.TestCase):
    SRC = """\
module m #(parameter N = 4) (input clk, input [N-1:0] a, b, output reg [N-1:0] q);
  reg [N-1:0] mem [0:7];
  reg [2:0] ptr;
  integer i;
  wire [N-1:0] w = a & b;
  sub u0 (.x(a), .y());
  always @(posedge clk) begin
    mem[ptr] <= a;
    for (i = 0; i < 2; i = i + 1) q <= q ^ mem[i];
    q <= mem[ptr] ^ w;
  end
  generate if (N > 2) begin : g
    sub u1 (.x(b), .y());
  end endgenerate
endmodule
module sub(input [3:0] x, output [3:0] y); assign y = x; endmodule
"""

    def test_scan_matches_walks(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.sv"
            p.write_text(self.SRC)
            parsed = parse_files([str(p)])
        m = parsed.module_map()["m"]
        sc = scan_module(m)
        insts = {n.module for it in m.items for n in A.walk(it) if type(n) is A.Instance}
        self.assertEqual(sc.insts, insts)
        refs = {}
        for idx, it in enumerate(m.items):
            for n in A.walk(it):
                if type(n) is A.Ident:
                    refs.setdefault(n.name, set()).add(idx)
        self.assertEqual(sc.refs, refs)
        self.assertEqual(mem_names_of(sc), {"mem"})


class SplitSignals(unittest.TestCase):
    def test_split_equals_slices(self):
        from gatecost.ir import Builder

        rnd = random.Random(3)
        b = Builder("t")
        nets = [b.port(f"i{k}", "in", rnd.choice([1, 3, 8, 13])) for k in range(6)]
        for _ in range(200):
            parts = []
            for _ in range(rnd.randint(1, 6)):
                kind = rnd.random()
                if kind < 0.4:
                    parts.append(SigSpec.const(rnd.getrandbits(5), 5, rnd.getrandbits(5) if rnd.random() < 0.3 else 0))
                else:
                    n = rnd.choice(nets)
                    lo = rnd.randrange(n.width)
                    parts.append(SigSpec.of(n, lo, rnd.randint(1, n.width - lo)))
            sig = SigSpec.concat(parts)
            for n in (1, 2, 3, 5):
                if sig.width % n == 0:
                    self.assertEqual([x.key() for x in sig.split(n)],
                                     [sig.slice(i * n, n).key() for i in range(sig.width // n)])
        with self.assertRaises(ValueError):
            SigSpec.const(0, 5).split(2)


def sample_tree(n_modules=3):
    mods, counts = {}, {}
    for k in range(n_modules):
        op = CostOp(1, "ADD", "addcmp", "good", Ge3(10 + k, 20 + k, 30 + k), 2, "a.v", 3, named_key=("x", k, ("y",)),
                    declared_widths=(8, 8), tags=("shared",), breakdown={"FA": 7}, arch={"typ": "rca"},
                    facts={"widths": {"a": 8}, "origins": {"a": ("a.v", 3)}, "__t": 1}, members=(2, 3))
        key = f"m{k}"
        mods[key] = ModuleCost(key, f"m{k}", (("W", "8"),), f"h{k}", (op,), (InstanceEdge("u", "m0", 2),) if k else (),
                               (Diagnostic("X", "m", line=2),), "a.v", 1)
        counts[key] = 1 + k
    return DesignCostTree("m1", mods, counts, (Diagnostic("D", "d"),), ("assumed",), {"profile": "p"}, frozenset({"m0"}))


class OperationPacks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "c"
        self.key = "ab" * 20

    def test_roundtrip_is_lazy_and_exact(self):
        tree = sample_tree()
        c = Cache(self.dir)
        self.assertTrue(c.put(self.key, SideResult(tree)))
        packs = list(self.dir.rglob("*.gcp"))
        self.assertEqual(len(packs), 1)
        c2 = Cache(self.dir)
        with mock.patch.object(Cache, "_read_ops", wraps=c2._read_ops) as rd:
            got = c2.get(self.key)
            self.assertEqual(rd.call_count, 0)
            t2 = got.tree
            self.assertTrue(all(isinstance(m, LazyModuleCost) for m in t2.modules.values()))
            self.assertEqual(t2.total(), tree.total())            # from the stored sums
            self.assertEqual(rd.call_count, 0)
            self.assertEqual(t2.modules["m1"].ops, tree.modules["m1"].ops)
            self.assertEqual(rd.call_count, 1)
        self.assertEqual(t2.modules, tree.modules)
        self.assertEqual((t2.top_key, t2.instance_counts, t2.diagnostics, t2.unsupported_in),
                         (tree.top_key, tree.instance_counts, tree.diagnostics, tree.unsupported_in))

    def test_second_tree_shares_the_pack_of_unchanged_modules(self):
        tree = sample_tree()
        c = Cache(self.dir)
        c.put(self.key, SideResult(tree))
        got = Cache(self.dir).get(self.key).tree
        new = ModuleCost("m9", "m9", (), "h9", (tree.modules["m0"].ops[0],), (), (), "a.v", 1)
        mods = dict(got.modules)
        mods["m9"] = new
        t2 = DesignCostTree("m1", mods, {**got.instance_counts, "m9": 1})
        c3 = Cache(self.dir)
        c3.put("cd" * 20, SideResult(t2))
        self.assertEqual(len(list(self.dir.rglob("*.gcp"))), 2)       # the original pack and the one with m9
        r = Cache(self.dir).get("cd" * 20).tree
        self.assertEqual(r.modules["m9"].ops, new.ops)
        self.assertEqual(r.modules["m2"].ops, tree.modules["m2"].ops)

    def test_missing_or_corrupt_pack_is_a_miss(self):
        c = Cache(self.dir)
        c.put(self.key, SideResult(sample_tree()))
        pack = next(self.dir.rglob("*.gcp"))
        data = pack.read_bytes()
        pack.write_bytes(b"XXXXX" + data[5:])                       # corrupt: detected when the operations are read
        got = Cache(self.dir).get(self.key)
        with self.assertRaises(RuntimeError):
            got.tree.modules["m0"].ops
        pack.unlink()                                               # gone: the entry referencing it is a miss
        self.assertIsNone(Cache(self.dir).get(self.key))

    def test_dict_keys_that_look_like_tags_roundtrip(self):
        v = {"__t": 1, "__tuple": [1, 2], "ok": ("a", 2), "n": {"__mc": 3}}
        self.assertEqual(cache_mod.decode(json.loads(json.dumps(cache_mod.encode(v)))), v)


if __name__ == "__main__":
    unittest.main()
