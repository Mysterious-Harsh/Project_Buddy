# buddy/memory/consolidation_engine.py
#
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  RESEARCH-GRADE MEMORY CONSOLIDATION ENGINE  v3.0                           ║
# ║  "Strength-Only Architecture" — No Categories, Fully Adaptive               ║
# ╠══════════════════════════════════════════════════════════════════════════════╣
# ║  THEORETICAL FOUNDATION                                                      ║
# ║                                                                              ║
# ║  The human brain stores NO category labels. "Episodic", "semantic",         ║
# ║  "procedural" are taxonomies invented by cognitive scientists to explain     ║
# ║  lesion-study patterns. The actual survival of a memory in the brain is      ║
# ║  determined by exactly four forces:                                          ║
# ║    1. Activation frequency + recency  (ACT-R base-level learning)           ║
# ║    2. Emotional arousal at encoding   (amygdala → norepinephrine boost)     ║
# ║    3. Prediction error / novelty      (dopamine-driven tagging)             ║
# ║    4. Association density             (spreading activation / fan effect)   ║
# ║  This engine models all four — and nothing else.                            ║
# ║                                                                              ║
# ║  WHAT CHANGED FROM v2                                                        ║
# ║  ✕ Removed: _classify_memory(), _EPISODIC_PATTERNS, has_semantic,          ║
# ║             use_episodic_semantic_split, all category-specific thresholds   ║
# ║  ✓ Added:   Petrov (2006) hybrid BLA  — more accurate than v2 approx       ║
# ║  ✓ Added:   Dynamic importance  — drifts toward access frequency            ║
# ║  ✓ Added:   Spreading activation  — neighbors boost each other              ║
# ║  ✓ Added:   Fan-effect interference  — high-fan dilutes activation          ║
# ║  ✓ Added:   Consolidation cycle counter  — promotion requires survival      ║
# ║                                                                              ║
# ║  KEY PAPERS                                                                  ║
# ║  [P1] Petrov (2006) "Computationally Efficient Approximation of BLA"        ║
# ║       ICCM 2006 — replaces Anderson 1982 standard approx (non-monotonic)   ║
# ║  [P2] Anderson & Reder (1999) "The Fan Effect"                              ║
# ║       Sji = S - ln(fan_j) — high fan dilutes/interferes with activation    ║
# ║  [P3] McClelland, McNaughton & O'Reilly (1995) CLS                         ║
# ║       Fast hippocampal store → slow neocortical store via replay            ║
# ║  [P4] Robinson et al. (2025) Neuron — large SWRs drive reactivation        ║
# ║       Sleep replay preferentially targets high-arousal, high-novelty items  ║
# ║  [P5] McGaugh (2004) Ann.Rev.Neurosci — arousal → amygdala → LTP boost    ║
# ║  [P6] Friston (2010) Nat.Rev.Neurosci — prediction error drives learning   ║
# ║  [P7] Tan et al. (2025) "Reflective Memory Management" — quality gating    ║
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

logger = get_logger("consolidation_v3")


# =============================================================================
# Module-level constants
# =============================================================================

_ACT_R_D: float = (
    0.5  # power-law decay exponent (Anderson 1998, validated 50+ datasets)
)
_PETROV_K: int = 3  # exact terms in Petrov hybrid; k=3 sufficient per paper
_IMP_ALPHA: float = 0.40  # importance reduces effective decay: d_eff = d*(1-alpha*imp)

_AROUSAL_MAX: float = (
    0.50  # max arousal amplification (50% boost for peak emotional content)
)
_AROUSAL_KEYWORDS: frozenset = frozenset([
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
])

_SURPRISE_SIM_MIN: float = 0.55  # similar enough to existing memory to be a correction
_SURPRISE_BOOST: float = 0.15  # strength bonus for prediction-error memories
_CONTRADICTION_PATTERNS = re.compile(
    r"\b(not|no longer|cancelled|fired|quit|left|resigned|actually|"
    r"correction|wrong|update|changed|instead|step.?down|failed|never|"
    r"stopped|ended|broke.?up|dissolved|bankrupt|retracted|clarif)\b",
    re.IGNORECASE,
)

