"""Symmetry, transitivity, determinism and additivity on seeded random trees (DIFF.md section 12)."""
import copy
import random
import unittest

from gatecost.diff import diff_trees
from tests.test_diff_support import check_additive, mod, op, tree

KINDS = ["ADD", "MUL", "MUX", "DFF", "AND", "SHL", "DIV"]


def gen_spec(rng, nmods=5):
    mods = []
    for i in range(nmods):
        ops = []
        for j in range(rng.randint(1, 6)):
            k = rng.choice(KINDS)
            ops.append({"kind": k, "typ": rng.randint(1, 80), "tgt": f"t{i}_{j}", "line": 3 * j + 1,
                        "w": rng.choice([4, 8, 16]), "q": "poor" if k == "DIV" else "good"})
        inst = [(f"u{c}", c, rng.randint(1, 3)) for c in range(i + 1, nmods) if rng.random() < 0.5]
        mods.append({"name": f"m{i}", "ops": ops, "inst": inst})
    return mods


def mutate(rng, spec):
    s = copy.deepcopy(spec)
    for _ in range(rng.randint(0, 4)):
        m = rng.choice(s)
        r = rng.random()
        if r < 0.3 and m["ops"]:
            o = rng.choice(m["ops"])
            o["typ"] = max(1, o["typ"] + rng.randint(-20, 30))
            if rng.random() < 0.5:
                o["w"] = rng.choice([4, 8, 16, 32])
        elif r < 0.5 and len(m["ops"]) > 1:
            m["ops"].pop(rng.randrange(len(m["ops"])))
        elif r < 0.7:
            k = rng.choice(KINDS)
            n = rng.randint(100, 999)
            m["ops"].append({"kind": k, "typ": rng.randint(1, 80), "tgt": f"n{n}", "line": n, "w": 8, "q": "poor" if k == "DIV" else "good"})
        elif r < 0.9 and m["inst"]:
            i = rng.randrange(len(m["inst"]))
            p, c, n = m["inst"][i]
            m["inst"][i] = (p, c, max(1, n + rng.choice([-1, 1, 2])))
        else:  # cosmetic: rename + move
            for o in m["ops"]:
                o["tgt"] += "_r"
                o["line"] += 7
    return s


def build(spec):
    mods = []
    for m in spec:
        ops = [op(o["kind"], o["typ"], tgt=o["tgt"], line=o["line"], widths=(o["w"],), quality=o["q"],
                  shape=f"{m['name']}|{o['kind']}|{o['tgt'].removesuffix('_r')}|{o['w']}") for o in m["ops"]]
        mods.append(mod(m["name"], ops, instances=[(p, f"m{c}", n) for p, c, n in m["inst"]]))
    return tree(mods, top="m0")


def mirror(g):
    return (-g.max, -g.typ, -g.min)


class Properties(unittest.TestCase):
    def test_random(self):
        for seed in range(150):
            rng = random.Random(seed)
            sa = gen_spec(rng)
            sb = mutate(rng, sa)
            sc_ = mutate(rng, sb)
            A, B, C = build(sa), build(sb), build(sc_)
            rab, rba, rbc, rac = diff_trees(A, B), diff_trees(B, A), diff_trees(B, C), diff_trees(A, C)
            for r, x, y in ((rab, A, B), (rba, B, A), (rbc, B, C), (rac, A, C)):
                check_additive(self, r, x, y)
            # symmetry: opposite deltas in every scenario, mirrored ranges, opposite categories
            ab, ba = rab.delta_scenarios, rba.delta_scenarios
            self.assertEqual((ab.min, ab.typ, ab.max), (-ba.min, -ba.typ, -ba.max), seed)
            self.assertEqual((rab.delta.min, rab.delta.typ, rab.delta.max),
                             mirror(rba.delta), seed)
            self.assertEqual({k: v.typ for k, v in rab.category_scenarios.items()},
                             {k: -v.typ for k, v in rba.category_scenarios.items()}, seed)
            # transitivity
            self.assertEqual(rac.delta_scenarios.typ, rab.delta_scenarios.typ + rbc.delta_scenarios.typ, seed)
            # determinism
            self.assertEqual(diff_trees(A, B), rab)
            # identical trees give NO_CHANGE
            self.assertEqual(diff_trees(A, build(sa)).verdict, "NO_CHANGE")

    def test_symmetry_reasons(self):
        A = tree([mod("top", [op("AND", 10, tgt="z")])])
        B = tree([mod("top", [op("AND", 10, tgt="z"), op("MUL", 50, tgt="m", line=5)])])
        self.assertEqual([c.reason for c in diff_trees(A, B).all_contributors], ["added"])
        self.assertEqual([c.reason for c in diff_trees(B, A).all_contributors], ["removed"])
        self.assertEqual(diff_trees(A, B).verdict, "INCREASE")
        self.assertEqual(diff_trees(B, A).verdict, "DECREASE")

    def test_cosmetic_mutations_only(self):
        for seed in range(30):
            rng = random.Random(1000 + seed)
            s = gen_spec(rng)
            t = copy.deepcopy(s)
            for m in t:
                for o in m["ops"]:
                    o["tgt"] += "_r"
                    o["line"] += 11
                m["ops"].reverse()
            # hash differs from the shape-only hash? shapes are name independent, so equal
            r = diff_trees(build(s), build(t))
            self.assertEqual(r.verdict, "NO_CHANGE", seed)


if __name__ == "__main__":
    unittest.main()
