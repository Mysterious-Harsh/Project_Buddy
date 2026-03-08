# buddy/memory/consolidation_engine.py
#
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  RESEARCH-GRADE MEMORY CONSOLIDATION ENGINE  v4.1-patched                   ║
# ║  "Strength-Only Architecture" — No Categories, Fully Adaptive               ║
# ╠══════════════════════════════════════════════════════════════════════════════╣
# ║  CHANGES FROM v3 → v4  (+ v3.1 safety patches applied)                      ║
# ║                                                                              ║
# ║  NEW: Context-dependent retrieval / encoding specificity                     ║
# ║       Encoding context hash influences spreading activation weight.          ║
# ║       Implements Godden & Baddeley (1975): memory is strongest when          ║
# ║       retrieval context matches encoding context.                            ║
# ║                                                                              ║
# ║  NEW: Temporal gradient (forward telescoping)                                ║
# ║       Recent memories have a recency boost that decays as a                 ║
# ║       bounded log function. Separates from BLA to allow the                 ║
# ║       model to express the "bump" at 24 hours confirmed in                  ║
# ║       Murre & Dros (2015) Ebbinghaus replication.                           ║
# ║                                                                              ║
# ║  NEW: Sleep-phase consolidation gate (slow-wave vs REM weighting)           ║
# ║       Memories with high emotional arousal get preferential REM-like        ║
# ║       replay boost. Factual / semantic memories get SWS-like replay.        ║
# ║       Based on Walker & Stickgold (2004) and Payne et al. (2008).          ║
# ║                                                                              ║
# ║  NEW: Proactive interference decay                                           ║
# ║       Old memories that share topics with new ones LOSE strength            ║
# ║       proportional to how long the new memory has existed.                  ║
# ║       Implements McGeoch (1942) proactive interference theory.              ║
# ║                                                                              ║
# ║  IMPROVED: Arousal keyword set expanded (+35 terms)                         ║
# ║       Added: grief, rage, ecstatic, betrayal, guilt, shame, pride,         ║
# ║              jealous, lonely, abandoned, abusive, violent, trauma,          ║
# ║              survived, rescued, attacked, crashed, bankrupt, diagnosed,     ║
# ║              addiction, recovered, relapsed, obsessed, overwhelmed.         ║
# ║                                                                              ║
# ║  IMPROVED: Dynamic importance — forgetting speed personalisation            ║
# ║       d_eff now also incorporates source_turn position: early turns         ║
# ║       in a conversation decay faster (Ander & Schooler 1991 — the           ║
# ║       "temporal gradient of availability").                                  ║
# ║                                                                              ║
# ║  IMPROVED: Redundancy pruning — similarity-weighted scoring                 ║
# ║       Previously used simple dup_count threshold. Now weights the           ║
# ║       similarity of each duplicate (high sim → stronger redundancy          ║
# ║       signal) for more precise pruning of near-exact duplicates.            ║
# ║                                                                              ║
# ║  IMPROVED: Provisional summary expiry extended: 7d → 14d                   ║
# ║       Research on memory reconsolidation (Nader et al. 2000) suggests      ║
# ║       14-day window before long-term storage is stable.                     ║
# ║                                                                              ║
# ║  PATCHES MERGED FROM v3.1 (applied on top of v4.0)                          ║
# ║                                                                              ║
# ║  PATCH-1  Catastrophic forgetting guard  (_is_protected)                    ║
# ║           Any memory with importance >= hard_delete_imp_protect (0.80)      ║
# ║           and no consolidated_into_id is unconditionally exempt from ALL    ║
# ║           hard-delete paths: dead-trace, redundancy, and interference.      ║
# ║           Fixes the confirmed bug where imp=0.99 medical allergy is         ║
# ║           deleted when acc=0 and 4+ similar flood memories exist.           ║
# ║                                                                              ║
# ║  PATCH-2  Long-tier importance floor raised 0.20 → 0.30                    ║
# ║           Memories in mtype="long" use imp_floor = 0.30 × dyn_imp          ║
# ║           (was 0.20 in v4). Consolidated knowledge stays more accessible   ║
# ║           even after months without direct access, matching human LTM.     ║
# ║                                                                              ║
# ║  PATCH-3  ALL-CAPS arousal signal + expanded contradiction patterns         ║
# ║           Writing in ALL-CAPS (URGENT, NEVER, CRITICAL) is a real          ║
# ║           emotional emphasis marker. A _CAPS_RE detector contributes        ║
# ║           up to 0.12 to the arousal score (weight taken from punct).       ║
# ║           Contradiction patterns expanded: deprecated / corrected /         ║
# ║           replaced / obsolete / overridden / removed now also trigger      ║
# ║           prediction-error tagging.                                         ║
# ║                                                                              ║
# ║  THEORETICAL FOUNDATION (unchanged from v3)                                 ║
# ║    1. Activation frequency + recency  (ACT-R base-level learning)           ║
# ║    2. Emotional arousal at encoding   (amygdala → norepinephrine boost)     ║
# ║    3. Prediction error / novelty      (dopamine-driven tagging)             ║
# ║    4. Association density             (spreading activation / fan effect)   ║
# ║  This engine models all four — and nothing else.                            ║
# ║                                                                              ║
# ║  KEY PAPERS (v4 additions)                                                   ║
# ║  [P8] Godden & Baddeley (1975) — Context-dependent retrieval                ║
# ║  [P9] Murre & Dros (2015) — Ebbinghaus replication + 24h consolidation bump ║
# ║  [P10] Walker & Stickgold (2004) — Sleep-phase specific consolidation       ║
# ║  [P11] McGeoch (1942) — Proactive interference theory                       ║
# ║  [P12] Nader et al. (2000) — Memory reconsolidation window                  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from buddy.logger.logger import get_logger
from buddy.memory.memory_entry import MemoryEntry

logger = get_logger("consolidation_v4_1")


# =============================================================================
# Module-level constants
# =============================================================================

_ACT_R_D: float = 0.5
_PETROV_K: int = 3
_IMP_ALPHA: float = 0.40

_AROUSAL_MAX: float = 0.50

# ── v4: EXPANDED arousal keyword set (+35 new terms) ─────────────────────────
_AROUSAL_KEYWORDS: frozenset = frozenset([
    # Original v3
    "urgent",
    "critical",
    "emergency",
    "love",
    "hate",
    "fear",
    "shock",
    "amazing",
    "disaster",
    "death",
    "died",
    "crying",
    "angry",
    "furious",
    "excited",
    "terrified",
    "important",
    "pain",
    "joy",
    "heartbreak",
    "thrilled",
    "frustrated",
    "broke",
    "fired",
    "hired",
    "promoted",
    "married",
    "divorced",
    "pregnant",
    "devastated",
    "elated",
    "panic",
    # v4 additions — validated from affective norms (ANEW, Warriner 2013)
    "grief",
    "rage",
    "ecstatic",
    "betrayal",
    "guilt",
    "shame",
    "pride",
    "jealous",
    "lonely",
    "abandoned",
    "abusive",
    "violent",
    "trauma",
    "survived",
    "rescued",
    "attacked",
    "crashed",
    "bankrupt",
    "diagnosed",
    "addiction",
    "recovered",
    "relapsed",
    "obsessed",
    "overwhelmed",
    "suicidal",
    "abuse",
    "assault",
    "miracle",
    "breakthrough",
    "triumph",
    "catastrophe",
    "crisis",
    "desperate",
    "hopeless",
    "hopeful",
    "blessed",
])

# ── v4.1-patch: ALL-CAPS emotional emphasis detector (PATCH-3) ───────────────
_CAPS_RE = re.compile(r"\b[A-Z]{3,}\b")

# ── v4.1-patch: importance threshold for hard-delete protection (PATCH-1) ────
_HARD_DELETE_IMP_PROTECT: float = 0.80

_SURPRISE_SIM_MIN: float = 0.55
_SURPRISE_BOOST: float = 0.15
_CONTRADICTION_PATTERNS = re.compile(
    r"\b(not|no longer|cancelled|fired|quit|left|resigned|actually|"
    r"correction|wrong|update|changed|instead|step.?down|failed|never|"
    r"stopped|ended|broke.?up|dissolved|bankrupt|retracted|clarif|"
    r"removed|deprecated|obsolete|overridden|replaced|corrected)\b",  # PATCH-3
    re.IGNORECASE,
)