# Fan effect [P2]: S=1.5 means fan must be > e^1.5 ≈ 4.5 before interference kicks in.
# At fan=2,  Sji = 1.5 - ln(2)  ≈  0.81 (positive spread)
# At fan=5,  Sji = 1.5 - ln(5)  ≈ -0.11 (very slight interference)
# At fan=20, Sji = 1.5 - ln(20) ≈ -1.50 (strong interference, no spread)
_SPREADING_S: float = 1.5
_SPREADING_W: float = 1.0  # total attentional weight budget

# Dynamic importance decay constants
_DYN_IMP_LAMBDA: float = 0.003  # per-day slow decay (half-life ≈ 230 days)
_DYN_IMP_ACCESS_WEIGHT: float = 0.35  # access frequency contribution
_DYN_IMP_AROUSAL_WEIGHT: float = 0.15  # encoding-time arousal contribution

# CLS cycle gate [P3]: must survive N sleep cycles before promotion to long-term store
_MIN_CYCLES_FOR_LONG: int = 2


# =============================================================================
# Budget
# =============================================================================


@dataclass(frozen=True)
class SleepBudget:
    # Scan limits
    max_candidates: int = 300
    top_k_neighbors: int = 20
    tau_dup: float = 0.80  # cosine similarity threshold for near-duplicates

    # Clustering
    max_cluster_size: int = 18
    min_cluster_size_to_summarize: int = 2
    max_summaries: int = 10

    # Tier update limits
    max_tier_updates: int = 200
    min_flash_age_sec: float = 3600.0  # don't touch flash memories < 1 hour old

    # Deletion budgets
    max_hard_deletes: int = 50
    hard_purge_soft_deleted_after_sec: float = 60.0 * 86400.0
    delete_dead_sec: float = 180.0 * 86400.0

    # Summary quality
    min_avg_importance_for_summary: float = 0.50
    min_total_chars_for_summary: int = 2000
    low_confidence_warn: float = 0.20
    reflective_confidence_min: float = 0.35  # [P7]

    # Tier thresholds — single uniform set, no per-category branches
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


# =============================================================================
# Internal structs
# =============================================================================


@dataclass(frozen=True)
class NeighborInfo:
    sim_max: float
    dup_ids: List[str]
    dup_count: int
    is_surprising: bool = False


@dataclass(frozen=True)
class Cluster:
    ids: List[str]
    avg_strength: float
    avg_importance: float  # dynamic importance
    total_chars: int
    has_long: bool


# =============================================================================
# [P1] Petrov (2006) Hybrid Base-Level Activation
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

    The key insight: the sum is dominated by the most recent access events
    (sharp transient peak). We compute the k most recent exactly and
    approximate the older n-k terms via a power-law integral.

    This fixes the standard approximation (Anderson 1982) which is
    non-monotonic with respect to d — rendering it invalid for studying
    individual differences in decay.

    Args:
        times_ago: Elapsed seconds since each access event (any order).
        d:         Decay exponent. Standard ACT-R value = 0.5.
        k:         Exact terms. k=3 sufficient per Petrov (2006).

    Returns:
        Log activation B. Negative values are valid. -inf if no accesses.
    """
    if not times_ago:
        return -math.inf

    # Sort ascending: ts[0] = most recent (smallest elapsed), ts[-1] = oldest.
    ts = sorted(max(1.0, float(t)) for t in times_ago)
    n = len(ts)
    k_eff = min(k, n)

    # Exact contribution from k most recent accesses (dominant terms).
    exact = sum(ts[i] ** (-d) for i in range(k_eff))

    if n <= k_eff:
        return math.log(exact) if exact > 0.0 else -math.inf

    # Power-law integral approximation for the older n-k terms.
    # Assumes locally uniform access density between t_boundary and t_oldest.
    t_boundary = ts[k_eff - 1]  # oldest of the k exact terms
    t_oldest = ts[-1]  # oldest access overall

    if t_oldest > t_boundary and d != 1.0:
        # Density of older accesses over [t_boundary, t_oldest]
        density = (n - k_eff) / (t_oldest - t_boundary)
        integral = density * (t_oldest ** (1 - d) - t_boundary ** (1 - d)) / (1 - d)
    else:
        # Degenerate: all older accesses clustered at same timestamp.
        integral = (n - k_eff) * t_boundary ** (-d)

    total = exact + max(0.0, integral)
    return math.log(total) if total > 0.0 else -math.inf


def _build_access_times(m: MemoryEntry, *, now: float) -> List[float]:
    """
    Reconstruct per-access timestamps from (created_at, last_accessed, access_count).
    Uses linear interpolation — an approximation because exact timestamps are
    not stored, but sufficient for BLA computation since recent terms dominate.
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
# Arousal [P5]
# =============================================================================


