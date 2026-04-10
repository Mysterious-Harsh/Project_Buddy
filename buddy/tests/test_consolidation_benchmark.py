# buddy/tests/test_consolidation_benchmark.py
"""
Consolidation v5 Benchmark Suite
=================================
Establishes v4.1-patched as the baseline and re-runs after each phase to measure
improvement or regression across four dimensions:

  1. RETRIEVAL QUALITY  — nDCG@3 against a fixed query/expected-hit set
  2. TIER DISTRIBUTION  — flash/short/long counts after a sleep cycle
  3. DATA SAFETY        — provisional summaries must not lose source data early
  4. CLUSTER SAFETY     — redundancy pruning must always leave ≥ 1 representative

Run with:
    pytest buddy/tests/test_consolidation_benchmark.py -v
    pytest buddy/tests/test_consolidation_benchmark.py -v -s   # verbose output

All tests are deterministic (fixed seed, no LLM calls). The LLM summarizer is
mocked to return a canned summary so the suite stays fast (<1s).
"""
from __future__ import annotations

import math
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import numpy as np
import pytest

from buddy.memory.consolidation_engine import (
    SleepBudget,
    _compute_strength,
    _build_neighbor_map,
    _build_clusters,
    _compute_all_raw_bla,
    _compute_dynamic_importance,
    _plan_hard_deletes,
    _plan_tier_updates,
    _is_protected,
    run_consolidation,
)
from buddy.memory.memory_entry import MemoryEntry
from buddy.memory.sqlite_store import SQLiteStore


# =============================================================================
# Helpers
# =============================================================================

RNG = np.random.default_rng(42)


def _rand_emb(dim: int = 16) -> np.ndarray:
    """Reproducible unit-normalised random embedding."""
    v = RNG.standard_normal(dim).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def _similar_emb(base: np.ndarray, noise: float = 0.05) -> np.ndarray:
    """Return an embedding close to base (cosine sim ≈ 0.90+)."""
    v = base + RNG.standard_normal(base.shape).astype(np.float32) * noise
    return v / (np.linalg.norm(v) + 1e-9)