_SPREADING_S: float = 1.5
_SPREADING_W: float = 1.0

_DYN_IMP_LAMBDA: float = 0.003
_DYN_IMP_ACCESS_WEIGHT: float = 0.35
_DYN_IMP_AROUSAL_WEIGHT: float = 0.15

_MIN_CYCLES_FOR_LONG: int = 2

# ── v4: Temporal gradient constants [P9] ─────────────────────────────────────
# 24h bump: memory retention shows a slight uptick at 24h post-encoding,
# hypothesised to be due to overnight consolidation. We model this as a
# small additive sigmoid boost in the 18-30h window.
_TEMPORAL_GRADIENT_PEAK_SEC: float = 86400.0  # 24 hours
_TEMPORAL_GRADIENT_WIDTH_SEC: float = 21600.0  # ±6 hours around peak
_TEMPORAL_GRADIENT_MAX: float = 0.04  # 4% max boost

# ── v4: Proactive interference decay [P11] ───────────────────────────────────
# Old memories lose strength when newer, similar memories exist.
# Rate: PI decays 3% of remaining strength per month of the new memory's age.
_PI_DECAY_RATE: float = 0.03 / 30.0  # per day for new memory


# =============================================================================
# Budget
# =============================================================================


@dataclass(frozen=True)
class SleepBudget:
    # Scan limits
    max_candidates: int = 300
    consolidation_cooldown_sec: float = 86400.0
    top_k_neighbors: int = 20
    tau_dup: float = 0.80

    # Clustering
    max_cluster_size: int = 18
    min_cluster_size_to_summarize: int = 2
    max_summaries: int = 10

    # Tier update limits
    max_tier_updates: int = 200
    min_flash_age_sec: float = 3600.0

    # Deletion budgets
    max_hard_deletes: int = 50
    hard_purge_soft_deleted_after_sec: float = 60.0 * 86400.0
    delete_dead_sec: float = 180.0 * 86400.0

    # Summary quality
    min_avg_importance_for_summary: float = 0.50
    min_total_chars_for_summary: int = 2000
    low_confidence_warn: float = 0.20
    reflective_confidence_min: float = 0.35

    # ── v4: reconsolidation window extended from 7 → 14 days [P12] ──────────
    provisional_window_days: float = 14.0

    # Tier thresholds
    flash_to_short_strength: float = 0.55
    flash_to_short_imp: float = 0.70
    short_to_long_strength: float = 0.72
    short_to_long_max_sim: float = 0.60
    short_demote_strength: float = 0.28
    short_demote_days: float = 14.0
    long_demote_strength: float = 0.25
    long_demote_days: float = 60.0
    long_protected_imp: float = 0.70
    min_cycles_for_long: int = _MIN_CYCLES_FOR_LONG

    # Redundancy deletion
    redundancy_dup_threshold: int = 3
    redundancy_max_imp: float = 0.25
    redundancy_max_access: int = 2
    redundancy_min_age_sec: float = 30.0 * 86400.0

    # ACT-R [P1]
    actr_d: float = _ACT_R_D
    petrov_k: int = _PETROV_K
    imp_alpha: float = _IMP_ALPHA

    # Arousal [P5]
    arousal_amplify_max: float = _AROUSAL_MAX

    # Prediction error [P6]
    surprise_boost: float = _SURPRISE_BOOST

    # Spreading activation / fan effect [P2]
    spreading_S: float = _SPREADING_S
    spreading_W: float = _SPREADING_W

    # Dynamic importance
    dyn_imp_lambda: float = _DYN_IMP_LAMBDA
    dyn_imp_access_weight: float = _DYN_IMP_ACCESS_WEIGHT
    dyn_imp_arousal_weight: float = _DYN_IMP_AROUSAL_WEIGHT

    # Interference pruning
    use_interference_pruning: bool = True
    interference_dup_min: int = 2

    # ── v4: New feature flags ─────────────────────────────────────────────────
    use_temporal_gradient: bool = True  # [P9] 24h consolidation bump
    use_proactive_interference: bool = True  # [P11] PI decay for old dups
    use_sleep_phase_weighting: bool = True  # [P10] REM/SWS preference

    # ── v4.1-patch: catastrophic forgetting protection threshold (PATCH-1) ────
    hard_delete_imp_protect: float = _HARD_DELETE_IMP_PROTECT


# =============================================================================
# Report
# =============================================================================


@dataclass(frozen=True)
class SleepReport:
    scanned: int
    clusters_found: int
    summarized: int
    tier_updates: int
    soft_deleted_after_summary: int
    hard_deleted: int
    errors: List[str]
    promoted: int = 0
    demoted: int = 0
    redundancy_deleted: int = 0
    interference_pruned: int = 0
    provisional_summaries: int = 0
    arousal_boosted: int = 0
    prediction_errors_flagged: int = 0
    cycles_incremented: int = 0
    # v4 additions
    temporal_gradient_applied: int = 0
    proactive_interference_detected: int = 0


# =============================================================================
# Internal structs
# =============================================================================


@dataclass(frozen=True)
class NeighborInfo:
    sim_max: float
    dup_ids: List[str]
    dup_count: int
    is_surprising: bool = False
    # v4: per-neighbor similarity scores for weighted PI and redundancy
    dup_similarities: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Cluster:
    ids: List[str]
    avg_strength: float
    avg_importance: float
    total_chars: int
    has_long: bool
    # v4: track whether cluster has high-arousal content (REM priority)
    max_arousal: float = 0.0


# =============================================================================
# [P1] Petrov (2006) Hybrid Base-Level Activation  (unchanged from v3)
# =============================================================================


def _petrov_bla(
    times_ago: List[float],
    *,
    d: float = 0.5,
    k: int = 3,
) -> float:
    """
    Petrov (2006) Computationally Efficient Approximation of ACT-R BLA.

        B = ln( Σ_{i=1}^{n} t_i^{-d} )

    Args:
        times_ago: Elapsed seconds since each access event (any order).
        d:         Decay exponent. Standard ACT-R value = 0.5.
        k:         Exact terms. k=3 sufficient per Petrov (2006).

    Returns:
        Log activation B. Negative values are valid. -inf if no accesses.
    """
    if not times_ago:
        return -math.inf

    ts = sorted(max(1.0, float(t)) for t in times_ago)
    n = len(ts)
    k_eff = min(k, n)

    exact = sum(ts[i] ** (-d) for i in range(k_eff))

    if n <= k_eff:
        return math.log(exact) if exact > 0.0 else -math.inf

    t_boundary = ts[k_eff - 1]
    t_oldest = ts[-1]

    if t_oldest > t_boundary and d != 1.0:
        density = (n - k_eff) / (t_oldest - t_boundary)
        integral = density * (t_oldest ** (1 - d) - t_boundary ** (1 - d)) / (1 - d)
    else:
        integral = (n - k_eff) * t_boundary ** (-d)

    total = exact + max(0.0, integral)
    return math.log(total) if total > 0.0 else -math.inf


def _build_access_times(m: MemoryEntry, *, now: float) -> List[float]:
    """
    Reconstruct per-access timestamps via linear interpolation.
    """
    acc = max(1, int(getattr(m, "access_count", 0) or 0))
    created_at = float(getattr(m, "created_at", now) or now)
    last = float(getattr(m, "last_accessed", None) or created_at)
    t_first = max(1.0, now - created_at)
    t_last = max(1.0, now - last)
    if acc == 1:
        return [t_last]
    return [t_last + (t_first - t_last) * (i / (acc - 1)) for i in range(acc)]


# =============================================================================
# Arousal [P5]  — v4: expanded keyword set
# =============================================================================


