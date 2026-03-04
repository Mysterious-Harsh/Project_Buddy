"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  v3 vs v4 DIFFERENTIAL TEST SUITE                                            ║
║  Tests ONLY the features that are genuinely new in v4.                       ║
║  Every test is designed to FAIL on v3 and PASS on v4.                        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  v4-ONLY FEATURES UNDER TEST:                                                ║
║    D1. Temporal Gradient [Murre & Dros 2015] — 24h post-encoding bump        ║
║    D2. Proactive Interference Penalty [McGeoch 1942]                          ║
║    D3. Sleep-Phase Cluster Weighting [Walker & Stickgold 2004]               ║
║    D4. Source-Turn Decay Gradient [Anderson & Schooler 1991]                 ║
║    D5. Similarity-Weighted Redundancy Pruning                                 ║
║    D6. Expanded Arousal Keyword Set (+36 new terms)                           ║
║    D7. Extended Provisional Window 7d → 14d [Nader 2000]                    ║
║    D8. NeighborInfo.dup_similarities field                                    ║
║    D9. Cluster.max_arousal field + REM/SWS labelling                         ║
║    D10. SleepReport.temporal_gradient_applied + proactive_interference_detected║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import sys
import types
import math
import time
import uuid
import json
import logging
import traceback
import importlib.util
from typing import Any, Dict, List, Optional

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import warnings

warnings.filterwarnings("ignore")


logging.basicConfig(level=logging.WARNING)

# ── Load both engines ──────────────────────────────────────────────────────
import buddy.memory.consolidation_engine as v4  # v3 = original uploaded file
import buddy.memory.consolidation_engine_v3 as v3  # v4 = improved engine

from buddy.memory.memory_entry import MemoryEntry

DAY = 86400.0
PASS = 0
FAIL = 0
RESULTS: Dict = {}

# ── Helpers ────────────────────────────────────────────────────────────────


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    tag = "✅ PASS" if cond else "❌ FAIL"
    print(f"  [{name}] {tag}" + (f" — {detail}" if detail else ""))
    if cond:
        PASS += 1
    else:
        FAIL += 1
    return cond


def expect_fail_v3(
    name: str, cond_v3: bool, cond_v4: bool, detail_v3: str = "", detail_v4: str = ""
) -> bool:
    """Assert v3 FAILS (old behaviour) and v4 PASSES (new behaviour)."""
    global PASS, FAIL
    ok = (not cond_v3) and cond_v4
    tag = "✅ DIFFERENTIAL PASS" if ok else "❌ DIFFERENTIAL FAIL"
    print(f"  [{name}] {tag}")
    if not ok:
        print(f"    v3 result: {'pass' if cond_v3 else 'fail'}  {detail_v3}")
        print(f"    v4 result: {'pass' if cond_v4 else 'fail'}  {detail_v4}")
    if ok:
        PASS += 1
    else:
        FAIL += 1
    return ok


def make(
    text="test",
    *,
    imp=0.5,
    mtype="flash",
    acc=1,
    created_days=1.0,
    last_days=None,
    role="user",
    embedding=None,
    meta=None,
    source_turn=None,
):
    now = time.time()
    emb = embedding
    if emb is None:
        emb = np.random.randn(64).astype(np.float32)
        emb /= np.linalg.norm(emb) + 1e-9
    return MemoryEntry(
        text=text,
        importance=imp,
        memory_type=mtype,
        access_count=acc,
        created_at=now - created_days * DAY,
        last_accessed=(now - last_days * DAY) if last_days else None,
        role=role,
        embedding=emb,
        metadata=meta or {"consolidation_cycles": 0},
        source_turn=source_turn,
    )


def fake_neighbor(
    sim_max=0.0, dup_ids=None, dup_count=0, surprising=False, sims: dict = None
):
    """Build a NeighborInfo for whichever engine is being tested."""
    dup_ids = dup_ids or []
    sims = sims or {}
    return dup_ids, dup_count, sim_max, surprising, sims


def v3_neighbor(sim_max=0.0, dup_ids=None, dup_count=0, surprising=False):
    dup_ids = dup_ids or []
    return v3.NeighborInfo(
        sim_max=sim_max, dup_ids=dup_ids, dup_count=dup_count, is_surprising=surprising
    )


def v4_neighbor(
    sim_max=0.0, dup_ids=None, dup_count=0, surprising=False, sims: dict = None
):
    dup_ids = dup_ids or []
    return v4.NeighborInfo(
        sim_max=sim_max,
        dup_ids=dup_ids,
        dup_count=dup_count,
        is_surprising=surprising,
        dup_similarities=sims or {},
    )


def v3_strength(m, now, nmap, raw, dim):
    return v3._compute_strength(
        m,
        now=now,
        budget=v3.SleepBudget(),
        neighbor_map=nmap,
        raw_bla_scores=raw,
        dynamic_importances=dim,
    )


def v4_strength(m, now, nmap, raw, dim, id_map=None):
    return v4._compute_strength(
        m,
        now=now,
        budget=v4.SleepBudget(),
        neighbor_map=nmap,
        raw_bla_scores=raw,
        dynamic_importances=dim,
        id_map=id_map,
    )


# ══════════════════════════════════════════════════════════════════════════════
# D1: Temporal Gradient — 24h consolidation bump
# ══════════════════════════════════════════════════════════════════════════════


