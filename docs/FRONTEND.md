# gatecost — Front-End Reference

Status: Draft. Normative companion to `SPEC.md` §5.1, §5.2 and §6.

The front-end turns RTL source files into the word-level IR consumed by the cost model. It is organized as one **language front-end per HDL** (Verilog/SystemVerilog, §2–§3, and VHDL, §10) feeding a single language-neutral IR; everything from the IR onward (cost model, differ, reporters) is shared. Principles (§1), unresolved information (§4), delta scope (§5), diagnostics (§6), limits (§7), and performance (§8) apply to both languages. It is designed for a **fast, approximate estimator**: coverage and speed take precedence over strict language conformance. When the front-end cannot be exact, it degrades gracefully, records an explicit assumption or diagnostic, and lets the confidence level (`COST_MODEL.md` §5.2) reflect it. It never aborts the whole analysis because of a local problem.

## 1. Principles

1. **Never fail globally.** A problem in one construct, module, or file produces a diagnostic and a partial result.
2. **Degradation ladder.** For every construct the front-end uses the first applicable level:
   1. *Exact*: the construct is understood and costed.
   2. *Approximated*: the construct is understood in part; a documented assumption is applied and recorded (for example, "unresolved parameter `W` assumed from its default value 32").
   3. *Skipped*: the construct is ignored with a diagnostic; its cost contribution is zero and confidence drops to `low` if it lies in the changed cone.
3. **Source fidelity.** Every token, AST node and IR operation keeps its original file and line, including code produced by macro expansion (attributed to the macro use site, with the macro definition as secondary location).
4. **Linear-time, demand-driven.** Work is proportional to the code that matters for the question asked. Unchanged, unreachable, or already-cached code is not re-analyzed.
5. **Bounded.** Every expansion (macro depth, generate iterations, loop unrolling, recursion, instance count) has a configurable limit. Reaching a limit is a diagnostic, not a crash or a hang.
6. **No execution.** Analyzed sources are only ever read as text. No `` `include`` outside the configured include directories, no `$system`, no shell expansion.

## 2. Verilog / SystemVerilog Pipeline

```
files ─► Reader ─► Preprocessor ─► Lexer ─► Parser ─► AST ─► Elaborator ─► IR
          │           │                        │                  │
       encoding   line map               error recovery       limits, assumptions
```

### 2.1 Reader

- Reads files as UTF-8 with a Latin-1 fallback; normalizes line endings; never raises on bad bytes.
- Resolves `` `include`` through, in order: the including file's directory, then the configured include directories.
- Computes a content hash per file (used by the cache, `SPEC.md` §12).

### 2.2 Preprocessor

Supported directives:

| Directive | Behavior |
|---|---|
| `` `define`` / `` `undef`` | Object-like and function-like macros, default arguments, multi-line bodies with `\` continuation, token pasting, stringification. |
| `` `ifdef`` / `` `ifndef`` / `` `elsif`` / `` `else`` / `` `endif`` | Full conditional compilation, nested. |
| `` `include`` | Resolved as above; cycles and a depth limit are detected. |
| `` `__FILE__`` / `` `__LINE__`` | Expanded. |
| `` `timescale``, `` `default_nettype``, `` `resetall``, `` `celldefine``, `` `pragma``, `` `line``, `` `begin_keywords`` | Recognized and ignored (`default_nettype none` is noted: it disables implicit-net creation). |
| Synthesis pragmas | Regions between `// synopsys translate_off` and `translate_on` (also `// pragma translate_off` and `// synthesis translate_off`) are excluded, as are branches guarded by a macro that is not defined. |

Defines come from the command line, the project file, and the sources. An undefined macro use produces a diagnostic and expands to nothing. `SYNTHESIS` is defined by default; `SIMULATION` and `FORMAL` are not. The preprocessor produces a **line map** from output positions back to original file and line; the lexer and everything downstream use it for source locations.

### 2.3 Lexer

