"""Analysis cache: store, corruption, atomicity, pruning, codec, locations."""
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gatecost import REPO_ROOT
from gatecost.cache import (
    Cache, analysis_key, cost_tree_from_json, cost_tree_to_json, decode, encode, from_json,
    module_cost_from_json, module_cost_to_json, resolve_cache, to_json,
)
from gatecost.config import Settings
from gatecost.cost.tree import CostOp, DesignCostTree, InstanceEdge, ModuleCost
from gatecost.result import Confidence, Diagnostic, Ge3

K = "ab" * 20


def sample_tree() -> DesignCostTree:
    op = CostOp(1, "ADD", "addcmp", "good", Ge3(10, 20, 30), 2, "a.v", 3, named_key=("x", 1, ("y",)),
                declared_widths=(8, 8), tags=("shared",), breakdown={"FA": 7}, arch={"typ": "rca"}, facts={"p": [1, 2]},
                members=(2, 3))
    leaf = ModuleCost("leaf{}", "leaf", (("W", "8"),), "h1", (op,), (), (Diagnostic("X", "m", line=2),), "a.v", 1)
    top = ModuleCost("top", "top", (), "h2", (), (InstanceEdge("u", "leaf{}", 2),), (), "a.v", 9)
    return DesignCostTree("top", {"top": top, "leaf{}": leaf}, {"top": 1, "leaf{}": 2},
                          (Diagnostic("D", "d", severity="info"),), ("assumed",), {"profile": "p", "defines": {"A": "1"}},
                          frozenset({"leaf{}"}))


class CacheStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "c"
        self.c = Cache(self.dir)

    def test_hit_miss(self):
        self.assertIsNone(self.c.get_bytes(K))
        self.assertTrue(self.c.put_bytes(K, b"hello"))
        self.assertEqual(self.c.get_bytes(K), b"hello")
        self.assertEqual((self.c.hits, self.c.misses, self.c.state), (1, 1, "warm"))

    def test_disabled(self):
        c = Cache(None)
        self.assertFalse(c.put_bytes(K, b"x"))
        self.assertIsNone(c.get_bytes(K))
        self.assertEqual(c.state, "off")

    def test_bad_keys_rejected(self):
        for k in ("", "../../etc/passwd", "ABC", "zz"):
            self.assertFalse(self.c.put_bytes(k, b"x"))
            self.assertIsNone(self.c.get_bytes(k))

    def test_corruption_ignored(self):
        self.c.put_bytes(K, b"payload-data")
        f = next(self.dir.rglob("*.gce"))
        for bad in (b"", b"GCC1", f.read_bytes()[:-3], b"junk" * 10, f.read_bytes()[:-1] + b"X"):
            f.write_bytes(bad)
            self.assertIsNone(self.c.get_bytes(K))
            self.assertFalse(f.exists())  # corrupted entry dropped
            self.c.put_bytes(K, b"payload-data")
            f = next(self.dir.rglob("*.gce"))
        self.assertEqual(self.c.get_bytes(K), b"payload-data")

    def test_undecodable_value_is_miss(self):
        self.c.put_bytes(K, b'{"__t":"os.system","f":{}}')
        self.assertIsNone(self.c.get(K))
        self.c.put_bytes(K, b"not json")
        self.assertIsNone(self.c.get(K))

    def test_atomic_no_partial_entries(self):
        real = os.replace
        with mock.patch("gatecost.cache.os.replace", side_effect=OSError("boom")):
            self.assertFalse(self.c.put_bytes(K, b"x" * 100))
        self.assertIsNone(self.c.get_bytes(K))
        self.assertEqual([p for p in self.dir.rglob("*") if p.is_file()], [])  # temp file cleaned
        self.assertTrue(real is os.replace)

    def test_concurrent_writers(self):
        def w(i):
            for _ in range(20):
                self.c.put_bytes(K, bytes([i]) * 5000)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        got = self.c.get_bytes(K)
        self.assertIn(got, [bytes([i]) * 5000 for i in range(4)])

    def test_prune_lru(self):
        keys = [f"{i:02x}" * 20 for i in range(5)]
        for i, k in enumerate(keys):
            self.c.put_bytes(k, b"x" * 1000)
            p = next(self.dir.rglob(k + ".gce"))
            os.utime(p, (1000 + i, 1000 + i))
        self.c.get_bytes(keys[0])  # touch: now most recent
        freed = self.c.prune(max_bytes=2 * 1040)
        self.assertGreater(freed, 0)
        self.assertLessEqual(self.c.size(), 2 * 1040)
        self.assertIsNotNone(self.c.get_bytes(keys[0]))
        self.assertIsNone(self.c.get_bytes(keys[1]))

    def test_key_sensitivity(self):
        base = dict(files=[("a.v", b"x")], defines={"A": "1"}, includes=[("i.vh", b"y")], profile="p", tool_version="1")
        k = analysis_key(**base)
        self.assertEqual(k, analysis_key(**base))
        for change in ({"files": [("a.v", b"z")]}, {"defines": {"A": "2"}}, {"includes": [("i.vh", b"q")]},
                       {"profile": "q"}, {"tool_version": "2"}):
            self.assertNotEqual(k, analysis_key(**{**base, **change}))


