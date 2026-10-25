# gatecost — Cost Model Reference

Status: Draft. Normative companion to `SPEC.md` §5.4.

Notation: `n`, `m` are operand widths, `w` is data width, `k` is an input count, `L = ⌈log2 n⌉` (computed with `int.bit_length`, never floating point). `P[x]` is the profile value, in gate equivalents (GE, NAND2 = 1), of primitive `x`. All formulas are expressed in primitives, so overriding `P[FA]` in a profile propagates to every operation that uses a full adder. Internally, costs are integer milli-GE; rounding happens only at output.

## 1. Primitive table (`generic-asic` defaults)

Defaults are area ratios derived from the Nangate 45 nm Open Cell Library (X1 drive) and fall within about ±20% of typical commercial 28–65 nm libraries; since the Yosys calibration (§5.3) they describe what a flow without full-adder cells builds. Every value is overridable by a profile. A profile must also provide `nand2_area_um2` to convert area reports into GE.

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
| FA | 6.67 | 4.67–7.0 |
| DLAT | 3.33 | 3.0–4.0 |
| DFF | 5.67 | 4.5–6.5 |
| DFFR (async reset or set) | 6.67 | 5.5–7.5 |
| DFFRS (async set and reset) | 7.67 | 6.5–8.5 |
| EDFF (enable flop) | 7.33 | 6.5–8.0 |
| SDFF adder (scan overhead on a DFF) | +1.00 | +0.67–1.67 |
| ICG (clock gate) | 5.00 | 4.0–6.0 |

`FA = 6.67` is a full adder built from single-output cells, XOR2 + XNOR2 + AOI222 (majority), the way Yosys/ABC builds it when the library gives no adder cell. The `FA_X1` cell of Nangate 45 is 5.33 GE; a commercial flow that infers adder cells is closer to that value. Measured with Yosys: an isolated FA 7.0, carry-save adders 6.1–6.4 per FA, ripple adders ≈ 7.0, multiplier trees 7.3–7.7. `G = 1.33` (prefix cell) is an AOI21/OAI21 pair with alternating polarity: Nangate 45 has no AO21 cell.

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
| ADD, n bits | ripple: `(n−1)·FA + HA` | Brent–Kung: `n·HA + (2n−2−L)·G + (n−1)·XOR2`, with `G = 1.33` | `σ·[n·HA + (n·L−n+1)·G + (n−1)·XOR2]` (Kogge–Stone) |
| SUB | ADD (the inversion of the operand is absorbed) | ADD typ + `n·INV` (`a + ~b + 1`; added after the α interpolation, whatever the architecture) | ADD max + `n·INV` |
| ADD/SUB with constant K | `INV + (n − tz(K) − 2)·HA + XOR2` (`tz` = trailing zeros; `n − tz = 1` is one INV) | ×1.3 | ×2 |
| NEG | `(n−1)·(OR2 + XOR2)` | same | ×σ |
| k-operand ADD (fused) | `(k−2)·w·FA + ADD_ripple(w + ⌈log2 k⌉)` | CSA tree + Brent–Kung CPA | larger of two stories: fused at tight timing (`σ·CSA + ADD_max`, `csa+kogge_stone`) or unfused at moderate timing (chain of `k−1` typ adders, widest operand first, label `unfused`) |
| MAC `a*b + c` (`c` of `w_c` bits, `a`×`b` is `n`×`m`) | MUL min + `min(w_c, n+m)·FA` + `max(0, w_c−n−m)·HA` | MUL typ + the same | larger of two stories: `MUL max + σ·FA rows` (fused, fastest timing) or `MUL typ + ADD typ` (unfused, moderate timing) |
| ADD/SUB, result `n` bits, narrow operand `m < n−1` bits | `ADD(m)` + `(n−m)·HA` | `ADD(m)` + `1.3·(n−m)·HA` at α = 1 | `ADD_max(m)` + `2·(n−m)·HA`, each capped by the full `n`-bit ADD |

A 1-bit ADD is an XOR2. The constant adder `a + K` with the lowest set bit of `K` at position `tz` has, from that bit up, the lowest bit `~a` with carry `a` (one INV), a half-adder cell per intermediate bit (XOR/XNOR + AND/OR) and a sum-only top bit (XOR2). In a MAC the columns of `c` above the `n+m` product bits only propagate a carry and count one HA each (as for the narrow operand below), so fusing never costs more than the adder it replaces. A carry-out-only use is costed as a comparator. When one operand has only `m` significant bits (after stripping known-zero and sign-replicated bits) and `m < n−1`, the upper `n−m` bits only propagate a carry and are costed as an incrementer (one HA per bit, scaled like an add with a constant), so `x + flag` costs about half of a full `n`-bit adder.

A fused k-operand sum built by the netlist builder uses the effective width `t_i` of every term rather than the result width: the CSA tree needs `max(0, Σt_i − 2·max t_i)` FA (equal to `(k−2)·w` for k terms of width `w`), followed by one CPA of the result width. A 32-input popcount therefore costs about 26 FA, not `30 × 6` FA.

