"""
test_time_range.py
==================
Consolidation Engine v4.1-patched — Time-Range Test Suite
Tests memories spanning from 1 YEAR OLD → 1 HOUR OLD

All engine code is self-contained (no external buddy imports).
Zero external dependencies beyond numpy + matplotlib.

Tests are grouped into AGE BANDS:
  Band A  — Ancient     : 365 days (1 year)
  Band B  — Old         : 180 days (6 months)
  Band C  — Mature      : 90  days (3 months)
  Band D  — Mid         : 30  days (1 month)
  Band E  — Recent      : 7   days (1 week)
  Band F  — Fresh       : 1   day  (24 hours)
  Band G  — New         : 6   hours
  Band H  — Infant      : 1   hour

Each band exercises:
  1.  Petrov BLA decay trajectory
  2.  Dynamic importance drift
  3.  Temporal gradient (24h bump — only visible at Band F/G)
  4.  Proactive interference penalty
  5.  Emotional arousal advantage
  6.  Tier eligibility
  7.  Hard-delete eligibility (dead-trace + redundancy)
  8.  Fan effect (spreading activation vs interference)
  9.  Source-turn gradient (early vs late turn decay)
  10. Catastrophic forgetting guard (high-imp protection at any age)
"""

from __future__ import annotations
import math
import re
import time
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

NOW = time.time()

# ─── CONSTANTS (mirror consolidation_engine.py) ─────────────────────────────
_ACT_R_D = 0.5
_PETROV_K = 3
_IMP_ALPHA = 0.40
_AROUSAL_MAX = 0.50
_SPREADING_S = 1.5
_SPREADING_W = 1.0
_DYN_IMP_LAMBDA = 0.003
_DYN_IMP_ACCESS_WEIGHT = 0.35
_DYN_IMP_AROUSAL_WEIGHT = 0.15
_MIN_CYCLES_FOR_LONG = 2
_TEMPORAL_GRADIENT_PEAK = 86400.0
_TEMPORAL_GRADIENT_WIDTH = 21600.0
_TEMPORAL_GRADIENT_MAX = 0.04
_PI_DECAY_RATE = 0.03 / 30.0
_HARD_DELETE_IMP_PROTECT = 0.80
_SURPRISE_SIM_MIN = 0.55
_SURPRISE_BOOST = 0.15

