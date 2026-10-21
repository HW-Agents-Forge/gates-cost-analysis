"""Randomised differential tests: the analysis passes (known bits, folding, dead logic,
fusion, sharing) and the lowering preserve the function of random designs."""
import random
import unittest

from gatecost.ir import Builder, Const, LowerBlock, SigSpec, analyze_ir, check_module

from tests.ir_dsl import E, Mod
from tests.ir_sim import Sim


def random_comb(seed, const_port=None, regs=False):
    rnd = random.Random(seed)
    m = Mod(f"rnd{seed}", const_ports={"p0": const_port} if const_port is not None else None)
    ins = []
    for i in range(4):
        w = rnd.choice([1, 3, 4, 8, 12])
        ins.append(m.inp(f"p{i}", w, signed=rnd.random() < 0.3))
    if const_port is not None:
        ins[0] = m.ports["p0"]
    clk = m.inp("clk", 1) if regs else None
    rst = m.inp("rst", 1) if regs else None
    pool: list[E] = list(ins)

    def pick():
        return rnd.choice(pool)

    def sized(e, w):
        return e.resize(w) if hasattr(e, "resize") else E(m, e.s.resize(w, e.signed), e.signed)

    for _ in range(rnd.randint(8, 22)):
        kind = rnd.choice(["add", "sub", "mul", "neg", "and", "or", "xor", "not", "mux", "shl",
                           "shr", "eq", "lt", "sext", "cat", "slice", "addc", "mulc", "red",
                           "div", "mod", "cmpc"])
        a, b = pick(), pick()
        w = rnd.choice([1, 2, 4, 8, 9, 12, 16])
        try:
            if kind in ("add", "sub", "mul"):
                r = getattr(a, kind)(b, w)
            elif kind == "neg":
                r = E(m, m.b.neg(a.s, w, sa=a.signed), a.signed)
            elif kind in ("and", "or", "xor"):
                r = {"and": a & b, "or": a | b, "xor": a ^ b}[kind]
            elif kind == "not":
                r = ~a
            elif kind == "mux":
                s = rnd.choice(pool)[0:1]
                r = s.mux(a, b)
            elif kind in ("shl", "shr"):
                amt = b[0:min(b.w, 3)]
                r = a << amt if kind == "shl" else a >> amt
            elif kind == "eq":
                r = a == b
            elif kind == "lt":
                r = a < b
            elif kind == "sext":
                r = a.sext(max(a.w, w))
            elif kind == "cat":
                r = E(m, SigSpec.concat([a.s, b.s]))
                if r.w > 24:
                    continue
            elif kind == "slice":
                lo = rnd.randint(0, a.w - 1)
                r = a[lo:rnd.randint(lo + 1, a.w)]
            elif kind == "addc":
                r = a.add(m.c(rnd.getrandbits(4), 4), max(a.w, 4))
            elif kind == "mulc":
                r = a.mul(m.c(rnd.choice([0, 1, 2, 3, 7, 10, 16]), 5), max(a.w, 5) + 2)
            elif kind == "red":
                r = [a.red_or, a.red_and, a.red_xor][rnd.randrange(3)]()
            elif kind in ("div", "mod"):
                r = E(m, m.b.op("DIV" if kind == "div" else "MOD", a.s, b.s, width=w,
                                sa=a.signed, sb=b.signed))
            else:
                r = a < m.c(rnd.getrandbits(3), 3)
        except ValueError:
            continue
        if r.w > 24:
            r = r[0:24]
        pool.append(r)
        if regs and rnd.random() < 0.25:
            q = m.b.net(r.w, f"q{len(pool)}")
            en = pick()[0:1]
            rr = m.b.dff(r.s, clk.s, en=en.s if rnd.random() < 0.5 else None,
                         srst=rst.s if rnd.random() < 0.5 else None, srst_value=rnd.getrandbits(r.w),
                         q=q)
            pool.append(E(m, q.sig))
    for i in range(3):
        e = pool[-1 - i] if i < len(pool) else pool[0]
        m.out(f"o{i}", e)
    return m


def run_equiv(test, seed, **kw):
    raw = random_comb(seed, **kw).b.finalize()
    ana = random_comb(seed, **kw).b.finalize()
    analyze_ir(ana)
    test.assertEqual(check_module(ana), [], seed)
    sim_a, sim_b = Sim(raw), Sim(ana)
    rnd = random.Random(seed * 7 + 1)
    names = [(p.name, p.width) for p in raw.ports if p.port == "in"]
    const_port = kw.get("const_port")
    for step in range(12):
        vec = {n: rnd.getrandbits(w) for n, w in names}
        if const_port is not None:
            vec["p0"] = const_port.value & ((1 << dict(names)["p0"]) - 1)
        if "rst" in vec:
            vec["rst"] = int(rnd.random() < 0.2)
        if kw.get("regs"):
            oa = sim_a.step(vec)
            ob = sim_b.step(vec)
        else:
            oa = sim_a.outputs(sim_a.comb(vec))
            ob = sim_b.outputs(sim_b.comb(vec))
        test.assertEqual(oa, ob, (seed, step, vec))


