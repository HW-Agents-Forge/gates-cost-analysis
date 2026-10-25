"""Error paths of the cost stage: invariant breaches become diagnostics or informative errors, never bare crashes."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import gatecost.ir as ir_pkg
from gatecost.cache import Cache, cost_tree_from_json, module_cost_from_json, to_json
from gatecost.cost import build_cost_tree, cost_op, load_profile
from gatecost.cost import arith, build, provenance, seq
from gatecost.errors import GatecostError
from gatecost.estimate import estimate_expression
from gatecost.result import Ge3
from tests.ir_dsl import Mod
from tests.test_cache import K, sample_tree

P = load_profile()


def _design():
    """An ADD and a MUL in one module."""
    m = Mod("t")
    a, b = m.inp("a", 16), m.inp("b", 16)
    m.out("y", a + b)
    m.out("z", a.mul(b, 32))
    d = ir_pkg.Design("t")
    d.add(m.done())
    return d


def _tree_with(mutate):
    """Cost tree of ``_design()`` where ``mutate(node) -> node | None`` rewrites exported nodes (copies)."""
    real = ir_pkg.export_design

    def export(design):
        exp = real(design)
        mods = {}
        for k, mod in exp["modules"].items():
            nodes = []
            for nd in mod["nodes"]:
                new = mutate(dict(nd))
                nodes.append(nd if new is None else new)
            mods[k] = dict(mod, nodes=nodes)
        return dict(exp, modules=mods)

    with mock.patch.object(ir_pkg, "export_design", export):
        return build_cost_tree(_design(), P)


class BuilderNodeFailure(unittest.TestCase):
    """build.py: a node the cost model cannot handle is an INTERNAL diagnostic, not an aborted run."""

    def setUp(self):
        self.ref = build_cost_tree(_design(), P)
        self.ref_ops = {o.op: o for o in self.ref.modules["t"].ops}
        self.assertEqual(set(self.ref_ops), {"ADD", "MUL"})

    def _check(self, tree, failing_op, fragment):
        m = tree.modules["t"]
        self.assertEqual([o.op for o in m.ops], [op for op in ("ADD", "MUL") if op != failing_op])
        kept = next(op for op in ("ADD", "MUL") if op != failing_op)
        self.assertEqual(tree.total(), self.ref_ops[kept].cost)        # the rest of the design is unchanged
        diags = [d for d in m.diagnostics if d.code == "INTERNAL"]
        self.assertEqual(len(diags), 1)
        d = diags[0]
        self.assertEqual((d.severity, d.affects_cost), ("error", True))
        self.assertIn(fragment, d.message)
        self.assertIn("zero assumed", d.message)
        self.assertIn("t", tree.unsupported_in)

    def test_unknown_op_is_a_diagnostic(self):
        t = _tree_with(lambda nd: dict(nd, op="FROB") if nd["op"] == "ADD" else None)
        m = t.modules["t"]
        self.assertEqual([o.op for o in m.ops], ["MUL"])
        self.assertEqual(t.total(), self.ref_ops["MUL"].cost)
        (d,) = [d for d in m.diagnostics if d.code == "INTERNAL"]
        self.assertIn("cannot cost FROB node", d.message)
        self.assertIn("not an IR operation", d.message)

    def test_invalid_attribute_is_a_diagnostic(self):
        def bad_mul(nd):
            if nd["op"] != "MUL":
                return None
            return dict(nd, eff=dict(nd["eff"], wb=-3))
        self._check(_tree_with(bad_mul), "MUL", "attribute m must be an integer >= 1, got -3")

    def test_missing_attribute_is_a_diagnostic(self):
        def no_eff(nd):
            if nd["op"] != "ADD":
                return None
            nd.pop("eff")
            return nd
        self._check(_tree_with(no_eff), "ADD", "missing attribute 'eff'")

    def test_debug_env_reraises(self):
        with mock.patch.dict("os.environ", {"GATECOST_ELAB_DEBUG": "1"}):
            with self.assertRaises(ValueError):
                _tree_with(lambda nd: dict(nd, op="FROB") if nd["op"] == "ADD" else None)

    def test_cost_node_unknown_op_message(self):
        nd = {"op": "FROB", "eff": {}, "inputs": [], "outputs": [], "tags": {}, "attrs": {}}
        with self.assertRaisesRegex(ValueError, r"cannot cost node op 'FROB': not an IR operation"):
            build._cost_node(nd, P)


class InvariantMessages(unittest.TestCase):
    """Invariant exceptions carry a message that names the offending value."""

    def test_unknown_adder_architecture(self):
        with self.assertRaisesRegex(ValueError, r"unknown adder architecture 'carry_skip' \(expected ripple"):
            arith.add_acc("carry_skip", 8, P)

    def test_division_by_non_positive_constant(self):
        with self.assertRaisesRegex(ValueError, r"positive divisor, got -3"):
            arith.div_op(8, 2, P, k=-3)

    def test_ilog2_fix(self):
        with self.assertRaisesRegex(ValueError, r"non-positive number \(0\)"):
            arith.ilog2_fix(0)

    def test_unknown_enable_style(self):
        with self.assertRaisesRegex(ValueError, r"unknown register enable style 'latch'"):
            seq._bank("DFF", 4, "latch", P)

    def test_cost_op_messages(self):
        with self.assertRaisesRegex(ValueError, r"unknown operation 'FROB'"):
            cost_op("FROB", {}, P)
        with self.assertRaisesRegex(ValueError, r"missing attribute n"):
            cost_op("ADD", {}, P)

    def test_cache_json_type_checks_name_the_type(self):
        with self.assertRaisesRegex(ValueError, r"not a DesignCostTree: the JSON decodes to Ge3"):
            cost_tree_from_json(to_json(Ge3(1, 2, 3)))
        with self.assertRaisesRegex(ValueError, r"not a ModuleCost: the JSON decodes to Ge3"):
            module_cost_from_json(to_json(Ge3(1, 2, 3)))


class EstimateUserErrors(unittest.TestCase):
    """estimate: a cost_op ValueError on user input is BAD_ARGUMENT with the reason, never INTERNAL."""

    def test_division_by_zero_constant(self):
        for expr in ("div 8 const=0", "mod 8 const=-3 signed"):
            with self.subTest(expr=expr), self.assertRaises(GatecostError) as cm:
                estimate_expression(expr, P)
            self.assertEqual(cm.exception.code, "BAD_ARGUMENT")
            self.assertIn("divisor", cm.exception.message)


class LazyPackVanished(unittest.TestCase):
    """cache.py: a pack removed between lookup and lazy read is a reportable error with the remedy."""

    def test_vanished_pack_is_gatecost_error(self):
        with tempfile.TemporaryDirectory() as td:
            c = Cache(Path(td) / "c")
            self.assertTrue(c.put(K, sample_tree()))
            t = c.get(K)
            self.assertIsNotNone(t)
            packs = sorted((Path(td) / "c").rglob("*.gcp"))
            self.assertTrue(packs)
            for p in packs:
                p.unlink()
            with self.assertRaises(GatecostError) as cm:
                _ = t.modules["leaf{}"].ops
            self.assertEqual(cm.exception.code, "INTERNAL")
            self.assertIn("rerun with --no-cache", cm.exception.message)
            self.assertIsInstance(cm.exception, RuntimeError)      # cache API contract kept

    def test_cli_prints_the_remedy(self):
        """main() renders the message (it used to print only "unexpected failure (RuntimeError)")."""
        import io
        from contextlib import redirect_stdout

        from gatecost import cli
        from gatecost.cache import PackUnavailable

        def boom(argv):
            raise PackUnavailable("cache pack ab (module operations) is unreadable or vanished while in use; "
                                  "rerun with --no-cache")

        out = io.StringIO()
        with mock.patch.object(cli, "_run", boom), redirect_stdout(out):
            code = cli.main(["cost"])
        self.assertEqual(code, 3)
        self.assertIn("rerun with --no-cache", out.getvalue())
        self.assertNotIn("unexpected failure", out.getvalue())


class ProvenanceEvaluator(unittest.TestCase):
    """provenance.evaluate is best effort: it returns None, it never raises."""

    def test_deep_nesting_returns_none(self):
        expr = "(" * 2000 + "1" + ")" * 2000
        self.assertIsNone(provenance.evaluate(expr, {}))
        self.assertIsNone(provenance.evaluate("-" * 5000 + "1", {}))

    def test_shallow_expressions_still_evaluate(self):
        self.assertEqual(provenance.evaluate("((W-1))+1", {"W": 8}), 8)
        self.assertIsNone(provenance.evaluate("W+", {"W": 8}))
        self.assertIsNone(provenance.evaluate("$frob(3)", {}))

    def test_apply_messages(self):
        with self.assertRaisesRegex(ValueError, r"exponent 300 outside"):
            provenance._apply("**", 2, 300)
        with self.assertRaisesRegex(ValueError, r"shift amount 5000 above 4096"):
            provenance._apply("<<", 1, 5000)
        with self.assertRaisesRegex(ValueError, r"operand of '\+' has no value"):
            provenance._apply("+", None, 1)


if __name__ == "__main__":
    unittest.main()