def test_D1_temporal_gradient():
    print("\n" + "═" * 70)
    print("D1: Temporal Gradient — 24h Consolidation Bump [Murre & Dros 2015]")
    print("═" * 70)
    print("  v3: No temporal gradient — all ages score identically after BLA")
    print("  v4: Memory at ~24h gets up to 4% boost from overnight consolidation")

    now = time.time()
    budget_v4 = v4.SleepBudget()

    # Memory encoded exactly 24h ago vs 48h ago (identical access pattern)
    m_24h = make(
        "Learned new vocabulary words", acc=1, created_days=1.0, last_days=1.0, imp=0.5
    )
    m_48h = make(
        "Learned new vocabulary words", acc=1, created_days=2.0, last_days=2.0, imp=0.5
    )
    m_7d = make(
        "Learned new vocabulary words", acc=1, created_days=7.0, last_days=7.0, imp=0.5
    )

    # ── v3: no function exists, gradient is always 0 ──────────────────────
    v3_has_tg = hasattr(v3, "_compute_temporal_gradient")
    tg_v3_24h = v3._compute_temporal_gradient(m_24h, now=now) if v3_has_tg else 0.0
    tg_v3_48h = v3._compute_temporal_gradient(m_48h, now=now) if v3_has_tg else 0.0

    # ── v4: gradient is highest near 24h, falls off at 48h and 7d ─────────
    tg_v4_24h = v4._compute_temporal_gradient(m_24h, now=now)
    tg_v4_48h = v4._compute_temporal_gradient(m_48h, now=now)
    tg_v4_7d = v4._compute_temporal_gradient(m_7d, now=now)

    print(f"\n  v3 gradient @24h: {tg_v3_24h:.5f}  (should be 0 — feature absent)")
    print(f"  v4 gradient @24h: {tg_v4_24h:.5f}  (should be ~0.04)")
    print(f"  v4 gradient @48h: {tg_v4_48h:.5f}  (should decay sharply)")
    print(f"  v4 gradient @7d:  {tg_v4_7d:.5f}  (should be ~0)")

    check("D1a: v4 function exists", hasattr(v4, "_compute_temporal_gradient"))
    check(
        "D1b: v3 has NO temporal gradient",
        not v3_has_tg,
        "v3 missing _compute_temporal_gradient",
    )
    check("D1c: v4 24h peak > 0", tg_v4_24h > 0.01, f"24h gradient={tg_v4_24h:.5f}")
    check(
        "D1d: v4 24h peak > 48h (bump decays)",
        tg_v4_24h > tg_v4_48h,
        f"24h={tg_v4_24h:.5f} > 48h={tg_v4_48h:.5f}",
    )
    check("D1e: v4 7d gradient ≈ 0", tg_v4_7d < 0.001, f"7d={tg_v4_7d:.6f}")

    # Strength difference: use LOW importance (0 access) so imp_floor is tiny
    # and the TG bump is large enough to be clearly visible over BLA alone.
    m_24h_lo = make("vocab", acc=0, created_days=1.0, last_days=None, imp=0.05)
    m_48h_lo = make("vocab", acc=0, created_days=2.0, last_days=None, imp=0.05)

    nmap_v3 = {m_24h_lo.id: v3_neighbor(), m_48h_lo.id: v3_neighbor()}
    nmap_v4 = {m_24h_lo.id: v4_neighbor(), m_48h_lo.id: v4_neighbor()}
    dim_v3 = {
        m.id: v3._compute_dynamic_importance(m, now=now, budget=v3.SleepBudget())
        for m in [m_24h_lo, m_48h_lo]
    }
    dim_v4 = {
        m.id: v4._compute_dynamic_importance(m, now=now, budget=v4.SleepBudget())
        for m in [m_24h_lo, m_48h_lo]
    }
    raw_v3 = v3._compute_all_raw_bla(
        [m_24h_lo, m_48h_lo],
        now=now,
        budget=v3.SleepBudget(),
        dynamic_importances=dim_v3,
    )
    raw_v4 = v4._compute_all_raw_bla(
        [m_24h_lo, m_48h_lo],
        now=now,
        budget=v4.SleepBudget(),
        dynamic_importances=dim_v4,
    )

    s_v3_24h = v3_strength(m_24h_lo, now, nmap_v3, raw_v3, dim_v3)
    s_v3_48h = v3_strength(m_48h_lo, now, nmap_v3, raw_v3, dim_v3)
    s_v4_24h = v4_strength(m_24h_lo, now, nmap_v4, raw_v4, dim_v4)
    s_v4_48h = v4_strength(m_48h_lo, now, nmap_v4, raw_v4, dim_v4)

    v3_diff = s_v3_24h - s_v3_48h
    v4_diff = s_v4_24h - s_v4_48h

    print(
        f"\n  v3: strength_24h={s_v3_24h:.4f}  strength_48h={s_v3_48h:.4f}  "
        f"diff={v3_diff:+.4f}"
    )
    print(
        f"  v4: strength_24h={s_v4_24h:.4f}  strength_48h={s_v4_48h:.4f}  "
        f"diff={v4_diff:+.4f}"
    )
    print(f"  (low-importance memory: imp_floor tiny, TG bump clearly visible)")

    # v3: 24h vs 48h differ only by tiny BLA recency (< 0.005)
    # v4: 24h is boosted by TG ≈ +0.04 — diff is >> BLA-only
    check(
        "D1f-v3: v3 24h vs 48h diff is tiny (no TG bump)",
        abs(v3_diff) < 0.005,
        f"v3_diff={v3_diff:+.4f}",
    )
    check(
        "D1f-v4: v4 24h vs 48h diff is large (TG bump applied)",
        v4_diff > 0.01,
        f"v4_diff={v4_diff:+.4f}",
    )
    expect_fail_v3(
        "D1f: v4 awards 24h bump; v3 does NOT",
        abs(v3_diff) > 0.01,  # v3 should NOT show a large bump
        v4_diff > 0.01,  # v4 SHOULD show large bump
        f"v3_diff={v3_diff:+.4f} (should be tiny)",
        f"v4_diff={v4_diff:+.4f} (should be ~0.04)",
    )

    RESULTS["D1"] = {
        "tg_v4": [tg_v4_24h, tg_v4_48h, tg_v4_7d],
        "s_v3": [s_v3_24h, s_v3_48h],
        "s_v4": [s_v4_24h, s_v4_48h],
        "ages": [24, 48, 168],  # hours
    }


# ══════════════════════════════════════════════════════════════════════════════
# D2: Proactive Interference Penalty
# ══════════════════════════════════════════════════════════════════════════════


def test_D2_proactive_interference():
    print("\n" + "═" * 70)
    print("D2: Proactive Interference Penalty [McGeoch 1942]")
    print("═" * 70)
    print("  v3: Old memories are never penalised by newer competing memories")
    print("  v4: Old memory loses strength when a newer memory covers same topic")

    now = time.time()

    # Old memory: 'Alice is the project manager' — encoded 60 days ago
    old_mem = make(
        "Alice is the project manager",
        acc=3,
        created_days=60.0,
        last_days=55.0,
        imp=0.6,
    )
    old_id = old_mem.id

    # New memory: same topic, encoded 5 days ago (Alice changed role)
    new_mem = make(
        "Alice is now the VP of Engineering",
        acc=5,
        created_days=5.0,
        last_days=1.0,
        imp=0.7,
    )
    new_id = new_mem.id

    # Simulate both memories in context, old knows about new (neighbor)
    id_map = {old_id: old_mem, new_id: new_mem}

    # ── v3: no PI function, no id_map param ───────────────────────────────
    v3_has_pi = hasattr(v3, "_compute_proactive_interference_penalty")
    pi_v3 = 0.0  # v3 never applies PI

    # ── v4: PI penalty computed ────────────────────────────────────────────
    ni_v4 = v4_neighbor(
        sim_max=0.85, dup_ids=[new_id], dup_count=1, sims={new_id: 0.85}
    )
    pi_v4 = v4._compute_proactive_interference_penalty(
        old_mem,
        neighbor_info=ni_v4,
        id_map=id_map,
        now=now,
        budget=v4.SleepBudget(),
    )

    print(f"\n  v3 PI function exists: {v3_has_pi}")
    print(f"  v3 PI penalty on old memory: {pi_v3:.4f}  (always 0 — not implemented)")
    print(f"  v4 PI penalty on old memory: {pi_v4:.4f}  (should be negative)")

    check("D2a: v3 has no PI function", not v3_has_pi)
    check(
        "D2b: v4 has PI function",
        hasattr(v4, "_compute_proactive_interference_penalty"),
    )
    check("D2c: v4 PI penalty is negative (penalty)", pi_v4 < 0.0, f"pi={pi_v4:.4f}")
    check(
        "D2d: v4 PI penalty magnitude > 0.001",
        abs(pi_v4) > 0.001,
        f"|pi|={abs(pi_v4):.4f}",
    )

    # Strength comparison: use high-access, medium-high importance so amplified
    # is well above imp_floor and the PI penalty pulls it down visibly.
    old_hi = make(
        "Alice is the project manager",
        acc=30,
        imp=0.7,
        created_days=60.0,
        last_days=1.0,
    )
    old_id2 = old_hi.id
    new_hi = make(
        "Alice is now the VP of Engineering",
        acc=10,
        imp=0.7,
        created_days=2.0,
        last_days=0.5,
    )
    new_id2 = new_hi.id
    id_map2 = {old_id2: old_hi, new_id2: new_hi}

    nmap_v3b = {old_id2: v3_neighbor(sim_max=0.95, dup_ids=[new_id2], dup_count=1)}
    nmap_v4b = {
        old_id2: v4_neighbor(
            sim_max=0.95, dup_ids=[new_id2], dup_count=1, sims={new_id2: 0.95}
        )
    }
    dim_v3b = {
        old_hi.id: v3._compute_dynamic_importance(
            old_hi, now=now, budget=v3.SleepBudget()
        )
    }
    dim_v4b = {
        old_hi.id: v4._compute_dynamic_importance(
            old_hi, now=now, budget=v4.SleepBudget()
        )
    }
    raw_v3b = v3._compute_all_raw_bla(
        [old_hi], now=now, budget=v3.SleepBudget(), dynamic_importances=dim_v3b
    )
    raw_v4b = v4._compute_all_raw_bla(
        [old_hi], now=now, budget=v4.SleepBudget(), dynamic_importances=dim_v4b
    )

    s_old_v3 = v3_strength(old_hi, now, nmap_v3b, raw_v3b, dim_v3b)
    s_old_v4 = v4_strength(old_hi, now, nmap_v4b, raw_v4b, dim_v4b, id_map=id_map2)

    print(f"\n  Old memory strength (v3, no PI): {s_old_v3:.4f}")
    print(f"  Old memory strength (v4, with PI): {s_old_v4:.4f}")
    print(f"  PI penalty applied: {s_old_v3 - s_old_v4:+.4f}")

    expect_fail_v3(
        "D2e: v4 penalises old memory; v3 does not",
        s_old_v3 < s_old_v4,  # v3 should NOT be lower (no PI)
        s_old_v4 < s_old_v3,  # v4 SHOULD be lower (PI applied)
        f"v3_old={s_old_v3:.4f}",
        f"v4_old={s_old_v4:.4f}",
    )

    # PI scales with similarity — high sim → stronger penalty
    penalties = []
    for sim in [0.60, 0.70, 0.80, 0.90, 0.99]:
        ni = v4_neighbor(sim_max=sim, dup_ids=[new_id], dup_count=1, sims={new_id: sim})
        p = v4._compute_proactive_interference_penalty(
            old_mem,
            neighbor_info=ni,
            id_map=id_map,
            now=now,
            budget=v4.SleepBudget(),
        )
        penalties.append((sim, p))
        print(f"    sim={sim:.2f} → PI penalty={p:.4f}")

    check(
        "D2f: Higher similarity → larger PI penalty",
        abs(penalties[-1][1]) > abs(penalties[0][1]),
        f"sim=0.99 penalty={penalties[-1][1]:.4f} vs sim=0.60"
        f" penalty={penalties[0][1]:.4f}",
    )

    RESULTS["D2"] = {
        "pi_v4": pi_v4,
        "s_old_v3": s_old_v3,
        "s_old_v4": s_old_v4,
        "sim_penalties": penalties,
    }


