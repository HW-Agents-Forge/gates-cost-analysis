# gatecost — Intermediate Representation Reference

Status: Draft. Normative companion to `SPEC.md` §5.3. It is the contract between the front-ends (`FRONTEND.md`), the cost model (`COST_MODEL.md`), and the differ (`DIFF.md`).

The IR is a **word-level netlist with bit-level facts**. It is language-neutral: Verilog/SystemVerilog and VHDL produce the same structures. It is deliberately close to the way a synthesis front-end sees a design before technology mapping, and deliberately far from a gate-level netlist: operations stay word-level (an adder is one node), while per-bit knowledge (constants, demanded bits) is tracked on the nets.

## 1. Structure

```
Design
 └─ Module IR  (one per (module, parameter set, constant-port set), shared by all instances)
     ├─ ports
     ├─ nets
     ├─ nodes (operations)
     ├─ instances of child Module IRs or black boxes
     └─ diagnostics, assumptions
```

- **Design:** the elaborated top plus a table of Module IRs keyed by `(module name, canonical parameter set, constant-port signature)`; and instance counts per key (`FRONTEND.md` §2.5.7).
- **Module IR:** immutable once built (after the analysis passes of §6, it is only read).
- Everything is identified by small integers local to the Module IR; names are kept only as hints for reporting.

## 2. Nets and Signal Specifications

A **net** is a vector of `width` bits (`width ≥ 1`) with:

| Field | Meaning |
|---|---|
| `id` | Integer, unique in the Module IR. |
| `width` | Declared width. |
| `signed` | Declared signedness (affects extension and comparisons at the *consumers*; storage is bit patterns). |
| `name` | Optional hint (source identifier), used for the driven-target key and for reports. |
| `driver` | The node, port, or instance output that drives it (exactly one; multiple drivers are a diagnostic, `MULTIPLE_DRIVERS`). |
| `known` | Per-bit known state: a pair of integers `(kmask, kval)`; bit `i` is a known constant when `kmask` bit `i` is 1, with value `kval` bit `i`. Filled by the forward pass (§6.1). |
| `demand` | Per-bit demanded state: an integer mask; bit `i` is 1 when some consumer uses bit `i`. Filled by the backward pass (§6.2). |
| `fanout` | Number of consumer references (including register D inputs, outputs, memory and instance ports). |

**Signal specifications (`SigSpec`).** Operands of nodes are not nets but *signal specifications*: ordered lists of chunks, least-significant first. A chunk is either `(net, lo, width)`, a slice of a net, or `Const(value, width, xmask)`, where `xmask` marks don't-care bits (`x`, `-`, `?`). Wiring operations of the source languages (slice, concatenation, replication, bit-selects with constant index, zero- and sign-extension, packed-struct field selection, type conversions) are expressed purely by constructing `SigSpec`s and therefore **never produce nodes and never cost anything**. This is what makes bit-level constant propagation straightforward.

Extensions are explicit: sign extension replicates the MSB chunk; zero extension appends `Const(0)`. A `SigSpec` knows when its upper bits are sign copies (`sign_rep`), which the demanded-bits pass uses to avoid costing extension bits (`COST_MODEL.md` §4.2).

## 3. Nodes

A **node** has: `id`, `op`, `inputs` (named `SigSpec`s), `outputs` (nets), `attrs`, `origin`, `tags`.

- `origin`: `(file, line, column)` of the construct; for macro-generated code, the use site plus the definition site (`FRONTEND.md` §1.3). Nodes created by lowering inherit the origin of the statement that caused them.
- `tags`: analysis annotations (`group`, `shared`, `fused`, `const_folded`, ...), added by the passes of §6; never part of identity.

### 3.1 Operation catalogue

`A`, `B` are operand `SigSpec`s; `Y` the output net; `S` a select. Result widths are those of the output net as determined by the front-end after applying the source language's sizing rules; the IR stores the widths and does not re-derive them. `sa`, `sb` attribute booleans give operand signedness.