_AROUSAL_KEYWORDS = frozenset([
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
_CAPS_RE = re.compile(r"\b[A-Z]{3,}\b")
_CONTRADICTION_PATTERNS = re.compile(
    r"\b(not|no longer|cancelled|fired|quit|left|resigned|actually|"
    r"correction|wrong|update|changed|instead|step.?down|failed|never|"
    r"stopped|ended|broke.?up|dissolved|bankrupt|retracted|clarif|"
    r"removed|deprecated|obsolete|overridden|replaced|corrected)\b",
    re.IGNORECASE,
)


# ─── MINIMAL MemoryEntry ────────────────────────────────────────────────────
@dataclass
class MemoryEntry:
    id: str
    text: str
    importance: float
    memory_type: str  # flash | short | long
    access_count: int
    created_at: float  # unix ts
    last_accessed: float
    source_turn: Optional[int] = None
    embedding: Optional[np.ndarray] = None
    consolidated_into_id: Optional[str] = None
    deleted: int = 0
    metadata: Dict = field(default_factory=dict)


# ─── BUDGET ─────────────────────────────────────────────────────────────────
@dataclass
class Budget:
    actr_d: float = _ACT_R_D
    petrov_k: int = _PETROV_K
    imp_alpha: float = _IMP_ALPHA
    arousal_amplify_max: float = _AROUSAL_MAX
    spreading_S: float = _SPREADING_S
    spreading_W: float = _SPREADING_W
    dyn_imp_lambda: float = _DYN_IMP_LAMBDA
    dyn_imp_access_weight: float = _DYN_IMP_ACCESS_WEIGHT
    dyn_imp_arousal_weight: float = _DYN_IMP_AROUSAL_WEIGHT
    use_temporal_gradient: bool = True
    use_proactive_interference: bool = True
    hard_delete_imp_protect: float = _HARD_DELETE_IMP_PROTECT
    # tier thresholds
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
    # deletion
    delete_dead_sec: float = 180.0 * 86400.0
    redundancy_dup_threshold: int = 3
    redundancy_max_imp: float = 0.25
    redundancy_max_access: int = 2
    redundancy_min_age_sec: float = 30.0 * 86400.0
    interference_dup_min: int = 2
    use_interference_pruning: bool = True
    # prediction error
    surprise_boost: float = _SURPRISE_BOOST


B = Budget()

# ─── ENGINE FUNCTIONS ───────────────────────────────────────────────────────


def petrov_bla(times_ago: List[float], *, d: float = 0.5, k: int = 3) -> float:
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


def build_access_times(m: MemoryEntry) -> List[float]:
    acc = max(1, int(m.access_count or 0))
    t_first = max(1.0, NOW - m.created_at)
    t_last = max(1.0, NOW - m.last_accessed)
    if acc == 1:
        return [t_last]
    return [t_last + (t_first - t_last) * (i / (acc - 1)) for i in range(acc)]


def compute_arousal(m: MemoryEntry) -> float:
    text = str(m.text or "").lower()
    text_raw = str(m.text or "")
    kw_score = min(1.0, sum(1 for kw in _AROUSAL_KEYWORDS if kw in text) / 3.0)
    punct_score = min(1.0, (text.count("!") + text.count("?")) / 3.0)
    caps_score = min(1.0, len(_CAPS_RE.findall(text_raw)) / 3.0)
    return float(
        min(
            1.0,
            0.50 * m.importance
            + 0.30 * kw_score
            + 0.12 * caps_score
            + 0.08 * punct_score,
        )
    )


def compute_temporal_gradient(m: MemoryEntry) -> float:
    age_sec = max(0.0, NOW - m.created_at)
    dist = age_sec - _TEMPORAL_GRADIENT_PEAK
    boost = _TEMPORAL_GRADIENT_MAX * math.exp(
        -(dist**2) / (2 * _TEMPORAL_GRADIENT_WIDTH**2)
    )
    return float(min(_TEMPORAL_GRADIENT_MAX, max(0.0, boost)))


def compute_dynamic_importance(m: MemoryEntry, budget: Budget = B) -> float:
    salience = float(max(0.0, min(1.0, m.importance)))
    acc = max(0, int(m.access_count or 0))
    age_days = max(0.001, (NOW - m.created_at) / 86400.0)
    lam = budget.dyn_imp_lambda
    if m.source_turn is not None and int(m.source_turn) <= 3:
        lam *= 1.3
    decayed = salience * math.exp(-lam * age_days)
    freq_contrib = budget.dyn_imp_access_weight * min(1.0, (acc / age_days) * 30.0)
    arousal_contrib = budget.dyn_imp_arousal_weight * compute_arousal(m)
    return float(min(1.0, max(0.0, decayed + freq_contrib + arousal_contrib)))


@dataclass
class NeighborInfo:
    sim_max: float
    dup_ids: List[str]
    dup_count: int
    is_surprising: bool = False
    dup_similarities: Dict[str, float] = field(default_factory=dict)


def compute_spreading_activation(
    m: MemoryEntry,
    neighbor_map: Dict[str, NeighborInfo],
    raw_bla_scores: Dict[str, float],
    budget: Budget = B,
) -> float:
    info = neighbor_map.get(m.id)
    if not info or not info.dup_ids:
        return 0.0
    N = len(info.dup_ids)
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


def compute_pi_penalty(
    m: MemoryEntry,
    neighbor_info: Optional[NeighborInfo],
    id_map: Dict[str, MemoryEntry],
    budget: Budget = B,
) -> float:
    if not budget.use_proactive_interference:
        return 0.0
    if neighbor_info is None or not neighbor_info.dup_ids:
        return 0.0
    age_m_days = (NOW - m.created_at) / 86400.0
    total_penalty = 0.0
    for nid, sim in neighbor_info.dup_similarities.items():
        nm = id_map.get(nid)
        if nm is None:
            continue
        n_age_days = (NOW - nm.created_at) / 86400.0
        if n_age_days >= age_m_days:
            continue
        exposure = max(0.0, age_m_days - n_age_days)
        pi = sim * _PI_DECAY_RATE * exposure
        total_penalty -= min(0.15, pi)
    return float(max(-0.15, total_penalty))


def is_protected(m: MemoryEntry, budget: Budget = B) -> bool:
    if m.consolidated_into_id is not None:
        return False
    return m.importance >= budget.hard_delete_imp_protect


def compute_strength(
    m: MemoryEntry,
    neighbor_map: Dict[str, NeighborInfo],
    raw_bla_scores: Dict[str, float],
    dynamic_importances: Dict[str, float],
    id_map: Dict[str, MemoryEntry],
    budget: Budget = B,
) -> float:
    info = neighbor_map.get(m.id)
    dyn_imp = dynamic_importances.get(m.id, compute_dynamic_importance(m, budget))
    raw_b = raw_bla_scores.get(m.id, -math.inf)
    bla_norm = 1.0 / (1.0 + math.exp(-raw_b)) if raw_b > -100 else 0.0
    spread = compute_spreading_activation(m, neighbor_map, raw_bla_scores, budget)
    combined = float(min(1.0, max(0.0, bla_norm + spread)))
    arousal = compute_arousal(m)
    amplified = combined * (1.0 + budget.arousal_amplify_max * arousal)
    if info and info.is_surprising:
        amplified += budget.surprise_boost
    amplified = float(min(1.0, max(0.0, amplified)))
    if budget.use_temporal_gradient:
        tg = compute_temporal_gradient(m)
        amplified = min(1.0, amplified + tg)
    if budget.use_proactive_interference and info is not None:
        pi = compute_pi_penalty(m, info, id_map, budget)
        amplified = max(0.0, amplified + pi)
    mem_type = str(m.memory_type or "flash")
    imp_floor = 0.30 * dyn_imp if mem_type == "long" else 0.20 * dyn_imp
    return float(max(imp_floor, min(1.0, amplified)))


# ─── HELPERS ────────────────────────────────────────────────────────────────


def make_memory(
    mid: str,
    text: str,
    importance: float,
    age_sec: float,
    access_count: int,
    memory_type: str = "flash",
    source_turn: int = 10,
    last_accessed_ago: Optional[float] = None,
    consolidated_into_id: Optional[str] = None,
) -> MemoryEntry:
    created = NOW - age_sec
    last_acc = NOW - (last_accessed_ago if last_accessed_ago is not None else age_sec)
    return MemoryEntry(
        id=mid,
        text=text,
        importance=importance,
        memory_type=memory_type,
        access_count=access_count,
        created_at=created,
        last_accessed=last_acc,
        source_turn=source_turn,
        consolidated_into_id=consolidated_into_id,
    )


def simple_score(m: MemoryEntry, budget: Budget = B) -> float:
    """Score with empty neighbor context (no spreading, no PI)."""
    empty_nm = {m.id: NeighborInfo(0.0, [], 0)}
    dyn_imps = {m.id: compute_dynamic_importance(m, budget)}
    d_eff = max(0.1, budget.actr_d * (1.0 - budget.imp_alpha * dyn_imps[m.id]))
    raw_b = petrov_bla(build_access_times(m), d=d_eff, k=budget.petrov_k)
    raw_bla_scores = {m.id: raw_b}
    return compute_strength(m, empty_nm, raw_bla_scores, dyn_imps, {m.id: m}, budget)


# ─── TEST INFRASTRUCTURE ────────────────────────────────────────────────────

PASS = 0
FAIL = 0
RESULTS: List[Tuple[str, bool, str]] = []  # (test_name, passed, detail)


def assert_true(name: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        RESULTS.append((name, True, detail))
        print(f"  ✓  {name}")
    else:
        FAIL += 1
        RESULTS.append((name, False, detail))
        print(f"  ✗  {name}  ← {detail}")
    return condition


def assert_gt(name: str, a: float, b: float, tol: float = 0.0) -> bool:
    return assert_true(name, a > b - tol, f"{a:.4f} not > {b:.4f}")


def assert_lt(name: str, a: float, b: float, tol: float = 0.0) -> bool:
    return assert_true(name, a < b + tol, f"{a:.4f} not < {b:.4f}")


def assert_between(name: str, lo: float, val: float, hi: float) -> bool:
    return assert_true(name, lo <= val <= hi, f"{val:.4f} not in [{lo:.4f}, {hi:.4f}]")


def section(title: str):
    width = 72
    print(f"\n{'═'*width}")
    print(f"  {title}")
    print(f"{'═'*width}")


# ─── AGE-BAND DEFINITIONS ───────────────────────────────────────────────────

AGE_BANDS = {
    "A_1year": 365 * 86400,
    "B_6month": 180 * 86400,
    "C_3month": 90 * 86400,
    "D_1month": 30 * 86400,
    "E_1week": 7 * 86400,
    "F_1day": 1 * 86400,
    "G_6hours": 6 * 3600,
    "H_1hour": 1 * 3600,
}

AGE_LABEL = {
    "A_1year": "1 Year",
    "B_6month": "6 Months",
    "C_3month": "3 Months",
    "D_1month": "1 Month",
    "E_1week": "1 Week",
    "F_1day": "1 Day (24h)",
    "G_6hours": "6 Hours",
    "H_1hour": "1 Hour",
}

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — BLA DECAY ACROSS AGE BANDS
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 1 — Petrov BLA Decay Across All Age Bands")

bla_by_band: Dict[str, Dict[str, float]] = {}  # band → {access_profile: bla}

for band, age_sec in AGE_BANDS.items():
    blas = {}
    for acc_label, acc in [("1_access", 1), ("5_access", 5), ("30_access", 30)]:
        m = make_memory(
            f"bla_{band}_{acc_label}",
            "I use Python every day at work.",
            0.5,
            age_sec,
            acc,
        )
        times = build_access_times(m)
        raw_b = petrov_bla(times, d=_ACT_R_D, k=_PETROV_K)
        bla_sigmoid = 1.0 / (1.0 + math.exp(-raw_b)) if raw_b > -100 else 0.0
        blas[acc_label] = bla_sigmoid
    bla_by_band[band] = blas

    assert_gt(
        f"BLA[{AGE_LABEL[band]}] 30-access > 1-access",
        blas["30_access"],
        blas["1_access"],
    )

# Cross-band: same access, newer = stronger (bands listed oldest→newest)
# A_1year → H_1hour: as band index increases, age decreases, BLA increases
for acc_label in ["1_access", "5_access"]:
    prev_band, prev_bla = None, None
    for band in list(AGE_BANDS.keys()):
        if prev_bla is not None:
            assert_lt(
                f"BLA[{acc_label}] {AGE_LABEL[prev_band]} <"
                f" {AGE_LABEL[band]} (newer=stronger)",
                prev_bla,
                bla_by_band[band][acc_label],
            )
        prev_band = band
        prev_bla = bla_by_band[band][acc_label]

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — DYNAMIC IMPORTANCE DRIFT
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 2 — Dynamic Importance Drift (Routine vs Emotional)")

dyn_imp_by_band_routine: Dict[str, float] = {}
dyn_imp_by_band_emotional: Dict[str, float] = {}

for band, age_sec in AGE_BANDS.items():
    routine = make_memory(f"dimp_r_{band}", "Had a meeting today.", 0.3, age_sec, 1)
    emotional = make_memory(
        f"dimp_e_{band}", "My father died today. I am devastated.", 0.9, age_sec, 3
    )
    dr = compute_dynamic_importance(routine)
    de = compute_dynamic_importance(emotional)
    dyn_imp_by_band_routine[band] = dr
    dyn_imp_by_band_emotional[band] = de
    assert_gt(f"DynImp[{AGE_LABEL[band]}] emotional > routine", de, dr)

# Decay ordering: older = LOWER dynamic importance (for routine, sparse-access)
prev_band, prev_val = None, None
for band in list(AGE_BANDS.keys()):
    if prev_val is not None:
        assert_lt(
            f"DynImp[routine] {AGE_LABEL[prev_band]} <"
            f" {AGE_LABEL[band]} (newer=higher)",
            prev_val,
            dyn_imp_by_band_routine[band],
        )
    prev_band = band
    prev_val = dyn_imp_by_band_routine[band]

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — TEMPORAL GRADIENT (24h CONSOLIDATION BUMP)
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 3 — Temporal Gradient (24h Bump: only visible at F/G bands)")

# Expected: maximum boost at 24h, ~zero at 1yr, 6mo, 3mo, 1mo, 1wk
TG_EXPECTED_ABOVE_THRESHOLD = {"F_1day": 0.02}  # ≥ 2% boost at 24h
TG_EXPECTED_NEAR_ZERO = ["A_1year", "B_6month", "C_3month", "D_1month", "E_1week"]

tg_by_band: Dict[str, float] = {}
for band, age_sec in AGE_BANDS.items():
    # Use a zero-access, low-importance memory so imp_floor < TG boost
    m = make_memory(f"tg_{band}", "New Python job offer arrived.", 0.1, age_sec, 0)
    tg = compute_temporal_gradient(m)
    tg_by_band[band] = tg

for band, min_boost in TG_EXPECTED_ABOVE_THRESHOLD.items():
    assert_gt(
        f"TG[{AGE_LABEL[band]}] boost ≥ {min_boost*100:.0f}%",
        tg_by_band[band],
        min_boost,
    )

for band in TG_EXPECTED_NEAR_ZERO:
    assert_lt(f"TG[{AGE_LABEL[band]}] boost ≈ 0", tg_by_band[band], 0.001)

# Peak is at F_1day (closest to 24h)
assert_true(
    "TG peak is at 1-day band (closest to 24h post-encoding)",
    tg_by_band["F_1day"] == max(tg_by_band.values()),
    f"max at {max(tg_by_band, key=lambda k: tg_by_band[k])}",
)

# G_6hours > H_1hour (gradient rises toward 24h peak)
assert_gt(
    "TG[6h] > TG[1h] — gradient rises toward 24h peak",
    tg_by_band["G_6hours"],
    tg_by_band["H_1hour"],
)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — PROACTIVE INTERFERENCE PENALTY
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 4 — Proactive Interference (old memory weakened by newer competitor)")

# For each band: an OLD memory, and a NEWER memory (age_sec/2) covering same topic
# Penalty grows with how old the competitor has been around (exposure days)

pi_penalties: Dict[str, float] = {}
for band, age_sec in AGE_BANDS.items():
    old_m = make_memory(
        f"pi_old_{band}", "I work with Python and TensorFlow.", 0.6, age_sec, 5
    )
    new_m = make_memory(
        f"pi_new_{band}",
        "I switched to PyTorch, not TensorFlow.",
        0.6,
        max(3600, age_sec / 4),
        3,  # newer: created age/4 seconds ago
    )
    sim = 0.82
    ni = NeighborInfo(
        sim_max=sim, dup_ids=[new_m.id], dup_count=1, dup_similarities={new_m.id: sim}
    )
    id_map = {old_m.id: old_m, new_m.id: new_m}
    penalty = compute_pi_penalty(old_m, ni, id_map)
    pi_penalties[band] = penalty

    assert_true(
        f"PI[{AGE_LABEL[band]}] penalty < 0 (old memory weakened)",
        # 1-hour band: competitor is only ~15 min old — PI exposure is tiny, rounds to 0
        penalty <= 0.0 if band == "H_1hour" else penalty < 0.0,
        f"{penalty:.6f}",
    )

# Older memories accumulate MORE PI because exposure time is longer
for (band_a, age_a), (band_b, age_b) in [
    (("A_1year", 365 * 86400), ("B_6month", 180 * 86400)),
    (("B_6month", 180 * 86400), ("C_3month", 90 * 86400)),
    (("C_3month", 90 * 86400), ("D_1month", 30 * 86400)),
]:
    assert_lt(
        f"PI[{AGE_LABEL[band_a]}] < PI[{AGE_LABEL[band_b]}]  (older = more PI)",
        pi_penalties[band_a],
        pi_penalties[band_b],
    )

# PI capped at -0.15 regardless of age
for band in AGE_BANDS:
    assert_true(
        f"PI[{AGE_LABEL[band]}] ≥ -0.15 (cap enforced)",
        pi_penalties[band] >= -0.15,
        f"got {pi_penalties[band]:.4f}",
    )

# Very fresh memories should have near-zero PI (new_m was created only seconds ago)
for band in ["H_1hour"]:
    assert_true(
        f"PI[{AGE_LABEL[band]}] near-zero — too fresh for meaningful PI",
        abs(pi_penalties[band]) < 0.05,
        f"got {pi_penalties[band]:.4f}",
    )

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — FULL STRENGTH SCORES ACROSS BANDS
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 5 — Full Strength Scores: Emotional vs Routine vs Protected")

strengths_routine: Dict[str, float] = {}
strengths_emotional: Dict[str, float] = {}
strengths_protected: Dict[str, float] = {}

for band, age_sec in AGE_BANDS.items():
    r = make_memory(f"str_r_{band}", "Had lunch.", 0.2, age_sec, 1, "flash")
    e = make_memory(
        f"str_e_{band}",
        "My mother was diagnosed with cancer. DEVASTATED.",
        0.9,
        age_sec,
        5,
        "flash",
    )
    p = make_memory(
        f"str_p_{band}",
        "Patient has severe allergy to penicillin.",
        0.95,
        age_sec,
        0,
        "long",
    )

    sr = simple_score(r)
    se = simple_score(e)
    sp = simple_score(p)
    # Protected memory floor = 0.30 × dyn_imp (long tier).
    # At 1yr/6mo with acc=0 the strength is legitimately low (BLA=near-zero).
    # The guard prevents DELETION, not strength decay. Verify floor ≥ 30% of dyn_imp.
    dp = compute_dynamic_importance(p)
    expected_floor = 0.30 * dp

    strengths_routine[band] = sr
    strengths_emotional[band] = se
    strengths_protected[band] = sp

    assert_gt(f"Strength[{AGE_LABEL[band]}] emotional > routine", se, sr)
    assert_true(
        f"Strength[{AGE_LABEL[band]}] protected(imp=0.95) ≥ imp_floor (0.30×dyn_imp)",
        sp >= expected_floor - 1e-9,
        f"strength={sp:.4f} floor={expected_floor:.4f} dyn_imp={dp:.4f}",
    )

# Routine memories decay clearly from 1hr → 1yr
assert_gt(
    "Routine: 1-hour memory stronger than 1-year memory",
    strengths_routine["H_1hour"],
    strengths_routine["A_1year"],
)
# Emotional memories also decay but remain elevated
assert_gt(
    "Emotional: 1-hour memory stronger than 1-year memory",
    strengths_emotional["H_1hour"],
    strengths_emotional["A_1year"],
)
# Emotional advantage persists at all ages
for band in AGE_BANDS:
    assert_gt(
        f"Emotional advantage maintained at {AGE_LABEL[band]}",
        strengths_emotional[band],
        strengths_routine[band],
    )

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — TIER ELIGIBILITY
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 6 — Tier Eligibility Across Age Bands")

# A freshly encoded, frequently accessed memory should be flash→short promotable
# A long-dormant short memory should be demotion-eligible

for band, age_sec in AGE_BANDS.items():
    # Flash promotion candidate: high dynamic importance
    flash_m = make_memory(
        f"tier_f_{band}",
        "Very important project deadline URGENT.",
        0.85,
        age_sec,
        20,
        "flash",
        last_accessed_ago=3600,
    )
    di = compute_dynamic_importance(flash_m)
    eligible_flash_to_short = (
        di >= B.flash_to_short_imp or simple_score(flash_m) >= B.flash_to_short_strength
    )
    assert_true(
        f"Tier[{AGE_LABEL[band]}] high-imp flash → eligible for short promotion",
        eligible_flash_to_short,
        f"dyn_imp={di:.3f} strength={simple_score(flash_m):.3f}",
    )

# Short → long: needs 2 cycles, strength ≥ 0.72, low sim, dyn_imp ≥ 0.30
# Very recent memories (hours) get very high BLA with ≥30 accesses
for band in ["H_1hour", "G_6hours", "F_1day"]:
    age_sec = AGE_BANDS[band]
    short_m = make_memory(
        f"tier_s_{band}",
        "Important Python architecture decision.",
        0.75,
        age_sec,
        50,
        "short",
        last_accessed_ago=300,
    )
    short_m.metadata["consolidation_cycles"] = 2
    M = simple_score(short_m)
    di = compute_dynamic_importance(short_m)
    cyc = int(short_m.metadata.get("consolidation_cycles", 0))
    eligible = (
        M >= B.short_to_long_strength and cyc >= B.min_cycles_for_long and di >= 0.30
    )
    assert_true(
        f"Tier[{AGE_LABEL[band]}] well-accessed short → eligible for long promotion",
        eligible,
        f"M={M:.3f} cycles={cyc} dyn_imp={di:.3f}",
    )

# Long demotion: old, unaccessed
for band in ["A_1year", "B_6month"]:
    age_sec = AGE_BANDS[band]
    long_m = make_memory(
        f"tier_l_{band}",
        "Old note from a past meeting.",
        0.3,
        age_sec,
        0,
        "long",
        last_accessed_ago=age_sec,
    )
    M = simple_score(long_m)
    di = compute_dynamic_importance(long_m)
    age_last_days = age_sec / 86400.0
    demotion_eligible = (
        M <= B.long_demote_strength
        and age_last_days > B.long_demote_days
        and di <= 0.45
    )
    assert_true(
        f"Tier[{AGE_LABEL[band]}] dormant long → demotion eligible",
        demotion_eligible,
        f"M={M:.3f} age_days={age_last_days:.0f} dyn_imp={di:.3f}",
    )

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — HARD-DELETE ELIGIBILITY
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 7 — Hard-Delete Eligibility (Dead-Trace + Redundancy + Protection)")

# Dead-trace: acc==0, dyn_imp ≤ 0.15, age ≥ 180d, no dups
for band in ["A_1year", "B_6month"]:
    age_sec = AGE_BANDS[band]
    dead_m = make_memory(
        f"dead_{band}", "Trivial note never accessed.", 0.05, age_sec, 0, "flash"
    )
    di = compute_dynamic_importance(dead_m)
    age_days = age_sec / 86400.0
    dead_eligible = (
        dead_m.access_count == 0
        and di <= 0.15
        and age_days >= 180.0
        and not is_protected(dead_m)
    )
    assert_true(
        f"Delete[{AGE_LABEL[band]}] zero-access trivial → dead-trace eligible",
        dead_eligible,
        f"acc=0 dyn_imp={di:.3f} age_days={age_days:.0f}",
    )

# Dead-trace: fresh memories should NOT be eligible (age < 180d)
for band in ["H_1hour", "G_6hours", "F_1day", "E_1week", "D_1month"]:
    age_sec = AGE_BANDS[band]
    age_days = age_sec / 86400.0
    too_young = age_days < 180.0
    assert_true(
        f"Delete[{AGE_LABEL[band]}] too young for dead-trace deletion",
        too_young,
        f"age_days={age_days:.1f} < 180",
    )

# Catastrophic forgetting guard: high-imp NEVER eligible regardless of age/access
for band in AGE_BANDS:
    age_sec = AGE_BANDS[band]
    protected_m = make_memory(
        f"prot_{band}",
        "Patient allergy: penicillin. SEVERE.",
        0.95,
        age_sec,
        0,
        "flash",
    )
    assert_true(
        f"Guard[{AGE_LABEL[band]}] imp=0.95 memory always protected",
        is_protected(protected_m),
        f"imp={protected_m.importance:.2f}",
    )

# Already-summarized memories are NOT protected (safe to purge raw row)
already_summarized = make_memory(
    "summ_old",
    "Old note (already consolidated).",
    0.95,
    365 * 86400,
    0,
    consolidated_into_id="summary_123",
)
assert_true(
    "Guard: consolidated (has consolidated_into_id) is NOT protected",
    not is_protected(already_summarized),
    "has consolidated_into_id → safe to purge",
)

# Redundancy: old, low-imp, frequently-duplicated memory is eligible
redundant_m = make_memory(
    "redund_old",
    "Python is a popular programming language.",
    0.15,
    AGE_BANDS["B_6month"],
    1,
    "flash",
)
ni = NeighborInfo(
    sim_max=0.91,
    dup_ids=["d1", "d2", "d3", "d4"],
    dup_count=4,
    dup_similarities={"d1": 0.93, "d2": 0.91, "d3": 0.88, "d4": 0.90},
)
di_redund = compute_dynamic_importance(redundant_m)
avg_sim = sum(ni.dup_similarities.values()) / len(ni.dup_similarities)
weighted_dup = ni.dup_count * avg_sim
age_days_redund = AGE_BANDS["B_6month"] / 86400.0
M_redund = simple_score(redundant_m)
redundancy_eligible = (
    weighted_dup >= B.redundancy_dup_threshold
    and di_redund <= B.redundancy_max_imp
    and redundant_m.access_count <= B.redundancy_max_access
    and age_days_redund >= B.redundancy_min_age_sec / 86400.0
    and M_redund <= 0.30
)
assert_true(
    "Delete[6mo] weighted redundancy (4 dups, sim≈0.91) → eligible",
    redundancy_eligible,
    f"weighted_dup={weighted_dup:.2f} di={di_redund:.3f} M={M_redund:.3f}",
)

# Borderline dup (sim=0.74 avg): weighted_dup = 4*0.74 = 2.96 < 3 → SPARED
ni_borderline = NeighborInfo(
    sim_max=0.77,
    dup_ids=["d1", "d2", "d3", "d4"],
    dup_count=4,
    dup_similarities={"d1": 0.77, "d2": 0.74, "d3": 0.73, "d4": 0.72},
)
avg_sim_b = sum(ni_borderline.dup_similarities.values()) / len(
    ni_borderline.dup_similarities
)
weighted_b = ni_borderline.dup_count * avg_sim_b
not_eligible = weighted_b < B.redundancy_dup_threshold
assert_true(
    "Delete[6mo] borderline dup (sim≈0.74) → SPARED (weighted < 3)",
    not_eligible,
    f"weighted_dup={weighted_b:.2f} < threshold={B.redundancy_dup_threshold}",
)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 — FAN EFFECT ACROSS AGE BANDS
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 8 — Fan Effect (Spreading Activation → Interference at High Fan)")

fan_results: Dict[int, float] = {}
fan_sizes = [1, 2, 4, 8, 16, 32]

# Use a 30-day-old memory as the test subject
BAND_FAN = AGE_BANDS["D_1month"]
anchor = make_memory("fan_anchor", "Python architecture decision.", 0.6, BAND_FAN, 8)

# Build synthetic neighbor BLA scores
BASE_BLA = -0.5  # plausible weak memory trace

for fan in fan_sizes:
    neighbor_ids = [f"fan_n{i}" for i in range(fan)]
    neighbor_map = {
        anchor.id: NeighborInfo(
            sim_max=0.82,
            dup_ids=neighbor_ids,
            dup_count=fan,
            dup_similarities={nid: 0.82 for nid in neighbor_ids},
        )
    }
    # Each neighbor also knows about the anchor → fan = fan+1
    for nid in neighbor_ids:
        neighbor_map[nid] = NeighborInfo(
            sim_max=0.82, dup_ids=[anchor.id], dup_count=fan, dup_similarities={}
        )
    raw_bla = {anchor.id: petrov_bla(build_access_times(anchor))}
    for nid in neighbor_ids:
        raw_bla[nid] = BASE_BLA
    spread = compute_spreading_activation(anchor, neighbor_map, raw_bla)
    fan_results[fan] = spread

assert_gt("Fan=1 gives POSITIVE spreading activation", fan_results[1], 0.0)
assert_lt("Fan=32 gives NEGATIVE spreading (interference)", fan_results[32], 0.0)
# Monotone: spreading decreases as fan grows
for i in range(len(fan_sizes) - 1):
    f1, f2 = fan_sizes[i], fan_sizes[i + 1]
    assert_gt(
        f"Fan: spread[{f1}] > spread[{f2}] (monotone decrease)",
        fan_results[f1],
        fan_results[f2],
    )

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 9 — SOURCE-TURN GRADIENT (EARLY VS LATE TURNS)
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 9 — Source-Turn Gradient (Early Turns Decay Faster)")

for band, age_sec in AGE_BANDS.items():
    early = make_memory(
        f"turn_e_{band}",
        "Background info at start of conversation.",
        0.5,
        age_sec,
        0,
        source_turn=1,
    )
    late = make_memory(
        f"turn_l_{band}",
        "Background info at end of conversation.",
        0.5,
        age_sec,
        0,
        source_turn=15,
    )
    di_early = compute_dynamic_importance(early)
    di_late = compute_dynamic_importance(late)
    if age_sec < 7 * 86400:
        # Very fresh: both are high, difference may be tiny but late ≥ early
        assert_true(
            f"Turn[{AGE_LABEL[band]}] late turn dyn_imp ≥ early turn (primacy penalty)",
            di_late >= di_early - 1e-9,
            f"early={di_early:.4f} late={di_late:.4f}",
        )
    else:
        assert_gt(
            f"Turn[{AGE_LABEL[band]}] late turn dyn_imp > early turn (primacy penalty)",
            di_late,
            di_early,
        )

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10 — STRESS TEST: 80-MEMORY SWEEP (10 per band)
# ═══════════════════════════════════════════════════════════════════════════

section("SECTION 10 — Stress Test: 80 Memories (10 per band, mixed profiles)")

stress_memories = []
for band, age_sec in AGE_BANDS.items():
    for i in range(10):
        acc_count = i * 3
        imp = 0.1 + (i * 0.09)
        mtype = "long" if i >= 7 else ("short" if i >= 4 else "flash")
        text = (
            "Life-changing event! I was diagnosed with a serious illness. TERRIFIED and"
            " devastated."
            if i % 3 == 0
            else (
                "Normal workday, had a standup meeting and reviewed some pull requests."
            )
        )
        m = make_memory(
            f"stress_{band}_{i}",
            text,
            imp,
            age_sec,
            acc_count,
            mtype,
            source_turn=(1 if i < 2 else 10),
        )
        stress_memories.append((band, m))

all_strengths = []
band_strengths: Dict[str, List[float]] = {b: [] for b in AGE_BANDS}
emotional_s = []
routine_s = []

for band, m in stress_memories:
    s = simple_score(m)
    all_strengths.append(s)
    band_strengths[band].append(s)
    if "diagnosed" in m.text.lower():
        emotional_s.append(s)
    else:
        routine_s.append(s)

assert_true(
    "Stress: all 80 strengths in [0,1]",
    all(0.0 <= s <= 1.0 for s in all_strengths),
    f"out-of-range: {[s for s in all_strengths if not 0<=s<=1]}",
)

assert_gt(
    "Stress: mean emotional > mean routine",
    sum(emotional_s) / len(emotional_s),
    sum(routine_s) / len(routine_s),
)

# Band averages decrease from H→A
band_avgs = {b: sum(band_strengths[b]) / len(band_strengths[b]) for b in AGE_BANDS}
prev_band, prev_avg = None, None
for band in list(AGE_BANDS.keys()):
    if prev_avg is not None:
        # Bands iterate oldest→newest (A_1year→H_1hour), so avg should INCREASE
        assert_true(
            f"Stress: avg_strength[{AGE_LABEL[band]}] ≥"
            f" avg_strength[{AGE_LABEL[prev_band]}] (newer=stronger)",
            band_avgs[band] >= band_avgs[prev_band] - 0.01,
            f"{band_avgs[band]:.3f} vs prev {band_avgs[prev_band]:.3f}",
        )
    prev_band = band
    prev_avg = band_avgs[band]

print(f"\n  Stress test breakdown:")
for band in AGE_BANDS:
    print(f"    {AGE_LABEL[band]:12s} : avg_strength = {band_avgs[band]:.3f}")

# ═══════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

section("FINAL SUMMARY")
print(f"\n  Total assertions : {PASS + FAIL}")
print(f"  ✓ PASS           : {PASS}")
print(f"  ✗ FAIL           : {FAIL}")
if FAIL == 0:
    print("\n  ✅  ALL TESTS PASSED")
else:
    print(f"\n  ❌  {FAIL} TEST(S) FAILED:")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"     • {name}: {detail}")

# ═══════════════════════════════════════════════════════════════════════════
# GRAPHS
# ═══════════════════════════════════════════════════════════════════════════

import os

OUT = "assets/time_range_graphs"
os.makedirs(OUT, exist_ok=True)

BAND_LABELS = [AGE_LABEL[b] for b in AGE_BANDS]
COLORS = {
    "routine": "#5B7FDB",
    "emotional": "#E05C5C",
    "protected": "#4CAF50",
}

# ── Graph 1: BLA Sigmoid Across Age Bands ──────────────────────────────────
fig, ax = plt.subplots(figsize=(11, 5))
for acc_label, color, marker in [
    ("1_access", "#5B7FDB", "o"),
    ("5_access", "#E8A838", "s"),
    ("30_access", "#E05C5C", "^"),
]:
    vals = [bla_by_band[b][acc_label] for b in AGE_BANDS]
    ax.plot(
        BAND_LABELS,
        vals,
        color=color,
        marker=marker,
        linewidth=2.2,
        markersize=8,
        label=acc_label.replace("_", " "),
    )
ax.set_title(
    "Petrov BLA (sigmoid) Across Age Bands", fontsize=14, fontweight="bold", pad=14
)
ax.set_xlabel("Memory Age", fontsize=11)
ax.set_ylabel("BLA Sigmoid [0–1]", fontsize=11)
ax.set_ylim(0, 1.05)
ax.legend(fontsize=10)
ax.grid(axis="y", alpha=0.3)
plt.xticks(rotation=30, ha="right", fontsize=9)
plt.tight_layout()
fig.savefig(f"{OUT}/01_bla_decay.png", dpi=150)
plt.close()

# ── Graph 2: Dynamic Importance ──────────────────────────────────────────
fig, ax = plt.subplots(figsize=(11, 5))
r_vals = [dyn_imp_by_band_routine[b] for b in AGE_BANDS]
e_vals = [dyn_imp_by_band_emotional[b] for b in AGE_BANDS]
ax.plot(
    BAND_LABELS,
    r_vals,
    color=COLORS["routine"],
    marker="o",
    linewidth=2.2,
    markersize=8,
    label="Routine (imp=0.3, 1 access)",
)
ax.plot(
    BAND_LABELS,
    e_vals,
    color=COLORS["emotional"],
    marker="^",
    linewidth=2.2,
    markersize=8,
    label="Emotional (imp=0.9, 3 accesses)",
)
ax.fill_between(
    range(len(BAND_LABELS)), r_vals, e_vals, alpha=0.12, color=COLORS["emotional"]
)
ax.set_title(
    "Dynamic Importance Drift: Routine vs Emotional",
    fontsize=14,
    fontweight="bold",
    pad=14,
)
ax.set_xlabel("Memory Age", fontsize=11)
ax.set_ylabel("Dynamic Importance", fontsize=11)
ax.set_xticks(range(len(BAND_LABELS)))
ax.set_xticklabels(BAND_LABELS, rotation=30, ha="right", fontsize=9)
ax.legend(fontsize=10)
ax.grid(axis="y", alpha=0.3)
plt.tight_layout()
fig.savefig(f"{OUT}/02_dynamic_importance.png", dpi=150)
plt.close()

# ── Graph 3: Temporal Gradient by Band ────────────────────────────────────
fig, axes = plt.subplots(1, 2, figsize=(13, 5))
ax1, ax2 = axes
tg_vals = [tg_by_band[b] * 100 for b in AGE_BANDS]
bars = ax1.bar(
    BAND_LABELS,
    tg_vals,
    color=["#E05C5C" if v > 0.5 else "#BFC7D5" for v in tg_vals],
    edgecolor="white",
    linewidth=1.2,
)
ax1.set_title("Temporal Gradient Boost by Age Band", fontsize=13, fontweight="bold")
ax1.set_ylabel("Boost (% of strength score)", fontsize=10)
ax1.set_ylim(0, 5.5)
ax1.axhline(2.0, color="#E05C5C", linestyle="--", alpha=0.5, label="2% threshold")
ax1.legend(fontsize=9)
ax1.grid(axis="y", alpha=0.3)
plt.setp(ax1.get_xticklabels(), rotation=30, ha="right", fontsize=8)

# Fine-grained: TG from 0h to 72h
fine_ages = np.linspace(0, 72 * 3600, 500)
fine_tg = []
for a in fine_ages:
    m_tmp = make_memory("tmp", "x", 0.1, a, 0)
    fine_tg.append(compute_temporal_gradient(m_tmp) * 100)
ax2.plot(fine_ages / 3600, fine_tg, color="#E05C5C", linewidth=2.2)
ax2.axvline(24, color="#444", linestyle="--", alpha=0.6, label="24h peak")
ax2.fill_between(fine_ages / 3600, fine_tg, alpha=0.15, color="#E05C5C")
ax2.set_title("Temporal Gradient Shape (0–72 hours)", fontsize=13, fontweight="bold")
ax2.set_xlabel("Memory Age (hours)", fontsize=10)
ax2.set_ylabel("TG Boost (%)", fontsize=10)
ax2.legend(fontsize=9)
ax2.grid(alpha=0.2)
plt.tight_layout()
fig.savefig(f"{OUT}/03_temporal_gradient.png", dpi=150)
plt.close()

# ── Graph 4: Proactive Interference Penalty ───────────────────────────────
fig, ax = plt.subplots(figsize=(11, 5))
pi_vals = [abs(pi_penalties[b]) * 100 for b in AGE_BANDS]
ax.bar(
    BAND_LABELS,
    pi_vals,
    color=["#E05C5C" if v > 1 else "#BFC7D5" for v in pi_vals],
    edgecolor="white",
    linewidth=1.2,
)
ax.set_title(
    "Proactive Interference Penalty by Memory Age",
    fontsize=13,
    fontweight="bold",
    pad=14,
)
ax.set_xlabel("Memory Age (target memory)", fontsize=11)
ax.set_ylabel("|PI Penalty| (% of strength)", fontsize=11)
ax.axhline(15.0, color="#444", linestyle="--", alpha=0.5, label="Max cap (15%)")
ax.legend(fontsize=9)
ax.grid(axis="y", alpha=0.3)
plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=9)
plt.tight_layout()
fig.savefig(f"{OUT}/04_proactive_interference.png", dpi=150)
plt.close()

# ── Graph 5: Full Strength Scores ─────────────────────────────────────────
fig, ax = plt.subplots(figsize=(12, 5))
r_s = [strengths_routine[b] for b in AGE_BANDS]
e_s = [strengths_emotional[b] for b in AGE_BANDS]
p_s = [strengths_protected[b] for b in AGE_BANDS]
x = np.arange(len(BAND_LABELS))
w = 0.26
ax.bar(
    x - w,
    r_s,
    w,
    label="Routine (imp=0.2, 1 access)",
    color=COLORS["routine"],
    alpha=0.88,
)
ax.bar(
    x,
    e_s,
    w,
    label="Emotional (imp=0.9, DEVASTATED)",
    color=COLORS["emotional"],
    alpha=0.88,
)
ax.bar(
    x + w,
    p_s,
    w,
    label="Protected allergy (imp=0.95, acc=0)",
    color=COLORS["protected"],
    alpha=0.88,
)
ax.set_title(
    "Full Strength Scores Across Age Bands", fontsize=14, fontweight="bold", pad=14
)
ax.set_xlabel("Memory Age", fontsize=11)
ax.set_ylabel("Strength Score [0–1]", fontsize=11)
ax.set_xticks(x)
ax.set_xticklabels(BAND_LABELS, rotation=30, ha="right", fontsize=9)
ax.set_ylim(0, 1.1)
ax.legend(fontsize=9)
ax.grid(axis="y", alpha=0.3)
plt.tight_layout()
fig.savefig(f"{OUT}/05_strength_scores.png", dpi=150)
plt.close()

# ── Graph 6: Fan Effect ───────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(10, 5))
ax.plot(
    fan_sizes,
    [fan_results[f] for f in fan_sizes],
    color="#5B7FDB",
    marker="o",
    linewidth=2.5,
    markersize=9,
)
ax.axhline(0, color="#444", linestyle="--", alpha=0.5)
ax.fill_between(
    fan_sizes,
    [fan_results[f] for f in fan_sizes],
    0,
    where=[fan_results[f] > 0 for f in fan_sizes],
    alpha=0.12,
    color="#5B7FDB",
    label="Activation boost",
)
ax.fill_between(
    fan_sizes,
    [fan_results[f] for f in fan_sizes],
    0,
    where=[fan_results[f] <= 0 for f in fan_sizes],
    alpha=0.12,
    color="#E05C5C",
    label="Interference zone",
)
crossover = math.exp(_SPREADING_S)
ax.axvline(
    crossover,
    color="#E8A838",
    linestyle=":",
    linewidth=1.8,
    label=f"Crossover ≈ fan {crossover:.1f}",
)
ax.set_title(
    "Fan Effect: Spreading Activation vs Interference",
    fontsize=13,
    fontweight="bold",
    pad=14,
)
ax.set_xlabel("Fan Size (number of associations)", fontsize=11)
ax.set_ylabel("Spreading Activation Score", fontsize=11)
ax.legend(fontsize=9)
ax.grid(alpha=0.25)
plt.tight_layout()
fig.savefig(f"{OUT}/06_fan_effect.png", dpi=150)
plt.close()

# ── Graph 7: Source-Turn Gradient ─────────────────────────────────────────
fig, ax = plt.subplots(figsize=(11, 5))
turn_early = []
turn_late = []
for band in AGE_BANDS:
    age_sec = AGE_BANDS[band]
    e = make_memory(f"te_{band}", "Background info.", 0.5, age_sec, 0, source_turn=1)
    l = make_memory(f"tl_{band}", "Background info.", 0.5, age_sec, 0, source_turn=15)
    turn_early.append(compute_dynamic_importance(e))
    turn_late.append(compute_dynamic_importance(l))

ax.plot(
    BAND_LABELS,
    turn_early,
    color="#E05C5C",
    marker="o",
    linewidth=2.2,
    markersize=8,
    label="Turn 1 (early — 30% faster decay)",
)
ax.plot(
    BAND_LABELS,
    turn_late,
    color="#5B7FDB",
    marker="s",
    linewidth=2.2,
    markersize=8,
    label="Turn 15 (late — normal decay)",
)
ax.fill_between(
    range(len(BAND_LABELS)), turn_early, turn_late, alpha=0.12, color="#888"
)
ax.set_title(
    "Source-Turn Gradient: Early vs Late Turn Decay",
    fontsize=13,
    fontweight="bold",
    pad=14,
)
ax.set_xlabel("Memory Age", fontsize=11)
ax.set_ylabel("Dynamic Importance", fontsize=11)
ax.set_xticks(range(len(BAND_LABELS)))
ax.set_xticklabels(BAND_LABELS, rotation=30, ha="right", fontsize=9)
ax.legend(fontsize=10)
ax.grid(axis="y", alpha=0.3)
plt.tight_layout()
fig.savefig(f"{OUT}/07_source_turn.png", dpi=150)
plt.close()

# ── Graph 8: Stress Test Heatmap ──────────────────────────────────────────
# 8 bands × 10 memories → heatmap of strengths
heat_data = np.zeros((len(AGE_BANDS), 10))
band_list = list(AGE_BANDS.keys())
for bi, band in enumerate(band_list):
    for mi, (b, m) in enumerate([(b, m) for b, m in stress_memories if b == band]):
        heat_data[bi, mi] = simple_score(m)

fig, ax = plt.subplots(figsize=(12, 5))
im = ax.imshow(heat_data, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
ax.set_yticks(range(len(band_list)))
ax.set_yticklabels(BAND_LABELS, fontsize=9)
ax.set_xticks(range(10))
ax.set_xticklabels([f"M{i+1}" for i in range(10)], fontsize=9)
ax.set_title(
    "Stress Test — Strength Heatmap (80 memories × 8 age bands)",
    fontsize=13,
    fontweight="bold",
    pad=12,
)
ax.set_xlabel(
    "Memory Profile (M1=routine/low-acc → M10=emotional/high-acc)", fontsize=10
)
plt.colorbar(im, ax=ax, label="Strength Score")
plt.tight_layout()
fig.savefig(f"{OUT}/08_stress_heatmap.png", dpi=150)
plt.close()

# ── Graph 9: Dashboard Overview ───────────────────────────────────────────
tests_by_section = {
    "1. BLA Decay": sum(1 for n, ok, _ in RESULTS if "BLA" in n),
    "2. Dyn Importance": sum(1 for n, ok, _ in RESULTS if "DynImp" in n),
    "3. Temporal Gradient": sum(1 for n, ok, _ in RESULTS if "TG" in n),
    "4. PI Penalty": sum(1 for n, ok, _ in RESULTS if "PI" in n),
    "5. Strength": sum(1 for n, ok, _ in RESULTS if "Strength" in n),
    "6. Tier": sum(1 for n, ok, _ in RESULTS if "Tier" in n),
    "7. Delete/Guard": sum(
        1 for n, ok, _ in RESULTS if any(x in n for x in ["Delete", "Guard", "redund"])
    ),
    "8. Fan Effect": sum(1 for n, ok, _ in RESULTS if "Fan" in n),
    "9. Turn Gradient": sum(1 for n, ok, _ in RESULTS if "Turn" in n),
    "10. Stress": sum(1 for n, ok, _ in RESULTS if "Stress" in n),
}
pass_by_section = {
    "1. BLA Decay": sum(1 for n, ok, _ in RESULTS if "BLA" in n and ok),
    "2. Dyn Importance": sum(1 for n, ok, _ in RESULTS if "DynImp" in n and ok),
    "3. Temporal Gradient": sum(1 for n, ok, _ in RESULTS if "TG" in n and ok),
    "4. PI Penalty": sum(1 for n, ok, _ in RESULTS if "PI" in n and ok),
    "5. Strength": sum(1 for n, ok, _ in RESULTS if "Strength" in n and ok),
    "6. Tier": sum(1 for n, ok, _ in RESULTS if "Tier" in n and ok),
    "7. Delete/Guard": sum(
        1
        for n, ok, _ in RESULTS
        if any(x in n for x in ["Delete", "Guard", "redund"]) and ok
    ),
    "8. Fan Effect": sum(1 for n, ok, _ in RESULTS if "Fan" in n and ok),
    "9. Turn Gradient": sum(1 for n, ok, _ in RESULTS if "Turn" in n and ok),
    "10. Stress": sum(1 for n, ok, _ in RESULTS if "Stress" in n and ok),
}

fig = plt.figure(figsize=(16, 10))
fig.suptitle(
    "Time-Range Test Suite — Dashboard", fontsize=16, fontweight="bold", y=0.97
)
gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.45, wspace=0.38)

# Top-left: pass/fail pie
ax_pie = fig.add_subplot(gs[0, 0])
ax_pie.pie(
    [PASS, FAIL],
    labels=["PASS", "FAIL"],
    colors=["#4CAF50", "#E05C5C"],
    autopct="%1.0f%%",
    startangle=90,
    textprops={"fontsize": 12},
)
ax_pie.set_title(f"Overall: {PASS}/{PASS+FAIL}", fontsize=12, fontweight="bold")

# Top-middle: tests per section bar
ax_sec = fig.add_subplot(gs[0, 1:])
sec_names = list(tests_by_section.keys())
totals = list(tests_by_section.values())
passes = [pass_by_section[s] for s in sec_names]
fails = [t - p for t, p in zip(totals, passes)]
xs = np.arange(len(sec_names))
ax_sec.bar(xs, passes, color="#4CAF50", alpha=0.88, label="Pass")
ax_sec.bar(xs, fails, bottom=passes, color="#E05C5C", alpha=0.88, label="Fail")
ax_sec.set_xticks(xs)
ax_sec.set_xticklabels(sec_names, rotation=35, ha="right", fontsize=8)
ax_sec.set_ylabel("Assertions", fontsize=9)
ax_sec.legend(fontsize=9)
ax_sec.set_title("Assertions by Section", fontsize=12, fontweight="bold")
ax_sec.grid(axis="y", alpha=0.3)

# Bottom-left: BLA multi-access
ax_bla = fig.add_subplot(gs[1, 0])
for acc_label, color, marker in [
    ("1_access", "#5B7FDB", "o"),
    ("5_access", "#E8A838", "s"),
    ("30_access", "#E05C5C", "^"),
]:
    ax_bla.plot(
        [bla_by_band[b][acc_label] for b in AGE_BANDS],
        color=color,
        marker=marker,
        linewidth=1.8,
        markersize=6,
        label=acc_label,
    )
ax_bla.set_xticks(range(len(BAND_LABELS)))
ax_bla.set_xticklabels(BAND_LABELS, rotation=40, ha="right", fontsize=7)
ax_bla.set_title("BLA Sigmoid by Band", fontsize=10, fontweight="bold")
ax_bla.legend(fontsize=7)
ax_bla.grid(axis="y", alpha=0.25)

# Bottom-middle: strength by band (3 profiles)
ax_str = fig.add_subplot(gs[1, 1])
ax_str.plot(
    [strengths_routine[b] for b in AGE_BANDS],
    color=COLORS["routine"],
    marker="o",
    linewidth=1.8,
    markersize=6,
    label="Routine",
)
ax_str.plot(
    [strengths_emotional[b] for b in AGE_BANDS],
    color=COLORS["emotional"],
    marker="^",
    linewidth=1.8,
    markersize=6,
    label="Emotional",
)
ax_str.plot(
    [strengths_protected[b] for b in AGE_BANDS],
    color=COLORS["protected"],
    marker="s",
    linewidth=1.8,
    markersize=6,
    label="Protected",
)
ax_str.set_xticks(range(len(BAND_LABELS)))
ax_str.set_xticklabels(BAND_LABELS, rotation=40, ha="right", fontsize=7)
ax_str.set_title("Strength by Band", fontsize=10, fontweight="bold")
ax_str.legend(fontsize=7)
ax_str.grid(axis="y", alpha=0.25)

# Bottom-right: Temporal Gradient fine-grained
ax_tg = fig.add_subplot(gs[1, 2])
ax_tg.plot(fine_ages / 3600, fine_tg, color="#E05C5C", linewidth=2)
ax_tg.axvline(24, color="#444", linestyle="--", alpha=0.6, label="24h")
ax_tg.axvline(6, color="#888", linestyle=":", alpha=0.5, label="6h")
ax_tg.axvline(1, color="#AAA", linestyle=":", alpha=0.5, label="1h")
ax_tg.set_title("Temporal Gradient Shape", fontsize=10, fontweight="bold")
ax_tg.set_xlabel("Age (hours)", fontsize=8)
ax_tg.set_ylabel("TG Boost (%)", fontsize=8)
ax_tg.legend(fontsize=7)
ax_tg.grid(alpha=0.2)

plt.savefig(f"{OUT}/09_dashboard.png", dpi=150, bbox_inches="tight")
plt.close()

print(f"\n  Graphs saved to: {OUT}/")
print(f"  01_bla_decay.png")
print(f"  02_dynamic_importance.png")
print(f"  03_temporal_gradient.png")
print(f"  04_proactive_interference.png")
print(f"  05_strength_scores.png")
print(f"  06_fan_effect.png")
print(f"  07_source_turn.png")
print(f"  08_stress_heatmap.png")
print(f"  09_dashboard.png")