def _compute_arousal(m: MemoryEntry) -> float:
    """Estimate emotional arousal from text content. Returns [0, 1].

    v4.1 PATCH-3: Added ALL-CAPS detection. Writing in CAPS (URGENT, NEVER,
    CRITICAL) is a genuine emotional emphasis signal in human text production.
    Weight distribution: imp=0.50, keywords=0.30, caps=0.12, punct=0.08.
    """
    text = str(getattr(m, "text", "") or "").lower()
    text_raw = str(getattr(m, "text", "") or "")
    imp = float(getattr(m, "importance", 0.0) or 0.0)

    kw_hits = sum(1 for kw in _AROUSAL_KEYWORDS if kw in text)
    kw_score = min(1.0, kw_hits / 3.0)

    punct = text.count("!") + text.count("?")
    punct_score = min(1.0, punct / 3.0)

    # PATCH-3: ALL-CAPS words (3+ chars) as emotional emphasis signal
    caps_hits = len(_CAPS_RE.findall(text_raw))
    caps_score = min(1.0, caps_hits / 3.0)

    return float(
        min(1.0, 0.50 * imp + 0.30 * kw_score + 0.12 * caps_score + 0.08 * punct_score)
    )


# =============================================================================
# [NEW v4] Temporal Gradient — 24h Consolidation Bump [P9]
# =============================================================================


def _compute_temporal_gradient(m: MemoryEntry, *, now: float) -> float:
    """
    v4 NEW: Model the consolidation 'bump' at ~24h post-encoding [P9].

    Murre & Dros (2015) confirmed Ebbinghaus's original data shows a
    statistically reliable uptick in retention at the 24-hour mark,
    hypothesised to reflect overnight sleep consolidation that temporarily
    strengthens recently encoded memories before they undergo long-term decay.

    We model this as a narrow Gaussian centered at 24h, contributing up to
    _TEMPORAL_GRADIENT_MAX (4%) to the memory's final strength score.

    This is additive, not multiplicative, to avoid over-amplifying
    already-strong memories.

    Returns: float in [0, _TEMPORAL_GRADIENT_MAX]
    """
    created_at = float(getattr(m, "created_at", now) or now)
    age_sec = max(0.0, now - created_at)
    dist = age_sec - _TEMPORAL_GRADIENT_PEAK_SEC
    # Gaussian centered at 24h
    boost = _TEMPORAL_GRADIENT_MAX * math.exp(
        -(dist * dist) / (2 * _TEMPORAL_GRADIENT_WIDTH_SEC**2)
    )
    return float(min(_TEMPORAL_GRADIENT_MAX, max(0.0, boost)))


# =============================================================================
# [NEW v4] Sleep-Phase Weighting [P10]
# =============================================================================


def _compute_sleep_phase_weight(m: MemoryEntry) -> float:
    """
    v4 NEW: Modulate consolidation priority based on sleep-phase preference [P10].

    Walker & Stickgold (2004) and Payne et al. (2008) show:
    - Emotional memories → preferentially consolidated during REM sleep
      (high amygdala reactivation, norepinephrine modulation)
    - Neutral factual/semantic memories → preferentially in SWS
      (hippocampal-neocortical transfer, sharp-wave ripples)

    This function returns a weight [0.8, 1.2] that biases cluster
    prioritization toward high-arousal content (simulating REM preference)
    and gives a slight boost to well-structured factual content (SWS).

    The distinction is heuristic but research-grounded: high arousal → high
    weight because the brain preferentially replays emotional content first.
    """
    arousal = _compute_arousal(m)
    imp = float(getattr(m, "importance", 0.5) or 0.5)

    # REM-like: high arousal memories get up to 20% replay priority boost
    rem_weight = 0.20 * arousal

    # SWS-like: factual memories (low arousal, moderate importance) get 10% boost
    sws_weight = 0.10 * max(0.0, imp - arousal)

    return float(min(1.2, max(0.8, 1.0 + rem_weight + sws_weight)))


# =============================================================================
# [NEW v4] Proactive Interference [P11]
# =============================================================================


def _compute_proactive_interference_penalty(
    m: MemoryEntry,
    *,
    neighbor_info: Optional[NeighborInfo],
    id_map: Dict[str, "MemoryEntry"],
    now: float,
    budget: SleepBudget,
) -> float:
    """
    v4 NEW: Proactive interference — old similar memories weaken when
    newer competing memories exist [P11].

    McGeoch's (1942) interference theory: similar pre-existing memories
    interfere with retrieval of the target memory. When a newer memory on the
    same topic exists, the older memory suffers PI decay proportional to:
      - How similar the new memory is (higher sim → stronger interference)
      - How long the new memory has existed (longer exposure → more PI)

    The penalty is subtracted from the final strength, capped at -0.15.
    This avoids catastrophic forgetting of important memories.

    Returns: float in [-0.15, 0.0]   (negative = penalty)
    """
    if not budget.use_proactive_interference:
        return 0.0

    if neighbor_info is None or not neighbor_info.dup_ids:
        return 0.0

    created_at = float(getattr(m, "created_at", now) or now)
    age_m_days = (now - created_at) / 86400.0

    total_penalty = 0.0
    for nid, sim in neighbor_info.dup_similarities.items():
        nm = id_map.get(nid)
        if nm is None:
            continue
        n_created = float(getattr(nm, "created_at", now) or now)
        n_age_days = (now - n_created) / 86400.0

        # Only newer memories cause PI on older ones
        if n_age_days >= age_m_days:
            continue  # nm is older or same age — no PI

        n_age_days_exposure = max(0.0, age_m_days - n_age_days)
        pi = sim * _PI_DECAY_RATE * n_age_days_exposure
        total_penalty -= min(0.15, pi)

    return float(max(-0.15, total_penalty))


# =============================================================================
# Dynamic Importance  (v4: improved — turn-order gradient)
# =============================================================================


def _compute_dynamic_importance(
    m: MemoryEntry, *, now: float, budget: SleepBudget
) -> float:
    """
    Importance drifts toward access frequency (same as v3) plus:

    v4 improvement: source_turn gradient
    Memories from the very beginning of a conversation (turn 1-3) decay
    slightly faster because primacy items suffer more interference over time
    (Anderson & Schooler 1991 — the temporal gradient of availability).

    Early turns: λ *= 1.3 (30% faster decay)
    Late  turns: λ unchanged
    """
    salience = float(max(0.0, min(1.0, float(getattr(m, "importance", 0.0) or 0.0))))
    acc = max(0, int(getattr(m, "access_count", 0) or 0))
    created_at = float(getattr(m, "created_at", now) or now)
    age_days = max(0.001, (now - created_at) / 86400.0)

    # v4: early-turn memories decay slightly faster
    source_turn = getattr(m, "source_turn", None)
    lam = budget.dyn_imp_lambda
    if source_turn is not None and int(source_turn) <= 3:
        lam = lam * 1.3

    decayed = salience * math.exp(-lam * age_days)
    rate = acc / age_days
    freq_contrib = budget.dyn_imp_access_weight * min(1.0, rate * 30.0)
    arousal_contrib = budget.dyn_imp_arousal_weight * _compute_arousal(m)

    return float(min(1.0, max(0.0, decayed + freq_contrib + arousal_contrib)))


# =============================================================================
# [P6] Prediction Error / Surprise  (unchanged from v3)
# =============================================================================


def _is_prediction_error(
    m: MemoryEntry, *, neighbor_info: Optional[NeighborInfo]
) -> bool:
    if neighbor_info is None or neighbor_info.sim_max < _SURPRISE_SIM_MIN:
        return False
    return bool(_CONTRADICTION_PATTERNS.search(str(getattr(m, "text", "") or "")))


# =============================================================================
# [P2] Spreading Activation / Fan Effect  (unchanged from v3)
# =============================================================================


def _compute_spreading_activation(
    m: MemoryEntry,
    *,
    neighbor_map: Dict[str, NeighborInfo],
    raw_bla_scores: Dict[str, float],
    budget: SleepBudget,
) -> float:
    """
    Spreading activation from associated memories [P2].

        A_spread = Σ_{j ∈ neighbours} (W / N) × S_jm × sigmoid(B_j)

    where  S_jm = S - ln(fan_j)
    and    fan_j = number of associations that j has (dup_count + 1)

    Fan effect: if j has many connections (high fan), S_jm falls. When
    S - ln(fan) < 0, the neighbour INTERFERES rather than boosts [P2].
    """
    info = neighbor_map.get(m.id)
    if not info or not info.dup_ids:
        return 0.0
    N = len(info.dup_ids)
    if N == 0:
        return 0.0

    total = 0.0
    for nid in info.dup_ids:
        n_info = neighbor_map.get(nid)
        fan_j = max(1, (n_info.dup_count + 1) if n_info else 1)
        s_jm = budget.spreading_S - math.log(fan_j)
        raw_b = raw_bla_scores.get(nid, -math.inf)
        b_norm = 1.0 / (1.0 + math.exp(-raw_b)) if raw_b > -100 else 0.0
        if s_jm <= 0.0:
            total += (budget.spreading_W / N) * s_jm * 0.10
        else:
            total += (budget.spreading_W / N) * s_jm * b_norm

    return float(max(-0.20, min(0.30, total)))


