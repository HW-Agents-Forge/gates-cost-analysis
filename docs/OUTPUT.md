# gatecost — Output Reference

Status: Draft. Normative companion to `SPEC.md` §5.7, §8 and §9.

`gatecost` has two output formats designed for programs and agents (`agent` and `json`) and two for humans (`text` and `markdown`). The first line of every format states the outcome. All formats are derived from one internal result object, so they never disagree.

## 1. Common Rules

1. **Streams.** Results go to standard output. Progress, logs, and warnings about the tool itself go to standard error. Standard output never contains anything but the requested format.
2. **Determinism.** The same inputs and profile produce byte-identical output, including ordering. There are no timestamps, durations, absolute paths, or random identifiers in `agent`, `text`, or `markdown` output. Timing appears only in `json` under `meta` and can be disabled with `--no-meta`.
3. **Paths** are relative to the project root (or to the working directory when no project file exists), with forward slashes.
4. **Numbers.** Costs are in gate equivalents (GE). `agent` and `text` round to the nearest integer and use no thousands separators; `json` carries exact values rounded to 0.001 GE (internally integer milli-GE). Signs are always explicit on deltas (`+0` is never printed; zero is `0`).
5. **Encoding.** UTF-8 for `json`; pure ASCII for `agent`, so an arrow is written `->` and a range separator is `..`.
6. **Exit codes** are those of `SPEC.md` §8.3, in every format.

## 2. The `agent` Format

### 2.1 Design goals

- Parseable by a trivial line-oriented parser and readable by a language model without explanation.
- Short: the default output fits in about 1,200 characters (about 400 tokens) for a diff, regardless of design size.
- Complete enough that the agent can answer the user's question *and* state how far to trust it, without a second command.

### 2.2 Grammar (`diff`)

The output is a sequence of lines, each beginning with a fixed keyword or a signed number.

```
DELTA <signed GE> GE (<signed lo>..<signed hi>) <signed pct>% verdict=<VERDICT> confidence=<LEVEL>
TOP <module> before=<GE> after=<GE>
<signed GE> <file>:<line> <reason> <description> [x<instances>] [cause=<cause>]     (0 to N times)
OTHERS <count> net=<signed GE>                                                       (optional)
WHY <level>: <reason text>                                                           (when confidence is not high)
WARN <code> <file>:<line> <message>                                                  (0 to M times)
ASSUMED <message>                                                                    (0 to K times)
NOTE ignored regions: <N> (<kind>, <kind>, ...)                                      (optional, last line)
```

- `DELTA`: total change of the typical scenario, its range (`min..max` from `COST_MODEL.md` §5.1), and the percentage relative to `before`. When `before` is zero the percentage is `n/a`.
- `TOP`: the analyzed top module and absolute totals.
- **Contributor lines** follow `DIFF.md` §8: grouped by cause, sorted by absolute delta, at most `--max-lines` entries (default 10). The description is a fixed template: `MUL 16x16`, `ADD w8->w12`, `DFF x36 (register stage)`, `MUX 4:1 w32`, `config W 16->32 (14 ops)`.
- `OTHERS`: the contributors folded under the report threshold or beyond `--max-lines`; omitted when there are none.
- `WHY`: present when confidence is `medium` or `low`; it names the dominant reasons in a single line (a range reason reads `+/-r%` when the range is symmetric around `typ`, `-x%/+y%` otherwise), for example `WHY low: 1 unsupported construct in the changed cone; 62% of delta in div class (poor model quality)`.
- `WARN`: only diagnostics flagged `affects_cost` (`FRONTEND.md` §6) and diagnostics that changed between the two sides. At most 5 lines; an extra line `WARN ... +N more (use --format json)` indicates truncation. When implicit discovery (no path, no `--filelist`, no `sources` in the project file) mixes several definitions of the same module, the first line is `WARN DISCOVERY_DUPLICATES <file>:<line> implicit discovery mixes variants: <n> duplicate definition(s) (module 'X', ...) across <files>; last wins, kept <files>; pass a path, --top or --filelist`, followed by the `DUPLICATE_MODULE` lines it explains. The confidence then says `WHY low: 1 DUPLICATE_MODULE diagnostic (module defined in several files; the last definition wins, the design may mix variants)` (with several unsupported constructs: `..., including k DUPLICATE_MODULE`). Do not run `gatecost` without a path in a directory that holds several variants of a design.
- `ASSUMED`: assumptions applied by the front-end that affect the changed cone (for example `ASSUMED parameter DEPTH unresolved, default 32 used`).
- `NOTE`: one line, always the last, summarising the `IGNORED_REGION` diagnostics (regions the tool skipped on purpose: `initial` blocks that initialise nothing, assertions, `$error` in a generate, ...): their count and at most 4 kinds, most frequent first, then `+N more`. Absent when no region is ignored and with `--quiet`. It is cost-neutral information, not a warning: it only tells the reader that some code was not modelled. `NOTE` ranks after `ASSUMED` in line order.

