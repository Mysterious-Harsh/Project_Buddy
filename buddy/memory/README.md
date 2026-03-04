# Cognitive Memory Consolidation Engine v4

> A biologically-grounded long-term memory system for conversational AI, implementing 12 peer-reviewed neuroscience mechanisms as executable algorithms.

![Differential Dashboard](memory_test_graphs/diff_DASHBOARD.png)

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture: The Four Forces Model](#architecture-the-four-forces-model)
3. [Core Formula: Memory Strength](#core-formula-memory-strength)
4. [v3 → v4: What's New and Why](#v3--v4-whats-new-and-why)
    - [D1 Temporal Gradient](#d1-temporal-gradient--murre--dros-2015)
    - [D2 Proactive Interference](#d2-proactive-interference-penalty--mcgeoch-1942)
    - [D3 Sleep-Phase Cluster Weighting](#d3-sleep-phase-cluster-weighting--walker--stickgold-2004)
    - [D4 Source-Turn Decay Gradient](#d4-source-turn-decay-gradient--anderson--schooler-1991)
    - [D5 Similarity-Weighted Redundancy](#d5-similarity-weighted-redundancy-pruning)
    - [D6 Expanded Arousal Vocabulary](#d6-expanded-arousal-vocabulary)
    - [D7 Extended Provisional Window](#d7-extended-provisional-window--nader-2000)
    - [D8 SleepReport Telemetry](#d8-sleepReport-telemetry)
5. [Validated Cognitive Phenomena (v3 + v4)](#validated-cognitive-phenomena)
6. [Test Suite Architecture](#test-suite-architecture)
7. [Results at a Glance](#results-at-a-glance)
8. [References](#references)

---

## Overview

Standard AI memory is a key-value store. Every memory is equally "present" until deleted. This fails to model how humans actually experience the past: recent events feel vivid, emotional ones persist, routine ones fade, and conflicting information interferes with old beliefs.

This engine implements the computational neuroscience of memory consolidation — the biological process by which the hippocampus transfers experiences into durable neocortical storage during sleep. Every scoring function maps directly to a mechanism described in the experimental literature.

**Key properties:**

- Memories decay following Ebbinghaus power-law curves
- Emotional memories resist decay via amygdala modulation
- Surprising/novel inputs receive a dopamine-equivalent boost
- Heavily-associated concepts interfere with each other at retrieval
- Consolidation happens in a "sleep cycle" pass over stored memories
- New memories can retroactively weaken older competing ones (proactive interference)

---

## Architecture: The Four Forces Model

```
┌─────────────────────────────────────────────────────┐
│                  MEMORY STRENGTH                     │
│                                                      │
│   Force 1: BLA (recency + frequency)                 │
│   Force 2: Arousal boost (emotion)                   │
│   Force 3: Surprise boost (novelty)                  │
│   Force 4: Spreading activation (associations)       │
│                                                      │
│   v4 adds:                                           │
│   Force 5: Temporal gradient (24h consolidation)     │
│   Force 6: Proactive interference (competition)      │
└─────────────────────────────────────────────────────┘
```

Every `MemoryEntry` is scored at each consolidation cycle. Scores drive tier promotion (flash → short → long) and deletion decisions.

---

## Core Formula: Memory Strength

### Step 1: Base Log-Likelihood Approximation (BLA)

Petrov (2006) derives a computationally efficient approximation to the exact ACT-R base-level activation:

$$B_i \approx \ln\!\left(\sum_{j=1}^{n} t_j^{-d_{\text{eff}}}\right)$$

where:

| Symbol           | Meaning                                 |
| ---------------- | --------------------------------------- |
| $t_j$            | Time since the $j$-th access in seconds |
| $n$              | Total number of past accesses           |
| $d_{\text{eff}}$ | Effective decay rate (see below)        |

For large $n$, the exact sum is prohibitively expensive. Petrov's $k=3$ hybrid approximates it using the 3 most recent accesses plus an analytical closed-form tail:

$$B_i \approx \ln\!\left(\sum_{j=1}^{\min(n,3)} t_j^{-d_{\text{eff}}} \;+\; \frac{(n-3) \cdot t_{\text{mean}}^{1-d_{\text{eff}}}}{1 - d_{\text{eff}}}\right)$$

![BLA decay curves](assets/memory_test_graphs/petrov_bla.png)

### Step 2: Importance-Modulated Decay

The standard ACT-R decay rate $d$ is fixed. We modulate it by static importance $I \in [0,1]$:

$$d_{\text{eff}} = d \cdot (1 - \alpha \cdot I)$$

where $\alpha = 0.4$ by default. High-importance memories (lectures, medical history) decay slower than low-importance ones (casual greetings).

### Step 3: Sigmoid Normalisation

BLA scores are unbounded. We map to $[0, 1]$:

$$\text{bla\_norm} = \sigma(B_i) = \frac{1}{1 + e^{-B_i}}$$

### Step 4: Dynamic Importance

At runtime, importance is not static. It decays with disuse:

$$I_{\text{dyn}}(t) = I_0 \cdot e^{-\lambda \cdot t_{\text{since\_access}}}$$

where $\lambda$ is a base decay constant. This ensures that even high-importance memories lose salience if they are never retrieved.

In v4, early-turn memories (source_turn ≤ 3) use:

$$\lambda_{\text{early}} = \lambda \cdot 1.3$$

![Dynamic importance trajectories](assets/memory_test_graphs/dynamic_importance.png)

### Step 5: Arousal Modulation (McGaugh 2004)

The amygdala enhances encoding of emotionally arousing events. We detect arousal via keyword matching against an ANEW-validated lexicon (68 terms in v4, up from 32 in v3):

$$\text{arousal}(m) = \min\!\left(1.0,\; 0.25 + 0.1 \cdot \text{count\_matches}(m)\right)$$

The arousal score boosts BLA normalised strength:

$$\text{amplified} = \text{bla\_norm} + \text{budget.arousal\_boost} \cdot \text{arousal}(m)$$

![Emotional arousal effect over time](assets/memory_test_graphs/arousal.png)

### Step 6: Surprise Boost (Friston 2010)

Memories that violate predictions receive a one-time dopaminergic encoding boost. Contradiction is detected via keyword patterns (quit, cancelled, correction, actually, never, wrong):

$$\text{amplified} \mathrel{+}= \text{budget.surprise\_boost} \quad \text{if is\_surprising}$$

### Step 7: Spreading Activation — Fan Effect (Anderson & Reder 1999)

When a concept is associated with many others, each association competes for retrieval:

$$A_{\text{spread}} = S - \ln(\text{fan})$$

where $S$ is the maximum associative strength constant and $\text{fan}$ is the number of semantic neighbours. When fan is small, spreading activation helps; when large, it causes interference:

$$\text{amplified} \mathrel{+}= A_{\text{spread}} \cdot w_{\text{activation}}$$

![Fan effect: activation vs interference](assets/memory_test_graphs/fan_effect.png)

### Step 8: Importance Floor

A minimum strength prevents important memories from being fully forgotten:

$$\text{strength} = \max\!\left(0.20 \cdot I_{\text{dyn}},\; \min(1.0, \text{amplified})\right)$$

---

## v3 → v4: What's New and Why

![v3 vs v4 summary](assets/v3_vs_v4/diff_DASHBOARD.png)

Every feature below was absent in v3. The differential test suite proves this: each test is designed to **fail on v3 and pass on v4**.

---

### D1: Temporal Gradient — Murre & Dros (2015)

Murre & Dros's large-scale Ebbinghaus replication revealed a secondary retention bump ~24 hours after encoding, attributed to overnight slow-wave sleep replay.

**Formula:** Gaussian centred at 86,400 seconds (24h):

$$\text{TG}(m, t) = A \cdot \exp\!\left(-\frac{(t_{\text{now}} - t_{\text{created}} - 86400)^2}{2\sigma^2}\right)$$

where $A = 0.04$ (4% maximum boost) and $\sigma = 21600\text{s}$ (±6 hours half-width).

The strength pipeline adds this before clamping:

$$\text{amplified} \mathrel{+}= \text{TG}(m, t_{\text{now}})$$

**Key debugging insight:** The floor clamp `max(imp_floor, amplified)` was absorbing the TG bonus for any memory with reasonable access history. The gradient is only observable on low-importance, zero-access memories where imp_floor ≈ 0. Tests must use those parameters explicitly.

| Memory age | v3 strength | v4 strength | TG contribution |
| ---------- | ----------- | ----------- | --------------- |
| 24 hours   | 0.0107      | **0.0439**  | **+0.0332**     |
| 48 hours   | 0.0107      | 0.0107      | 0               |

![Temporal gradient](assets/v3_vs_v4/diff_D1_temporal_gradient.png)

---

### D2: Proactive Interference Penalty — McGeoch (1942)

When a new memory $m_{\text{new}}$ covers the same topic as an older memory $m_{\text{old}}$, the old memory is retroactively weakened. This models the mechanism by which "Alice is now VP" interferes with the stored belief "Alice is project manager."

**Formula:**

$$\text{PI}(m_{\text{old}}) = -\,\text{PI\_RATE} \cdot \text{sim}(m_{\text{old}}, m_{\text{new}}) \cdot \Delta t_{\text{exposure}}$$

where $\text{sim}$ is cosine similarity between embeddings and $\Delta t_{\text{exposure}}$ is time since the new memory was encoded. The penalty is capped at −0.15 to prevent catastrophic forgetting.

Conditions for PI to apply:

1. `budget.use_proactive_interference = True`
2. `id_map` is passed to `_compute_strength()`
3. The competitor was encoded **after** the target memory
4. Cosine similarity > threshold

**Key debugging insight:** The penalty is computed correctly but only appears in final strength when `amplified` sits well above `imp_floor`. Tests must use high-access (acc≥30), high-importance (imp≥0.7) memories.

| Similarity | PI Penalty | Effect              |
| ---------- | ---------- | ------------------- |
| 0.60       | −0.033     | Mild interference   |
| 0.80       | −0.044     | Moderate            |
| 0.99       | −0.055     | Strong interference |
| v3 (any)   | **0.000**  | No PI implemented   |

![Proactive interference](assets/v3_vs_v4/diff_D2_proactive_interference.png)

---

### D3: Sleep-Phase Cluster Weighting — Walker & Stickgold (2004)

Not all memories are consolidated equally during sleep. Emotional, high-arousal memories are preferentially replayed during REM sleep. Factual, semantic memories are consolidated during slow-wave sleep (SWS). This asymmetry means emotional clusters should receive higher consolidation priority per cycle.

**Formula:** Priority score is multiplied by a sleep-phase weight:

$$w_{\text{sleep}}(C) = 1.0 + 0.20 \cdot \text{max\_arousal}(C)$$

This requires the `Cluster` dataclass to carry a `max_arousal` field (absent in v3).

The full cluster priority score becomes:

$$P(C) = |C| \cdot \overline{I_{\text{dyn}}} \cdot \left(0.5 + 0.5 \cdot \overline{\text{str}}\right) \cdot w_{\text{sleep}}(C)$$

where $|C|$ is cluster size, $\overline{I_{\text{dyn}}}$ is mean dynamic importance, and $\overline{\text{str}}$ is mean strength.

| Cluster type | max_arousal | v3 priority | v4 priority | Boost      |
| ------------ | ----------- | ----------- | ----------- | ---------- |
| Neutral      | 0.05        | 1.560       | 1.576       | +1.0%      |
| Emotional    | 0.85        | **1.560**   | **1.825**   | **+15.8%** |

![Sleep phase weighting](assets/v3_vs_v4/diff_D3_sleep_phase.png)

---

### D4: Source-Turn Decay Gradient — Anderson & Schooler (1991)

Anderson & Schooler's rational analysis of memory shows that information from the very beginning of an interaction is the most subject to interference — it has been "on record" longest and competes with the most subsequent content.

**Formula:** For memories with `source_turn ≤ 3`, the decay rate is amplified:

$$\lambda_{\text{effective}} = \begin{cases} \lambda \cdot 1.3 & \text{if source\_turn} \leq 3 \\ \lambda & \text{otherwise} \end{cases}$$

In v3, `source_turn` is stored in the `MemoryEntry` metadata but never read by `_compute_dynamic_importance`. In v4, this field drives a 30% faster decay for early primacy items.

| Source turn | v3 importance | v4 importance | Δ           |
| ----------- | ------------- | ------------- | ----------- |
| 1           | 0.9434        | **0.9288**    | **−0.0146** |
| 15          | 0.9434        | 0.9434        | 0           |
| 50          | 0.9434        | 0.9434        | 0           |

![Source turn decay](assets/v3_vs_v4/diff_D4_source_turn.png)

---

### D5: Similarity-Weighted Redundancy Pruning

In v3, the redundancy score used raw duplicate count: any memory with `dup_count ≥ threshold` was marked for pruning. This treats 4 near-identical duplicates (sim=0.99) identically to 4 borderline near-duplicates (sim=0.74).

**v4 formula:**

$$\text{score\_redundancy}(m) = \text{dup\_count} \times \overline{\text{sim}}$$

where $\overline{\text{sim}}$ is the average cosine similarity across all flagged duplicates.

This requires `NeighborInfo` to carry a `dup_similarities: Dict[str, float]` field (absent in v3).

**Threshold decision:**

$$\text{prune?} = \begin{cases} \text{True} & \text{if } \text{dup\_count} \times \overline{\text{sim}} \geq \tau \\ \text{False} & \text{otherwise} \end{cases}$$

where $\tau = 3$ (default).

| sim      | v3 decision | v4 weighted score | v4 decision |
| -------- | ----------- | ----------------- | ----------- |
| 0.95     | Prune       | 3.80              | Prune       |
| 0.81     | Prune       | 3.24              | Prune       |
| **0.74** | **Prune**   | **2.96**          | **Spare**   |

![Weighted redundancy](assets/v3_vs_v4/diff_D5_weighted_redundancy.png)

---

### D6: Expanded Arousal Vocabulary

v3 used 32 arousal keywords drawn informally from emotional language lists. v4 expands to 68 terms validated against the ANEW (Affective Norms for English Words) database (Warriner et al., 2013), covering clinical, trauma, and high-stakes life event vocabulary that the v3 set missed.

**36 new terms added:**

`abandoned`, `abuse`, `abusive`, `addiction`, `assault`, `attacked`, `bankrupt`, `betrayal`, `blessed`, `breakthrough`, `catastrophe`, `crashed`, `crisis`, `desperate`, `diagnosed`, `ecstatic`, `grief`, `guilt`, `hopeful`, `hopeless`, `jealous`, `lonely`, `miracle`, `obsessed`, `overwhelmed`, `pride`, `rage`, `recovered`, `relapsed`, `rescued`, `shame`, `suicidal`, `survived`, `trauma`, `triumph`, `violent`

**Impact on a representative sentence:**

> _"My grief and trauma after the betrayal left me desperate and overwhelmed"_

| Version | Arousal detected | Score     |
| ------- | ---------------- | --------- |
| v3      | 0 matching terms | 0.250     |
| v4      | 5 matching terms | **0.600** |

![Arousal keyword expansion](assets/v3_vs_v4/diff_D6_arousal_keywords.png)

---

### D7: Extended Provisional Window — Nader (2000)

When a cluster of memories is summarised into a long-term trace, the original memories are kept in a "provisional" state for a window of time — during which they can be retrieved if the summary fails, modified, or reconsolidated.

Nader et al. (2000) showed that memory reconsolidation (re-stabilisation after retrieval) takes substantially longer than initial consolidation, with meaningful vulnerability windows extending well beyond one week.

**v3:** `7 * 86400` hardcoded in `_apply_summary_cluster()`.

**v4:**

```python
@dataclass
class SleepBudget:
    provisional_window_days: float = 14.0  # Nader et al. 2000
```

The function reads `budget.provisional_window_days * 86400`, making the window fully configurable without source changes.

|                | v3           | v4                     |
| -------------- | ------------ | ---------------------- |
| Default window | 7 days       | **14 days**            |
| Configurable   | ❌ hardcoded | ✅ `SleepBudget` param |
| Paper basis    | —            | Nader et al. 2000      |

![Provisional window](assets/v3_vs_v4/diff_D7_provisional_window.png)

---

### D8: SleepReport Telemetry

v4 adds two observability counters to `SleepReport` so engineers can monitor how often each new mechanism fires in production:

```python
@dataclass
class SleepReport:
    # ... existing v3 fields ...
    temporal_gradient_applied: int = 0       # How many memories got the 24h bump
    proactive_interference_detected: int = 0  # How many old memories were penalised
```

These counters are incremented inside `run_consolidation()` and returned with every sleep cycle report.

---

## Validated Cognitive Phenomena

The full test suite (v3 base + v4 differential) validates 12 distinct cognitive phenomena:

![Test dashboard](assets/memory_test_graphs/dashboard.png)

| #   | Phenomenon                        | Paper                    | Key result                             |
| --- | --------------------------------- | ------------------------ | -------------------------------------- |
| 1   | Ebbinghaus forgetting curve       | Ebbinghaus 1885          | Power-law decay confirmed              |
| 2   | Spaced repetition effect          | Cepeda et al. 2006       | Spaced: 82% stronger than massed       |
| 3   | Emotional arousal enhancement     | McGaugh 2004             | Emotional 177% stronger at day 90      |
| 4   | Prediction error / novelty boost  | Friston 2010             | +0.056 strength on contradiction       |
| 5   | Fan effect / spreading activation | Anderson & Reder 1999    | Fan=1: +0.30; Fan=32: −0.20            |
| 6   | Dynamic importance drift          | Anderson & Schooler 1991 | Unused high-imp decays 60% in 1yr      |
| 7   | Tier promotion pipeline           | McClelland 1995 (CLS)    | Flash→Short→Long gating correct        |
| 8   | Petrov BLA accuracy               | Petrov 2006              | All 6 mathematical properties verified |
| 9   | Serial position effect            | Murdock 1962             | Recency effect confirmed               |
| 10  | CLS cycle gate                    | McClelland et al. 1995   | Min 2 cycles for long-term promotion   |
| 11  | Memory cluster summarisation      | —                        | Topic clustering + summary generation  |
| 12  | 500-memory stress test            | —                        | Emotional 177% > routine, <0.05s       |

![Ebbinghaus forgetting curve](assets/memory_test_graphs/ebbinghaus.png)
![Spaced repetition](assets/memory_test_graphs/spaced_repetition.png)
![Serial position effect](assets/memory_test_graphs/serial_position.png)
![Stress test overview](assets/memory_test_graphs/stress_test.png)

---

## Test Suite Architecture

### Two Layers of Tests

**Layer 1 — Cognitive Phenomena (`test_human_memory.py`)**
Validates that the engine reproduces 12 known human memory effects. Passes on both v3 and v4 — these are the foundation, not the differentiator.

**Layer 2 — Differential Tests (`test_v3_vs_v4_differential.py`)**
Every test is explicitly structured to prove v4 is different from v3:

```python
def expect_fail_v3(name, cond_v3, cond_v4):
    # Asserts: v3 gets it WRONG, v4 gets it RIGHT
    ok = (not cond_v3) and cond_v4
    ...
```

57 assertions across 8 differential tests. 57 pass.

### Engineering Notes for Differential Testing

**The imp_floor problem:** `max(imp_floor, amplified)` silently swallows small bonuses. Always test new small-scale effects (TG, PI) with memories that have very low `imp_floor`. Use `importance=0.05`, `access_count=0`.

**The PI visibility problem:** Proactive interference only appears in final strength when `amplified` starts well above `imp_floor`. Use `importance=0.7`, `access_count=30` to ensure the penalty has room to be seen.

**The id_map requirement:** PI requires `_compute_strength()` to be called with an `id_map` parameter. Omitting it silently disables PI — no error, just missing effect.

---

## Results at a Glance

| Metric                                 | Value                                                            |
| -------------------------------------- | ---------------------------------------------------------------- |
| Total test assertions                  | 103 (46 phenomena + 57 differential)                             |
| Pass rate                              | **100%**                                                         |
| Arousal keywords                       | 68 (v3: 32)                                                      |
| New v4 mechanisms                      | 4 (TG, PI, sleep-phase, turn-gradient)                           |
| New v4 improvements                    | 4 (weighted redundancy, turn decay, 14d window, cluster arousal) |
| 500-memory stress runtime              | < 0.05s                                                          |
| Emotional vs routine strength (day 90) | **177% stronger**                                                |
| TG strength delta at 24h               | +3.32%                                                           |
| PI penalty at sim=0.95                 | −5.5%                                                            |
| Sleep-phase REM boost                  | +15.8%                                                           |
| Provisional window                     | 7d → **14d**                                                     |

---

## References

| Tag | Citation                                                                                                                                                            |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P1  | Petrov, A. (2006). Computationally efficient approximation of the base-level learning equation. _Proc. ICCM 2006_.                                                  |
| P2  | Anderson, J.R. & Reder, L.M. (1999). The fan effect: New results and new theories. _J. Experimental Psychology: General_.                                           |
| P3  | McClelland, J.L., McNaughton, B.L., & O'Reilly, R.C. (1995). Why there are complementary learning systems in the hippocampus and neocortex. _Psychological Review_. |
| P4  | McGaugh, J.L. (2004). The amygdala modulates the consolidation of memories of emotionally arousing experiences. _Annual Review of Neuroscience_.                    |
| P5  | Friston, K. (2010). The free-energy principle: a unified brain theory? _Nature Reviews Neuroscience_.                                                               |
| P6  | Anderson, J.R. & Schooler, L.J. (1991). Reflections of the environment in memory. _Psychological Science_.                                                          |
| P7  | Murdock, B.B. (1962). The serial position effect of free recall. _J. Experimental Psychology_.                                                                      |
| P8  | Murre, J.M.J. & Dros, J. (2015). Replication and analysis of Ebbinghaus' forgetting curve. _PLOS ONE_.                                                              |
| P9  | Walker, M.P. & Stickgold, R. (2004). Sleep-dependent learning and memory consolidation. _Neuron_.                                                                   |
| P10 | McGeoch, J.A. (1942). _The Psychology of Human Learning_. Longmans, Green.                                                                                          |
| P11 | Nader, K., Schafe, G.E., & LeDoux, J.E. (2000). Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval. _Nature_.              |
| P12 | Warriner, A.B., Kuperman, V., & Brysbaert, M. (2013). Norms of valence, arousal, and dominance for 13,915 English lemmas. _Behavior Research Methods_.              |
| P13 | Ebbinghaus, H. (1885). _Über das Gedächtnis_. Duncker & Humblot.                                                                                                    |
| P14 | Cepeda, N.J., et al. (2006). Distributed practice in verbal recall tasks. _Psychological Bulletin_.                                                                 |
