"""Unit tests of the pipeline helpers (inputs, keys, cone detection, rebasing, config diff, cached analysis)."""
import os
import tempfile
import types
import unittest
from pathlib import Path

from gatecost import pipeline as P
from gatecost.cache import Cache
from gatecost.cost.profile import load_profile
from gatecost.cost.tree import DesignCostTree, InstanceEdge, ModuleCost
from gatecost.errors import GatecostError
from gatecost.result import Diagnostic

ADD = "module top(input [7:0] a, b, output [7:0] y); assign y = a + b; endmodule\n"
SUB = "module top(input [7:0] a, b, output [7:0] y); assign y = a - b; endmodule\n"


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name).resolve()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


class Names(Tmp):
    def test_rel_name(self):
        self.assertEqual(P.rel_name(self.root / "a" / "b.v", self.root), "a/b.v")
        self.assertEqual(P.rel_name(Path("/elsewhere/x.v"), self.root), "/elsewhere/x.v")
        self.assertEqual(P.rel_name(self.root / "a" / ".." / "c.v", self.root), "c.v")

    def test_clean_paths(self):
        r = str(self.root)
        self.assertEqual(P._clean_paths(f"bad {r}/a.v:3 and {r}/b/c.v", self.root), "bad a.v:3 and b/c.v")
        self.assertEqual(P._clean_paths("nothing", self.root), "nothing")

    def test_as_diag_variants(self):
        d = Diagnostic("X", "in %s/f.v" % self.root)
        self.assertEqual(P._as_diag(d, self.root).message, "in f.v")
        self.assertIs(P._as_diag(d), d)
        r = P._as_diag({"code": "C", "message": "m", "affects_cost": 1, "line": 4})
        self.assertEqual((r.code, r.severity, r.affects_cost, r.side, r.line), ("C", "warning", True, "after", 4))

        class H:
            def as_result_diagnostic(self, side):
                return Diagnostic("H", "h", side=side)

        self.assertEqual(P._as_diag(H()).side, "after")

    def test_block_name(self):
        self.assertEqual([P._block_name(k) for k in ("top", "top(W=8)", "m#1", "m[x]", "m{a}")], ["top"] * 2 + ["m"] * 3)

    def test_compose_assumptions_dedup_and_order(self):
        t = DesignCostTree("a", {"a": ModuleCost("a", "a", (), "h", (), (), (), "a.v", 1)}, {"a": 1})
        out = P.compose_assumptions(("g", "g"), {"a": ("m", "g"), "zz": ("ignored",)}, t, ("note",), self.root)
        self.assertEqual(out, ("g", "m", "note"))

    def test_code_fingerprint_stable(self):
        a = P.code_fingerprint()
        self.assertEqual(a, P.code_fingerprint())
        self.assertEqual(len(a), 16)


class ReadInputs(Tmp):
    def test_missing_file_is_empty_not_error(self):
        inp = P.read_inputs([self.root / "nope.v"], [], {}, self.root)
        self.assertEqual(inp.file_items, [("nope.v", b"")])

    def test_invalid_utf8_replaced(self):
        p = self.root / "bad.v"
        p.write_bytes(b"module m; // \xff\xfe\nendmodule\n")
        inp = P.read_inputs([p], [], {}, self.root)
        self.assertIn("�", inp.texts["bad.v"])
        self.assertEqual(inp.file_items[0][1][-10:], b"endmodule\n")

    def test_incdir_files_collected_sorted_without_duplicates(self):
        top = self.write("src/top.v", ADD)
        self.write("inc/b.vh", "// b")
        self.write("inc/a.vh", "// a")
        self.write("inc/.git/ignored.vh", "x")
        self.write("inc/__pycache__/ignored.vh", "x")
        self.write("inc/data.txt", "t")
        inp = P.read_inputs([top], [self.root / "inc", self.root / "src"], {"A": "1"}, self.root)
        names = [n for n, _ in inp.include_items]
        self.assertEqual(names, ["inc/a.vh", "inc/b.vh", "inc/data.txt"])      # top.v already a source
        self.assertNotIn("inc/data.txt", inp.texts)                              # not an RTL extension

    def test_implicit_include_found_next_to_source(self):
        top = self.write("rtl/top.v", '`include "defs.vh"\n' + ADD)
        self.write("rtl/defs.vh", "`define W 8\n")
        inp = P.read_inputs([top], [], {}, self.root)
        self.assertEqual([n for n, _ in inp.include_items], ["rtl/defs.vh"])

    def test_implicit_include_missing_or_absolute_ignored(self):
        top = self.write("top.v", '`include "nothere.vh"\n`include "/etc/passwd"\n' + ADD)
        self.assertEqual(P.read_inputs([top], [], {}, self.root).include_items, [])

    def test_recursive_include_terminates(self):
        top = self.write("top.v", '`include "a.vh"\n' + ADD)
        self.write("a.vh", '`include "b.vh"\n')
        self.write("b.vh", '`include "a.vh"\n')
        inp = P.read_inputs([top], [], {}, self.root)
        self.assertEqual(sorted(n for n, _ in inp.include_items), ["a.vh", "b.vh"])


