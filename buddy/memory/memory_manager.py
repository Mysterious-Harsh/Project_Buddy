# buddy/memory/memory_manager.py
"""
MemoryManager — integration layer between Brain and the memory subsystem.

Responsibilities (v2):
  - SQLite is the source of truth for all memory entries.
  - VectorStore/Qdrant is a best-effort retrieval index (may be absent).
  - No LLM calls here; accepts pre-classified memory dicts from Brain.
  - Provides Brain-friendly candidate retrieval via search_candidates().
  - Owns the ConsolidationController lifecycle:
      start_consolidation() / stop_consolidation() / is_consolidating.

Sleep/consolidation flow (initiated from main.py):
  main.py detects inactivity → actions.set_sleeping(True)
      → memory_manager.start_consolidation()
      → background thread runs run_consolidation_sleep() with cancel_event
  user wakes buddy → actions.set_sleeping(False)
      → memory_manager.stop_consolidation()  [sets cancel_event, joins thread]
      → buddy responds immediately

Policy:
  - Do NOT introduce new storage backends without migrations/tests.
  - No LLM calls inside MemoryManager.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

import numpy as np

from buddy.logger.logger import get_logger
from buddy.memory.memory_entry import MemoryEntry
from buddy.memory.sqlite_store import SQLiteStore

logger = get_logger("memory_manager")


# ==========================================================
# Protocols (avoid tight coupling to concrete types)
# ==========================================================


class EmbedderLike(Protocol):
    """Embedding provider contract (duck-typed)."""

    def embed_query(self, text: str) -> np.ndarray: ...
    def embed_passage(self, text: str) -> np.ndarray: ...


class VectorStoreLike(Protocol):
    """
    Vector store contract.
    Compatible with buddy/memory/vector_store.py v1.
    """

    def upsert(self, entry: MemoryEntry) -> None: ...

    def search(
        self,
        *,
        query_vector: Sequence[float] | np.ndarray,
        top_k: int = 10,
        memory_types: Optional[List[str]] = None,
        include_deleted: bool = False,
        query_text: Optional[str] = None,
        mode: str = "auto",
        rerank_mode: str = "auto",
    ) -> List[Tuple[str, float]]: ...

    def search_with_payloads(
        self,
        *,
        query_vector: Sequence[float] | np.ndarray,
        query_text: str,
        top_k: int = 10,
        memory_types: Optional[List[str]] = None,
        include_deleted: bool = False,
        mode: str = "auto",
        rerank_mode: str = "auto",
    ) -> List[Tuple[str, float, Optional[Dict[str, Any]]]]: ...


# ==========================================================
# Brain-facing DTOs
# ==========================================================


@dataclass(frozen=True)
class MemoryCandidateLite:
    """
    Lightweight memory candidate returned to the Brain.

    Fields are intentionally redundant (summary + full content) so the Brain
    prompt builder never needs extra fetches in the hot path.
    """

    memory_id: str
    semantic_score: float
    rerank_score: float
    summary: str
    content: str
    source: Optional[str] = None
    created_at_iso: Optional[str] = None
    memory_type: str = "flash"


# ==========================================================
# Re-scoring weights — Phase 3 (v5)
# ==========================================================
#
# Formula (P15): semantic×0.40 + strength×0.25 + rerank×0.15
#                + tier×0.10 + encoding_arousal×0.10
#
# recency/frequency are now folded into consolidation_strength (computed
# by the sleep engine) rather than carried as separate signals here.

W_SEMANTIC  = 0.40
W_STRENGTH  = 0.25  # consolidation_strength written by sleep engine (P14)
W_RERANK    = 0.15  # only when reranker is active
W_TIER      = 0.10
W_AROUSAL   = 0.10  # encoding_arousal from raw user message (P1/Phase 2)

_TIER_BOOST = {"long": 1.0, "short": 0.5, "flash": 0.0}


def _composite_score(
    semantic: float,
    rerank: float,
    consolidation_strength: float,
    tier: str,
    encoding_arousal: float,
) -> float:
    tier_boost = _TIER_BOOST.get(str(tier).lower(), 0.0)
    return (
        semantic               * W_SEMANTIC
        + consolidation_strength * W_STRENGTH
        + rerank               * W_RERANK
        + tier_boost           * W_TIER
        + encoding_arousal     * W_AROUSAL
    )


# ==========================================================
# Small helpers
# ==========================================================


def _now() -> float:
    return float(time.time())


def _iso(ts: Optional[float]) -> Optional[str]:
    """Float timestamp → ISO 8601 string, or None on failure."""
    if ts is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))
    except Exception:
        return None


def _clamp01(x: Any) -> float:
    """Clamp to [0, 1] with safe type conversion."""
    try:
        v = float(x)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, v))


def _summarize(text: str, max_chars: int = 160) -> str:
    """
    Cheap truncation-based summary for display/prompt injection.
    Not semantic — no LLM calls, O(n) on small strings.
    """
    t = (text or "").strip().replace("\n", " ")
    if not t:
        return ""
    return t if len(t) <= max_chars else t[: max_chars - 1].rstrip() + "…"


# ==========================================================
# ConsolidationController
# ==========================================================


class ConsolidationController:
    """
    Manages the lifecycle of a single background consolidation run.

    Design principles:
      - One active run at a time; start() while running is a no-op.
      - cancel() sets a threading.Event that run_consolidation_sleep()
        checks between phases, so wakeup latency is bounded to finishing
        the current cluster's LLM call.
      - The background thread is a daemon so it never blocks process exit.

    Usage:
        ctrl = ConsolidationController(sqlite, vector, brain, embed_fn)
        ctrl.start()          # launch
        ctrl.cancel(wait=True) # stop and wait
    """

    def __init__(
        self,
        sqlite_store: Any,
        vector_store: Any,
        brain: Any,
        embed_fn: Callable[[str], Any],
        *,
        on_done: Optional[Callable[[Any], None]] = None,
    ) -> None:
        """
        Args:
            sqlite_store: SQLiteStore instance.
            vector_store: VectorStoreLike instance (may be None).
            brain:        Brain instance used for LLM summarization calls.
            embed_fn:     Callable[[str], list[float] | np.ndarray].
            on_done:      Optional callback(SleepReport) fired on completion
                          (including early cancellation).
        """
        self._sqlite = sqlite_store
        self._vector = vector_store
        self._brain = brain
        self._embed_fn = embed_fn
        self._on_done = on_done

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_event: Optional[threading.Event] = None

    @property
    def is_running(self) -> bool:
        """True while the consolidation thread is alive."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, budget: Optional[Any] = None, *, dry_run: bool = False) -> bool:
        """
        Launch the background consolidation thread.

        Returns:
            True if a new run was started, False if already running (no-op).
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.debug("consolidation.start: already running — ignored")
                return False

            self._cancel_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run,
                args=(budget, dry_run, self._cancel_event),
                name="buddy-consolidation",
                daemon=True,
            )
            self._thread.start()
            logger.info("consolidation.start: thread launched dry_run=%s", dry_run)
            return True

    def cancel(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        """
        Signal cancellation and optionally block until the thread exits.

        The thread finishes its current atomic sub-operation (one LLM call or
        one DB write) before checking the cancel flag, so wakeup latency is
        bounded and data is never left in a partial state.

        Args:
            wait:    If True, block until thread exits or timeout expires.
            timeout: Max seconds to wait (only relevant when wait=True).
        """
        with self._lock:
            ev = self._cancel_event
            t = self._thread

        if ev is not None:
            ev.set()
            logger.info("consolidation.cancel: cancel_event set")

        if wait and t is not None and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                logger.warning(
                    "consolidation.cancel: thread still alive after %.1fs", timeout
                )
            else:
                logger.info("consolidation.cancel: thread stopped cleanly")

    def _run(
        self,
        budget: Optional[Any],
        dry_run: bool,
        cancel_event: threading.Event,
    ) -> None:
        """Background thread entry point."""
        # Deferred import avoids circular imports at module load time
        from buddy.memory.consolidation_engine import (  # noqa: PLC0415
            run_consolidation,
        )

        logger.info("consolidation.thread: starting")
        report = None
        try:
            report = run_consolidation(
                sqlite_store=self._sqlite,
                vector_store=self._vector,
                brain=self._brain,
                embed=self._embed_fn,
                budget=budget,
                dry_run=dry_run,
                cancel_event=cancel_event,
            )
            was_cancelled = any("cancelled" in e for e in (report.errors or []))
            logger.info(
                "consolidation.thread: done cancelled=%s summarized=%d "
                "tier_updates=%d hard_deleted=%d errors=%d",
                was_cancelled,
                report.summarized,
                report.tier_updates,
                report.hard_deleted,
                len(report.errors),
            )
        except Exception as exc:
            logger.exception("consolidation.thread: crashed err=%r", exc)
        finally:
            # Clear thread reference FIRST so is_running returns False
            # before the callback fires (avoids re-entrant start attempts).
            with self._lock:
                self._thread = None
            # Always fire on_done — even on crash (report will be None).
            # This is the only way main.py can clear sys_state.consolidating
            # when the engine raises an unexpected exception.
            if self._on_done is not None:
                try:
                    self._on_done(report)
                except Exception:
                    logger.exception("consolidation.on_done callback raised")


# ==========================================================
# MemoryManager (v2)
# ==========================================================


class MemoryManager:
    """
    Integration layer between Brain and the memory subsystem.

    Public API (all safe to call from Brain or pipeline code):
      add_text()               — store a raw text entry
      add_entry()              — store a pre-built MemoryEntry
      create_memory_entry()    — build an entry without storing (factory)
      get_entry()              — fetch one entry by ID
      search_candidates()      — vector search returning Brain DTOs

    Consolidation API (called from main.py RuntimeActions):
      start_consolidation()    — launch background consolidation (on sleep)
      stop_consolidation()     — cancel and join thread (on wake)
      is_consolidating         — bool property
    """

    def __init__(
        self,
        *,
        sqlite_store: SQLiteStore,
        vector_store: VectorStoreLike,
        embedder: EmbedderLike,
        brain: Any,
        debug: bool = False,
    ):
        """
        Args:
            sqlite_store: Persistent storage (source of truth).
            vector_store: Optional vector index for semantic search.
            embedder:     Optional text embedder.
            llm:          Legacy alias for brain; ignored if brain is given.
            brain:        Brain instance for consolidation LLM calls.
            debug:        Enable verbose hot-path logging.
        """
        self.sqlite = sqlite_store
        self.vector = vector_store
        self.embedder = embedder
        self.debug = bool(debug)

        # brain supersedes llm for backward compat
        self._brain = brain
        self.min_similarity = self._read_min_similarity_env()

        # ConsolidationController created lazily on first start_consolidation()
        self._consolidation: Optional[ConsolidationController] = None

        logger.info(
            "MemoryManager ready | vector=%s embedder=%s brain=%s "
            "min_score=%.3f debug=%s",
            "on" if self.vector is not None else "off",
            "on" if self.embedder is not None else "off",
            "on" if self._brain is not None else "off",
            float(self.min_similarity),
            self.debug,
        )

    # ------------------------------------------------------------------
    # Consolidation lifecycle
    # ------------------------------------------------------------------

    @property
    def is_consolidating(self) -> bool:
        """True while a background consolidation run is active."""
        return self._consolidation is not None and self._consolidation.is_running

    def start_consolidation(
        self,
        budget: Optional[Any] = None,
        *,
        dry_run: bool = False,
        on_done: Optional[Callable[[Any], None]] = None,
    ) -> bool:
        """
        Start background memory consolidation (called when buddy sleeps).

        Safe to call repeatedly — no-op when already running.

        Args:
            budget:  Optional SleepBudget configuration.
            dry_run: Compute plan but perform no writes.
            on_done: Optional callback(SleepReport) fired on completion.

        Returns:
            True if a new run was launched, False if already running.
        """
        if self.embedder is None:
            logger.warning("consolidation.start: no embedder — skipping")
            return False

        embed_fn: Callable[[str], Any] = self.embedder.embed_passage

        if self._consolidation is None or not self._consolidation.is_running:
            self._consolidation = ConsolidationController(
                sqlite_store=self.sqlite,
                vector_store=self.vector,
                brain=self._brain,
                embed_fn=embed_fn,
                on_done=on_done,
            )

        started = self._consolidation.start(budget=budget, dry_run=dry_run)
        if started:
            logger.info("memory_manager: consolidation started")
        return started

    def stop_consolidation(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        """
        Cancel background consolidation (called when buddy wakes up).

        Blocks until the thread exits so the caller (main loop) can safely
        respond to the user immediately after this returns.

        Args:
            wait:    Block until thread exits (recommended).
            timeout: Max seconds to wait.
        """
        if self._consolidation is not None:
            self._consolidation.cancel(wait=wait, timeout=timeout)
            logger.info("memory_manager: consolidation stopped wait=%s", wait)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read_min_similarity_env(self) -> float:
        """Read BUDDY_MM_MIN_SCORE with safe fallback."""
        try:
            return float(os.getenv("BUDDY_MM_MIN_SCORE", "0.40"))
        except Exception:
            return 0.30

    def _dbg(self, msg: str, *args: Any) -> None:
        """Debug logging — zero overhead when debug=False."""
        if self.debug:
            logger.debug(msg, *args)

    # ------------------------------------------------------------------
    # Entry building
    # ------------------------------------------------------------------

    def create_memory_entry(
        self,
        *,
        text: Optional[str] = None,
        memory: Optional[Dict[str, Any]] = None,
        role: Optional[str] = None,
        source: Optional[str] = None,
        source_turn: Optional[int] = None,
        memory_type: str = "flash",
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
        normalize_text_lower: bool = False,
    ) -> Optional[MemoryEntry]:
        """
        Single factory for all MemoryEntry construction.
        Does NOT write to any storage.

        Returns None if input is empty or explicitly discarded.
        """
        if memory is not None and not isinstance(memory, dict):
            raise TypeError("memory must be a dict or None")

        # ── memory path ───────────────────────────────────────────────
        if memory is not None:
            mem_type = (
                str(memory.get("memory_type", "discard") or "discard")
                .strip()
                .lower()
            )
            mem_text = str(memory.get("memory_text", "") or "").strip()
            if not mem_text or mem_type == "discard":
                return None

            imp = _clamp01(memory.get("salience", 0.0))
            md: Dict[str, Any] = dict(metadata or {})
            if source is not None:
                md.setdefault("source", str(source))

            # P3 (Phase 2): capture protection_tier from Brain ingestion output
            pt = str(memory.get("protection_tier", "normal") or "normal").strip().lower()
            if pt not in ("normal", "critical", "immortal"):
                pt = "normal"
            if pt != "normal":
                md["protection_tier"] = pt

            txt = mem_text.lower() if normalize_text_lower else mem_text
            e = MemoryEntry(text=txt)
            e.embedding = self.embedder.embed_passage(text=txt)
            e.role = role if role is not None else "buddy"
            e.memory_type = mem_type
            e.importance = imp
            e.metadata = md
            if source_turn is not None:
                e.source_turn = int(source_turn)
            return e

        # ── raw text path ─────────────────────────────────────────────
        t = (text or "").strip()
        if not t:
            return None
        if normalize_text_lower:
            t = t.lower()

        md2: Dict[str, Any] = dict(metadata or {})
        if source is not None:
            md2.setdefault("source", str(source))

        e2 = MemoryEntry(text=t)
        e2.embedding = self.embedder.embed_passage(text=t)
        e2.role = role if role is not None else "user"
        e2.memory_type = (memory_type or "flash").strip().lower() or "flash"
        e2.importance = _clamp01(importance)
        e2.metadata = md2
        if source_turn is not None:
            e2.source_turn = int(source_turn)
        return e2

    # ------------------------------------------------------------------
    # Stats / UI helpers
    # ------------------------------------------------------------------

    def tier_counts(self) -> dict:
        """Return {flash, short, long} live counts from SQLite."""
        try:
            return self.sqlite.tier_counts()
        except Exception:
            return {"flash": 0, "short": 0, "long": 0}

    # Most recent memory text surfaced by search_candidates — updated in-place.
    last_retrieved_text: str = ""

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def add_entry(
        self,
        entry: MemoryEntry,
        *,
        upsert_vector: bool = True,
        mark_pending: bool = True,
        discard_if: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Persist a MemoryEntry.

        Write order:
          1) Discard gate  (fast, no I/O)
          2) SQLite upsert (source of truth — always first)
          3) Embedding     (computed if missing and vector upsert requested)
          4) Vector upsert (best-effort index — failure is non-fatal)

        Returns:
            False only when the discard gate fires.
        """
        txt = getattr(entry, "text", "")
        if not txt:
            logger.error("Memory Entry Without text can not be stored")

        if not getattr(entry, "created_at", None):
            entry.created_at = _now()

        # 1) SQLite — persist regardless of vector availability
        try:
            self.sqlite.upsert_memory(entry)
        except Exception as ex:
            self._dbg(
                "sqlite.upsert_memory failed: id=%s err=%r",
                getattr(entry, "id", "?"),
                ex,
            )

        if getattr(entry, "embedding") is None:
            entry.embedding = self.embedder.embed_passage(text=txt)

        # 4) Vector upsert (best-effort)
        try:
            self.vector.upsert(entry)
            try:
                self.sqlite.mark_upserted(entry.id)
            except Exception:
                pass
            if self.debug:
                logger.info(
                    "memory.stored | id=%s type=%s vec=ok",
                    getattr(entry, "id", "?"),
                    getattr(entry, "memory_type", None),
                )
        except Exception as ex:
            if mark_pending:
                try:
                    self.sqlite.mark_pending_upsert(
                        entry.id, reason=f"vector_upsert_failed:{ex}"
                    )
                except Exception:
                    pass
            if self.debug:
                logger.info(
                    "memory.stored | id=%s type=%s vec=pending(upsert_failed)",
                    getattr(entry, "id", "?"),
                    getattr(entry, "memory_type", None),
                )

        # X5 (Phase 5): novelty-burst micro-consolidation
        # High-arousal new memories propagate a small strength boost to their
        # semantic neighbors — simulating the way emotional novelty accelerates
        # consolidation of related knowledge (Cahill & McGaugh 1998).
        self._novelty_burst(entry)

        return True

    def _novelty_burst(self, entry: MemoryEntry) -> None:
        """X5: If encoding_arousal >= 0.7, boost semantic neighbors' consolidation_strength."""
        if self.vector is None or self.embedder is None:
            return
        arousal = float((getattr(entry, "metadata", {}) or {}).get("encoding_arousal", 0.0) or 0.0)
        if arousal < 0.7:
            return
        emb = getattr(entry, "embedding", None)
        if emb is None:
            return
        try:
            hits = self.vector.search(
                query_vector=emb,
                top_k=5,
                include_deleted=False,
                query_text=str(getattr(entry, "text", "") or ""),
                mode="auto",
                rerank_mode="none",
            )
        except Exception:
            return
        updates: List[Tuple[str, float]] = []
        for mid, _sim in hits:
            if mid == entry.id:
                continue
            neighbor = self.sqlite.get_memory(mid)
            if neighbor is None:
                continue
            cur = float(getattr(neighbor, "consolidation_strength", 0.0) or 0.0)
            updates.append((mid, min(1.0, cur + 0.03)))
        if updates:
            try:
                self.sqlite.batch_update_consolidation_strength(updates)
                self._dbg(
                    "novelty_burst | arousal=%.2f boosted=%d neighbors",
                    arousal, len(updates),
                )
            except Exception:
                pass

    def add_text(
        self,
        text: str,
        *,
        role: Optional[str] = "buddy",
        memory_type: str = "flash",
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
        upsert_vector: bool = True,
        mark_pending: bool = True,
        discard_if: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
        source_turn: Optional[int] = None,
    ) -> Optional[MemoryEntry]:
        """
        Convenience wrapper: build an entry from raw text and store it.

        Returns:
            The stored MemoryEntry, or None if text is empty / discarded.
        """
        e = self.create_memory_entry(
            text=text,
            role=role,
            memory_type=memory_type,
            importance=importance,
            metadata=metadata,
            source=source,
            source_turn=source_turn,
        )
        if e is None:
            return None
        ok = self.add_entry(
            e,
            upsert_vector=upsert_vector,
            mark_pending=mark_pending,
            discard_if=discard_if,
        )
        return e if ok else None

    # def create_memory_entry(
    #     self,
    #     *,
    #     text: Optional[str] = None,
    #     memory: Optional[Dict[str, Any]] = None,
    #     role: Optional[str] = None,
    #     source: Optional[str] = None,
    #     source_turn: Optional[int] = None,
    #     memory_type: str = "flash",
    #     importance: float = 0.5,
    #     metadata: Optional[Dict[str, Any]] = None,
    #     normalize_text_lower: bool = False,
    # ) -> Optional[MemoryEntry]:
    #     """
    #     Public factory alias — build without storing.
    #     Kept for API compatibility with callers that pre-build entries.
    #     """
    #     e = self._build_entry(
    #         text=text,
    #         memory=memory,
    #         role=role,
    #         source=source,
    #         source_turn=source_turn,
    #         memory_type=memory_type,
    #         importance=importance,
    #         metadata=metadata,
    #         normalize_text_lower=normalize_text_lower,
    #     )
    #     if self.debug and e is not None:
    #         logger.info(
    #             "memory.entry_built | id=%s role=%s type=%s imp=%.3f",
    #             getattr(e, "id", "?"),
    #             getattr(e, "role", None),
    #             getattr(e, "memory_type", None),
    #             float(getattr(e, "importance", 0.0) or 0.0),
    #         )
    #     return e

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_entry(self, memory_id: str) -> Optional[MemoryEntry]:
        """Fetch a single entry from SQLite by ID."""
        return self.sqlite.get_memory(str(memory_id))

    def search_candidates(
        self,
        *,
        query_text: str,
        top_k: int = 8,
        memory_types: Optional[List[str]] = None,
        mode: str = "auto",
        rerank_mode: str = "auto",
        include_deleted: bool = False,
    ) -> List[MemoryCandidateLite]:
        """
        Retrieve top-k memory candidates for a query text.

        Pipeline:
          1) Embed query via embedder
          2) Vector search (payloads preferred; falls back to ID-only search)
          3) Hydrate full entries from SQLite (source of truth)
          4) Touch each result and return Brain-friendly DTOs

        Returns empty list when vector store or embedder is unavailable.
        """
        qt = (query_text or "").strip()
        if not qt or self.vector is None or self.embedder is None:
            return []

        t0 = time.perf_counter()

        # 1) Embed
        try:
            qv = np.asarray(self.embedder.embed_query(qt), dtype=np.float32).reshape(-1)
        except Exception as ex:
            self._dbg("embed_query failed: err=%r", ex)
            return []
        if qv.size == 0:
            return []

        # 2) Vector search (prefer payloads for rerank score + source field)
        try:
            hits = self.vector.search_with_payloads(
                query_vector=qv,
                query_text=qt,
                top_k=top_k,
                memory_types=memory_types,
                include_deleted=include_deleted,
                mode=mode,
                rerank_mode=rerank_mode,
            )
        except Exception:
            base = self.vector.search(
                query_vector=qv,
                top_k=top_k,
                memory_types=memory_types,
                include_deleted=include_deleted,
                query_text=qt,
                mode=mode,
                rerank_mode=rerank_mode,
            )
            hits = [(mid, float(sc), None) for mid, sc in base]

        if not hits:
            return []

        # 3) Hydrate from SQLite (preserve ranking order)
        hydrated: List[Tuple[MemoryEntry, float, Optional[Dict[str, Any]]]] = []
        sqlite_get = self.sqlite.get_memory
        for mid, sc, pl in hits:
            e = sqlite_get(mid)
            if e is not None:
                hydrated.append((e, float(sc), pl))

        if not hydrated:
            return []

        # 4) Build Brain DTOs with composite re-scoring
        now_ts = time.time()
        scored: List[Tuple[float, MemoryCandidateLite]] = []
        sqlite_touch = self.sqlite.touch
        _touch_ids: List[str] = []  # collect for batched touch after scoring

        for e, sc, pl in hydrated:
            _touch_ids.append(e.id)

            rerank_score = 0.0
            source = None
            if isinstance(pl, dict):
                rr = pl.get("_rerank")
                if isinstance(rr, dict):
                    try:
                        rerank_score = float(rr.get("score", 0.0) or 0.0)
                    except Exception:
                        pass
                sv = pl.get("source")
                source = sv if isinstance(sv, str) else None

            # P14/P15: composite score using sleep-persisted consolidation_strength
            strength = float(getattr(e, "consolidation_strength", 0.0) or 0.0)
            arousal = float(
                (getattr(e, "metadata", {}) or {}).get("encoding_arousal", 0.0) or 0.0
            )
            final = _composite_score(
                semantic=float(sc),
                rerank=rerank_score,
                consolidation_strength=strength,
                tier=str(getattr(e, "memory_type", "flash") or "flash"),
                encoding_arousal=arousal,
            )

            scored.append((
                final,
                MemoryCandidateLite(
                    memory_id=str(e.id),
                    semantic_score=float(sc),
                    rerank_score=final,  # expose composite as rerank_score for downstream
                    summary=_summarize(e.text),
                    content=str(e.text),
                    source=source,
                    created_at_iso=_iso(getattr(e, "created_at", None)),
                    memory_type=str(getattr(e, "memory_type", "flash") or "flash"),
                )
            ))

        # Re-sort by composite score
        scored.sort(key=lambda x: x[0], reverse=True)

        # P16: 1-hop spreading activation
        # Top-3 scoring hits → reuse stored embedding (no re-inference) →
        # secondary vector search → promote semantically adjacent memories.
        _SPREAD_DAMP   = 0.4
        _N_ACTIVATORS  = min(3, len(scored))
        _SPREAD_TOP_K  = 5
        spread_seen: set = {c.memory_id for _, c in scored}
        spread_pool: List[Tuple[float, MemoryCandidateLite]] = []

        # Build id→entry map once so spreading activation can reuse stored embeddings
        # instead of calling embed_query (model inference) per activator.
        _entry_map: Dict[str, MemoryEntry] = {e.id: e for e, _, _ in hydrated}

        if self.vector is not None:
            for act_score, act_cand in scored[:_N_ACTIVATORS]:
                # Use stored embedding — avoids 3 extra embed_query model calls.
                _act_entry = _entry_map.get(act_cand.memory_id)
                _stored_emb = getattr(_act_entry, "embedding", None) if _act_entry else None
                if _stored_emb is None:
                    continue
                act_qv = np.asarray(_stored_emb, dtype=np.float32).reshape(-1)
                if act_qv.size == 0:
                    continue
                try:
                    spread_hits = self.vector.search(
                        query_vector=act_qv,
                        top_k=_SPREAD_TOP_K,
                        include_deleted=False,
                        query_text=act_cand.content,
                        mode=mode,
                        rerank_mode="none",
                    )
                except Exception:
                    continue

                for spread_mid, spread_sim in spread_hits:
                    if spread_mid in spread_seen:
                        continue
                    spread_e = self.sqlite.get_memory(spread_mid)
                    if spread_e is None:
                        continue
                    spread_seen.add(spread_mid)
                    _touch_ids.append(spread_mid)
                    spread_final = _SPREAD_DAMP * float(act_score) * float(spread_sim)
                    spread_pool.append((
                        spread_final,
                        MemoryCandidateLite(
                            memory_id=spread_mid,
                            semantic_score=float(spread_sim),
                            rerank_score=spread_final,
                            summary=_summarize(spread_e.text),
                            content=str(spread_e.text),
                            source=None,
                            created_at_iso=_iso(getattr(spread_e, "created_at", None)),
                            memory_type=str(
                                getattr(spread_e, "memory_type", "flash") or "flash"
                            ),
                        ),
                    ))

        if spread_pool:
            scored = sorted(scored + spread_pool, key=lambda x: x[0], reverse=True)
            self._dbg("spreading_activation | added=%d new candidates", len(spread_pool))

        out = [c for _, c in scored[:top_k]]

        # Record top result for UI display (InfoPane "last memory" line)
        if out:
            self.last_retrieved_text = out[0].content[:120]

        # Batch touch all accessed memories (primary + spread) in one pass
        for _mid in _touch_ids:
            try:
                sqlite_touch(_mid)
            except Exception:
                pass

        if self.debug:
            logger.info(
                "memory.search | q=%r hits=%d hydrated=%d returned=%d dt=%.3fs "
                "mode=%s rerank=%s",
                qt,
                len(hits),
                len(hydrated),
                len(out),
                time.perf_counter() - t0,
                mode,
                rerank_mode,
            )

        return out