### 2.3 Examples

Typical increase:

```
DELTA +342 GE (+281..+410) +1.2% verdict=INCREASE confidence=medium
TOP top_core before=28410 after=28752
+210 alu.sv:87 added MUL 16x16
+96 alu.sv:102 width MUX 4:1 w16->w32
+36 regfile.sv:41 added DFF x36 (register stage)
OTHERS 2 net=0
WHY medium: delta range is -18%/+20% of the estimate
```

A parameter change affecting many operations:

```
DELTA +1260 GE (+990..+1580) +4.4% verdict=INCREASE confidence=medium
TOP top_core before=28410 after=29670
+1260 config.sv:12 parameter W 16->32 (14 ops) x1
WHY medium: delta range is -21%/+25% of the estimate
```

Increase with a caveat:

```
DELTA +180 GE (+60..+480) +0.6% verdict=INCREASE confidence=low
TOP top_core before=28410 after=28590
+170 div.sv:33 added DIV 32/16
+10 div.sv:40 added MUX 2:1 w32
WHY low: 94% of delta in div class (poor model quality)
WARN UNSUPPORTED_CONSTRUCT alu.sv:140 unbounded while loop (cost not included)
```

Formatting-only edit:

```
DELTA 0 GE (0..0) 0.0% verdict=NO_CHANGE confidence=high
TOP top_core before=28410 after=28410
```

No baseline could be compared:

```
DELTA n/a verdict=NOT_COMPARABLE confidence=low
WHY low: baseline has no module named top_core (top changed?)
```

Error (no sources found):

```
ERROR NO_SOURCES no RTL files found under . (searched *.v *.sv *.vh *.svh *.vhd *.vhdl)
```

### 2.4 Other commands in `agent` format

`cost`:

```
COST <typ> GE (<min>..<max>) confidence=<LEVEL>
TOP <module>
<GE> <module> [x<instances>] <share>%        (top N modules by cost)
CATEGORIES seq=<GE> addcmp=<GE> mul=<GE> select=<GE> logic=<GE> shift=<GE> div=<GE> mem=<GE>
WHY ... / WARN ... / ASSUMED ... / NOTE ...    (as for diff)
```

`cost` has no changed cone: its confidence reason reads `N unsupported constructs in the analyzed design`. Only the diagnostics of the analyzed cone are listed: those of a source file that hosts no module instantiated under the top (for example the `IGNORED_REGION` of the hundreds of modules a large repository holds besides the one analyzed) are dropped when they cannot change the cost; the JSON `meta.diagnostics_out_of_cone` counts them. Every `affects_cost` diagnostic stays visible, wherever it comes from, and so do diagnostics without a file or in a header file.

`estimate`:

```
ESTIMATE <typ> GE (<min>..<max>) confidence=<LEVEL>
OP <description normalized, e.g. MUL 32x32>
ARCH min=<arch> typ=<arch> max=<arch>
BREAKDOWN <primitive>=<count> ...
```

`estimate --compare`:

```
COMPARE 2 alternatives basis=typ
A <typ> GE (<min>..<max>) <expression A>
B <typ> GE (<min>..<max>) <expression B>
BEST <A|B> saves <GE> GE vs <other> ranges=<overlap|disjoint>
ASSUMED ...
```

`ranges=overlap` means the preference is uncertain; the agent should say so.

`snapshot`:

```
SNAPSHOT saved id=<short hash> files=<count> modules=<count>
```

### 2.5 Verbosity

| Option | Effect |
|---|---|
| `--max-lines N` | Maximum number of contributor lines (default 10). |
| `--verbose` | Adds, after the contributors: `MODULE` lines (intrinsic and multiplicity per changed module), a `CATEGORIES` line (per-category delta), and `STRUCT` lines (modules or instances added or removed). |
| `--quiet` | Prints only the `DELTA` line (and `ERROR`); no `NOTE`. |

## 3. The `json` Format

### 3.1 Envelope

Every JSON output is one object:

```json
{
  "schema_version": 1,
  "tool": "gatecost",
  "command": "diff",
  "status": "ok",
  "profile": {"name": "generic-asic", "nand2_area_um2": 0.798},
  "config": {"top": "top_core", "defines": {"SYNTHESIS": "1"}, "params": {"DATA_WIDTH": 8}, "sources": 42},
  "result": {},
  "diagnostics": [],
  "meta": {"elapsed_ms": 212, "cache": "warm"}
}
```

