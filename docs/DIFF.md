# gatecost — Differ and Attribution Reference

Status: Draft. Normative companion to `SPEC.md` §5.5 and §5.6.

The differ answers two questions for a modification of an RTL design:

1. **How much** does the design's gate cost change (total, per module, per category)?
2. **Why**: which source lines, and which underlying causes, account for the change?

Both answers must be exact in the arithmetic sense (contributors add up to the total) and stable in the practical sense (cosmetic edits yield no delta, and small edits yield small, understandable explanations). Estimation uncertainty follows `COST_MODEL.md` §5.

## 1. Inputs

The differ takes two **cost trees**, `before` and `after` (`SPEC.md` §5.5), produced with the same profile, defines, and top module. If the profile, defines, parameters, or top module differ, the differ still runs but emits a `CONFIG_MISMATCH` diagnostic and caps confidence at `low`.

### 1.1 Configuration of each side

Normally one `gatecost.toml` (plus the command line) configures both sides. When the project file itself differs between the sides, each side is analyzed with its own:

- **git (`--base-ref`, default `HEAD`) and `--since-snapshot`:** the baseline side reads the file as of the base reference or snapshot (absent there: no project file); the current side reads the working tree's. If the two are byte-identical nothing changes. A baseline file that cannot be parsed is replaced by the current one, with a `CONFIG_MISMATCH` warning.
- **`--before DIR --after DIR`:** without `--config`, each side reads the `gatecost.toml` of its own directory; a directory without one uses the project file found for the invocation (`--root`/working directory). `--config FILE` serves both sides.
- Command-line options (`--top`, `--define`, `--param`, `--incdir`, ...) apply to both sides and win over either file.

`CONFIG_MISMATCH` names exactly what differs: `top` (both sides pin a different module), `defines.NAME`, `params.NAME`, `sources`, `incdirs`, `filelists`, `exclude`, `limits`, `profile`. Such a diff is never answered from the changed-cone shortcut. A relative `--incdir` or `--filelist` of a `--before`/`--after` run that does not point into the `--after` tree is resolved against each side's own directory.

Cost-tree nodes form the hierarchy `design → module instance → operation`. Each operation node carries:

- a **node key** (§2) used for matching;
- its cost under the three scenarios and its residual uncertainty;
- its operation kind, category, quality class, declared and effective widths, assumption tags, and primitive breakdown;
- its **origin** (file, line, column; for macro-generated code, the use site plus the definition site);
- its **provenance facts**: the parameters, constants, and widths that determined its cost (for example `{"W": 16}` or `{"enable": "en"}`), used for cause analysis (§5).

## 2. Node Identity

### 2.1 Costs are attributed to operations

After reductions (`COST_MODEL.md` §4), the cost of the design is exactly the sum of its operation costs: cost saved by sharing, fusion, or folding is assigned to the operation that remains and is zero on the operations that disappear. The differ relies on this identity: **the sum of all contributor deltas equals the total delta, per scenario.** No residual bucket is needed for arithmetic reasons.

### 2.2 Keys

Each operation has two keys:

- **Named key:** `(module, driven target, role path, op kind)`. The *driven target* is the register, port, or named signal driven by the expression the operation belongs to. The *role path* is the operation's position in the expression tree (for example `then.lhs.mul`). It is sensitive to names and to the expression's shape.
- **Shape key:** a Merkle hash over the operation's fanin cone in which internal wire names are erased and only ports, registers, and constants (value and width) remain. It is insensitive to renaming of internal signals and to the order of commutative operands.

Instances are keyed by their hierarchical instance path (`u_core.u_alu`, `gen[3].u_mul`). Modules are keyed by `(module name, parameter set)`.

## 3. Matching

Matching proceeds from the most to the least specific tier. Each node is matched at most once.

1. **Unchanged short-circuit.** A module whose canonical cost-tree hash is equal on both sides contributes exactly zero and is not traversed. Comment, whitespace, formatting, file-move, and declaration-reordering edits therefore produce a zero delta.
2. **Module matching.** Modules are matched by name regardless of the file they live in. A module present on one side only is *added* or *removed*. A rename is detected when an unmatched removed module and an unmatched added module have equal shape hashes; it is then reported as `renamed` with a zero intrinsic delta.
3. **Instance matching.** Instances are matched by hierarchical path. Unmatched instances are paired by `(module, port-connection signature)`; the rest are *added* or *removed*. An instance whose parameter set or constant ports changed is matched by path and compared parameterization to parameterization (§4).
4. **Operation matching** inside each matched module-instance pair:
   1. Named key equal → matched.
   2. Shape key equal (and unmatched) → matched, reported as `renamed` with zero delta if the cost is equal.
   3. **Line alignment.** A line alignment between the before and after sources is computed with `difflib.SequenceMatcher` on token-normalized lines (whitespace and comments stripped). Remaining operations of the same kind are paired when their origins map to each other through the alignment, closest first. A pair with different widths or parameters is reported as `modified`.
   4. Leftover before-operations are *removed*; leftover after-operations are *added*.

