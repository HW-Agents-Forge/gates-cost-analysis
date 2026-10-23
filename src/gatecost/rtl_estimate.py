"""``estimate --rtl``: wrap an RTL fragment in an implicit module/entity and cost it (CLI.md 3.2).

Inference rules (heuristics, each reported through ``ASSUMED`` lines):
  * identifiers used but not declared are inputs; width from ``--widths``, default 32 (ASSUMED);
  * identifiers assigned and not declared are outputs (registers when assigned in a clocked
    block); width from ``--widths``, else inferred from the right-hand side (the widest input,
    the sum of the two widest for a product, 1 for a comparison), ASSUMED;
  * ``clk`` (and ``rst``/``reset``/``rst_n``...) are implicit 1-bit inputs.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .cost import Profile, build_cost_tree, tree_confidence
from .errors import GatecostError
from .result import EstimateResult, Ge3

TOP = "gatecost_fragment"
CLOCKS = {"clk", "clock", "clk_i"}
RESETS = {"rst", "reset", "rst_n", "resetn", "reset_n", "arst", "arst_n", "rstn"}
V_KW = set("""module endmodule input output inout wire reg logic integer genvar generate endgenerate assign always
always_ff always_comb always_latch begin end if else case casez casex endcase default for while posedge negedge
or and not signed unsigned parameter localparam function endfunction initial int bit byte""".split())
VHDL_KW = set("""library use all entity is port in out inout of architecture begin end process if then elsif else case when
others for loop generate signal variable constant to downto and or xor nand nor xnor not mod rem abs rising_edge
falling_edge event std_logic std_logic_vector unsigned signed integer natural resize to_unsigned to_signed
to_integer ieee numeric_std std_logic_1164 null wait until after open with select shift_left shift_right
rotate_left rotate_right sll srl sra sla rol ror downto""".split())


def _strip(text: str, lang: str) -> str:
    if lang == "vhdl":
        text = re.sub(r"--[^\n]*", " ", text)
        text = re.sub(r"\"[^\"]*\"", " 0 ", text)
        text = re.sub(r"'\w'", " 0 ", text)
        text = re.sub(r"'\w+", " ", text)  # attributes
        text = re.sub(r"\b\d+#\w+#", " 0 ", text)
    else:
        text = re.sub(r"//[^\n]*", " ", text)
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        text = re.sub(r"\"[^\"]*\"", " 0 ", text)
        text = re.sub(r"\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+", " 0 ", text)
        text = re.sub(r"[$`]\w+", " ", text)
    return text


def _ids(text: str, kw: set[str], lang: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?<![\w.'])([A-Za-z_]\w*)", text):
        n = m.group(1)
        if (n.lower() if lang == "vhdl" else n) in kw:
            continue
        out.append(n.lower() if lang == "vhdl" else n)
    return out


def _infer_width(rhs: str, widths: dict[str, int], known: dict[str, int], lang: str) -> int:
    kw = VHDL_KW if lang == "vhdl" else V_KW
    names = [n for n in _ids(_strip(rhs, lang), kw, lang) if n in known or n in widths]
    ws = sorted((widths.get(n, known.get(n, 32)) for n in names), reverse=True) or [32]
    if re.search(r"==|!=|>=|<=|(?<![<>=!])[<>](?![<>=])|&&|\|\||(?<![<>/])=(?![=>])", rhs) and lang == "verilog" and "?" not in rhs:
        return 1
    pw = 0
    for m in re.finditer(r"([A-Za-z_]\w*)\s*\*\s*([A-Za-z_]\w*)", rhs):
        a, b = m.group(1), m.group(2)
        a = a.lower() if lang == "vhdl" else a
        b = b.lower() if lang == "vhdl" else b
        pw = max(pw, widths.get(a, known.get(a, 32)) + widths.get(b, known.get(b, 32)))
    return max(ws[0], pw)


class Wrapped:
    def __init__(self, source: str, suffix: str, assumed: list[str], inputs, outputs, regs):
        self.source, self.suffix, self.assumed = source, suffix, assumed
        self.inputs, self.outputs, self.regs = inputs, outputs, regs


def wrap_verilog(fragment: str, widths: dict[str, int]) -> Wrapped:
    clean = _strip(fragment, "verilog")
    assumed: list[str] = []
    declared: dict[str, tuple[str, int]] = {}
    decl_lines: list[str] = []
    body = fragment
    for m in re.finditer(r"\b(input|output|inout|wire|reg|logic)\b\s*(?:signed\s*)?(\[[^\]]*\])?\s*([^;]*);", clean):
        kind, rng, names = m.groups()
        w = 1
        if rng:
            r = re.match(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", rng)
            w = abs(int(r.group(1)) - int(r.group(2))) + 1 if r else 32
        for n in re.findall(r"[A-Za-z_]\w*", names.split("=")[0]):
            declared[n] = (kind, w)
    has_struct = re.search(r"\b(always\w*|assign|initial)\b", clean) is not None
    stmts = [s.strip() for s in re.split(r";", fragment) if s.strip()]
    if not has_struct:
        comb, seq = [], []
        for s in stmts:
            c = _strip(s, "verilog")
            if re.match(r"\s*(input|output|inout|wire|reg|logic)\b", c):
                decl_lines.append(s + ";")
            elif "<=" in c.split("?")[0] and not re.search(r"\bif\b", c):
                seq.append(s + ";")
            elif "=" in c:
                comb.append("assign " + s + ";")
            else:
                seq.append(s + ";")
        body = "\n".join(decl_lines + comb)
        if seq:
            body += "\nalways @(posedge clk) begin\n" + "\n".join(seq) + "\nend"
        clean = _strip(body, "verilog")
    # targets
    clocked_txt = " ".join(re.findall(r"always\w*\s*@\s*\(\s*(?:posedge|negedge)[^)]*\)(.*?)(?=\balways|\Z)", clean, flags=re.S))
    if "@(posedge" in re.sub(r"\s", "", clean) or "@(negedge" in re.sub(r"\s", "", clean):
        clocked_txt = clocked_txt or clean
    targets: dict[str, str] = {}   # name -> rhs sample
    for m in re.finditer(r"(?:\bassign\s+|[;\s]|\bbegin\b|\)\s*)([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*(<=|=)(?!=)\s*([^;]*)", clean):
        n = m.group(1)
        if n in V_KW:
            continue
        targets.setdefault(n, m.group(3))
    seq_targets = {m.group(1) for m in re.finditer(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*<=", clocked_txt)}
    used = {n for n in _ids(clean, V_KW, "verilog")}
    inputs: dict[str, int] = {}
    outputs: dict[str, int] = {}
    regs: set[str] = set()
    sens = set(re.findall(r"(?:posedge|negedge)\s+([A-Za-z_]\w*)", clean))
    for n in sorted(used | sens):
        if n in declared:
            continue
        if n in sens or n in CLOCKS or (n in RESETS and n not in targets):
            if n in sens or n in CLOCKS or n in RESETS:
                inputs[n] = 1
                continue
        if n in targets:
            continue
        if n in widths:
            inputs[n] = widths[n]
        else:
            inputs[n] = 32
            assumed.append(f"input {n} width not given, 32 used")
    if (not has_struct and seq_targets) and "clk" not in inputs and "clk" not in declared:
        inputs["clk"] = 1
    known = {**{n: w for n, (k, w) in declared.items()}, **inputs}
    for n in sorted(targets):
        if n in declared:
            continue
        if n in widths:
            outputs[n] = widths[n]
        else:
            outputs[n] = _infer_width(targets[n], widths, known, "verilog")
            assumed.append(f"output {n} width inferred as {outputs[n]}")
        known[n] = outputs[n]
        if n in seq_targets or re.search(r"always\w*\s*(@\s*\(\s*\*|_comb)", clean):
            regs.add(n)
    ports = [f"input wire {'[%d:0] ' % (w - 1) if w > 1 else ''}{n}" for n, w in inputs.items()]
    ports += [f"output logic {'[%d:0] ' % (w - 1) if w > 1 else ''}{n}" for n, w in outputs.items()]
    text = f"module {TOP}(\n  " + ",\n  ".join(ports) + "\n);\n" + body + "\nendmodule\n"
    return Wrapped(text, ".sv", assumed, inputs, outputs, regs)


def wrap_vhdl(fragment: str, widths: dict[str, int]) -> Wrapped:
    clean = _strip(fragment, "vhdl")
    assumed: list[str] = []
    low = clean.lower()
    sig_decl = {m.group(1).lower() for m in re.finditer(r"\b(?:signal|variable|constant)\s+([A-Za-z_]\w*)", clean, flags=re.I)}
    targets: dict[str, str] = {}
    for m in re.finditer(r"([A-Za-z_]\w*)\s*(?:\([^)]*\)\s*)?<=\s*([^;]*)", clean):
        targets.setdefault(m.group(1).lower(), m.group(2))
    clocked = bool(re.search(r"rising_edge|falling_edge|\bevent\b", low)) or bool(re.search(r"\bprocess\b", low) and "clk" in low)
    used = set(_ids(clean, VHDL_KW, "vhdl"))
    inputs: dict[str, int] = {}
    outputs: dict[str, int] = {}
    for n in sorted(used):
        if n in sig_decl or n in targets:
            continue
        if n in CLOCKS or n in RESETS:
            inputs[n] = 1
        elif n in widths or n in {k.lower() for k in widths}:
            inputs[n] = widths.get(n, {k.lower(): v for k, v in widths.items()}.get(n, 32))
        else:
            inputs[n] = 32
            assumed.append(f"input {n} width not given, 32 used")
    if clocked and not (set(inputs) & CLOCKS) and "clk" not in sig_decl:
        inputs["clk"] = 1
    lw = {k.lower(): v for k, v in widths.items()}
    known = dict(inputs)
    for n in sorted(targets):
        if n in sig_decl:
            continue
        if n in lw:
            outputs[n] = lw[n]
        else:
            outputs[n] = _infer_width(targets[n], lw, known, "vhdl")
            assumed.append(f"output {n} width inferred as {outputs[n]}")
        known[n] = outputs[n]

    def typ(n, w):
        return "std_logic" if (n in CLOCKS or n in RESETS) else f"unsigned({w - 1} downto 0)"
    ports = [f"{n} : in {typ(n, w)}" for n, w in inputs.items()] + [f"{n} : out {typ(n, w)}" for n, w in outputs.items()]
    frag = fragment
    if clocked and not re.search(r"\bprocess\b", low):
        frag = "process (clk) begin\nif rising_edge(clk) then\n" + fragment + "\nend if;\nend process;"
    text = ("library ieee;\nuse ieee.std_logic_1164.all;\nuse ieee.numeric_std.all;\n"
            f"entity {TOP} is\n  port (\n    " + ";\n    ".join(ports) + "\n  );\nend entity;\n"
            f"architecture rtl of {TOP} is\nbegin\n{frag}\nend architecture;\n")
    return Wrapped(text, ".vhd", assumed, inputs, outputs, set())


def estimate_fragment(fragment: str, widths: dict[str, int], lang: str, profile: Profile, elaborate=None) -> EstimateResult:
    if elaborate is None:
        from .cli.cmd_cost import get_elaborator
        elaborate = get_elaborator()
    lang = (lang or "verilog").lower()
    if lang in ("vhdl",):
        w = wrap_vhdl(fragment, widths)
    elif lang in ("verilog", "systemverilog", "sv"):
        w = wrap_verilog(fragment, widths)
    else:
        raise GatecostError("BAD_ARGUMENT", f"--lang must be verilog or vhdl, got {lang!r}")
    with tempfile.TemporaryDirectory(prefix="gatecost_rtl_") as td:
        path = Path(td) / f"{TOP}{w.suffix}"
        path.write_text(w.source, encoding="utf-8")
        res = elaborate([path], root=Path(td), top=TOP, defines={}, incdirs=[], limits={}, side="after")
        errs = [d for d in res.diagnostics if getattr(d, "severity", "") == "error"]
        if errs or res.top_key is None:
            msg = errs[0].message if errs else "no module produced"
            raise GatecostError("BAD_ARGUMENT", f"RTL fragment could not be elaborated: {msg}")
        tree = build_cost_tree(res.design, profile, config={"top": TOP, "root": td})
    tot = tree.total()
    ops = [o for m in tree.modules.values() for o in m.ops]
    ops.sort(key=lambda o: (-o.cost.typ, o.id))
    arch = dict(ops[0].arch) if ops else {}
    bd: dict[str, int] = {}
    for o in ops:
        for k, v in o.breakdown.items():
            bd[k] = bd.get(k, 0) + v
    normalized = "RTL " + (", ".join(dict.fromkeys(o.description for o in ops)) if ops else "(no hardware)")
    assumed = tuple(w.assumed) + tuple(res.assumptions or ())
    return EstimateResult("rtl", fragment.strip(), normalized, Ge3(tot.min, tot.typ, tot.max), arch, bd,
                          tree_confidence(tree, profile), assumed)