### 3.2 Comparators and bitwise operations

| Operation | min | typ | max |
|---|---|---|---|
| EQ/NE, n bits | `n·XOR2 + (n−1)·0.55` | `n·XOR2 + (n−1)·0.75` | `3.33·n` |
| EQ with constant | `(n−1)·0.55` | `(n−1)·0.75` | `(n−1)·1.33` |
| LT/LE/GT/GE | `2.7·n` | `3.5·n` | `σ·5.0·n` |
| LT with constant | `1.0·n` | `1.33·n` | `2.0·n` |

Signed comparison adds one XOR2. Reduction AND/OR costs 0.55 / 0.75 / 1.33 GE per merged input; reduction XOR costs 1.67 / 2.0 / 2.33. Bitwise operations cost `w·P[gate]`; a NOT absorbed into neighbouring logic costs 0 (typ uses `0.5·P[INV]`). The `logic` scale of typ (§5) applies to AND/OR/NAND/NOR and to reduction AND/OR, which synthesis merges into complex gates. It does not apply to XOR, XNOR and reduction XOR (they cannot be merged into AOI/OAI cells): those cost their full gate count in typ.

### 3.3 Selection

| Operation | min | typ | max |
|---|---|---|---|
| MUX k:1, binary select, width w | `(k−1)·w·1.17` (MUX4 cells) | `(k−1)·w·MUX2` | `σ·typ + ⌈w/8⌉·BUF` per select bit |

2:1 mux bit identities (per bit, applied before costing; each is an exact Boolean identity): equal arms → wire; one constant arm → AND2/OR2; both constant → wire or INV(sel); arms `x` and `~x` (a NOT/`^1`/`~^0` of the other arm) → `s ? ~x : x = x ^ s` = XOR2 (2.0 GE, not MUX2 2.33); one arm equal to the 1-bit select → `s ? s : x = s | x`, `s ? x : s = s & x` → AND2/OR2. The NOT feeding such an XOR keeps its own (absorbed, 0.5·INV) cost.
| PMUX k inputs, one-hot select (plus default) | `w·((k+1)·AOI22/2 + (h−1)·0.55)` | `w·((k+1)·AOI22/2 + (h−1)·0.75)` | `k·w·MUX2` |
| PRIORITY chain of k | PMUX + `(k−1)·AND2` | `0.9·k·w·MUX2 + (k−1)·AND2` | `k·w·MUX2` |
| DECODE s → 2^s | `2^s·NOR2` + predecode `2·2^(s/2)·1.33` | `2^s·AND2` + predecode | `2^s·(s−1)·AND2` |
| DECODE, `s ≤ 4`, direct form | `2^s·NOR_s + s·INV` (`NOR_s` = NOR2/NOR3/NOR4) | same | — |

PMUX: each of the `k+1` arms (the `k` selected inputs and the default) costs half an AOI22 (an AOI22 serves two arms), and the `h = ⌈(k+1)/2⌉` AOI outputs are merged by a NAND tree whose cost per merged input is the comparator-chain cost (0.55 min, 0.75 typ). The architecture label is `aoi22` (it was `ao22`). The direct DECODE form replaces the predecoded form in min and typ when it is cheaper (label `direct_nor`).

For `case` statements the select decode is costed separately, as `k` EQ-with-constant comparators; structural hashing shares common sub-terms.

### 3.4 Shifters (data n bits, amount s bits, `Ls = min(s, L)`)

- Logical shift, typ: `Σ_{j<Ls} [(n − 2^j)·MUX2 + 2^j·AND2]`, plus, when `s > L`, an OR-reduction of the excess amount bits and `n·AND2` masking.
- Arithmetic shift and rotate: `n·Ls·MUX2`.
- min = `0.85·typ`; max = `σ·typ + Ls·⌈n/8⌉·BUF`.
- Attribute `tz` (known-zero low bits of the amount): the shift is by a multiple of `2^tz`, so the first `tz` levels do not exist; every `Ls` above counts `Ls − tz` levels (`s = 3` bits with `tz = 2`: one level).
- Attribute `wa` (data width, only for `SHR`/`SAR` with `wa > n`): `n` output bits are selected out of a wider word, as in `d[s*W +: W]`. The levels run `j = tz..⌈log2 wa⌉−1`; at each level a bit whose source lies above the word is an AND2 (zero fill), every other bit a MUX2, and the number of bits needed grows by `2^j` towards the input (at most `wa`). `d[s*W +: W]` (`wa = k·W`, `tz = log2 W`) costs `(k−1)·W·MUX2`. For `SAR` every selected bit is a MUX2. With `wa = n` and `tz = 0` the formulas above apply.
- A shift by a constant costs 0. `1 << x` costs DECODE(s).

### 3.5 Multipliers (n × m, n ≥ m)

