#!/usr/bin/env python3
"""Reference validation (maintainers only, SPEC.md section 14): gatecost estimates against real synthesis areas.

This script never runs a synthesis tool. You produce the reference areas yourself (for example the
"Chip area for module" line of Yosys ``stat -liberty <lib>`` or any other area report, in square micrometres)
and list them in a CSV file; the script runs ``gatecost cost`` on each design, converts the reference area to
gate equivalents (GE = area / area of one NAND2) and prints error statistics.

Usage::

    python3 scripts/validate_reference.py RTL_DIR REFERENCE.csv [--nand2-area UM2] [--profile NAME]
                                          [--format table|json] [--tolerance PCT]

CSV format (header line required, comma separated, ``#`` lines ignored)::

    design,top,area_um2
    alu16,alu,83.0
    fifo,fifo_top,2120.5
    cpu_core,,48210.0

* ``design``: a directory (or a single RTL file) under ``RTL_DIR``; it is analyzed on its own, so each design
  should be self-contained.
* ``top``: top module or entity; empty means automatic detection.
* ``area_um2``: reference area in square micrometres (> 0).
* Optional extra columns: ``ge`` (reference area already in GE; used instead of ``area_um2`` when present and
  non-empty) and ``nand2_area_um2`` (per-row NAND2 area, overriding ``--nand2-area``).

The NAND2 area defaults to the one declared by the cost profile in the ``gatecost`` JSON output
(``profile.nand2_area_um2``); pass ``--nand2-area`` with the NAND2_X1 area of the library used for the
reference run, so that both sides use the same unit.

Output: one line per design (estimate and its range, reference, signed error in percent, whether the reference
lies inside the estimate range) and aggregate statistics: mean/median absolute percentage error, bias (mean
signed error), geometric-mean ratio, worst case, and the share of references inside the estimated range. These
numbers drive the tuning of the profile defaults and the confidence thresholds (COST_MODEL.md section 5).
Exit code 0, or 1 when ``--tolerance`` is given and the median absolute error exceeds it.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent / "gatecost"


@dataclass
class Row:
    design: str
    top: str
    ref_ge: float | None          # None until converted
    area_um2: float | None
    nand2_area_um2: float | None


@dataclass
class Result:
    design: str
    est_min: float
    est_typ: float
    est_max: float
    ref_ge: float

    @property
    def error_pct(self) -> float:
        return 100.0 * (self.est_typ - self.ref_ge) / self.ref_ge

    @property
    def ratio(self) -> float:
        return self.est_typ / self.ref_ge if self.est_typ > 0 else float("nan")

    @property
    def inside(self) -> bool:
        return self.est_min <= self.ref_ge <= self.est_max


def read_reference(path: Path) -> list[Row]:
    with open(path, newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    need = {"design"}
    if reader.fieldnames is None or not need <= set(reader.fieldnames):
        raise ValueError("reference CSV needs a header with at least: design,top,area_um2")
    if not ({"area_um2", "ge"} & set(reader.fieldnames)):
        raise ValueError("reference CSV needs an area_um2 (or ge) column")
    rows = []
    for n, rec in enumerate(reader, start=2):
        rec = {k: (v or "").strip() for k, v in rec.items() if k}
        try:
            ge = float(rec["ge"]) if rec.get("ge") else None
            area = float(rec["area_um2"]) if rec.get("area_um2") else None
            n2 = float(rec["nand2_area_um2"]) if rec.get("nand2_area_um2") else None
        except ValueError as exc:
            raise ValueError(f"{path.name} row {n}: {exc}") from None
        if not rec.get("design") or (ge is None and area is None):
            raise ValueError(f"{path.name} row {n}: design and an area are required")
        if (ge is not None and ge <= 0) or (area is not None and area <= 0):
            raise ValueError(f"{path.name} row {n}: areas must be positive")
        rows.append(Row(rec["design"], rec.get("top", ""), ge, area, n2))
    return rows


def run_gatecost(path: Path, top: str, profile: str | None) -> tuple[tuple[float, float, float], float]:
    """((min, typ, max) in GE, NAND2 area in um2 of the profile) from ``gatecost cost --format json``."""
    cmd = [sys.executable, str(LAUNCHER), "cost", "--format", "json", "--no-cache"]
    if path.is_dir():
        cmd += ["--root", str(path)]
    else:
        cmd += ["--root", str(path.parent), str(path)]
    if top:
        cmd += ["--top", top]
    if profile:
        cmd += ["--profile", profile]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"gatecost failed on {path}: {proc.stdout.strip()[:200] or proc.stderr.strip()[-200:]}") from None
    if doc.get("status") == "error" or not doc.get("result"):
        err = doc.get("error") or {}
        raise RuntimeError(f"gatecost failed on {path}: {err.get('code')} {err.get('message')}")
    ge = doc["result"]["total"]["ge"]
    return (ge["min"], ge["typ"], ge["max"]), doc["profile"]["nand2_area_um2"]


def compare(rows: list[Row], estimate, nand2_area: float | None = None) -> tuple[list[Result], list[str]]:
    """Pure core. ``estimate(row) -> ((min, typ, max), profile_nand2_area)``. Returns results and skipped notes."""
    results: list[Result] = []
    skipped: list[str] = []
    for row in rows:
        try:
            (lo, typ, hi), prof_nand2 = estimate(row)
        except RuntimeError as exc:
            skipped.append(f"{row.design}: {exc}")
            continue
        if row.ref_ge is not None:
            ref = row.ref_ge
        else:
            unit = row.nand2_area_um2 or nand2_area or prof_nand2
            ref = row.area_um2 / unit
        results.append(Result(row.design, lo, typ, hi, ref))
    return results, skipped


def statistics_of(results: list[Result]) -> dict:
    if not results:
        return {"n": 0}
    abs_err = [abs(r.error_pct) for r in results]
    logs = [math.log(r.ratio) for r in results if r.ratio > 0 and not math.isnan(r.ratio)]
    return {
        "n": len(results),
        "mean_abs_error_pct": round(statistics.fmean(abs_err), 2),
        "median_abs_error_pct": round(statistics.median(abs_err), 2),
        "bias_pct": round(statistics.fmean(r.error_pct for r in results), 2),
        "geomean_ratio": round(math.exp(statistics.fmean(logs)), 3) if logs else None,
        "log_ratio_stdev": round(statistics.pstdev(logs), 3) if len(logs) > 1 else 0.0,
        "worst_design": max(results, key=lambda r: abs(r.error_pct)).design,
        "worst_abs_error_pct": round(max(abs_err), 2),
        "inside_range_share": round(sum(r.inside for r in results) / len(results), 3),
    }


def render_table(results: list[Result], skipped: list[str], stats: dict) -> str:
    out = [f"{'design':<24}{'estimate (min..max)':>26}{'reference':>12}{'error%':>9}  in-range"]
    for r in results:
        est = f"{r.est_typ:.0f} ({r.est_min:.0f}..{r.est_max:.0f})"
        out.append(f"{r.design:<24}{est:>26}{r.ref_ge:>12.0f}{r.error_pct:>+9.1f}  {'yes' if r.inside else 'no'}")
    out.append("")
    out += [f"{k}: {v}" for k, v in stats.items()]
    out += [f"SKIPPED {s}" for s in skipped]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare gatecost estimates with reference synthesis areas.")
    ap.add_argument("rtl_dir", type=Path)
    ap.add_argument("reference_csv", type=Path)
    ap.add_argument("--nand2-area", type=float, metavar="UM2", help="area of one NAND2 in the reference library")
    ap.add_argument("--profile", help="gatecost cost profile")
    ap.add_argument("--format", choices=("table", "json"), default="table")
    ap.add_argument("--tolerance", type=float, metavar="PCT", help="exit 1 if the median absolute error exceeds PCT")
    args = ap.parse_args(argv)
    try:
        rows = read_reference(args.reference_csv)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    def estimate(row: Row):
        return run_gatecost(args.rtl_dir / row.design, row.top, args.profile)

    results, skipped = compare(rows, estimate, args.nand2_area)
    stats = statistics_of(results)
    if args.format == "json":
        doc = {"statistics": stats, "skipped": skipped, "designs": [
            {"design": r.design, "min": r.est_min, "typ": r.est_typ, "max": r.est_max, "reference_ge": round(r.ref_ge, 3),
             "error_pct": round(r.error_pct, 2), "inside_range": r.inside} for r in results]}
        sys.stdout.write(json.dumps(doc, indent=2) + "\n")
    else:
        sys.stdout.write(render_table(results, skipped, stats))
    if args.tolerance is not None and stats.get("median_abs_error_pct", 0) > args.tolerance:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