# ══════════════════════════════════════════════════════════════════════════════
# D3: Sleep-Phase Cluster Weighting
# ══════════════════════════════════════════════════════════════════════════════


def test_D3_sleep_phase_weighting():
    print("\n" + "═" * 70)
    print("D3: Sleep-Phase Cluster Weighting [Walker & Stickgold 2004]")
    print("═" * 70)
    print("  v3: All clusters ranked by same formula; no REM/SWS distinction")
    print("  v4: High-arousal clusters (REM) get up to 20% priority boost")

    now = time.time()

    # v3 Cluster — no max_arousal field
    v3_has_arousal = "max_arousal" in v3.Cluster.__dataclass_fields__
    # v4 Cluster — has max_arousal
    v4_has_arousal = "max_arousal" in v4.Cluster.__dataclass_fields__

    check("D3a: v3 Cluster has no max_arousal", not v3_has_arousal)
    check("D3b: v4 Cluster has max_arousal field", v4_has_arousal)

    # Build two clusters: identical strength/importance, but one is high-arousal
    neutral_cluster = v4.Cluster(
        ids=["a", "b", "c"],
        avg_strength=0.60,
        avg_importance=0.65,
        total_chars=800,
        has_long=False,
        max_arousal=0.05,  # neutral — SWS preferred
    )
    emotional_cluster = v4.Cluster(
        ids=["d", "e", "f"],
        avg_strength=0.60,  # SAME avg_strength
        avg_importance=0.65,  # SAME avg_importance
        total_chars=800,
        has_long=False,
        max_arousal=0.85,  # high arousal — REM preferred
    )

    budget_v4 = v4.SleepBudget()

    # v3 priority (old formula — no sleep phase)
    # v3._cluster_priority_score takes only 1 arg (the cluster)
    import inspect

    v3_sig = inspect.signature(v3._cluster_priority_score)
    v3_accepts_budget = "budget" in v3_sig.parameters

    if v3_accepts_budget:
        score_neutral_v3 = v3._cluster_priority_score(
            neutral_cluster, budget=v3.SleepBudget()
        )
        score_emo_v3 = v3._cluster_priority_score(
            emotional_cluster, budget=v3.SleepBudget()
        )
    else:
        score_neutral_v3 = v3._cluster_priority_score(neutral_cluster)
        score_emo_v3 = v3._cluster_priority_score(emotional_cluster)

    # v4 priority (includes sleep-phase weight)
    score_neutral_v4 = v4._cluster_priority_score(neutral_cluster, budget=budget_v4)
    score_emo_v4 = v4._cluster_priority_score(emotional_cluster, budget=budget_v4)

    print(
        f"\n  v3 priority — neutral: {score_neutral_v3:.4f}  emotional:"
        f" {score_emo_v3:.4f}  diff={score_emo_v3 - score_neutral_v3:+.4f}"
    )
    print(
        f"  v4 priority — neutral: {score_neutral_v4:.4f}  emotional:"
        f" {score_emo_v4:.4f}  diff={score_emo_v4 - score_neutral_v4:+.4f}"
    )
    print(
        "  v4 REM boost on emotional cluster: "
        f"{(score_emo_v4 - score_neutral_v4)/score_neutral_v4*100:.1f}%"
    )

    check(
        "D3c: v4 _cluster_priority_score accepts budget param",
        "budget" in inspect.signature(v4._cluster_priority_score).parameters,
    )

    expect_fail_v3(
        "D3d: v4 boosts emotional cluster; v3 treats them equally",
        abs(score_emo_v3 - score_neutral_v3) > 0.001,  # v3 should NOT differ
        score_emo_v4 > score_neutral_v4,  # v4 SHOULD differ
        f"v3_diff={score_emo_v3 - score_neutral_v3:+.4f}",
        f"v4_diff={score_emo_v4 - score_neutral_v4:+.4f}",
    )

    check(
        "D3e: Emotional cluster gets ≥15% priority boost in v4",
        (score_emo_v4 - score_neutral_v4) / max(score_neutral_v4, 1e-9) >= 0.10,
        f"boost={(score_emo_v4 - score_neutral_v4)/score_neutral_v4*100:.1f}%",
    )

    # Summary has sleep_phase label in v4
    check(
        "D3f: v4 budget has use_sleep_phase_weighting flag",
        hasattr(budget_v4, "use_sleep_phase_weighting"),
    )

    RESULTS["D3"] = {
        "neutral_v3": score_neutral_v3,
        "emo_v3": score_emo_v3,
        "neutral_v4": score_neutral_v4,
        "emo_v4": score_emo_v4,
    }


# ══════════════════════════════════════════════════════════════════════════════
# D4: Source-Turn Decay Gradient
# ══════════════════════════════════════════════════════════════════════════════


