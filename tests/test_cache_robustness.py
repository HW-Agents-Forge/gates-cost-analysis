"""Cache robustness: corruption variants, invalidation, packs, pruning, concurrency, key hygiene."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gatecost.cache import Cache, analysis_key, make_key, resolve_cache
from tests.test_cache import K, sample_tree

K2 = "cd" * 20


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "c"
        self.c = Cache(self.dir)

    def files(self, suffix):
        return sorted(self.dir.rglob("*" + suffix))

    def test_overwrite_replaces(self):
        self.c.put_bytes(K, b"one")
        self.c.put_bytes(K, b"two")
        self.assertEqual(self.c.get_bytes(K), b"two")
        self.assertEqual(len(self.files(".gce")), 1)

    def test_empty_payload_roundtrip(self):
        self.assertTrue(self.c.put_bytes(K, b""))
        self.assertEqual(self.c.get_bytes(K), b"")

    def test_large_payload(self):
        data = os.urandom(300_000)
        self.c.put_bytes(K, data)
        self.assertEqual(self.c.get_bytes(K), data)

    def test_bit_flip_every_region(self):
        self.c.put_bytes(K, b"payload-data-1234567890")
        f = self.files(".gce")[0]
        good = f.read_bytes()
        for i in (0, 3, 4, 19, 20, len(good) - 1):      # magic, digest, payload
            bad = bytearray(good)
            bad[i] ^= 0x01
            f.write_bytes(bytes(bad))
            self.assertIsNone(self.c.get_bytes(K), i)
            self.c.put_bytes(K, b"payload-data-1234567890")
            f = self.files(".gce")[0]

    def test_corrupt_does_not_count_as_hit(self):
        self.c.put_bytes(K, b"abc")
        self.files(".gce")[0].write_bytes(b"garbage garbage garbage garbage")
        self.assertIsNone(self.c.get_bytes(K))
        self.assertEqual((self.c.hits, self.c.misses), (0, 1))
        self.assertEqual(self.c.state, "cold")

    def test_count_false_leaves_stats(self):
        self.c.put_bytes(K, b"abc")
        self.c.get_bytes(K, count=False)
        self.c.get_bytes(K2, count=False)
        self.assertEqual((self.c.hits, self.c.misses), (0, 0))

    def test_entry_is_a_directory(self):
        self.c.put_bytes(K, b"abc")
        f = self.files(".gce")[0]
        f.unlink()
        f.mkdir()
        self.assertIsNone(self.c.get_bytes(K))       # OSError on read: a miss, not an exception

    def test_put_when_parent_is_file(self):
        self.dir.parent.mkdir(exist_ok=True)
        self.dir.write_text("not a dir")
        self.assertFalse(Cache(self.dir).put_bytes(K, b"x"))

    def test_put_when_mkstemp_fails(self):
        with mock.patch("gatecost.cache.tempfile.mkstemp", side_effect=OSError("full")):
            self.assertFalse(self.c.put_bytes(K, b"x"))
        self.assertIsNone(self.c.get_bytes(K))

    def test_missing_dir_recreated_after_removal(self):
        self.c.put_bytes(K, b"x")
        import shutil
        shutil.rmtree(self.dir)
        self.assertIsNone(self.c.get_bytes(K))
        self.assertTrue(self.c.put_bytes(K, b"y"))
        self.assertEqual(self.c.get_bytes(K), b"y")

    def test_non_hex_and_upper_keys(self):
        for k in ("G" * 40, "AB" * 20, "ab/cd", "a b", "../x"):
            self.assertFalse(self.c.put_bytes(k, b"x"), k)

    def test_short_key_works_or_rejected_consistently(self):
        ok = self.c.put_bytes("ab", b"x")
        self.assertEqual(self.c.get_bytes("ab"), b"x" if ok else None)

    def test_no_stray_tmp_files_after_concurrent_writes(self):
        def w(i):
            for j in range(15):
                self.c.put_bytes(K if j % 2 else K2, bytes([i]) * 2000)
                self.c.get_bytes(K)

        ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual([p for p in self.dir.rglob(".tmp-*")], [])
        for k in (K, K2):
            got = self.c.get_bytes(k)
            self.assertTrue(got is None or len(set(got)) == 1)   # never an interleaving

    def test_two_cache_objects_share_directory(self):
        other = Cache(self.dir)
        self.c.put_bytes(K, b"shared")
        self.assertEqual(other.get_bytes(K), b"shared")

    def test_prune_zero_removes_everything(self):
        for k in (K, K2):
            self.c.put_bytes(k, b"x" * 50)
        self.c.prune(max_bytes=0)
        self.assertEqual(self.c.size(), 0)

    def test_prune_noop_when_within_bound(self):
        self.c.put_bytes(K, b"x")
        self.assertEqual(self.c.prune(max_bytes=10_000_000), 0)
        self.assertIsNotNone(self.c.get_bytes(K))

    def test_prune_disabled_cache(self):
        c = Cache(None)
        self.assertEqual((c.size(), c.prune()), (0, 0))

    def test_prune_runs_automatically_after_64_puts(self):
        c = Cache(self.dir, max_bytes=2000)
        for i in range(70):
            c.put_bytes(f"{i:02x}" * 20, b"x" * 500)
        self.assertLess(c.size(), 70 * 520)

    def test_close_idempotent(self):
        self.c.close()
        self.c.close()


class TypedValues(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "c"
        self.c = Cache(self.dir)

    def test_tree_roundtrip_via_store(self):
        t = sample_tree()
        self.assertTrue(self.c.put(K, t))
        self.assertEqual(self.c.get(K), t)

    def test_missing_pack_is_miss_and_entry_dropped(self):
        self.assertTrue(self.c.put(K, sample_tree()))
        packs = list(self.dir.rglob("*.gcp"))
        self.assertTrue(packs)
        for p in packs:
            p.unlink()
        self.assertIsNone(self.c.get(K))
        self.assertEqual(list(self.dir.rglob("*.gce")), [])
        self.assertEqual(self.c.hits, 0)

    def test_corrupt_pack_does_not_raise(self):
        self.c.put(K, sample_tree())
        for p in self.dir.rglob("*.gcp"):
            p.write_bytes(b"\x00" * 10)
        try:
            self.c.get(K)
        except Exception as e:      # any outcome but an exception is acceptable
            self.fail(f"corrupt pack raised {e!r}")

    def test_truncated_pack_does_not_raise(self):
        self.c.put(K, sample_tree())
        for p in self.dir.rglob("*.gcp"):
            p.write_bytes(p.read_bytes()[:5])
        try:
            self.c.get(K)
        except Exception as e:
            self.fail(f"truncated pack raised {e!r}")

    def test_unserializable_value_returns_false(self):
        self.assertFalse(self.c.put(K, object()))
        self.assertIsNone(self.c.get(K))

    def test_json_garbage_variants_are_misses(self):
        for raw in (b"\xff\xfe", b"{", b'{"__t": 5}', b'{"__t": "nope.Cls", "f": {}}', b"1" * 10000):
            self.c.put_bytes(K, raw)
            self.assertIsNone(self.c.get(K), raw[:20])

    def test_hit_counters_after_failed_decode(self):
        self.c.put_bytes(K, b"not json")
        self.c.get(K)
        self.assertEqual((self.c.hits, self.c.misses), (0, 1))

    def test_shared_pack_between_trees(self):
        self.c.put(K, sample_tree())
        n = len(list(self.dir.rglob("*.gcp")))
        self.c.put(K2, sample_tree())
        self.assertEqual(len(list(self.dir.rglob("*.gcp"))), n)     # identical operations share the pack


class Keys(unittest.TestCase):
    def test_make_key_is_hex40_and_stable(self):
        k = make_key("a", b"b", 3, {"x": [1]})
        self.assertEqual(len(k), 40)
        int(k, 16)
        self.assertEqual(k, make_key("a", b"b", 3, {"x": [1]}))

    def test_make_key_part_boundaries(self):
        self.assertNotEqual(make_key("ab", "c"), make_key("a", "bc"))
        self.assertNotEqual(make_key("a"), make_key(b"a"))
        self.assertNotEqual(make_key("1"), make_key(1))
        self.assertNotEqual(make_key(), make_key(""))

    def test_make_key_dict_order_independent(self):
        self.assertEqual(make_key({"a": 1, "b": 2}), make_key({"b": 2, "a": 1}))

    def test_analysis_key_file_order_independent(self):
        a = analysis_key([("a.v", b"1"), ("b.v", b"2")], tool_version="1")
        b = analysis_key([("b.v", b"2"), ("a.v", b"1")], tool_version="1")
        self.assertEqual(a, b)

    def test_analysis_key_rename_and_extra_invalidate(self):
        base = analysis_key([("a.v", b"1")], tool_version="1")
        self.assertNotEqual(base, analysis_key([("c.v", b"1")], tool_version="1"))
        self.assertNotEqual(base, analysis_key([("a.v", b"1")], tool_version="1", extra={"top": "t"}))
        self.assertNotEqual(base, analysis_key([("a.v", b"1")], includes=[("a.v", b"1")], tool_version="1"))
        self.assertNotEqual(analysis_key([("a.v", b"1")], profile="p", tool_version="1"),
                            analysis_key([("a.v", b"1")], profile=b"p", tool_version="1"))

    def test_analysis_key_defaults_to_tool_version(self):
        with mock.patch("gatecost.cache.version", return_value="9.9"):
            a = analysis_key([("a.v", b"1")])
        with mock.patch("gatecost.cache.version", return_value="9.8"):
            b = analysis_key([("a.v", b"1")])
        self.assertNotEqual(a, b)


class Resolve(unittest.TestCase):
    class S:
        no_cache = False

        def __init__(self, root):
            self.root = root

    def test_env_disables(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"GATECOST_NO_CACHE": "1"}):
            c = resolve_cache(self.S(d))
            self.assertFalse(c.enabled)
            self.assertEqual(c.state, "off")
            self.assertTrue(c.note)

    def test_explicit_dir_wins(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as e:
            env = {k: v for k, v in os.environ.items() if k != "GATECOST_NO_CACHE"}
            with mock.patch.dict(os.environ, env, clear=True):
                c = resolve_cache(self.S(d), cache_dir=e)
            self.assertEqual(c.dir, Path(e))

    def test_cache_dir_unwritable_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "file"
            f.write_text("x")
            env = {k: v for k, v in os.environ.items() if k != "GATECOST_NO_CACHE"}
            with mock.patch.dict(os.environ, env, clear=True):
                c = resolve_cache(self.S(d), cache_dir=str(f / "sub"))
            self.assertNotEqual(c.dir, f / "sub")


if __name__ == "__main__":
    unittest.main()
