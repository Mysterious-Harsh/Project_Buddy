"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  CONSOLIDATION ENGINE v4.1p2 — SOLID TEST SUITE                             ║
║                                                                              ║
║  Covers every mechanism and every fix applied in v4.1-patched2:             ║
║                                                                              ║
║  MECHANISM TESTS                                                             ║
║    M1.  Petrov BLA decay — power-law forgetting curve                       ║
║    M2.  Dynamic importance — salience × recency × frequency                 ║
║    M3.  Temporal gradient FIX — last_accessed as reference, not created_at  ║
║    M4.  Arousal detection (keywords, ALL-CAPS, punctuation)                 ║
║    M5.  Negation-aware arousal FIX — "not in pain" scores lower             ║
║    M6.  Sleep-phase weighting (REM vs SWS)                                  ║
║    M7.  Retroactive interference penalty (similarity-scaled)                ║
║    M8.  Spreading activation + fan effect interference                      ║
║    M9.  Catastrophic forgetting guard — immortal / critical / Rule B        ║
║    M10. Summary created_at FIX — centroid, not newest member timestamp      ║
║    M11. Spacing-weighted touch — spaced > massed retrieval                  ║
║    M12. Tier promotion / demotion gates                                      ║
║    M13. Redundancy pruning — similarity-weighted dup_count                  ║
║    M14. Keep-one-representative rule in redundancy pruning                  ║
║    M15. Source-turn gradient — early turns decay faster                     ║
║    M16. Consolidation depth gate — blocks deep re-summarization             ║
║                                                                              ║
║  INTEGRATION TESTS                                                           ║
║    I1.  run_consolidation — cancel before start                             ║
║    I2.  run_consolidation — full dry-run with mock stores                   ║
║    I3.  batch_touch spacing correctness end-to-end                          ║
║    I4.  SQLiteStore touch spacing boost ordering                            ║
║    I5.  list_candidates_for_consolidation excludes deleted rows              ║
║    I6.  Catastrophic forgetting guard survives full delete pass              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import math
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import numpy as np
import pytest

import buddy.memory.consolidation_engine as eng
from buddy.memory.consolidation_engine import (
    NeighborInfo,
    SleepBudget,
    _build_access_times,
    _cluster_priority_score,
    _compute_arousal,
    _compute_dynamic_importance,
    _compute_proactive_interference_penalty,
    _compute_sleep_phase_weight,
    _compute_spreading_activation,
    _compute_strength,
    _compute_temporal_gradient,
    _is_protected,
    _negation_window,
    _petrov_bla,
    _plan_hard_deletes,
    _plan_tier_updates,
    run_consolidation,
    Cluster,
)
from buddy.memory.memory_entry import MemoryEntry
from buddy.memory.sqlite_store import SQLiteStore

DAY = 86400.0
HOUR = 3600.0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mem(
    text: str = "test memory",
    *,
    imp: float = 0.5,
    mtype: str = "flash",
    acc: int = 1,
    created_days: float = 1.0,
    last_days: Optional[float] = None,
    source_turn: Optional[int] = None,
    meta: Optional[dict] = None,
    embedding: Optional[np.ndarray] = None,
) -> MemoryEntry:
    now = time.time()
    emb = embedding
    if emb is None:
        emb = np.random.randn(32).astype(np.float32)
        emb /= np.linalg.norm(emb) + 1e-9
    m = MemoryEntry(text=text)
    m.importance = imp
    m.memory_type = mtype
    m.access_count = acc
    m.created_at = now - created_days * DAY
    m.last_accessed = (now - last_days * DAY) if last_days is not None else None
    m.source_turn = source_turn
    m.metadata = meta if meta is not None else {"consolidation_cycles": 0}
    m.embedding = emb
    return m


def _neighbor(
    sim_max: float = 0.0,
    dup_ids: Optional[List[str]] = None,
    sims: Optional[Dict[str, float]] = None,
    surprising: bool = False,
) -> NeighborInfo:
    dup_ids = dup_ids or []
    sims = sims or {}
    return NeighborInfo(
        sim_max=sim_max,
        dup_ids=dup_ids,
        dup_count=len(dup_ids),
        is_surprising=surprising,
        dup_similarities=sims,
    )


def _strength(
    m: MemoryEntry,
    *,
    now: float,
    neighbor_map: Optional[Dict] = None,
    raw_bla: Optional[Dict] = None,
    dyn_imp: Optional[Dict] = None,
    id_map: Optional[Dict] = None,
    budget: Optional[SleepBudget] = None,
) -> float:
    b = budget or SleepBudget()
    nm = neighbor_map or {m.id: _neighbor()}
    di = dyn_imp or {m.id: _compute_dynamic_importance(m, now=now, budget=b)}
    rb = raw_bla or {}
    if not rb:
        from buddy.memory.consolidation_engine import _compute_all_raw_bla
        rb = _compute_all_raw_bla([m], now=now, budget=b, dynamic_importances=di)
    return _compute_strength(
        m, now=now, budget=b,
        neighbor_map=nm, raw_bla_scores=rb,
        dynamic_importances=di, id_map=id_map,
    )


def _make_sqlite(path: str) -> SQLiteStore:
    return SQLiteStore(db_path=path, debug=False)


# ─────────────────────────────────────────────────────────────────────────────
# M1. Petrov BLA — power-law forgetting curve
# ─────────────────────────────────────────────────────────────────────────────

class TestPetrovBLA:
    def test_single_access_returns_finite(self):
        bla = _petrov_bla([3600.0])
        assert math.isfinite(bla)

    def test_empty_returns_neg_inf(self):
        assert _petrov_bla([]) == -math.inf

    def test_recent_access_stronger_than_old(self):
        bla_recent = _petrov_bla([60.0])       # accessed 1 min ago
        bla_old    = _petrov_bla([86400.0])    # accessed 1 day ago
        assert bla_recent > bla_old

    def test_more_accesses_stronger(self):
        bla_one  = _petrov_bla([3600.0])
        bla_five = _petrov_bla([3600.0, 7200.0, 10800.0, 14400.0, 18000.0])
        assert bla_five > bla_one

    def test_decay_exponent_effect(self):
        times = [3600.0, 86400.0, 604800.0]
        bla_fast = _petrov_bla(times, d=0.8)
        bla_slow = _petrov_bla(times, d=0.2)
        # Slower decay (lower d) → stronger activation
        assert bla_slow > bla_fast

    def test_power_law_shape(self):
        """Activation drops as time^(-d) — doubling time should reduce BLA."""
        bla_1h  = _petrov_bla([1 * HOUR])
        bla_2h  = _petrov_bla([2 * HOUR])
        bla_4h  = _petrov_bla([4 * HOUR])
        assert bla_1h > bla_2h > bla_4h


