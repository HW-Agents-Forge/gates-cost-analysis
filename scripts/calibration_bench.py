#!/usr/bin/env python3
"""Calibration bench (maintainers only): gatecost estimates against a Yosys + Nangate 45 nm ground truth.

For every case (generated micro-benchmarks per primitive and width, repository examples, corpus designs) the
bench obtains

* the Yosys area (``scripts/yosys_area.py``: synth -flatten, dfflibmap, abc -liberty, stat -liberty) in GE of
  ``NAND2_X1``, or the frozen value of ``tests/reference/yosys_nangate45.csv`` with ``--frozen`` (no Yosys needed);
* the gatecost estimate (min, typ, max GE) of the *same* RTL, profile ``generic-asic`` by default,

and writes a CSV (name, category, params, GE_yosys, GE_min, GE_typ, GE_max, error_pct, in_range) and a summary
(MAPE, bias, geometric ratio, coverage of [min, max], per category, ten worst deviations).

Usage (from the repository root)::

    python3 scripts/calibration_bench.py                 # full run with Yosys, results in --out
    python3 scripts/calibration_bench.py --frozen        # estimates vs the frozen reference, no Yosys
    python3 scripts/calibration_bench.py --freeze        # (re)write tests/reference/yosys_nangate45.csv from Yosys
    python3 scripts/calibration_bench.py --only add,mul  # restrict to categories or name prefixes

Liberty: ``--liberty`` or ``GATECOST_LIBERTY`` (default ``~/gatecost-refs/nangate45/
NangateOpenCellLibrary_typical.lib``, outside the repository). The micro-benchmark RTL is written, identically
on every run, to ``tests/reference/designs/<name>/t.v``. The frozen CSV is only ever rewritten by ``--freeze``.
VHDL designs are skipped (Yosys cannot read VHDL without the ghdl plugin). Not a test and never run by the suite.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import importlib.util
import math
import os
import statistics
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
REF_DIR = REPO / "tests" / "reference"
DESIGNS = REF_DIR / "designs"
FROZEN = REF_DIR / "yosys_nangate45.csv"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------------------------- cases
@dataclass(frozen=True)
class Case:
    name: str
    category: str
    params: str
    design: str          # path relative to the repository root (directory)
    top: str
    source: str | None = None   # generated Verilog (micro-benchmarks); None for existing designs


def _clog2(n: int) -> int:
    return max(1, (n - 1).bit_length())


def _mod(body: str, ports: str) -> str:
    return f"module t({ports});\n{body}endmodule\n"


def _binop(op: str, w: int, signed: bool = False, outw: str | None = None) -> str:
    s = "signed " if signed else ""
    o = outw or f"[{w - 1}:0]"
    return _mod(f"  assign y = a {op} b;\n", f"input {s}[{w - 1}:0] a, b, output {o} y")


def _gen() -> list[tuple[str, str, str, str]]:
    """(name, category, params, verilog)."""
    out: list[tuple[str, str, str, str]] = []

    def add(name, cat, params, src):
        out.append((name, cat, params, src))

    for w in (4, 8, 16, 32, 64):
        add(f"add_w{w}", "add", f"w={w}", _binop("+", w))
        add(f"sub_w{w}", "sub", f"w={w}", _binop("-", w))
    add("add_cout_w16", "add", "w=16 carry_out", _mod("  assign y = a + b;\n", "input [15:0] a, b, output [16:0] y"))
    add("add_const_w16", "add", "w=16 const=0x1234", _mod("  assign y = a + 16'h1234;\n", "input [15:0] a, output [15:0] y"))
    add("inc_w16", "add", "w=16 const=1", _mod("  assign y = a + 16'd1;\n", "input [15:0] a, output [15:0] y"))
    for w in (4, 8, 16, 32):
        add(f"mul_u{w}", "mul", f"{w}x{w} unsigned", _binop("*", w, False, f"[{2 * w - 1}:0]"))
        add(f"mul_s{w}", "mul", f"{w}x{w} signed", _binop("*", w, True, f"[{2 * w - 1}:0]"))
    add("mul_trunc_u16", "mul", "16x16 trunc=16", _binop("*", 16))
    add("mul_u16x8", "mul", "16x8 unsigned", _mod("  assign y = a * b;\n", "input [15:0] a, input [7:0] b, output [23:0] y"))
    add("mul_const_u16", "mul", "16 const=10", _mod("  assign y = a * 16'd10;\n", "input [15:0] a, output [31:0] y").replace("16'd10", "32'd10"))
    for w in (8, 16):
        add(f"div_u{w}", "div", f"{w}/{w} unsigned", _binop("/", w))
        add(f"mod_u{w}", "div", f"{w}/{w} unsigned", _binop("%", w))
    for w in (4, 8, 16, 32, 64):
        add(f"eq_w{w}", "cmp", f"w={w} eq", _binop("==", w, outw=""))
    for w in (8, 16, 32, 64):
        add(f"ne_w{w}", "cmp", f"w={w} ne", _binop("!=", w, outw=""))
        add(f"lt_u{w}", "cmp", f"w={w} lt unsigned", _binop("<", w, outw=""))
        add(f"slt_s{w}", "cmp", f"w={w} lt signed", _binop("<", w, True, ""))
    for w in (8, 32):
        add(f"le_u{w}", "cmp", f"w={w} le unsigned", _binop("<=", w, outw=""))
    add("eq_const_w32", "cmp", "w=32 eq const", _mod("  assign y = a == 32'hdeadbeef;\n", "input [31:0] a, output y"))
    for w in (8, 32, 64):
        add(f"mux2_w{w}", "mux", f"w={w} 2:1",
            _mod("  assign y = s ? a : b;\n", f"input s, input [{w - 1}:0] a, b, output [{w - 1}:0] y"))
    for k in (4, 8, 16):
        for w in (8, 32):
            sb = _clog2(k)
            ports = f"input [{sb - 1}:0] s, input [{k * w - 1}:0] d, output [{w - 1}:0] y"
            arr = "".join(f"  wire [{w - 1}:0] v{i} = d[{i * w} +: {w}];\n" for i in range(k))
            cases = "".join(f"      {sb}'d{i}: y = v{i};\n" for i in range(k))
            add(f"mux{k}_w{w}", "mux", f"w={w} {k}:1 case",
                _mod(arr + f"  reg [{w - 1}:0] y_r;\n  assign y = y_r;\n  always @* case (s)\n" + cases.replace("y =", "y_r =") +
                     f"      default: y_r = v0;\n  endcase\n", ports))
    add("mux8_slice_w32", "mux", "w=32 8:1 indexed slice",
        _mod("  assign y = d[s*32 +: 32];\n", "input [2:0] s, input [255:0] d, output [31:0] y"))
    add("pmux_case5_w16", "mux", "w=16 5-way case",
        _mod("  always @* case (s)\n    3'd0: y = a; 3'd1: y = b; 3'd2: y = c; 3'd3: y = d; default: y = e;\n  endcase\n",
             "input [2:0] s, input [15:0] a, b, c, d, e, output reg [15:0] y"))
    for w in (8, 16, 32, 64):
        sb = _clog2(w)
        for nm, op in (("shl", "<<"), ("shr", ">>"), ("sar", ">>>")):
            sg = "signed " if nm == "sar" else ""
            add(f"{nm}_w{w}", "shift", f"w={w} {nm} var",
                _mod(f"  assign y = a {op} s;\n", f"input {sg}[{w - 1}:0] a, input [{sb - 1}:0] s, output {sg}[{w - 1}:0] y"))
        add(f"rol_w{w}", "shift", f"w={w} rotate left",
            _mod(f"  assign y = (a << s) | (a >> ({w} - s));\n", f"input [{w - 1}:0] a, input [{sb - 1}:0] s, output [{w - 1}:0] y"))
    for w in (8, 32, 64):
        for nm, op in (("and", "&"), ("or", "|"), ("xor", "^")):
            add(f"{nm}_w{w}", "bitwise", f"w={w}", _binop(op, w))
        add(f"not_w{w}", "bitwise", f"w={w}", _mod("  assign y = ~a;\n", f"input [{w - 1}:0] a, output [{w - 1}:0] y"))
        for nm, op in (("red_and", "&"), ("red_or", "|"), ("red_xor", "^")):
            add(f"{nm}_w{w}", "reduce", f"w={w}", _mod(f"  assign y = {op}a;\n", f"input [{w - 1}:0] a, output y"))
    for s in (3, 5):
        add(f"decode_s{s}", "decode", f"sel={s}", _mod(f"  assign y = {1 << s}'d1 << a;\n", f"input [{s - 1}:0] a, output [{(1 << s) - 1}:0] y"))
    add("popcount_w16", "reduce", "w=16 popcount", _mod("  integer i;\n  always @* begin y = 0; for (i = 0; i < 16; i = i + 1) y = y + a[i]; end\n",
                                                          "input [15:0] a, output reg [4:0] y"))
    for w in (8, 32):
        p = f"input clk, input [{w - 1}:0] d, output reg [{w - 1}:0] q"
        add(f"dff_w{w}", "dff", f"w={w} plain", _mod("  always @(posedge clk) q <= d;\n", p))
        add(f"dff_arst_w{w}", "dff", f"w={w} async_reset",
            _mod(f"  always @(posedge clk or negedge rn) if (!rn) q <= 0; else q <= d;\n", p.replace("input clk", "input clk, rn")))
        add(f"dff_srst_w{w}", "dff", f"w={w} sync_reset",
            _mod("  always @(posedge clk) if (rst) q <= 0; else q <= d;\n", p.replace("input clk", "input clk, rst")))
        add(f"dff_en_w{w}", "dff", f"w={w} enable",
            _mod("  always @(posedge clk) if (en) q <= d;\n", p.replace("input clk", "input clk, en")))
        add(f"dff_en_srst_w{w}", "dff", f"w={w} enable sync_reset",
            _mod("  always @(posedge clk) if (rst) q <= 0; else if (en) q <= d;\n", p.replace("input clk", "input clk, rst, en")))
        add(f"dff_en_arst_w{w}", "dff", f"w={w} enable async_reset",
            _mod("  always @(posedge clk or negedge rn) if (!rn) q <= 0; else if (en) q <= d;\n", p.replace("input clk", "input clk, rn, en")))
    # Asynchronous reset / set flops (category dff_async): active low (arstn) and active high (arstp), with and
    # without enable, combined async + sync reset, non-zero reset value (set), and a register bank sharing one reset.
    for pol, edge, cond in (("n", "negedge rst_n", "!rst_n"), ("p", "posedge rst", "rst")):
        rp = "rst_n" if pol == "n" else "rst"
        for w in (1, 8, 32):
            p = f"input clk, {rp}, input [{w - 1}:0] d, output reg [{w - 1}:0] q"
            pe = p.replace(f"{rp},", f"{rp}, en,")
            if not (pol == "n" and w in (8, 32)):   # dff_arst_w8/32 and dff_en_arst_w8/32 already cover these
                add(f"dff_arst{pol}_w{w}", "dff_async", f"w={w} async_reset active_{'low' if pol == 'n' else 'high'}",
                    _mod(f"  always @(posedge clk or {edge}) if ({cond}) q <= 0; else q <= d;\n", p))
                add(f"dff_arst{pol}_en_w{w}", "dff_async", f"w={w} enable async_reset active_{'low' if pol == 'n' else 'high'}",
                    _mod(f"  always @(posedge clk or {edge}) if ({cond}) q <= 0; else if (en) q <= d;\n", pe))
    for w in (8, 32):
        p = f"input clk, rst_n, srst, input [{w - 1}:0] d, output reg [{w - 1}:0] q"
        add(f"dff_arstn_srst_w{w}", "dff_async", f"w={w} async_reset sync_reset",
            _mod("  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else if (srst) q <= 0; else q <= d;\n", p))
        add(f"dff_arstn_srst_en_w{w}", "dff_async", f"w={w} enable async_reset sync_reset",
            _mod("  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else if (srst) q <= 0; else if (en) q <= d;\n",
                 p.replace("srst,", "srst, en,")))
        ones = f"{{{w}{{1'b1}}}}"
        pe = f"input clk, rst_n, en, input [{w - 1}:0] d, output reg [{w - 1}:0] q"
        add(f"dff_asetn_w{w}", "dff_async", f"w={w} async_set (reset value all ones) active_low",
            _mod(f"  always @(posedge clk or negedge rst_n) if (!rst_n) q <= {ones}; else q <= d;\n", pe.replace(" en,", "")))
        add(f"dff_asetn_en_w{w}", "dff_async", f"w={w} enable async_set (reset value all ones) active_low",
            _mod(f"  always @(posedge clk or negedge rst_n) if (!rst_n) q <= {ones}; else if (en) q <= d;\n", pe))
    add("dff_arstn_val_w8", "dff_async", "w=8 async reset value 0x5A (mixed set/reset bits)",
        _mod("  always @(posedge clk or negedge rst_n) if (!rst_n) q <= 8'h5A; else q <= d;\n",
             "input clk, rst_n, input [7:0] d, output reg [7:0] q"))
    add("dff_asetp_w8", "dff_async", "w=8 async_set (reset value 0xFF) active_high",
        _mod("  always @(posedge clk or posedge rst) if (rst) q <= 8'hFF; else q <= d;\n",
             "input clk, rst, input [7:0] d, output reg [7:0] q"))
    for w in (8, 32):
        regs = "".join(f"  always @(posedge clk or negedge rst_n) if (!rst_n) r{i} <= 0; else if (we{i}) r{i} <= wd;\n"
                       for i in range(8))
        decl = "".join(f"  reg [{w - 1}:0] r{i};\n" for i in range(8))
        outs = ", ".join(f"output [{w - 1}:0] q{i}" for i in range(8))
        assigns = "".join(f"  assign q{i} = r{i};\n" for i in range(8))
        add(f"regbank8_arstn_w{w}", "dff_async", f"8 regs x w={w} enable per register, common async_reset",
            _mod(decl + "".join(f"  wire we{i} = we[{i}];\n" for i in range(8)) + regs + assigns,
                 f"input clk, rst_n, input [7:0] we, input [{w - 1}:0] wd, " + outs))
    add("latch_w8", "dff", "w=8 latch", _mod("  always @* if (g) q = d;\n", "input g, input [7:0] d, output reg [7:0] q"))
    for depth, w in ((4, 8), (8, 8), (16, 8)):
        add(f"shreg_d{depth}_w{w}", "shreg", f"depth={depth} w={w}",
            _mod(f"  reg [{w - 1}:0] r [0:{depth - 1}];\n  integer i;\n  always @(posedge clk) begin\n    r[0] <= d;\n"
                 f"    for (i = 1; i < {depth}; i = i + 1) r[i] <= r[i-1];\n  end\n  assign q = r[{depth - 1}];\n",
                 f"input clk, input [{w - 1}:0] d, output [{w - 1}:0] q"))
    for d, w in ((16, 8), (32, 8), (16, 16), (64, 8)):
        ab = _clog2(d)
        add(f"mem_async_{d}x{w}", "mem", f"{d}x{w} async_read 1w1r",
            _mod(f"  reg [{w - 1}:0] m [0:{d - 1}];\n  always @(posedge clk) if (we) m[wa] <= wd;\n  assign rd = m[ra];\n",
                 f"input clk, we, input [{ab - 1}:0] wa, ra, input [{w - 1}:0] wd, output [{w - 1}:0] rd"))
    add("mem_sync_16x8", "mem", "16x8 sync_read 1w1r",
        _mod("  reg [7:0] m [0:15];\n  always @(posedge clk) begin if (we) m[wa] <= wd; rd <= m[ra]; end\n",
             "input clk, we, input [3:0] wa, ra, input [7:0] wd, output reg [7:0] rd"))
    seed = 12345
    for d, w in ((16, 8), (64, 8)):
        ab = _clog2(d)
        rows = []
        for i in range(d):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            rows.append(f"    {ab}'d{i}: y = {w}'h{(seed >> 8) & ((1 << w) - 1):0{(w + 3) // 4}x};")
        add(f"rom_{d}x{w}", "rom", f"{d}x{w}",
            _mod("  always @* case (a)\n" + "\n".join(rows) + f"\n    default: y = {w}'h00;\n  endcase\n",
                 f"input [{ab - 1}:0] a, output reg [{w - 1}:0] y"))
    for w in (8, 16, 32):
        add(f"counter_w{w}", "counter", f"w={w} enable async_reset",
            _mod("  always @(posedge clk or negedge rn) if (!rn) q <= 0; else if (en) q <= q + 1;\n",
                 f"input clk, rn, en, output reg [{w - 1}:0] q"))
    for k in (3, 4, 8):
        ops = " + ".join(f"a{i}" for i in range(k))
        add(f"sum{k}_w16", "sum", f"k={k} w=16",
            _mod(f"  assign y = {ops};\n", "input [15:0] " + ", ".join(f"a{i}" for i in range(k)) + ", output [15:0] y"))
    for w in (8, 16):
        add(f"mac_comb_w{w}", "mac", f"{w}x{w}+{2 * w}",
            _mod("  assign y = a * b + c;\n", f"input [{w - 1}:0] a, b, input [{2 * w - 1}:0] c, output [{2 * w - 1}:0] y"))
    add("mac_reg_w8", "mac", "8x8 acc=24 reg",
        _mod("  always @(posedge clk or negedge rn) if (!rn) acc <= 0; else if (en) acc <= acc + a * b;\n",
             "input clk, rn, en, input [7:0] a, b, output reg [23:0] acc"))
    return out


# repository designs: (name, directory relative to repo, top, params note)
EXISTING = [
    ("ex_counter_baseline", "examples/counter_width/baseline", "counter", "W=8"),
    ("ex_counter_wider", "examples/counter_width/wider", "counter", "W=32"),
    ("ex_alu_baseline", "examples/alu_multiplier/baseline", "alu", "W=16"),
    ("ex_alu_with_mul", "examples/alu_multiplier/with_mul", "alu", "W=16 +mul"),
    ("ex_fir_baseline", "examples/fir_pipeline/baseline", "fir", "4 taps"),
    ("ex_fir_pipelined", "examples/fir_pipeline/pipelined", "fir", "4 taps registered products"),
    ("ex_mac_baseline", "examples/mac/baseline/sv", "mac", "W=8 ACC_W=24"),
    ("ex_mac_pipelined", "examples/mac/pipelined/sv", "mac", "pipelined"),
    ("ex_fifo_baseline", "examples/fifo_depth/baseline", "fifo", "W=8 D=16"),
    ("ex_fifo_deeper", "examples/fifo_depth/deeper", "fifo", "deeper"),
    ("ex_uart_tx", "examples/uart_tx/baseline", "uart_tx", "DIV=434"),
    ("ex_sv_features", "examples/sv_features/baseline", "top", "N=4 (real parameter)"),
    ("ex_udp_mux", "examples/udp_mux/baseline", "mux4w", "UDP mux2 x4"),
    ("corpus_cpu_datapath", "tests/corpus/verilog", "cpu_datapath", "XLEN=32 (file cpu_datapath.sv)"),
    ("corpus_dma_desc", "tests/corpus/verilog", "dma_desc", "file dma_desc.sv"),
    ("corpus_fifo_gen", "tests/corpus/verilog", "fifo_gen", "DEPTH=32 WIDTH=16 FWFT=1"),
    ("corpus_mac_array", "tests/corpus/verilog", "mac_array", "N=4 W=8"),
]
CORPUS_FILES = {"corpus_cpu_datapath": "cpu_datapath.sv", "corpus_dma_desc": "dma_desc.sv",
                "corpus_fifo_gen": "fifo_gen.sv", "corpus_mac_array": "mac_array.sv"}
SKIPPED_VHDL = ["examples/vhdl_fsm_alu/baseline", "examples/vhdl_fsm_alu/wider",
                "examples/mac/baseline/vhdl", "examples/mac/pipelined/vhdl"]


def all_cases(write: bool = True) -> list[Case]:
    cases = []
    for name, cat, params, src in _gen():
        d = DESIGNS / name
        rel = d.relative_to(REPO).as_posix()
        if write:
            d.mkdir(parents=True, exist_ok=True)
            (d / "t.v").write_text(src, encoding="utf-8")
        cases.append(Case(name, cat, params, rel, "t", src))
    for name, rel, top, params in EXISTING:
        cat = "design" if name.startswith("ex_") else "corpus"
        cases.append(Case(name, cat, params, rel, top))
    return cases


def case_files(case: Case) -> list[Path]:
    d = REPO / case.design
    if case.name in CORPUS_FILES:
        return [d / CORPUS_FILES[case.name]]
    return sorted(p for p in d.iterdir() if p.suffix in (".v", ".sv"))


def gatecost_path(case: Case) -> Path:
    d = REPO / case.design
    return d / CORPUS_FILES[case.name] if case.name in CORPUS_FILES else d


# ------------------------------------------------------------------------------------------- statistics
@dataclass
class Row:
    case: Case
    ge_yosys: float
    est: tuple[float, float, float] | None
    note: str = ""

    @property
    def error_pct(self) -> float | None:
        return None if self.est is None else 100.0 * (self.est[1] - self.ge_yosys) / self.ge_yosys

    @property
    def in_range(self) -> bool | None:
        return None if self.est is None else self.est[0] <= self.ge_yosys <= self.est[2]


def stats(rows: list[Row]) -> dict:
    rs = [r for r in rows if r.est is not None and r.est[1] > 0]
    if not rs:
        return {"n": 0}
    err = [r.error_pct for r in rs]
    logs = [math.log(r.est[1] / r.ge_yosys) for r in rs]
    return {"n": len(rs), "mape_pct": round(statistics.fmean(abs(e) for e in err), 1),
            "median_ape_pct": round(statistics.median(abs(e) for e in err), 1),
            "bias_pct": round(statistics.fmean(err), 1),
            "geomean_ratio": round(math.exp(statistics.fmean(logs)), 3),
            "coverage_pct": round(100.0 * sum(bool(r.in_range) for r in rs) / len(rs), 1)}


def summary(rows: list[Row]) -> str:
    out = ["# gatecost vs Yosys + Nangate 45 nm", ""]
    g = stats(rows)
    out.append("| scope | n | MAPE % | median APE % | bias % | geomean typ/yosys | coverage [min,max] % |")
    out.append("|---|---|---|---|---|---|---|")

    def line(label, s):
        if s["n"]:
            out.append(f"| {label} | {s['n']} | {s['mape_pct']} | {s['median_ape_pct']} | {s['bias_pct']:+} | {s['geomean_ratio']} | {s['coverage_pct']} |")

    line("ALL", g)
    for cat in sorted({r.case.category for r in rows}):
        line(cat, stats([r for r in rows if r.case.category == cat]))
    out += ["", "## Ten worst deviations (|error| of typ)", ""]
    ok = [r for r in rows if r.est is not None]
    for r in sorted(ok, key=lambda r: -abs(r.error_pct))[:10]:
        out.append(f"- {r.case.name} ({r.case.params}): yosys {r.ge_yosys:.1f} GE, typ {r.est[1]:.1f} "
                   f"[{r.est[0]:.1f}..{r.est[2]:.1f}], error {r.error_pct:+.1f}%")
    fails = [r for r in rows if r.est is None]
    if fails:
        out += ["", "## Estimate unavailable", ""] + [f"- {r.case.name}: {r.note}" for r in fails]
    return "\n".join(out) + "\n"


CSV_HEADER = ["name", "category", "params", "GE_yosys", "GE_min", "GE_typ", "GE_max", "error_pct", "in_range"]


def write_results(rows: list[Row], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(CSV_HEADER)
        for r in rows:
            e = r.est or ("", "", "")
            w.writerow([r.case.name, r.case.category, r.case.params, f"{r.ge_yosys:.3f}",
                        *(f"{x:.3f}" if x != "" else "" for x in e),
                        "" if r.error_pct is None else f"{r.error_pct:.2f}", "" if r.in_range is None else int(r.in_range)])


# ------------------------------------------------------------------------------------------------- run
def read_frozen(path: Path = FROZEN) -> dict[str, float]:
    with open(path, newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    return {r["name"]: float(r["ge"]) for r in csv.DictReader(lines)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--frozen", action="store_true", help="use tests/reference/yosys_nangate45.csv instead of running Yosys")
    ap.add_argument("--freeze", action="store_true", help="run Yosys and rewrite the frozen reference CSV")
    ap.add_argument("--liberty", type=Path)
    ap.add_argument("--abc-delay", type=int, metavar="PS")
    ap.add_argument("--profile", default="generic-asic")
    ap.add_argument("--only", help="comma-separated categories or name prefixes")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 2))
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "gatecost_calibration")
    args = ap.parse_args(argv)

    ya = _load("yosys_area")
    vr = _load("validate_reference")
    cases = all_cases()
    if args.only:
        keys = args.only.split(",")
        cases = [c for c in cases if c.category in keys or any(c.name.startswith(k) for k in keys)]
    if args.freeze and args.only:
        sys.stderr.write("error: --freeze rewrites the whole reference; do not combine it with --only\n")
        return 2
    frozen = read_frozen() if args.frozen else None
    lib = ya.liberty_path(str(args.liberty) if args.liberty else None)

    def work(c: Case) -> Row:
        if frozen is not None:
            if c.name not in frozen:
                return Row(c, float("nan"), None, "not in frozen reference")
            ref = frozen[c.name]
        else:
            try:
                ref = ya.synthesize(case_files(c), c.top, lib, abc_delay=args.abc_delay, timeout=args.timeout).ge
            except ya.YosysError as exc:
                return Row(c, float("nan"), None, "YOSYS: " + str(exc).splitlines()[0])
        try:
            (lo, typ, hi), _ = vr.run_gatecost(gatecost_path(c), c.top, args.profile)
        except RuntimeError as exc:
            return Row(c, ref, None, str(exc)[:160])
        return Row(c, ref, (lo, typ, hi))

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        rows = list(pool.map(work, cases))
    failed = sorted((r for r in rows if r.est is None or math.isnan(r.ge_yosys)), key=lambda r: r.case.name)
    notes = [f"{r.case.name}: {r.note}" for r in failed]
    rows = [r for r in rows if not math.isnan(r.ge_yosys)]
    args.out.mkdir(parents=True, exist_ok=True)
    write_results(rows, args.out / "results.csv")
    text = summary(rows) + "\n## Not measured\n\n" + "".join(f"- {n}\n" for n in notes) + "".join(f"- {d} (VHDL: Yosys has no VHDL front-end here)\n" for d in SKIPPED_VHDL)
    (args.out / "summary.md").write_text(text, encoding="utf-8")
    sys.stdout.write(text + f"\nresults: {args.out / 'results.csv'}\n")

    if args.freeze:
        if frozen is not None:
            sys.stderr.write("error: --freeze needs a Yosys run, not --frozen\n")
            return 2
        ver = ya.yosys_version()
        nand2 = ya.read_cell_area(lib)
        flow = (f"read_verilog -sv (plain .v files that fail are retried without -sv); hierarchy -check -top T; synth -flatten -top T; dfflibmap -liberty LIB; "
                f"abc -liberty LIB{' -D ' + str(args.abc_delay) if args.abc_delay else ''}; opt_clean -purge; stat -liberty LIB")
        with open(FROZEN, "w", newline="", encoding="utf-8") as fh:
            fh.write(f"# Yosys ground truth (GE of NAND2_X1), frozen by scripts/calibration_bench.py --freeze; never edited by hand.\n")
            fh.write(f"# yosys: {ver}\n# liberty: {lib.name} (NangateOpenCellLibrary typical, 45 nm), NAND2_X1 = {nand2} um2\n")
            fh.write(f"# command: {flow}\n# date: {datetime.date.today().isoformat()}\n")
            fh.write("# memories/ROM have no macro in this liberty: mapped to flip-flops + logic. VHDL designs are not measured.\n")
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["name", "design", "top", "category", "params", "area_um2", "ge", "nand2_area_um2"])
            for r in sorted(rows, key=lambda r: r.case.name):
                w.writerow([r.case.name, r.case.design if r.case.name not in CORPUS_FILES else f"{r.case.design}/{CORPUS_FILES[r.case.name]}",
                            r.case.top, r.case.category, r.case.params, f"{r.ge_yosys * nand2:.3f}", f"{r.ge_yosys:.4f}", nand2])
        sys.stdout.write(f"frozen reference written: {FROZEN}\n")
    for n in notes:
        sys.stderr.write("WARNING " + n + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