class EquivalenceTests(unittest.TestCase):
    def test_random_combinational_designs(self):
        for seed in range(150):
            run_equiv(self, seed)

    def test_random_designs_with_constant_ports(self):
        for seed in range(100):
            m = random_comb(seed)
            w = next(p.width for p in m.b.module.ports if p.name == "p0")
            run_equiv(self, seed, const_port=Const(random.Random(seed).getrandbits(w), w))

    def test_random_sequential_designs(self):
        for seed in range(100):
            run_equiv(self, seed, regs=True)


class ExportRobustness(unittest.TestCase):
    def test_export_and_dump_of_random_designs(self):
        import json
        from gatecost.ir import dump, export_module
        for seed in range(60):
            ir = random_comb(seed, regs=seed % 2 == 0).b.finalize()
            analyze_ir(ir)
            json.dumps(export_module(ir), sort_keys=True)
            text = dump(ir)
            self.assertTrue(text.startswith("module "))
            self.assertEqual(text, dump(ir))


class LoweringEquivalence(unittest.TestCase):
    def test_random_if_case_trees_match_a_python_model(self):
        for seed in range(60):
            rnd = random.Random(seed)
            b = Builder("t")
            sel = b.port("sel", "in", 3)
            c = [b.port(f"c{i}", "in", 1) for i in range(3)]
            d = [b.port(f"d{i}", "in", 6) for i in range(4)]
            y = b.port("y", "out", 6)
            z = b.port("z", "out", 6)
            blk = LowerBlock(b, "comb")
            blk.assign(y, d[0].sig)
            # a random decision tree, mirrored by a python function
            def make(depth):
                kind = rnd.choice(["leaf", "if", "case"]) if depth < 3 else "leaf"
                if kind == "leaf":
                    which, tgt = rnd.randrange(4), rnd.choice(["y", "z", "both"])
                    def fn():
                        if tgt in ("y", "both"):
                            blk.assign(y, d[which].sig)
                        if tgt in ("z", "both"):
                            blk.assign(z, d[(which + 1) % 4].sig)
                    def ref(env, v):
                        if tgt in ("y", "both"):
                            env["y"] = v["d"][which]
                        if tgt in ("z", "both"):
                            env["z"] = v["d"][(which + 1) % 4]
                    return fn, ref
                if kind == "if":
                    ci = rnd.randrange(3)
                    t = make(depth + 1)
                    e = make(depth + 1) if rnd.random() < 0.6 else None
                    def fn():
                        blk.if_(c[ci].sig, t[0], e[0] if e else None)
                    def ref(env, v):
                        if v["c"][ci]:
                            t[1](env, v)
                        elif e:
                            e[1](env, v)
                    return fn, ref
                labels = rnd.sample(range(8), rnd.randint(1, 5))
                arms = [(lab, make(depth + 1)) for lab in labels]
                dflt = make(depth + 1) if rnd.random() < 0.5 else None
                def fn():
                    blk.case_(sel.sig, [([SigSpec.const(lab, 3)], a[0]) for lab, a in arms],
                              dflt[0] if dflt else None)
                def ref(env, v):
                    for lab, a in arms:
                        if v["sel"] == lab:
                            a[1](env, v)
                            return
                    if dflt:
                        dflt[1](env, v)
                return fn, ref
            fn, ref = make(0)
            fn()
            blk.finish()
            m = b.finalize()
            analyze_ir(m)
            self.assertEqual(check_module(m), [], seed)
            # outputs not assigned on every path become latches: compare only when no latch
            latch = any(n.op == "LATCH" for n in m.nodes)
            sim = Sim(m)
            for _ in range(40):
                v = {"sel": rnd.getrandbits(3), "c": [rnd.getrandbits(1) for _ in range(3)],
                     "d": [rnd.getrandbits(6) for _ in range(4)]}
                inputs = {"sel": v["sel"], **{f"c{i}": v["c"][i] for i in range(3)},
                          **{f"d{i}": v["d"][i] for i in range(4)}}
                env = {"y": v["d"][0], "z": None}
                ref(env, v)
                out = sim.outputs(sim.comb(inputs))
                self.assertEqual(out["y"], env["y"], (seed, v))
                if env["z"] is not None and not latch:
                    self.assertEqual(out["z"], env["z"], (seed, v))