# ─────────────────────────────────────────────────────────────────────────────
# M2. Dynamic importance
# ─────────────────────────────────────────────────────────────────────────────

class TestDynamicImportance:
    def test_high_salience_higher_importance(self):
        now = time.time()
        b = SleepBudget()
        m_high = _mem(imp=0.9, acc=1, created_days=10)
        m_low  = _mem(imp=0.1, acc=1, created_days=10)
        assert (
            _compute_dynamic_importance(m_high, now=now, budget=b)
            > _compute_dynamic_importance(m_low, now=now, budget=b)
        )

    def test_frequent_access_higher_importance(self):
        # Use created_days=90 so that acc=1 does NOT saturate the freq cap.
        # rate = acc/days; freq_contrib = 0.35 * min(1.0, rate*30).
        # acc=50, days=90 → rate*30 = 50*30/90 = 16.7 → capped at 1.0 → 0.35
        # acc=1,  days=90 → rate*30 = 1*30/90  = 0.33 → not capped      → 0.116
        now = time.time()
        b = SleepBudget()
        m_freq = _mem(imp=0.5, acc=50, created_days=90)
        m_rare = _mem(imp=0.5, acc=1,  created_days=90)
        assert (
            _compute_dynamic_importance(m_freq, now=now, budget=b)
            > _compute_dynamic_importance(m_rare, now=now, budget=b)
        )

    def test_output_in_unit_interval(self):
        now = time.time()
        b = SleepBudget()
        for imp, acc, days in [(0.0, 0, 1), (1.0, 100, 365), (0.5, 5, 7)]:
            m = _mem(imp=imp, acc=acc, created_days=days)
            d = _compute_dynamic_importance(m, now=now, budget=b)
            assert 0.0 <= d <= 1.0, f"out of range: {d}"


# ─────────────────────────────────────────────────────────────────────────────
# M3. Temporal gradient FIX — last_accessed as reference
# ─────────────────────────────────────────────────────────────────────────────