def _compute_arousal(m: MemoryEntry) -> float:
    """Estimate emotional arousal from text content. Returns [0, 1]."""
    text = str(getattr(m, "text", "") or "").lower()
    imp = float(getattr(m, "importance", 0.0) or 0.0)
    kw_hits = sum(1 for kw in _AROUSAL_KEYWORDS if kw in text)
    kw_score = min(1.0, kw_hits / 3.0)
    punct = text.count("!") + text.count("?")
    punct_score = min(1.0, punct / 3.0)
    return float(min(1.0, 0.50 * imp + 0.35 * kw_score + 0.15 * punct_score))


# =============================================================================
# Dynamic Importance
# =============================================================================


def _compute_dynamic_importance(
    m: MemoryEntry, *, now: float, budget: SleepBudget
) -> float:
    """
    Importance is NOT static.  It drifts toward access frequency and
    slowly decays away from the initial Brain-assigned salience.

        dynamic_imp = salience × e^{-λ·age_days}        (slow decay)
                    + access_weight × (access_rate/30d)  (frequency rises it)
                    + arousal_weight × arousal            (encoding-time boost)

    Consequence: a memory the user keeps referencing stays important
    regardless of how old it is.  A one-off high-salience memory
    eventually returns to low importance if never re-accessed.
    Emotionally charged memories are slightly protected even without re-access.
    """
    salience = float(max(0.0, min(1.0, float(getattr(m, "importance", 0.0) or 0.0))))
    acc = max(0, int(getattr(m, "access_count", 0) or 0))
    created_at = float(getattr(m, "created_at", now) or now)
    age_days = max(0.001, (now - created_at) / 86400.0)

    decayed = salience * math.exp(-budget.dyn_imp_lambda * age_days)
    rate = acc / age_days
    freq_contrib = budget.dyn_imp_access_weight * min(1.0, rate * 30.0)
    arousal_contrib = budget.dyn_imp_arousal_weight * _compute_arousal(m)

    return float(min(1.0, max(0.0, decayed + freq_contrib + arousal_contrib)))


# =============================================================================
# [P6] Prediction Error / Surprise
# =============================================================================


def _is_prediction_error(
    m: MemoryEntry, *, neighbor_info: Optional[NeighborInfo]
) -> bool:
    """
    A memory is a prediction error if:
    1. A near-duplicate already exists (same topic, sim >= threshold)
    2. The new memory contains contradiction/correction language

    These get a dopamine-driven encoding boost [P6] — the brain tags
    information that violates expectation for stronger consolidation.
    """
    if neighbor_info is None or neighbor_info.sim_max < _SURPRISE_SIM_MIN:
        return False
    return bool(_CONTRADICTION_PATTERNS.search(str(getattr(m, "text", "") or "")))


# =============================================================================
# [P2] Spreading Activation / Fan Effect
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

    Fan effect: if a neighbour j has many connections (high fan), the
    associative strength S_jm falls. When fan is large enough that
    S - ln(fan) < 0, the neighbour INTERFERES — it inhibits retrieval
    rather than boosting it. This is the core fan-effect mechanism [P2].

    Spreading activation is a bonus on top of BLA, capped at ±0.30.
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
            # Interference: over-associated neighbour actively inhibits.
            total += (budget.spreading_W / N) * s_jm * 0.10
        else:
            # Positive spread: neighbour boosts this memory.
            total += (budget.spreading_W / N) * s_jm * b_norm

    return float(max(-0.20, min(0.30, total)))


# =============================================================================
# Unified Strength Scorer
# =============================================================================


