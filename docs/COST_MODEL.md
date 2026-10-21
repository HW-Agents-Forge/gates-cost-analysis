# gatecost — Cost Model Reference

Status: Draft. Normative companion to `SPEC.md` §5.4.

Notation: `n`, `m` are operand widths, `w` is data width, `k` is an input count, `L = ⌈log2 n⌉` (computed with `int.bit_length`, never floating point). `P[x]` is the profile value, in gate equivalents (GE, NAND2 = 1), of primitive `x`. All formulas are expressed in primitives, so overriding `P[FA]` in a profile propagates to every operation that uses a full adder. Internally, costs are integer milli-GE; rounding happens only at output.

## 1. Primitive table (`generic-asic` defaults)

Defaults are area ratios derived from the Nangate 45 nm Open Cell Library (X1 drive) and fall within about ±20% of typical commercial 28–65 nm libraries. Every value is overridable by a profile. A profile must also provide `nand2_area_um2` to convert area reports into GE.

| Primitive | Default GE | Spread across libraries |
|---|---|---|
| INV | 0.67 | 0.5–0.75 |
| BUF | 1.00 | 0.75–1.33 |
| NAND2 / NOR2 | 1.00 | 1.0–1.33 |
| NAND3 / NOR3 | 1.33 | 1.25–1.67 |
| NAND4 / NOR4 | 1.67 | 1.5–2.0 |
| AND2 / OR2 | 1.33 | 1.25–1.5 |
| AOI21 / OAI21 | 1.33 | 1.25–1.67 |
| AOI22 / OAI22 | 1.67 | 1.5–2.0 |
| AO22 | 2.00 | 1.75–2.33 |
| XOR2 / XNOR2 | 2.00 | 1.67–2.67 |
| MUX2 | 2.33 | 2.0–2.67 |
| MUX4 | 4.67 | 4.0–5.5 |
| MAJ3 | 2.33 | 2.0–3.0 |
| HA | 3.33 | 3.0–4.0 |
| FA | 5.33 | 4.67–7.0 |
| DLAT | 3.33 | 3.0–4.0 |
| DFF | 5.67 | 4.5–6.5 |
| DFFR (async reset or set) | 6.67 | 5.5–7.5 |
| DFFRS (async set and reset) | 7.67 | 6.5–8.5 |
| EDFF (enable flop) | 7.33 | 6.5–8.0 |
| SDFF adder (scan overhead on a DFF) | +1.00 | +0.67–1.67 |
| ICG (clock gate) | 5.00 | 4.0–6.0 |

## 2. Scenarios

Every formula is evaluated under three **consistent scenarios**, not three independent bounds:

- **min**: smallest plausible architecture, relaxed timing.
- **typ**: the profile's architecture policy at moderate timing.
- **max**: fastest architecture, multiplied by a sizing/buffering factor σ (default 1.3 for arithmetic, 1.15 for everything else).

For classes with several architectures, `typ = min_arch + α_c · (mid_arch − min_arch)` with `α_c ∈ [0, 1]` (default 0.4). It is a fixed profile constant.

## 3. Formulas

### 3.1 Arithmetic

| Operation | min | mid (feeds typ) | max |
|---|---|---|---|
| ADD, n bits | ripple: `(n−1)·FA + HA` | Brent–Kung: `n·HA + (2n−2−L)·G + (n−1)·XOR2`, with `G = 2.0` | `σ·[n·HA + (n·L−n+1)·G + (n−1)·XOR2]` (Kogge–Stone) |
| SUB | ADD | ADD + `0.33·n` | ADD + `n·INV` |
| ADD/SUB with constant K | `(n − tz(K))·HA` (`tz` = trailing zeros) | ×1.3 | ×2 |
| NEG | `(n−1)·(OR2 + XOR2)` | same | ×σ |
| k-operand ADD (fused) | `(k−2)·w·FA + ADD_ripple(w + ⌈log2 k⌉)` | CSA tree + Brent–Kung CPA | unfused: `(k−1)·ADD_max` |

A 1-bit ADD is an XOR2. A carry-out-only use is costed as a comparator.