- Implemented with a single compiled master regular expression iterated with `re`, never a per-character Python loop.
- Tokens: identifiers (including escaped identifiers), system identifiers (`$clog2`), keywords, operators (longest match), numbers, strings, attributes `(* ... *)` (skipped, except `full_case`, `parallel_case`, and `keep`, which are recorded).
- Number literals: sized and unsized, any base, signed (`'sd`), with `x`/`z`/`?` digits, underscores, and unsized fill literals (`'0`, `'1`, `'x`, `'z`). Real numbers are rejected in synthesizable contexts.
- Tokens are tuples or `__slots__` objects, with interned keyword strings.

### 2.4 Parser

- Hand-written recursive descent for declarations and statements; Pratt (precedence climbing) for expressions, following the SystemVerilog operator table.
- Recursion depth on expressions is bounded; long operator chains are handled iteratively.
- **Error recovery** by synchronization:
  - in an expression or statement: skip to the next `;`, `end`, `endcase`, `endmodule`, or the next statement keyword;
  - in a module item: skip to the next `;` or to the matching `end*` keyword;
  - in a module: skip to `endmodule`; the module is marked *incomplete*, and whatever was parsed before the error is retained.
- **Skipped regions.** The following are recognized by their opening keyword, skipped by balanced matching of their closing keyword, and reported at `info` level only: `initial`, `final`, `specify`, `class`, `program`, `covergroup`, `property`, `sequence`, `clocking`, `checker`, assertions (`assert`, `assume`, `cover`, `restrict`), non-synthesizable `task`/`function` bodies (containing delays, events, or `$display`), `$display`/`$error`-family system tasks, and `bind`.
- **Pure-Python AST:** `__slots__` classes, one class per node kind, each with a location. No generic dictionary-based nodes.

### 2.5 Elaborator

Demand-driven: starts at the top module (or at the modules of the changed files, see §5) and visits instances on demand.

1. **Symbol resolution.** Scopes for packages, modules, generate blocks, functions, and procedural blocks. `import pkg::*` and `pkg::name` are supported. Implicit nets follow `default_nettype`.
2. **Parameters.** `parameter`, `localparam`, type parameters (limited), and per-instance overrides (named, positional, `defparam` in a limited form). Constant expressions are evaluated with Python integers, with the standard operator set, `$clog2`, `$bits`, `$signed`, `$unsigned`, `$min`/`$max`, ternaries, and constant functions (interpreted, bounded by a step limit).
3. **Generate.** `for`, `if`, `case`; the iteration count is bounded. Named blocks produce hierarchical names (`gen[3].u_alu`).
4. **Types.** `typedef`, `enum`, packed `struct`/`union`, packed multidimensional arrays, `logic`/`bit`/`reg`/`wire`, signedness, and unpacked arrays (memories). Packed structs are flattened to vectors with field offsets.
5. **Widths and signedness.** The language's self-determined and context-determined sizing rules are implemented for operators, concatenation, replication, ternary, comparison, and assignment. Mismatches are implicitly extended or truncated.
6. **Procedural to dataflow.** `always_comb`, `always @(*)`, `always_ff`, `always @(posedge ...)`, `always_latch`, and continuous assignments are converted to IR by symbolic execution of the control flow:
   - `if`/`case`/`?:` become `MUX`/`PMUX` trees carrying the exclusive-branch tag (`COST_MODEL.md` §4.5);
   - blocking and non-blocking semantics are respected for ordering within a block;
   - default assignments and unassigned paths produce a feedback path (an inferred latch if combinational; a hold, with enable recognition, for a clocked block);
   - `for` loops with constant bounds are unrolled up to the limit; other loops are reported;
   - `casez`/`casex` don't-cares and `unique`/`priority`/`full_case`/`parallel_case` select PMUX versus PRIORITY.
7. **Hierarchy.** Instances are expanded lazily with parameter and constant-port propagation (`COST_MODEL.md` §4.7). Identical `(module, parameters, constant ports)` combinations are elaborated once and shared. Instance arrays are expanded up to the limit.
8. **Interfaces and modports.** Interfaces are flattened into their member signals at each port connection; modports only give direction. Virtual interfaces and interface arrays beyond simple cases are skipped with a diagnostic.
9. **Memories.** An unpacked array that is indexed and written in a clocked block is a memory; the access pattern determines read and write ports, and the read style (synchronous or asynchronous).