- **Array:** partial products `n·m·AND2`, plus `(n·m − n − m)·FA + n·HA`. The array ends in a ripple carry-propagate chain of FA.
- **Booth radix-4 with Wallace tree:** `R = ⌈(m+1)/2⌉` rows of `n+2` bits. Cost = `R·(n+2)·3.5` (selectors) + `⌈m/2⌉·6` (encoders) + `(R·(n+2) − 2·(n+m))·FA` + `CPA(n+m)`. An `m`-bit operand is also a valid `(m+1)`-bit Booth operand, so the candidate is `booth_pair = min(Booth(m), Booth(m+1))`, which keeps it non-decreasing in `m`.
- **Scenarios:** min = smaller of array and Booth with a ripple CPA (label `booth_wallace` when Booth wins). typ with the default `arch.mul.mid = "array"` (plain partial products, the default of generic synthesis; Yosys does not Booth-recode unless asked) = array + `α · max(0, CPA_mid(n+m) − CPA_ripple(n+m))`, the array moving towards a faster final adder by `α` (shown as the pseudo-primitive `CPA_ALPHA` in the breakdown). With `arch.mul.mid = "booth_wallace"` typ is the former rule: the smaller of the array and Booth with an `α`-interpolated CPA (`m ≥ booth_min_width`). max = `σ·` (Wallace tree, non-Booth partial products, Kogge–Stone CPA).
- Signed (Baugh–Wooley) adds `(t)·INV + 2·HA` (`t` = product bits). In typ (unless Booth is chosen) and max, a signed product also adds `signed_ext · array` (default `signed_ext = 0.15`): synthesis without Baugh–Wooley recoding sign-extends the operands to the product width and the array grows. This factor is **empirical** (Yosys: 8×8 signed / unsigned = 1.23); `signed_ext = 0` gives a pure Baugh–Wooley cost. min does not include it. An operand counts as signed when the builder flagged it or when it is *sign-extended* (its top bits replicate a non-constant net bit): the elaborator extends signed operands to the context width before the MUL node exists, so the flag alone would be lost. Fused `a*b + c` (MAC) carries the same flag. A square (`a*a`) costs `0.55×` a general multiply.
- **Truncated product** (only the low `t` bits demanded): scale partial-product and FA terms by the fraction of the array below column `t`, about `t(t+1) / (2nm)` for `t ≤ m`.
- **Constant multiplication:** write K in canonical signed-digit (CSD) form with `h` non-zero digits. Cost is 0 when `h ≤ 1`; otherwise typ = `(h−1)·ADD_typ(n + ⌈log2|K|⌉ − d_min)`, with `d_min` the smallest digit spacing; min = `0.7·typ`; max = `(h−1)·ADD_max(n + ⌈log2|K|⌉)`. Negative digits are subtractions.
- **POW** with constant exponent `e`: `⌊log2 e⌋ + popcount(e) − 1` multiplications at their effective widths. `2**x` costs DECODE.

### 3.6 Divider and modulo (n dividend bits, m divisor bits)

- Cell count: row `i` (one quotient bit) works on a partial remainder with only `i+1` significant bits, because the dividend is zero-extended; the array is therefore triangular while `i+1 < m` and rectangular afterwards: `T = Σ_{i<n} min(i+1, m)` subtract/select cells (`T = n(n+1)/2` for `n ≤ m`). The upper divisor bits of the first rows only need a shared "upper bits non-zero" OR chain (`m` inputs at the reduction-OR typ cost).
- min: restoring cell with the select absorbed (label `restoring_merged`): `T·FA` + OR chain.
- typ: restoring, `div_typ · [T·(FA + AND2) + n·MAJ3 + OR chain]` with `div_typ = 1.0`. The restore select is merged into the sum bit, `r ^ (q & d)`, so a row cell is an FA and an AND2 (not a MUX2); one borrow-out (MAJ3) per row sets the quotient bit. `div16` (16/16) = 1.14k GE (Yosys + Nangate 45: 1.16k; the previous formula `1.2·[(T+n)·FA + T·MUX2 + chain]` gave 1.37k, the rectangular `n·(m+1)` array 2.46k).
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

- **Flop-based** (read ports whose address is a constant are plain word taps and add no read mux):
  - typ = `D·W·(DFF + MUX2) + R·(D−1)·W·MUX2 + P_w·DECODE(log2 D) + (P_w−1)·D·W·MUX2`
  - min = `D·W·(DLAT + ICG/W) + R·(D−1)·W·1.17 + DECODE`
  - max = `σ·typ`
