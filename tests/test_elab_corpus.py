"""Hardening corpus (tests/corpus): own realistic designs in both languages.

* every design elaborates with no PARSE_ERROR / INTERNAL diagnostic and at most the documented skip diagnostics;
* the share of designs hitting a skip is reported (``python -m tests.test_elab_corpus`` prints the report);
* the paired Verilog/VHDL descriptions (tests/corpus/pairs) yield the same operation multiset and equal cost.
"""
import os
import sys
import unittest
from collections import Counter

from gatecost.cost import build_cost_tree, load_profile
from gatecost.elab import elaborate_project

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus")
PAIRS = os.path.join(CORPUS, "pairs")
P = load_profile()

# Documented skip diagnostics that are accepted for a corpus file: {relative path: [message fragments]}.
DOCUMENTED_SKIPS = {
    "verilog/mac_array.sv": ["unpacked array port"],      # array ports are flattened into one vector (FRONTEND.md 3)
}
# Diagnostic codes that must never appear.
FORBIDDEN = {"PARSE_ERROR", "INTERNAL", "MISSING_MODULE", "UNRESOLVED_NAME", "UNRESOLVED_OVERLOAD", "MISSING_UNIT"}
SKIP_CODES = {"UNSUPPORTED_CONSTRUCT", "LIMIT_REACHED", "UNRESOLVED_PARAM"}
# Operations that are free wiring/inversions whose count may legitimately differ between the two languages.
NEGLIGIBLE = {"NOT", "BUF"}


def singles():
    out = []
    for sub in ("verilog", "vhdl"):
        d = os.path.join(CORPUS, sub)
        out.extend(sub + "/" + f for f in sorted(os.listdir(d)))
    return out


def pair_names():
    return sorted(os.path.splitext(f)[0] for f in os.listdir(PAIRS) if f.endswith(".v"))


def elab_file(rel, root, top=None):
    return elaborate_project([rel], root=root, top=top)


def all_designs():
    """``[(label, root, relative file, top)]`` for the whole corpus (pairs count twice)."""
    out = [(rel, CORPUS, rel, None) for rel in singles()]
    for n in pair_names():
        out.append(("pairs/%s.v" % n, PAIRS, n + ".v", n))
        out.append(("pairs/%s.vhd" % n, PAIRS, n + ".vhd", n))
    return out


def skip_diagnostics(res):
    return [d for d in res.diagnostics if d.code in SKIP_CODES and d.affects_cost]


def report():
    rows = []
    for label, root, rel, top in all_designs():
        res = elab_file(rel, root, top)
        m = res.design.modules[res.top_key]
        rows.append((label, len(m.nodes), len(skip_diagnostics(res))))
    skipped = [r for r in rows if r[2]]
    lines = ["corpus: %d designs, %d with skipped constructs (%.1f%%), %d skip diagnostics for %d IR nodes"
             % (len(rows), len(skipped), 100.0 * len(skipped) / len(rows), sum(r[2] for r in rows),
                sum(r[1] for r in rows))]
    lines.extend("  %-28s %5d nodes  %d skipped" % r for r in skipped)
    return "\n".join(lines)


class Corpus(unittest.TestCase):
    def test_corpus_is_large_enough(self):
        self.assertGreaterEqual(len(all_designs()), 25)
        self.assertGreaterEqual(len(pair_names()), 8)
        self.assertTrue(any(s.startswith("verilog/") for s in singles()))
        self.assertTrue(any(s.startswith("vhdl/") for s in singles()))

    def test_every_design_elaborates_without_parse_errors(self):
        for label, root, rel, top in all_designs():
            with self.subTest(design=label):
                res = elab_file(rel, root, top)
                bad = [(d.code, d.message) for d in res.diagnostics if d.code in FORBIDDEN]
                self.assertEqual(bad, [])
                self.assertIsNotNone(res.top_key)
                self.assertGreater(len(res.design.modules[res.top_key].nodes), 0)
                allowed = DOCUMENTED_SKIPS.get(label, [])
                for d in skip_diagnostics(res):
                    self.assertTrue(any(a in d.message for a in allowed), (label, d.code, d.message))

    def test_skipped_share_is_small(self):
        rows = [skip_diagnostics(elab_file(rel, root, top)) for _l, root, rel, top in all_designs()]
        share = sum(1 for r in rows if r) / len(rows)
        self.assertLessEqual(share, 0.10)

    def test_each_design_costs(self):
        for label, root, rel, top in all_designs():
            with self.subTest(design=label):
                res = elab_file(rel, root, top)
                tree = build_cost_tree(res.design, P)
                self.assertGreater(tree.total().typ, 0)


def significant_ops(m):
    return Counter(n.op for n in m.nodes if n.op not in NEGLIGIBLE)


class Pairs(unittest.TestCase):
    def test_pairs_have_equal_operations_and_cost(self):
        for n in pair_names():
            with self.subTest(pair=n):
                rv = elab_file(n + ".v", PAIRS, n)
                rh = elab_file(n + ".vhd", PAIRS, n)
                mv, mh = rv.design.modules[rv.top_key], rh.design.modules[rh.top_key]
                self.assertEqual(significant_ops(mv), significant_ops(mh))
                cv = build_cost_tree(rv.design, P).total().typ
                ch = build_cost_tree(rh.design, P).total().typ
                self.assertLessEqual(abs(cv - ch), 0.01 * max(cv, ch), (cv, ch))

    def test_most_pairs_have_identical_canonical_hashes(self):
        same = 0
        names = pair_names()
        for n in names:
            rv = elab_file(n + ".v", PAIRS, n)
            rh = elab_file(n + ".vhd", PAIRS, n)
            same += rv.design.modules[rv.top_key].canonical_hash == rh.design.modules[rh.top_key].canonical_hash
        self.assertGreaterEqual(same, len(names) // 3)

    def test_pairs_do_not_differ_in_diagnostics(self):
        for n in pair_names():
            with self.subTest(pair=n):
                kinds = []
                for ext in (".v", ".vhd"):
                    res = elab_file(n + ext, PAIRS, n)
                    kinds.append(sorted({d.code for d in skip_diagnostics(res)}))
                self.assertEqual(kinds[0], kinds[1])


if __name__ == "__main__":
    if "--report" in sys.argv:
        print(report())
    else:
        unittest.main()