## 3. Verilog / SystemVerilog Support Matrix

| Area | Support | Notes |
|---|---|---|
| Modules, ports (ANSI and non-ANSI), parameters | Exact | |
| `wire`, `reg`, `logic`, `bit`, signed/unsigned | Exact | |
| Packed arrays and structs, `typedef`, `enum` | Exact | Enums are treated as their base type. |
| Unpacked arrays | Exact | Memories and register files. |
| Continuous assignments, `always*` blocks | Exact | |
| `if`, `case`, `casez`, `casex`, `unique`/`priority` | Exact | |
| `for` loops, `generate` (`for`/`if`/`case`) | Exact within limits | Beyond limits: approximated, flagged. |
| Functions and tasks (synthesizable) | Exact | Inlined at each call, bounded depth, no recursion. |
| Packages, `import`, `localparam` in packages | Exact | |
| `$clog2`, `$bits`, `$signed`, `$unsigned`, `$countones` | Exact | Constant when arguments are constants. |
| Streaming operators, `inside`, assignment patterns | Exact | Lowered to wiring, comparators, or constants. |
| Casting (`type'(x)`, `signed'(x)`) | Exact | |
| Interfaces, modports | Approximated | Flattened. |
| Parameterized types, `typedef` in parameters | Approximated | Common cases only. |
| Tri-state (`inout`, `z` drivers) | Approximated | Costed as a mux with enable; flagged. |
| `defparam` | Approximated | Simple hierarchical paths only. |
| Gate-level primitives (`and`, `nand`, `not`, `buf`, ...) | Exact | One primitive per gate instance. |
| UDPs | Skipped | Reported as black boxes. |
| `initial`, `final`, delays, `fork`, classes, DPI, assertions | Skipped | Ignored without effect on the estimate. |
| Hierarchical references into other modules | Skipped | Diagnostic. |

## 4. Unresolved Information

Real-world RTL is frequently incomplete from the tool's point of view. Defaults:

| Situation | Behavior |
|---|---|
| Module instantiated but not found in the sources | Black box: profile cost if listed, otherwise zero plus a `MISSING_MODULE` diagnostic. Confidence `low` when in the changed cone. |
| Parameter without a value | Use its declared default; if none, record `UNRESOLVED_PARAM` and assume width 32 (flagged). |
| Undefined macro | Expands to nothing, `UNDEFINED_MACRO` diagnostic. |
| Unresolvable width | Assume the width of the other operand if there is one, otherwise 32 (flagged). |
| Include file not found | Diagnostic; the include is skipped. |
| Several definitions of one module | Last definition in command-line order wins, with a `DUPLICATE_MODULE` diagnostic. |
| No unique top module (several roots, or a cycle) | Analyze each root separately and report each; `--top` selects one. |

## 5. Scope of Analysis for a Delta

For `diff`, the front-end avoids analyzing the whole design when it can:

1. Parse and hash every source file (parsed files are cached by content hash).
2. Determine the **changed modules**: those whose normalized AST hash differs between baseline and current. Whitespace, comments, and reordering of independent declarations do not count as a change.
3. Elaborate and cost only the changed modules, for each parameterization in which they are instantiated (found by walking the instance hierarchy from the top), plus the instance-count effects on their ancestors.
4. Reuse the cached cost of every unchanged module for each `(module, parameters, constant ports)` key. An entry is invalidated when the module's AST, the interface (ports and parameters) of any dependency, the defines, or the profile change.
5. If the hierarchy cannot be resolved cheaply (for example, a changed file defines the top), fall back to full elaboration.

## 6. Diagnostics

Each diagnostic has a stable code, a severity (`info`, `warning`, `error`), a location, and a one-line message. Diagnostics that can affect the figure are marked `affects_cost = true`; they are the only ones that appear in the compact `agent` output. Representative codes:

| Code | Severity | Affects cost |
|---|---|---|
| `PARSE_ERROR` | error | yes, if in the changed cone |
| `UNSUPPORTED_CONSTRUCT` | warning | yes |
| `MISSING_MODULE` | warning | yes |
| `UNRESOLVED_PARAM` | warning | yes |
| `UNDEFINED_MACRO` | warning | yes |
| `LIMIT_REACHED` (unroll, generate, depth) | warning | yes |
| `INFERRED_LATCH` | warning | no (costed) |
| `DUPLICATE_MODULE` | warning | possibly |
| `IGNORED_REGION` (initial, assertions, ...) | info | no |
| `INCLUDE_NOT_FOUND` | warning | possibly |

## 7. Default Limits

All configurable in `gatecost.toml`:

| Limit | Default |
|---|---|
| Macro expansion depth | 64 |
| Include depth | 32 |
| Generate iterations (per loop) | 4096 |
| Procedural loop unrolling (per loop) | 1024 |
| Function call inlining depth | 32 |
| Total elaborated instances | 100,000 |
| Expression nesting depth | 500 |
| Constant-function interpreter steps | 100,000 |

## 8. Performance Requirements

- Lexing and parsing of at least **1 MB of RTL per second** on a modern laptop in CPython.
- Peak memory proportional to the number of distinct `(module, parameters)` elaborations, not to the instance count.
- Cold analysis of a design of about 100k lines in a few seconds; warm analysis of a single-file change well under one second.
- Hot paths avoid per-character Python loops, repeated string slicing, and recursion over long lists.

**Measured status** (generated designs of `tests/bench/`, CPython 3.15 on an Apple M2; `python3 -m tests.bench.run_bench --lines 10000 100000 --diff`):

| | 10k lines | 100k lines | target |
|---|---|---|---|
| lex + parse | 0.10 s (SV), 0.12 s (VHDL) | 1.0 s (SV, 3 MB/s), 1.0 s (VHDL, 3.5 MB/s) | at least 1 MB/s: met |
| elaboration + IR analysis (cold) | 0.65 s (SV), 0.53 s (VHDL) | 5.8 s (SV, 66k IR nodes), 5.2 s (VHDL, 44k nodes) | a few seconds: **not met** |
| cost tree | 0.12 s | 1.2 s (SV), 0.8 s (VHDL) | |
| IR build + analysis of one synthetic module of 100k requested nodes (75k built, 54k live) | | 0.9 s + 1.7 s | well under 1 s (`IR.md` section 11): **not met** |
| `cost` end to end, cold cache | 0.9 s | 9.8 s (SV), 8.2 s (VHDL) | a few seconds: **not met** |
| `diff` with a warm cache, nothing changed | 0.08 s | 0.23 s (SV), 0.17 s (VHDL) | < 0.3 s: met |
| `diff` after a one-file edit, baseline cached | 0.26 s (SV), 0.38 s (VHDL) | 0.7 s (SV), 2.2 s (VHDL) | < 0.3 s at 100k lines: **not met** |
| the same edit again (cached) | 0.11 s | 0.29 s (SV), 0.22 s (VHDL) | < 0.3 s: met |

Where the time goes at 100k lines (SV, cold): elaboration and IR construction 4.0 s, IR analysis 1.9 s, cost tree 1.2 s,
parsing 1.0 s, provenance annotation 1.0 s, cache write 0.6 s.  The profile is flat (no function above 5 %), so further
gains need a lower per-node cost across the elaborator and the IR passes rather than a single fix.  The one-file edit
reads only the changed files, the files that must always be read (macros, packages, interfaces, compilation-unit items)
and the headers of the instances of the changed modules' ancestors (a per-file index stored with the baseline analysis
supplies everything else, `SPEC.md` §12); for the generated 100k-line SV project (one top with 250 children) that
is 253 of 351 files.  VHDL projects are always read completely.

## 9. Tests

