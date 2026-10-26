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

    python3 scripts/yosys_area.py design.v [more.v ...] --top T [-I DIR]... [-D NAME[=VAL]]... [--param NAME=VALUE]...
        [--filelist FILE.f]... [--liberty LIB] [--abc-delay PS] [--timeout S] [--json]

``--param`` is applied with ``chparam`` after ``read_verilog`` and before ``hierarchy -top`` (the only order in which
Yosys re-elaborates the top with the new value; a quoted value uses ``-set-string``). A design that synthesises to no
cell at all (stat prints no "Chip area" line) is 0 GE with a note. A filelist holds one source per line plus
``+incdir+DIR``, ``+define+NAME[=VAL]``, ``-I DIR``, ``-f FILE`` (nested), ``//`` or ``#`` comments; relative paths
resolve against the filelist's directory, ``$VAR``/``${VAR}`` are expanded from the environment.

Stdlib only; the result is deterministic for a given Yosys build.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
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
    note: str | None = None


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


def parse_stat_or_empty(output: str) -> tuple[float, float | None, str | None]:
    """Like :func:`parse_stat`, but a stat report listing a module with no "Chip area" line (a design with no cell,
    e.g. pure wiring or fully optimised away) is ``(0.0, None, note)``. Text with no module header still raises."""
    try:
        area, seq = parse_stat(output)
    except ValueError:
        if re.search(r"^=== .+ ===\s*$", output, re.M):
            return 0.0, None, "design has no cells after synthesis (zero area)"
        raise
    return area, seq, None


def parse_filelist(path: Path, _seen: frozenset[Path] = frozenset()) -> tuple[list[Path], list[Path], list[str]]:
    """(sources, include dirs, defines) of a ``.f`` file; nested ``-f``/``-F`` are followed (cycles ignored)."""
    path = Path(path).resolve()
    if path in _seen:
        return [], [], []
    seen = _seen | {path}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise YosysError(f"cannot read filelist {path}: {exc}") from None
    base = path.parent
    files: list[Path] = []
    incs: list[Path] = []
    defs: list[str] = []

    def res(x: str) -> Path:
        q = Path(os.path.expandvars(x))
        return q if q.is_absolute() else (base / q)

    for raw in text.splitlines():
        line = re.sub(r"//.*$", "", raw).strip()
        if not line or line.startswith("#"):
            continue
        toks = shlex.split(line)
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.startswith("+incdir+"):
                incs += [res(x) for x in t[8:].split("+") if x]
            elif t.startswith("+define+"):
                defs += [x for x in t[8:].split("+") if x]
            elif t in ("-f", "-F", "-I", "-incdir") and i + 1 < len(toks):
                i += 1
                if t in ("-f", "-F"):
                    f2, i2, d2 = parse_filelist(res(toks[i]), seen)
                    files += f2
                    incs += i2
                    defs += d2
                else:
                    incs.append(res(toks[i]))
            elif t.startswith("-I") and len(t) > 2:
                incs.append(res(t[2:]))
            elif t.startswith("-") or t.startswith("+"):
                pass  # unknown tool option: ignored
            else:
                files.append(res(t))
            i += 1
    return files, incs, defs


def _chparam_cmd(top: str, param: str) -> str:
    name, sep, value = param.partition("=")
    if not sep or not name.strip():
        raise YosysError(f"bad --param {param!r}: expected NAME=VALUE")
    name, value = name.strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return f"chparam -set-string {name} {shlex.quote(value[1:-1])} {top}"
    return f"chparam -set {name} {shlex.quote(value)} {top}"


def yosys_version(yosys: str = "yosys") -> str:
    try:
        out = subprocess.run([yosys, "-V"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise YosysError(f"cannot run {yosys}: {exc}") from None
    return out


def build_script(files: list[Path], top: str, liberty: Path, abc_delay: int | None, stat_out: Path, sv: bool = True,
                 *, incdirs: list[Path] | None = None, defines: list[str] | None = None,
                 params: list[str] | None = None) -> str:
    abc = f"abc -liberty {liberty}" + (f" -D {abc_delay}" if abc_delay else "")
    opts = [f"-I{Path(d).resolve()}" for d in incdirs or []] + [f"-D{d}" for d in defines or []]
    srcs = " ".join(str(f) for f in files)
    read = "read_verilog " + " ".join(([] if not sv else ["-sv"]) + opts + [srcs])
    return "; ".join([read] + [_chparam_cmd(top, p) for p in params or []] + [
        f"hierarchy -check -top {top}",
        f"synth -flatten -top {top}",
        f"dfflibmap -liberty {liberty}",
        abc,
        "opt_clean -purge",
        f"tee -o {stat_out} stat -liberty {liberty}",
    ])


def synthesize(files: list[Path], top: str, liberty: Path | None = None, *, abc_delay: int | None = None,
               timeout: float = 600.0, yosys: str = "yosys", incdirs: list[Path] | None = None,
               defines: list[str] | None = None, params: list[str] | None = None) -> AreaResult:
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
            script = build_script(files, top, lib, abc_delay, stat_out, sv, incdirs=incdirs, defines=defines, params=params)
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
            area, seq, note = parse_stat_or_empty(stat_out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise YosysError(f"cannot parse stat output for {top}: {exc}") from None
    return AreaResult(area, area / nand2, seq, nand2, yosys_version(yosys), note)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--top", required=True)
    ap.add_argument("-I", "--incdir", action="append", default=[], type=Path, metavar="DIR")
    ap.add_argument("-D", "--define", action="append", default=[], metavar="NAME[=VAL]")
    ap.add_argument("--param", action="append", default=[], metavar="NAME=VALUE", help="chparam on the top before hierarchy")
    ap.add_argument("--filelist", "-f", action="append", default=[], type=Path, metavar="FILE.f")
    ap.add_argument("--liberty", type=Path)
    ap.add_argument("--abc-delay", type=int, metavar="PS", help="abc -D (ps); default: no timing constraint")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        files, incs, defs = list(args.files), list(args.incdir), list(args.define)
        for fl in args.filelist:
            f2, i2, d2 = parse_filelist(fl)
            files += f2
            incs += i2
            defs += d2
        if not files:
            raise YosysError("no source file (give FILES or --filelist)")
        res = synthesize(files, args.top, args.liberty, abc_delay=args.abc_delay, timeout=args.timeout,
                         incdirs=incs, defines=defs, params=args.param)
    except YosysError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    if args.json:
        print(json.dumps(asdict(res), indent=2))
    else:
        print(f"area_um2={res.area_um2:.3f} ge={res.ge:.2f}" + (f" note={res.note}" if res.note else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