- A synchronous read port on a flop-based array adds a register of `W` flops per read port (`R·W·DFF`, in min and typ; max scales typ). The macro formula already contains its read register.
- **Macro** for synchronous-read memories with macros enabled in the profile. The smallest macro has `macro_min_bits` bits (default 4096) and 16 words, so a smaller memory is costed as that smallest macro (`D' = max(D, 16, ⌈macro_min_bits/W⌉)`), and **each scenario is the minimum of the flop array and the macro**: `cost = min(flops(D, W), macro(D', W))`. Both operands are non-decreasing in `D`, hence so is their minimum — there is no cliff at the threshold (127×32 and 128×32 both cost ≈ 6.9k GE; the old hard switch went 42k → 6.9k). Below ≈ 700 bits flops win, above the macro wins. Cost `c0 + c_bit·D·W + c_row·D + c_col·W` (defaults 3000, 0.6, 4, 30); min = 0.5×, max = 2×. Category `memory_macro`; confidence is capped at `low` unless the profile supplies a `(D, W) → GE` table, which is bilinearly interpolated.
- **ROM** (`D × W` constant table; also a constant array read with a signal index, a `case` that maps constants to constants, a VHDL constant array, and an array filled by a Verilog `initial` block and never written; an array loaded with `$readmem*` is a ROM of unknown contents): constant columns cost 0; columns equal to an address bit or its complement cost 0 or INV; duplicate and complemented columns are shared. typ is the cheaper of two textbook implementations of the remaining `c` columns: **predecoder + OR planes** `1.33·(2^⌊s/2⌋ + 2^⌈s/2⌉) + Σ_col min(ones, zeros)·0.55` with `s = ⌈log2 D⌉` (only the predecoder is counted: the NAND3/NAND4 first-level gates of each plane take the predecoded lines directly, so the `2^s` word lines of a full decoder are never built; NAND4 first level plus OR tree per merged input; random contents have `D/2` set words per column), and **per-column two-level logic** `0.75 GE per literal` of a minimal sum of products (Quine–McCluskey for `log2 D ≤ 6`, `D·log2 D/6` literals for random data or larger tables). For random data this is ≈ 0.3 GE/bit for large ROMs (the previous `2.0·D/log2 D` per column gave 0.2 GE/bit, about 1.5× low). min = `0.5·typ`, max = `c·(D/2)·MUX2`. Each ROM read port is one such ROM (+ `W` flops when synchronous). A memory that is never written and not initialised is not a ROM: it is costed as an array of unknown nets.

## 4. Reducing over-estimation

Naive per-operator costing over-estimates what synthesis produces. The IR and cost evaluation implement the following, all in linear time. A general logic optimizer is explicitly out of scope.

1. **Bit-level constness.** Each net carries a known-bit mask `(kmask, kval)` and a sign-replication marker. Before costing: a MUX2 with one constant input becomes AND2/OR2; identical inputs become a wire, and so do the individual bits of a MUX whose two arms resolve to the same net bit through `^ 0`, `& 1`, `| 0` or `~^ 1` (the `if (x) n = n ^ K` idiom of CRCs and LFSRs needs selectors only where `K` is 1); both inputs constant yields a wire or INV(sel); a known-zero operand bit in an ADD makes that bit a wire.
2. **Demanded bits.** A backward pass from outputs, register D pins, and memory ports yields an MSB/LSB demand per operation. Effective ADD width is `min(demand, max(w_a', w_b') + 1)`, where `w'` strips known-zero and sign-replicated MSBs. MUL widths strip trailing zeros (they become shifts) and known-zero MSBs.
3. **Dead and duplicate logic.** Logic without a transitive path to an output port, black box / instance, or an *observed* memory read costs 0. This holds for state too: a register (or a whole register chain or self-incrementing counter) whose value reaches no output is removed, and a memory with no observed read port is removed together with its writers. Removal is reported by the info diagnostic `UNOBSERVABLE_LOGIC` (`N unobservable registers/memories and M unobservable operations removed`, `affects_cost = false`) so that adding a counter nobody reads is understood to change nothing; operations are only counted when the designer named them (an unused `wire`), not the temporaries of lowering.  A register whose D input is a constant equal to its reset value costs 0. Structural hashing on `(op, operand ids, constants, attributes)` merges common subexpressions and equivalent flops (merged in min/typ, retained in max).
4. **Datapath fusion.** Chains of ADD/SUB/NEG/MUL whose intermediates have fanout 1 become one `SUM` cluster: `a*b + c` folds into the multiplier reduction tree (about `w·FA`); `a+b+c+d` becomes a CSA tree plus one CPA. min and typ use the fused cost; max is the larger of two consistent scenarios: fused at the tightest timing, or not fused at moderate timing (§3.1). Stacking "no fusion" on "fastest adder" is not done: it reached ×3.5–4 the typ cost.
5. **Resource sharing across exclusive branches.** MUX/PMUX trees built from one `if`/`case`/`?:` within one always block or assign are tagged. For leaves that are same-class arithmetic operations (MUL, DIV, ADD/SUB with `w ≥ 8`): `shared = max_i cost(op_i) + (k−1)·(w_a + w_b)·MUX2 − (k−1)·w_o·MUX2`. It applies when `shared < unshared`. Policy `resource_sharing = "auto"` uses it in typ; min uses the smaller of the two; max uses unshared. Sharing is never applied across always blocks or modules.
6. **Enable recognition.** `if (en) q <= d;` becomes the register's enable, not a separate MUX.
7. **Constant ports.** A constant tied to an instance port is propagated into a uniquified instance (`propagate_port_constants`, default on).

## 5. Error sources and confidence