def _compute_all_raw_bla(
    candidates: List[MemoryEntry],
    *,
    now: float,
    budget: SleepBudget,
    dynamic_importances: Dict[str, float],
) -> Dict[str, float]:
    """
    Pre-compute raw (un-normalised) BLA for all candidates.

    Importance-modulated decay: d_eff = d × (1 - alpha × dyn_imp).
    High-importance memories have lower d → decay more slowly → higher BLA.
    This is the biologically-inspired mechanism: important information
    consolidates more robustly in both hippocampus and neocortex [P3].
    """
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
) -> float:
    """
    Unified strength score in [0, 1].  No categories.

    Components (in order of application):
      1. Petrov BLA → normalised via sigmoid        (primary survival signal)
      2. Spreading activation                        (association boost / fan interference)
      3. Arousal amplifier [P5]                      (emotional memories survive longer)
      4. Prediction error bonus [P6]                 (surprise → stronger encoding)
      5. Dynamic importance floor                    (high-salience memories floor at ~20%)

    Total: strength = max(imp_floor, sigmoid(BLA + spread) × arousal_amp + surprise)
    """
    info = neighbor_map.get(m.id)
    dyn_imp = dynamic_importances.get(
        m.id, _compute_dynamic_importance(m, now=now, budget=budget)
    )

    # 1. Petrov BLA normalised via sigmoid.
    raw_b = raw_bla_scores.get(m.id, -math.inf)
    bla_norm = 1.0 / (1.0 + math.exp(-raw_b)) if raw_b > -100 else 0.0

    # 2. Spreading activation (may be negative for high-fan interference).
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

    # 6. Dynamic importance floor: high-importance memories never score near 0
    #    even after long gaps without access.
    imp_floor = 0.20 * dyn_imp

    return float(max(imp_floor, min(1.0, amplified)))


# =============================================================================
# Candidates + Neighbor Map
# =============================================================================


def _load_candidates(sqlite_store: Any, *, limit: int) -> List[MemoryEntry]:
    cands = sqlite_store.list_candidates_for_consolidation(limit=limit)
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
            out[m.id] = NeighborInfo(sim_max=0.0, dup_ids=[], dup_count=0)
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
        for mid, score, _ in hits:
            if str(mid) == str(m.id):
                continue
            sc = float(score)
            sim_max = max(sim_max, sc)
            if sc >= budget.tau_dup:
                dup_ids.append(str(mid))

        prelim = NeighborInfo(sim_max=sim_max, dup_ids=dup_ids, dup_count=len(dup_ids))
        out[m.id] = NeighborInfo(
            sim_max=sim_max,
            dup_ids=dup_ids,
            dup_count=len(dup_ids),
            is_surprising=_is_prediction_error(m, neighbor_info=prelim),
        )
    return out


# =============================================================================
# Cluster Building
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
            )
            for x in ids
            if x in id_map
        ]
        if not strengths:
            continue

        dyn_imps = [dynamic_importances.get(x, 0.0) for x in ids]
        texts = [getattr(id_map.get(x), "text", "") or "" for x in ids if x in id_map]

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
            )
        )

    return clusters


def _cluster_priority_score(c: Cluster) -> float:
    """Priority = size × dynamic_importance × (0.5 + 0.5 × avg_strength). No category bonus."""
    return len(c.ids) * c.avg_importance * (0.5 + 0.5 * c.avg_strength)


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
    eligible.sort(key=_cluster_priority_score, reverse=True)
    return eligible[: budget.max_summaries]


def _iso(ts: Optional[float]) -> Optional[str]:
    """Float timestamp → ISO 8601 string, or None on failure."""
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))
    except Exception:
        return None


# =============================================================================
# [P7] Summary Application with Reflective Gating
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

    [P7] Provisional summaries (low LLM confidence) keep originals flagged
    for re-examination in 7 days to prevent catastrophic data loss.
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
            items.append((
                created_at,
                text,
            ))

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
            "provisional_expires_at": now + 7 * 86400 if is_provisional else None,
            "consolidation_cycles": 0,  # summary starts fresh, must earn promotion
        },
    )

    if not dry_run:
        sqlite_store.upsert_memory(summary_mem)
        vector_store.upsert(summary_mem)
        soft_deleted_count = 0
        for mid in cluster.ids:
            sqlite_store.mark_consolidated(mid, into_id=summary_mem.id)
            sqlite_store.soft_delete(mid)
            soft_deleted_count += 1
    else:
        soft_deleted_count = len(cluster.ids)

    return summary_mem.id, soft_deleted_count, is_provisional


# =============================================================================
# Consolidation Cycle Counter  [P3 CLS gate]
# =============================================================================


