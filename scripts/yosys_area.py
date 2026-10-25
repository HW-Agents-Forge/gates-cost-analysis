#!/usr/bin/env python3
"""Ground-truth area of a Verilog/SystemVerilog design from a real Yosys + liberty synthesis (maintainers only).

Flow (no timing constraint unless ``--abc-delay`` is given, so ABC maps for its default delay-then-area goal)::

    read_verilog -sv FILES ; hierarchy -check -top TOP ; synth -flatten -top TOP
    dfflibmap -liberty LIB ; abc -liberty LIB [-D PS] ; opt_clean -purge ; stat -liberty LIB

Memories are mapped to flip-flops and logic by ``synth`` (``memory_map``): no memory macro exists in the
Nangate 45 nm liberty file, so a RAM/ROM costs its bit-cell flip-flops plus read/write muxes. Latches, enable
and synchronous-reset flops are built from the library cells available (Nangate has no enable/sync-reset flop).

The liberty path comes from ``--liberty`` or the environment variable ``GATECOST_LIBERTY``; the default is
``~/gatecost-refs/nangate45/NangateOpenCellLibrary_typical.lib`` (kept outside the repository).
GE = area / area of ``NAND2_X1`` read from the liberty file.

Usage::

    python3 scripts/yosys_area.py design.v --top T [--liberty LIB] [--abc-delay PS] [--timeout S] [--json]

Stdlib only; the result is deterministic for a given Yosys build.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_LIBERTY = Path.home() / "gatecost-refs" / "nangate45" / "NangateOpenCellLibrary_typical.lib"
NAND2_CELL = "NAND2_X1"

_AREA_RE = re.compile(r"Chip area for (top )?module '([^']*)':\s*([0-9]+(?:\.[0-9]*)?(?:[eE][-+]?[0-9]+)?)")
_SEQ_RE = re.compile(r"of which used for sequential elements:\s*([0-9.]+)")


class YosysError(RuntimeError):
    """Yosys failed, timed out, or printed no parsable area."""


@dataclass
class AreaResult:
    area_um2: float
    ge: float
    sequential_um2: float | None
    nand2_area_um2: float
    yosys_version: str


def liberty_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("GATECOST_LIBERTY") or DEFAULT_LIBERTY)


def read_cell_area(liberty: Path, cell: str = NAND2_CELL) -> float:
    """Area of ``cell`` in the liberty file (first ``area`` attribute after ``cell (NAME)``)."""
    text = liberty.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"cell\s*\(\s*\"?%s\"?\s*\)\s*\{(.*?)\barea\s*:\s*([0-9.]+)" % re.escape(cell), text, re.S)
    if not m:
        raise YosysError(f"cell {cell} (or its area) not found in {liberty}")
    return float(m.group(2))


def parse_stat(output: str) -> tuple[float, float | None]:
    """(chip area um2, sequential area um2 or None) from ``stat -liberty`` text.

    Prefers the ``top module`` line of a hierarchical report, else the last ``module`` line."""
    hits = _AREA_RE.findall(output)
    if not hits:
        raise ValueError("no 'Chip area for module' line in the stat output")
    top = [h for h in hits if h[0]]
    area = float((top or hits)[-1][2])
    seq = _SEQ_RE.findall(output)
    return area, (float(seq[-1]) if seq else None)


def yosys_version(yosys: str = "yosys") -> str:
    try:
        out = subprocess.run([yosys, "-V"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise YosysError(f"cannot run {yosys}: {exc}") from None
    return out


def build_script(files: list[Path], top: str, liberty: Path, abc_delay: int | None, stat_out: Path, sv: bool = True) -> str:
    abc = f"abc -liberty {liberty}" + (f" -D {abc_delay}" if abc_delay else "")
    srcs = " ".join(str(f) for f in files)
    return "; ".join([
        f"read_verilog {'-sv ' if sv else ''}{srcs}",
        f"hierarchy -check -top {top}",
        f"synth -flatten -top {top}",
        f"dfflibmap -liberty {liberty}",
        abc,
        "opt_clean -purge",
        f"tee -o {stat_out} stat -liberty {liberty}",
    ])


def synthesize(files: list[Path], top: str, liberty: Path | None = None, *, abc_delay: int | None = None,
               timeout: float = 600.0, yosys: str = "yosys") -> AreaResult:
    lib = liberty_path(str(liberty) if liberty else None)
    if not lib.is_file():
        raise YosysError(f"liberty file not found: {lib} (set GATECOST_LIBERTY or --liberty)")
    nand2 = read_cell_area(lib)
    files = [Path(f).resolve() for f in files]
    with tempfile.TemporaryDirectory(prefix="gc_yosys_") as tmp:
        stat_out = Path(tmp) / "stat.txt"
        # Plain Verilog files that fail as SystemVerilog (e.g. ``bit`` used as an identifier) are retried as Verilog-2005.
        modes = [True] + ([False] if all(f.suffix == ".v" for f in files) else [])
        for i, sv in enumerate(modes):
            script = build_script(files, top, lib, abc_delay, stat_out, sv)
            try:
                proc = subprocess.run([yosys, "-q", "-p", script], capture_output=True, text=True, timeout=timeout, cwd=tmp)
            except subprocess.TimeoutExpired:
                raise YosysError(f"yosys timed out after {timeout:.0f}s on {top}") from None
            except OSError as exc:
                raise YosysError(f"cannot run {yosys}: {exc}") from None
            if proc.returncode == 0:
                break
            if i == len(modes) - 1:
                lines = (proc.stdout + proc.stderr).strip().splitlines()
                errs = [ln for ln in lines if "ERROR" in ln] or lines[-1:]
                raise YosysError(f"yosys failed (exit {proc.returncode}) on {top}: {errs[0].strip()}\n" + "\n".join(lines[-8:]))
        try:
            area, seq = parse_stat(stat_out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise YosysError(f"cannot parse stat output for {top}: {exc}") from None
    return AreaResult(area, area / nand2, seq, nand2, yosys_version(yosys))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--top", required=True)
    ap.add_argument("--liberty", type=Path)
    ap.add_argument("--abc-delay", type=int, metavar="PS", help="abc -D (ps); default: no timing constraint")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        res = synthesize(args.files, args.top, args.liberty, abc_delay=args.abc_delay, timeout=args.timeout)
    except YosysError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    if args.json:
        print(json.dumps(asdict(res), indent=2))
    else:
        print(f"area_um2={res.area_um2:.3f} ge={res.ge:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