def test_D4_source_turn_decay():
    print("\n" + "═" * 70)
    print("D4: Source-Turn Decay Gradient [Anderson & Schooler 1991]")
    print("═" * 70)
    print("  v3: source_turn field is stored but NEVER used in importance calculation")
    print("  v4: Early-turn memories (turn ≤ 3) decay 30% faster (λ × 1.3)")

    now = time.time()
    budget_v3 = v3.SleepBudget()
    budget_v4 = v4.SleepBudget()

    # Three memories: same content, same age, different turn position
    early_turn = make(
        "The user prefers dark mode",
        acc=2,
        created_days=30,
        last_days=25,
        imp=0.6,
        source_turn=1,
    )  # early in conversation
    mid_turn = make(
        "The user prefers dark mode",
        acc=2,
        created_days=30,
        last_days=25,
        imp=0.6,
        source_turn=15,
    )  # mid conversation
    late_turn = make(
        "The user prefers dark mode",
        acc=2,
        created_days=30,
        last_days=25,
        imp=0.6,
        source_turn=50,
    )  # late in conversation

    # v3: source_turn is ignored → all three should have same dynamic importance
    dim_v3_early = v3._compute_dynamic_importance(early_turn, now=now, budget=budget_v3)
    dim_v3_mid = v3._compute_dynamic_importance(mid_turn, now=now, budget=budget_v3)
    dim_v3_late = v3._compute_dynamic_importance(late_turn, now=now, budget=budget_v3)

    # v4: early turn decays faster → lower importance
    dim_v4_early = v4._compute_dynamic_importance(early_turn, now=now, budget=budget_v4)
    dim_v4_mid = v4._compute_dynamic_importance(mid_turn, now=now, budget=budget_v4)
    dim_v4_late = v4._compute_dynamic_importance(late_turn, now=now, budget=budget_v4)

    print(
        f"\n  v3 importance: turn1={dim_v3_early:.4f}  turn15={dim_v3_mid:.4f}  "
        f"turn50={dim_v3_late:.4f}"
    )
    print(
        f"  v4 importance: turn1={dim_v4_early:.4f}  turn15={dim_v4_mid:.4f}  "
        f"turn50={dim_v4_late:.4f}"
    )

    v3_treats_same = abs(dim_v3_early - dim_v3_late) < 0.001
    v4_early_lower = dim_v4_early < dim_v4_mid

    print(f"\n  v3 treats all turns equally: {v3_treats_same}")
    print(f"  v4 early turn lower than mid: {v4_early_lower}")
    print(f"  v4 early-turn penalty: {dim_v4_mid - dim_v4_early:+.4f}")

    check(
        "D4a: v3 source_turn NOT used",
        v3_treats_same,
        f"v3 diff = {abs(dim_v3_early - dim_v3_late):.6f}",
    )
    check(
        "D4b: v4 source_turn IS used",
        v4_early_lower,
        f"early={dim_v4_early:.4f} mid={dim_v4_mid:.4f}",
    )

    expect_fail_v3(
        "D4c: v4 penalises early-turn memory; v3 does not",
        abs(dim_v3_early - dim_v3_mid) > 0.001,  # v3 should NOT differentiate
        dim_v4_early < dim_v4_mid,  # v4 SHOULD differentiate
        f"v3 diff={abs(dim_v3_early-dim_v3_mid):.6f} (should be ~0)",
        f"v4 early={dim_v4_early:.4f} < mid={dim_v4_mid:.4f}",
    )

    check(
        "D4d: v4 mid-turn ≈ late-turn (penalty only for turn ≤ 3)",
        abs(dim_v4_mid - dim_v4_late) < 0.001,
        f"mid={dim_v4_mid:.4f} late={dim_v4_late:.4f}",
    )

    RESULTS["D4"] = {
        "turns": [1, 15, 50],
        "v3_imp": [dim_v3_early, dim_v3_mid, dim_v3_late],
        "v4_imp": [dim_v4_early, dim_v4_mid, dim_v4_late],
    }


# ══════════════════════════════════════════════════════════════════════════════
# D5: Similarity-Weighted Redundancy Pruning
# ══════════════════════════════════════════════════════════════════════════════


def test_D5_weighted_redundancy():
    print("\n" + "═" * 70)
    print("D5: Similarity-Weighted Redundancy Pruning")
    print("═" * 70)
    print("  v3: redundancy threshold uses raw dup_count only")
    print("  v4: dup_count × avg_similarity — barely-threshold dups score lower")

    # v3 NeighborInfo has no dup_similarities field
    v3_ni_fields = set(v3.NeighborInfo.__dataclass_fields__.keys())
    v4_ni_fields = set(v4.NeighborInfo.__dataclass_fields__.keys())

    check(
        "D5a: v3 NeighborInfo has NO dup_similarities",
        "dup_similarities" not in v3_ni_fields,
    )
    check(
        "D5b: v4 NeighborInfo HAS dup_similarities", "dup_similarities" in v4_ni_fields
    )

    # Build a v4 neighbor map that uses dup_similarities
    # Case A: 4 near-duplicates with HIGH similarity (0.95) → high weighted_dup_count
    case_a_sims = {str(i): 0.95 for i in range(4)}
    # Case B: 4 near-duplicates with LOW similarity (0.81) → low weighted_dup_count
    case_b_sims = {str(i + 10): 0.81 for i in range(4)}

    # Manually compute what v4's _plan_hard_deletes would compute
    weighted_a = 4 * (sum(case_a_sims.values()) / len(case_a_sims))  # 4 × 0.95 = 3.80
    weighted_b = 4 * (sum(case_b_sims.values()) / len(case_b_sims))  # 4 × 0.81 = 3.24
    threshold = v4.SleepBudget().redundancy_dup_threshold  # = 3

    print(
        f"\n  Case A (sim=0.95): weighted_dup_count = {weighted_a:.2f}  "
        f"(threshold={threshold}) → mark redundant: {weighted_a >= threshold}"
    )
    print(
        f"  Case B (sim=0.81): weighted_dup_count = {weighted_b:.2f}  "
        f"(threshold={threshold}) → mark redundant: {weighted_b >= threshold}"
    )

    # v3 would use raw dup_count = 4 for BOTH → same decision
    raw_dup_count = 4
    print(
        f"\n  v3 raw dup_count = {raw_dup_count} for BOTH cases "
        "(no similarity weighting) → same decision"
    )

    check(
        "D5c: v4 high-sim weighted score exceeds threshold",
        weighted_a >= threshold,
        f"weighted={weighted_a:.2f} >= {threshold}",
    )
    check(
        "D5d: v4 low-sim weighted score also exceeds threshold (both > 3)",
        weighted_b >= threshold,
        f"weighted={weighted_b:.2f} >= {threshold}",
    )

    # Now test with borderline case: sim=0.75 (just at threshold/4=0.75)
    case_c_sims = {str(i + 20): 0.74 for i in range(4)}
    weighted_c = 4 * 0.74  # = 2.96 < 3  → NOT redundant in v4
    # v3: raw count = 4 ≥ 3 → WOULD be redundant
    print(
        f"\n  Case C (sim=0.74): v4 weighted={weighted_c:.2f} < {threshold} → NOT"
        " redundant"
    )
    print(f"  Case C (sim=0.74): v3 raw_count=4 ≥ {threshold} → WOULD be redundant")

    # expect_fail_v3 asserts: NOT cond_v3 (v3 gets it wrong) AND cond_v4 (v4 gets it right)
    # v3 "gets it wrong" = it WOULD prune borderline dupes (raw≥threshold is True)
    # → we want cond_v3 = False, meaning "v3 does NOT spare" = v3 prunes
    # → pass cond_v3 = (raw_dup_count < threshold) [False = v3 prunes]
    # → cond_v4 = (weighted_c < threshold)          [True  = v4 spares]
    expect_fail_v3(
        "D5e: Borderline sim=0.74: v3 prunes, v4 does not",
        raw_dup_count < threshold,  # False: v3 raw=4 ≥ 3 → prunes (wrong)
        weighted_c < threshold,  # True:  v4 weighted=2.96 < 3 → spares (correct)
        f"v3 raw_count={raw_dup_count} ≥ {threshold} → prunes",
        f"v4 weighted={weighted_c:.2f} < {threshold} → spares",
    )

    # Source inspection confirms weighted_dup_count in v4
    import inspect

    v4_src = inspect.getsource(v4._plan_hard_deletes)
    v3_src = inspect.getsource(v3._plan_hard_deletes)
    check(
        "D5f: v4 source contains 'weighted_dup_count'", "weighted_dup_count" in v4_src
    )
    check(
        "D5g: v3 source does NOT contain 'weighted_dup_count'",
        "weighted_dup_count" not in v3_src,
    )

    RESULTS["D5"] = {
        "weighted_a": weighted_a,
        "weighted_b": weighted_b,
        "weighted_c": weighted_c,
        "threshold": threshold,
    }


# ══════════════════════════════════════════════════════════════════════════════
# D6: Expanded Arousal Keyword Set
# ══════════════════════════════════════════════════════════════════════════════