def _increment_cycle_counts(
    *,
    sqlite_store: Any,
    survivor_ids: List[str],
) -> int:
    """
    Increment metadata["consolidation_cycles"] for every memory that survived
    this consolidation run without being summarised or deleted.

    This implements the CLS requirement [P3]: memories must survive multiple
    sleep cycles before being eligible for long-term promotion, mirroring
    the gradual hippocampus → neocortex transfer process.

    Returns count of rows successfully updated.
    """
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
# Tier Updates  (strength-only, cycle gate for long promotion)
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

    flash → short:  M >= threshold  OR  dyn_imp >= high threshold
    short → long:   M >= threshold  AND  sufficiently unique  AND  cycles >= min
    short → flash:  M very low  AND  unaccessed for N days
    long → short:   M very low  AND  dyn_imp low  AND  aged out
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
            # Promote to long: requires strength, uniqueness, AND cycle survival.
            # The cycle gate mirrors the gradual CLS transfer [P3].
            if (
                M >= budget.short_to_long_strength
                and sim_max <= budget.short_to_long_max_sim
                and cycles >= budget.min_cycles_for_long
                and dyn_imp >= 0.30
            ):
                updates.append((m.id, cur_type, "long"))
                continue
            # Demote if too weak and stale.
            if (
                M <= budget.short_demote_strength
                and age_last_days > budget.short_demote_days
            ):
                updates.append((m.id, cur_type, "flash"))
            continue

        if cur_type == "long":
            if dyn_imp > budget.long_protected_imp:
                continue  # High dynamic importance → always protected
            if (
                M <= budget.long_demote_strength
                and age_last_days > budget.long_demote_days
                and dyn_imp <= 0.45
            ):
                updates.append((m.id, cur_type, "short"))

    return updates