class Keys(Tmp):
    def setUp(self):
        super().setUp()
        self.profile = load_profile()
        self.p = self.write("top.v", ADD)

    def key(self, text=ADD, defines=None, top=None, limits=None, prefer=None, kind="tree", params=None):
        self.p.write_text(text)
        inp = P.read_inputs([self.p], [], defines or {}, self.root, params)
        return P.side_key(inp, self.profile, top, limits, prefer, kind)

    def test_stable(self):
        self.assertEqual(self.key(), self.key())

    def test_sensitive_to_each_input(self):
        base = self.key()
        for k in (self.key(SUB), self.key(defines={"X": "1"}), self.key(top="top"), self.key(limits={"max": 1}),
                  self.key(prefer={"top.v"}), self.key(kind="tree1")):
            self.assertNotEqual(base, k)

    def test_prefer_order_irrelevant_but_empty_differs_from_none(self):
        self.assertEqual(self.key(prefer=["a", "b"]), self.key(prefer=["b", "a"]))
        self.assertNotEqual(self.key(prefer=()), self.key(prefer=None))

    def test_profile_override_changes_key(self):
        a = self.key()
        self.profile = load_profile()
        self.assertEqual(a, self.key())
        self.assertTrue(P.profile_digest(self.profile).startswith(self.profile.name + ":"))


class Cone(unittest.TestCase):
    def test_modules_collected_from_both_versions(self):
        n = P.cone_names(["a.v"], {"a.v": "module old(); endmodule"}, {"a.v": "module new1(); endmodule"})
        self.assertEqual(n, {"old", "new1"})

    def test_none_for_unsafe_text(self):
        for t in ("package p; endpackage", "`define X 1\nmodule m; endmodule", "module m; import p::*; endmodule",
                  '`include "x.vh"\nmodule m; endmodule', "interface i; endinterface"):
            self.assertIsNone(P.cone_names(["a.v"], {"a.v": t}, {"a.v": "module m; endmodule"}), t)

    def test_unsafe_in_comment_ignored(self):
        t = "// package here\n/* `define X */\nmodule m; endmodule"
        self.assertEqual(P.cone_names(["a.v"], {"a.v": t}, {"a.v": t}), {"m"})

    def test_none_for_headers_and_unknown_ext(self):
        for f in ("x.vh", "x.svh", "x.txt", "x"):
            self.assertIsNone(P.cone_names([f], {f: "module m; endmodule"}, {f: "module m; endmodule"}), f)

    def test_none_when_no_module(self):
        self.assertIsNone(P.cone_names(["a.v"], {"a.v": ""}, {"a.v": "// nothing"}))
        self.assertIsNone(P.cone_names([], {}, {}))

    def test_vhdl(self):
        t = "entity E is end; architecture rtl of E is begin end;"
        self.assertEqual(P.cone_names(["a.vhd"], {"a.vhd": t}, {"a.vhd": t}), {"E"})
        self.assertIsNone(P.cone_names(["a.vhd"], {"a.vhd": "use work.p.all; entity E is end;"}, {"a.vhd": t}))

    def test_added_file_missing_from_before(self):
        self.assertEqual(P.cone_names(["n.v"], {}, {"n.v": "module n; endmodule"}), {"n"})


class Recount(unittest.TestCase):
    @staticmethod
    def mc(key, *children):
        return ModuleCost(key, key, (), "h", (), tuple(InstanceEdge(f"u{i}", c, n) for i, (c, n) in enumerate(children)), (), "a.v", 1)

    def test_multiplicities_multiply(self):
        mods = {"t": self.mc("t", ("m", 2)), "m": self.mc("m", ("l", 3)), "l": self.mc("l")}
        self.assertEqual(P._recount(mods, "t"), {"t": 1, "m": 2, "l": 6})

    def test_diamond_sums_paths(self):
        mods = {"t": self.mc("t", ("a", 1), ("b", 1)), "a": self.mc("a", ("l", 2)), "b": self.mc("b", ("l", 5)), "l": self.mc("l")}
        self.assertEqual(P._recount(mods, "t")["l"], 7)

    def test_unknown_child_ignored(self):
        self.assertEqual(P._recount({"t": self.mc("t", ("ghost", 4))}, "t"), {"t": 1})

    def test_cycle_terminates(self):
        mods = {"t": self.mc("t", ("a", 1)), "a": self.mc("a", ("t", 1))}
        P._recount(mods, "t")          # must not recurse forever


