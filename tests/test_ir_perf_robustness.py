"""Regression tests for the IR perf/robustness pass (IR.md sections 6, 6.1, 7).

- keys: the output position of a net (``root`` and instance port names) is an O(1) lookup, not a
  ``list.index`` per output (quadratic on instances with many outputs), with identical keys;
- check: the cycle path is cut with an O(1) position lookup and wide connections are split in one pass,
  with the same connection order as splicing them one at a time;
- knownbits: exact folding of a malformed node falls back to the transfer rules only for the errors
  folding can raise; any other exception is a defect and propagates.
"""
import unittest
from unittest import mock

from gatecost.ir import Builder, SigSpec, analyze_ir, check_module, dump
from gatecost.ir import check as check_mod
from gatecost.ir import knownbits
from gatecost.ir.core import Conn, ModuleIR, Net, Node
from gatecost.ir.graph import topo_order
from gatecost.ir.keys import H, KeyBuilder, _i, _s, compute_keys


class NoIndexTuple(tuple):
    """An outputs tuple that fails on any linear ``index`` search."""

    def index(self, *a, **k):  # type: ignore[override]
        raise AssertionError("linear outputs.index() search")


def _wide_instance(n: int) -> tuple[ModuleIR, dict[str, SigSpec]]:
    b = Builder("t")
    a = b.port("a", "in", 1)
    outs = b.inst("child", "u", {"a": a.sig}, {f"o{i:04d}": 1 + i % 3 for i in range(n)})
    for i, (p, s) in enumerate(sorted(outs.items())):
        if i % 2 == 0:
            o = b.port(f"q{i}", "out", s.width)
            b.connect(o, b.not_(s))
    return b.finalize(), outs


class KeysOutputIndex(unittest.TestCase):
    def test_instance_keys_do_not_scan_outputs(self):
        m, _ = _wide_instance(300)
        ref = _wide_instance(300)[0]
        order, _ = topo_order(ref)
        compute_keys(ref, order)
        inst = next(n for n in m.nodes if n.op == "INST")
        inst.outputs = NoIndexTuple(inst.outputs)
        order, _ = topo_order(m)
        compute_keys(m, order)                       # raised AssertionError with list.index
        self.assertEqual(m.canonical_hash, ref.canonical_hash)
        self.assertEqual(sorted((n.named_key, n.shape) for n in m.nodes),
                         sorted((n.named_key, n.shape) for n in ref.nodes))

    def test_instance_output_root_uses_the_port_name_at_its_position(self):
        m, outs = _wide_instance(7)
        order, _ = topo_order(m)
        seen: list[KeyBuilder] = []
        orig = KeyBuilder.__init__

        def spy(self: KeyBuilder, mod: ModuleIR) -> None:
            orig(self, mod)
            seen.append(self)
        with mock.patch.object(KeyBuilder, "__init__", spy):
            compute_keys(m, order)
        kb = seen[0]
        inst = next(n for n in m.nodes if n.op == "INST")
        ports = inst.attrs["out_ports"]
        self.assertEqual(sorted(outs), list(ports))
        for i, o in enumerate(inst.outputs):
            expected = H(b"instout" + _s("child") + _s("u") + _s(ports[i]) + _i(o.width))
            self.assertEqual(kb.root_hash[o.id], expected, ports[i])

    def test_output_index_matches_list_index_semantics(self):
        m = ModuleIR("x")
        n0, n1, n2 = Net(1, 1), Net(2, 2), Net(3, 3)
        twin = Net(2, 2)                             # foreign net sharing an id (malformed IR)
        nd = Node(10, "INST", (), (n0, n1, n2, n1), {}, None)
        kb = KeyBuilder(m)
        for net in (n0, n1, n2):
            self.assertEqual(kb.output_index(nd, net), list(nd.outputs).index(net))
        self.assertEqual(kb.output_index(nd, n1), 1)             # first occurrence
        with self.assertRaises(ValueError):
            kb.output_index(nd, twin)
        with self.assertRaises(ValueError):
            kb.output_index(nd, Net(99, 1))