| Source | Typical magnitude | Treatment |
|---|---|---|
| Library ratios (DFF/NAND2, XOR, FA) | ±20% per class | profile primitives (user-overridable) |
| Timing constraint → architecture | arithmetic ×1–3 | min/typ/max scenarios; fixed `α_c` |
| Boolean optimization of glue logic | −10 to −30% | class `logic` scale (default 0.85 in typ; not applied to XOR/XNOR/RED_XOR) |
| Buffering, sizing, hold fixing | +5–15% | global `overhead` (default 1.0, range 1.0–1.15) |
| Hierarchy boundaries / constant propagation | design-dependent | §4.7; diagnostic when disabled |
| DFT, clock gating, retiming | ±10–20% on sequential logic | explicit policy switches |
| Memories, ROM, DIV | ±50% or more | `quality = poor` |

### 5.1 Combining errors

Each operation has a typ value `typ_i`, a correlated systematic band `[lo_i, hi_i]` from the scenarios, and an independent residual `σ_i = typ_i · rms_c`, where `rms_c` is a fixed per-quality-class constant of the profile (defaults: 0.15 for good, 0.25 for medium, 0.5 for poor classes).

- **Deltas are computed per scenario**: `Δ_s = Σ cost_s(after) − Σ cost_s(before)`. The tool must never compute `max(after) − min(before)`; interval subtraction inflates ranges excessively. Unchanged operations cancel exactly.
- Range: with `lo = min_s Δ_s`, `hi = max_s Δ_s`, `σ = √(Σ σ_i²)` (changed operations only), the range is `(lo+hi)/2 ∓ √(((hi−lo)/2)² + (2σ)²)`. The systematic spread and the residual add in quadrature, not linearly. The range is centred on the scenarios and always contains `[lo, hi]`, hence `Δ_typ`. `σ = 0` gives `[lo, hi]`; `lo = hi` gives `lo ∓ 2σ`. It is never wider than the linear sum `[lo − 2σ, hi + 2σ]`. Integer arithmetic rounds the bounds outwards.
- Matched operations (same operation before and after, resized, memories included) have a correlated residual: `σ = rms · |n_a·typ_a − n_b·typ_b|`, with the more pessimistic `rms = σ/typ` of the two sides (`n` = instance count). It falls back to `√(σ_b² + σ_a²)` if neither side has `typ > 0`. Added and removed operations keep an independent `σ`. See DIFF.md.

### 5.2 Confidence derivation

Let `r = (Δ_max − Δ_min) / (2 · max(|Δ_typ|, A_floor))` with `A_floor = 20 GE`.

- **high**: no unsupported construct in the changed cone; at least 90% of `|Δ|` in good-quality classes; `r ≤ 0.2`.
- **medium**: no unsupported construct in the changed cone and `r ≤ 0.5`.
- **low**: anything else. Unknown black boxes, a macro without a table, or a poor-quality share above 30% of `|Δ|` force `low`.

`r` is exposed in the JSON output so the caller can see why.

When `r > 0.2` and nothing else explains the level, the reason is `delta range is +/-r% of the estimate` if the range is symmetric around `typ`, else `delta range is -x%/+y% of the estimate` with `x = (typ − min)/b`, `y = (max − typ)/b`, `b = max(|typ|, 20 GE)` (so `r = (x+y)/2`). The same wording applies to `cost` and `estimate`.

Limits that remain: a delta made of a single operation of medium quality cannot be `medium` (its `2σ` term alone gives `r = 0.5` and any scenario spread adds to it), and a single `good` operation cannot be `high` (`2σ` gives `r = 0.3 > 0.2`).

### 5.3 Validation

The default profile was calibrated against one reference: Yosys 0.70 (`synth -flatten`, `dfflibmap`, `abc -liberty`, no timing constraint, `opt_clean -purge`, `stat -liberty`) mapped on the Nangate 45 nm Open Cell Library (typical corner, X1 cells), areas converted to GE with `NAND2_X1 = 0.798 µm²`. The reference is frozen in `tests/reference/yosys_nangate45.csv` (144 cases; Yosys 0.70+post sha `0c11ea09`, 2026-10-25); `scripts/calibration_bench.py` regenerates the 131 micro-benchmarks and compares them with the estimate (see `CONTRIBUTING.md`). Error = `(typ − yosys) / yosys`; coverage = share of cases where the Yosys area lies in `[min, max]`.

| Scope | n | MAPE % | bias % | coverage % |
|---|---|---|---|---|
| all micro-benchmarks | 131 | 8.2 | +0.1 | 87 (92 at ±0.5% rounding) |
| all 144 cases | 144 | 9.1 | +1.7 | 84 (90 at ±0.5% rounding) |
| complete designs and corpus | 13 | 18.4 | +17.8 | 54 |

Per category, micro-benchmarks only:

| Category | n | MAPE % | bias % | coverage % |
|---|---|---|---|---|
| add | 8 | 3.4 | +1.5 | 88 |
| sub | 5 | 5.6 | +2.6 | 80 |
| cmp | 20 | 9.5 | +0.8 | 100 |
| bitwise | 12 | 20.0 | −20.0 | 100 |
| reduce | 10 | 5.1 | +4.9 | 100 |
| mux | 11 | 7.4 | −3.2 | 100 |
| decode | 2 | 6.1 | −6.1 | 100 |
| shift | 16 | 12.9 | +12.8 | 75 |
| mul | 11 | 8.1 | −6.7 | 100 |
| mac | 3 | 5.3 | −5.3 | 100 |
| div | 4 | 7.2 | +1.3 | 100 |
| sum | 3 | 4.0 | +4.0 | 0 |
| dff | 13 | 1.3 | −0.1 | 69 |
| shreg | 3 | 0.1 | +0.1 | 0 |
| counter | 3 | 20.9 | +20.9 | 67 |
| mem | 5 | 0.9 | +0.9 | 100 |
| rom | 2 | 4.2 | +3.8 | 100 |

Coverage is 0 for `shreg` and low for `dff` for a trivial reason: `min = typ` and the estimate is 0.06% above the Yosys area (`DFF = 5.67` against 5.667 measured), so the Yosys area falls just below `min` (at ±0.5% rounding they are covered). For `sum` the estimate is 4% high and `min` is above the Yosys area. In `bitwise`, XOR is exact, AND/OR are 15% low because of the glue-logic scale of 0.85 (an intended reduction that Yosys does not realise on isolated gates) and an isolated NOT is 50% low (the model costs an absorbed NOT at `0.5·INV`, §3.2; Yosys places one inverter per bit). On complete designs (examples and corpus) the estimate is high by +17.8% on average. The excess comes from patterns the IR does not recognise: a counter enable that is not merged into the incrementer (`counter`: +21%), rotations (`rol_w8`: +78%), and sign-extension flops. Fixing these is future work; the per-design figures are in the bench summary (`--frozen`).

Limits of this calibration:

- One technology node and library (Nangate 45 nm, X1 drive, typical corner) and one flow (Yosys/ABC, no timing constraint, no FA/HA cells, no memory macro). Nothing is measured against a commercial flow, another node, or a timing-driven run.
- VHDL designs are not measured (Yosys has no VHDL front-end here), and four Verilog/SystemVerilog designs (`corpus_cpu_datapath`, `corpus_mac_array`, `ex_sv_features`, `ex_udp_mux`) have no frozen reference.
- `σ` (sizing factor), `α` and the `rms` constants of §5.1 are not fitted to data; the `max` scenario and the confidence thresholds of §5.2 have not been tuned.
- Constant multiplication (`mul_const`: −25%), rotation and a counter with enable were left out of the fit.

To get closer to a commercial flow that infers adder cells and Booth multipliers, override `FA` (about 5.33 for `FA_X1`), set `arch.mul.mid = "booth_wallace"` and `signed_ext = 0`.

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
FA = 6.67                       # 5.33 for a library with a full-adder cell
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
mul = { min = "array", mid = "array", max = "wallace_ks", booth_min_width = 12 }   # mid = "booth_wallace": Booth in typ

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

**Reference sanity bands** (`generic-asic`, typ): ADD32 ∈ [170, 300]; MUL16 ∈ [1200, 2000]; MUL32 ∈ [4500, 8000]; 32-bit DFF bank ∈ [180, 260]; MUX 4:1 × 32 ∈ [200, 260]; 32-bit barrel shifter ∈ [330, 420]. They check the order of magnitude only. MUL16 (1901.5) and MUL32 (7901.8) are close to the upper bounds, and Yosys + Nangate 45 measures 2047 and 8616 GE, above them: the bands are not a statement about synthesis accuracy (§5.3). `tests/test_calibration_reference.py` holds the finer guard against the frozen reference.

## 8. Implementation pitfalls

1. Compute deltas per scenario (§5.1); interval subtraction is the most likely implementation error.
2. Use integer milli-GE and a fixed evaluation order. Use `int.bit_length` for `⌈log2⌉`.
3. Avoid common double counts: the enable-feedback MUX; an `always_comb` default assignment followed by a `case` (one PMUX, not a chain); `casez`/`casex` don't-cares, which reduce compare literals and must not be costed as full EQ.
4. Unintended latches are costed as DLAT with a warning diagnostic. `x` assignments are don't-cares and allow folding; min and typ may treat them as wires.
5. Keep the class set small. Random glue logic is the least predictable class and must be labelled as such.
6. Do not build a logic optimizer. Bit-level constness, demanded bits, structural hashing, and the three pattern rules (sharing, fusion, enable) capture most of what synthesis recovers, in linear time.

## 9. Implementation notes and deviations (stage 2)

The implementation (`src/gatecost/cost/`) follows the formulas above. The following points were left open or had to be adjusted so that the exact-value, monotonicity, and sanity-band requirements of section 7 hold together.