# =============================================================================
# Hard Deletes  (pure strength, no category protection)
# =============================================================================


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
    """Select the weakest near-duplicate for interference pruning [P2]."""
    if len(dup_ids) < budget.interference_dup_min:
        return None
    m_str = _compute_strength(
        m,
        now=now,
        budget=budget,
        neighbor_map=neighbor_map,
        raw_bla_scores=raw_bla_scores,
        dynamic_importances=dynamic_importances,
    )
    weakest_id: Optional[str] = None
    weakest_str = m_str
    for did in dup_ids:
        dup = id_map.get(did)
        if dup is None or int(getattr(dup, "deleted", 0) or 0) == 1:
            continue
        if dynamic_importances.get(did, 0.0) > 0.50:
            continue  # protect high-importance duplicates
        d_str = _compute_strength(
            dup,
            now=now,
            budget=budget,
            neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla_scores,
            dynamic_importances=dynamic_importances,
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
        if db_path:
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
        )

        # Dead trace: never accessed, negligible importance, very old.
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

        # Redundant: many near-duplicates, low importance, low access, old, weak.
        if (
            dup_count >= budget.redundancy_dup_threshold
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

        # Interference pruning: select weakest duplicate only when m is weak [P2].
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
    """Return True if a cancel event has been set (None-safe)."""
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
    v3 Sleep Consolidation.  Drop-in replacement for v2.

    Architecture mirrors biological sleep consolidation [P3, P4]:

    PHASE 0 — SCAN:    Load candidates, build similarity neighbor map.
    PHASE 0b — SCORE:  Compute Petrov BLA, dynamic importance, spreading
                        activation, arousal, surprise for every candidate.
                        All pre-computed so later calls are O(1) lookups.
    PHASE 1 — REPLAY:  Select high-activation clusters for LLM summarisation.
                        Analogous to SWR-driven hippocampal replay [P4]:
                        the brain selects emotionally-tagged, novel memories
                        for preferential reactivation during sleep.
    PHASE 2 — TIERS:   Promote/demote based on strength + cycle count.
                        Cycle gate mirrors gradual CLS transfer [P3].
    PHASE 3 — PRUNE:   Hard-delete dead traces, redundant, and interfering.
    PHASE 4 — CYCLES:  Increment survival counter for all surviving memories.

    cancel_event: when set, the engine finishes its current sub-operation
    and returns a partial SleepReport.  Zero wait for voice wake — the
    thread exits during STT transcription (2-5 s) at the next checkpoint.
    """
    b = budget or SleepBudget()
    now = time.time()
    errors: List[str] = []

    logger.info(
        "sleep_v3.start dry_run=%s d=%.2f k=%d alpha=%.2f S=%.1f W=%.1f min_cycles=%d",
        dry_run,
        b.actr_d,
        b.petrov_k,
        b.imp_alpha,
        b.spreading_S,
        b.spreading_W,
        b.min_cycles_for_long,
    )

    # ── CHECKPOINT 0: before any work ────────────────────────────────────────
    if _cancelled(cancel_event):
        logger.info("sleep_v3.cancelled before_start")
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
    cands = _load_candidates(sqlite_store, limit=b.max_candidates)
    id_map: Dict[str, MemoryEntry] = {m.id: m for m in cands}
    neighbor_map = _build_neighbor_map(
        vector_store=vector_store,
        candidates=cands,
        budget=b,
        now=now,
    )

    # ── CHECKPOINT 1: after scan ──────────────────────────────────────────────
    if _cancelled(cancel_event):
        logger.info("sleep_v3.cancelled after_scan scanned=%d", len(cands))
        return SleepReport(
            scanned=len(cands),
            clusters_found=0,
            summarized=0,
            tier_updates=0,
            soft_deleted_after_summary=0,
            hard_deleted=0,
            errors=["cancelled:after_scan"],
        )

    # Phase 0b: Pre-compute scores (batched for efficiency).
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
        # ── CHECKPOINT 2: before each LLM call ───────────────────────────────
        if _cancelled(cancel_event):
            logger.info(
                "sleep_v3.cancelled mid_replay summarized=%d remaining=%d",
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

    # ── CHECKPOINT 3: after replay ────────────────────────────────────────────
    if _cancelled(cancel_event):
        logger.info("sleep_v3.cancelled after_replay summarized=%d", summarized)
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
        )

    # Phase 2: Tier updates — strength + cycle gate.
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
        "sleep_v3.done scanned=%d clusters=%d summarized=%d(prov=%d) "
        "tiers=%d(+%d/-%d) hard=%d redundant=%d interference=%d "
        "cycles_bumped=%d surprises=%d arousal=%d errors=%d",
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
    print("\n" + "=" * 86)
    print("  DRY RUN v3  |  CANDIDATE ANALYSIS (top 15)")
    print("=" * 86)
    for m in cands[:15]:
        info = neighbor_map.get(m.id)
        M = _compute_strength(
            m,
            now=now,
            budget=budget,
            neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla_scores,
            dynamic_importances=dynamic_importances,
        )
        dyn_imp = dynamic_importances.get(m.id, 0.0)
        raw_b = raw_bla_scores.get(m.id, -99.0)
        arousal = _compute_arousal(m)
        cycles = int((getattr(m, "metadata", {}) or {}).get("consolidation_cycles", 0))
        flag = "⚡" if (info and info.is_surprising) else "  "
        print(
            f"  {flag}{m.id}  [{m.memory_type:5s}] "
            f"M={M:.3f} bla={raw_b:+.2f} dyn_imp={dyn_imp:.2f} "
            f"ar={arousal:.2f} cyc={cycles:2d} fan={info.dup_count if info else 0:2d}  "
            f"'{(m.text or '')[:60]}'"
        )

    print("\n" + "=" * 86)
    print("  REPLAY TARGETS  (LLM summarisation clusters)")
    print("=" * 86)
    for i, cl in enumerate(summary_targets, 1):
        print(
            f"\n  Cluster #{i}  size={len(cl.ids)}  "
            f"avg_M={cl.avg_strength:.2f}  avg_dyn_imp={cl.avg_importance:.2f}"
        )
        for mid in cl.ids[:5]:
            m = id_map.get(mid)

            if m:
                print(
                    f"{_iso(m.created_at)} "
                    f"   {mid}  dyn_imp={dynamic_importances.get(mid,0):.2f} "
                    f" '{(m.text or '')[:75]}'"
                )

    print("\n" + "=" * 86)
    print("  TIER UPDATES")
    print("=" * 86)
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
            )
            cyc = int((getattr(m, "metadata", {}) or {}).get("consolidation_cycles", 0))
            print(
                f"  {d} {mem_id}  {old} → {new}  M={M:.3f}  "
                f"dyn_imp={dynamic_importances.get(mem_id,0):.2f}  cyc={cyc}"
            )

    print("\n" + "=" * 86)
    print("  HARD DELETES")
    print("=" * 86)
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
                f"dyn_imp={dynamic_importances.get(mem_id,0):.2f}"
            )
        else:
            print(f"  {mem_id}  reason=purge (consolidated row)")