class LoopCutting(unittest.TestCase):
    def test_false_loop_through_a_concatenation_is_split_not_cut(self):
        # e[3:0] = a; e[7:4] = ~e[3:0], written as one connection e <- {~e[3:0], a}
        b = Builder("cat")
        a = b.port("a", "in", 4)
        y = b.port("y", "out", 8)
        e = b.net(8, "e")
        b.connect(e, SigSpec.concat([a.sig, b.not_(SigSpec.of(e, 0, 4))]))
        b.connect(y, e.sig)
        m = b.finalize()
        self.assertEqual([d.code for d in m.diagnostics if d.code == "COMBINATIONAL_LOOP"], [])
        self.assertEqual(check_module(m), [])
        self.assertEqual([len(c.src.chunks) for c in m.conns_by_net[e.id]], [1, 1])

    def _wide_module(self) -> ModuleIR:
        b = Builder("w")
        a = b.port("a", "in", 4)
        c = b.port("c", "in", 4)
        nets = [b.net(12, f"n{i}") for i in range(5)]
        for i, n in enumerate(nets):
            b.connect(n, SigSpec.concat([a.sig, SigSpec.const(i, 4), c.sig]))
        y = b.port("y", "out", 12)
        b.connect(y, nets[0].sig)
        return b.module

    @staticmethod
    def _shape(m: ModuleIR) -> tuple:
        conns = tuple((c.net.id, c.lo, c.src.key()) for c in m.conns)
        by_net = tuple((k, tuple((c.net.id, c.lo, c.src.key()) for c in v))
                       for k, v in sorted(m.conns_by_net.items()))
        return conns, by_net

    def test_batched_split_matches_one_at_a_time_splicing(self):
        def reference(m: ModuleIR, conn: Conn) -> None:      # the former _split_conn
            parts = check_mod._conn_parts(conn)
            i = next(k for k, x in enumerate(m.conns) if x is conn)
            m.conns[i:i + 1] = parts
            lst = m.conns_by_net[conn.net.id]
            j = next(k for k, x in enumerate(lst) if x is conn)
            lst[j:j + 1] = parts

        m1, m2 = self._wide_module(), self._wide_module()
        pick = [1, 3, 4, 0]
        w1 = [c for c in m1.conns if len(c.src.chunks) > 1]
        w2 = [c for c in m2.conns if len(c.src.chunks) > 1]
        for k in pick:
            reference(m1, w1[k])
        conns_alias = m2.conns
        check_mod._split_conns(m2, [w2[k] for k in pick])
        self.assertEqual(self._shape(m1), self._shape(m2))
        self.assertIs(m2.conns, conns_alias)                 # mutated in place, aliases stay valid
        self.assertEqual(sum(len(c.src.chunks) > 1 for c in m2.conns), 1)

    def test_long_ring_is_cut_once_and_deterministically(self):
        def ring(n: int) -> ModuleIR:
            b = Builder("ring")
            a = b.port("a", "in", 1)
            y = b.port("y", "out", 1)
            ws = [b.net(1, f"w{i}") for i in range(n)]
            for i in range(n):
                prev = ws[i - 1]
                src = b.xor(prev.sig, a.sig, 1) if i % 2 else b.not_(prev.sig)
                b.connect(ws[i], src)
            b.connect(y, ws[-1].sig)
            return b.finalize()
        m1, m2 = ring(1500), ring(1500)
        self.assertEqual([d.code for d in m1.diagnostics].count("COMBINATIONAL_LOOP"), 1)
        self.assertEqual(check_module(m1), [])
        analyze_ir(m1)
        analyze_ir(m2)
        self.assertEqual(dump(m1), dump(m2))
        self.assertEqual(m1.canonical_hash, m2.canonical_hash)


class KnownBitsFolding(unittest.TestCase):
    def _pow_node(self) -> tuple[Node, Net]:
        b = Builder("p")
        a = b.port("a", "in", 8)
        y = b.port("y", "out", 8)
        b.connect(y, b.pow(a.sig, 3, 8))
        m = b.finalize()
        nd = next(n for n in m.nodes if n.op == "POW")
        a.kmask, a.kval = 0xFF, 3                    # the operand is fully known
        return nd, a

    def test_fully_known_node_is_folded_exactly(self):
        nd, _ = self._pow_node()
        self.assertEqual(knownbits.node_known(nd), [(0xFF, 27)])

    def test_malformed_node_falls_back_without_raising(self):
        nd, a = self._pow_node()
        del nd.attrs["exp"]                          # KeyError inside eval_const
        self.assertEqual(len(knownbits.node_known(nd)), 1)
        nd.attrs["exp"] = "three"                    # ValueError (int("three"))
        self.assertEqual(len(knownbits.node_known(nd)), 1)
        nd.attrs["exp"] = None                       # TypeError
        self.assertEqual(len(knownbits.node_known(nd)), 1)
        nd.attrs["exp"] = -1
        a.kval = 2                                   # pow(2, -1, 256): ValueError (not invertible)
        self.assertEqual(len(knownbits.node_known(nd)), 1)

    def test_malformed_arity_falls_back_without_raising(self):
        b = Builder("m")
        a = b.port("a", "in", 4)
        c = b.port("c", "in", 4)
        y = b.port("y", "out", 4)
        b.connect(y, b.not_(a.sig))
        m = b.finalize()
        nd = next(n for n in m.nodes if n.op == "NOT")
        nd.inputs = nd.inputs + (("B", c.sig),)    # NOT with two operands: unpack ValueError in eval_const
        for net in (a, c):
            net.kmask, net.kval = 0xF, 5
        self.assertEqual(knownbits.node_known(nd), [(0xF, 0xA)])   # transfer rule of NOT on A

    def test_unexpected_exception_is_not_swallowed(self):
        nd, _ = self._pow_node()
        with mock.patch.object(knownbits, "eval_const", side_effect=AttributeError("defect")):
            with self.assertRaises(AttributeError):
                knownbits.node_known(nd)

    def test_fold_errors_are_the_documented_set(self):
        self.assertEqual(knownbits._FOLD_ERRORS, (ValueError, LookupError, TypeError, ArithmeticError))


if __name__ == "__main__":
    unittest.main()