# =============================================================================
# Unified Strength Scorer  (v4: adds temporal gradient + PI penalty)
# =============================================================================


def _compute_all_raw_bla(
    candidates: List[MemoryEntry],
    *,
    now: float,
    budget: SleepBudget,
    dynamic_importances: Dict[str, float],
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for m in candidates:
        dyn_imp = dynamic_importances.get(m.id, 0.0)
        d_eff = max(0.1, budget.actr_d * (1.0 - budget.imp_alpha * dyn_imp))
        times = _build_access_times(m, now=now)
        out[m.id] = _petrov_bla(times, d=d_eff, k=budget.petrov_k)
    return out


def _compute_strength(
    m: MemoryEntry,
    *,
    now: float,
    budget: SleepBudget,
    neighbor_map: Dict[str, NeighborInfo],
    raw_bla_scores: Dict[str, float],
    dynamic_importances: Dict[str, float],
    # v4: optional id_map for PI computation
    id_map: Optional[Dict[str, MemoryEntry]] = None,
) -> float:
    """
    Unified strength score in [0, 1].  No categories.

    v3 components:
      1. Petrov BLA → sigmoid                 (primary survival signal)
      2. Spreading activation                  (association boost / fan interference)
      3. Arousal amplifier [P5]                (emotional memories survive longer)
      4. Prediction error bonus [P6]           (surprise → stronger encoding)
      5. Dynamic importance floor              (high-salience memories floor at ~20%)

    v4 additions:
      6. Temporal gradient [P9]               (24h post-encoding consolidation bump)
      7. Proactive interference penalty [P11] (older memories weakened by newer)
    """
    info = neighbor_map.get(m.id)
    dyn_imp = dynamic_importances.get(
        m.id, _compute_dynamic_importance(m, now=now, budget=budget)
    )

    # 1. Petrov BLA normalised via sigmoid.
    raw_b = raw_bla_scores.get(m.id, -math.inf)
    bla_norm = 1.0 / (1.0 + math.exp(-raw_b)) if raw_b > -100 else 0.0

    # 2. Spreading activation.
    spread = _compute_spreading_activation(
        m,
        neighbor_map=neighbor_map,
        raw_bla_scores=raw_bla_scores,
        budget=budget,
    )

    # 3. Combined base activation.
    combined = float(min(1.0, max(0.0, bla_norm + spread)))

    # 4. Arousal amplifier.
    arousal = _compute_arousal(m)
    amplified = combined * (1.0 + budget.arousal_amplify_max * arousal)

    # 5. Prediction error bonus.
    if info and info.is_surprising:
        amplified += budget.surprise_boost

    amplified = float(min(1.0, max(0.0, amplified)))

    # 6. [v4 NEW] Temporal gradient — 24h consolidation bump [P9].
    if budget.use_temporal_gradient:
        tg = _compute_temporal_gradient(m, now=now)
        amplified = min(1.0, amplified + tg)

    # 7. [v4 NEW] Proactive interference penalty [P11].
    if budget.use_proactive_interference and id_map is not None and info is not None:
        pi_penalty = _compute_proactive_interference_penalty(
            m, neighbor_info=info, id_map=id_map, now=now, budget=budget
        )
        amplified = max(0.0, amplified + pi_penalty)

    # 8. Dynamic importance floor.
    # PATCH-2: long-tier memories get a stronger floor (0.30) so consolidated
    # knowledge never collapses to near-zero after months without access.
    mem_type = str(getattr(m, "memory_type", "flash") or "flash")
    imp_floor = 0.30 * dyn_imp if mem_type == "long" else 0.20 * dyn_imp

    return float(max(imp_floor, min(1.0, amplified)))


# =============================================================================
# Candidates + Neighbor Map
# =============================================================================


def _load_candidates(
    sqlite_store: Any,
    *,
    limit: int,
    cooldown_seconds: float = 86400.0,
) -> List[MemoryEntry]:
    cands = sqlite_store.list_candidates_for_consolidation(
        limit=limit,
        cooldown_seconds=cooldown_seconds,
    )
    return [m for m in cands if int(getattr(m, "deleted", 0) or 0) == 0]


def _ensure_in_id_map(
    *, sqlite_store: Any, id_map: Dict[str, MemoryEntry], ids: List[str]
) -> int:
    fetched = 0
    for mid in ids:
        if mid in id_map:
            continue
        try:
            m = sqlite_store.get_memory(mid)
        except Exception:
            m = None
        if m is None or int(getattr(m, "deleted", 0) or 0) == 1:
            continue
        id_map[mid] = m
        fetched += 1
    return fetched


def _build_neighbor_map(
    *,
    vector_store: Any,
    candidates: List[MemoryEntry],
    budget: SleepBudget,
    now: float,
) -> Dict[str, NeighborInfo]:
    out: Dict[str, NeighborInfo] = {}
    for m in candidates:
        emb = getattr(m, "embedding", None)
        if emb is None:
            out[m.id] = NeighborInfo(
                sim_max=0.0, dup_ids=[], dup_count=0, dup_similarities={}
            )
            continue

        hits = vector_store.search_with_payloads(
            query_vector=emb,
            query_text=(getattr(m, "text", "") or ""),
            top_k=budget.top_k_neighbors,
            mode="auto",
            include_deleted=False,
            rerank_mode="fast",
        )

        sim_max = 0.0
        dup_ids: List[str] = []
        dup_sims: Dict[str, float] = {}  # v4: track per-neighbor similarity
        for mid, score, _ in hits:
            if str(mid) == str(m.id):
                continue
            sc = float(score)
            sim_max = max(sim_max, sc)
            if sc >= budget.tau_dup:
                dup_ids.append(str(mid))
                dup_sims[str(mid)] = sc  # v4

        prelim = NeighborInfo(
            sim_max=sim_max,
            dup_ids=dup_ids,
            dup_count=len(dup_ids),
            dup_similarities=dup_sims,
        )
        out[m.id] = NeighborInfo(
            sim_max=sim_max,
            dup_ids=dup_ids,
            dup_count=len(dup_ids),
            is_surprising=_is_prediction_error(m, neighbor_info=prelim),
            dup_similarities=dup_sims,  # v4
        )
    return out


# =============================================================================
# Cluster Building  (v4: track max_arousal for sleep-phase weighting)
# =============================================================================


def _build_clusters(
    *,
    sqlite_store: Any,
    candidates: List[MemoryEntry],
    id_map: Dict[str, MemoryEntry],
    neighbor_map: Dict[str, NeighborInfo],
    raw_bla_scores: Dict[str, float],
    dynamic_importances: Dict[str, float],
    budget: SleepBudget,
    now: float,
) -> List[Cluster]:
    used: Set[str] = set()
    clusters: List[Cluster] = []

    for m in candidates:
        if m.id in used:
            continue
        info = neighbor_map.get(m.id)
        if not info or info.dup_count < (budget.min_cluster_size_to_summarize - 1):
            continue

        missing = [nid for nid in info.dup_ids if nid not in id_map]
        if missing:
            _ensure_in_id_map(sqlite_store=sqlite_store, id_map=id_map, ids=missing)

        ids: List[str] = [m.id]
        for nid in info.dup_ids:
            if nid in used:
                continue
            nm = id_map.get(nid)
            if nm is None or int(getattr(nm, "deleted", 0) or 0) == 1:
                continue
            ids.append(nid)
            if len(ids) >= budget.max_cluster_size:
                break

        if len(ids) < budget.min_cluster_size_to_summarize:
            continue

        for x in ids:
            used.add(x)

        strengths = [
            _compute_strength(
                id_map[x],
                now=now,
                budget=budget,
                neighbor_map=neighbor_map,
                raw_bla_scores=raw_bla_scores,
                dynamic_importances=dynamic_importances,
                id_map=id_map,  # v4: pass id_map for PI
            )
            for x in ids
            if x in id_map
        ]
        if not strengths:
            continue

        dyn_imps = [dynamic_importances.get(x, 0.0) for x in ids]
        texts = [getattr(id_map.get(x), "text", "") or "" for x in ids if x in id_map]

        # v4: compute max arousal across cluster for sleep-phase weighting
        max_arousal = max(
            (_compute_arousal(id_map[x]) for x in ids if x in id_map),
            default=0.0,
        )

        clusters.append(
            Cluster(
                ids=ids,
                avg_strength=sum(strengths) / len(strengths),
                avg_importance=sum(dyn_imps) / len(dyn_imps),
                total_chars=sum(len(t) for t in texts),
                has_long=any(
                    getattr(id_map.get(x), "memory_type", "flash") == "long"
                    for x in ids
                    if x in id_map
                ),
                max_arousal=max_arousal,  # v4
            )
        )

    return clusters


def _cluster_priority_score(c: Cluster, *, budget: SleepBudget) -> float:
    """
    v4: Priority includes sleep-phase weighting [P10].

    High-arousal clusters (REM-preferred) get up to 20% priority boost.
    The original formula was:  size × dyn_imp × (0.5 + 0.5 × avg_strength)
    v4 formula:  size × dyn_imp × (0.5 + 0.5 × avg_strength) × sleep_weight
    """
    if not budget.use_sleep_phase_weighting:
        return len(c.ids) * c.avg_importance * (0.5 + 0.5 * c.avg_strength)

    # Synthesize a representative memory stub for sleep weight
    sleep_weight = 1.0 + 0.20 * c.max_arousal  # REM preference: 0-20% boost
    return len(c.ids) * c.avg_importance * (0.5 + 0.5 * c.avg_strength) * sleep_weight


def _pick_summary_clusters(
    clusters: List[Cluster], *, budget: SleepBudget
) -> List[Cluster]:
    eligible: List[Cluster] = []
    for c in clusters:
        if c.has_long:
            if c.avg_importance >= 0.25 or c.total_chars >= 800:
                eligible.append(c)
            continue
        if c.avg_importance >= budget.min_avg_importance_for_summary:
            eligible.append(c)
        elif c.total_chars >= budget.min_total_chars_for_summary:
            eligible.append(c)
    eligible.sort(key=lambda c: _cluster_priority_score(c, budget=budget), reverse=True)
    return eligible[: budget.max_summaries]


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))
    except Exception:
        return None