- `status` is `ok`, `partial` (a result exists but diagnostics that affect cost were raised), or `error` (no result).
- `result` has a shape specific to `command` (§3.2 to §3.4). When `status` is `error`, `result` is `null` and an `error` object `{"code": ..., "message": ...}` is present.
- Keys are emitted in a fixed order. Unknown keys must be ignored by consumers. `schema_version` is incremented only for changes that remove or alter existing fields; adding fields never increments it.

### 3.2 `diff` result

```json
{
  "verdict": "INCREASE",
  "confidence": {"level": "medium", "range_ratio": 0.19,
                 "reasons": ["delta range is -18%/+20% of the estimate"]},
  "top": "top_core",
  "before": {"ge": {"min": 27900, "typ": 28410, "max": 29100}},
  "after":  {"ge": {"min": 28181, "typ": 28752, "max": 29510}},
  "delta":  {"ge": {"min": 281, "typ": 342, "max": 410}, "percent": 1.2},
  "contributors": [
    {
      "rank": 1,
      "file": "alu.sv", "line": 87,
      "location_side": "after",
      "reason": "added",
      "cause": null,
      "description": "MUL 16x16",
      "op": "MUL", "widths": [16, 16], "category": "mul",
      "module": "alu", "instances": 1,
      "delta_ge": {"min": 180, "typ": 210, "max": 250},
      "per_instance_ge": {"min": 180, "typ": 210, "max": 250},
      "children": []
    }
  ],
  "others": {"count": 2, "net_ge": {"min": 0, "typ": 0, "max": 0}},
  "by_module": [
    {"module": "alu", "parameters": {}, "instances_before": 1, "instances_after": 1,
     "intrinsic_ge": {"min": 180, "typ": 210, "max": 250},
     "multiplicity_ge": {"min": 0, "typ": 0, "max": 0}}
  ],
  "by_category": {"mul": 210, "select": 96, "seq": 36},
  "structural": [],
  "diagnostics_delta": {"new": [], "resolved": []}
}
```

- `location_side` is `after` or `before` (`DIFF.md` §6).
- `cause` is `null` or an object `{"kind": "parameter" | "constant" | "define", "name": "W", "file": ..., "line": ..., "from": "16", "to": "32"}`.
- `children` lists the grouped operations of a cause-grouped contributor, with the same fields as a contributor.
- `by_category` values are `typ` deltas.
- `verdict` is derived with the capped sign sigma of `DIFF.md` §7 and §9: `delta.ge.min .. delta.ge.max` may include 0 while the verdict is `INCREASE` or `DECREASE`.
- `diagnostics` lists, besides the cost-affecting ones, the `IGNORED_REGION` diagnostics of the "after" tree.
- Array lengths are not truncated in `json`; `--max-lines` does not apply (an explicit `--limit` may be given).

### 3.3 `cost` result

```json
{
  "top": "top_core",
  "total": {"ge": {"min": 27900, "typ": 28410, "max": 29100}},
  "confidence": {"level": "medium", "reasons": []},
  "modules": [
    {"module": "alu", "parameters": {"W": 32}, "instances": 1,
     "own_ge": {"min": 1200, "typ": 1400, "max": 1800},
     "total_ge": {"min": 1900, "typ": 2200, "max": 2800}}
  ],
  "by_category": {"seq": 7100, "addcmp": 3300, "mul": 4800, "select": 6100, "logic": 3200,
                  "shift": 900, "div": 0, "mem": 3010},
  "operations": []
}
```

`operations` is present only with `--detail ops` and lists every costed operation with the fields of the cost tree node (`SPEC.md` §5.5).

### 3.4 `estimate` result

```json
{
  "input": {"kind": "op", "text": "mul 32x32"},
  "normalized": "MUL 32x32 unsigned",
  "ge": {"min": 5543.37, "typ": 7901.752, "max": 10535.577},
  "architecture": {"min": "booth_wallace", "typ": "array", "max": "wallace_ks"},
  "breakdown": {"AND2": 1024, "FA": 960, "HA": 32, "CPA_ALPHA": 1},
  "confidence": {"level": "medium", "range_ratio": 0.316,
                 "reasons": ["delta range is -30%/+33% of the estimate"]}
}
```

### 3.5 Diagnostics

```json
{
  "code": "UNSUPPORTED_CONSTRUCT",
  "severity": "warning",
  "affects_cost": true,
  "side": "after",
  "file": "alu.sv", "line": 140, "column": 5,
  "message": "unbounded while loop"
}
```

