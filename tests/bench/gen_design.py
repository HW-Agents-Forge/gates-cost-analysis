"""Synthetic design generator for the benchmarks (IMPLEMENTATION.md stage 8).

``generate(kind, lines, seed)`` returns ``{filename: text}`` for a hierarchical design of roughly
``lines`` source lines: leaf blocks with datapaths, FSMs, memories and generate loops, mid-level
modules instantiating them, and one top.  ``kind`` is ``"sv"`` or ``"vhdl"``.  Deterministic.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

SV_LEAF = """\
// leaf block {k}
module leaf{k} #(parameter W = {w}, parameter D = {d}) (
  input  logic clk, input logic rst_n,
  input  logic [W-1:0] a, input logic [W-1:0] b,
  input  logic [1:0] op, input logic en,
  output logic [W-1:0] q, output logic [W:0] sum, output logic flag
);
  logic [W-1:0] r0, r1, t0;
  logic [3:0] cnt;
  typedef enum logic [1:0] {{IDLE, RUN, DONE}} st_t;
  st_t st;
  logic [W-1:0] mem [0:15];
  wire [W-1:0] mulx = a[W/2-1:0] * b[W/2-1:0];
  assign sum = a + b;
  assign flag = (r0 == r1) && (cnt == D);
  always_comb begin
    case (op)
      2'd0: t0 = a & b;
      2'd1: t0 = a | b;
      2'd2: t0 = a ^ b;
      default: t0 = a - b;
    endcase
  end
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      r0 <= '0; r1 <= '0; cnt <= '0; q <= '0; st <= IDLE;
    end else begin
      if (en) begin
        r0 <= t0;
        r1 <= r0 + mulx;
        cnt <= (cnt == D) ? 4'd0 : cnt + 1'b1;
        mem[a[3:0]] <= r1;
      end
      case (st)
        IDLE: if (en) st <= RUN;
        RUN: if (cnt == D) st <= DONE;
        default: st <= IDLE;
      endcase
      q <= (st == DONE) ? mem[b[3:0]] : (r1 ^ {{{{(W/2){{cnt[0]}}}}, r0[W/2-1:0]}});
    end
  end
  genvar i;
  generate for (i = 0; i < 4; i = i + 1) begin : g
    wire [W-1:0] t = a ^ (b << i);
    wire p = ^t;
  end endgenerate
endmodule
"""

SV_MID = """\
module mid{k} (input logic clk, input logic rst_n, input logic [31:0] a, input logic [31:0] b,
               input logic [1:0] op, input logic en, output logic [31:0] q);
  logic [31:0] qs [0:{n1}];
  logic [32:0] ss [0:{n1}];
  logic [{n1}:0] fs;
{insts}
  always_comb begin
    q = '0;
{xors}
  end
endmodule
"""

VHDL_LEAF = """\
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity vleaf{k} is
  generic (W : natural := {w}; D : natural := {d});
  port (
    clk, rst_n : in std_logic;
    a, b : in std_logic_vector(W-1 downto 0);
    op : in std_logic_vector(1 downto 0);
    en : in std_logic;
    q : out std_logic_vector(W-1 downto 0);
    sum : out std_logic_vector(W downto 0);
    flag : out std_logic
  );
end entity;

architecture rtl of vleaf{k} is
  signal r0, r1, t0 : unsigned(W-1 downto 0);
  signal cnt : unsigned(3 downto 0);
  type st_t is (IDLE, RUN, DONE);
  signal st : st_t;
  type mem_t is array (0 to 15) of std_logic_vector(W-1 downto 0);
  signal mem : mem_t;
  signal mulx : unsigned(W-1 downto 0);
begin
  sum <= std_logic_vector(resize(unsigned(a), W+1) + resize(unsigned(b), W+1));
  mulx <= resize(unsigned(a(W/2-1 downto 0)) * unsigned(b(W/2-1 downto 0)), W);
  flag <= '1' when (r0 = r1) and (cnt = to_unsigned(D, 4)) else '0';
  comb : process (a, b, op)
  begin
    case op is
      when "00" => t0 <= unsigned(a) and unsigned(b);
      when "01" => t0 <= unsigned(a) or unsigned(b);
      when "10" => t0 <= unsigned(a) xor unsigned(b);
      when others => t0 <= unsigned(a) - unsigned(b);
    end case;
  end process;
  seq : process (clk, rst_n)
  begin
    if rst_n = '0' then
      r0 <= (others => '0'); r1 <= (others => '0'); cnt <= (others => '0');
      q <= (others => '0'); st <= IDLE;
    elsif rising_edge(clk) then
      if en = '1' then
        r0 <= t0;
        r1 <= r0 + mulx;
        if cnt = to_unsigned(D, 4) then
          cnt <= (others => '0');
        else
          cnt <= cnt + 1;
        end if;
        mem(to_integer(unsigned(a(3 downto 0)))) <= std_logic_vector(r1);
      end if;
      case st is
        when IDLE => if en = '1' then st <= RUN; end if;
        when RUN => if cnt = to_unsigned(D, 4) then st <= DONE; end if;
        when others => st <= IDLE;
      end case;
      if st = DONE then
        q <= mem(to_integer(unsigned(b(3 downto 0))));
      else
        q <= std_logic_vector(r1 xor r0);
      end if;
    end if;
  end process;
  gen : for i in 0 to 3 generate
    signal t : std_logic_vector(W-1 downto 0);
  begin
    t <= a xor b;
  end generate;