### 3.2 Comparators and bitwise operations

| Operation | min | typ | max |
|---|---|---|---|
| EQ/NE, n bits | `n·XOR2 + (n−1)·0.55` | `n·XOR2 + (n−1)·0.75` | `3.33·n` |
| EQ with constant | `(n−1)·0.55` | `(n−1)·0.75` | `(n−1)·1.33` |
| LT/LE/GT/GE | `2.7·n` | `3.5·n` | `σ·5.0·n` |
| LT with constant | `1.0·n` | `1.33·n` | `2.0·n` |

Signed comparison adds one XOR2. Reduction AND/OR costs 0.55 / 0.75 / 1.33 GE per merged input; reduction XOR costs 1.67 / 2.0 / 2.33. Bitwise operations cost `w·P[gate]`; a NOT absorbed into neighbouring logic costs 0 (typ uses `0.5·P[INV]`).

### 3.3 Selection

| Operation | min | typ | max |
|---|---|---|---|
| MUX k:1, binary select, width w | `(k−1)·w·1.17` (MUX4 cells) | `(k−1)·w·MUX2` | `σ·typ + ⌈w/8⌉·BUF` per select bit |
| PMUX k inputs, one-hot select | `w·(⌈k/2⌉·AO22 + (⌈k/2⌉−1)·0.75)` | `w·(⌈k/2⌉·AO22 + (⌈k/2⌉−1)·OR2)` | `k·w·MUX2` |
| PRIORITY chain of k | PMUX + `(k−1)·AND2` | `0.9·k·w·MUX2 + (k−1)·AND2` | `k·w·MUX2` |
| DECODE s → 2^s | `2^s·NOR2` + predecode `2·2^(s/2)·1.33` | `2^s·AND2` + predecode | `2^s·(s−1)·AND2` |

For `case` statements the select decode is costed separately, as `k` EQ-with-constant comparators; structural hashing shares common sub-terms.

### 3.4 Shifters (data n bits, amount s bits, `Ls = min(s, L)`)

- Logical shift, typ: `Σ_{j<Ls} [(n − 2^j)·MUX2 + 2^j·AND2]`, plus, when `s > L`, an OR-reduction of the excess amount bits and `n·AND2` masking.
- Arithmetic shift and rotate: `n·Ls·MUX2`.
- min = `0.85·typ`; max = `σ·typ + Ls·⌈n/8⌉·BUF`.
- A shift by a constant costs 0. `1 << x` costs DECODE(s).

### 3.5 Multipliers (n × m, n ≥ m)

- **Array:** partial products `n·m·AND2`, plus `(n·m − n − m)·FA + n·HA`.
- **Booth radix-4 with Wallace tree:** `R = ⌈(m+1)/2⌉` rows of `n+2` bits. Cost = `R·(n+2)·3.5` (selectors) + `⌈m/2⌉·6` (encoders) + `(R·(n+2) − 2·(n+m))·FA` + `CPA(n+m)`.
- **Scenarios:** min = smaller of array and Booth with a ripple CPA; typ = Booth with an α-interpolated CPA when `m ≥ 12`, array otherwise; max = `σ·` (Wallace tree, non-Booth partial products, Kogge–Stone CPA).
- Signed (Baugh–Wooley) adds `(n+m)·INV + 2·HA`. A square (`a*a`) costs `0.55×` a general multiply.
- **Truncated product** (only the low `t` bits demanded): scale partial-product and FA terms by the fraction of the array below column `t`, about `t(t+1) / (2nm)` for `t ≤ m`.
- **Constant multiplication:** write K in canonical signed-digit (CSD) form with `h` non-zero digits. Cost is 0 when `h ≤ 1`; otherwise typ = `(h−1)·ADD_typ(n + ⌈log2|K|⌉ − d_min)`, with `d_min` the smallest digit spacing; min = `0.7·typ`; max = `(h−1)·ADD_max(n + ⌈log2|K|⌉)`. Negative digits are subtractions.
- **POW** with constant exponent `e`: `⌊log2 e⌋ + popcount(e) − 1` multiplications at their effective widths. `2**x` costs DECODE.