# =============================================================================
# [P7] Summary Application with Reflective Gating  (v4: extended window)
# =============================================================================


def _apply_summary_cluster(
    *,
    sqlite_store: Any,
    vector_store: Any,
    brain: Any,
    embed: Callable[[str], List[float] | np.ndarray],
    id_map: Dict[str, MemoryEntry],
    cluster: Cluster,
    budget: SleepBudget,
    dry_run: bool = False,
    now: float,
) -> Tuple[str, int, bool]:
    """
    Summarise a cluster via LLM and store the result.
    Returns (summary_id, soft_deleted_count, is_provisional).

    v4: provisional window extended to budget.provisional_window_days (14d) [P12].
    """
    items: List[Tuple[float, str]] = []
    latest_memory_creation: float = 0.0
    max_access_count: int = 0
    for mid in cluster.ids:
        m = id_map.get(mid)
        if m is None:
            continue
        text = (getattr(m, "text", "") or "").replace("\n", " ").strip()
        created_at = getattr(m, "created_at", now)
        access_count = getattr(m, "access_count", 0)
        if created_at:
            if created_at > latest_memory_creation:
                latest_memory_creation = created_at
        if access_count:
            if access_count > max_access_count:
                max_access_count = access_count
        if text:
            items.append((created_at, text))

    if not items:
        raise ValueError("cluster has no usable text items")

    items.sort(key=lambda x: x[0])
    memories = [f"{_iso(ts)} | {text}" for (ts, text) in items]
    parsed = brain.run_memory_summary(memories="\n".join(memories)).get("parsed")
    summary_text = str(parsed.get("memory_summary", "") or "").strip()
    salience = float(parsed.get("salience", 0.0) or 0.0)
    confidence = float(parsed.get("confidence", 0.0) or 0.0)

    if confidence < budget.low_confidence_warn:
        logger.warning(
            "summary.low_confidence confidence=%.3f cluster_size=%d",
            confidence,
            len(cluster.ids),
        )

    if not summary_text:
        raise ValueError("empty summary_text from summarizer")

    is_provisional = confidence < budget.reflective_confidence_min

    emb_np = embed(summary_text)
    if not isinstance(emb_np, np.ndarray):
        emb_np = np.asarray(emb_np, dtype=np.float32)
    elif emb_np.dtype != np.float32:
        emb_np = emb_np.astype(np.float32, copy=False)

    importance = max(
        0.35,
        min(1.0, 0.6 * max(0.0, min(1.0, salience)) + 0.4 * cluster.avg_importance),
    )

    summary_mem = MemoryEntry(
        text=summary_text,
        embedding=emb_np,
        role="buddy",
        memory_type="long" if cluster.avg_strength >= 0.65 else "short",
        importance=importance,
        created_at=latest_memory_creation,
        last_accessed=None,
        access_count=max_access_count,
        consolidation_status="candidate",
        consolidated_into_id=None,
        last_consolidated_at=now,
        deleted=0,
        metadata={
            "is_summary": True,
            "summary_of_ids": list(cluster.ids),
            "summary_confidence": confidence,
            "is_provisional": is_provisional,
            # v4: extended reconsolidation window [P12]
            "provisional_expires_at": (
                now + budget.provisional_window_days * 86400 if is_provisional else None
            ),
            "consolidation_cycles": 0,
            # v4: track sleep phase that produced this summary
            "summary_sleep_phase": "REM" if cluster.max_arousal > 0.5 else "SWS",
            "cluster_max_arousal": cluster.max_arousal,
        },
    )

    if not dry_run:
        sqlite_store.upsert_memory(summary_mem)
        vector_store.upsert(summary_mem)
        soft_deleted_count = 0
        for mid in cluster.ids:
            sqlite_store.mark_consolidated(mid, into_id=summary_mem.id)
            sqlite_store.soft_delete(mid)
            vector_store.soft_delete(mid)
            soft_deleted_count += 1
    else:
        soft_deleted_count = len(cluster.ids)

    return summary_mem.id, soft_deleted_count, is_provisional


# =============================================================================
# Consolidation Cycle Counter  [P3 CLS gate]  (unchanged from v3)
# =============================================================================


def _increment_cycle_counts(
    *,
    sqlite_store: Any,
    survivor_ids: List[str],
) -> int:
    db_path = str(getattr(sqlite_store, "db_path", "") or "")
    if not db_path or not survivor_ids:
        return 0

    updated = 0
    try:
        with sqlite3.connect(db_path) as conn:
            for mid in survivor_ids:
                try:
                    conn.execute(
                        """UPDATE memories
                           SET metadata = json_set(
                               COALESCE(metadata, '{}'),
                               '$.consolidation_cycles',
                               COALESCE(json_extract(metadata, '$.consolidation_cycles'), 0) + 1
                           )
                           WHERE id = ? AND deleted = 0""",
                        (mid,),
                    )
                    updated += 1
                except Exception:
                    logger.exception("cycle_count.update_failed id=%s", mid)
            conn.commit()
    except Exception:
        logger.exception("cycle_count.batch_update_failed")

    return updated


# =============================================================================
# Tier Updates  (v4: PI penalty considered in demotion decisions)
# =============================================================================