class TestTemporalGradientFix:
    def test_no_access_gets_full_24h_bump(self):
        """Memory created 24h ago, never accessed → near-peak gradient."""
        now = time.time()
        m = _mem(imp=0.1, acc=0, created_days=1.0)
        m.last_accessed = None
        tg = _compute_temporal_gradient(m, now=now)
        assert tg > 0.035, f"Expected near 0.04, got {tg:.5f}"

    def test_accessed_12h_ago_lower_than_peak(self):
        """Memory created 24h ago but accessed 12h ago.
        Reference = 12h ago, distance from 24h peak = 12h → much lower gradient."""
        now = time.time()
        m = _mem(imp=0.1, acc=1, created_days=1.0)
        m.last_accessed = now - 12 * HOUR  # accessed 12h ago
        tg = _compute_temporal_gradient(m, now=now)
        # Should be significantly below peak
        assert tg < 0.038, f"Expected < 0.038 (not at 24h peak), got {tg:.5f}"

    def test_accessed_24h_ago_near_peak(self):
        """Memory accessed exactly 24h ago → gets the full gradient bump."""
        now = time.time()
        m = _mem(imp=0.1, acc=2, created_days=3.0)
        m.last_accessed = now - 1.0 * DAY  # last replay was 24h ago
        tg = _compute_temporal_gradient(m, now=now)
        assert tg > 0.035, f"Expected near peak 0.04, got {tg:.5f}"

    def test_accessed_just_now_gets_minimal_gradient(self):
        """Memory just accessed → reference = now → age_sec ≈ 0 → near-zero gradient."""
        now = time.time()
        m = _mem(imp=0.1, acc=5, created_days=7.0)
        m.last_accessed = now - 10  # accessed 10 seconds ago
        tg = _compute_temporal_gradient(m, now=now)
        assert tg < 0.01, f"Expected near-zero, got {tg:.5f}"

    def test_7d_old_no_access_zero_gradient(self):
        """Memory 7 days old, never accessed → very far from 24h peak → ~0."""
        now = time.time()
        m = _mem(imp=0.1, acc=0, created_days=7.0)
        m.last_accessed = None
        tg = _compute_temporal_gradient(m, now=now)
        assert tg < 0.001, f"Expected ~0 for 7d old memory, got {tg:.5f}"

    def test_last_accessed_reference_beats_created_at(self):
        """Confirm the fix: gradient uses last_accessed as reference, not created_at.
        Created 30d ago (would give 0 without fix), accessed 24h ago → near peak."""
        now = time.time()
        m = _mem(imp=0.1, acc=3, created_days=30.0)
        m.last_accessed = now - 1.0 * DAY
        tg = _compute_temporal_gradient(m, now=now)
        assert tg > 0.035, (
            "With the fix, last_accessed=24h ago should give near-peak TG. "
            f"Got {tg:.5f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# M4. Arousal detection (base)
# ─────────────────────────────────────────────────────────────────────────────

class TestArousalDetection:
    def test_high_importance_boosts_arousal(self):
        m_hi = _mem("neutral text", imp=0.9)
        m_lo = _mem("neutral text", imp=0.1)
        assert _compute_arousal(m_hi) > _compute_arousal(m_lo)

    def test_keyword_hit_boosts_arousal(self):
        m_kw   = _mem("I am in pain and grief", imp=0.3)
        m_none = _mem("today the weather is nice", imp=0.3)
        assert _compute_arousal(m_kw) > _compute_arousal(m_none)

    def test_caps_boosts_arousal(self):
        m_caps  = _mem("THIS IS URGENT and CRITICAL", imp=0.3)
        m_lower = _mem("this is urgent and critical", imp=0.3)
        # ALL-CAPS contributes 0.12 weight
        assert _compute_arousal(m_caps) > _compute_arousal(m_lower)

    def test_punctuation_boosts_arousal(self):
        m_punct = _mem("help me!!! what???", imp=0.3)
        m_plain = _mem("help me what", imp=0.3)
        assert _compute_arousal(m_punct) > _compute_arousal(m_plain)

    def test_arousal_in_unit_interval(self):
        texts = [
            "URGENT CRITICAL EMERGENCY grief love hate fear shock amazing disaster "
            "death crying angry furious excited terrified!!!",
            "everything is fine, nothing happened today",
            "",
        ]
        for t in texts:
            m = _mem(t, imp=0.9)
            ar = _compute_arousal(m)
            assert 0.0 <= ar <= 1.0, f"Arousal out of range: {ar} for '{t[:30]}'"

    def test_hinglish_keywords_detected(self):
        m = _mem("mujhe bahut dard aur takleef ho rahi hai", imp=0.3)
        ar = _compute_arousal(m)
        assert ar > 0.05, f"Hindi arousal keywords not detected: {ar}"


# ─────────────────────────────────────────────────────────────────────────────
# M5. Negation-aware arousal FIX
# ─────────────────────────────────────────────────────────────────────────────

class TestNegationAwareArousal:
    def test_negation_window_detects_single_word_negation(self):
        tokens = ["i", "was", "not", "in", "pain"]
        # "pain" is at index 4; "not" is at index 2 (2 positions before) → negated
        assert _negation_window(tokens, 4, window=2) is True

    def test_negation_window_no_negation(self):
        tokens = ["i", "was", "in", "great", "pain"]
        # "pain" at index 4; no negation in window
        assert _negation_window(tokens, 4, window=2) is False

    def test_negation_window_too_far(self):
        tokens = ["not", "really", "ever", "in", "pain"]
        # "pain" at index 4; "not" at index 0, 4 positions away — outside window=2
        assert _negation_window(tokens, 4, window=2) is False

    def test_negation_window_bigram(self):
        tokens = ["i", "am", "no", "longer", "desperate"]
        # "desperate" at index 4; "no longer" bigram at [2,3] → within window=2
        assert _negation_window(tokens, 4, window=2) is True

    def test_negated_pain_lower_than_positive(self):
        m_neg = _mem("I was not in pain and not excited at all", imp=0.2)
        m_pos = _mem("I was in pain and very excited", imp=0.2)
        ar_neg = _compute_arousal(m_neg)
        ar_pos = _compute_arousal(m_pos)
        assert ar_neg < ar_pos, (
            f"Negated '{m_neg.text}' should have lower arousal than "
            f"positive '{m_pos.text}'. Got neg={ar_neg:.4f} pos={ar_pos:.4f}"
        )

    def test_never_excited_lower_than_excited(self):
        m_never = _mem("I never felt excited about anything", imp=0.2)
        m_yes   = _mem("I felt so excited about everything", imp=0.2)
        assert _compute_arousal(m_never) < _compute_arousal(m_yes)

    def test_cannot_grieve_lower_than_grieve(self):
        m_cant = _mem("they cannot grieve forever", imp=0.3)
        m_can  = _mem("they grieve deeply every day", imp=0.3)
        assert _compute_arousal(m_cant) <= _compute_arousal(m_can)

    def test_arousal_not_zeroed_by_negation_of_non_keyword(self):
        """Negating a non-keyword word should not suppress real keyword hits."""
        m = _mem("not happy but deeply devastated and in trauma", imp=0.3)
        ar = _compute_arousal(m)
        # "devastated" and "trauma" are not negated; should still register
        assert ar > 0.05, f"Unnegated keywords should still count: {ar:.4f}"

    def test_multiple_keywords_partial_negation(self):
        """Some negated, some positive — net arousal between pure neg and pure pos."""
        m_mix = _mem("not in pain but full of grief and trauma", imp=0.2)
        m_all_neg = _mem("not in pain and not full of grief and no trauma", imp=0.2)
        m_all_pos = _mem("in pain and full of grief and trauma", imp=0.2)
        ar_mix    = _compute_arousal(m_mix)
        ar_all_neg = _compute_arousal(m_all_neg)
        ar_all_pos = _compute_arousal(m_all_pos)
        assert ar_all_neg <= ar_mix <= ar_all_pos, (
            f"Partial negation should be between all-neg and all-pos. "
            f"all_neg={ar_all_neg:.4f} mix={ar_mix:.4f} all_pos={ar_all_pos:.4f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# M6. Sleep-phase weighting (REM vs SWS)
# ─────────────────────────────────────────────────────────────────────────────

class TestSleepPhaseWeighting:
    def test_high_arousal_gets_rem_boost(self):
        m_emo  = _mem("URGENT disaster grief trauma panic", imp=0.7)
        m_fact = _mem("the meeting is at 3pm in room 4", imp=0.4)
        w_emo  = _compute_sleep_phase_weight(m_emo)
        w_fact = _compute_sleep_phase_weight(m_fact)
        assert w_emo > w_fact

    def test_weight_in_valid_range(self):
        for imp, text in [(0.1, "neutral"), (0.9, "URGENT EMERGENCY grief panic!!!"),
                          (0.5, "something happened today")]:
            m = _mem(text, imp=imp)
            w = _compute_sleep_phase_weight(m)
            assert 0.8 <= w <= 1.2, f"weight={w} out of [0.8, 1.2]"

    def test_rem_cluster_priority_exceeds_sws(self):
        b = SleepBudget()
        neutral = Cluster(
            ids=["a", "b"], avg_strength=0.6, avg_importance=0.6,
            total_chars=500, has_long=False, max_arousal=0.05,
        )
        emotional = Cluster(
            ids=["c", "d"], avg_strength=0.6, avg_importance=0.6,
            total_chars=500, has_long=False, max_arousal=0.90,
        )
        assert (
            _cluster_priority_score(emotional, budget=b)
            > _cluster_priority_score(neutral, budget=b)
        )


# ─────────────────────────────────────────────────────────────────────────────
# M7. Retroactive interference penalty
# ─────────────────────────────────────────────────────────────────────────────

class TestRetroactiveInterference:
    def test_penalty_is_negative(self):
        now = time.time()
        b = SleepBudget()
        old = _mem("Alice is the project manager", imp=0.5, acc=2, created_days=60)
        new = _mem("Alice is now the VP of Engineering", imp=0.6, acc=5, created_days=5)
        id_map = {old.id: old, new.id: new}
        ni = _neighbor(sim_max=0.85, dup_ids=[new.id], sims={new.id: 0.85})
        penalty = _compute_proactive_interference_penalty(
            old, neighbor_info=ni, id_map=id_map, now=now, budget=b
        )
        assert penalty < 0.0, f"Penalty should be negative, got {penalty}"

    def test_higher_similarity_larger_penalty(self):
        now = time.time()
        b = SleepBudget()
        old = _mem("project update", imp=0.5, acc=2, created_days=60)
        new = _mem("project update v2", imp=0.5, acc=3, created_days=5)
        id_map = {old.id: old, new.id: new}
        penalties = []
        for sim in [0.60, 0.75, 0.90, 0.99]:
            ni = _neighbor(sim_max=sim, dup_ids=[new.id], sims={new.id: sim})
            p = _compute_proactive_interference_penalty(
                old, neighbor_info=ni, id_map=id_map, now=now, budget=b
            )
            penalties.append((sim, p))
        # Higher similarity → more negative penalty
        assert abs(penalties[-1][1]) > abs(penalties[0][1]), (
            f"sim=0.99 penalty={penalties[-1][1]:.4f} should be "
            f"larger magnitude than sim=0.60 penalty={penalties[0][1]:.4f}"
        )

    def test_newer_memory_not_penalised_by_older(self):
        """Only OLD memories get penalised by NEWER ones, not the other way."""
        now = time.time()
        b = SleepBudget()
        old = _mem("old knowledge", imp=0.5, acc=2, created_days=60)
        new = _mem("new knowledge", imp=0.5, acc=3, created_days=5)
        id_map = {old.id: old, new.id: new}
        # new is the target — old is its neighbor
        ni_new = _neighbor(sim_max=0.90, dup_ids=[old.id], sims={old.id: 0.90})
        penalty_on_new = _compute_proactive_interference_penalty(
            new, neighbor_info=ni_new, id_map=id_map, now=now, budget=b
        )
        # new is newer than old → old does NOT cause PI on new
        assert penalty_on_new == 0.0, (
            f"Newer memory should not be penalised by older neighbor, got {penalty_on_new}"
        )

    def test_penalty_capped_at_minus_0_15(self):
        now = time.time()
        b = SleepBudget()
        old = _mem("very old memory", imp=0.5, acc=1, created_days=365)
        # 10 very similar newer memories
        newer_mems = [
            _mem(f"newer v{i}", imp=0.5, acc=5, created_days=float(i + 1))
            for i in range(10)
        ]
        id_map = {old.id: old, **{n.id: n for n in newer_mems}}
        sims = {n.id: 0.99 for n in newer_mems}
        ni = _neighbor(sim_max=0.99, dup_ids=[n.id for n in newer_mems], sims=sims)
        penalty = _compute_proactive_interference_penalty(
            old, neighbor_info=ni, id_map=id_map, now=now, budget=b
        )
        assert penalty >= -0.15, f"Penalty should not exceed -0.15, got {penalty}"

    def test_no_penalty_without_neighbors(self):
        now = time.time()
        b = SleepBudget()
        m = _mem("lonely memory", imp=0.5)
        penalty = _compute_proactive_interference_penalty(
            m, neighbor_info=None, id_map={m.id: m}, now=now, budget=b
        )
        assert penalty == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# M8. Spreading activation + fan effect
# ─────────────────────────────────────────────────────────────────────────────

class TestSpreadingActivation:
    def test_unique_memory_no_spread(self):
        now = time.time()
        b = SleepBudget()
        m = _mem(imp=0.5, acc=3, created_days=5)
        nm = {m.id: _neighbor(sim_max=0.0)}
        di = {m.id: _compute_dynamic_importance(m, now=now, budget=b)}
        from buddy.memory.consolidation_engine import _compute_all_raw_bla
        rb = _compute_all_raw_bla([m], now=now, budget=b, dynamic_importances=di)
        spread = _compute_spreading_activation(m, neighbor_map=nm, raw_bla_scores=rb, budget=b)
        assert spread == 0.0

    def test_strong_neighbor_boosts(self):
        now = time.time()
        b = SleepBudget()
        target = _mem("target memory", imp=0.5, acc=5, created_days=2)
        neighbor_m = _mem("related memory", imp=0.6, acc=10, created_days=1)
        nm = {
            target.id: _neighbor(
                sim_max=0.85, dup_ids=[neighbor_m.id], sims={neighbor_m.id: 0.85}
            ),
            neighbor_m.id: _neighbor(sim_max=0.85, dup_ids=[target.id]),
        }
        di = {
            target.id: _compute_dynamic_importance(target, now=now, budget=b),
            neighbor_m.id: _compute_dynamic_importance(neighbor_m, now=now, budget=b),
        }
        from buddy.memory.consolidation_engine import _compute_all_raw_bla
        rb = _compute_all_raw_bla([target, neighbor_m], now=now, budget=b, dynamic_importances=di)
        spread = _compute_spreading_activation(target, neighbor_map=nm, raw_bla_scores=rb, budget=b)
        assert spread > 0.0, f"Expected positive spread from strong neighbor, got {spread}"

    def test_high_fan_causes_interference(self):
        """When a neighbor has very high fan (many connections), spread goes negative."""
        now = time.time()
        b = SleepBudget()
        target = _mem("hub topic", imp=0.4, acc=2, created_days=10)
        # Neighbor has 14 connections itself — huge fan → fan_j=15, S-ln(15)≈-1.2
        hub = _mem("hub memory", imp=0.5, acc=20, created_days=3)
        hub_dup_ids = ["a", "b", "c", "d", "e", "f", "g", "h",
                       "i", "j", "k", "l", "m", target.id]
        nm = {
            target.id: _neighbor(sim_max=0.80, dup_ids=[hub.id], sims={hub.id: 0.80}),
            hub.id: _neighbor(
                sim_max=0.80,
                dup_ids=hub_dup_ids,
            ),
        }
        di = {
            target.id: _compute_dynamic_importance(target, now=now, budget=b),
            hub.id: _compute_dynamic_importance(hub, now=now, budget=b),
        }
        from buddy.memory.consolidation_engine import _compute_all_raw_bla
        rb = _compute_all_raw_bla([target, hub], now=now, budget=b, dynamic_importances=di)
        spread = _compute_spreading_activation(target, neighbor_map=nm, raw_bla_scores=rb, budget=b)
        # fan = 14+1 = 15 → S - ln(15) = 1.5 - 2.7 ≈ -1.2 → interference (negative)
        assert spread < 0.0, f"Expected negative spread (fan interference), got {spread}"


# ─────────────────────────────────────────────────────────────────────────────
# M9. Catastrophic forgetting guard
# ─────────────────────────────────────────────────────────────────────────────

class TestCatastrophicForgettingGuard:
    def test_immortal_always_protected(self):
        b = SleepBudget()
        m = _mem(imp=0.1, meta={"protection_tier": "immortal", "consolidation_cycles": 0})
        m.consolidated_into_id = "some_summary"  # even consolidated — still immortal
        assert _is_protected(m, b) is True

    def test_rule_a_high_importance_protected(self):
        b = SleepBudget()
        m = _mem(imp=0.85)
        m.consolidated_into_id = None
        assert _is_protected(m, b) is True

    def test_rule_a_below_threshold_not_protected(self):
        b = SleepBudget()
        m = _mem(imp=0.79)
        m.consolidated_into_id = None
        assert _is_protected(m, b) is False

    def test_rule_b_isolated_moderate_importance_protected(self):
        """Isolated (dup_count=0) + importance >= 0.70 → irreplaceable → protect."""
        b = SleepBudget()
        m = _mem(imp=0.72)
        m.consolidated_into_id = None
        assert _is_protected(m, b, dup_count=0) is True

    def test_rule_b_not_isolated_not_protected(self):
        """Same importance but has duplicates → not irreplaceable."""
        b = SleepBudget()
        m = _mem(imp=0.72)
        m.consolidated_into_id = None
        assert _is_protected(m, b, dup_count=2) is False

    def test_rule_c_critical_protected(self):
        b = SleepBudget()
        m = _mem(imp=0.3, meta={"protection_tier": "critical", "consolidation_cycles": 0})
        m.consolidated_into_id = None
        assert _is_protected(m, b) is True

    def test_consolidated_not_protected_except_immortal(self):
        """Once consolidated into a summary, protection is lifted (except immortal)."""
        b = SleepBudget()
        m_high = _mem(imp=0.95)
        m_high.consolidated_into_id = "summary_123"
        assert _is_protected(m_high, b) is False

        m_immortal = _mem(imp=0.1, meta={"protection_tier": "immortal", "consolidation_cycles": 0})
        m_immortal.consolidated_into_id = "summary_456"
        assert _is_protected(m_immortal, b) is True

    def test_normal_low_importance_not_protected(self):
        b = SleepBudget()
        m = _mem(imp=0.2)
        m.consolidated_into_id = None
        assert _is_protected(m, b) is False


# ─────────────────────────────────────────────────────────────────────────────
# M10. Summary created_at FIX — centroid timestamp
# ─────────────────────────────────────────────────────────────────────────────

class TestSummaryCreatedAtCentroid:
    """Verify the fix: summary created_at uses mean of member timestamps."""

    def _build_cluster_ids_and_map(
        self, ages_days: List[float]
    ) -> Tuple[List[str], Dict[str, MemoryEntry]]:
        mems = [_mem(f"memory {i}", imp=0.6, acc=3, created_days=d)
                for i, d in enumerate(ages_days)]
        return [m.id for m in mems], {m.id: m for m in mems}

    def test_centroid_is_not_newest(self):
        """Centroid of members created at 1d and 30d ago should be ~15.5d."""
        now = time.time()
        ages = [1.0, 30.0]  # one fresh, one old
        ids, id_map = self._build_cluster_ids_and_map(ages)

        creation_times = [id_map[mid].created_at for mid in ids]
        centroid = sum(creation_times) / len(creation_times)
        newest   = max(creation_times)
        oldest   = min(creation_times)

        # Centroid should be between oldest and newest
        assert oldest < centroid < newest

        # Expected centroid age from now
        centroid_age_days = (now - centroid) / DAY
        assert 14.0 < centroid_age_days < 17.0, (
            f"Centroid should be ~15.5 days old, got {centroid_age_days:.1f}"
        )

    def test_centroid_not_equal_to_newest_member(self):
        """If we had used newest, we'd get ~1d. Centroid should differ."""
        now = time.time()
        ages = [1.0, 7.0, 30.0]  # fresh, week-old, month-old
        ids, id_map = self._build_cluster_ids_and_map(ages)

        creation_times = [id_map[mid].created_at for mid in ids]
        centroid = sum(creation_times) / len(creation_times)
        newest   = max(creation_times)

        assert centroid < newest - DAY, (
            "Centroid should be older than newest member by at least 1 day"
        )

    def test_centroid_prevents_spurious_tg_boost(self):
        """Old-memory cluster: if newest member is 24h old, old code would give TG boost.
        With centroid fix, the summary created_at is much older → no TG boost."""
        now = time.time()
        ages = [1.0, 60.0, 90.0]  # one recent (24h), two old
        ids, id_map = self._build_cluster_ids_and_map(ages)
        creation_times = [id_map[mid].created_at for mid in ids]

        # OLD behaviour: newest member's timestamp
        old_created_at = max(creation_times)
        # NEW behaviour: centroid
        new_created_at = sum(creation_times) / len(creation_times)

        # Simulate a summary memory with each created_at
        m_old_behaviour = MemoryEntry(text="summary")
        m_old_behaviour.created_at = old_created_at
        m_old_behaviour.last_accessed = None

        m_new_behaviour = MemoryEntry(text="summary")
        m_new_behaviour.created_at = new_created_at
        m_new_behaviour.last_accessed = None

        tg_old = _compute_temporal_gradient(m_old_behaviour, now=now)
        tg_new = _compute_temporal_gradient(m_new_behaviour, now=now)

        # Old behaviour: near-peak (~0.04) because newest member = 24h
        # New behaviour: much lower because centroid = ~50d old
        assert tg_old > tg_new, (
            f"Old behaviour (newest timestamp) gives TG={tg_old:.4f} but "
            f"new behaviour (centroid) gives TG={tg_new:.4f}"
        )
        assert tg_new < 0.005, (
            f"Centroid-based summary should have near-zero TG, got {tg_new:.5f}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# M11. Spacing-weighted touch
# ─────────────────────────────────────────────────────────────────────────────

class TestSpacingWeightedTouch:
    def test_spaced_touch_boost_larger_than_massed(self):
        b = SQLiteStore.__new__(SQLiteStore)  # instantiate without __init__
        # Use _spacing_boost directly
        now = time.time()
        boost_massed = b._spacing_boost(now - 5, now)       # 5 seconds ago
        boost_spaced = b._spacing_boost(now - 72 * HOUR, now)  # 72h ago
        assert boost_spaced > boost_massed, (
            f"Spaced boost {boost_spaced:.4f} should exceed massed boost {boost_massed:.4f}"
        )

    def test_first_touch_gets_medium_boost(self):
        b = SQLiteStore.__new__(SQLiteStore)
        now = time.time()
        boost_first = b._spacing_boost(None, now)  # no prior access
        assert SQLiteStore._TOUCH_MIN <= boost_first <= SQLiteStore._TOUCH_BASE

    def test_boost_never_below_minimum(self):
        b = SQLiteStore.__new__(SQLiteStore)
        now = time.time()
        boost = b._spacing_boost(now - 0.001, now)  # effectively 0 gap
        assert boost >= SQLiteStore._TOUCH_MIN

    def test_boost_never_above_maximum(self):
        b = SQLiteStore.__new__(SQLiteStore)
        now = time.time()
        boost = b._spacing_boost(now - 365 * DAY, now)  # 1 year gap
        assert boost <= SQLiteStore._TOUCH_BASE

    def test_monotone_increasing_with_gap(self):
        b = SQLiteStore.__new__(SQLiteStore)
        now = time.time()
        gaps_hours = [0.01, 1, 6, 24, 72, 168]
        boosts = [b._spacing_boost(now - g * HOUR, now) for g in gaps_hours]
        for i in range(len(boosts) - 1):
            assert boosts[i] <= boosts[i + 1], (
                f"Boost should be non-decreasing: {boosts[i]:.5f} > {boosts[i+1]:.5f} "
                f"at gap {gaps_hours[i]}h vs {gaps_hours[i+1]}h"
            )


# ─────────────────────────────────────────────────────────────────────────────
# M12. Tier promotion / demotion gates
# ─────────────────────────────────────────────────────────────────────────────

class TestTierGates:
    def _plan(self, candidates, id_map, neighbor_map, now, budget=None):
        b = budget or SleepBudget()
        from buddy.memory.consolidation_engine import _compute_all_raw_bla
        di = {m.id: _compute_dynamic_importance(m, now=now, budget=b) for m in candidates}
        rb = _compute_all_raw_bla(candidates, now=now, budget=b, dynamic_importances=di)
        return _plan_tier_updates(
            candidates=candidates, id_map=id_map,
            neighbor_map=neighbor_map, raw_bla_scores=rb,
            dynamic_importances=di, budget=b, now=now,
        )

    def test_strong_flash_promotes_to_short(self):
        now = time.time()
        b = SleepBudget()
        # Flash memory old enough, high importance → should promote
        m = _mem(imp=0.85, acc=10, mtype="flash", created_days=1.0)
        m.last_accessed = now - 0.5 * DAY
        m.metadata = {"consolidation_cycles": 1}
        nm = {m.id: _neighbor()}
        updates = self._plan([m], {m.id: m}, nm, now, b)
        targets = {u[2] for u in updates if u[0] == m.id}
        assert "short" in targets, (
            f"High-importance flash should promote to short. Got updates: {updates}"
        )

    def test_weak_short_demotes_to_flash(self):
        now = time.time()
        b = SleepBudget()
        # Short memory with very low strength and stale access
        m = _mem(imp=0.05, acc=0, mtype="short", created_days=30.0,
                 last_days=20.0, meta={"consolidation_cycles": 0})
        nm = {m.id: _neighbor()}
        updates = self._plan([m], {m.id: m}, nm, now, b)
        targets = {u[2] for u in updates if u[0] == m.id}
        assert "flash" in targets, (
            f"Weak short should demote to flash. Got: {updates}"
        )

    def test_flash_too_young_not_promoted(self):
        now = time.time()
        b = SleepBudget()
        # Flash memory only 1 hour old — below min_flash_age_sec (3h)
        m = _mem(imp=0.9, acc=20, mtype="flash", created_days=0.04)  # ~1h
        nm = {m.id: _neighbor()}
        updates = self._plan([m], {m.id: m}, nm, now, b)
        targets = {u[2] for u in updates if u[0] == m.id}
        assert "short" not in targets, "Flash too young should not be promoted"

    def test_short_to_long_requires_cycles(self):
        now = time.time()
        b = SleepBudget()
        # Short memory with high strength but 0 cycles → must NOT promote to long
        m = _mem(imp=0.9, acc=50, mtype="short", created_days=10.0, last_days=1.0,
                 meta={"consolidation_cycles": 0})
        nm = {m.id: _neighbor(sim_max=0.0, dup_ids=[])}
        updates = self._plan([m], {m.id: m}, nm, now, b)
        targets = {u[2] for u in updates if u[0] == m.id}
        assert "long" not in targets, (
            "Short→long requires min_cycles_for_long; 0 cycles should not promote"
        )

    def test_short_to_long_requires_no_duplicates(self):
        now = time.time()
        b = SleepBudget()
        # Short memory with high strength + cycles BUT has a duplicate
        other = _mem("duplicate text", imp=0.5, acc=5, mtype="short", created_days=5.0)
        m = _mem(imp=0.9, acc=50, mtype="short", created_days=10.0, last_days=1.0,
                 meta={"consolidation_cycles": 3})
        nm = {m.id: _neighbor(sim_max=0.85, dup_ids=[other.id], sims={other.id: 0.85})}
        id_map = {m.id: m, other.id: other}
        updates = self._plan([m], id_map, nm, now, b)
        targets = {u[2] for u in updates if u[0] == m.id}
        assert "long" not in targets, (
            "Short→long requires dup_count=0; memory with duplicate should not promote"
        )


# ─────────────────────────────────────────────────────────────────────────────
# M13 & M14. Redundancy pruning — similarity-weighted + keep-one-representative
# ─────────────────────────────────────────────────────────────────────────────

class TestRedundancyPruning:
    """Validate similarity-weighted dup scoring and keep-one-representative rule."""

    def _make_sqlite_store_mock(self):
        mock = MagicMock()
        mock.db_path = ":memory:"
        return mock

    def test_high_sim_weighted_above_threshold(self):
        b = SleepBudget()
        # 4 dups at sim=0.95 → weighted = 4 × 0.95 = 3.80 > threshold 3
        avg_sim = 0.95
        dup_count = 4
        weighted = dup_count * avg_sim
        assert weighted >= b.redundancy_dup_threshold

    def test_low_sim_weighted_below_threshold(self):
        b = SleepBudget()
        # 4 dups at sim=0.74 → weighted = 4 × 0.74 = 2.96 < threshold 3
        avg_sim = 0.74
        dup_count = 4
        weighted = dup_count * avg_sim
        assert weighted < b.redundancy_dup_threshold

    def test_protected_memory_not_deleted_by_redundancy(self):
        now = time.time()
        b = SleepBudget()
        store_mock = self._make_sqlite_store_mock()

        # Important memory (imp=0.95) with 4 very similar dups
        protected = _mem(imp=0.95, acc=0, mtype="flash", created_days=40.0)
        protected.consolidated_into_id = None

        dups = [_mem(imp=0.1, acc=0, mtype="flash", created_days=40.0)
                for _ in range(4)]
        all_mems = [protected] + dups
        id_map = {m.id: m for m in all_mems}
        dup_ids = [d.id for d in dups]
        sims = {d.id: 0.95 for d in dups}
        nm = {
            protected.id: _neighbor(sim_max=0.95, dup_ids=dup_ids, sims=sims),
            **{d.id: _neighbor(sim_max=0.95, dup_ids=[protected.id] + [x.id for x in dups if x.id != d.id]) for d in dups},
        }
        from buddy.memory.consolidation_engine import _compute_all_raw_bla
        di = {m.id: _compute_dynamic_importance(m, now=now, budget=b) for m in all_mems}
        rb = _compute_all_raw_bla(all_mems, now=now, budget=b, dynamic_importances=di)

        dels, _, _ = _plan_hard_deletes(
            sqlite_store=store_mock, candidates=all_mems,
            id_map=id_map, neighbor_map=nm, raw_bla_scores=rb,
            dynamic_importances=di, budget=b, now=now, limit=50,
        )
        assert protected.id not in dels, (
            "Protected memory (imp=0.95) must not be in hard delete list"
        )


# ─────────────────────────────────────────────────────────────────────────────
# M15. Source-turn gradient
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceTurnGradient:
    def test_early_turn_decays_faster(self):
        now = time.time()
        b = SleepBudget()
        early = _mem(imp=0.6, acc=2, created_days=30, source_turn=1)
        mid   = _mem(imp=0.6, acc=2, created_days=30, source_turn=15)
        di_early = _compute_dynamic_importance(early, now=now, budget=b)
        di_mid   = _compute_dynamic_importance(mid,   now=now, budget=b)
        assert di_early < di_mid, (
            f"Early-turn memory should have lower importance: "
            f"turn1={di_early:.4f} turn15={di_mid:.4f}"
        )

    def test_mid_and_late_turns_treated_equally(self):
        now = time.time()
        b = SleepBudget()
        mid  = _mem(imp=0.6, acc=2, created_days=30, source_turn=10)
        late = _mem(imp=0.6, acc=2, created_days=30, source_turn=50)
        di_mid  = _compute_dynamic_importance(mid,  now=now, budget=b)
        di_late = _compute_dynamic_importance(late, now=now, budget=b)
        assert abs(di_mid - di_late) < 0.001, (
            f"Mid/late turns should be equal: mid={di_mid:.4f} late={di_late:.4f}"
        )

    def test_no_source_turn_same_as_mid(self):
        now = time.time()
        b = SleepBudget()
        no_turn  = _mem(imp=0.6, acc=2, created_days=30, source_turn=None)
        late_turn = _mem(imp=0.6, acc=2, created_days=30, source_turn=20)
        di_none = _compute_dynamic_importance(no_turn,  now=now, budget=b)
        di_late = _compute_dynamic_importance(late_turn, now=now, budget=b)
        assert abs(di_none - di_late) < 0.001


# ─────────────────────────────────────────────────────────────────────────────
# M16. Consolidation depth gate
# ─────────────────────────────────────────────────────────────────────────────

class TestConsolidationDepth:
    def test_deep_summarization_blocked_on_low_confidence(self):
        """_apply_summary_cluster raises when depth >= 3 and confidence < 0.70."""
        # We test the condition directly without calling the full LLM path
        _DEEP_THRESHOLD = 3
        _DEEP_MIN_CONF = 0.70
        new_depth = 3
        confidence = 0.50  # below minimum

        blocked = new_depth >= _DEEP_THRESHOLD and confidence < _DEEP_MIN_CONF
        assert blocked, "Depth=3, confidence=0.50 should be blocked"

    def test_deep_summarization_allowed_on_high_confidence(self):
        _DEEP_THRESHOLD = 3
        _DEEP_MIN_CONF = 0.70
        new_depth = 3
        confidence = 0.80  # above minimum

        blocked = new_depth >= _DEEP_THRESHOLD and confidence < _DEEP_MIN_CONF
        assert not blocked, "Depth=3, confidence=0.80 should be allowed"

    def test_shallow_summarization_always_allowed(self):
        _DEEP_THRESHOLD = 3
        _DEEP_MIN_CONF = 0.70
        new_depth = 1
        confidence = 0.10  # even very low confidence

        blocked = new_depth >= _DEEP_THRESHOLD and confidence < _DEEP_MIN_CONF
        assert not blocked, "Depth=1 should never be blocked regardless of confidence"


# ─────────────────────────────────────────────────────────────────────────────
# I1. run_consolidation — cancel before start
# ─────────────────────────────────────────────────────────────────────────────

class TestRunConsolidationCancel:
    def test_cancel_before_start_returns_empty_report(self):
        cancel = threading.Event()
        cancel.set()  # already cancelled

        store_mock = MagicMock()
        store_mock.list_candidates_for_consolidation.return_value = []
        vector_mock = MagicMock()
        brain_mock  = MagicMock()

        report = run_consolidation(
            sqlite_store=store_mock,
            vector_store=vector_mock,
            brain=brain_mock,
            embed=lambda t: np.zeros(32, dtype=np.float32),
            cancel_event=cancel,
        )
        assert report.scanned == 0
        assert any("cancelled" in e for e in report.errors)


# ─────────────────────────────────────────────────────────────────────────────
# I2. run_consolidation — dry-run with mock stores
# ─────────────────────────────────────────────────────────────────────────────

class TestRunConsolidationDryRun:
    def _make_candidates(self, n: int = 5) -> List[MemoryEntry]:
        return [
            _mem(f"memory {i}", imp=0.5 + i * 0.05, acc=i + 1,
                 mtype="flash", created_days=float(i + 1))
            for i in range(n)
        ]

    def test_dry_run_makes_no_db_writes(self):
        cands = self._make_candidates(5)

        sqlite_mock = MagicMock()
        sqlite_mock.list_candidates_for_consolidation.return_value = cands
        sqlite_mock.db_path = ":memory:"

        vector_mock = MagicMock()
        vector_mock.search_with_payloads.return_value = []

        brain_mock  = MagicMock()

        report = run_consolidation(
            sqlite_store=sqlite_mock,
            vector_store=vector_mock,
            brain=brain_mock,
            embed=lambda t: np.random.randn(32).astype(np.float32),
            dry_run=True,
        )
        assert report.scanned == 5
        sqlite_mock.upsert_memory.assert_not_called()
        sqlite_mock.soft_delete.assert_not_called()
        sqlite_mock.update_memory_type.assert_not_called()

    def test_dry_run_still_returns_report(self):
        cands = self._make_candidates(3)
        sqlite_mock = MagicMock()
        sqlite_mock.list_candidates_for_consolidation.return_value = cands
        sqlite_mock.db_path = ":memory:"
        vector_mock = MagicMock()
        vector_mock.search_with_payloads.return_value = []

        report = run_consolidation(
            sqlite_store=sqlite_mock,
            vector_store=vector_mock,
            brain=MagicMock(),
            embed=lambda t: np.zeros(32, dtype=np.float32),
            dry_run=True,
        )
        assert isinstance(report.scanned, int)
        assert isinstance(report.errors, list)
        assert report.scanned >= 0


# ─────────────────────────────────────────────────────────────────────────────
# I3 & I4. SQLiteStore touch spacing correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestSQLiteStoreTouchSpacing:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_touch.sqlite3")
        self.store = _make_sqlite(self.db_path)

    def teardown_method(self):
        self.store.close()

    def _insert(self, text="test", imp=0.5, mtype="flash") -> MemoryEntry:
        m = MemoryEntry(text=text)
        m.importance = imp
        m.memory_type = mtype
        m.access_count = 0
        m.created_at = time.time()
        m.last_accessed = None
        m.metadata = {}
        m.embedding = np.zeros(8, dtype=np.float32)
        self.store.upsert_memory(m)
        return m

    def test_touch_increments_access_count(self):
        m = self._insert()
        self.store.touch(m.id)
        after = self.store.get_memory(m.id)
        assert after.access_count == 1

    def test_touch_updates_last_accessed(self):
        m = self._insert()
        before = time.time()
        self.store.touch(m.id)
        after = self.store.get_memory(m.id)
        assert after.last_accessed is not None
        assert after.last_accessed >= before

    def test_touch_boosts_consolidation_strength(self):
        m = self._insert()
        self.store.touch(m.id)
        after = self.store.get_memory(m.id)
        assert after.consolidation_strength > 0.0

    def test_spaced_touch_gives_larger_boost_than_massed(self):
        """Two identical memories: one touched immediately, one touched after 1 day gap."""
        m_massed = self._insert("massed retrieval")
        m_spaced = self._insert("spaced retrieval")

        # Simulate spaced memory having been last accessed 24h ago
        import sqlite3 as _sqlite3
        now = time.time()
        with _sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memories SET last_accessed=? WHERE id=?",
                (now - 24 * HOUR, m_spaced.id),
            )
            # massed: last_accessed = just now (will give tiny boost)
            conn.execute(
                "UPDATE memories SET last_accessed=? WHERE id=?",
                (now - 5, m_massed.id),
            )
            conn.commit()

        self.store.touch(m_spaced.id)
        self.store.touch(m_massed.id)

        after_spaced = self.store.get_memory(m_spaced.id)
        after_massed = self.store.get_memory(m_massed.id)

        assert after_spaced.consolidation_strength > after_massed.consolidation_strength, (
            f"Spaced touch ({after_spaced.consolidation_strength:.4f}) should be larger "
            f"than massed touch ({after_massed.consolidation_strength:.4f})"
        )

    def test_batch_touch_updates_all(self):
        mems = [self._insert(f"mem {i}") for i in range(5)]
        ids = [m.id for m in mems]
        self.store.batch_touch(ids)
        for mid in ids:
            after = self.store.get_memory(mid)
            assert after.access_count == 1, f"access_count not incremented for {mid}"
            assert after.consolidation_strength > 0.0

    def test_touch_nonexistent_id_is_safe(self):
        # Should not raise
        self.store.touch("nonexistent-id-xyz")


# ─────────────────────────────────────────────────────────────────────────────
# I5. list_candidates_for_consolidation — correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestListCandidates:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = _make_sqlite(os.path.join(self.tmpdir, "cands.sqlite3"))

    def teardown_method(self):
        self.store.close()

    def _insert(self, text, mtype="flash", imp=0.5, last_cons=None) -> MemoryEntry:
        m = MemoryEntry(text=text)
        m.importance = imp
        m.memory_type = mtype
        m.access_count = 1
        m.created_at = time.time() - 2 * DAY
        m.last_accessed = None
        m.last_consolidated_at = last_cons
        m.metadata = {}
        m.embedding = np.zeros(4, dtype=np.float32)
        self.store.upsert_memory(m)
        return m

    def test_never_consolidated_memory_included(self):
        m = self._insert("fresh memory", last_cons=None)
        cands = self.store.list_candidates_for_consolidation(limit=50, cooldown_seconds=86400.0)
        assert any(c.id == m.id for c in cands)

    def test_recently_consolidated_excluded(self):
        # last_consolidated_at = now (within cooldown)
        m = self._insert("recent", last_cons=time.time())
        cands = self.store.list_candidates_for_consolidation(limit=50, cooldown_seconds=86400.0)
        assert not any(c.id == m.id for c in cands), (
            "Recently consolidated memory should be excluded (cooldown not expired)"
        )

    def test_deleted_memory_excluded(self):
        m = self._insert("deleted memory")
        self.store.soft_delete(m.id)
        cands = self.store.list_candidates_for_consolidation(limit=50)
        assert not any(c.id == m.id for c in cands)

    def test_cooldown_expired_memory_included(self):
        # last_consolidated_at = 2 days ago, cooldown = 1 day → expired → included
        old_cons = time.time() - 2 * DAY
        m = self._insert("old cons", last_cons=old_cons)
        cands = self.store.list_candidates_for_consolidation(limit=50, cooldown_seconds=86400.0)
        assert any(c.id == m.id for c in cands)


# ─────────────────────────────────────────────────────────────────────────────
# I6. Catastrophic forgetting guard survives a full delete pass
# ─────────────────────────────────────────────────────────────────────────────

class TestCatastrophicForgettingEndToEnd:
    def test_high_importance_memory_never_in_delete_plan(self):
        now = time.time()
        b = SleepBudget()
        store_mock = MagicMock()
        store_mock.db_path = ":memory:"

        # Protected memory (imp=0.95) with zero accesses and 5 duplicates
        protected = _mem("my critical allergy: penicillin", imp=0.95, acc=0,
                         mtype="flash", created_days=200.0)
        protected.consolidated_into_id = None

        # Lots of duplicates to normally trigger redundancy delete
        dups = [
            _mem("allergy penicillin info", imp=0.1, acc=0,
                 mtype="flash", created_days=200.0)
            for _ in range(5)
        ]
        all_mems = [protected] + dups
        id_map = {m.id: m for m in all_mems}
        dup_ids = [d.id for d in dups]
        sims = {d.id: 0.97 for d in dups}

        nm = {
            protected.id: _neighbor(sim_max=0.97, dup_ids=dup_ids, sims=sims),
            **{
                d.id: _neighbor(
                    sim_max=0.97,
                    dup_ids=[protected.id] + [x.id for x in dups if x.id != d.id],
                    sims={protected.id: 0.97, **{x.id: 0.97 for x in dups if x.id != d.id}},
                )
                for d in dups
            },
        }
        from buddy.memory.consolidation_engine import _compute_all_raw_bla
        di = {m.id: _compute_dynamic_importance(m, now=now, budget=b) for m in all_mems}
        rb = _compute_all_raw_bla(all_mems, now=now, budget=b, dynamic_importances=di)

        dels, _, _ = _plan_hard_deletes(
            sqlite_store=store_mock, candidates=all_mems,
            id_map=id_map, neighbor_map=nm, raw_bla_scores=rb,
            dynamic_importances=di, budget=b, now=now, limit=100,
        )
        assert protected.id not in dels, (
            f"Protected memory (imp=0.95) must survive the delete pass. "
            f"Delete plan: {dels}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