### 3.6 Divider and modulo (n dividend bits, m divisor bits)

- min: non-restoring array, `n·(m+1)·(FA + XOR2)`.
- typ: restoring, `1.2 · n·[(m+1)·FA + m·MUX2]`.
- max: `2.5 × typ`.
- DIV and MOD of the same operands are costed once.
- Unsigned division or modulo by `2^k` costs 0; signed division by `2^k` costs one n-bit ripple ADD.
- Division by another constant: reciprocal multiplication, a constant multiply with `h ≈ (n+1)/3` at width `2n`, plus a shift.
- The whole class is flagged `quality = poor`.

### 3.7 Registers (per bit)

| Variant | min | typ | max |
|---|---|---|---|
| plain | DFF | DFF | DFF (+ scan adder if `scan`) |
| async reset/set | DFFR | DFFR | + scan |
| sync reset | DFF + 0.67 | DFF + AND2 | DFF + MUX2 |
| enable | DFF + ICG/w (gated bank, if `w ≥ icg_min_width`, default 3) | per `enable_style`: DFF + MUX2 (`mux`), EDFF (`edff`), or min-style (`icg`) | DFF + MUX2 + scan |

Clock gating is not the default for typ because it only occurs when the flow enables it. Scan is off by default (pre-DFT reports exclude it). A latch costs DLAT; a latch enable is its gate.

### 3.8 Memories (D words × W bits, R read ports, `P_w` write ports)

- **Flop-based:**
  - typ = `D·W·(DFF + MUX2) + R·(D−1)·W·MUX2 + P_w·DECODE(log2 D) + (P_w−1)·D·W·MUX2`
  - min = `D·W·(DLAT + ICG/W) + R·(D−1)·W·1.17 + DECODE`
  - max = `σ·typ`
- **Macro**, used only when all hold: synchronous read, `D·W ≥ macro_min_bits` (default 4096), `D ≥ 16`, and macros enabled in the profile. Cost `c0 + c_bit·D·W + c_row·D + c_col·W` (defaults 3000, 0.6, 4, 30); min = 0.5×, max = 2×. Category `memory_macro`; confidence is capped at `low` unless the profile supplies a `(D, W) → GE` table, which is bilinearly interpolated.
- **ROM** (`D × W` constant table): constant columns cost 0; columns equal to an address bit or its complement cost 0 or INV; duplicate and complemented columns are shared. For each remaining column, typ = `2.0·D / log2 D`, min = `0.5·typ`, max = `(D/2)·MUX2`. For `log2 D ≤ 6`, a cheap two-level minimization (literal count × 0.75) replaces typ.

## 4. Reducing over-estimation

Naive per-operator costing over-estimates what synthesis produces. The IR and cost evaluation implement the following, all in linear time. A general logic optimizer is explicitly out of scope.

1. **Bit-level constness.** Each net carries a known-bit mask `(kmask, kval)` and a sign-replication marker. Before costing: a MUX2 with one constant input becomes AND2/OR2; identical inputs become a wire; both inputs constant yields a wire or INV(sel); a known-zero operand bit in an ADD makes that bit a wire.
2. **Demanded bits.** A backward pass from outputs, register D pins, and memory ports yields an MSB/LSB demand per operation. Effective ADD width is `min(demand, max(w_a', w_b') + 1)`, where `w'` strips known-zero and sign-replicated MSBs. MUL widths strip trailing zeros (they become shifts) and known-zero MSBs.
3. **Dead and duplicate logic.** Logic without a transitive path to an output, register, or memory port costs 0. A register whose D input is a constant equal to its reset value costs 0. Structural hashing on `(op, operand ids, constants, attributes)` merges common subexpressions and equivalent flops (merged in min/typ, retained in max).
4. **Datapath fusion.** Chains of ADD/SUB/NEG/MUL whose intermediates have fanout 1 become one `SUM` cluster: `a*b + c` folds into the multiplier reduction tree (about `w·FA`); `a+b+c+d` becomes a CSA tree plus one CPA. min and typ use the fused cost; max uses the unfused cost.
5. **Resource sharing across exclusive branches.** MUX/PMUX trees built from one `if`/`case`/`?:` within one always block or assign are tagged. For leaves that are same-class arithmetic operations (MUL, DIV, ADD/SUB with `w ≥ 8`): `shared = max_i cost(op_i) + (k−1)·(w_a + w_b)·MUX2 − (k−1)·w_o·MUX2`. It applies when `shared < unshared`. Policy `resource_sharing = "auto"` uses it in typ; min uses the smaller of the two; max uses unshared. Sharing is never applied across always blocks or modules.
6. **Enable recognition.** `if (en) q <= d;` becomes the register's enable, not a separate MUX.
7. **Constant ports.** A constant tied to an instance port is propagated into a uniquified instance (`propagate_port_constants`, default on).