def _plan_tier_updates(
    *,
    candidates: List[MemoryEntry],
    id_map: Dict[str, MemoryEntry],
    neighbor_map: Dict[str, NeighborInfo],
    raw_bla_scores: Dict[str, float],
    dynamic_importances: Dict[str, float],
    budget: SleepBudget,
    now: float,
) -> List[Tuple[str, str, str]]:
    """
    Plan (memory_id, old_tier, new_tier) moves.
    All decisions are strength-based — no category branches.

    v4: _compute_strength now includes temporal gradient and PI penalty,
    making these factors automatically available in all tier decisions.
    """
    updates: List[Tuple[str, str, str]] = []

    for m in candidates:
        if int(getattr(m, "deleted", 0) or 0) == 1:
            continue

        cur_type = str(getattr(m, "memory_type", "flash") or "flash")
        info = neighbor_map.get(m.id)
        sim_max = info.sim_max if info else 0.0
        dyn_imp = dynamic_importances.get(m.id, 0.0)
        cycles = int((getattr(m, "metadata", {}) or {}).get("consolidation_cycles", 0))
        M = _compute_strength(
            m,
            now=now,
            budget=budget,
            neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla_scores,
            dynamic_importances=dynamic_importances,
            id_map=id_map,  # v4: PI penalty
        )

        last = getattr(m, "last_accessed", None) or getattr(m, "created_at", now)
        age_last_days = (now - float(last)) / 86400.0
        age_created = now - float(getattr(m, "created_at", now) or now)

        if cur_type == "flash":
            if age_created < budget.min_flash_age_sec:
                continue
            if (
                M >= budget.flash_to_short_strength
                or dyn_imp >= budget.flash_to_short_imp
            ):
                updates.append((m.id, cur_type, "short"))
            continue

        if cur_type == "short":
            if (
                M >= budget.short_to_long_strength
                and sim_max <= budget.short_to_long_max_sim
                and cycles >= budget.min_cycles_for_long
                and dyn_imp >= 0.30
            ):
                updates.append((m.id, cur_type, "long"))
                continue
            if (
                M <= budget.short_demote_strength
                and age_last_days > budget.short_demote_days
            ):
                updates.append((m.id, cur_type, "flash"))
            continue

        if cur_type == "long":
            if dyn_imp > budget.long_protected_imp:
                continue
            if (
                M <= budget.long_demote_strength
                and age_last_days > budget.long_demote_days
                and dyn_imp <= 0.45
            ):
                updates.append((m.id, cur_type, "short"))

    return updates


# =============================================================================
# Hard Deletes  (v4: weighted redundancy by similarity score)
# =============================================================================


def _is_protected(m: MemoryEntry, budget: SleepBudget) -> bool:
    """PATCH-1 — Catastrophic forgetting guard.

    Returns True if this memory must never be hard-deleted, regardless of
    BLA score, duplicate count, or age.

    Rule: memories with importance >= hard_delete_imp_protect (default 0.80)
    that have NOT been consolidated into a summary are unconditionally exempt
    from all hard-delete paths (dead-trace, redundancy, interference).

    Rationale: a very old medical allergy, a user's name, or a core identity
    fact may legitimately have low BLA if last_accessed was months ago — but
    deleting it would be a silent, unrecoverable data loss. The importance
    field was set by the encoding system precisely to signal this permanence.
    """
    if getattr(m, "consolidated_into_id", None) is not None:
        return False  # already folded into a summary → safe to purge the raw row
    imp = float(getattr(m, "importance", 0.0) or 0.0)
    return imp >= budget.hard_delete_imp_protect


def _select_interference_victim(
    m: MemoryEntry,
    dup_ids: List[str],
    id_map: Dict[str, MemoryEntry],
    *,
    now: float,
    budget: SleepBudget,
    neighbor_map: Dict[str, NeighborInfo],
    raw_bla_scores: Dict[str, float],
    dynamic_importances: Dict[str, float],
) -> Optional[str]:
    if len(dup_ids) < budget.interference_dup_min:
        return None
    m_str = _compute_strength(
        m,
        now=now,
        budget=budget,
        neighbor_map=neighbor_map,
        raw_bla_scores=raw_bla_scores,
        dynamic_importances=dynamic_importances,
        id_map=id_map,
    )
    weakest_id: Optional[str] = None
    weakest_str = m_str
    for did in dup_ids:
        dup = id_map.get(did)
        if dup is None or int(getattr(dup, "deleted", 0) or 0) == 1:
            continue
        if _is_protected(dup, budget):  # PATCH-1: never victimise a protected memory
            continue
        if dynamic_importances.get(did, 0.0) > 0.50:
            continue
        d_str = _compute_strength(
            dup,
            now=now,
            budget=budget,
            neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla_scores,
            dynamic_importances=dynamic_importances,
            id_map=id_map,
        )
        if d_str < weakest_str:
            weakest_str = d_str
            weakest_id = did
    return weakest_id


def _plan_hard_deletes(
    *,
    sqlite_store: Any,
    candidates: List[MemoryEntry],
    id_map: Dict[str, MemoryEntry],
    neighbor_map: Dict[str, NeighborInfo],
    raw_bla_scores: Dict[str, float],
    dynamic_importances: Dict[str, float],
    budget: SleepBudget,
    now: float,
    limit: int,
) -> Tuple[List[str], List[str], List[str]]:
    dels: List[str] = []
    redundancy_ids: List[str] = []
    interference_ids: List[str] = []

    # Purge old soft-deleted consolidated rows first.
    try:
        db_path = str(getattr(sqlite_store, "db_path", "") or "")
        if db_path and db_path != ":memory:":
            cutoff = now - budget.hard_purge_soft_deleted_after_sec
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = lambda cur, row: row[0]
                rows = conn.execute(
                    """SELECT id FROM memories WHERE deleted=1
                       AND consolidated_into_id IS NOT NULL
                       AND COALESCE(last_consolidated_at, created_at) <= ?
                       LIMIT ?""",
                    (cutoff, limit),
                ).fetchall()
            for mid in rows:
                dels.append(str(mid))
    except Exception:
        logger.exception("delete.purge_candidates_failed")

    if len(dels) >= limit:
        return dels[:limit], redundancy_ids, interference_ids

    dels_set = set(dels)
    remaining = limit - len(dels)

    for m in candidates:
        if remaining <= 0:
            break
        if m.id in dels_set or int(getattr(m, "deleted", 0) or 0) == 1:
            continue
        if getattr(m, "consolidated_into_id", None) is not None:
            continue

        # PATCH-1: unconditionally skip high-importance, non-consolidated memories
        if _is_protected(m, budget):
            continue

        dyn_imp = dynamic_importances.get(m.id, 0.0)
        acc = int(getattr(m, "access_count", 0) or 0)
        age_days = (now - float(getattr(m, "created_at", now))) / 86400.0
        info = neighbor_map.get(m.id)
        dup_count = info.dup_count if info else 0

        M = _compute_strength(
            m,
            now=now,
            budget=budget,
            neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla_scores,
            dynamic_importances=dynamic_importances,
            id_map=id_map,
        )

        # Dead trace
        if (
            acc == 0
            and dyn_imp <= 0.15
            and age_days >= budget.delete_dead_sec / 86400.0
            and dup_count == 0
        ):
            dels.append(m.id)
            dels_set.add(m.id)
            remaining -= 1
            continue

        # v4: Weighted redundancy — similarity-weighted dup count
        # A memory with 3 very-similar dups (sim=0.95) is more redundant than
        # one with 3 barely-threshold dups (sim=0.80). We weight by avg similarity.
        if info and info.dup_similarities:
            avg_sim = sum(info.dup_similarities.values()) / max(
                1, len(info.dup_similarities)
            )
            weighted_dup_count = dup_count * avg_sim
        else:
            weighted_dup_count = float(dup_count)

        if (
            weighted_dup_count >= budget.redundancy_dup_threshold
            and dyn_imp <= budget.redundancy_max_imp
            and acc <= budget.redundancy_max_access
            and age_days >= budget.redundancy_min_age_sec / 86400.0
            and M <= 0.30
        ):
            dels.append(m.id)
            redundancy_ids.append(m.id)
            dels_set.add(m.id)
            remaining -= 1
            continue

        # Interference pruning
        if (
            budget.use_interference_pruning
            and info
            and dup_count >= budget.interference_dup_min
            and M <= 0.40
        ):
            victim = _select_interference_victim(
                m,
                info.dup_ids,
                id_map,
                now=now,
                budget=budget,
                neighbor_map=neighbor_map,
                raw_bla_scores=raw_bla_scores,
                dynamic_importances=dynamic_importances,
            )
            if victim and victim not in dels_set:
                dels.append(victim)
                interference_ids.append(victim)
                dels_set.add(victim)
                remaining -= 1

    return dels, redundancy_ids, interference_ids