class Rebase(Tmp):
    def test_within(self):
        self.assertTrue(P._within(self.root / "a", self.root))
        self.assertTrue(P._within(self.root / "a" / ".." / "b", self.root))
        self.assertFalse(P._within(self.root / ".." / "x", self.root))

    def test_absolute_inside_rebased(self):
        other = self.root / "other"
        self.assertEqual(P._rebase([str(self.root / "inc" / "x")], self.root, other), [str(other / "inc" / "x")])

    def test_absolute_outside_kept(self):
        self.assertEqual(P._rebase(["/abs/elsewhere"], self.root, self.root / "o"), ["/abs/elsewhere"])

    def test_none_and_empty(self):
        self.assertEqual(P._rebase(None, self.root, self.root), [])
        self.assertEqual(P._rebase([], self.root, self.root), [])

    def test_relative_resolved_against_cwd(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.root)
        side = self.root / "side"
        self.assertEqual(P._rebase(["inc"], self.root, side), [str(side / "inc")])

    def test_per_side_prefers_side_copy(self):
        elsewhere = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(elsewhere, ignore_errors=True))
        side = elsewhere / "side"
        (side / "inc").mkdir(parents=True)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(elsewhere)          # cwd outside the after tree
        self.assertEqual(P._rebase(["inc"], self.root, side, per_side=True), [str(side / "inc")])
        self.assertEqual(P._rebase(["inc"], self.root, side, per_side=False), ["inc"])


class ConfigDiff(unittest.TestCase):
    @staticmethod
    def s(**kw):
        d = dict(top=None, defines={}, params={}, sources=[], incdirs=[], filelists=[], exclude=[])
        d.update(kw)
        return types.SimpleNamespace(**d)

    def test_identical(self):
        self.assertEqual(P.config_differences(self.s(), self.s()), [])

    def test_top_only_when_both_pinned(self):
        self.assertEqual(P.config_differences(self.s(top="a"), self.s(top="b")), ["top"])
        self.assertEqual(P.config_differences(self.s(top="a"), self.s()), [])

    def test_defines_sorted_and_one_sided(self):
        out = P.config_differences(self.s(defines={"B": "1", "A": "1"}), self.s(defines={"B": "2", "C": "1"}))
        self.assertEqual(out, ["defines.A", "defines.B", "defines.C"])

    def test_lists(self):
        out = P.config_differences(self.s(sources=["a"], exclude=["x"]), self.s(sources=["b"], incdirs=["i"]))
        self.assertEqual(out, ["sources", "incdirs", "exclude"])

    def test_replace_defines_copies(self):
        a = self.s(defines={"A": "1"})
        b = P.replace_defines(a, {"B": "2"})
        self.assertEqual((a.defines, b.defines), ({"A": "1"}, {"B": "2"}))


class AnalyzeSide(Tmp):
    def setUp(self):
        super().setUp()
        self.profile = load_profile()
        self.p = self.write("top.v", ADD)
        self.cache = Cache(self.root / "cache")

    def run_side(self, cache=None, top="top"):
        inp = P.read_inputs([self.p], [], {}, self.root)
        return P.analyze_side(inp, self.profile, top, None, cache or self.cache)

    def test_cold_then_warm_equal(self):
        a = self.run_side()
        self.assertEqual(self.cache.state, "cold")
        b = self.run_side()
        self.assertEqual(self.cache.state, "warm")
        self.assertEqual(a.tree.total(), b.tree.total())
        self.assertEqual(a.assumptions, b.assumptions)

    def test_uncached_equals_cached(self):
        self.assertEqual(self.run_side(Cache(None)).tree.total(), self.run_side().tree.total())

    def test_edit_invalidates(self):
        a = self.run_side().tree.total()
        self.p.write_text(SUB + "// x\n")
        self.run_side()
        self.assertEqual(self.cache.hits, 0)                    # a changed source never hits
        self.p.write_text(ADD)
        self.assertEqual(self.run_side().tree.total(), a)       # old entry still valid for old content

    def test_corrupted_cache_entries_recompute(self):
        a = self.run_side().tree.total()
        for f in (self.root / "cache").rglob("*"):
            if f.is_file():
                f.write_bytes(b"corrupt!" * 8)
        self.assertEqual(self.run_side().tree.total(), a)

    def test_unknown_top_raises_diagnostic_error(self):
        with self.assertRaises(GatecostError) as cm:
            self.run_side(top="nope")
        self.assertEqual(cm.exception.code if hasattr(cm.exception, "code") else None, "TOP_NOT_FOUND")

    def test_empty_sources_error(self):
        self.p.write_text("// nothing\n")
        with self.assertRaises(GatecostError):
            self.run_side(top=None)

    def test_no_stray_writes_outside_cache(self):
        self.run_side()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["cache", "top.v"])


if __name__ == "__main__":
    unittest.main()
