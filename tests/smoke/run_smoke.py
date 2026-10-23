#!/usr/bin/env python3
"""Run `gatecost cost` over every project directory of a corpus and report crashes and diagnostic coverage."""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import time
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[2] / "scripts" / "gatecost"


def run_one(project: Path, timeout: float) -> dict:
    cmd = [sys.executable, str(LAUNCHER), "cost", "--format", "json", "--no-cache", "--root", str(project)]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"project": project.name, "crash": "timeout", "seconds": timeout}
    info = {"project": project.name, "exit": p.returncode, "seconds": round(time.perf_counter() - t0, 2)}
    if "Traceback" in p.stderr:
        info["crash"] = "traceback"
    try:
        doc = json.loads(p.stdout)
    except json.JSONDecodeError:
        info["crash"] = info.get("crash", "unparseable output")
        return info
    if doc.get("status") == "error":
        code = (doc.get("error") or {}).get("code")
        info["error"] = code
        if code == "INTERNAL":
            info["crash"] = "INTERNAL"
    else:
        info["confidence"] = doc["result"]["confidence"]["level"]
        info["typ_ge"] = doc["result"]["total"]["ge"]["typ"]
    info["diagnostics"] = dict(collections.Counter(d["code"] for d in doc.get("diagnostics", [])))
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    projects = sorted(p for p in args.corpus.iterdir() if p.is_dir() and not p.name.startswith("."))
    results = [run_one(p, args.timeout) for p in projects]
    totals = collections.Counter()
    for r in results:
        totals.update(r.get("diagnostics", {}))
    crashed = [r for r in results if r.get("crash")]
    if args.json:
        print(json.dumps({"projects": results, "diagnostic_totals": dict(totals), "crashed": len(crashed)}, indent=2))
    else:
        for r in results:
            print(f"{r['project']:<32} exit={r.get('exit', '-')} {r.get('seconds', '-')}s "
                  f"{r.get('confidence', r.get('error', r.get('crash', '')))} {r.get('diagnostics', {})}")
        print(f"\n{len(results)} projects, {len(crashed)} crashed; diagnostics: {dict(totals)}")
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())
