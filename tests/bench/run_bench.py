"""Print timings of the pipeline stages on generated designs (IMPLEMENTATION.md stage 8).

    PYTHONPATH=src python3 -m tests.bench.run_bench [--lines 10000 100000] [--kinds sv vhdl] [--profile]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests.bench.gen_design import generate, write


def timed(fn):
    t0 = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t0


def lex_parse(kind: str, root: Path, names):
    paths = [root / n for n in names]
    if kind == "sv":
        from gatecost.frontend.verilog import parse_files
    else:
        from gatecost.frontend.vhdl import parse_files
    return parse_files(paths)


def pipeline(kind: str, lines: int, seed: int = 1, profile: bool = False) -> dict:
    """Return stage timings (seconds) and sizes."""
    from gatecost.cost import build_cost_tree, load_profile
    from gatecost.elab import elaborate_project

    files = generate(kind, lines, seed)
    nbytes = sum(len(t.encode()) for t in files.values())
    nlines = sum(t.count("\n") for t in files.values())
    out: dict = {"kind": kind, "lines": nlines, "bytes": nbytes}
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        names = write(files, root)
        _, out["parse_s"] = timed(lambda: lex_parse(kind, root, names))
        prof = None
        if profile:
            import cProfile
            import pstats
            prof = cProfile.Profile()
            prof.enable()
        res, out["elab_total_s"] = timed(lambda: elaborate_project(names, root=root))
        if prof:
            prof.disable()
            pstats.Stats(prof).sort_stats("tottime").print_stats(25)
        out["nodes"] = sum(len(m.nodes) for m in res.design.modules.values())
        p = load_profile("generic-asic", None, Path.cwd())
        tree, out["cost_tree_s"] = timed(lambda: build_cost_tree(res.design, p, config={"root": str(root)}))
    out["parse_MBps"] = nbytes / 1e6 / out["parse_s"]
    out["elab_s"] = out["elab_total_s"] - out["parse_s"]
    return out


def synthetic_ir(n_nodes: int) -> dict:
    """Build and analyze one Module IR of about ``n_nodes`` nodes (IR.md section 11)."""
    from gatecost.ir import Builder, analyze_ir  # noqa: F401
    from tests.bench.ir_gen import build_big_module

    t0 = time.perf_counter()
    m = build_big_module(n_nodes)
    t1 = time.perf_counter()
    analyze_ir(m)
    t2 = time.perf_counter()
    return {"nodes": len(m.nodes), "build_s": t1 - t0, "analyze_s": t2 - t1}


def startup() -> dict:
    """Cold (empty bytecode cache) and warm start-up of the launcher, best of several runs."""
    launcher = Path(__file__).resolve().parents[2] / "scripts" / "gatecost"
    res = {}
    for label, args in (("version", ["--version"]), ("estimate", ["estimate", "mul 16x16 + add 32"])):
        cold, warm = [], []
        for _ in range(3):
            with tempfile.TemporaryDirectory() as cache:
                env = dict(os.environ, GATECOST_CACHE_DIR=cache)
                for bucket in (cold, warm, warm):
                    t0 = time.perf_counter()
                    subprocess.run([sys.executable, str(launcher)] + args, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    bucket.append(time.perf_counter() - t0)
        res[label] = (min(cold), min(warm))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, nargs="*", default=[10000, 100000])
    ap.add_argument("--kinds", nargs="*", default=["sv", "vhdl"])
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--ir", type=int, default=100000)
    a = ap.parse_args()
    for n in a.lines:
        for k in a.kinds:
            r = pipeline(k, n, profile=a.profile)
            print(f"{k:5s} {r['lines']:7d} lines {r['bytes'] / 1e6:6.2f} MB  parse {r['parse_s']:6.2f}s ({r['parse_MBps']:.2f} MB/s)  "
                  f"elab+analyze {r['elab_s']:6.2f}s  cost-tree {r['cost_tree_s']:6.2f}s  IR nodes {r['nodes']}")
    if a.ir:
        r = synthetic_ir(a.ir)
        print(f"IR    {r['nodes']} nodes: build {r['build_s']:.2f}s  analyze {r['analyze_s']:.2f}s")
    s = startup()
    for k, (c, w) in s.items():
        print(f"start-up {k:9s} cold {c * 1000:5.0f} ms  warm {w * 1000:5.0f} ms")


if __name__ == "__main__":
    main()