class FusionEquivalence(unittest.TestCase):
    def test_random_arithmetic_trees(self):
        fused = 0
        for seed in range(150):
            rnd = random.Random(1000 + seed)

            def build():
                r = random.Random(1000 + seed)
                m = Mod("t")
                ins = [m.inp(f"i{k}", r.choice([3, 5, 8, 8, 12]), signed=r.random() < 0.4) for k in range(5)]
                W = r.choice([8, 12, 16, 20])

                def tree(depth):
                    if depth == 0 or r.random() < 0.25:
                        e = r.choice(ins)
                        return E(m, e.s.resize(W, e.signed), e.signed)
                    op = r.choice(["add", "add", "sub", "neg", "mul"])
                    if op == "neg":
                        return -tree(depth - 1)
                    a, b = tree(depth - 1), tree(depth - 1)
                    if op == "mul":
                        return a.mul(b, W)
                    return getattr(a, op)(b, W)

                m.out("y", tree(r.randint(2, 4)))
                m.out("z", tree(2))
                return m

            raw = build().b.finalize()
            ana = build().b.finalize()
            analyze_ir(ana)
            fused += sum(1 for n in ana.nodes if n.op == "SUM")
            self.assertEqual(check_module(ana), [], seed)
            sa, sb = Sim(raw), Sim(ana)
            names = [(p.name, p.width) for p in raw.ports if p.port == "in"]
            for _ in range(15):
                vec = {n: rnd.getrandbits(w) for n, w in names}
                self.assertEqual(sa.outputs(sa.comb(vec)), sb.outputs(sb.comb(vec)), (seed, vec))
        self.assertGreater(fused, 30)


class ClockedLoweringEquivalence(unittest.TestCase):
    def test_random_clocked_blocks_match_a_cycle_model(self):
        stats = {"en": 0, "srst": 0, "arst": 0}
        for seed in range(80):
            rnd = random.Random(seed)
            b = Builder("t")
            clk = b.port("clk", "in", 1)
            arst = b.port("arst", "in", 1)
            c = [b.port(f"c{i}", "in", 1) for i in range(3)]
            d = [b.port(f"d{i}", "in", 6) for i in range(3)]
            y = b.port("y", "out", 6)
            q = b.net(6, "q")
            use_async = rnd.random() < 0.5
            blk = LowerBlock(b, "clocked", clock=clk, async_resets=[(arst, 1)] if use_async else [])
            consts = [rnd.getrandbits(6) for _ in range(3)]

            def make(depth):
                kind = rnd.choice(["leaf", "if", "if"]) if depth < 3 else "leaf"
                if kind == "leaf":
                    what = rnd.choice(["d", "const", "inc", "hold"])
                    i = rnd.randrange(3)
                    def fn():
                        if what == "d":
                            blk.assign(q, d[i].sig, True)
                        elif what == "const":
                            blk.assign(q, SigSpec.const(consts[i], 6), True)
                        elif what == "inc":
                            blk.assign(q, b.add(q.sig, SigSpec.const(1, 6), 6), True)
                    def ref(st, v):
                        if what == "d":
                            return v["d"][i]
                        if what == "const":
                            return consts[i]
                        if what == "inc":
                            return (st + 1) & 63
                        return st
                    return fn, ref
                ci = rnd.randrange(3)
                t = make(depth + 1)
                e = make(depth + 1) if rnd.random() < 0.7 else None
                def fn():
                    blk.if_(c[ci].sig, t[0], e[0] if e else None)
                def ref(st, v):
                    if v["c"][ci]:
                        return t[1](st, v)
                    return e[1](st, v) if e else st
                return fn, ref

            body = make(0)
            arst_val = rnd.getrandbits(6)
            if use_async:
                inner = body
                def top():
                    blk.if_(arst.sig, lambda: blk.assign(q, SigSpec.const(arst_val, 6), True), inner[0])
                top()
            else:
                body[0]()
            blk.finish()
            b.connect(y, q)
            m = b.finalize()
            analyze_ir(m)
            self.assertEqual(check_module(m), [], seed)
            for n in m.nodes:
                if n.op == "DFF":
                    stats["en"] += "EN" in dict(n.inputs)
                    stats["srst"] += "SRST" in dict(n.inputs)
                    stats["arst"] += "ARST" in dict(n.inputs)
            sim = Sim(m)
            # power-up value: registers with a reset start in their reset state, others at 0
            st = sim.outputs(sim.comb({}))["y"]
            for _ in range(40):
                v = {"c": [rnd.getrandbits(1) for _ in range(3)], "d": [rnd.getrandbits(6) for _ in range(3)]}
                a = int(rnd.random() < 0.15) if use_async else 0
                inputs = {"arst": a, **{f"c{i}": v["c"][i] for i in range(3)}, **{f"d{i}": v["d"][i] for i in range(3)}}
                if use_async and a:
                    st = arst_val
                    sim.apply_async(inputs)
                out = sim.step(inputs)
                self.assertEqual(out["y"], st, (seed,))
                if use_async and a:
                    st = arst_val
                else:
                    st = body[1](st, v)
        self.assertGreater(stats["en"], 5)
        self.assertGreater(stats["srst"], 5)
        self.assertGreater(stats["arst"], 5)


if __name__ == "__main__":
    unittest.main()