## 5. Error sources and confidence

| Source | Typical magnitude | Treatment |
|---|---|---|
| Library ratios (DFF/NAND2, XOR, FA) | ±20% per class | profile primitives (user-overridable) |
| Timing constraint → architecture | arithmetic ×1–3 | min/typ/max scenarios; fixed `α_c` |
| Boolean optimization of glue logic | −10 to −30% | class `logic` scale (default 0.85 in typ) |
| Buffering, sizing, hold fixing | +5–15% | global `overhead` (default 1.0, range 1.0–1.15) |
| Hierarchy boundaries / constant propagation | design-dependent | §4.7; diagnostic when disabled |
| DFT, clock gating, retiming | ±10–20% on sequential logic | explicit policy switches |
| Memories, ROM, DIV | ±50% or more | `quality = poor` |

### 5.1 Combining errors

Each operation has a typ value `typ_i`, a correlated systematic band `[lo_i, hi_i]` from the scenarios, and an independent residual `σ_i = typ_i · rms_c`, where `rms_c` is a fixed per-quality-class constant of the profile (defaults: 0.15 for good, 0.25 for medium, 0.5 for poor classes).

- **Deltas are computed per scenario**: `Δ_s = Σ cost_s(after) − Σ cost_s(before)`. The tool must never compute `max(after) − min(before)`; interval subtraction inflates ranges excessively. Unchanged operations cancel exactly.
- Range: `Δ_min = min_s Δ_s − 2·√(Σ σ_i²)` and `Δ_max = max_s Δ_s + 2·√(Σ σ_i²)`, with the sums over changed operations only.

### 5.2 Confidence derivation

Let `r = (Δ_max − Δ_min) / (2 · max(|Δ_typ|, A_floor))` with `A_floor = 20 GE`.

- **high**: no unsupported construct in the changed cone; at least 90% of `|Δ|` in good-quality classes; `r ≤ 0.2`.
- **medium**: no unsupported construct in the changed cone and `r ≤ 0.5`.
- **low**: anything else. Unknown black boxes, a macro without a table, or a poor-quality share above 30% of `|Δ|` force `low`.

`r` is exposed in the JSON output so the caller can see why.

## 6. Profile format

Profiles are TOML and may `extends = "generic-asic"` to override individual keys.

```toml
[meta]
name = "acme28-hd"
source = "acme28hd_tt0p9v25c.lib"
nand2_area_um2 = 0.4704

[primitives]            # GE, NAND2 = 1
INV = 0.67
NAND2 = 1.0
XOR2 = 2.0
MUX2 = 2.33
HA = 3.33
FA = 5.33
DFF = 5.67
DFFR = 6.67
EDFF = 7.33
ICG = 5.0
# ... every primitive in §1

[policy]
timing = "moderate"             # relaxed | moderate | aggressive -> default alpha
enable_style = "mux"            # mux | icg | edff
fsm_encoding = "binary"         # binary | one_hot (enumerated state registers)
icg_min_width = 3
scan = false
resource_sharing = "auto"       # off | auto
datapath_fusion = true
propagate_port_constants = true
sizing_factor = { arith = 1.3, other = 1.15 }
overhead = { typ = 1.0, max = 1.15 }

[arch]
add = { min = "ripple", mid = "brent_kung", max = "kogge_stone", alpha = 0.4 }
mul = { min = "array", mid = "booth_wallace", max = "wallace_ks", booth_min_width = 12 }

[memory]
macro_enable = true
macro_min_bits = 4096
macro = { c0 = 3000, c_bit = 0.6, c_row = 4, c_col = 30 }
# macro_table = [{ d = 256, w = 32, ge = 9800 }]

[blackbox]
"acme_pll" = 4200
"acme_sync2" = { typ = 13.5, min = 12, max = 15 }

```