1. **Coefficients and factors are profile data.** The numeric constants of section 3 that are not primitives (`0.55`, `0.75`, `1.17`, `3.33`, `2.7`, `3.5`, `G = 1.33`, `3.5`/`6` of Booth, ...) live in the profile tables `[coefficients]` (GE) and `[factors]` (dimensionless: `0.85`, `0.7`, `div_typ = 1.0`, `2.5`, `signed_ext = 0.15`, `0.55` for squares, `rms_good/medium/poor`, ...); `[alpha]` maps the `timing` policy to the default `α` (relaxed 0, moderate 0.4, aggressive 1). `Profile.scaled(f)` scales primitives, coefficients, macro parameters, and black boxes, so the "scaling" property holds. A profile that omits a table other than `[primitives]` inherits it from `generic-asic`.
2. **Overhead and σ.** `overhead.max` (default 1.15) is applied to the max scenario of every formula whose max column carries no sizing factor σ of its own; `overhead.typ` (default 1.0) is applied to typ everywhere. This avoids multiplying σ and the overhead for the same effect.
3. **Multiplier architecture selection.** min is `min(array, Booth with ripple CPA)` for every `m`. typ follows `arch.mul.mid`: with the default `"array"` it is the carry-save array plus `α·max(0, CPA_mid − CPA_ripple)` (no Booth, §3.5); with `"booth_wallace"` it is `min(array, Booth with α-interpolated CPA)` (the `booth_min_width` gate then makes typ drop when `m` crosses it). `arch.mul.booth_min_width` is kept and defaults to 2. The Booth candidate is `booth_pair`, the cheaper of `m` and `m+1` bits (section 3.5). A multiplier with `m = 1` is a row of AND2. max is `σ·(AND2 partial products + Wallace FAs) + ADD_max(n+m)`.
4. **Ordering is enforced.** After the overhead factors, `min ≤ typ ≤ max` is imposed per operation (`min` and `max` are clamped to `typ`). Where a table's max column is smaller than typ for tiny sizes (PRIORITY, DECODE with `s = 2`) the clamp applies; PRIORITY max additionally includes the `(k−1)·AND2` chain.
5. **Constants.** An operation with a constant operand is capped, scenario by scenario, at the cost of its variable counterpart. `a ± K` with `K ≡ 0 (mod 2^n)` is free, a 1-bit constant add is an INV, `n − tz(K)` uses `K mod 2^n`; with `b = n − tz(K) ≥ 2` bits the cost is `INV + (b−2)·HA + XOR2` (section 3.1). An unknown non-zero constant (`const_unknown`) assumes `tz = 0` for adders, and for multipliers a CSD density of 1/3 (`h = ⌈(n+1)/3⌉` digits, spacing 2, width `n` bits); the estimate reports an `ASSUMED` line. Constant multiplication uses SUB for negative digits (adders = positive digits − 1, subtractors = negative digits; an all-negative form needs one NEG). A single negative power of two is a NEG. An optional `out` width caps the adder widths. The divisor `K` of `DIV`/`MOD` is a power of two: free (unsigned) or one ripple ADD (signed); otherwise reciprocal multiplication with `h = ⌈(n+1)/3⌉` digits at width `2n`, and `MOD` adds the constant multiplier back and one SUB.
6. **MUX.** For `k = 2` the min scenario equals typ (a MUX4 cell cannot implement a 2:1 mux). With one constant data input a 2:1 mux is `w·AND2`, with two it is at most `w` inverters, with identical inputs a wire.
7. **Shifters.** `SAR` with an amount wider than `⌈log2 n⌉` adds the OR-reduction of the excess bits and `n·MUX2` (sign fill). Rotates ignore excess amount bits. `1 << x` is DECODE(s).
8. **DECODE / ENCODE.** The predecode term is `(2^⌊s/2⌋ + 2^⌈s/2⌉)·1.33`, equal to `2·2^(s/2)·1.33` for even `s`; a 1→2 decoder is an INV. For `2 ≤ s ≤ 4` the direct form `2^s·NOR_s + s·INV` replaces it in min and typ when cheaper (label `direct_nor`). ENCODE (not tabulated in section 3) is `⌈log2 k⌉·(⌈k/2⌉−1)` OR gates, plus a `(k−1)·AND2` chain for a priority encoder.
9. **Logic scale.** The `logic` scale (0.85) applies to typ of AND/OR/NAND/NOR and of reduction AND/OR (not to NOT, which has its own 0 / 0.5·INV / INV rule, and not to XOR, XNOR and reduction XOR, which cannot merge into complex gates); min is then clamped to typ.
10. **Registers.** `scan` adds `SDFF_ADD` per bit in every scenario. Enable: min is the cheapest of EDFF, gated bank (`w·DFF + ICG`, only if `w ≥ icg_min_width`), and DFF+MUX2; typ follows `enable_style` (`icg` falls back to EDFF below `icg_min_width`); max is DFF+MUX2. Asynchronous reset or set adds `DFFR − DFF` per bit (also with EDFF). Sync reset and enable add up.
11. **Memories.** (See also §3.8: the flop/macro choice is now a per-scenario minimum, so the cost is monotone in `D`; the monotonicity property is verified for depths 1..4096.) `log2 D` for the decoder uses `⌈log2 D⌉`. The macro model ignores the port counts. A profile `macro_table` that forms a complete `(D, W)` grid is interpolated bilinearly (linear extrapolation at the edges); its min/max are `0.9×`/`1.1×` and no `macro_no_table` flag is raised. Macros are reported as category `mem` with architecture `macro`. The switch from flop-based to macro makes the cost of a synchronous memory non-monotone across the `macro_min_bits` threshold; the monotonicity property is therefore verified for flop-based memories.
12. **ROM.** The `D·log2 D/6` literal estimate uses a piecewise-linear `log2` (exact at powers of two) to stay monotone for non-power-of-two depths; the decoder term is the predecoder only, `1.33·(2^⌊s/2⌋ + 2^⌈s/2⌉)` with `s = ⌈log2 D⌉` (the word lines are not built; not scaled by `D/2^s`, which would make the cost step down at powers of two). With `contents`, columns are classified (constant, address bit, complemented address bit, duplicate, complemented duplicate) and, for `⌈log2 D⌉ ≤ 6`, the literal count comes from a Quine–McCluskey cover of the column or of its complement, with unwritten addresses as don't-cares. Each complemented column costs one INV in all scenarios.
13. **Fusion and sharing helpers.** `SUM` max is the larger of `σ_arith·CSA + ADD_max(out)` (fused, tight timing) and the unfused chain of typ adders (moderate timing, widest operand first, exact widths); the bit-heap bound scales max like typ. Ratio max/typ ≈ ×1–3. `MAC` is `MUL + FA·min(w_c, n+m) + HA·max(0, w_c−n−m)` (max: larger of `MUL_max + σ_arith·(FA and HA rows)` and `MUL_typ + ADD_typ(max(n+m, w_c))`; extra addends add `w_c·FA` each in min and typ). `shared_leaf_cost` returns the value that *replaces* the sum of the leaf costs: `max_i cost_i + (k−1)(w_a+w_b−w_o)·MUX2` for min and typ when smaller than the unshared sum, and the unshared sum for max.
14. **Confidence of `estimate`.** For an estimate, the range is the scenario band `[min, max]` (the independent residual of section 5.1 is only added for deltas, by `delta_range`), and `r = (max − min) / (2·max(typ, 20 GE))`. `delta_range` uses `min_s Δ_s` and `max_s Δ_s`, so `Δ_min ≤ Δ_typ ≤ Δ_max` always holds.
15. **Breakdown.** `breakdown` counts the primitives (and a few pseudo-primitives such as `PREFIX`, `BOOTH_SEL`, `AND_IN`) of the typ architecture before sizing and overhead factors; for α-interpolated typ the counts are interpolated too.