Each matched pair yields `delta = cost_after − cost_before`, per scenario. Added operations have `before = 0`; removed ones have `after = 0`.

## 4. Hierarchy and Multiplicity

For a module instantiated `n` times, the contribution to its parent is `n × cost(module)`. The differ separates the two ways this can change:

```
n_a·C_a − n_b·C_b  =  (C_a − C_b)·n_a   +   C_b·(n_a − n_b)
                        intrinsic             multiplicity
```

where `b` and `a` denote before and after. Both terms are reported:

- **intrinsic**: the module body (or its parameterization) changed, multiplied by the *current* instance count;
- **multiplicity**: the instance count changed, valued at the *previous* per-instance cost.

When a module has several parameterizations, the decomposition applies to each `(module, parameter set)` separately. If an instance moves from parameter set `P1` to `P2`, it is reported as one `modified` instance with the cost difference `C(P2) − C(P1)`, and the contributors inside the two elaborations are matched by operation matching (§3), so the reported lines are the module's own lines.

Operations inside a module instantiated more than once are reported once per module with the multiplier shown (`instances: 4`); their `delta_ge` is the total over all instances, and a `per_instance` figure is also given.

## 5. Cause Analysis

Each non-zero contributor receives one **reason** from this closed set, and optionally a **cause** pointing to the source location responsible.

| Reason | Meaning |
|---|---|
| `added` | Operation exists only after. |
| `removed` | Operation exists only before. |
| `width` | Same operation, different effective width. |
| `constant` | Same operation, a constant operand changed (including becoming or ceasing to be constant). |
| `parameter` | Cost changed because a parameter, generic, or generate condition changed. |
| `structure` | Same cone, different composition (for example a `case` gained a branch, a `PMUX` input count changed). |
| `architecture` | The cost-model architecture choice changed (for example Booth versus array at a width threshold) without a change in the operation itself. |
| `sharing` | A resource-sharing group changed (an operation became shared or stopped being shared). |
| `fusion` | A datapath-fusion cluster changed. |
| `register` | A register's attributes changed: reset kind, enable, width, or loss of constant-reset equivalence. |
| `multiplicity` | The instance count of the enclosing module changed. |
| `blackbox` | The cost or presence of a black box changed. |

**Reason assignment.** For `modified` pairs the differ compares provenance facts one by one in a fixed order (parameters, constants, widths, structure, sharing, fusion, register attributes, architecture); the first difference sets the reason.

**Cause tracing.** For `width`, `constant`, and `parameter` reasons, the differ follows the provenance fact back to its **origin**: a parameter override, a localparam definition, a port width, or a constant assignment. If that origin lies on a line that changed between before and after, the contributor is attributed to it as `cause` and is **grouped** with the other contributors sharing that cause. Example: a one-line change from `localparam W = 16` to `32` produces one grouped contributor "W: 16 → 32 (config.sv:12)" with the 14 affected operations as children, instead of 14 unrelated lines.

## 6. Granularities

The same set of matched contributors is aggregated into the following views, each summing exactly to the total delta:

| View | Content |
|---|---|
| `total` | Delta for the top design. |
| `by_module` | Per `(module, parameter set)`: intrinsic and multiplicity components, instance count before and after. |
| `by_line` | Per source location `(file, line)`: sum of the delta of operations whose origin is that line (operations from a macro are charged to the use site). Removed operations are charged to their *before* location and marked as such. |
| `by_cause` | Grouped contributors from §5. |
| `by_category` | Per cost class (`seq`, `addcmp`, `mul`, `select`, `logic`, `shift`, `div`, `mem`). |
| `structural` | Modules added, removed, or renamed; instances added or removed; parameter changes; black boxes added or removed; changes in the set of diagnostics. |

**Line view details.** Line numbers are reported in *after* coordinates. Removed operations are translated from before coordinates through the line alignment (§3.4.3) when a nearby surviving line exists; otherwise the *before* coordinates are used and the contributor is tagged `before_location`.

## 7. Uncertainty

All deltas are computed **per scenario** (`min`, `typ`, `max`) as in `COST_MODEL.md` §5.1, never as differences between scenario extremes. The residual term is the quadrature sum of `σ_i` over the contributors of the scope being reported (total, module, or line), so a line's range is computed from its own operations only. Contributor ranges therefore do not sum to the total's range, which is narrower; the total range is authoritative.

## 8. Presentation Rules

The differ hands the reporters a ranked list of contributors, which the `agent` format (`SPEC.md` §8.1) truncates:

1. Contributors are grouped by `cause` when one exists; otherwise by `(file, line)`; otherwise by module.
2. They are sorted by absolute `typ` delta, descending.
3. Items whose absolute `typ` delta is below the report threshold (`threshold_ge`, default 5 GE) are folded into one *others* entry giving the count and the net sum.
4. Each contributor carries: location, operation kind and widths, reason, cause if any, `typ` delta with range, instance count, and a short explanation generated from a fixed template per reason (for example `MUL 16x16 added`, `ADD width 8 → 12`, `register stage added (36 DFF)`, `multiplier no longer shared`).
5. Diagnostics that changed between before and after (a new `UNSUPPORTED_CONSTRUCT`, a resolved `MISSING_MODULE`, ...) are always displayed, since they bear on how the figure should be read.

## 9. Verdicts

The differ computes a verdict consumed by all reporters:

| Verdict | Condition |
|---|---|
| `NO_CHANGE` | Canonical cost trees are identical (formatting or comment-only edit, file move, or reordering). |
| `NEGLIGIBLE` | `|Δ_typ| < threshold_ge` and the range includes zero. |
| `INCREASE` / `DECREASE` | `|Δ_typ|` above the threshold and the range excludes zero. |
| `UNCERTAIN` | The range includes zero but `|Δ_typ|` exceeds the threshold (the sign is not established). |
| `NOT_COMPARABLE` | The sources could not be analyzed on one of the two sides, or the configuration mismatch of §1 prevents a meaningful delta. |

## 10. Edge Cases

| Situation | Behavior |
|---|---|
| Baseline contains parse errors | Analyze what parsed; diagnostics from both sides are shown, and confidence is capped at `low` if the damaged region intersects the changed cone. |
| The edit introduces or repairs an error-severity diagnostic that affects cost (parse error, ...) | Confidence is `low` with the diagnostic code as the reason, whatever the verdict (a broken file can read `NO_CHANGE`). |
| Top module differs between sides | Compare the designs as wholes and emit `TOP_CHANGED`. |
| A file is added, deleted, or moved | Modules are matched by name; the move itself yields no delta. |
| Module renamed with identical body | Reported as `renamed`, zero delta. |
| Large reformat that also changes a few lines | Zero delta for the reformatted code; only genuinely changed operations contribute. |
| Parameter default changed, overridden at every instance | Zero delta. |
| Instance-array size changed | `multiplicity` contributor valued at the previous per-instance cost. |
| `ifdef`/define change flips a code path | Contributors are tagged with the macro as the cause (`cause = define:NAME`). |
| Change only in a skipped region (`initial`, assertion) | `NO_CHANGE`, with an `info` note. |
| Operation moves between modules (refactor) | Reported as one removal and one addition; the net is shown in `structural` as a `moved` hint when shape keys are equal. |

## 11. Complexity and Performance

- Work is proportional to the size of the *changed* modules and their instance paths, not to the design (step 1 of §3 prunes unchanged modules).
- Matching tiers use hash tables; line alignment is computed per changed file with `difflib` over the file's normalized lines and is skipped for modules whose operations all match in tiers 1 and 2.
- Target: under 50 ms for a single-module edit once the cost trees are available.

## 12. Tests

- **Zero-delta invariants:** reformatting, comment edits, signal renaming, declaration reordering, file moves, module renames, and operand commutation produce `NO_CHANGE` with an exactly zero delta and a zero range.
- **Additivity:** for every test pair and every scenario, the sum of all contributor deltas equals the total delta; the sums of `by_module`, `by_line`, `by_cause`, and `by_category` also equal the total.
- **Matching:** fixture pairs with the expected matching and reason for each operation: add one adder; widen a bus; change a constant; add a case branch; add a pipeline stage; wrap logic in a new module; split a module into two; change a parameter used in many places (grouped under one cause); toggle a define.
- **Multiplicity:** the `n_a·C_a − n_b·C_b` decomposition for instance-count changes, intrinsic-only changes, and both together, with several parameter sets.
- **Reasons:** one fixture per reason in §5, whose expected reason is checked.
- **Sharing and fusion:** a change that makes a multiplier stop being shared produces a `sharing` contributor with a positive delta and no spurious contributors elsewhere.
- **Line view:** macro-generated operations are charged to the use site; removed operations keep their before location when no alignment exists.
- **Symmetry:** `diff(A, B)` and `diff(B, A)` give opposite deltas in every scenario (ranges mirrored), with reasons `added` ↔ `removed`.
- **Transitivity:** for three revisions A, B, C, `Δ(A,C)` equals `Δ(A,B) + Δ(B,C)` in the `typ` scenario, within one milli-GE per touched operation.
- **Incremental:** results with the module-level cache equal results without it.
- **Verdicts:** one fixture per verdict, including `UNCERTAIN` and `NOT_COMPARABLE`.
