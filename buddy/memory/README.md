# Cognitive Memory Consolidation Engine v4.1-patched

> Research-grade, biologically-grounded long-term memory for conversational AI.
> Every mechanism maps to a peer-reviewed paper. Every parameter has a reason.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [Memory Entry Structure](#2-memory-entry-structure)
3. [Memory Tiers](#3-memory-tiers)
4. [Scoring Pipeline](#4-scoring-pipeline)
5. [Cluster Building & Sleep-Phase Weighting](#5-cluster-building--sleep-phase-weighting)
6. [Hard Deletion](#6-hard-deletion)
7. [SleepBudget — Parameters](#7-sleepbudget--parameters)
8. [SleepReport — Observability](#8-sleepreport--observability)
9. [Phenomena Test Suite](#9-phenomena-test-suite)
10. [Time-Range Test Suite](#10-time-range-test-suite)
11. [References](#11-references)

---

## 1. Architecture

```
run_consolidation()
├─ PHASE 0   SCAN    — Load ≤300 candidates. Build vector neighbor map (top-20).
├─ PHASE 0b  SCORE   — BLA + spreading + arousal + surprise + TG + PI → strength [0,1]
├─ PHASE 1   REPLAY  — Cluster near-dups. REM-weighted priority. LLM summarise → long mem.
├─ PHASE 2   TIERS   — Promote/demote based on strength.
├─ PHASE 3   PRUNE   — Hard-delete dead traces, redundant copies, interference victims.
└─ PHASE 4   CYCLES  — Increment consolidation_cycles (CLS gate).
```

---

## 2. Memory Entry Structure

| Field                  | Type          | Description                                                  |
| ---------------------- | ------------- | ------------------------------------------------------------ |
| `id`                   | `str`         | UUID                                                         |
| `text`                 | `str`         | Raw text                                                     |
| `embedding`            | `np.ndarray`  | Float32 vector                                               |
| `importance`           | `float [0,1]` | Static importance at encoding                                |
| `memory_type`          | `str`         | `flash` / `short` / `long`                                   |
| `access_count`         | `int`         | Total retrievals                                             |
| `created_at`           | `float`       | Unix timestamp                                               |
| `last_accessed`        | `float\|None` | Last retrieval                                               |
| `source_turn`          | `int\|None`   | Conversation turn at encoding                                |
| `consolidated_into_id` | `str\|None`   | Summary this was merged into                                 |
| `deleted`              | `int`         | 0 = live, 1 = soft-deleted                                   |
| `metadata`             | `dict`        | `consolidation_cycles`, `is_summary`, `is_provisional`, etc. |

---

## 3. Memory Tiers

```
FLASH ──(M≥0.55 OR I_dyn≥0.70)──▶ SHORT ──(M≥0.72 AND cycles≥2 AND sim≤0.60 AND I_dyn≥0.30)──▶ LONG
  ◀──(M≤0.28 AND days>14)──────────             ◀──(M≤0.25 AND days>60 AND I_dyn≤0.45)──────────
```

Flash promotion requires age > 1h. Long demotion blocked if `I_dyn > 0.70`.

---

## 4. Scoring Pipeline

### 4.1 Petrov BLA [P1]

```
B_i ≈ ln( Σ_{j=1}^{3} t_j^(-d_eff) + (n-3)×integral_approx )

d_eff = d × (1 − α × I)    d=0.5, α=0.40
bla_norm = sigmoid(B_i)

Access times reconstructed by linear interpolation between created_at and last_accessed.
```

![Petrov BLA](../../assets/memory_test_graphs/petrov_bla.png)

### 4.2 Dynamic Importance [P7]

```
I_dyn = I_0×exp(−λ×t_age) + w_acc×min(1,(acc/t_age)×30) + w_ar×arousal(m)
λ=0.003/day,  w_acc=0.35,  w_ar=0.15

source_turn ≤ 3  →  λ_eff = λ×1.3   (primacy penalty)
```

![Dynamic importance](../../assets/memory_test_graphs/dynamic_importance.png)

### 4.3 Spreading Activation / Fan Effect [P2]

```
S_ji = S − ln(fan_j)    S=1.5
A_spread = Σ (W/N)×S_ji×sigmoid(B_j)    clamped [−0.20, +0.30]
Crossover at fan ≈ exp(1.5) ≈ 4.5
```

![Fan effect](../../assets/memory_test_graphs/fan_effect.png)

### 4.4 Emotional Arousal Amplifier [P5]

```
arousal = min(1, 0.50×I + 0.30×min(1,keywords/3) + 0.12×min(1,CAPS/3) + 0.08×min(1,punct/3))
amplified = combined × (1 + 0.50×arousal)
```

68 ANEW-validated keywords spanning trauma, loss, urgency, and high-stakes life events.

![Arousal](../../assets/memory_test_graphs/arousal.png)

### 4.5 Prediction Error / Surprise [P6]

```
Fires when: sim_max ≥ 0.55  AND  text matches contradiction pattern
amplified += 0.15
```

Patterns: `not | no longer | cancelled | fired | quit | actually | correction | wrong | changed | failed | never | deprecated | replaced | corrected` (+ others)

### 4.6 Temporal Gradient — 24h Bump [P9]

```
TG = 0.04 × exp(−(t_age − 86400)² / (2×21600²))
amplified += TG    [additive; <0.001 by 7 days]
```

![Ebbinghaus](../../assets/memory_test_graphs/ebbinghaus.png)

### 4.7 Proactive Interference [P11]

```
PI = −Σ min(0.15, sim×r×Δt_j)    r=0.001/day,  total cap=−0.15
amplified = max(0, amplified + PI)
Only newer memories cause PI on older ones.
```

### 4.8 Importance Floor & Clamp

```
strength = max(f×I_dyn, min(1.0, amplified))
flash/short: f=0.20  |  long: f=0.30  [PATCH-2]
```

---

## 5. Cluster Building & Sleep-Phase Weighting

```
w_sleep = 1.0 + 0.20×max_arousal(C)
P(C) = |C| × mean_I_dyn × (0.5 + 0.5×mean_strength) × w_sleep

I_summary = clip(0.6×salience + 0.4×mean_I_cluster, 0.35, 1.0)
provisional_expires_at = now + 14×86400  if LLM confidence < 0.35  [P12]
```

CLS gate — Short→Long requires `cycles ≥ 2` (incremented each cycle via `json_set` SQL).

---

## 6. Hard Deletion

**Forgetting guard [PATCH-1]:** `importance ≥ 0.80` AND `consolidated_into_id is None` → exempt from all deletion paths.

**Dead trace:** `acc==0 AND I_dyn≤0.15 AND age≥180d AND dup_count==0`

**Weighted redundancy:**

```
weighted_dup = dup_count × mean_sim ≥ 3
AND I_dyn≤0.25  AND acc≤2  AND age≥30d  AND strength≤0.30
```

4 dups sim=0.95 → 3.80 (pruned). 4 dups sim=0.74 → 2.96 (spared).

**Interference pruning:** Selects weakest neighbour as victim. Skips `I_dyn > 0.50`. Trigger must have `strength ≤ 0.40`.

---

## 7. SleepBudget — Parameters

| Parameter                    | Default   |     | Parameter                   | Default |
| ---------------------------- | --------- | --- | --------------------------- | ------- |
| `max_candidates`             | 300       |     | `flash_to_short_strength`   | 0.55    |
| `consolidation_cooldown_sec` | 86400     |     | `flash_to_short_imp`        | 0.70    |
| `top_k_neighbors`            | 20        |     | `short_to_long_strength`    | 0.72    |
| `tau_dup`                    | 0.80      |     | `short_to_long_max_sim`     | 0.60    |
| `max_cluster_size`           | 18        |     | `short_demote_strength`     | 0.28    |
| `max_summaries`              | 10        |     | `long_demote_strength`      | 0.25    |
| `max_hard_deletes`           | 50        |     | `long_protected_imp`        | 0.70    |
| `delete_dead_sec`            | 180×86400 |     | `min_cycles_for_long`       | 2       |
| `hard_delete_imp_protect`    | 0.80      |     | `provisional_window_days`   | 14.0    |
| `actr_d`                     | 0.5       |     | `reflective_confidence_min` | 0.35    |
| `imp_alpha`                  | 0.40      |     | `arousal_amplify_max`       | 0.50    |
| `dyn_imp_lambda`             | 0.003     |     | `surprise_boost`            | 0.15    |
| `spreading_S`                | 1.5       |     | `redundancy_dup_threshold`  | 3       |

---

## 8. SleepReport — Observability

`scanned` · `clusters_found` · `summarized` · `tier_updates` · `promoted` · `demoted` · `soft_deleted_after_summary` · `hard_deleted` · `redundancy_deleted` · `interference_pruned` · `provisional_summaries` · `arousal_boosted` · `prediction_errors_flagged` · `cycles_incremented` · `temporal_gradient_applied` · `proactive_interference_detected` · `errors`

---

## 9. Phenomena Test Suite

`python test_human_memory.py` — 12 phenomena · 46 assertions · zero external dependencies

![Dashboard](../../assets/memory_test_graphs/dashboard.png)

| #   | Phenomenon                    | Paper | Result                                              |
| --- | ----------------------------- | ----- | --------------------------------------------------- |
| 1   | Ebbinghaus forgetting curve   | [P13] | Power-law decay confirmed. 0.01d=0.177 → 180d=0.077 |
| 2   | Spaced repetition             | [P14] | 34 spaced → 82% stronger than 1 access              |
| 3   | Emotional arousal enhancement | [P5]  | Emotional 177% > routine at day 90                  |
| 4   | Prediction error / novelty    | [P6]  | 100% detection on 6 pairs; +0.056 boost             |
| 5   | Fan effect                    | [P2]  | fan=1: +0.30, fan=32: −0.20, crossover ≈4.5         |
| 6   | Dynamic importance drift      | [P7]  | Unused high-imp decays 60% in 1 year                |
| 7   | Tier promotion pipeline       | [P3]  | All 4 cases correct; CLS gate enforced              |
| 8   | Petrov BLA accuracy           | [P1]  | All 6 mathematical properties verified              |
| 9   | Serial position effect        | [P8]  | Recency effect confirmed                            |
| 10  | CLS cycle gate                | [P3]  | cycles<2 blocks long promotion                      |
| 11  | Cluster summarisation         | —     | 4/4 related memories clustered; unrelated excluded  |
| 12  | 500-memory stress test        | —     | Emotional 177% > routine; runtime <0.05s            |

![Spaced repetition](../../assets/memory_test_graphs/spaced_repetition.png)
![Serial position](../../assets/memory_test_graphs/serial_position.png)
![Stress test](../../assets/memory_test_graphs/stress_test.png)

```
46 / 46 PASS  |  <0.10s  |  No external dependencies
```

---

## 10. Time-Range Test Suite

Tests every mechanism at 8 age bands simultaneously — 1 Year down to 1 Hour.

`python test_time_range.py`

### Age Bands

| Band | Age      |     | Band | Age                 |
| ---- | -------- | --- | ---- | ------------------- |
| A    | 1 Year   |     | E    | 1 Week              |
| B    | 6 Months |     | F    | **1 Day** (TG peak) |
| C    | 3 Months |     | G    | 6 Hours             |
| D    | 1 Month  |     | H    | 1 Hour              |

---

### S1 — BLA Decay (22 assertions)

30-access > 1-access at every band. BLA strictly increases 1yr→1hr.

```
1-access:  1yr=0.0002  6mo=0.0003  1mo=0.0006  1d=0.0034  1h=0.0164  (16× stronger)
```

![BLA decay](../../assets/time_range_graphs/01_bla_decay.png)

---

### S2 — Dynamic Importance Drift (15 assertions)

Emotional > routine at every band. Routine strictly increases 1yr→1hr.

```
Routine (imp=0.3, acc=1):  1yr=0.152  →  1h=0.673
Emotional (imp=0.9, acc=3): 2–4× higher at every band
```

![Dynamic importance](../../assets/time_range_graphs/02_dynamic_importance.png)

---

### S3 — Temporal Gradient (8 assertions)

TG fires at 1-day band only, zero at ≥1 week, rising from 1h→6h→24h.

```
1yr–1wk = 0.000%  |  1d = 3.88% (peak)  |  6h = 0.27%  |  1h = 0.03%
```

![Temporal gradient](../../assets/time_range_graphs/03_temporal_gradient.png)

---

### S4 — Proactive Interference (19 assertions)

PI grows with age, capped at −0.15, near-zero for fresh memories. Setup: competitor at `age/4`, sim=0.82.

```
1yr=−0.150(cap)  6mo=−0.150(cap)  3mo=−0.122  1mo=−0.046  1wk=−0.011  1h≈0.000
```

![Proactive interference](../../assets/time_range_graphs/04_proactive_interference.png)

---

### S5 — Full Strength Scores (28 assertions)

Three profiles at every band. Emotional > routine, protected ≥ `0.30×I_dyn`.

```
              1yr    6mo    3mo    1mo    1wk    1d     6h     1h
Routine:     0.031  0.046  0.057  0.073  0.097  0.152  0.183  0.213
Emotional:   0.145  0.187  0.224  0.283  0.364  0.541  0.612  0.678
Protected:   0.117  0.188  0.239  0.297  0.341  0.451  0.481  0.519
```

Protected scores 0.117 at 1yr with acc=0. Correct — guard prevents **deletion**, not decay.

![Full strength](../../assets/time_range_graphs/05_strength_scores.png)

---

### S6 — Tier Eligibility (13 assertions)

- High-imp flash (`imp=0.85`, 20 acc) eligible for short promotion at all 8 bands
- Well-accessed short (`imp=0.75`, 50 acc, cycles=2) eligible for long at 1h/6h/1d
- Dormant long (`imp=0.3`, 0 acc) demotion-eligible at 1yr and 6mo

---

### S7 — Hard-Delete Eligibility (19 assertions)

- Dead-trace: eligible at 1yr/6mo; blocked at 1h/6h/1d/1wk/1mo (180-day gate)
- `imp=0.95` → protected at all 8 bands (unconditional)
- `consolidated_into_id` set → **not** protected
- 4 dups sim=0.91 → weighted=3.64 → eligible
- 4 dups sim=0.74 → weighted=2.96 → spared

---

### S8 — Fan Effect (7 assertions)

Strictly monotone. Positive at low fan, negative at high fan.

```
Fan=1 → +0.300  |  Fan=4 → +0.113  |  Fan=8 → +0.007  |  Fan=32 → −0.200
```

![Fan effect](../../assets/time_range_graphs/06_fan_effect.png)

---

### S9 — Source-Turn Gradient (8 assertions)

Turn-15 `I_dyn` ≥ Turn-1 at every band. Gap compounds over time.

```
I_dyn gap (T15−T1):  1yr=+0.044  6mo=+0.031  3mo=+0.020  1mo=+0.007  1d/6h/1h≈0.000
```

![Source-turn](../../assets/time_range_graphs/07_source_turn.png)

---

### S10 — Stress Test: 80 Memories (9 assertions)

10 profiles per band (imp 0.1→0.91, acc 0→27, flash/short/long). All in [0,1], emotional > routine, monotone oldest→newest.

```
1yr=0.119  6mo=0.157  3mo=0.177  1mo=0.194  1wk=0.231  1d=0.354*  6h=0.409  1h=0.533
* +53% jump at 1d = temporal gradient signature. 1h is 4.5× stronger than 1yr.
```

![Stress heatmap](../../assets/time_range_graphs/08_stress_heatmap.png)

---

### Results

![Time-range dashboard](../../assets/time_range_graphs/09_dashboard.png)

```
┌──────────────────────────────────────────────┐
│  TIME-RANGE SUITE     146 / 146 PASS         │
│  PHENOMENA SUITE       46 /  46 PASS         │
│  COMBINED             192 / 192 PASS         │
│  8 bands · 10 sections · 18 graphs · <0.15s  │
└──────────────────────────────────────────────┘
```

---

## 11. References

| Tag | Citation                                                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| P1  | Petrov, A. (2006). Computationally efficient approximation of the base-level learning equation. _Proc. 7th ICCM_, 292–297.                 |
| P2  | Anderson, J.R. & Reder, L.M. (1999). The fan effect. _JEP: General_, 128(2), 186–197.                                                      |
| P3  | McClelland, J.L., McNaughton, B.L., & O'Reilly, R.C. (1995). Complementary learning systems. _Psychological Review_, 102(3), 419–457.      |
| P4  | Robinson, N.T.M., et al. (2025). Large sharp-wave ripples and memory reactivation. _Cell_, 188(1).                                         |
| P5  | McGaugh, J.L. (2004). Amygdala modulates consolidation of emotionally arousing memories. _Annual Review of Neuroscience_, 27, 1–28.        |
| P6  | Friston, K. (2010). The free-energy principle. _Nature Reviews Neuroscience_, 11(2), 127–138.                                              |
| P7  | Anderson, J.R. & Schooler, L.J. (1991). Reflections of the environment in memory. _Psychological Science_, 2(6), 396–408.                  |
| P8  | Murdock, B.B. (1962). The serial position effect of free recall. _JEP_, 64(5), 482–488.                                                    |
| P9  | Murre, J.M.J. & Dros, J. (2015). Replication of Ebbinghaus' forgetting curve. _PLOS ONE_, 10(7).                                           |
| P10 | Walker, M.P. & Stickgold, R. (2004). Sleep-dependent memory consolidation. _Neuron_, 44(1), 121–133.                                       |
| P11 | McGeoch, J.A. (1942). _The Psychology of Human Learning_. Longmans, Green.                                                                 |
| P12 | Nader, K., Schafe, G.E., & LeDoux, J.E. (2000). Fear memories require protein synthesis for reconsolidation. _Nature_, 406(6797), 722–726. |
| P13 | Ebbinghaus, H. (1885). _Uber das Gedachtnis_. Duncker & Humblot.                                                                           |
| P14 | Cepeda, N.J., et al. (2006). Distributed practice in verbal recall tasks. _Psychological Bulletin_, 132(3), 354–380.                       |
| P15 | Warriner, A.B., et al. (2013). Norms of valence, arousal for 13,915 English lemmas. _BRM_, 45(4), 1191–1207.                               |
| P16 | Godden, D.R. & Baddeley, A.D. (1975). Context-dependent memory. _British Journal of Psychology_, 66(3), 325–331.                           |