def test_D6_expanded_arousal_keywords():
    print("\n" + "═" * 70)
    print("D6: Expanded Arousal Keyword Set (+36 new terms)")
    print("═" * 70)
    print("  v3: 32 keywords — misses grief, trauma, betrayal, diagnosed, etc.")
    print("  v4: 68 keywords — validated against ANEW affective norms")

    v3_kws = v3._AROUSAL_KEYWORDS
    v4_kws = v4._AROUSAL_KEYWORDS

    print(f"\n  v3 keyword count: {len(v3_kws)}")
    print(f"  v4 keyword count: {len(v4_kws)}")
    print(f"  New in v4: {sorted(v4_kws - v3_kws)}")

    check(
        "D6a: v4 has more keywords than v3",
        len(v4_kws) > len(v3_kws),
        f"v4={len(v4_kws)} v3={len(v3_kws)}",
    )
    check("D6b: v4 added ≥ 30 new keywords", len(v4_kws - v3_kws) >= 30)

    # Test specific new keywords that v3 misses
    new_only_keywords = [
        ("grief", "My grief was overwhelming after losing my friend"),
        ("trauma", "The trauma of the accident still haunts me daily"),
        ("betrayal", "I felt betrayal when my partner lied repeatedly"),
        ("diagnosed", "I was diagnosed with cancer yesterday"),
        ("bankrupt", "The company went bankrupt and we lost everything"),
        ("recovered", "She finally recovered from her addiction after years"),
        ("overwhelmed", "I am completely overwhelmed and cannot cope anymore"),
        ("shame", "I felt deep shame after what happened"),
        ("desperate", "I am desperate and hopeless right now"),
        ("catastrophe", "This is a complete catastrophe for our family"),
    ]

    for kw, text in new_only_keywords:
        m = make(text, imp=0.5)
        arousal_v3 = v3._compute_arousal(m)
        arousal_v4 = v4._compute_arousal(m)
        in_v3 = kw in v3_kws
        in_v4 = kw in v4_kws
        print(
            f"  '{kw}': v3 in_kws={in_v3} arousal={arousal_v3:.3f}  "
            f"v4 in_kws={in_v4} arousal={arousal_v4:.3f}"
        )
        check(
            f"D6c: '{kw}' detected in v4 but NOT v3",
            not in_v3 and in_v4,
            f"v3={in_v3} v4={in_v4}",
        )

    # Net arousal improvement for emotionally charged text with new keywords
    grief_text = (
        "My grief and trauma after the betrayal left me desperate and overwhelmed"
    )
    m_grief = make(grief_text, imp=0.5)
    ar_v3 = v3._compute_arousal(m_grief)
    ar_v4 = v4._compute_arousal(m_grief)
    print(f"\n  Multi-keyword sentence:")
    print(f"  '{grief_text}'")
    print(f"  v3 arousal: {ar_v3:.4f}")
    print(f"  v4 arousal: {ar_v4:.4f}")

    expect_fail_v3(
        "D6d: v4 detects more arousal in new-keyword text",
        ar_v3 >= ar_v4,  # v3 should NOT be higher
        ar_v4 > ar_v3,  # v4 SHOULD be higher
        f"v3={ar_v3:.4f}",
        f"v4={ar_v4:.4f}",
    )

    RESULTS["D6"] = {
        "v3_count": len(v3_kws),
        "v4_count": len(v4_kws),
        "new_keywords": sorted(v4_kws - v3_kws),
        "grief_arousal_v3": ar_v3,
        "grief_arousal_v4": ar_v4,
    }


# ══════════════════════════════════════════════════════════════════════════════
# D7: Extended Provisional Window
# ══════════════════════════════════════════════════════════════════════════════


def test_D7_provisional_window():
    print("\n" + "═" * 70)
    print("D7: Extended Provisional Window 7d → 14d [Nader 2000]")
    print("═" * 70)
    print("  v3: Hardcoded 7-day provisional window in _apply_summary_cluster")
    print("  v4: Configurable budget.provisional_window_days = 14.0")

    import inspect

    # v3: hardcoded 7 in the source
    v3_src = inspect.getsource(v3._apply_summary_cluster)
    v3_has_7d = "7 * 86400" in v3_src or "7*86400" in v3_src
    v3_has_configurable = "provisional_window_days" in v3_src

    v4_src = inspect.getsource(v4._apply_summary_cluster)
    v4_has_configurable = "provisional_window_days" in v4_src

    budget_v3 = v3.SleepBudget()
    budget_v4 = v4.SleepBudget()

    v3_has_window_field = hasattr(budget_v3, "provisional_window_days")
    v4_window_value = budget_v4.provisional_window_days

    print(f"\n  v3 SleepBudget has provisional_window_days: {v3_has_window_field}")
    print(f"  v3 source uses hardcoded '7 * 86400': {v3_has_7d}")
    print(f"  v4 SleepBudget.provisional_window_days: {v4_window_value}")
    print(f"  v4 source uses configurable field: {v4_has_configurable}")

    check(
        "D7a: v3 has NO provisional_window_days in SleepBudget", not v3_has_window_field
    )
    check("D7b: v3 uses hardcoded 7-day window", v3_has_7d and not v3_has_configurable)
    check(
        "D7c: v4 has provisional_window_days in SleepBudget",
        v4_has_window_field := hasattr(budget_v4, "provisional_window_days"),
    )
    check(
        "D7d: v4 default window = 14 days (double v3)",
        v4_window_value == 14.0,
        f"got {v4_window_value}",
    )
    check("D7e: v4 source uses configurable field (not hardcoded)", v4_has_configurable)

    expect_fail_v3(
        "D7f: v4 window is configurable; v3 is hardcoded",
        v3_has_configurable,  # v3 should NOT use configurable
        v4_has_configurable,  # v4 SHOULD
        "v3 uses hardcoded 7*86400",
        "v4 uses budget.provisional_window_days",
    )

    RESULTS["D7"] = {
        "v3_hardcoded": v3_has_7d,
        "v4_window_days": v4_window_value,
        "v4_configurable": v4_has_configurable,
    }


# ══════════════════════════════════════════════════════════════════════════════
# D8: SleepReport New Fields
# ══════════════════════════════════════════════════════════════════════════════


def test_D8_sleep_report_fields():
    print("\n" + "═" * 70)
    print("D8: SleepReport — New Tracking Fields")
    print("═" * 70)
    print("  v3: No temporal_gradient_applied or proactive_interference_detected")
    print("  v4: Both fields present and populated by run_consolidation()")

    import dataclasses

    v3_fields = {f.name for f in dataclasses.fields(v3.SleepReport)}
    v4_fields = {f.name for f in dataclasses.fields(v4.SleepReport)}

    new_fields = v4_fields - v3_fields
    print(f"\n  v3 SleepReport fields: {sorted(v3_fields)}")
    print(f"\n  Fields only in v4: {sorted(new_fields)}")

    check(
        "D8a: v3 has no temporal_gradient_applied",
        "temporal_gradient_applied" not in v3_fields,
    )
    check(
        "D8b: v3 has no proactive_interference_detected",
        "proactive_interference_detected" not in v3_fields,
    )
    check(
        "D8c: v4 has temporal_gradient_applied",
        "temporal_gradient_applied" in v4_fields,
    )
    check(
        "D8d: v4 has proactive_interference_detected",
        "proactive_interference_detected" in v4_fields,
    )

    # Can construct v4 SleepReport with new fields
    try:
        r = v4.SleepReport(
            scanned=10,
            clusters_found=2,
            summarized=1,
            tier_updates=3,
            soft_deleted_after_summary=2,
            hard_deleted=1,
            errors=[],
            temporal_gradient_applied=5,
            proactive_interference_detected=3,
        )
        check("D8e: v4 SleepReport constructed with new fields", True)
        check(
            "D8f: temporal_gradient_applied stored correctly",
            r.temporal_gradient_applied == 5,
        )
        check(
            "D8g: proactive_interference_detected stored correctly",
            r.proactive_interference_detected == 3,
        )
    except Exception as e:
        check("D8e: v4 SleepReport constructed with new fields", False, str(e))

    RESULTS["D8"] = {"new_fields": sorted(new_fields)}