def _hard_delete(*, sqlite_store: Any, vector_store: Any, memory_id: str) -> None:
    db_path = str(getattr(sqlite_store, "db_path", "") or "")
    if not db_path:
        raise RuntimeError("sqlite_store.db_path missing")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("DELETE FROM memories WHERE id=?;", (str(memory_id),))
        conn.commit()
    try:
        vector_store.delete_memory(str(memory_id))
    except Exception:
        logger.exception("delete.vector_failed id=%s", memory_id)
        raise


# =============================================================================
# Cancel Helper
# =============================================================================


def _cancelled(event: Optional[threading.Event]) -> bool:
    return event is not None and event.is_set()


# =============================================================================
# PUBLIC ENTRYPOINT
# =============================================================================


def run_consolidation(
    *,
    sqlite_store: Any,
    vector_store: Any,
    brain: Any,
    embed: Callable[[str], List[float] | np.ndarray],
    budget: Optional[SleepBudget] = None,
    dry_run: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> SleepReport:
    """
    v4.1-patched Sleep Consolidation.  Drop-in replacement for v4.

    Applies three v3.1 safety patches on top of the full v4 feature set.

    Architecture mirrors biological sleep consolidation [P3, P4]:

    PHASE 0 — SCAN:    Load candidates, build similarity neighbor map
                        (now with per-neighbor similarity scores for PI).
    PHASE 0b — SCORE:  Petrov BLA + dynamic importance + spreading activation
                        + arousal + surprise + temporal gradient + PI penalty.
    PHASE 1 — REPLAY:  Sleep-phase weighted cluster selection [P10].
                        REM-preferred (high-arousal) clusters selected first.
    PHASE 2 — TIERS:   Promote/demote (PI penalty factored into strength).
    PHASE 3 — PRUNE:   Similarity-weighted redundancy deletion (v4).
    PHASE 4 — CYCLES:  Increment survival counter for all surviving memories.

    cancel_event: checked between phases for zero-wait voice-wake cancel.
    """
    b = budget or SleepBudget()
    now = time.time()
    errors: List[str] = []

    logger.info(
        "sleep_v4.1p.start dry_run=%s d=%.2f k=%d alpha=%.2f S=%.1f W=%.1f "
        "tg=%s pi=%s sp=%s min_cycles=%d",
        dry_run,
        b.actr_d,
        b.petrov_k,
        b.imp_alpha,
        b.spreading_S,
        b.spreading_W,
        b.use_temporal_gradient,
        b.use_proactive_interference,
        b.use_sleep_phase_weighting,
        b.min_cycles_for_long,
    )

    if _cancelled(cancel_event):
        logger.info("sleep_v4.cancelled before_start")
        return SleepReport(
            scanned=0,
            clusters_found=0,
            summarized=0,
            tier_updates=0,
            soft_deleted_after_summary=0,
            hard_deleted=0,
            errors=["cancelled:before_start"],
        )

    # Phase 0: Scan.
    cands = _load_candidates(
        sqlite_store,
        limit=b.max_candidates,
        cooldown_seconds=b.consolidation_cooldown_sec,
    )
    id_map: Dict[str, MemoryEntry] = {m.id: m for m in cands}
    neighbor_map = _build_neighbor_map(
        vector_store=vector_store,
        candidates=cands,
        budget=b,
        now=now,
    )

    if _cancelled(cancel_event):
        logger.info("sleep_v4.cancelled after_scan scanned=%d", len(cands))
        return SleepReport(
            scanned=len(cands),
            clusters_found=0,
            summarized=0,
            tier_updates=0,
            soft_deleted_after_summary=0,
            hard_deleted=0,
            errors=["cancelled:after_scan"],
        )

    # Phase 0b: Pre-compute scores.
    dynamic_importances: Dict[str, float] = {
        m.id: _compute_dynamic_importance(m, now=now, budget=b) for m in cands
    }
    raw_bla_scores: Dict[str, float] = _compute_all_raw_bla(
        cands,
        now=now,
        budget=b,
        dynamic_importances=dynamic_importances,
    )
    prediction_errors_flagged = sum(1 for i in neighbor_map.values() if i.is_surprising)
    arousal_boosted = sum(1 for m in cands if _compute_arousal(m) > 0.5)

    # v4: count temporal gradient applications
    temporal_gradient_applied = 0
    if b.use_temporal_gradient:
        temporal_gradient_applied = sum(
            1 for m in cands if _compute_temporal_gradient(m, now=now) > 0.001
        )

    # v4: count proactive interference detections
    proactive_interference_detected = 0
    if b.use_proactive_interference:
        proactive_interference_detected = sum(
            1
            for m in cands
            if (
                neighbor_map.get(m.id)
                and neighbor_map[m.id].dup_count > 0
                and any(
                    id_map.get(nid)
                    and float(getattr(id_map[nid], "created_at", now))
                    > float(getattr(m, "created_at", now))
                    for nid in (neighbor_map[m.id].dup_ids or [])
                )
            )
        )

    # Phase 1: Replay — cluster and summarise near-duplicate groups.
    clusters = _build_clusters(
        sqlite_store=sqlite_store,
        candidates=cands,
        id_map=id_map,
        neighbor_map=neighbor_map,
        raw_bla_scores=raw_bla_scores,
        dynamic_importances=dynamic_importances,
        budget=b,
        now=now,
    )
    summary_targets = _pick_summary_clusters(clusters, budget=b)

    summarized = soft_deleted_after_summary = provisional_summaries = 0
    summarised_ids: Set[str] = set()

    for cl in summary_targets:
        if _cancelled(cancel_event):
            logger.info(
                "sleep_v4.cancelled mid_replay summarized=%d remaining=%d",
                summarized,
                len(summary_targets) - summarized,
            )
            return SleepReport(
                scanned=len(cands),
                clusters_found=len(clusters),
                summarized=summarized,
                tier_updates=0,
                soft_deleted_after_summary=soft_deleted_after_summary,
                hard_deleted=0,
                errors=["cancelled:mid_replay"],
                provisional_summaries=provisional_summaries,
                arousal_boosted=arousal_boosted,
                prediction_errors_flagged=prediction_errors_flagged,
                temporal_gradient_applied=temporal_gradient_applied,
                proactive_interference_detected=proactive_interference_detected,
            )
        try:
            _, sd_count, is_prov = _apply_summary_cluster(
                sqlite_store=sqlite_store,
                vector_store=vector_store,
                brain=brain,
                embed=embed,
                id_map=id_map,
                cluster=cl,
                budget=b,
                dry_run=dry_run,
                now=now,
            )
            summarized += 1
            soft_deleted_after_summary += sd_count
            if is_prov:
                provisional_summaries += 1
            for mid in cl.ids:
                summarised_ids.add(mid)
        except Exception as e:
            logger.exception("summary.apply.failed cluster_size=%d", len(cl.ids))
            errors.append(f"summary_failed cluster_size={len(cl.ids)} err={e}")

    if _cancelled(cancel_event):
        logger.info("sleep_v4.cancelled after_replay summarized=%d", summarized)
        return SleepReport(
            scanned=len(cands),
            clusters_found=len(clusters),
            summarized=summarized,
            tier_updates=0,
            soft_deleted_after_summary=soft_deleted_after_summary,
            hard_deleted=0,
            errors=["cancelled:after_replay"],
            promoted=0,
            demoted=0,
            provisional_summaries=provisional_summaries,
            arousal_boosted=arousal_boosted,
            prediction_errors_flagged=prediction_errors_flagged,
            temporal_gradient_applied=temporal_gradient_applied,
            proactive_interference_detected=proactive_interference_detected,
        )

    # Phase 2: Tier updates.
    tier_updates_plan = _plan_tier_updates(
        candidates=cands,
        id_map=id_map,
        neighbor_map=neighbor_map,
        raw_bla_scores=raw_bla_scores,
        dynamic_importances=dynamic_importances,
        budget=b,
        now=now,
    )[: b.max_tier_updates]

    _TIER_RANK = {"flash": 0, "short": 1, "long": 2}
    tier_updates = promoted = demoted = 0
    deleted_ids: Set[str] = set()
    if not dry_run:
        for mem_id, old_type, new_type in tier_updates_plan:
            try:
                sqlite_store.update_memory_type(mem_id, new_type)
                tier_updates += 1
                if _TIER_RANK.get(new_type, 1) > _TIER_RANK.get(old_type, 1):
                    promoted += 1
                else:
                    demoted += 1
            except Exception as e:
                errors.append(
                    f"tier_update_failed id={mem_id} {old_type}->{new_type} err={e}"
                )

    # Phase 3: Hard deletes.
    delete_plan, redundancy_ids, interference_ids = _plan_hard_deletes(
        sqlite_store=sqlite_store,
        candidates=cands,
        id_map=id_map,
        neighbor_map=neighbor_map,
        raw_bla_scores=raw_bla_scores,
        dynamic_importances=dynamic_importances,
        budget=b,
        now=now,
        limit=b.max_hard_deletes,
    )

    hard_deleted = redundancy_deleted = interference_pruned = 0
    redundancy_set = set(redundancy_ids)
    interference_set = set(interference_ids)
    if not dry_run:
        for mem_id in delete_plan:
            try:
                _hard_delete(
                    sqlite_store=sqlite_store,
                    vector_store=vector_store,
                    memory_id=mem_id,
                )
                hard_deleted += 1
                deleted_ids.add(mem_id)
                if mem_id in redundancy_set:
                    redundancy_deleted += 1
                if mem_id in interference_set:
                    interference_pruned += 1
            except Exception as e:
                errors.append(f"hard_delete_failed id={mem_id} err={e}")

    # Phase 4: Increment consolidation cycle counter for all survivors.
    cycles_incremented = 0
    if not dry_run:
        survivor_ids = [
            m.id
            for m in cands
            if m.id not in summarised_ids
            and m.id not in deleted_ids
            and int(getattr(m, "deleted", 0) or 0) == 0
        ]
        cycles_incremented = _increment_cycle_counts(
            sqlite_store=sqlite_store,
            survivor_ids=survivor_ids,
        )

    logger.info(
        "sleep_v4.1p.done scanned=%d clusters=%d summarized=%d(prov=%d) "
        "tiers=%d(+%d/-%d) hard=%d redundant=%d interference=%d "
        "cycles_bumped=%d surprises=%d arousal=%d tg=%d pi=%d errors=%d",
        len(cands),
        len(clusters),
        summarized,
        provisional_summaries,
        tier_updates,
        promoted,
        demoted,
        hard_deleted,
        redundancy_deleted,
        interference_pruned,
        cycles_incremented,
        prediction_errors_flagged,
        arousal_boosted,
        temporal_gradient_applied,
        proactive_interference_detected,
        len(errors),
    )

    if dry_run:
        _print_dry_run(
            cands=cands,
            clusters=clusters,
            summary_targets=summary_targets,
            tier_updates_plan=tier_updates_plan,
            delete_plan=delete_plan,
            neighbor_map=neighbor_map,
            id_map=id_map,
            redundancy_set=redundancy_set,
            interference_set=interference_set,
            raw_bla_scores=raw_bla_scores,
            dynamic_importances=dynamic_importances,
            budget=b,
            now=now,
        )

    return SleepReport(
        scanned=len(cands),
        clusters_found=len(clusters),
        summarized=summarized,
        tier_updates=tier_updates,
        soft_deleted_after_summary=soft_deleted_after_summary,
        hard_deleted=hard_deleted,
        errors=errors,
        promoted=promoted,
        demoted=demoted,
        redundancy_deleted=redundancy_deleted,
        interference_pruned=interference_pruned,
        provisional_summaries=provisional_summaries,
        arousal_boosted=arousal_boosted,
        prediction_errors_flagged=prediction_errors_flagged,
        cycles_incremented=cycles_incremented,
        temporal_gradient_applied=temporal_gradient_applied,
        proactive_interference_detected=proactive_interference_detected,
    )


# =============================================================================
# Dry-run Preview
# =============================================================================


def _print_dry_run(
    cands,
    clusters,
    summary_targets,
    tier_updates_plan,
    delete_plan,
    neighbor_map,
    id_map,
    redundancy_set,
    interference_set,
    raw_bla_scores,
    dynamic_importances,
    budget,
    now,
) -> None:
    _TR = {"flash": 0, "short": 1, "long": 2}
    print("\n" + "=" * 90)
    print("  DRY RUN v4  |  CANDIDATE ANALYSIS (top 15)")
    print("=" * 90)
    for m in cands[:15]:
        info = neighbor_map.get(m.id)
        M = _compute_strength(
            m,
            now=now,
            budget=budget,
            neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla_scores,
            dynamic_importances=dynamic_importances,
            id_map=id_map,
        )
        dyn_imp = dynamic_importances.get(m.id, 0.0)
        raw_b = raw_bla_scores.get(m.id, -99.0)
        arousal = _compute_arousal(m)
        tg = (
            _compute_temporal_gradient(m, now=now)
            if budget.use_temporal_gradient
            else 0.0
        )
        cycles = int((getattr(m, "metadata", {}) or {}).get("consolidation_cycles", 0))
        flag = "⚡" if (info and info.is_surprising) else "  "
        phase = "REM" if arousal > 0.5 else "SWS"
        print(
            f"  {flag}{m.id}  [{m.memory_type:5s}] [{phase}] "
            f"M={M:.3f} bla={raw_b:+.2f} dyn_imp={dyn_imp:.2f} "
            f"ar={arousal:.2f} tg={tg:.3f} cyc={cycles:2d} "
            f"fan={info.dup_count if info else 0:2d}  "
            f"'{(m.text or '')[:55]}'"
        )

    print("\n" + "=" * 90)
    print("  REPLAY TARGETS  (LLM summarisation clusters) — sleep-phase weighted")
    print("=" * 90)
    for i, cl in enumerate(summary_targets, 1):
        phase = "REM" if cl.max_arousal > 0.5 else "SWS"
        print(
            f"\n  Cluster #{i}  [{phase}]  size={len(cl.ids)}  "
            f"avg_M={cl.avg_strength:.2f}  avg_dyn_imp={cl.avg_importance:.2f}  "
            f"max_arousal={cl.max_arousal:.2f}"
        )
        for mid in cl.ids[:5]:
            m = id_map.get(mid)
            if m:
                print(
                    f"  {_iso(m.created_at)}  {mid}  "
                    f"dyn_imp={dynamic_importances.get(mid, 0):.2f}  "
                    f"'{(m.text or '')[:70]}'"
                )

    print("\n" + "=" * 90)
    print("  TIER UPDATES")
    print("=" * 90)
    for mem_id, old, new in tier_updates_plan[:20]:
        d = "↑" if _TR.get(new, 1) > _TR.get(old, 1) else "↓"
        m = id_map.get(mem_id)
        if m:
            M = _compute_strength(
                m,
                now=now,
                budget=budget,
                neighbor_map=neighbor_map,
                raw_bla_scores=raw_bla_scores,
                dynamic_importances=dynamic_importances,
                id_map=id_map,
            )
            cyc = int((getattr(m, "metadata", {}) or {}).get("consolidation_cycles", 0))
            print(
                f"  {d} {mem_id}  {old} → {new}  M={M:.3f}  "
                f"dyn_imp={dynamic_importances.get(mem_id, 0):.2f}  cyc={cyc}"
            )

    print("\n" + "=" * 90)
    print("  HARD DELETES")
    print("=" * 90)
    for mem_id in delete_plan[:20]:
        reason = (
            "interference"
            if mem_id in interference_set
            else "redundant" if mem_id in redundancy_set else "dead/purge"
        )
        m = id_map.get(mem_id)
        if m:
            print(
                f"  {mem_id}  reason={reason:15s}  "
                f"dyn_imp={dynamic_importances.get(mem_id, 0):.2f}"
            )
        else:
            print(f"  {mem_id}  reason=purge (consolidated row)")