16. **Fused sums of narrow terms (bit-heap bound).** `sum_terms_op` caps min and typ at `(Σt_i − out)·FA + out·HA`: every FA removes one bit of the heap, so compressing `Σt_i` bits to the `out` result bits needs at most that many cells. "Two rows + CPA" over-counts for terms much narrower than the result (popcount of 8 bits: 34.6 GE instead of 54.5; popcount32 158.6 instead of 195.5).

Reference values with `generic-asic` (typ, GE): ADD32 223.8, SUB32 245.2, MUL16 1901.5, MUL16 signed 2212.8, MUL32 7901.8, DIV 16/16 1137.3, 32-bit DFF bank 181.4, MUX 4:1×32 223.7, barrel shifter 32 (logical) 341.8, rotate 372.8. (Before the Yosys calibration: ADD32 214.2, MUL16 1351.5, MUL32 4952.6.)

## 10. Implementation notes (cost-tree builder, `cost/build.py`)

1. Zero-cost operations are not emitted, except non-surviving members of a shared set (kept with tags `shared`, `shared_zeroed` for attribution). Sharing puts the whole shared cost, per scenario, on the most expensive leaf.
2. Duplicate flops: min/typ cost once, max is `merge_count` times. A fused `SUM` containing a multiplier is `MAC` (attribute `extra` = number of further `w`-bit addends: `extra·w` more FA; max is the larger of `MUL_max + σ_arith·(rows of FA and HA)` and `MUL_typ + ADD_typ(max(n+m, w)) + extra·ADD_typ(w)`; the breakdown counts `min(w, n+m) + w·extra` FA and the columns above `n+m` as HA). With `datapath_fusion = false` a `SUM` is costed from its member nodes.
3. Instances of modules absent from the design are black boxes costed from the profile `[blackbox]` table (zero plus `MISSING_MODULE` otherwise); their category is `blackbox` and appears in `by_category` only when non-zero.
4. `cost` reports per-instance `own`/`total` for each module; `--by file` replaces the module rows by per-file rows; `--by category` omits them. Unknown top: with several candidate tops each is costed, the largest is reported and the others appear as `ASSUMED` lines.
5. A node the builder cannot cost (an operation outside the IR set, a missing or invalid attribute: an IR/cost-model invariant breach, not a property of the input) costs 0 and adds an `INTERNAL` diagnostic (`error`, `affects_cost = true`) on that node; its module counts as unsupported for the confidence. The rest of the design is costed normally instead of the whole run ending in `ERROR INTERNAL`. `GATECOST_ELAB_DEBUG=1` re-raises the exception.