## 7. Test requirements for the cost model

**Exact formula values** (tolerance 0.01 GE): ADD at 1/8/32/64 bits in all scenarios; 1-bit cases (ADD → XOR2, MUL 1×1 → AND2, MUX with `k = 1` → 0); EQ 32; LT 32 signed and unsigned; MUX 2:1/3:1/4:1/16:1 at 32 bits; PMUX `k = 5`; DECODE 3→8 and 5→32; shift/arithmetic-shift/rotate at 32 bits with `s = 5` and `s = 8`; MUL 8×8, 16×16, 32×32, 24×8 (operand order); DIV 32/16; every register reset/enable style with and without scan; MEM 16×8 flop-based, 1024×32 macro-eligible, 1024×32 asynchronous read (stays flop-based); ROM with constant, duplicate, and address-equal columns.

**Constant handling:** shift by a constant = 0; `× 2^k` = 0; `× 10` = one adder; `× 7` (CSD `8 − 1`) = one subtractor; `× 0` = 0; `a + 1` = incrementer; `a == 0` = reduction NOR only; MUX with constant inputs → AND/OR; unsigned `/4` = 0; `%8` = 0.

**Width handling:** a 32-bit add truncated to 16 bits costs ADD 16; `{16'b0, a} + {16'b0, b}` costs ADD 17; sign-extended operands cost one extra bit; a low-half-only MUL follows the triangular cost.

**Sharing and fusion:** a `case` of 4 MULs costs less than 2 MULs; the same expression written twice costs once; `a*b + c` is cheaper than MUL + ADD; `a+b+c+d` is cheaper than 3 ADDs in typ; DIV and MOD of the same operands cost one divider; duplicate registers are merged in typ and kept in max.

**Properties** (randomized over widths 1..128, seeded):
- `min ≤ typ ≤ max` for every operation;
- every scenario is monotone non-decreasing in each width (architecture switches must not break this: typ takes the minimum over eligible architectures, or monotonicity is verified);
- MUL is symmetric in its operands under the formula's operand convention;
- an operation with a constant operand never costs more than its variable counterpart;
- `Δ_min ≤ Δ_typ ≤ Δ_max`;
- unchanged operations contribute exactly 0 to a delta;
- scaling every primitive GE by a factor scales the total by that factor;
- identical before/after states give a zero delta with a zero range;
- a profile with `extends` and no overrides produces byte-identical output;
- results are byte-identical across runs.

**Reference sanity bands** (`generic-asic`, typ): ADD32 ∈ [170, 300]; MUL16 ∈ [1200, 2000]; MUL32 ∈ [4500, 8000]; 32-bit DFF bank ∈ [180, 260]; MUX 4:1 × 32 ∈ [200, 260]; 32-bit barrel shifter ∈ [330, 420].

## 8. Implementation pitfalls

1. Compute deltas per scenario (§5.1); interval subtraction is the most likely implementation error.
2. Use integer milli-GE and a fixed evaluation order. Use `int.bit_length` for `⌈log2⌉`.
3. Avoid common double counts: the enable-feedback MUX; an `always_comb` default assignment followed by a `case` (one PMUX, not a chain); `casez`/`casex` don't-cares, which reduce compare literals and must not be costed as full EQ.
4. Unintended latches are costed as DLAT with a warning diagnostic. `x` assignments are don't-cares and allow folding; min and typ may treat them as wires.
5. Keep the class set small. Random glue logic is the least predictable class and must be labelled as such.
6. Do not build a logic optimizer. Bit-level constness, demanded bits, structural hashing, and the three pattern rules (sharing, fusion, enable) capture most of what synthesis recovers, in linear time.