`side` is `before`, `after`, or `both`. Codes are stable identifiers (`FRONTEND.md` §6, §10.6; `DIFF.md` §1 and §10). `DISCOVERY_DUPLICATES` (`warning`, `affects_cost`) is emitted first when implicit discovery mixes variants of a module (§2.2). For `cost`, `meta.diagnostics_out_of_cone` (integer, present when non-zero) counts the cost-neutral diagnostics dropped because their file is outside the cone of the top (§2.4).

### 3.6 Published schema

The repository ships `schema/gatecost-result.schema.json`, a JSON Schema (draft 2020-12) for the envelope and each `result`. The package includes a small validator (standard library only) used by the test suite and available as `gatecost validate-output <file>`.

## 4. The `text` and `markdown` Formats

- `text`: the `agent` content laid out as aligned columns with section titles, a legend for reasons, and, with `--verbose`, the per-module and per-category tables. Width is capped at 100 columns. It prints the confidence reasons (`Confidence <level>: ...`, also for `cost`), the `WARN` lines and the `NOTE` line, as `agent` does.
- `markdown`: a summary header (verdict, delta, range, confidence), a table of contributors (`Δ GE | location | reason | description`), the confidence reasons, the `WARN` lines (bold) and the `NOTE` line (italic), a collapsible details block for module and category breakdowns, and a footnote stating that the figures are estimates. For `cost` it wraps the `agent` text. It is intended for pull-request comments and never exceeds 60,000 characters.

## 5. Interpretation Guide for Agents

The shipped `SKILL.md` contains this guidance:

1. Quote the `typ` figure with its range, never the figure alone: "about +340 GE (between +280 and +410)".
2. State the confidence. When `confidence=low`, repeat the `WHY` line to the user and avoid precise-sounding conclusions.
3. When a `WARN` or `ASSUMED` line is present, mention it: it means part of the change was not costed or was costed under an assumption.
4. A `verdict=UNCERTAIN` means the sign of the change is not established; say so. An `INCREASE` or `DECREASE` with a range that crosses 0 is a firm sign with an uncertain size: say "increases, by about +6800 (between -1500 and +12600)" and repeat the confidence.
5. A `verdict=NO_CHANGE` means the edit does not alter the hardware as the tool models it; do not describe it as a saving.
6. Explain contributors in the user's terms (location and reason), not in tool terminology.
7. To compare design alternatives before writing code, use `estimate`, then `diff` after editing.
8. The figures are estimates for orderings and orders of magnitude, not synthesis results; recommend synthesis for sign-off decisions.

## 6. Errors

Errors are never mixed with partial results in `agent` format: a failed run prints exactly one `ERROR <code> <message>` line (and exits with the corresponding exit code). In `json` format the envelope has `status: "error"`. Codes:

| Code | Exit | Meaning |
|---|---|---|
| `NO_SOURCES` | 3 | No RTL files found. |
| `NO_BASELINE` | 2 | `diff` was given no baseline and none could be inferred. |
| `BAD_ARGUMENT` | 2 | Invalid command-line usage. |
| `BAD_CONFIG` | 2 | Invalid `gatecost.toml` or profile. |
| `TOP_NOT_FOUND` | 3 | The requested top module does not exist. |
| `INTERNAL` | 3 | Unexpected internal failure (the message includes a short report identifier; details on standard error). |
| `INTERNAL` (cache pack gone) | 3 | A cache pack vanished or was rewritten during the run (`gatecost clean`, or pruning by another process). The message names the pack and the remedy (`rerun with --no-cache`). |
| `BUDGET_EXCEEDED` | 1 | A `--budget` or `--fail-on-increase` threshold was crossed (a normal result is still printed first). |

## 7. Tests

- **Golden outputs:** for a corpus of before/after fixtures, the exact expected `agent`, `text`, `markdown`, and `json` outputs (byte comparison).
- **Grammar:** a reference parser for the `agent` grammar (§2.2) accepts every golden output and rejects malformed lines; fuzzed results (random trees) always produce output that parses.
- **Schema:** every `json` output validates against the shipped schema; the schema accepts the documented examples.
- **Budget:** the default `agent` output for a diff never exceeds 2,000 characters for a corpus including large hierarchies; truncation markers are present whenever data is dropped.
- **Consistency:** the `agent`, `text`, `markdown`, and `json` outputs of one run agree on the verdict, the total, the range, and the ordered contributor list.
- **ASCII:** `agent` output contains only printable ASCII.
- **Determinism:** two runs, with and without the cache and on different platforms, produce identical bytes (excluding `meta`).
- **Exit codes:** each row of §6 is exercised, including `BUDGET_EXCEEDED` printing a normal result.