- **Lexer and preprocessor:** token streams for every literal form; macro edge cases (nested, recursion guard, arguments containing commas inside parentheses); conditional compilation; include resolution; line-map correctness (a macro-expanded operation maps to the right source line).
- **Parser:** a fixture per construct in §3; error-recovery fixtures verifying that a syntax error in one module, statement, or expression leaves neighbouring code analyzed and yields exactly the expected diagnostics.
- **Elaborator:** parameter propagation; nested generate; width and signedness rules (a table of expression → expected width and sign); case/if → mux structures; latch inference; enable recognition; memory port inference; each limit in §7 being reached cleanly.
- **Robustness:** a seeded fuzzing test that mutates fixtures (deleting, swapping, and duplicating tokens, truncating files) and asserts that the front-end terminates within limits, raises no uncaught exception, and returns diagnostics.
- **Presentation invariance:** reformatting, renaming locals, and reordering independent items leave the IR cost unchanged.
- **Incremental:** the delta computed with the cache equals the delta computed without it, over a corpus of edit scripts.
- **Real-world smoke tests (optional, maintainers):** parse and elaborate permissively licensed open-source cores to detect crashes and to track the share of constructs hitting the *skipped* level. The sources are fetched by the test harness, never vendored.

## 10. VHDL Front-End

The VHDL front-end follows the same principles as §1 and produces the same IR as the Verilog/SystemVerilog front-end. Files with extensions `.vhd` and `.vhdl` are routed to it. Supported standard: VHDL-93 and VHDL-2008 synthesizable subsets (including VHDL-2008 conveniences such as `process(all)`, conditional and selected assignments inside processes, `generate` with `case`, and `numeric_std_unsigned`).

### 10.1 Pipeline

```
files ─► Reader ─► Lexer ─► Parser ─► AST ─► Elaborator ─► IR
                              │                  │
                       error recovery      built-in IEEE model, type-directed resolution
```

There is no preprocessor stage. Synthesis pragma regions (`-- synopsys translate_off` / `translate_on`, `-- pragma translate_off`, `-- synthesis translate_off`) are excluded by the lexer.

### 10.2 Lexer and parser