end architecture;
"""

VHDL_MID = """\
library ieee;
use ieee.std_logic_1164.all;

entity vmid{k} is
  port (clk, rst_n : in std_logic; a, b : in std_logic_vector(31 downto 0);
        op : in std_logic_vector(1 downto 0); en : in std_logic; q : out std_logic_vector(31 downto 0));
end entity;

architecture rtl of vmid{k} is
{decls}
begin
{insts}
  q <= {xors};
end architecture;
"""


def _sv(lines: int, rnd: random.Random) -> dict[str, str]:
    leaf_lines = SV_LEAF.count("\n")
    n_leaf = max(2, lines // (leaf_lines + 3))
    per_mid = 8
    files = {}
    chunk = []
    for k in range(n_leaf):
        chunk.append(SV_LEAF.format(k=k, w=rnd.choice([8, 16, 32]), d=rnd.choice([3, 4, 7])))
        if len(chunk) == 20 or k == n_leaf - 1:
            files[f"leaf_{k // 20:04d}.sv"] = "".join(chunk)
            chunk = []
    mids = []
    for m, base in enumerate(range(0, n_leaf, per_mid)):
        ks = list(range(base, min(base + per_mid, n_leaf)))
        insts = "\n".join(
            f"  leaf{k} #(.W({rnd.choice([8, 16, 32])})) u{j}(.clk(clk), .rst_n(rst_n), .a(a), .b(b), .op(op), .en(en),"
            f" .q(qs[{j}][31:0]), .sum(ss[{j}]), .flag(fs[{j}]));" for j, k in enumerate(ks))
        xors = "\n".join(f"    q = q ^ qs[{j}];" for j in range(len(ks)))
        mids.append(SV_MID.format(k=m, n1=len(ks) - 1, insts=insts, xors=xors))
        files[f"mid_{m:04d}.sv"] = mids[-1]
    top = ["module top(input logic clk, input logic rst_n, input logic [31:0] a, b, input logic [1:0] op,"
           " input logic en, output logic [31:0] q);\n"]
    top.append(f"  logic [31:0] y [0:{len(mids) - 1}];\n")
    for m in range(len(mids)):
        top.append(f"  mid{m} u{m}(.clk(clk), .rst_n(rst_n), .a(a), .b(b), .op(op), .en(en), .q(y[{m}]));\n")
    top.append("  always_comb begin q = '0;\n" + "".join(f"    q = q ^ y[{m}];\n" for m in range(len(mids))) + "  end\nendmodule\n")
    files["top.sv"] = "".join(top)
    return files


def _vhdl(lines: int, rnd: random.Random) -> dict[str, str]:
    leaf_lines = VHDL_LEAF.count("\n")
    n_leaf = max(2, lines // (leaf_lines + 3))
    per_mid = 8
    files = {}
    chunk = []
    for k in range(n_leaf):
        chunk.append(VHDL_LEAF.format(k=k, w=rnd.choice([8, 16, 32]), d=rnd.choice([3, 4, 7])))
        if len(chunk) == 20 or k == n_leaf - 1:
            files[f"leaf_{k // 20:04d}.vhd"] = "\n".join(chunk)
            chunk = []
    n_mid = 0
    for m, base in enumerate(range(0, n_leaf, per_mid)):
        ks = list(range(base, min(base + per_mid, n_leaf)))
        decls = "\n".join(f"  signal q{j} : std_logic_vector(31 downto 0);\n  signal s{j} : std_logic_vector(32 downto 0);\n  signal f{j} : std_logic;"
                          for j in range(len(ks)))
        insts = "\n".join(
            f"  u{j} : entity work.vleaf{k} generic map (W => 32) port map (clk => clk, rst_n => rst_n, a => a, b => b,"
            f" op => op, en => en, q => q{j}, sum => s{j}, flag => f{j});" for j, k in enumerate(ks))
        xors = " xor ".join(f"q{j}" for j in range(len(ks)))
        files[f"mid_{m:04d}.vhd"] = VHDL_MID.format(k=m, decls=decls, insts=insts, xors=xors)
        n_mid = m + 1
    decls = "\n".join(f"  signal y{m} : std_logic_vector(31 downto 0);" for m in range(n_mid))
    insts = "\n".join(f"  u{m} : entity work.vmid{m} port map (clk => clk, rst_n => rst_n, a => a, b => b, op => op, en => en, q => y{m});"
                      for m in range(n_mid))
    xors = " xor ".join(f"y{m}" for m in range(n_mid))
    files["top.vhd"] = VHDL_MID.format(k="_top", decls=decls, insts=insts, xors=xors).replace("vmid_top", "vtop")
    return files


def generate(kind: str, lines: int, seed: int = 1) -> dict[str, str]:
    rnd = random.Random(seed)
    if kind == "sv":
        return _sv(lines, rnd)
    if kind == "vhdl":
        return _vhdl(lines, rnd)
    raise ValueError(kind)


def write(files: dict[str, str], directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name).write_text(text)
    return [Path(n) for n in sorted(files)]


if __name__ == "__main__":
    kind, lines, out = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
    fs = generate(kind, lines)
    write(fs, out)
    print(f"{len(fs)} files, {sum(t.count(chr(10)) for t in fs.values())} lines")