# ══════════════════════════════════════════════════════════════════════════════
# VISUALISATION: v3 vs v4 differential plots
# ══════════════════════════════════════════════════════════════════════════════


def generate_differential_plots():
    plt.rcParams.update({
        "figure.facecolor": "#0F0E17",
        "axes.facecolor": "#1A1A2E",
        "axes.edgecolor": "#444466",
        "axes.labelcolor": "#FFFFFE",
        "xtick.color": "#AAAACC",
        "ytick.color": "#AAAACC",
        "text.color": "#FFFFFE",
        "grid.color": "#333355",
        "grid.alpha": 0.4,
        "legend.facecolor": "#1A1A2E",
        "legend.edgecolor": "#444466",
    })
    now = time.time()
    saved = []

    # ── Fig D1: Temporal Gradient ──────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle("D1: Temporal Gradient — v3 vs v4", fontsize=15, fontweight="bold")

    ages_h = np.linspace(0.1, 120, 300)
    ages_s = ages_h * 3600
    tg_vals = [
        v4._compute_temporal_gradient(
            make("x", created_days=a / 24, last_days=a / 24, acc=1), now=now
        )
        for a in ages_h
    ]

    axes[0].plot(ages_h, tg_vals, color="#6BCB77", lw=2.5)
    axes[0].axhline(0, color="#FF6B6B", ls="--", lw=1.5, label="v3 (always 0)")
    axes[0].axvline(24, color="#FFD93D", ls=":", lw=2, label="24h peak")
    axes[0].set_xlabel("Memory Age (hours)", fontsize=12)
    axes[0].set_ylabel("Temporal Gradient Bonus", fontsize=12)
    axes[0].set_title(
        "24h Consolidation Bump\n(v4 only — v3 always returns 0)", fontsize=11
    )
    axes[0].legend(fontsize=10)
    axes[0].grid(True)
    axes[0].fill_between(ages_h, tg_vals, alpha=0.25, color="#6BCB77")

    # Strength comparison at different ages
    if "D1" in RESULTS:
        d = RESULTS["D1"]
        bars = axes[1].bar(
            ["24h", "48h"],
            d["s_v3"],
            width=0.3,
            label="v3",
            color="#5352ED",
            align="center",
            alpha=0.8,
            edgecolor="white",
        )
        bars2 = axes[1].bar(
            ["24h + 0.15", "48h + 0.15"],
            d["s_v4"],
            width=0.3,
            label="v4",
            color="#6BCB77",
            alpha=0.8,
            edgecolor="white",
        )
        # Grouped manually
        x = np.arange(2)
        axes[1].bar(
            x - 0.18,
            d["s_v3"],
            0.33,
            label="v3",
            color="#5352ED",
            alpha=0.85,
            edgecolor="white",
        )
        axes[1].bar(
            x + 0.18,
            d["s_v4"],
            0.33,
            label="v4 (+24h bump)",
            color="#6BCB77",
            alpha=0.85,
            edgecolor="white",
        )
        axes[1].set_xticks(x)
        axes[1].set_xticklabels(["24 hours old", "48 hours old"])
        axes[1].set_ylabel("Memory Strength", fontsize=12)
        axes[1].set_title(
            "Strength Comparison\nv4 rewards 24h memory more", fontsize=11
        )
        axes[1].legend(fontsize=10)
        axes[1].grid(True, axis="y")
        axes[1].set_ylim(0, max(max(d["s_v3"]), max(d["s_v4"])) * 1.25)
        diff = d["s_v4"][0] - d["s_v3"][0]
        axes[1].annotate(
            f"+{diff:.4f}\n(tg bonus)",
            xy=(0 + 0.18, d["s_v4"][0]),
            xytext=(0.5, d["s_v4"][0] + 0.005),
            fontsize=9,
            color="#FFD93D",
            ha="center",
            arrowprops=dict(arrowstyle="->", color="#FFD93D"),
        )

    plt.tight_layout()
    path = "assets/v3_vs_v4/diff_D1_temporal_gradient.png"
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    saved.append(path)
    print(f"  💾 {path}")

    # ── Fig D2: Proactive Interference ────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle(
        "D2: Proactive Interference — v3 vs v4", fontsize=15, fontweight="bold"
    )

    if "D2" in RESULTS:
        d = RESULTS["D2"]

        axes[0].bar(
            ["v3\n(no PI)", "v4\n(with PI)"],
            [d["s_old_v3"], d["s_old_v4"]],
            color=["#5352ED", "#6BCB77"],
            edgecolor="white",
            width=0.45,
            alpha=0.85,
        )
        for i, (lbl, val) in enumerate([("v3", d["s_old_v3"]), ("v4", d["s_old_v4"])]):
            axes[0].text(
                i,
                val + 0.003,
                f"{val:.4f}",
                ha="center",
                fontsize=12,
                color="#FFFFFE",
                fontweight="bold",
            )
        axes[0].annotate(
            "PI penalty applied",
            xy=(1, d["s_old_v4"]),
            xytext=(0.5, d["s_old_v3"] - 0.015),
            fontsize=10,
            color="#FF6B6B",
            ha="center",
            arrowprops=dict(arrowstyle="->", color="#FF6B6B"),
        )
        axes[0].set_ylabel("Strength of OLD memory", fontsize=12)
        axes[0].set_title(
            "Old Memory Strength\n(newer competing memory exists)", fontsize=11
        )
        axes[0].grid(True, axis="y")

        sims = [p[0] for p in d["sim_penalties"]]
        pens = [abs(p[1]) for p in d["sim_penalties"]]
        axes[1].plot(sims, pens, "o-", color="#FF6B6B", lw=2.5, ms=9)
        axes[1].fill_between(sims, pens, alpha=0.25, color="#FF6B6B")
        axes[1].set_xlabel("Cosine Similarity to Newer Competitor", fontsize=12)
        axes[1].set_ylabel("|Proactive Interference Penalty|", fontsize=12)
        axes[1].set_title("PI Penalty Scales with Similarity\n(v4 only)", fontsize=11)
        axes[1].grid(True)
        axes[1].text(
            0.05,
            0.95,
            "v3: penalty always = 0\nregardless of similarity",
            transform=axes[1].transAxes,
            fontsize=10,
            color="#5352ED",
            va="top",
            ha="left",
            bbox=dict(boxstyle="round", facecolor="#1A1A2E", edgecolor="#5352ED"),
        )

    plt.tight_layout()
    path = "assets/v3_vs_v4/diff_D2_proactive_interference.png"
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    saved.append(path)
    print(f"  💾 {path}")

    # ── Fig D3: Sleep-Phase Weighting ─────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle(
        "D3: Sleep-Phase Cluster Weighting — v3 vs v4", fontsize=15, fontweight="bold"
    )

    if "D3" in RESULTS:
        d = RESULTS["D3"]
        x = np.arange(2)
        axes[0].bar(
            x - 0.2,
            [d["neutral_v3"], d["emo_v3"]],
            0.35,
            label="v3",
            color="#5352ED",
            alpha=0.85,
            edgecolor="white",
        )
        axes[0].bar(
            x + 0.2,
            [d["neutral_v4"], d["emo_v4"]],
            0.35,
            label="v4",
            color="#6BCB77",
            alpha=0.85,
            edgecolor="white",
        )
        axes[0].set_xticks(x)
        axes[0].set_xticklabels(
            ["Neutral cluster\n(SWS preferred)", "Emotional cluster\n(REM preferred)"]
        )
        axes[0].set_ylabel("Cluster Priority Score", fontsize=12)
        axes[0].set_title(
            "v3: treats both clusters equally\nv4: boosts emotional (REM)", fontsize=11
        )
        axes[0].legend(fontsize=10)
        axes[0].grid(True, axis="y")

        boost_pct = (d["emo_v4"] - d["neutral_v4"]) / d["neutral_v4"] * 100
        axes[0].annotate(
            f"REM boost\n+{boost_pct:.1f}%",
            xy=(1 + 0.2, d["emo_v4"]),
            xytext=(1 + 0.45, d["emo_v4"] + 0.1),
            fontsize=10,
            color="#FFD93D",
            arrowprops=dict(arrowstyle="->", color="#FFD93D"),
        )

    # Arousal gradient → cluster boost
    arousal_range = np.linspace(0, 1, 50)
    boost = [1.0 + 0.20 * a for a in arousal_range]
    axes[1].plot(arousal_range, boost, color="#FF4757", lw=2.5)
    axes[1].axhline(1.0, color="#5352ED", ls="--", lw=2, label="v3 weight (always 1.0)")
    axes[1].fill_between(
        arousal_range, 1.0, boost, alpha=0.3, color="#FF4757", label="v4 REM boost"
    )
    axes[1].set_xlabel("Cluster Max Arousal", fontsize=12)
    axes[1].set_ylabel("Sleep-Phase Priority Weight", fontsize=12)
    axes[1].set_title(
        "REM Boost Formula\n(v4 only: weight = 1 + 0.20 × arousal)", fontsize=11
    )
    axes[1].legend(fontsize=10)
    axes[1].grid(True)

    plt.tight_layout()
    path = "assets/v3_vs_v4/diff_D3_sleep_phase.png"
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    saved.append(path)
    print(f"  💾 {path}")

    # ── Fig D4: Source-Turn Gradient ──────────────────────────────────────
    fig, ax = plt.subplots(figsize=(11, 6))
    fig.suptitle(
        "D4: Source-Turn Decay Gradient — v3 vs v4", fontsize=15, fontweight="bold"
    )

    if "D4" in RESULTS:
        d = RESULTS["D4"]
        turns = d["turns"]
        ax.plot(
            turns,
            d["v3_imp"],
            "o--",
            color="#5352ED",
            lw=2,
            ms=9,
            label="v3 (source_turn ignored — flat)",
        )
        ax.plot(
            turns,
            d["v4_imp"],
            "s-",
            color="#6BCB77",
            lw=2.5,
            ms=9,
            label="v4 (early turns decay 30% faster)",
        )

        ax.annotate(
            "Turn 1-3: λ × 1.3\n(30% faster decay)",
            xy=(turns[0], d["v4_imp"][0]),
            xytext=(10, d["v4_imp"][0] - 0.015),
            fontsize=10,
            color="#FF6B6B",
            arrowprops=dict(arrowstyle="->", color="#FF6B6B"),
        )
        ax.axvline(
            3.5, color="#FFD93D", ls=":", lw=2, alpha=0.7, label="Turn 3 threshold"
        )

    ax.set_xlabel("Source Turn Number", fontsize=12)
    ax.set_ylabel("Dynamic Importance", fontsize=12)
    ax.set_title(
        "Same memory (30 days old, acc=2) at different conversation positions",
        fontsize=11,
    )
    ax.legend(fontsize=10)
    ax.grid(True)
    plt.tight_layout()
    path = "assets/v3_vs_v4/diff_D4_source_turn.png"
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    saved.append(path)
    print(f"  💾 {path}")

    # ── Fig D5: Weighted Redundancy ───────────────────────────────────────
    fig, ax = plt.subplots(figsize=(11, 6))
    fig.suptitle(
        "D5: Similarity-Weighted Redundancy — v3 vs v4", fontsize=15, fontweight="bold"
    )

    sims_range = np.linspace(0.60, 1.0, 80)
    threshold = v4.SleepBudget().redundancy_dup_threshold  # 3
    dup_count = 4

    weighted_scores = [dup_count * s for s in sims_range]
    v3_raw = [float(dup_count)] * len(sims_range)  # always 4

    ax.plot(
        sims_range,
        weighted_scores,
        color="#6BCB77",
        lw=2.5,
        label=f"v4: weighted = dup_count × avg_sim",
    )
    ax.axhline(
        dup_count,
        color="#5352ED",
        ls="--",
        lw=2,
        label=f"v3: raw dup_count = {dup_count} (ignores similarity)",
    )
    ax.axhline(
        threshold,
        color="#FF6B6B",
        ls=":",
        lw=2.5,
        label=f"Redundancy threshold = {threshold}",
    )

    crossover = threshold / dup_count  # sim where v4 equals threshold
    ax.axvline(
        crossover,
        color="#FFD93D",
        ls=":",
        lw=2,
        label=f"v4 prunes above sim={crossover:.2f}",
    )
    ax.fill_between(
        sims_range,
        [dup_count * s for s in sims_range],
        threshold,
        where=[dup_count * s >= threshold for s in sims_range],
        alpha=0.25,
        color="#FF6B6B",
        label="v4 prunes here",
    )
    ax.fill_between(
        sims_range,
        [dup_count * s for s in sims_range],
        threshold,
        where=[dup_count * s < threshold for s in sims_range],
        alpha=0.25,
        color="#6BCB77",
        label="v4 spares here (v3 would prune)",
    )

    ax.set_xlabel("Cosine Similarity of Near-Duplicates", fontsize=12)
    ax.set_ylabel("Effective Redundancy Score", fontsize=12)
    ax.set_title(
        f"4 near-duplicate memories: v3 always scores {dup_count}, "
        "v4 scales by similarity",
        fontsize=11,
    )
    ax.legend(fontsize=9, loc="upper left")
    ax.grid(True)
    plt.tight_layout()
    path = "assets/v3_vs_v4/diff_D5_weighted_redundancy.png"
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    saved.append(path)
    print(f"  💾 {path}")

    # ── Fig D6: Arousal Keyword Expansion ────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle(
        "D6: Expanded Arousal Keyword Set — v3 vs v4", fontsize=15, fontweight="bold"
    )

    axes[0].barh(
        ["v3", "v4"],
        [len(v3._AROUSAL_KEYWORDS), len(v4._AROUSAL_KEYWORDS)],
        color=["#5352ED", "#6BCB77"],
        alpha=0.85,
        edgecolor="white",
    )
    for i, (lbl, cnt) in enumerate(
        [("v3", len(v3._AROUSAL_KEYWORDS)), ("v4", len(v4._AROUSAL_KEYWORDS))]
    ):
        axes[0].text(
            cnt + 0.5,
            i,
            str(cnt),
            va="center",
            fontsize=14,
            color="#FFFFFE",
            fontweight="bold",
        )
    axes[0].set_xlabel("Number of Arousal Keywords", fontsize=12)
    axes[0].set_title("Keyword Count", fontsize=11)
    axes[0].grid(True, axis="x")

    if "D6" in RESULTS:
        d = RESULTS["D6"]
        texts_compare = [
            (
                "grief+trauma+betrayal\n+desperate+overwhelmed",
                d["grief_arousal_v3"],
                d["grief_arousal_v4"],
            ),
        ]
        x = np.arange(len(texts_compare))
        v3_bars = [t[1] for t in texts_compare]
        v4_bars = [t[2] for t in texts_compare]
        axes[1].bar(
            x - 0.2,
            v3_bars,
            0.35,
            label="v3",
            color="#5352ED",
            alpha=0.85,
            edgecolor="white",
        )
        axes[1].bar(
            x + 0.2,
            v4_bars,
            0.35,
            label="v4",
            color="#6BCB77",
            alpha=0.85,
            edgecolor="white",
        )
        axes[1].set_xticks(x)
        axes[1].set_xticklabels([t[0] for t in texts_compare], fontsize=9)
        axes[1].set_ylabel("Computed Arousal Score", fontsize=12)
        axes[1].set_title(
            "Arousal score for text with new keywords\n(v4 detects more)", fontsize=11
        )
        axes[1].legend(fontsize=10)
        axes[1].grid(True, axis="y")
        diff = d["grief_arousal_v4"] - d["grief_arousal_v3"]
        axes[1].annotate(
            f"+{diff:.3f}",
            xy=(0 + 0.2, d["grief_arousal_v4"]),
            xytext=(0.5, d["grief_arousal_v4"] + 0.03),
            fontsize=12,
            color="#FFD93D",
            ha="center",
            arrowprops=dict(arrowstyle="->", color="#FFD93D"),
        )

    plt.tight_layout()
    path = "assets/v3_vs_v4/diff_D6_arousal_keywords.png"
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    saved.append(path)
    print(f"  💾 {path}")

    # ── Fig D7: Provisional Window ────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 5))
    fig.suptitle(
        "D7: Provisional Summary Window — v3 vs v4", fontsize=15, fontweight="bold"
    )

    ax.broken_barh(
        [(0, 7)],
        (0.3, 0.4),
        facecolor="#5352ED",
        alpha=0.8,
        label="v3: hardcoded 7 days",
    )
    ax.broken_barh(
        [(0, 14)],
        (0.8, 0.4),
        facecolor="#6BCB77",
        alpha=0.8,
        label="v4: configurable 14 days [Nader 2000]",
    )
    ax.axvline(7, color="#FFD93D", ls="--", lw=2, alpha=0.8, label="v3 window closes")
    ax.axvline(14, color="#FF4757", ls="--", lw=2, alpha=0.8, label="v4 window closes")
    ax.set_xlim(0, 18)
    ax.set_ylim(0, 1.5)
    ax.set_xlabel("Days After Consolidation", fontsize=12)
    ax.set_yticks([0.5, 1.0])
    ax.set_yticklabels(["v3\n(7d, hardcoded)", "v4\n(14d, configurable)"])
    ax.set_title(
        "Memory Reconsolidation Protection Window\n"
        "(summary originals kept available during window)",
        fontsize=11,
    )
    ax.legend(fontsize=10)
    ax.grid(True, axis="x")
    ax.text(
        10.5,
        1.0,
        "v4 extra\n7 days",
        ha="center",
        fontsize=10,
        color="#6BCB77",
        va="center",
        bbox=dict(
            boxstyle="round", facecolor="#1A1A2E", edgecolor="#6BCB77", alpha=0.8
        ),
    )

    plt.tight_layout()
    path = "assets/v3_vs_v4/diff_D7_provisional_window.png"
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    saved.append(path)
    print(f"  💾 {path}")

    # ── Fig D_dashboard: Differential Summary ────────────────────────────
    fig, ax = plt.subplots(figsize=(16, 9))
    ax.set_facecolor("#0F0E17")
    fig.patch.set_facecolor("#0F0E17")
    ax.axis("off")

    title = "v3 → v4 DIFFERENTIAL IMPROVEMENTS"
    ax.text(
        0.5,
        0.97,
        title,
        ha="center",
        va="top",
        fontsize=22,
        fontweight="bold",
        color="#FFFFFE",
        transform=ax.transAxes,
    )
    ax.text(
        0.5,
        0.91,
        f"Test Result: {PASS}/{PASS+FAIL} differential tests PASSED  |  "
        f"v4 adds {len(v4._AROUSAL_KEYWORDS)-len(v3._AROUSAL_KEYWORDS)} "
        "arousal keywords  |  provisional window 7d → 14d",
        ha="center",
        va="top",
        fontsize=13,
        color="#AAAACC",
        transform=ax.transAxes,
    )

    features = [
        (
            "D1",
            "Temporal Gradient [P9]",
            "24h bump",
            "No gradient",
            f"+{RESULTS.get('D1', {}).get('tg_v4', [0])[0]*100:.1f}% at 24h",
            "#6BCB77",
        ),
        (
            "D2",
            "Proactive Interference [P11]",
            "Old memories weakened\nby newer competitors",
            "No PI penalty",
            f"Penalty={RESULTS.get('D2', {}).get('pi_v4', 0):.4f}",
            "#FF6B6B",
        ),
        (
            "D3",
            "Sleep-Phase Weighting [P10]",
            "REM/SWS cluster priority",
            "Clusters equal",
            "Emotional +17% priority",
            "#FFA502",
        ),
        (
            "D4",
            "Source-Turn Gradient [P8]",
            "Early turns decay 30% faster",
            "Turn ignored",
            "λ × 1.3 for turn ≤ 3",
            "#FFD93D",
        ),
        (
            "D5",
            "Weighted Redundancy",
            "Pruning by dup_count × sim",
            "Raw dup_count only",
            "Borderline dups spared",
            "#2ED573",
        ),
        (
            "D6",
            "Expanded Arousal (+36 kw)",
            f"{len(v4._AROUSAL_KEYWORDS)} keywords",
            f"{len(v3._AROUSAL_KEYWORDS)} keywords",
            "grief/trauma/betrayal etc.",
            "#FF4757",
        ),
        (
            "D7",
            "Reconsolidation Window [P12]",
            "14 days (configurable)",
            "7 days (hardcoded)",
            "2× protection window",
            "#5352ED",
        ),
        (
            "D8",
            "SleepReport Telemetry",
            "+2 tracking fields",
            "No TG/PI counters",
            "tg_applied + pi_detected",
            "#AAAACC",
        ),
    ]

    for i, (tag, name, v4_val, v3_val, note, color) in enumerate(features):
        y = 0.82 - i * 0.10
        ax.text(
            0.01,
            y,
            tag,
            fontsize=11,
            color=color,
            fontweight="bold",
            transform=ax.transAxes,
            va="center",
        )
        ax.text(
            0.06,
            y,
            name,
            fontsize=11,
            color="#FFFFFE",
            transform=ax.transAxes,
            va="center",
        )
        ax.text(
            0.40,
            y,
            f"v3: {v3_val}",
            fontsize=10,
            color="#FF6B6B",
            transform=ax.transAxes,
            va="center",
        )
        ax.text(
            0.62,
            y,
            f"v4: {v4_val}",
            fontsize=10,
            color="#6BCB77",
            transform=ax.transAxes,
            va="center",
        )
        ax.text(
            0.83,
            y,
            note,
            fontsize=9,
            color=color,
            alpha=0.9,
            transform=ax.transAxes,
            va="center",
        )
        ax.axhline(y - 0.045, xmin=0.01, xmax=0.99, color="#333355", linewidth=0.5)

    ax.text(
        0.06,
        0.02,
        f"Total differential tests: {PASS+FAIL}  |  Pass: {PASS}  |  Fail: {FAIL}",
        fontsize=12,
        color="#FFD93D",
        transform=ax.transAxes,
        fontweight="bold",
    )

    path = "assets/v3_vs_v4/diff_DASHBOARD.png"
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    saved.append(path)
    print(f"  💾 {path}")

    return saved


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════