| Op | Inputs | Attrs | Semantics |
|---|---|---|---|
| `ADD`, `SUB` | `A`, `B` | `sa`, `sb` | Arithmetic at the width of `Y` (operands extended according to `sa`, `sb`). |
| `NEG` | `A` | `sa` | Two's complement negation. |
| `MUL` | `A`, `B` | `sa`, `sb` | Product truncated or extended to `Y`. |
| `DIV`, `MOD` | `A`, `B` | `sa`, `sb` | Quotient or remainder. |
| `POW` | `A` | `exp` (constant) | Power with constant exponent. |
| `SHL`, `SHR`, `SAR`, `ROL`, `ROR` | `A`, `B` | `sa`, `sb` | Shift or rotate by amount `B`. |
| `AND`, `OR`, `XOR`, `XNOR` | `A`, `B` | — | Bitwise at the width of `Y`. |
| `NOT` | `A` | — | Bitwise complement. |
| `RED_AND`, `RED_OR`, `RED_XOR` (reductions) | `A` | — | 1-bit result. |
| `EQ`, `NE` | `A`, `B` | — | 1-bit. |
| `LT`, `LE`, `GT`, `GE` | `A`, `B` | `sa`, `sb` | 1-bit; signedness decided by `sa and sb`. |
| `MUX` | `A`, `B`, `S` | `group` | `Y = S ? B : A`; `S` is 1 bit. |
| `PMUX` | `A` (default), `B` (k inputs concatenated), `S` (k one-hot bits) | `group`, `k` | One-hot parallel mux. |
| `PRIORITY` | as `PMUX` | `group`, `k` | Priority-ordered (an `if`/`elsif` chain whose conditions are not known to be exclusive). |
| `DECODE` | `A` | — | One-hot decode of `A` (`2^width` outputs, or the number demanded). |
| `ENCODE` | `A` | — | Priority or one-hot encode. |
| `SUM` | `A1 ... An` (operands), optionally one `MUL` pair | `signs` | Fused datapath cluster produced by the fusion pass (§6.5); not produced by front-ends. |
| `DFF` | `D`, `CLK`, optional `EN`, `SRST`, `ARST` | `reset_kind`, `reset_value`, `clk_edge`, `enable_recognized`, `init` | State element. |
| `LATCH` | `D`, `EN`, optional `RST` | `polarity`, `inferred` | Level-sensitive element; `inferred = true` when created from an incomplete assignment. |
| `MEM` | read and write ports (address, data, enable) | `depth`, `width`, `rports`, `wports`, `read_style` | Memory array (array of `DFF` words inferred from indexed signals). |
| `ROM` | `A` (address) | `contents` (list of integers), `depth`, `width` | Constant lookup table. |
| `INST` | port connections | `module_key` | Instance of a Module IR; the child is costed through the hierarchy. |
| `BLACKBOX` | port connections | `name` | Instance of an unknown or library unit; cost from the profile. |

Tri-state drivers (`'Z'`, `inout`) are represented by a `MUX` with an enable select and an attribute `tristate = true`, so that the cost model can flag them (`FRONTEND.md` §10.4.6).

## 4. Builder and Local Simplification

All nodes are created through a **builder**. The builder enforces, at construction time and in linear time:

1. **Structural hashing (hash-consing).** Before creating a node, the builder looks up `(op, attrs, canonical inputs)`. If a node exists, its output is reused. Commutative operations (`ADD`, `MUL`, `AND`, `OR`, `XOR`, `XNOR`, `EQ`, `NE`) sort their operands into a canonical order (by a deterministic hash of the `SigSpec`) before lookup. Registers hash on all inputs and attributes, so identical flops are merged (retained for the `max` scenario by a `merge_count` attribute, `COST_MODEL.md` §4.3).
2. **Constant folding.** Any operation whose inputs are all constants is evaluated with Python integers, and its output is a `Const`.
3. **Identity and absorption rules** (local, at most one level deep, with the single exception of the masked merge below):

   | Pattern | Result |
   |---|---|
   | `x + 0`, `x - 0`, `x \| 0`, `x ^ 0`, `x << 0` | `x` |
   | `x & 0`, `x * 0` | `0` |
   | `x & 1…1`, `x * 1` | `x` |
   | `x ^ x`, `x - x` | `0` |
   | `x & x`, `x \| x` | `x` |
   | `~~x` | `x` |
   | `MUX(S const)` | the selected input |
   | `MUX(S, a, a)` | `a` |
   | `MUX(S, 0, 1)` (1-bit) | `S` |
   | `MUX(S, c0, c1)` with constant data | logic on `S` (AND/OR/NOT/wire) |
   | `x == x` | `1` |
   | `x * 2^k` | `x << k` as wiring |
   | `x / 2^k`, `x % 2^k` (unsigned) | wiring |

   **Masked merge (the one rule that looks two levels deep).** `OR(AND(x, NOT m), AND(y, m))` (all 8 operand orders) and `XOR(x, AND(XOR(x, y), m))` are, bit for bit, `m[i] ? y[i] : x[i]`. The builder rewrites them to `MUX` nodes, one per run of bits that share the same select bit:

   - a byte mask `{8{be[3]}, 8{be[2]}, 8{be[1]}, 8{be[0]}}` gives four 8-bit `MUX` nodes selected by `be[3]` to `be[0]`; a free mask gives 1-bit `MUX` nodes;
   - the bits of `m` are followed through the wiring already recorded (`Conn`), at most 8 hops, and nets driven by more than 4096 pieces are not followed (this only affects the grouping, never the function);
   - there is no rewrite when a named result would need several `MUX` nodes (a named result stays one net);
   - the unused `AND`/`NOT`/`XOR` nodes are swept at finalization.

   The rewrite is exact: nothing is assumed about `m`. It changes the tree structure: the pattern is recognised on the two levels already built, and the new `MUX` nodes replace them. This is the documented exception to "at most one level deep" and to §4.5.

   These rules must be applied identically in both baseline and current analyses, otherwise they would create spurious deltas; they are therefore part of the specification and covered by golden tests.
4. **Don't-care handling.** `x`/`z` constant bits (`xmask`) are kept in the `Const`; folding rules that may choose either value pick the one that simplifies. A constant that is entirely `x` is replaced by a wire to any existing equivalent when available (`COST_MODEL.md` §8, item 4).
5. **No global rewriting.** The builder never restructures expression trees (no re-association, no factoring, no Boolean minimization), except for the masked merge of §4.3, which restructures the two levels it matches and nothing else.

## 5. Procedural Lowering Interface

The front-ends convert procedural code to nodes with a shared **symbolic executor** (`FRONTEND.md` §2.5.6). The IR provides it with:

- an **environment**: for each assigned signal, the current value as a `SigSpec` (blocking assignments update it immediately; for non-blocking assignments, reads see the value from before the block);
- **branch merging**: after an `if`/`case`, differing environments are merged with `MUX`/`PMUX`/`PRIORITY` nodes, each tagged with a fresh `group` identifier that is the same for all nodes created from the same source statement. Branches with the same value need no node;
- **finalization** of a block:
  - combinational block: each assigned signal's final value is connected to its net; a path that does not assign the signal creates a feedback to the signal itself, which becomes a `LATCH` (`inferred = true`, diagnostic `INFERRED_LATCH`);
  - clocked block: each assigned signal becomes a `DFF` whose `D` is the final value. The finalizer then recognizes patterns, in order: asynchronous reset or set (from the sensitivity list or the leading branch of a VHDL `if ... elsif rising_edge`), synchronous reset (the top-level branch assigning a constant), then clock enable (`D = MUX(en, q, x)` with `q` the register's own output becomes `EN = en`, `D = x`, `enable_recognized = true`). The recognized `MUX` nodes are not created, so the enable is never double-counted. A register written `q <= (q & ~m) | (d & m)` goes through the masked merge (§4.3) and so gets one enable per segment of `m`, as if it were written `if (m_k) q[seg] <= d[seg]`. Limit: the rewrite uses the wiring recorded when the expression is built, so if the mask is assigned after the `always` block in the source, its bits are not yet resolved when the register is built, and the grouping into segments depends on the source order.

## 6. Analysis Passes

After a Module IR is built, the following passes run in this order. Each is linear in the number of nodes and nets and annotates the IR; none changes the structure except where stated.

1. **Known-bits propagation (forward).** Computes `known` for every net from `Const` chunks through each operation's bit-level transfer rules (for example, `AND` with a known 0 bit; `ADD` of known low bits; `MUX` with a known select; shifts by a constant; comparison of known operands). The result is exact for bitwise, shift, extension, and mux operations and conservative for arithmetic (carry propagation is not modeled beyond the low-bit run of known zeros).
2. **Dead-logic elimination (structure change).** Reverse reachability from module outputs (only the bits in `ModuleIR.port_demand` when the module is a context variant, §8), instance and black-box input ports, memory write ports, and registers that themselves reach an output. An `INST` node is a root only if it is not `prunable` (§8); a prunable instance is live only when one of its outputs is demanded, and then all its inputs are demanded. Unreached nodes are removed; their cost is zero. A register whose `D` is a constant equal to its reset value and that has no other driver is replaced by that constant.
3. **Demanded-bits (backward).** Starting from outputs (all bits demanded, or the bits of `port_demand` for a context variant), register `D`/`EN` inputs, and instance ports, computes `demand` for every net using each operation's rule (for example, `ADD` demands bits `0..k` of its operands when only bits `0..k` of the result are demanded; a comparison demands all bits of both operands; a slice demands only the sliced bits).
4. **Effective widths.** For each node, computes the post-folding widths used by the cost model: strip known-zero and sign-replicated upper bits, strip trailing known zeros for `MUL`, and clamp to the demanded range (`COST_MODEL.md` §4.2). The declared widths are kept alongside.
5. **Datapath fusion.** Finds chains of `ADD`/`SUB`/`NEG`/`MUL` where each intermediate result has `fanout = 1` and is consumed only by the next member; replaces each chain with one `SUM` node recording its members (and their original origins). The original nodes remain as `members` for attribution; the cost lives on the `SUM`.
6. **Resource sharing.** For each `group` of `MUX`/`PMUX` nodes, finds leaves that are same-class arithmetic operations (`MUL`, `DIV`, `ADD`/`SUB` with `width ≥ 8`) whose select conditions are mutually exclusive by construction (distinct branches of one statement). Marks the shared candidates (`shared = group id`); the cost model decides whether sharing is cheaper (`COST_MODEL.md` §4.5). Across `always` blocks and modules no sharing is marked.
7. **Select trees (annotation only, `ir/muxtree.py`).** Runs after resource sharing and before the keys. Nested `if`/`case` produce trees of mux-family nodes that each repeat the hold value, `c1 ? (c2 ? a : h) : h`; the function is `(c1 & c2) ? a : h`. The pass finds these trees and records them in `ModuleIR.mux_trees`; the cost model reads the records. It changes no structure, attribute, tag, key or hash: a design without such trees is analysed exactly as before.
   - An **internal node** is a mux-family node (`MUX`, `PMUX`, `PRIORITY`, not tri-state) whose output has fanout 1 and whose single consumer reference is a **data** input (`A` or `B`, inside one arm) of another mux-family node. Copies of a net through `Conn` are followed. A select, a port or a shared node is never internal. A **root** is a non-internal mux-family node with at least one internal node below it.
   - `_flatten` goes bit by bit over the demanded, not-known bits of the root. Each bit is the leaf reached by the unique path whose conditions hold. Leaves are distinct net bits (resolved through wiring), known constants 0/1, or don't-care bits (dropped). The position of a leaf is the structural path of arm indices from the root (`MUX`: 0 = `A`, 1 = `B`; `PMUX`/`PRIORITY`: case index, or the default), never a node or net id.
   - A record holds `root` (node id), `nodes` (absorbed internal ids), `bits` (a histogram `[V, c0, c1, dflt, count]` of root bits with at least two distinct leaves: `V` variable leaves, `c0`/`c1` whether the constants are leaves, `dflt` the kind of the default leaf), `gates` (`[id, AND2, OR2]`: the 1-bit gates that build the conditions, shared by all bits and by trees testing the same conditions) and `sel_and`/`sel_or` (per-tree totals before sharing). `export.py` exports `mux_trees`.
   - Assumption: the selects of a `PMUX` are one-hot, as everywhere in the IR.
8. **Keys.** Computes, for every costed node, the *named key* and the *shape key* used by the differ (§7).

The hierarchical analysis of §8 runs these passes module by module, parents first. The passes are pure functions of the Module IR; the same input always yields the same annotations.

## 7. Keys and Canonical Forms

The differ (`DIFF.md` §2.2) and the cache require deterministic identities.

- **Hash function:** `hashlib.blake2b` with a 16-byte digest over a canonical serialization (length-prefixed fields; integers in big-endian fixed width). Python's built-in `hash` is never used for anything persisted or order-relevant.
- **Named key** of a node: `(module name, driven-target name, role path, op)`. The driven target is the name of the register, output port, or named net at the end of the expression tree containing the node (the nearest named consumer). The role path is the sequence of operand positions from that net to the node (`lhs`, `rhs`, `then`, `else`, `sel`, ...) with commutative operands in canonical order.
- **Shape key** of a node: a Merkle hash `H(op, attrs, H(input_1), ..., H(input_n))` where the hash of an input is, for a constant, its value and width; for a port or register output, its name and width; for any other net, the hash of its driving node (so internal wire names never enter). `SigSpec` chunks contribute `(source hash, lo, width)`.
- **Cost of the named-key pass.** `_named_keys` is linear in the number of inputs of a node: the name-to-signal map of a consumer node is built once per node, not once per consumer edge. (It used to be quadratic on a node with thousands of inputs, such as a wide `MEM`; a flat module of 8,000 instances went from 6.8 s to about 1.2 s.)
- **Module canonical hash:** the hash of the sorted multiset of all shape keys of costed nodes and instances, plus the parameter set and the port list. It excludes locations, comments, names of internal nets, and declaration order, and is the quantity compared by the differ's unchanged short-circuit (`DIFF.md` §3.1) and used as part of the cost-cache key.

## 8. Hierarchy and Instances

- An `INST` node refers to a Module IR key. Constant-port propagation (`COST_MODEL.md` §4.7) is applied at instantiation: a port tied to a constant produces a *uniquified key* whose `known` information for that port is set before the child's passes run; the resulting Module IR therefore has its own folded structure.
- The design's instance count for a key is the number of `INST` nodes referring to it, multiplied along the hierarchy (arrays of instances multiply by their size).
- Black boxes carry only a name, its ports, and the profile cost; they have no internals.
- **Hierarchical observability (`ir/hier.py`, `analyze_hierarchy`).** Analysed alone, every module has all its outputs as roots, so a child whose outputs reach no output of the top would keep its full cost. `Design.analyze` therefore analyses the hierarchy top-down, parents first, and carries the *observed* bits of every instance output down to the child:
  - An `INST` whose child (transitively) has no effect other than its outputs (no black box, no instance of a module absent from the design, no recursion, no `inout` port, not replaced by a black box) is tagged `prunable`. It is live only when one of its outputs is demanded, so a dead instance disappears together with the parent logic that only feeds it.
  - A live instance whose outputs are only partly demanded instantiates a **context variant** of the child, keyed `key{obs:port=mask,...}` (`context_key`): a copy whose output roots are restricted to the demanded bits (`ModuleIR.port_demand`, a map from output port name to mask). The fully observed context keeps the plain key, so a design whose instance outputs are all observed is analysed exactly as before. If no instance observes every output, the plain key leaves the design.
  - The `INST` node keeps `module_key`; the variant is named by the attribute `ctx_key`, which is not an identity attribute (`NON_IDENTITY_ATTRS`) and so enters no shape key or hash. `child_key(node)` returns `ctx_key` if present, else `module_key`.
  - `Design.unresolved_contexts` lists the keys of modules absent from the design (stubbed by a changed-cone elaboration, or missing) whose instances are not fully observed, or which keep an otherwise unobservable instance alive: the cost taken for such a stub would not be that of a full elaboration.
  - `Limits.max_context_variants = 64` bounds the variants of one key; beyond it, partly observed instances count as fully observed (a conservative over-estimate), so the number of analysed modules stays linear.
  - `GATECOST_NO_HIER=1` (debugging) or `Design.analyze(hierarchy=False)` analyses every module on its own, as before.
  - Changed-cone hook (`analyze_cone` in `pipeline.py`): a stub whose instance is not fully observed (it is in `unresolved_contexts`) is elaborated too, for up to 4 iterations; if that does not converge, the analysis falls back to the full one.
- Recursive instantiation is detected and reported (`RECURSIVE_INSTANCE`), and the recursion is cut at the second occurrence.

## 9. Textual Dump (testing and debugging)

A stable, line-oriented dump of a Module IR is available through the Python API and an internal debug switch (not a public CLI feature):

```
module alu (W=32)
  port in  a[31:0]
  port in  b[31:0]
  port out y[31:0]
  net n1[31:0] = ADD(a[31:0], b[31:0]) sa=0 sb=0           @alu.sv:12
  net n2[31:0] = MUX(a[31:0], n1[31:0], sel[0]) group=g1   @alu.sv:14
  y[31:0] <- n2[31:0]
  known n1: none  demand n1: 0xffffffff  eff_w(ADD)=32
```

The dump format is fixed so that golden tests can compare the IR of a fixture exactly. It prints nodes in a deterministic order (topological, ties broken by shape key).

## 10. Limits

| Limit | Default |
|---|---|
| Nodes per Module IR | 2,000,000 |
| Nets per Module IR | 2,000,000 |
| Width of a single net | 65,536 bits |
| Memory depth (flop-based costing) | 1,048,576 words |
| Context variants of one module key (§8) | 64 |

Exceeding a limit produces `LIMIT_REACHED`; the module is replaced by a black box with the affected construct reported, and the analysis continues.

## 11. Memory and Speed Requirements

- Nodes, nets, and `SigSpec` chunks use `__slots__` classes; chunk lists are tuples; constants are interned.
- No pass allocates per-bit objects: per-bit facts are Python integers used as bit masks.
- The builder's hash table is a single dictionary per Module IR.
- Speed is a comfort budget for use by an AI agent, not a hard constraint (`SPEC.md` §12, `FRONTEND.md` §8): building and annotating a Module IR of 100,000 requested nodes (about 75,000 built) takes a few seconds (about 1 s to build and 2 to 3 s to analyse), and the time grows roughly linearly with the number of nodes.

## 12. Tests

- **Builder:** one fixture per rule of §4.3 asserting the produced structure; hash-consing merges equal subexpressions and keeps different ones; commutative operands in either order give the same node; constant folding agrees with Python evaluation on random operand values.
- **Wiring:** slicing, concatenation, replication, and extension produce no nodes; sign-extension tracking is reported by `sign_rep`.
- **Passes:** known-bits and demanded-bits golden tests per operation (a table of input facts and expected output facts); dead-logic elimination removes exactly the unreachable cone; effective widths for the examples of `COST_MODEL.md` §7 (truncated add, zero-extended add, sign-extended operands); fusion and sharing detection on the fixtures of `COST_MODEL.md` §7.
- **Lowering:** fixtures for `if`/`case` to `MUX`/`PMUX`/`PRIORITY`; incomplete assignment to a `LATCH`; enable, synchronous reset, and asynchronous reset recognition in Verilog, SystemVerilog, and VHDL, each producing the same IR dump.
- **Language neutrality:** the paired Verilog/VHDL corpus (`FRONTEND.md` §10.7) yields identical dumps after name normalization.
- **Masked merge:** the 8 operand orders of the `OR` form and the `XOR` form give `MUX` nodes, a byte mask gives four 8-bit `MUX` nodes, a named result needing several `MUX` nodes is not rewritten, and a register written `q <= (q & ~m) | (d & m)` gets one enable per segment.
- **Select trees:** the records of `mux_trees` for nested `if`/`case`, with no change of structure, keys or hashes.
- **Hierarchy:** a dead instance costs 0, a partly observed instance gets a context variant, a fully observed design is analysed as before, `GATECOST_NO_HIER=1` restores independent analysis.
- **Keys:** renaming internal nets, reordering declarations, reformatting, and swapping commutative operands leave shape keys and the module canonical hash unchanged; changing a width, a constant, or an operator changes them.
- **Determinism:** two runs produce byte-identical dumps and hashes, independent of dictionary insertion order, `PYTHONHASHSEED`, and platform.
- **Limits:** each limit of §10 is reached by a generated fixture and handled cleanly.
- **Invariants checked after every build:** each net has one driver; every `SigSpec` chunk lies within its net; widths in the catalogue are consistent; no cycles in combinational logic (a combinational loop is reported as `COMBINATIONAL_LOOP` and cut at a register-like boundary).