class Codec(unittest.TestCase):
    def test_roundtrip_tree(self):
        t = sample_tree()
        t2 = cost_tree_from_json(cost_tree_to_json(t))
        self.assertEqual(t, t2)
        self.assertEqual(list(t2.modules), list(t.modules))
        self.assertIsInstance(t2.unsupported_in, frozenset)
        self.assertEqual(t2.total(), t.total())

    def test_roundtrip_module(self):
        m = sample_tree().modules["leaf{}"]
        self.assertEqual(module_cost_from_json(module_cost_to_json(m)), m)

    def test_result_types(self):
        v = {"c": Confidence("high", ("r",), 1.5), "d": {1: Ge3(1, 2, 3)}, "t": (1, (2, 3)), "s": {1, 2}}
        self.assertEqual(from_json(to_json(v)), v)

    def test_rejects_unregistered(self):
        import fractions

        with self.assertRaises(TypeError):
            encode(fractions.Fraction(1, 2))
        with self.assertRaises(KeyError):
            decode({"__t": "subprocess.Popen", "f": {}})

    def test_store_roundtrip_identical(self):
        with tempfile.TemporaryDirectory() as d:
            c = Cache(Path(d))
            t = sample_tree()
            c.put(K, t)
            self.assertEqual(c.get(K), t)
            self.assertEqual(Cache(None).get(K), None)

    def test_wrong_type(self):
        with self.assertRaises(ValueError):
            cost_tree_from_json(to_json(Ge3(1, 2, 3)))


class Locations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = Path(self.tmp.name) / "user"
        p = mock.patch.dict(os.environ, {"GATECOST_CACHE_DIR": str(self.user)})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("GATECOST_NO_CACHE", None)
        self.root = Path(self.tmp.name) / "proj"
        self.root.mkdir()

    def test_project_dir(self):
        c = resolve_cache(Settings(root=self.root))
        self.assertEqual(c.dir, self.root / ".gatecost" / "cache")
        self.assertEqual((self.root / ".gatecost/.gitignore").read_text(), "*\n")

    def test_no_cache(self):
        c = resolve_cache(Settings(root=self.root, no_cache=True))
        self.assertFalse(c.enabled)
        self.assertFalse((self.root / ".gatecost").exists())
        with mock.patch.dict(os.environ, {"GATECOST_NO_CACHE": "1"}):
            self.assertFalse(resolve_cache(Settings(root=self.root)).enabled)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs permissions")
    def test_read_only_project_uses_user_cache(self):
        os.chmod(self.root, stat.S_IREAD | stat.S_IEXEC)
        self.addCleanup(os.chmod, self.root, 0o755)
        c = resolve_cache(Settings(root=self.root))
        self.assertEqual(c.dir, self.user / "analysis")

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs permissions")
    def test_nothing_writable_disables(self):
        os.chmod(self.root, stat.S_IREAD | stat.S_IEXEC)
        self.addCleanup(os.chmod, self.root, 0o755)
        with mock.patch.dict(os.environ, {"GATECOST_CACHE_DIR": "/proc/none/x" if os.path.isdir("/proc") else "/dev/null/x"}):
            c = resolve_cache(Settings(root=self.root))
        self.assertFalse(c.enabled)
        self.assertTrue(c.note)

    def test_never_in_skill_dir(self):
        c = resolve_cache(Settings(root=REPO_ROOT))
        self.assertEqual(c.dir, self.user / "analysis")
        self.assertFalse((REPO_ROOT / ".gatecost").exists())
        c = resolve_cache(Settings(root=self.root), cache_dir=str(REPO_ROOT / "cachehere"))
        self.assertFalse((REPO_ROOT / "cachehere").exists())
        self.assertEqual(c.dir, self.root / ".gatecost" / "cache")


if __name__ == "__main__":
    unittest.main()