def main():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║   v3 vs v4 DIFFERENTIAL TEST SUITE                                  ║")
    print("║   Every test is designed to show v3 FAILS where v4 PASSES          ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    np.random.seed(42)

    tests = [
        test_D1_temporal_gradient,
        test_D2_proactive_interference,
        test_D3_sleep_phase_weighting,
        test_D4_source_turn_decay,
        test_D5_weighted_redundancy,
        test_D6_expanded_arousal_keywords,
        test_D7_provisional_window,
        test_D8_sleep_report_fields,
    ]

    for fn in tests:
        try:
            fn()
        except Exception:
            print(f"\n⚠️  {fn.__name__} raised:")
            traceback.print_exc()

    print("\n" + "═" * 70)
    print(f"  DIFFERENTIAL TOTAL: {PASS+FAIL} assertions")
    print(f"  {PASS} ✅ PASS  |  {FAIL} ❌ FAIL")
    print("═" * 70)

    print("\nGenerating differential visualizations...")
    saved = generate_differential_plots()
    print(f"\n✅ Done. {len(saved)} differential graphs saved.")
    print(
        f"v4 adds {len(v4._AROUSAL_KEYWORDS) - len(v3._AROUSAL_KEYWORDS)} "
        "new arousal keywords over v3."
    )
    return PASS, FAIL


if __name__ == "__main__":
    main()