- Case-insensitive: identifiers and keywords are normalized to lower case. Extended identifiers (`\Foo\`) are case-sensitive and kept verbatim.
- Literals: decimal and based integers (`16#FF#`), real numbers, character literals (`'0'`, `'1'`, `'Z'`, `'-'`), string literals, bit-string literals (`x"FF"`, `b"1010"`, `d"10"`, `o"7"`, and the 2008 sized forms such as `8x"FF"`, `12UB"..."`).
- Comments (`--`, and `/* */` in 2008) are skipped.
- Same parsing technique as §2.4: recursive descent for declarations and statements, Pratt for expressions, bounded depth.
- **Error recovery** synchronizes on `;`, `end` followed by the matching construct keyword (`end process`, `end architecture`, `end entity`, `end if`, `end case`, `end loop`, `end generate`), and design-unit keywords (`entity`, `architecture`, `package`, `configuration`). A broken architecture keeps its entity and the items parsed before the error.
- **Skipped regions** reported at `info` level only: `assert`/`report` statements, `wait for` delays, `after` clauses (ignored, the assignment is kept), file declarations and `textio` use, access types (`new`, `deallocate`), protected types, `shared variable` of protected type, PSL, `attribute` specifications not related to synthesis, and testbench-style processes without sensitivity (`wait`-driven). Simulation-only processes are detected by the presence of `wait for`, `report`, or file I/O in a process with no clock.

### 10.3 Design units and library handling

- Design units: `library`/`use` clauses, `entity`, `architecture`, `package`, `package body`, `configuration` (ignored: default binding by name), `context` (2008, limited).
- **Multiple architectures** for one entity: the last one compiled in file order is selected unless a `configuration` declaration or the project file names one; the others are reported at `info` level.
- **Libraries.** `work` is the default library. The project file may map file groups to named libraries. Units are resolved **lazily by name**, so compile order is not required. Absence of a package or library file is handled as in §4 (`MISSING_UNIT`).
- **Built-in IEEE model.** The front-end ships a built-in model of the standard packages, and does not need their sources:
  - `std.standard`, `ieee.std_logic_1164`, `ieee.numeric_std`, `ieee.numeric_bit`, `ieee.numeric_std_unsigned`, `ieee.math_real` and `ieee.math_complex` (constants and constant functions only);
  - `ieee.fixed_pkg` / `ieee_proposed.fixed_pkg` (`ufixed`, `sfixed`, `to_ufixed`/`to_sfixed`, `resize`, arithmetic with the package result ranges, see §10.5) and `ieee.float_pkg` (`float16`/`float32`/`float64`/`float128`, `float(e downto -f)`; approximate arithmetic), plus `ieee.fixed_float_types` (`fixed_wrap`, `fixed_saturate`, `fixed_round`, `fixed_truncate`);
  - the legacy `std_logic_arith`, `std_logic_unsigned`, `std_logic_signed` packages, with their well-known signedness conventions for `std_logic_vector` arithmetic;
  - `std.textio` and `std.env` are recognized and ignored.
- **Component and entity instantiation.** Direct entity instantiation (`entity work.foo(rtl)`) and component instantiation are both resolved to the entity of the same name in the active libraries; a component without a matching entity is a black box.
- **Mixed-language designs.** A VHDL instance may target a Verilog/SystemVerilog module and vice versa. Cross-language lookup tries the exact name, then the case-insensitive match. Generics and parameters are mapped by name; port types are mapped to bit vectors by width.

### 10.4 Elaboration

1. **Generics and constants.** `generic`, `constant` (including deferred constants in packages), `generic map` overrides (named and positional), and constant expressions evaluated with Python integers: arithmetic, `**`, `mod`, `rem`, `abs`, relational operators, boolean operators, attributes (`'length`, `'high`, `'low`, `'left`, `'right`, `'range`, `'reverse_range`, `'pos`, `'val`, `'succ`, `'pred`), `log2`/`ceil` from `math_real`, and pure functions of constants (interpreted, bounded by a step limit).
2. **Types.**
   - Scalar: `bit`, `boolean`, `std_logic`/`std_ulogic` (1 bit; `'U'`, `'X'`, `'-'`, `'W'`, `'L'`, `'H'` are treated as don't-care; `'Z'` as tri-state, see below), `integer`/`natural`/`positive` and user-defined ranges (width is the minimum number of bits for the declared range, signed when the range includes negative values; an unconstrained `integer` is 32 bits), `character`.
   - Enumerations: width is `⌈log2 N⌉` (binary encoding), unless an `enum_encoding` attribute or the profile's `fsm_encoding` policy (`binary` | `one_hot`) says otherwise; an enumeration used as a state register is detected from its usage.
   - Arrays: constrained and unconstrained (constrained by association), `std_logic_vector`, `unsigned`, `signed`, `bit_vector`, multidimensional and arrays of vectors; index ranges are either `to` or `downto`.
   - Records: flattened into their fields; record ports are flattened at the boundary.
   - Subtypes and aliases are resolved transparently.
3. **Operators and conversions (type-directed).** Overload resolution is performed with a lightweight type checker sufficient for the numeric_std-style API:
   - `+`, `-`, `*`, `/`, `mod`, `rem`, `**`, `abs`, unary `-` on `signed`, `unsigned`, `integer`, and mixed forms (`unsigned + natural`);
   - relational, logical (`and`, `or`, `nand`, `nor`, `xor`, `xnor`, `not`), shift (`sll`, `srl`, `sla`, `sra`, `rol`, `ror`, `shift_left`, `shift_right`, `rotate_left`, `rotate_right`), reduction (2008 unary `and`/`or`/`xor`), concatenation `&`, and 2008 matching operators `?=` and family;
   - conversions and resizing: `to_integer`, `to_unsigned`, `to_signed`, `resize`, `std_logic_vector(x)`, `unsigned(x)`, `signed(x)`, `conv_integer`, `conv_std_logic_vector`, `to_stdlogicvector`: wiring or sign/zero extension at no cost, and an `integer` ↔ vector conversion costs nothing beyond the extension;
   - aggregates (`(others => '0')`, positional and named associations), slices, indexing with constant or variable index (variable index → `MUX`/`DECODE`).
   - `numeric_std` result widths follow the package's rules: for example, `unsigned + unsigned` has the width of the wider operand, `unsigned * unsigned` the sum of widths, `unsigned + integer` the width of the vector. These rules are tabulated in a width-rule test table.
4. **Concurrent statements.** Simple, conditional (`when ... else`), and selected (`with ... select`, including `?` matching) signal assignments; concurrent procedure calls and assertions (ignored); component/entity instantiation; `generate` (`for`, `if`, `case`, with `elsif`/`else` branches in 2008), with bounded iteration counts; blocks (flattened).
5. **Processes.** Processes are converted to IR by the same symbolic execution as in §2.5.6:
   - **Variables** have immediate (blocking-like) semantics; **signals** have deferred semantics, read as their pre-process value.
   - Clocked-process recognition: `rising_edge(clk)`, `falling_edge(clk)`, `clk'event and clk = '1'`, `wait until rising_edge(clk)`, with synchronous reset (inside the clocked branch) and asynchronous reset/set (the first branch of an `if rst = '1' then ... elsif rising_edge(clk)` chain) and clock enable (an `if en = '1'` inside the clocked branch → enable recognition).
   - Combinational processes (sensitivity list or `process(all)`) produce `MUX`/`PMUX` trees; signals not assigned on every path are inferred latches (`INFERRED_LATCH`).
   - `if`/`elsif`/`else` chains produce `PRIORITY` structures; `case` produces `PMUX`; `case?` and `case` over `std_logic_vector` with `'-'` follow don't-care handling.
   - `for ... loop` with constant bounds is unrolled up to the limit; `while` and `loop ... exit` with non-constant bounds are reported (`UNSUPPORTED_CONSTRUCT`), `next`/`exit` under constant conditions are handled.
   - Subprograms: pure functions and procedures are inlined at each call, bounded depth, no recursion unless the recursion depth is constant-evaluable; `out`/`inout` parameters are handled by copy-in/copy-out.
   - Resolution functions and driver conflicts: a signal driven by several concurrent sources is reported (`MULTIPLE_DRIVERS`) and the first driver is costed.
6. **Tri-state.** `'Z'` assignments are costed as in the Verilog front-end (mux with enable, flagged).
7. **Memories.** Signals of array-of-vector type that are indexed with a variable address and written in a clocked process are memories (same inference as §2.5.9); constants of array type with an indexed read are ROMs.
8. **Hierarchy.** As in §2.5.7: lazy instance expansion, `(entity, architecture, generics, constant ports)` keyed sharing, constant-port propagation.

### 10.5 Support matrix

| Area | Support | Notes |
|---|---|---|
| Entities, architectures, generics, ports | Exact | |
| `std_logic`, `std_logic_vector`, `unsigned`, `signed`, `integer`, `boolean`, `bit` | Exact | |
| `numeric_std` and `numeric_std_unsigned` operators and conversions | Exact | Built-in model. |
| `std_logic_arith`, `std_logic_unsigned`, `std_logic_signed` | Exact | Legacy conventions. |
| `ieee.fixed_pkg` (`ufixed`, `sfixed`) | Exact | Array `high downto low` (negative `low`). Result ranges as in the package: `+ -` `max(high)+1 downto min(low)`; `*` `ha+hb+1 downto la+lb`; `/` `ha-lb downto la-hb-1` (signed `ha-lb+1 downto la-hb`); `rem`, `mod`, `abs`, unary `-`, comparisons, `minimum`/`maximum`, shifts, `scalb`, `reciprocal`/`divide`/`remainder`/`modulo`, `to_slv`/`to_sulv`, `to_integer`/`to_unsigned`/`to_signed`, `to_real` on constants, `find_leftmost`/`find_rightmost`. Real and integer operands are converted to the other operand's range. `resize` and `to_ufixed`/`to_sfixed` honour `fixed_wrap`/`fixed_saturate` (overflow detector + clamp mux) and `fixed_round`/`fixed_truncate` (round incrementer or plain wiring); defaults are the package's (saturate, round). Constants fold exactly. |
| `ieee.float_pkg` | Approximated | Types and widths exact (`float16/32/64/128`, `float(e downto -f)`), constants and conversions to/from `real` exact. `+ - * /`, compares, `to_float` from integer/fixed, float to integer, `resize` between formats are an approximate gate-level model (alignment/normalising shifters, mantissa adder/multiplier/divider, rounding incrementers); zero, infinity, NaN and denormal special cases are not modelled. The run records an assumption. |
| Enumerations and FSM state encoding | Exact | Binary by default; one-hot by policy. |
| Records, constrained and unconstrained arrays, subtypes, aliases | Exact | Records flattened. |
| Concurrent and sequential assignments, `when`/`select` | Exact | |
| Processes (clocked, combinational, with sync/async reset) | Exact | |
| `generate` (`for`, `if`, `case`) | Exact within limits | |
| Functions and procedures | Exact | Inlined. |
| Packages and package bodies | Exact | |
| Attributes (`'length`, `'range`, `'high`, `'low`, `'event`, ...) | Exact | |
| `math_real` constants and functions | Exact | Constant evaluation only. |
| Configurations | Approximated | Default binding; named architecture via project file. |
| Generic types, generic subprograms, generic packages (2008) | Approximated | Common cases only. |
| Tri-state (`'Z'`) | Approximated | Mux with enable; flagged. |
| `wait`-based synthesizable styles | Approximated | Only `wait until rising_edge(clk)`. |
| `assert`, `report`, `after`, `wait for`, file I/O, access and protected types, PSL | Skipped | No effect on the estimate. |
| External names (2008 `<<signal ...>>`) | Skipped | Parsed in every position (expression, assignment target, alias); one `UNSUPPORTED_CONSTRUCT` per name: the value is unknown (X) of the declared type, assignments are ignored. |
| Mode views (VHDL-2019 `view ... of`, `port (p : view v [of T])`, `v'converse`) | Exact for record ports | Each leaf of the flattened record port takes its direction from the view; array-of-record view ports take their direction from the use. |
| Generic subprograms and packages in `generic (...)` | Skipped | One `UNSUPPORTED_CONSTRUCT` per generic function/procedure/package; the following generics and the ports are unaffected. Legacy `generic (T : type)` is a generic type. |
| `.v`/`.sv` file that contains VHDL | Parsed as VHDL | Detected from the first non-comment token (`library`, `use`, `entity`, `architecture`, `package ... is`, `context`, `configuration`); one `LANGUAGE_MISMATCH` warning (does not affect cost). `//` line comments are accepted by the VHDL lexer for such files. |
| Verilog-style user-defined primitives | Not applicable | |

### 10.6 Language-specific diagnostics

In addition to §6:

| Code | Severity | Affects cost |
|---|---|---|
| `MISSING_UNIT` (package, library, or entity not found) | warning | yes |
| `AMBIGUOUS_ARCHITECTURE` (several architectures, none selected explicitly) | warning | possibly |
| `LANGUAGE_MISMATCH` (a Verilog-extension file that holds VHDL, parsed as VHDL) | warning | no |
| `MULTIPLE_DRIVERS` | warning | yes |
| `UNRESOLVED_OVERLOAD` (operator or function cannot be resolved with the lightweight type checker) | warning | yes |
| `SIMULATION_ONLY_PROCESS` | info | no |

### 10.7 Tests

- Lexer: case folding, extended identifiers, every literal form (based, bit-string with each base and size prefix), pragma regions.
- Parser: a fixture per construct in §10.5 and error-recovery fixtures per construct keyword.
- Elaborator: a width-rule table for `numeric_std` operators and conversions (expression → expected width and signedness); clocked-process recognition fixtures (rising_edge, `'event` style, sync/async reset, enable); latch inference; enumeration encoding; `generate` forms; overload resolution including mixed `unsigned`/`integer` forms.
- **Cross-language equivalence:** a corpus of paired VHDL and Verilog/SystemVerilog descriptions of the same circuits (counter, FIFO control, ALU, FSM, register file, adder tree, multiplier). Each pair must yield the same IR operation multiset and the same cost within a tight tolerance. This keeps the two front-ends consistent.
- Mixed-language instantiation fixtures in both directions.
- Robustness: the fuzzing procedure of §9 applied to VHDL fixtures.