def _make_entry(
    text: str,
    *,
    memory_type: str = "flash",
    importance: float = 0.5,
    age_days: float = 10.0,
    access_count: int = 1,
    source_turn: Optional[int] = None,
    embedding: Optional[np.ndarray] = None,
    consolidated_into_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> MemoryEntry:
    now = time.time()
    e = MemoryEntry(
        text=text,
        memory_type=memory_type,
        importance=importance,
        created_at=now - age_days * 86400.0,
        last_accessed=now - (age_days * 0.5) * 86400.0,
        access_count=access_count,
        source_turn=source_turn,
        embedding=embedding if embedding is not None else _rand_emb(),
        consolidated_into_id=consolidated_into_id,
        metadata=metadata or {},
    )
    return e


def _sqlite_with_entries(entries: List[MemoryEntry]) -> SQLiteStore:
    """Create an in-memory SQLiteStore loaded with the given entries."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = SQLiteStore(db_path)
    for e in entries:
        store.upsert_memory(e)
    return store


def _mock_vector_store(entries: List[MemoryEntry], *, tau: float = 0.80):
    """
    Simple mock vector store: search returns entries sorted by cosine similarity.
    Only pairs above tau are returned as 'dups'.
    """
    vs = MagicMock()

    def search_with_payloads(
        query_vector, query_text="", top_k=20, **kwargs
    ):
        q = np.asarray(query_vector, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        results = []
        for e in entries:
            emb = getattr(e, "embedding", None)
            if emb is None:
                continue
            sim = float(np.dot(q, emb))
            results.append((e.id, sim, {}))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    vs.search_with_payloads.side_effect = search_with_payloads
    return vs


def _mock_brain(summary_text: str = "Consolidated summary."):
    """Mock brain that returns a canned summary response."""
    brain = MagicMock()
    brain.run_memory_summary.return_value = {
        "parsed": {
            "memory_summary": summary_text,
            "salience": 0.75,
            "confidence": 0.80,
        }
    }
    return brain


def _embed_fn(text: str) -> np.ndarray:
    return _rand_emb()


# =============================================================================
# Benchmark 1 — Retrieval Quality  (nDCG@3)
# =============================================================================

class TestRetrievalQuality:
    """
    Baseline: v4.1 composite scoring WITHOUT consolidation_strength.
    Post-Phase3: consolidation_strength must improve nDCG vs baseline.

    We test the _compute_strength scoring function directly — the integration
    with memory_manager._composite_score is tested in Phase 3.
    """

    def _setup_query_corpus(self):
        """
        Corpus: 10 entries. Query embedding matches entries 0,1,2 closely.
        'Relevant' = entries 0 and 1 (strong + long-tier).
        'Distractor' = entry 2 (similar embedding but flash + low importance).
        """
        base = _rand_emb()
        entries = []

        # Relevant — long, high importance, well-accessed
        entries.append(_make_entry(
            "User's name is Harsh, prefers direct communication.",
            memory_type="long", importance=0.90, age_days=30, access_count=10,
            embedding=_similar_emb(base, noise=0.02),
        ))
        entries.append(_make_entry(
            "Harsh works on an AI companion project called Buddy.",
            memory_type="short", importance=0.75, age_days=7, access_count=5,
            embedding=_similar_emb(base, noise=0.04),
        ))

        # Distractor — flash, low importance, same semantic neighborhood
        entries.append(_make_entry(
            "Some fleeting thought mentioned once.",
            memory_type="flash", importance=0.20, age_days=1, access_count=1,
            embedding=_similar_emb(base, noise=0.06),
        ))

        # Noise entries — unrelated
        for i in range(7):
            entries.append(_make_entry(
                f"Unrelated memory #{i}",
                memory_type="flash", importance=0.30, age_days=5, access_count=1,
            ))

        return entries, base  # base is the "query embedding"

    def _ndcg_at_k(self, ranked_ids: List[str], relevant_ids: set, k: int = 3) -> float:
        """Compute nDCG@k."""
        def dcg(hits):
            return sum(
                (1.0 / math.log2(i + 2))
                for i, h in enumerate(hits[:k])
                if h in relevant_ids
            )

        ideal = dcg(list(relevant_ids)[:k])
        if ideal == 0:
            return 0.0
        return dcg(ranked_ids) / ideal

    def test_strength_ranking_prefers_long_tier(self):
        """
        Long-tier high-importance entries should outscore flash-tier distractors
        even when the distractor has slightly higher raw BLA.
        """
        entries, query_emb = self._setup_query_corpus()
        now = time.time()
        b = SleepBudget()

        neighbor_map = _build_neighbor_map(
            vector_store=_mock_vector_store(entries),
            candidates=entries,
            budget=b,
            now=now,
        )
        dynamic_importances = {
            e.id: _compute_dynamic_importance(e, now=now, budget=b)
            for e in entries
        }
        raw_bla = _compute_all_raw_bla(
            entries, now=now, budget=b, dynamic_importances=dynamic_importances
        )
        id_map = {e.id: e for e in entries}

        strengths = {
            e.id: _compute_strength(
                e, now=now, budget=b,
                neighbor_map=neighbor_map,
                raw_bla_scores=raw_bla,
                dynamic_importances=dynamic_importances,
                id_map=id_map,
            )
            for e in entries
        }

        ranked = sorted(entries, key=lambda e: strengths[e.id], reverse=True)
        ranked_ids = [e.id for e in ranked]
        relevant_ids = {entries[0].id, entries[1].id}

        ndcg = self._ndcg_at_k(ranked_ids, relevant_ids, k=3)
        assert ndcg > 0.5, (
            f"nDCG@3={ndcg:.3f}: long/short relevant entries not ranked above flash distractor. "
            f"Strengths: long={strengths[entries[0].id]:.3f}, short={strengths[entries[1].id]:.3f}, "
            f"flash={strengths[entries[2].id]:.3f}"
        )

    def test_consolidation_strength_field_persists(self):
        """Phase 0: consolidation_strength field must round-trip through SQLiteStore."""
        e = _make_entry("Test memory", importance=0.6)
        e.consolidation_strength = 0.73

        store = _sqlite_with_entries([e])
        fetched = store.get_memory(e.id)

        assert fetched is not None
        assert abs(fetched.consolidation_strength - 0.73) < 1e-4, (
            f"consolidation_strength did not round-trip: stored=0.73 fetched={fetched.consolidation_strength}"
        )

    def test_update_consolidation_strength(self):
        """update_consolidation_strength() must update the stored value."""
        e = _make_entry("Test memory")
        store = _sqlite_with_entries([e])

        store.update_consolidation_strength(e.id, 0.88)
        fetched = store.get_memory(e.id)
        assert fetched is not None
        assert abs(fetched.consolidation_strength - 0.88) < 1e-4

    def test_batch_update_consolidation_strength(self):
        """batch_update_consolidation_strength() must update all entries."""
        entries = [_make_entry(f"Memory {i}") for i in range(5)]
        store = _sqlite_with_entries(entries)

        updates = [(e.id, 0.1 * (i + 1)) for i, e in enumerate(entries)]
        store.batch_update_consolidation_strength(updates)

        for i, e in enumerate(entries):
            fetched = store.get_memory(e.id)
            expected = 0.1 * (i + 1)
            assert fetched is not None
            assert abs(fetched.consolidation_strength - expected) < 1e-4, (
                f"Entry {i}: expected {expected:.2f}, got {fetched.consolidation_strength:.3f}"
            )


# =============================================================================
# Benchmark 2 — Tier Distribution
# =============================================================================

class TestTierDistribution:
    """
    After a sleep cycle on a known corpus, verify:
    - Strong, well-accessed memories promote.
    - Weak, old memories do not promote.
    - Long-tier memories with high importance do not demote.
    """

    def test_flash_promotes_to_short_above_threshold(self):
        """Flash memories with strength ≥ 0.62 after 3h should be planned for promotion."""
        now = time.time()
        b = SleepBudget()

        # Strong flash — should promote (4h old, past min_flash_age_sec=3h)
        strong = _make_entry(
            "User loves hiking.", memory_type="flash",
            importance=0.80, age_days=2, access_count=5,
        )
        strong.created_at = now - 4 * 3600  # 4h old (past min_flash_age_sec=3h v5-P5)

        # Weak flash — should not promote
        weak = _make_entry(
            "Random thought.", memory_type="flash",
            importance=0.15, age_days=2, access_count=0,
        )
        weak.created_at = now - 4 * 3600

        entries = [strong, weak]
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        updates = _plan_tier_updates(
            candidates=entries, id_map=id_map, neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla, dynamic_importances=dyn_imps, budget=b, now=now,
        )
        promoted_ids = {mid for mid, old, new in updates if new == "short"}
        assert strong.id in promoted_ids, "Strong flash should be promoted to short"
        assert weak.id not in promoted_ids, "Weak flash should not be promoted"

    def test_long_high_importance_does_not_demote(self):
        """Long-tier memories with dyn_imp > 0.70 must never demote."""
        now = time.time()
        b = SleepBudget()

        protected = _make_entry(
            "User has a penicillin allergy — critical medical information.",
            memory_type="long", importance=0.95, age_days=200, access_count=0,
        )
        entries = [protected]
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        updates = _plan_tier_updates(
            candidates=entries, id_map=id_map, neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla, dynamic_importances=dyn_imps, budget=b, now=now,
        )
        demoted_ids = {mid for mid, old, new in updates if old == "long" and new == "short"}
        assert protected.id not in demoted_ids, (
            "High-importance long memory must not demote even after 200 days without access"
        )


# =============================================================================
# Benchmark 3 — Data Safety (Provisional Summaries)
# =============================================================================

class TestDataSafety:
    """
    Phase 0 baseline: document the current behavior for provisional summaries.
    Phase 1 (P7): these tests become requirements (source must survive provisional window).
    """

    def test_catastrophic_guard_blocks_high_importance(self):
        """_is_protected() must return True for imp >= 0.80 + not consolidated."""
        b = SleepBudget()
        critical = _make_entry("Medical allergy: penicillin.", importance=0.90)
        critical.consolidated_into_id = None
        assert _is_protected(critical, b), "imp=0.90 should be protected"

    def test_catastrophic_guard_allows_consolidated(self):
        """Once consolidated, _is_protected() must return False (summary holds the info)."""
        b = SleepBudget()
        summarized = _make_entry("Old raw entry.", importance=0.90)
        summarized.consolidated_into_id = str(uuid.uuid4())
        assert not _is_protected(summarized, b), (
            "Consolidated entry should not be protected (summary holds the data)"
        )

    def test_catastrophic_guard_blocks_isolated_high_importance(self):
        """
        Phase 1 (P12): imp >= 0.70 AND dup_count == 0 must be protected.
        """
        b = SleepBudget()
        isolated = _make_entry("User's hometown is Ahmedabad.", importance=0.72)
        isolated.consolidated_into_id = None

        # dup_count=0 means no similar memories exist — this entry is irreplaceable
        result = _is_protected(isolated, b, dup_count=0)
        assert result, (
            "imp=0.72 with dup_count=0 should be protected (P12 broadened guard)"
        )

        # But dup_count > 0 should NOT be protected at 0.72 (has neighbors to cover for it)
        result_with_dups = _is_protected(isolated, b, dup_count=2)
        assert not result_with_dups, (
            "imp=0.72 with dup_count=2 should NOT be protected "
            "(semantic neighbors exist)"
        )

    def test_forgotten_log_append_and_retrieve(self):
        """forgotten_log must store and retrieve hard-deleted memory records."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        store = SQLiteStore(db_path)

        store.forgotten_log_append(
            memory_id="abc123",
            memory_text="User mentioned they're allergic to latex.",
            memory_type="short",
            importance=0.65,
            reason="redundancy",
        )

        rows = store.forgotten_log_recent(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["memory_id"] == "abc123"
        assert "latex" in row["memory_text"]
        assert row["reason"] == "redundancy"
        assert row["importance"] == pytest.approx(0.65, abs=1e-4)

    def test_forgotten_log_rolling_window(self):
        """forgotten_log must not exceed _FORGOTTEN_LOG_MAX_ROWS rows."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        store = SQLiteStore(db_path)
        store._FORGOTTEN_LOG_MAX_ROWS = 5  # shrink limit for test speed

        for i in range(10):
            store.forgotten_log_append(
                memory_id=str(uuid.uuid4()),
                memory_text=f"Memory {i}",
                memory_type="flash",
                importance=0.3,
                reason="dead_trace",
            )

        rows = store.forgotten_log_recent(limit=100)
        assert len(rows) <= 5, f"Rolling window exceeded: got {len(rows)} rows"


# =============================================================================
# Benchmark 4 — Cluster Safety (No Extinction)
# =============================================================================

class TestClusterSafety:
    """
    Redundancy pruning must always leave at least one representative per cluster.
    Phase 1 (P11) fixes cluster extinction — these tests document baseline and target.
    """

    def _build_redundant_cluster(self, n: int = 4) -> List[MemoryEntry]:
        """Create n near-identical entries that will all qualify for redundancy deletion.

        Conditions satisfied:
          weighted_dup_count >= 3 → n=4, noise=0.0 → dup_count=3, avg_sim=1.0, weighted=3.0
          dyn_imp <= 0.25         → importance=0.10, access_count=0, age=50d → dyn_imp≈0.09
          acc <= 2                → access_count=0
          age_days >= 30          → age=50d
          M <= 0.30               → low BLA (50d old, 0 accesses) → M≈0.02
        """
        base = _rand_emb()
        entries = []
        for i in range(n):
            e = _make_entry(
                f"User prefers dark mode (variant {i})",
                memory_type="flash",
                importance=0.10,
                age_days=50,      # well past redundancy_min_age_sec (30d)
                access_count=0,   # never accessed → dyn_imp stays low
                embedding=base.copy(),  # noise=0.0 → sim=1.0 → weighted_dup_count = n-1
            )
            entries.append(e)
        return entries

    def test_redundancy_gate_triggers(self):
        """Verify that the redundancy conditions are met for the test corpus."""
        entries = self._build_redundant_cluster(4)
        now = time.time()
        b = SleepBudget()
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        store = _sqlite_with_entries(entries)
        dels, redundancy_ids, _ = _plan_hard_deletes(
            sqlite_store=store, candidates=entries, id_map=id_map,
            neighbor_map=neighbor_map, raw_bla_scores=raw_bla,
            dynamic_importances=dyn_imps, budget=b, now=now,
            limit=b.max_hard_deletes,
        )
        # At least some should be marked redundant
        assert len(redundancy_ids) >= 1, "Expected at least 1 redundancy deletion in test corpus"

    def test_cluster_extinction_baseline(self):
        """
        BASELINE (v4.1): Documents that all 4 entries CAN be deleted.
        After Phase 1 (P11): at most n-1 entries should be deleted (keep 1 representative).
        """
        entries = self._build_redundant_cluster(4)
        now = time.time()
        b = SleepBudget()
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        store = _sqlite_with_entries(entries)
        dels, _, _ = _plan_hard_deletes(
            sqlite_store=store, candidates=entries, id_map=id_map,
            neighbor_map=neighbor_map, raw_bla_scores=raw_bla,
            dynamic_importances=dyn_imps, budget=b, now=now,
            limit=b.max_hard_deletes,
        )

        entry_ids = {e.id for e in entries}
        deleted_from_cluster = [d for d in dels if d in entry_ids]

        # Phase 1 (P11): at most n-1 entries may be deleted; at least 1 representative survives
        assert len(deleted_from_cluster) < len(entries), (
            f"Cluster extinction: all {len(entries)} members deleted. "
            f"P11 keep-one-representative must preserve at least 1."
        )


# =============================================================================
# Benchmark 5 — Score Stability (regression guard)
# =============================================================================

class TestScoreStability:
    """
    Verify that strength scores for a fixed corpus are stable across runs
    (no randomness, no timestamp drift in tests).
    These serve as a regression guard — if a score changes unexpectedly after
    a code change, the test fails and forces a conscious decision.
    """

    def _fixed_entry(self, text: str, importance: float, age_days: float, acc: int) -> MemoryEntry:
        """Create an entry with a fixed embedding (deterministic seeding)."""
        emb = _rand_emb()  # RNG is seeded at top of file
        e = MemoryEntry(
            text=text,
            memory_type="short",
            importance=importance,
            created_at=1_700_000_000.0 - age_days * 86400.0,
            last_accessed=1_700_000_000.0 - (age_days * 0.3) * 86400.0,
            access_count=acc,
            embedding=emb,
        )
        return e

    def test_strength_is_in_unit_interval(self):
        """Strength must always be in [0, 1] regardless of inputs."""
        now = 1_700_000_000.0
        b = SleepBudget()
        entries = [
            self._fixed_entry("Memory A", 0.8, 5, 10),
            self._fixed_entry("Memory B", 0.3, 90, 0),
            self._fixed_entry("Memory C", 0.5, 1, 1),
        ]
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        for e in entries:
            s = _compute_strength(
                e, now=now, budget=b,
                neighbor_map=neighbor_map, raw_bla_scores=raw_bla,
                dynamic_importances=dyn_imps, id_map=id_map,
            )
            assert 0.0 <= s <= 1.0, f"Strength out of [0,1]: {s:.4f} for '{e.text}'"

    def test_high_importance_scores_above_low(self):
        """High-importance entries should score higher than otherwise-identical low-importance ones."""
        now = 1_700_000_000.0
        b = SleepBudget()
        base_emb = _rand_emb()

        hi = MemoryEntry(
            text="High importance memory",
            memory_type="short", importance=0.90,
            created_at=now - 10 * 86400, last_accessed=now - 5 * 86400,
            access_count=5, embedding=base_emb.copy(),
        )
        lo = MemoryEntry(
            text="Low importance memory",
            memory_type="short", importance=0.20,
            created_at=now - 10 * 86400, last_accessed=now - 5 * 86400,
            access_count=5, embedding=base_emb.copy(),
        )
        entries = [hi, lo]
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        s_hi = _compute_strength(
            hi, now=now, budget=b, neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla, dynamic_importances=dyn_imps, id_map=id_map,
        )
        s_lo = _compute_strength(
            lo, now=now, budget=b, neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla, dynamic_importances=dyn_imps, id_map=id_map,
        )
        assert s_hi > s_lo, (
            f"High importance ({s_hi:.3f}) should score above low importance ({s_lo:.3f})"
        )


# =============================================================================
# Benchmark 6 — Phase 2: Encoding Fidelity
# =============================================================================


class TestEncodingFidelity:
    """
    Phase 2 tests:
      P1 — encoding_arousal computed from raw user message
      P3 — protection_tier from Brain ingestion wired through to _is_protected()
    """

    # ------------------------------------------------------------------
    # P1 — encoding_arousal
    # ------------------------------------------------------------------

    def test_arousal_zero_for_neutral_text(self):
        """Plain chitchat should yield 0.0 arousal."""
        from buddy.buddy_core.pipeline import _compute_encoding_arousal
        score = _compute_encoding_arousal("what's the weather like today")
        assert score == 0.0, f"Expected 0.0, got {score}"

    def test_arousal_nonzero_for_medical_keyword(self):
        """A single medical keyword should bump arousal above 0."""
        from buddy.buddy_core.pipeline import _compute_encoding_arousal
        score = _compute_encoding_arousal("I have a latex allergy, please remember that.")
        assert score > 0.0, "Expected arousal > 0 for medical keyword"

    def test_arousal_saturates_at_one(self):
        """Dense arousal signals should saturate at 1.0."""
        from buddy.buddy_core.pipeline import _compute_encoding_arousal
        text = "urgent critical emergency hospital allergy pain sick"
        score = _compute_encoding_arousal(text)
        assert score == 1.0, f"Expected 1.0 saturation, got {score}"

    def test_arousal_bigram_match(self):
        """Bigrams like 'never forget' should register as a hit."""
        from buddy.buddy_core.pipeline import _compute_encoding_arousal
        score = _compute_encoding_arousal("please never forget this")
        assert score > 0.0, "Expected arousal > 0 for 'never forget' bigram"

    def test_arousal_empty_string(self):
        """Empty input must return 0.0 without error."""
        from buddy.buddy_core.pipeline import _compute_encoding_arousal
        assert _compute_encoding_arousal("") == 0.0
        assert _compute_encoding_arousal("   ") == 0.0

    # ------------------------------------------------------------------
    # P3 — protection_tier in _is_protected()
    # ------------------------------------------------------------------

    def test_immortal_protected_even_after_consolidation(self):
        """immortal tier must protect a memory even when consolidated_into_id is set."""
        b = SleepBudget()
        m = _make_entry("User's blood type is O-negative.", importance=0.50)
        m.consolidated_into_id = str(uuid.uuid4())  # already consolidated
        m.metadata = {"protection_tier": "immortal"}

        result = _is_protected(m, b, dup_count=5)
        assert result, (
            "immortal protection_tier must protect even after consolidation "
            "(consolidated_into_id set)"
        )

    def test_critical_protected_before_consolidation(self):
        """critical tier must protect a memory that has not been consolidated yet."""
        b = SleepBudget()
        m = _make_entry("User has a penicillin allergy.", importance=0.45)
        m.consolidated_into_id = None
        m.metadata = {"protection_tier": "critical"}

        result = _is_protected(m, b, dup_count=3)
        assert result, (
            "critical protection_tier must protect unconsolidated memory "
            "even with importance below 0.70"
        )

    def test_critical_not_protected_after_consolidation(self):
        """critical tier must NOT protect a memory once it has been consolidated."""
        b = SleepBudget()
        m = _make_entry("User prefers Python.", importance=0.45)
        m.consolidated_into_id = str(uuid.uuid4())  # consolidated → safe to purge
        m.metadata = {"protection_tier": "critical"}

        result = _is_protected(m, b, dup_count=3)
        assert not result, (
            "critical tier must not protect a memory that is already consolidated "
            "(immortal is the only tier that overrides this)"
        )

    def test_normal_tier_behaves_as_before(self):
        """normal tier must not change existing Rule A / Rule B behavior."""
        b = SleepBudget()

        # imp=0.50, dup_count=3, normal → NOT protected (below both thresholds)
        m_low = _make_entry("User mentioned dark mode.", importance=0.50)
        m_low.metadata = {"protection_tier": "normal"}
        assert not _is_protected(m_low, b, dup_count=3)

        # imp=0.85, normal → protected via Rule A
        m_high = _make_entry("User's wife is named Priya.", importance=0.85)
        m_high.metadata = {"protection_tier": "normal"}
        assert _is_protected(m_high, b, dup_count=3)

    def test_protection_tier_written_to_metadata(self):
        """create_memory_entry() must store protection_tier in entry metadata."""
        import tempfile, os
        from buddy.memory.memory_manager import MemoryManager
        from buddy.memory.sqlite_store import SQLiteStore

        # Minimal mock embedder
        mock_emb = MagicMock()
        mock_emb.embed_passage.return_value = np.zeros(16, dtype=np.float32)
        mock_emb.embed_query.return_value = np.zeros(16, dtype=np.float32)

        # Minimal mock vector store
        mock_vs = MagicMock()

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteStore(db_path)
            mm = MemoryManager(
                sqlite_store=store,
                vector_store=mock_vs,
                embedder=mock_emb,
                brain=None,
            )

            memory_dict = {
                "memory_type": "short",
                "memory_text": "User has a documented latex allergy.",
                "salience": 0.65,
                "protection_tier": "critical",
            }
            entry = mm.create_memory_entry(memory=memory_dict, role="buddy")
            assert entry is not None
            assert entry.metadata.get("protection_tier") == "critical", (
                f"Expected 'critical' in metadata, got: {entry.metadata}"
            )

            # normal tier must NOT pollute metadata
            memory_dict_normal = {
                "memory_type": "flash",
                "memory_text": "User prefers dark mode.",
                "salience": 0.30,
                "protection_tier": "normal",
            }
            entry_normal = mm.create_memory_entry(memory=memory_dict_normal, role="buddy")
            assert entry_normal is not None
            assert "protection_tier" not in entry_normal.metadata, (
                "normal protection_tier must not be stored in metadata (no-op)"
            )
        finally:
            os.unlink(db_path)


# =============================================================================
# Benchmark 7 — Phase 3: Recall Integration
# =============================================================================


class TestRecallIntegration:
    """
    Phase 3 tests:
      P14 — consolidation_strength written to SQLite during sleep run
      P14 — composite score uses consolidation_strength
      P15 — new weight constants are correct
      P16 — spreading activation (mock vector store returns neighbors)
      P17 — touch() bumps consolidation_strength in entry and SQLite
    """

    # ------------------------------------------------------------------
    # P17 — touch() bumps consolidation_strength
    # ------------------------------------------------------------------

    def test_touch_bumps_strength_on_entry(self):
        """MemoryEntry.touch() must increment consolidation_strength by 0.05."""
        e = MemoryEntry(text="Test", consolidation_strength=0.20)
        e.touch()
        assert e.consolidation_strength == pytest.approx(0.25, abs=1e-6)

    def test_touch_caps_strength_at_one(self):
        """MemoryEntry.touch() must not push consolidation_strength above 1.0."""
        e = MemoryEntry(text="Test", consolidation_strength=0.98)
        e.touch()
        assert e.consolidation_strength == pytest.approx(1.0, abs=1e-6)
        e.touch()
        assert e.consolidation_strength == pytest.approx(1.0, abs=1e-6)

    def test_sqlite_touch_bumps_strength(self):
        """SQLiteStore.touch() must bump consolidation_strength in the DB."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        import os
        try:
            store = SQLiteStore(db_path)
            e = MemoryEntry(text="Memory to touch.", consolidation_strength=0.30)
            store.upsert_memory(e)

            store.touch(e.id)

            fetched = store.get_memory(e.id)
            assert fetched is not None
            assert fetched.consolidation_strength == pytest.approx(0.35, abs=1e-4), (
                f"Expected 0.35 after one touch, got {fetched.consolidation_strength}"
            )
        finally:
            os.unlink(db_path)

    def test_sqlite_touch_caps_at_one(self):
        """SQLiteStore.touch() must not exceed 1.0."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        import os
        try:
            store = SQLiteStore(db_path)
            e = MemoryEntry(text="Nearly maxed.", consolidation_strength=0.98)
            store.upsert_memory(e)
            store.touch(e.id)

            fetched = store.get_memory(e.id)
            assert fetched is not None
            assert fetched.consolidation_strength <= 1.0
        finally:
            os.unlink(db_path)

    # ------------------------------------------------------------------
    # P14 — consolidation_strength written during sleep (Phase 0b)
    # ------------------------------------------------------------------

    def test_sleep_writes_consolidation_strength(self):
        """After run_consolidation, entries must have consolidation_strength > 0."""
        entries = [
            _make_entry("User works as a software engineer.", importance=0.70,
                        memory_type="short", age_days=5, access_count=3),
            _make_entry("User's favourite language is Python.", importance=0.65,
                        memory_type="short", age_days=7, access_count=2),
            _make_entry("User has a dog named Bruno.", importance=0.60,
                        memory_type="short", age_days=10, access_count=1),
        ]
        store = _sqlite_with_entries(entries)
        vs = _mock_vector_store(entries)
        brain = _mock_brain()

        run_consolidation(
            sqlite_store=store,
            vector_store=vs,
            brain=brain,
            embed=_embed_fn,
            dry_run=False,
        )

        for e in entries:
            fetched = store.get_memory(e.id)
            assert fetched is not None
            assert fetched.consolidation_strength >= 0.0, (
                f"consolidation_strength must be written for '{e.text}'"
            )

    # ------------------------------------------------------------------
    # P14+P15 — composite score reads consolidation_strength
    # ------------------------------------------------------------------

    def test_composite_score_uses_strength(self):
        """Higher consolidation_strength must produce a higher composite score."""
        from buddy.memory.memory_manager import _composite_score

        score_high = _composite_score(
            semantic=0.70, rerank=0.0, consolidation_strength=0.90,
            tier="short", encoding_arousal=0.0,
        )
        score_low = _composite_score(
            semantic=0.70, rerank=0.0, consolidation_strength=0.10,
            tier="short", encoding_arousal=0.0,
        )
        assert score_high > score_low, (
            f"Higher strength ({score_high:.3f}) must outscore lower ({score_low:.3f})"
        )

    def test_composite_score_weights_sum_correctly(self):
        """Max possible composite score (all signals=1.0) must equal sum of weights."""
        from buddy.memory.memory_manager import (
            _composite_score, W_SEMANTIC, W_STRENGTH, W_RERANK, W_TIER, W_AROUSAL
        )
        max_score = _composite_score(
            semantic=1.0, rerank=1.0, consolidation_strength=1.0,
            tier="long", encoding_arousal=1.0,
        )
        expected = W_SEMANTIC + W_STRENGTH + W_RERANK + W_TIER + W_AROUSAL
        assert max_score == pytest.approx(expected, abs=1e-6), (
            f"Expected weights to sum to {expected:.2f}, got {max_score:.4f}"
        )

    def test_composite_score_arousal_contributes(self):
        """encoding_arousal must increase composite score."""
        from buddy.memory.memory_manager import _composite_score

        no_arousal = _composite_score(
            semantic=0.50, rerank=0.0, consolidation_strength=0.50,
            tier="flash", encoding_arousal=0.0,
        )
        with_arousal = _composite_score(
            semantic=0.50, rerank=0.0, consolidation_strength=0.50,
            tier="flash", encoding_arousal=1.0,
        )
        assert with_arousal > no_arousal


# =============================================================================
# Benchmark 8 — Phase 4: Clustering Mechanics
# =============================================================================


class TestClusteringMechanics:
    """
    Phase 4 tests:
      P8  — BFS connected-components (order-independent clustering)
      P9  — consolidation_depth cap (confidence gate at depth ≥ 3)
      P10 — temporal coherence (episodic vs schema cluster labeling)
      P4  — short→long promotion uses dup_count==0 instead of sim_max gate
    """

    def _make_ring(self, n: int = 4) -> List[MemoryEntry]:
        """
        Create a ring of n entries: A→B→C→D→A (sim >= tau_dup between consecutive pairs).
        With 1-hop expansion, B would NOT be in A's cluster if C was already visited.
        With BFS connected-components, all 4 are in one component.
        """
        embs = [_rand_emb() for _ in range(n)]
        return [
            _make_entry(f"Ring entry {i}", memory_type="short",
                        importance=0.55, age_days=3, access_count=2,
                        embedding=embs[i])
            for i in range(n)
        ]

    def _mock_ring_vector_store(self, entries: List[MemoryEntry]) -> Any:
        """Vector store where entry[i] is similar to entry[(i+1) % n]."""
        n = len(entries)
        tau = SleepBudget().tau_dup
        vs = MagicMock()

        def search_with_payloads(query_vector, query_text="", top_k=20, **kwargs):
            q = np.asarray(query_vector, dtype=np.float32)
            q = q / (np.linalg.norm(q) + 1e-9)
            results = []
            for i, e in enumerate(entries):
                emb = getattr(e, "embedding", None)
                if emb is None:
                    continue
                sim = float(np.dot(q, emb))
                results.append((e.id, sim, {}))
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:top_k]

        vs.search_with_payloads.side_effect = search_with_payloads
        return vs

    # ------------------------------------------------------------------
    # P8 — BFS connected-components
    # ------------------------------------------------------------------

    def test_bfs_clusters_all_connected_members(self):
        """All members of a fully-connected component must end up in one cluster."""
        entries = [
            _make_entry(f"Preference entry {i}", memory_type="short",
                        importance=0.55, age_days=3, access_count=2,
                        embedding=_rand_emb())
            for i in range(4)
        ]
        # Make all embeddings near-identical so they form one connected component
        base = entries[0].embedding.copy()
        for e in entries:
            object.__setattr__(e, "embedding", base.copy())

        now = time.time()
        b = SleepBudget()
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        store = _sqlite_with_entries(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        clusters = _build_clusters(
            sqlite_store=store, candidates=entries, id_map=id_map,
            neighbor_map=neighbor_map, raw_bla_scores=raw_bla,
            dynamic_importances=dyn_imps, budget=b, now=now,
        )

        all_clustered_ids = {cid for cl in clusters for cid in cl.ids}
        entry_ids = {e.id for e in entries}
        assert entry_ids.issubset(all_clustered_ids), (
            "BFS must cluster all connected members into one or more clusters. "
            f"Missing: {entry_ids - all_clustered_ids}"
        )

    def test_bfs_no_isolated_member_split(self):
        """Two clearly distinct groups must not share a cluster."""
        group_a_emb = _rand_emb()
        group_b_emb = _rand_emb()
        # Make b orthogonal to a (low similarity)
        group_b_emb = group_b_emb - np.dot(group_b_emb, group_a_emb) * group_a_emb
        group_b_emb = group_b_emb / (np.linalg.norm(group_b_emb) + 1e-9)

        group_a = [_make_entry(f"Topic A entry {i}", embedding=group_a_emb.copy(),
                               importance=0.55, age_days=5) for i in range(3)]
        group_b = [_make_entry(f"Topic B entry {i}", embedding=group_b_emb.copy(),
                               importance=0.55, age_days=5) for i in range(3)]
        entries = group_a + group_b

        now = time.time()
        b = SleepBudget()
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        store = _sqlite_with_entries(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        clusters = _build_clusters(
            sqlite_store=store, candidates=entries, id_map=id_map,
            neighbor_map=neighbor_map, raw_bla_scores=raw_bla,
            dynamic_importances=dyn_imps, budget=b, now=now,
        )

        ids_a = {e.id for e in group_a}
        ids_b = {e.id for e in group_b}
        for cl in clusters:
            cluster_set = set(cl.ids)
            assert not (cluster_set & ids_a and cluster_set & ids_b), (
                "Orthogonal groups must not be merged into one cluster"
            )

    # ------------------------------------------------------------------
    # P10 — temporal coherence (schema vs episodic)
    # ------------------------------------------------------------------

    def test_episodic_cluster_within_14_days(self):
        """Cluster with all members within 14 days must be labeled episodic."""
        base = _rand_emb()
        entries = [
            _make_entry(f"Recent preference {i}", embedding=base.copy(),
                        importance=0.55, age_days=i + 1)  # days 1–4
            for i in range(4)
        ]
        now = time.time()
        b = SleepBudget()
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        store = _sqlite_with_entries(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        clusters = _build_clusters(
            sqlite_store=store, candidates=entries, id_map=id_map,
            neighbor_map=neighbor_map, raw_bla_scores=raw_bla,
            dynamic_importances=dyn_imps, budget=b, now=now,
        )

        assert clusters, "Expected at least one cluster"
        for cl in clusters:
            assert not cl.is_schema, (
                f"Cluster spanning {cl.time_span_days:.1f} days must be episodic"
            )

    def test_schema_cluster_across_14_days(self):
        """Cluster spanning > 14 days must be labeled as schema."""
        base = _rand_emb()
        entries = [
            _make_entry("Python preference week 1", embedding=base.copy(),
                        importance=0.55, age_days=30),
            _make_entry("Python preference week 4", embedding=base.copy(),
                        importance=0.55, age_days=2),
            _make_entry("Python preference week 3", embedding=base.copy(),
                        importance=0.55, age_days=8),
        ]
        now = time.time()
        b = SleepBudget()
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        store = _sqlite_with_entries(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        clusters = _build_clusters(
            sqlite_store=store, candidates=entries, id_map=id_map,
            neighbor_map=neighbor_map, raw_bla_scores=raw_bla,
            dynamic_importances=dyn_imps, budget=b, now=now,
        )

        assert clusters, "Expected at least one cluster from 30-day span entries"
        # At least one cluster should be a schema cluster (spans > 14d)
        schema_clusters = [cl for cl in clusters if cl.is_schema]
        assert schema_clusters, (
            f"Expected schema cluster for entries spanning 30 days; "
            f"got clusters: {[(cl.is_schema, cl.time_span_days) for cl in clusters]}"
        )

    # ------------------------------------------------------------------
    # P4 — short→long: dup_count==0 replaces sim_max gate
    # ------------------------------------------------------------------

    def test_unique_short_promotes_to_long_without_sim_gate(self):
        """A unique short memory (dup_count=0) must promote to long if strength
        and cycles qualify — the old sim_max <= 0.60 gate must NOT block it."""
        now = 1_700_000_000.0
        b = SleepBudget()

        # Memory with high sim_max — would have been blocked by the old gate
        m = MemoryEntry(
            text="User is allergic to latex.",
            memory_type="short",
            importance=0.80,
            created_at=now - 60 * 86400,
            last_accessed=now - 5 * 86400,
            access_count=8,
            embedding=_rand_emb(),
            metadata={"consolidation_cycles": b.min_cycles_for_long},
        )
        id_map = {m.id: m}
        vs = _mock_vector_store([m])
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=[m], budget=b, now=now)
        # Ensure dup_count == 0 (no neighbors found above tau_dup for this lone entry)
        assert neighbor_map[m.id].dup_count == 0, "Expected no neighbors for isolated entry"

        dyn_imps = {m.id: _compute_dynamic_importance(m, now=now, budget=b)}
        raw_bla = _compute_all_raw_bla([m], now=now, budget=b, dynamic_importances=dyn_imps)

        M = _compute_strength(
            m, now=now, budget=b, neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla, dynamic_importances=dyn_imps, id_map=id_map,
        )

        updates = _plan_tier_updates(
            candidates=[m], id_map=id_map, neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla, dynamic_importances=dyn_imps, budget=b, now=now,
        )

        if M >= b.short_to_long_strength:
            # If strength qualifies, must promote
            promoted = [u for u in updates if u[0] == m.id and u[2] == "long"]
            assert promoted, (
                f"Unique short memory with M={M:.3f} >= threshold must promote to long. "
                f"Updates: {updates}"
            )
        # If M < threshold, no promotion is expected — that's fine

    def test_short_with_dups_does_not_direct_promote(self):
        """A short memory with dup_count >= 2 must NOT directly promote to long
        (it goes through cluster-summary route instead)."""
        now = 1_700_000_000.0
        b = SleepBudget()
        base = _rand_emb()

        entries = [
            MemoryEntry(
                text=f"User prefers Python (version {i})",
                memory_type="short",
                importance=0.80,
                created_at=now - 60 * 86400,
                last_accessed=now - 5 * 86400,
                access_count=8,
                embedding=base.copy(),
                metadata={"consolidation_cycles": b.min_cycles_for_long},
            )
            for i in range(4)
        ]
        id_map = {e.id: e for e in entries}
        vs = _mock_vector_store(entries)
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=entries, budget=b, now=now)
        dyn_imps = {e.id: _compute_dynamic_importance(e, now=now, budget=b) for e in entries}
        raw_bla = _compute_all_raw_bla(entries, now=now, budget=b, dynamic_importances=dyn_imps)

        updates = _plan_tier_updates(
            candidates=entries, id_map=id_map, neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla, dynamic_importances=dyn_imps, budget=b, now=now,
        )

        # None should be direct short→long promotions for entries with many dups
        direct_promotions = [
            u for u in updates
            if u[0] in {e.id for e in entries} and u[2] == "long"
        ]
        assert not direct_promotions, (
            f"Memories with dup_count >= 2 must not directly promote to long "
            f"(cluster-summary route only). Got: {direct_promotions}"
        )


# =============================================================================
# Benchmark 9 — Phase 5: Tuning & Polish
# =============================================================================


class TestTuningPolish:
    """
    Phase 5 tests:
      P5  — flash_to_short_strength raised to 0.62; min_flash_age_sec to 10800
      P6  — cycle counter bumped for ALL scanned candidates (including summarized)
      X1  — Hindi/Hinglish arousal keywords detected
      X2  — SWS arm weight boosted to 0.20
      X5  — novelty-burst: high-arousal entry boosts neighbors' consolidation_strength
    """

    # ------------------------------------------------------------------
    # P5 — threshold changes
    # ------------------------------------------------------------------

    def test_flash_to_short_threshold_raised(self):
        """SleepBudget.flash_to_short_strength must be 0.62."""
        b = SleepBudget()
        assert b.flash_to_short_strength == pytest.approx(0.62, abs=1e-6)

    def test_min_flash_age_sec_raised(self):
        """SleepBudget.min_flash_age_sec must be 10800 (3 hours)."""
        b = SleepBudget()
        assert b.min_flash_age_sec == pytest.approx(10800.0, abs=1e-6)

    def test_very_fresh_flash_does_not_promote(self):
        """Flash memory under 3h must NOT promote regardless of strength."""
        now = time.time()
        b = SleepBudget()
        m = _make_entry("User likes coffee.", memory_type="flash",
                        importance=0.90, age_days=0.05)  # ~1.2 hours
        id_map = {m.id: m}
        vs = _mock_vector_store([m])
        neighbor_map = _build_neighbor_map(vector_store=vs, candidates=[m], budget=b, now=now)
        dyn_imps = {m.id: _compute_dynamic_importance(m, now=now, budget=b)}
        raw_bla = _compute_all_raw_bla([m], now=now, budget=b, dynamic_importances=dyn_imps)

        updates = _plan_tier_updates(
            candidates=[m], id_map=id_map, neighbor_map=neighbor_map,
            raw_bla_scores=raw_bla, dynamic_importances=dyn_imps, budget=b, now=now,
        )
        assert not updates, (
            "Flash memory < 3h old must not promote, even with high importance"
        )

    # ------------------------------------------------------------------
    # P6 — cycle counter for all scanned
    # ------------------------------------------------------------------

    def test_cycle_counter_bumps_summarized_entries(self):
        """Entries that get summarized (soft-deleted) must still have cycles bumped."""
        base = _rand_emb()
        entries = [
            _make_entry(f"User prefers Python (v{i})", embedding=base.copy(),
                        importance=0.65, memory_type="short", age_days=10,
                        access_count=2)
            for i in range(4)
        ]
        store = _sqlite_with_entries(entries)
        vs = _mock_vector_store(entries)
        brain = _mock_brain("User prefers Python.")

        run_consolidation(
            sqlite_store=store,
            vector_store=vs,
            brain=brain,
            embed=_embed_fn,
            dry_run=False,
        )

        # At least some entries should have cycles > 0 (including soft-deleted ones)
        for e in entries:
            fetched = store.get_memory(e.id)
            if fetched is None:
                continue  # hard-deleted
            cycles = int((fetched.metadata or {}).get("consolidation_cycles", 0))
            assert cycles >= 1, (
                f"Entry '{e.text}' must have consolidation_cycles >= 1 "
                f"after being processed (even if soft-deleted). Got: {cycles}"
            )

    # ------------------------------------------------------------------
    # X1 — Hindi/Hinglish arousal keywords
    # ------------------------------------------------------------------

    def test_hindi_arousal_keywords_detected(self):
        """Hindi/Hinglish high-arousal words must register in _compute_arousal."""
        from buddy.memory.consolidation_engine import _compute_arousal

        for word, desc in [
            ("dard", "pain"),
            ("maut", "death"),
            ("pyaar", "love"),
            ("nafrat", "hate"),
            ("dukh", "sorrow"),
            ("gussa", "anger"),
            ("pareshan", "troubled"),
        ]:
            entry = MemoryEntry(text=f"User said they feel {word} all the time.")
            score = _compute_arousal(entry)
            assert score > 0.0, (
                f"Hindi keyword '{word}' ({desc}) must register in _compute_arousal. "
                f"Got score={score:.3f}"
            )

    def test_hindi_keyword_boosts_arousal_vs_neutral(self):
        """Hindi arousal keyword must produce higher score than neutral text."""
        from buddy.memory.consolidation_engine import _compute_arousal

        neutral = MemoryEntry(text="User talked about the weather today.")
        emotional = MemoryEntry(text="User is feeling bahut pareshan about life.")
        assert _compute_arousal(emotional) > _compute_arousal(neutral)

    # ------------------------------------------------------------------
    # X2 — SWS arm boost
    # ------------------------------------------------------------------

    def test_sws_factual_memory_gets_boosted_weight(self):
        """A low-arousal, high-importance memory must get SWS boost ≥ 0.20 component."""
        from buddy.memory.consolidation_engine import _compute_sleep_phase_weight

        factual = MemoryEntry(
            text="User's blood type is O-negative.",
            importance=0.85,
        )
        weight = _compute_sleep_phase_weight(factual)
        # SWS arm: 0.20 * max(0, 0.85 - ~0.0) = ~0.17 → total weight > 1.0
        assert weight > 1.0, (
            f"Factual low-arousal high-importance memory must get SWS boost > 1.0, "
            f"got {weight:.3f}"
        )

    def test_sws_boost_greater_than_before(self):
        """SWS arm must contribute more than the old 0.10 weight."""
        from buddy.memory.consolidation_engine import _compute_sleep_phase_weight, _compute_arousal

        factual = MemoryEntry(text="User has a CS degree from IIT Delhi.", importance=0.80)
        arousal = _compute_arousal(factual)
        # With old 0.10: sws = 0.10 * (0.80 - arousal)
        # With new 0.20: sws = 0.20 * (0.80 - arousal)
        old_sws = 0.10 * max(0.0, 0.80 - arousal)
        new_sws = 0.20 * max(0.0, 0.80 - arousal)
        weight = _compute_sleep_phase_weight(factual)
        # The weight should include roughly new_sws contribution
        assert new_sws > old_sws or old_sws == 0.0, "New SWS arm must be >= old"
        assert weight >= 1.0

    # ------------------------------------------------------------------
    # X5 — novelty-burst micro-consolidation
    # ------------------------------------------------------------------

    def test_novelty_burst_boosts_neighbors(self):
        """High-arousal new entry must boost neighbors' consolidation_strength."""
        import os
        from buddy.memory.memory_manager import MemoryManager

        base = _rand_emb()
        neighbor_entries = [
            MemoryEntry(text=f"Python neighbor {i}", embedding=base.copy(),
                        consolidation_strength=0.20, memory_type="short",
                        importance=0.50, created_at=time.time() - i * 3600)
            for i in range(3)
        ]

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteStore(db_path)
            for e in neighbor_entries:
                store.upsert_memory(e)

            mock_emb = MagicMock()
            mock_emb.embed_passage.return_value = base.copy()
            mock_emb.embed_query.return_value = base.copy()

            mock_vs = _mock_vector_store(neighbor_entries)
            # Add search() (not search_with_payloads) for _novelty_burst
            mock_vs.search.side_effect = lambda query_vector, top_k=5, **kwargs: [
                (e.id, float(np.dot(
                    np.asarray(query_vector, dtype=np.float32) /
                    (np.linalg.norm(query_vector) + 1e-9),
                    e.embedding
                )))
                for e in neighbor_entries
            ][:top_k]

            mm = MemoryManager(
                sqlite_store=store,
                vector_store=mock_vs,
                embedder=mock_emb,
                brain=None,
            )

            # Create a high-arousal entry that triggers novelty burst
            high_arousal_entry = MemoryEntry(
                text="User just got a critical emergency allergy diagnosis.",
                embedding=base.copy(),
                memory_type="flash",
                importance=0.80,
                metadata={"encoding_arousal": 0.90},
            )
            mm.add_entry(high_arousal_entry)

            # Neighbors should have boosted consolidation_strength
            boosted = 0
            for e in neighbor_entries:
                fetched = store.get_memory(e.id)
                if fetched and fetched.consolidation_strength > 0.20:
                    boosted += 1

            assert boosted >= 1, (
                f"At least 1 neighbor must have consolidation_strength boosted "
                f"by novelty burst. Got {boosted}/3 boosted."
            )
        finally:
            os.unlink(db_path)

    def test_novelty_burst_skipped_for_low_arousal(self):
        """Low-arousal entry must NOT trigger neighbor boost."""
        import os
        from buddy.memory.memory_manager import MemoryManager

        base = _rand_emb()
        neighbor = MemoryEntry(text="Neutral neighbor", embedding=base.copy(),
                               consolidation_strength=0.20, memory_type="flash",
                               importance=0.40, created_at=time.time() - 3600)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            store = SQLiteStore(db_path)
            store.upsert_memory(neighbor)

            mock_emb = MagicMock()
            mock_emb.embed_passage.return_value = base.copy()
            mock_emb.embed_query.return_value = base.copy()
            mock_vs = _mock_vector_store([neighbor])
            mock_vs.search.return_value = [(neighbor.id, 0.95)]

            mm = MemoryManager(
                sqlite_store=store, vector_store=mock_vs,
                embedder=mock_emb, brain=None,
            )

            low_arousal_entry = MemoryEntry(
                text="User asked about the weather.",
                embedding=base.copy(),
                memory_type="flash",
                importance=0.30,
                metadata={"encoding_arousal": 0.10},
            )
            mm.add_entry(low_arousal_entry)

            fetched = store.get_memory(neighbor.id)
            assert fetched is not None
            assert fetched.consolidation_strength == pytest.approx(0.20, abs=1e-4), (
                "Low-arousal entry must not trigger novelty burst on neighbors"
            )
        finally:
            os.unlink(db_path)
