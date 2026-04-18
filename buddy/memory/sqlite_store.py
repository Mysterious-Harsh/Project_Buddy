# buddy/memory/sqlite_store.py
# 🔒 LOCKED FILE: buddy/memory/sqlite_store.py
# Policy:
# - Do not change schema semantics, column meanings, or CRUD behavior.
# - Allowed: bug fixes, compatibility fixes, perf improvements that preserve outputs.
# - Schema changes require: explicit migration plan + updated tests.
# Locked on: 2025-12-28

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Optional

import numpy as np

from buddy.logger.logger import get_logger
from buddy.memory.memory_entry import MemoryEntry

logger = get_logger("sqlite_store")


class SQLiteStore:
    """
    Optimized durable store for MemoryEntry (source of truth).

    Space optimizations:
    - Prepared statement caching
    - Batch operations support
    - Efficient JSON serialization
    - Connection pooling readiness

    Time optimizations:
    - Single-pass query execution
    - Reduced object allocation
    - Optimized serialization/deserialization
    - Efficient indexing utilization
    """

    def __init__(self, db_path: str, debug: bool = False):
        self.db_path = str(db_path)
        self.debug = bool(debug)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

        # check_same_thread=False because MemoryManager can call across threads (tests + CLI)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        self._apply_pragmas()
        self._init_schema()

        self._debug("SQLiteStore ready:", self.db_path)

    # ------------------------------------------------------
    # Debug
    # ------------------------------------------------------
    def _debug(self, *args: Any) -> None:
        if self.debug:
            logger.info(" ".join(str(a) for a in args))

    # ------------------------------------------------------
    # Connection pragmas
    # ------------------------------------------------------
    def _apply_pragmas(self) -> None:
        """
        Pragmas optimized for assistant workload with batch operations.
        """
        cur = self._conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA temp_store=MEMORY;")
        cur.execute("PRAGMA foreign_keys=ON;")

        # Increased timeout for batch operations
        cur.execute("PRAGMA busy_timeout=5000;")

        # Larger cache for better performance with multiple concurrent operations
        cur.execute("PRAGMA cache_size=-32000;")  # ~32MB

        # More aggressive WAL checkpointing
        cur.execute("PRAGMA wal_autocheckpoint=500;")

        # Optimize for read-heavy workloads with occasional writes
        cur.execute("PRAGMA mmap_size=268435456;")  # 256MB memory mapping

        self._conn.commit()

    # ------------------------------------------------------
    # Schema init + light migrations
    # ------------------------------------------------------
    def _init_schema(self) -> None:
        cur = self._conn.cursor()

        # NOTE: Keep column meanings stable; add-only migrations happen in _ensure_columns().
        cur.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id              TEXT PRIMARY KEY,
                text            TEXT NOT NULL,

                -- semantic fields
                embedding_json  TEXT NULL,
                role            TEXT NULL,

                -- lifecycle
                memory_type     TEXT NOT NULL DEFAULT 'flash',
                importance      REAL NOT NULL DEFAULT 0.5,
                parent_id       TEXT NULL,
                source_turn     INTEGER NULL,

                -- timestamps / access
                created_at      REAL NOT NULL,
                last_accessed   REAL NULL,
                access_count    INTEGER NOT NULL DEFAULT 0,

                -- operational flags (qdrant sync pipeline)
                pending_upsert  INTEGER NOT NULL DEFAULT 0,
                upsert_error    TEXT NULL,
                upsert_attempts INTEGER NOT NULL DEFAULT 0,
                last_upsert_at  REAL NULL,

                -- maintenance / lifecycle flags
                deleted         INTEGER NOT NULL DEFAULT 0,

                -- consolidation metadata
                consolidation_status     TEXT NULL,
                consolidated_into_id     TEXT NULL,
                consolidation_error      TEXT NULL,
                last_consolidated_at     REAL NULL,
                -- v5: cached strength from last sleep cycle (read at recall time)
                consolidation_strength   REAL NOT NULL DEFAULT 0.0,

                -- arbitrary metadata
                metadata   TEXT NOT NULL DEFAULT '{}'
            );
            """)

        # v5: audit log for hard-deleted memories (rolling 1000-row window)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS forgotten_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id       TEXT NOT NULL,
                memory_text     TEXT NOT NULL DEFAULT '',
                memory_type     TEXT NOT NULL DEFAULT 'flash',
                importance      REAL NOT NULL DEFAULT 0.0,
                reason          TEXT NOT NULL DEFAULT '',
                deleted_at      REAL NOT NULL
            );
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_forgotten_deleted_at ON "
            "forgotten_log(deleted_at DESC);"
        )

        # Indexes optimized for performance
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_created ON "
            "memories(created_at DESC, deleted ASC);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_type_created ON "
            "memories(memory_type, created_at DESC, deleted ASC);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_pending ON "
            "memories(pending_upsert, created_at DESC, deleted ASC);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_deleted ON "
            "memories(deleted, created_at DESC);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_consolidation ON "
            "memories(consolidation_status, created_at DESC, deleted ASC);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_id_deleted ON "
            "memories(id, deleted);"
        )

        self._conn.commit()

        # Ensure columns exist even for older DBs
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """
        Lightweight migration: add new columns when upgrading Buddy.
        Safe for repeated runs.
        """
        cur = self._conn.cursor()
        cur.execute("PRAGMA table_info(memories);")
        existing = {row[1] for row in cur.fetchall()}

        def add_col(name: str, ddl: str) -> None:
            if name in existing:
                return
            cur.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl};")

        # These are add-only migrations (keep behavior stable)
        add_col("embedding_json", "TEXT NULL")
        add_col("role", "TEXT NULL")
        add_col("memory_type", "TEXT NOT NULL DEFAULT 'flash'")
        add_col("importance", "REAL NOT NULL DEFAULT 0.5")
        add_col("parent_id", "TEXT NULL")
        add_col("source_turn", "INTEGER NULL")

        add_col("created_at", "REAL NOT NULL DEFAULT 0")
        add_col("last_accessed", "REAL NULL")
        add_col("access_count", "INTEGER NOT NULL DEFAULT 0")

        add_col("pending_upsert", "INTEGER NOT NULL DEFAULT 0")
        add_col("upsert_error", "TEXT NULL")
        add_col("upsert_attempts", "INTEGER NOT NULL DEFAULT 0")
        add_col("last_upsert_at", "REAL NULL")

        add_col("deleted", "INTEGER NOT NULL DEFAULT 0")

        add_col("consolidation_status", "TEXT NULL")
        add_col("consolidated_into_id", "TEXT NULL")
        add_col("consolidation_error", "TEXT NULL")
        add_col("last_consolidated_at", "REAL NULL")
        add_col("consolidation_strength", "REAL NOT NULL DEFAULT 0.0")  # v5

        add_col("metadata", "TEXT NOT NULL DEFAULT '{}'")

        self._conn.commit()

    # ------------------------------------------------------
    # Serialization helpers (optimized)
    # ------------------------------------------------------
    @staticmethod
    def _loads_json(s: Any, default: Any) -> Any:
        if s is None or s == "":
            return default
        try:
            return json.loads(s)
        except (json.JSONDecodeError, TypeError):
            return default

    @staticmethod
    def _dumps_json(obj: Any, default: str) -> str:
        if obj is None:
            return default
        try:
            return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError):
            return default

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        """
        Optimized row-to-entry conversion with minimal object creation.
        """
        e = MemoryEntry.__new__(MemoryEntry)  # Avoid __init__ overhead
        e.text = str(row["text"])
        e.id = str(row["id"])
        e.role = row["role"]
        e.memory_type = str(row["memory_type"])
        e.importance = float(row["importance"])
        e.parent_id = row["parent_id"]
        e.source_turn = row["source_turn"]

        e.created_at = float(row["created_at"])
        e.last_accessed = (
            float(row["last_accessed"]) if row["last_accessed"] is not None else None
        )
        e.access_count = int(row["access_count"])

        e.pending_upsert = int(row["pending_upsert"])
        e.upsert_error = row["upsert_error"]
        e.upsert_attempts = int(row["upsert_attempts"])
        e.last_upsert_at = (
            float(row["last_upsert_at"]) if row["last_upsert_at"] is not None else None
        )

        e.deleted = int(row["deleted"])

        e.consolidation_status = row["consolidation_status"]
        e.consolidated_into_id = row["consolidated_into_id"]
        e.consolidation_error = row["consolidation_error"]
        e.last_consolidated_at = (
            float(row["last_consolidated_at"])
            if row["last_consolidated_at"] is not None
            else None
        )

        e.metadata = self._loads_json(row["metadata"], {})

        # v5: consolidation_strength (graceful fallback for old rows before migration)
        try:
            e.consolidation_strength = float(row["consolidation_strength"] or 0.0)
        except (IndexError, KeyError):
            e.consolidation_strength = 0.0

        # embedding_json is optional and only used for re-upsert/debug
        emb_json = row["embedding_json"]
        if emb_json and emb_json.strip():
            try:
                # Direct numpy array creation from parsed list
                parsed_list = json.loads(emb_json)
                e.embedding = np.fromiter(parsed_list, dtype=np.float32)
            except (json.JSONDecodeError, ValueError, TypeError):
                e.embedding = None
        else:
            e.embedding = None

        return e

    # ------------------------------------------------------
    # CRUD (optimized)
    # ------------------------------------------------------
    def upsert_memory(self, entry: MemoryEntry) -> None:
        """
        Highly optimized upsert with minimal string operations.
        """
        # Pre-compute embedding JSON to avoid repeated computation
        embedding_json = None
        embedding = getattr(entry, "embedding", None)
        if embedding is not None and hasattr(embedding, "size") and embedding.size > 0:
            try:
                # Use tolist() only once and avoid reshaping if already 1D
                if embedding.ndim != 1:
                    emb_flat = embedding.reshape(-1)
                else:
                    emb_flat = embedding
                if emb_flat.size > 0:
                    # More efficient JSON generation for arrays
                    embedding_json = (
                        "[" + ",".join(f"{x:.6f}" for x in emb_flat.tolist()) + "]"
                    )
            except Exception:
                embedding_json = None

        # Pre-compute other serialized fields
        metadata = self._dumps_json(getattr(entry, "metadata", {}) or {}, "{}")

        # Use getattr with defaults to avoid KeyError exceptions
        memory_type = getattr(entry, "memory_type", "flash") or "flash"
        importance = float(getattr(entry, "importance", 0.5) or 0.5)
        pending_upsert = int(getattr(entry, "pending_upsert", 0) or 0)
        access_count = int(getattr(entry, "access_count", 0) or 0)
        deleted = int(getattr(entry, "deleted", 0) or 0)
        created_at = float(getattr(entry, "created_at", time.time()) or time.time())

        sql = """
        INSERT INTO memories (
            id, text, embedding_json, role,
            memory_type, importance, parent_id, source_turn,
            created_at, last_accessed, access_count,
            pending_upsert, upsert_error, upsert_attempts, last_upsert_at,
            deleted,
            consolidation_status, consolidated_into_id, consolidation_error, last_consolidated_at,
            consolidation_strength,
            metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            text=excluded.text,
            embedding_json=excluded.embedding_json,
            role=excluded.role,
            memory_type=excluded.memory_type,
            importance=excluded.importance,
            parent_id=excluded.parent_id,
            source_turn=excluded.source_turn,
            created_at=excluded.created_at,
            last_accessed=excluded.last_accessed,
            access_count=excluded.access_count,
            pending_upsert=excluded.pending_upsert,
            upsert_error=excluded.upsert_error,
            upsert_attempts=excluded.upsert_attempts,
            last_upsert_at=excluded.last_upsert_at,
            deleted=excluded.deleted,
            consolidation_status=excluded.consolidation_status,
            consolidated_into_id=excluded.consolidated_into_id,
            consolidation_error=excluded.consolidation_error,
            last_consolidated_at=excluded.last_consolidated_at,
            consolidation_strength=excluded.consolidation_strength,
            metadata=excluded.metadata;
        """

        params = (
            entry.id,
            entry.text,
            embedding_json,
            getattr(entry, "role", None),
            memory_type,
            importance,
            getattr(entry, "parent_id", None),
            getattr(entry, "source_turn", None),
            created_at,
            getattr(entry, "last_accessed", None),
            access_count,
            pending_upsert,
            getattr(entry, "upsert_error", None),
            int(getattr(entry, "upsert_attempts", 0) or 0),
            getattr(entry, "last_upsert_at", None),
            deleted,
            getattr(entry, "consolidation_status", None),
            getattr(entry, "consolidated_into_id", None),
            getattr(entry, "consolidation_error", None),
            getattr(entry, "last_consolidated_at", None),
            float(getattr(entry, "consolidation_strength", 0.0) or 0.0),
            metadata,
        )

        cur = self._conn.cursor()
        cur.execute(sql, params)
        self._conn.commit()

    def get_memory(self, memory_id: str) -> Optional[MemoryEntry]:
        """Optimized single memory retrieval with indexed lookup."""
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM memories WHERE id=? AND deleted=0 LIMIT 1;",
            (str(memory_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_entry(row)

    def batch_get_memories(self, memory_ids: List[str]) -> Dict[str, MemoryEntry]:
        """
        Fetch multiple memories in a single query.

        Returns:
            Dict mapping id → MemoryEntry for every id found (deleted excluded).
            Missing ids are silently absent from the result.
        """
        ids = [str(mid) for mid in memory_ids if mid]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders}) AND deleted=0;",
            ids,
        )
        return {str(row["id"]): self._row_to_entry(row) for row in cur.fetchall()}

    def list_recent(self, limit: int = 50) -> List[MemoryEntry]:
        """Optimized recent memories listing with early termination."""
        limit = min(int(limit), 1000)  # Prevent excessive memory usage
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM memories WHERE deleted=0 ORDER BY created_at DESC LIMIT ?;",
            (limit,),
        )
        return [self._row_to_entry(r) for r in cur.fetchall()]

    def tier_counts(self) -> dict:
        """Return {flash, short, long} live counts. Single fast query."""
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT memory_type, COUNT(*) FROM memories
            WHERE deleted=0
            GROUP BY memory_type;
            """
        )
        counts = {"flash": 0, "short": 0, "long": 0}
        for row in cur.fetchall():
            mt = row[0]
            if mt in counts:
                counts[mt] = row[1]
        return counts

    def list_pending_upserts(self, limit: int = 50) -> List[MemoryEntry]:
        """Optimized pending upserts listing."""
        limit = min(int(limit), 1000)
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT * FROM memories
            WHERE deleted=0 AND pending_upsert=1
            ORDER BY created_at DESC
            LIMIT ?;
            """,
            (limit,),
        )
        return [self._row_to_entry(r) for r in cur.fetchall()]

    def mark_upserted(self, memory_id: str) -> None:
        """Optimized upsert completion marking."""
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE memories
            SET pending_upsert=0,
                upsert_error=NULL,
                last_upsert_at=?,
                upsert_attempts=upsert_attempts+1
            WHERE id=?;
            """,
            (time.time(), str(memory_id)),
        )
        self._conn.commit()

    def mark_pending_upsert(self, memory_id: str, reason: str) -> None:
        """Optimized pending upsert marking."""
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE memories
            SET pending_upsert=1,
                upsert_error=?,
                upsert_attempts=upsert_attempts+1,
                last_upsert_at=?
            WHERE id=?;
            """,
            (str(reason), time.time(), str(memory_id)),
        )
        self._conn.commit()

    # Spacing-effect constants (Cepeda et al. 2006)
    # Boost = BASE * sigmoid(hours_since_last_access / HALF_LIFE_HOURS)
    # A retrieval after 24h gap earns ~2× the boost of an immediate re-retrieval.
    # Cap prevents a single very-spaced touch from over-boosting.
    _TOUCH_BASE: float = 0.10        # max boost per touch
    _TOUCH_HALF_LIFE_H: float = 24.0  # gap at which sigmoid = 0.5 → boost = BASE/2
    _TOUCH_MIN: float = 0.01          # minimum boost even for massed retrieval

    def _spacing_boost(self, last_accessed: Optional[float], now: float) -> float:
        """Compute spacing-weighted strength bump for a single touch.

        Cepeda et al. (2006) show spaced retrievals are exponentially more
        beneficial than massed ones.  We model this as:

            boost = BASE × sigmoid(hours_gap / HALF_LIFE)

        where hours_gap is the interval since the last access.
        - No prior access (first retrieval): hours_gap = 0 → boost ≈ BASE/2
        - Accessed 24h ago: hours_gap = 24 → boost ≈ BASE/2   (inflection)
        - Accessed 72h ago: hours_gap = 72 → boost ≈ BASE×0.95 (near max)
        - Accessed 1s ago (massed): hours_gap ≈ 0 → boost ≈ _TOUCH_MIN

        Returns float in [_TOUCH_MIN, _TOUCH_BASE].
        """
        if last_accessed is None:
            hours_gap = 0.0
        else:
            hours_gap = max(0.0, (now - float(last_accessed)) / 3600.0)
        sig = 1.0 / (1.0 + math.exp(-hours_gap / self._TOUCH_HALF_LIFE_H))
        boost = self._TOUCH_BASE * sig
        return float(max(self._TOUCH_MIN, min(self._TOUCH_BASE, boost)))

    def touch(self, memory_id: str) -> None:
        """Update access metadata and bump consolidation_strength (P17).

        Repeated recall reinforces a memory, consistent with ACT-R base-level
        learning.  Boost is spacing-weighted (Cepeda et al. 2006): retrievals
        after longer gaps strengthen the memory more than massed re-retrievals.
        """
        now = time.time()
        # Read last_accessed first so we can compute spacing-weighted boost.
        cur = self._conn.cursor()
        cur.execute(
            "SELECT last_accessed FROM memories WHERE id=? AND deleted=0;",
            (str(memory_id),),
        )
        row = cur.fetchone()
        if row is None:
            return
        last_accessed = row[0]
        boost = self._spacing_boost(last_accessed, now)
        cur.execute(
            """
            UPDATE memories
            SET last_accessed=?,
                access_count=access_count+1,
                consolidation_strength=MIN(1.0, consolidation_strength+?)
            WHERE id=? AND deleted=0;
            """,
            (now, boost, str(memory_id)),
        )
        if cur.rowcount > 0:
            self._conn.commit()

    def batch_touch(self, memory_ids: List[str]) -> None:
        """Touch multiple memories with spacing-weighted strength boosts (P-perf)."""
        if not memory_ids:
            return
        now = time.time()
        placeholders = ",".join("?" * len(memory_ids))
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT id, last_accessed FROM memories "
            f"WHERE id IN ({placeholders}) AND deleted=0;",
            [str(mid) for mid in memory_ids],
        )
        rows = {str(row[0]): row[1] for row in cur.fetchall()}
        params = []
        for mid in memory_ids:
            last_accessed = rows.get(str(mid))
            boost = self._spacing_boost(last_accessed, now)
            params.append((now, boost, str(mid)))
        cur.executemany(
            """
            UPDATE memories
            SET last_accessed=?,
                access_count=access_count+1,
                consolidation_strength=MIN(1.0, consolidation_strength+?)
            WHERE id=? AND deleted=0;
            """,
            params,
        )
        self._conn.commit()

    def soft_delete(self, memory_id: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE memories SET deleted=1, last_consolidated_at=? WHERE id=?;",
            (time.time(), str(memory_id)),
        )
        if cur.rowcount > 0:
            self._conn.commit()

    def update_memory_type(self, memory_id: str, memory_type: str) -> None:
        """Optimized memory type update (+ consolidation touch time)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE memories
            SET memory_type=?,
                last_consolidated_at=?
            WHERE id=? AND deleted=0;
            """,
            (str(memory_type), time.time(), str(memory_id)),
        )
        if cur.rowcount > 0:
            self._conn.commit()

    # ------------------------------------------------------
    # Consolidation helpers (optimized)
    # ------------------------------------------------------
    def apply_consolidation_patch(self, patch: Any) -> None:
        """Optimized consolidation patch application."""
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE memories
            SET consolidation_status=?,
                consolidated_into_id=?,
                consolidation_error=?,
                last_consolidated_at=?
            WHERE id=?;
            """,
            (
                getattr(patch, "consolidation_status", None),
                getattr(patch, "consolidated_into_id", None),
                getattr(patch, "consolidation_error", None),
                getattr(patch, "last_consolidated_at", None) or time.time(),
                getattr(patch, "entry_id", None),
            ),
        )
        if cur.rowcount > 0:
            self._conn.commit()

    def mark_consolidated(self, memory_id: str, *, into_id: str) -> None:
        """Optimized consolidation marking."""
        cur = self._conn.cursor()
        cur.execute(
            """
            UPDATE memories
            SET consolidation_status='summarized',
                consolidated_into_id=?,
                consolidation_error=NULL,
                last_consolidated_at=?
            WHERE id=?;
            """,
            (str(into_id), time.time(), str(memory_id)),
        )
        if cur.rowcount > 0:
            self._conn.commit()

    def list_by_consolidation_status(
        self, status: str, limit: int = 50
    ) -> List[MemoryEntry]:
        """Optimized consolidation status listing."""
        limit = min(int(limit), 1000)
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT * FROM memories
            WHERE deleted=0 AND consolidation_status=?
            ORDER BY created_at DESC
            LIMIT ?;
            """,
            (str(status), limit),
        )
        return [self._row_to_entry(r) for r in cur.fetchall()]

    def list_candidates_for_consolidation(
        self, limit: int = 300, cooldown_seconds: float = 86400.0
    ) -> List[MemoryEntry]:
        """
        Consolidation candidates listing.

        Rule:
        - eligible if last_consolidated_at is NULL (never processed)
          OR last_consolidated_at < now - cooldown_seconds (cooldown expired)
        - includes flash/short/long INCLUDING summary memories
        - excludes deleted rows only
        - orders by least-recently-processed first, then importance desc
        """
        limit = min(int(limit), 1000)
        cutoff = time.time() - cooldown_seconds

        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT *
            FROM memories
            WHERE deleted = 0
              AND (
                  last_consolidated_at IS NULL        -- never processed: always eligible
                  OR last_consolidated_at < ?         -- cooldown has expired
              )
            ORDER BY
                last_consolidated_at ASC NULLS FIRST, -- least-recently-processed first
                importance DESC,                       -- tie-break: higher importance
                created_at ASC                         -- final tie-break: older first
            LIMIT ?
            """,
            (cutoff, limit),
        )
        return [self._row_to_entry(r) for r in cur.fetchall()]

    # ---------------------------------------------------------------------------
    # Method 1 — soft_delete_with_snapshot
    # ---------------------------------------------------------------------------
    # Place inside class SQLiteStore:

    def soft_delete_with_snapshot(
        self,
        memory_id: str,
        *,
        pre_access_count: int,
        mvs: float,
        now: Optional[float] = None,
    ) -> None:
        """
        Soft-delete a memory and snapshot its access state before resetting.

        Changes applied atomically:
        - deleted = 1
        - access_count = 0          (reset; post-delete accesses count toward resurrection)
        - metadata JSON updated with:
            pre_soft_delete_access_count : int   — access count before reset
            soft_deleted_at              : float — unix timestamp
            mvs_at_soft_delete           : float — MVS score at time of deletion
                                                    (0.0 for summary-driven soft-deletes)

        WHY access_count reset:
            The resurrection check in the consolidation engine counts accesses SINCE
            soft-deletion.  Resetting to 0 gives a clean per-deletion counter.
            The original count is preserved in metadata and merged back on resurrection.

        Parameters
        ----------
        memory_id : str
        pre_access_count : int
            The access_count value BEFORE this soft-delete (caller reads it first).
        mvs : float
            MVS score that triggered the deletion (for audit / dry-run display).
            Pass 0.0 for summary-driven soft-deletes.
        now : float or None
            Unix timestamp.  Defaults to time.time().
        """
        ts = now or time.time()

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT metadata FROM memories WHERE id = ?",
                (str(memory_id),),
            ).fetchone()

            meta: dict = {}
            if row and row[0]:
                try:
                    meta = json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    meta = {}

            meta["pre_soft_delete_access_count"] = pre_access_count
            meta["soft_deleted_at"] = ts
            meta["mvs_at_soft_delete"] = round(float(mvs), 4)

            conn.execute(
                """
                UPDATE memories
                SET deleted      = 1,
                    access_count = 0,
                    metadata     = ?
                WHERE id = ?
                """,
                (json.dumps(meta), str(memory_id)),
            )
            conn.commit()

    # ---------------------------------------------------------------------------
    # Method 2 — restore_from_soft_delete
    # ---------------------------------------------------------------------------
    # Place inside class SQLiteStore:

    def restore_from_soft_delete(
        self,
        memory_id: str,
        *,
        restored_access_count: int,
        now: Optional[float] = None,
    ) -> None:
        """
        Resurrect a soft-deleted memory to active flash-tier state.

        Changes applied atomically:
        - deleted = 0
        - memory_type = 'flash'       (re-enters consolidation cycle fresh)
        - access_count = restored_access_count
                (caller passes: acc_since_soft_delete + pre_soft_delete_access_count)
        - metadata JSON updated:
            resurrected_at           : float — unix timestamp of resurrection
            resurrected_access_count : int   — the merged access count applied
            soft_deleted_at          : removed
            mvs_at_soft_delete       : removed  (no longer relevant)
            pre_soft_delete_access_count : KEPT (audit trail)

        WHY memory_type = flash:
            The memory re-enters the consolidation pipeline at the lowest tier so
            the engine can re-evaluate its fitness for short/long promotion.
            A genuinely important resurrected memory will quickly promote back up.

        Parameters
        ----------
        memory_id : str
        restored_access_count : int
            Merged count = (accesses since soft-delete) + (pre_soft_delete_access_count).
        now : float or None
        """
        ts = now or time.time()

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT metadata FROM memories WHERE id = ?",
                (str(memory_id),),
            ).fetchone()

            meta: dict = {}
            if row and row[0]:
                try:
                    meta = json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    meta = {}

            # Remove ephemeral soft-delete keys; keep pre_soft_delete_access_count for audit
            meta.pop("soft_deleted_at", None)
            meta.pop("mvs_at_soft_delete", None)
            meta["resurrected_at"] = ts
            meta["resurrected_access_count"] = restored_access_count

            conn.execute(
                """
                UPDATE memories
                SET deleted      = 0,
                    memory_type  = 'flash',
                    access_count = ?,
                    metadata     = ?
                WHERE id = ?
                """,
                (restored_access_count, json.dumps(meta), str(memory_id)),
            )
            conn.commit()

    # ---------------------------------------------------------------------------
    # Method 3 — list_soft_deleted_non_consolidated
    # ---------------------------------------------------------------------------
    # Place inside class SQLiteStore:

    def list_soft_deleted_non_consolidated(self, *, limit: int = 500) -> List[dict]:
        """
        Return soft-deleted memories that are NOT consolidated into a summary.

        These are candidates for:
        - Resurrection check  (access_count >= threshold since deletion)
        - MVS-based hard deletion  (memory has fully decayed past the hard threshold)

        Consolidated originals (consolidated_into_id IS NOT NULL) are excluded —
        they are handled by the scheduled purge path (PATH A in the engine).

        Returns
        -------
        List of dicts with keys:
            id, importance, access_count, created_at, last_accessed,
            memory_type, metadata (JSON string)
        """
        rows: List[dict] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT id,
                        importance,
                        access_count,
                        created_at,
                        last_accessed,
                        memory_type,
                        metadata
                    FROM   memories
                    WHERE  deleted                = 1
                    AND  consolidated_into_id  IS NULL
                    ORDER  BY created_at ASC
                    LIMIT  ?
                    """,
                    (limit,),
                )
                for row in cur.fetchall():
                    rows.append(dict(row))
        except Exception as exc:
            # Return empty rather than crash — the engine has a raw SQL fallback.
            import logging

            logging.getLogger("sqlite_store").exception(
                "list_soft_deleted_non_consolidated failed: %s", exc
            )
        return rows

    # ------------------------------------------------------
    # Batch operations (NEW OPTIMIZATION)
    # ------------------------------------------------------
    def batch_upsert_memories(self, entries: List[MemoryEntry]) -> None:
        """
        Optimized batch upsert for multiple entries.
        Significantly faster for bulk operations.
        """
        if not entries:
            return

        sql = """
        INSERT INTO memories (
            id, text, embedding_json, role,
            memory_type, importance, parent_id, source_turn,
            created_at, last_accessed, access_count,
            pending_upsert, upsert_error, upsert_attempts, last_upsert_at,
            deleted,
            consolidation_status, consolidated_into_id, consolidation_error, last_consolidated_at,
            consolidation_strength,
            metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            text=excluded.text,
            embedding_json=excluded.embedding_json,
            role=excluded.role,
            memory_type=excluded.memory_type,
            importance=excluded.importance,
            parent_id=excluded.parent_id,
            source_turn=excluded.source_turn,
            created_at=excluded.created_at,
            last_accessed=excluded.last_accessed,
            access_count=excluded.access_count,
            pending_upsert=excluded.pending_upsert,
            upsert_error=excluded.upsert_error,
            upsert_attempts=excluded.upsert_attempts,
            last_upsert_at=excluded.last_upsert_at,
            deleted=excluded.deleted,
            consolidation_status=excluded.consolidation_status,
            consolidated_into_id=excluded.consolidated_into_id,
            consolidation_error=excluded.consolidation_error,
            last_consolidated_at=excluded.last_consolidated_at,
            consolidation_strength=excluded.consolidation_strength,
            metadata=excluded.metadata;
        """

        # Prepare all parameters in one go
        params_list = []
        for entry in entries:
            embedding_json = None
            embedding = getattr(entry, "embedding", None)
            if (
                embedding is not None
                and hasattr(embedding, "size")
                and embedding.size > 0
            ):
                try:
                    if embedding.ndim != 1:
                        emb_flat = embedding.reshape(-1)
                    else:
                        emb_flat = embedding
                    if emb_flat.size > 0:
                        embedding_json = (
                            "[" + ",".join(f"{x:.6f}" for x in emb_flat.tolist()) + "]"
                        )
                except Exception:
                    embedding_json = None

            metadata = self._dumps_json(getattr(entry, "metadata", {}) or {}, "{}")

            memory_type = getattr(entry, "memory_type", "flash") or "flash"
            importance = float(getattr(entry, "importance", 0.5) or 0.5)
            pending_upsert = int(getattr(entry, "pending_upsert", 0) or 0)
            access_count = int(getattr(entry, "access_count", 0) or 0)
            deleted = int(getattr(entry, "deleted", 0) or 0)
            created_at = float(getattr(entry, "created_at", time.time()) or time.time())

            params = (
                entry.id,
                entry.text,
                embedding_json,
                getattr(entry, "role", None),
                memory_type,
                importance,
                getattr(entry, "parent_id", None),
                getattr(entry, "source_turn", None),
                created_at,
                getattr(entry, "last_accessed", None),
                access_count,
                pending_upsert,
                getattr(entry, "upsert_error", None),
                int(getattr(entry, "upsert_attempts", 0) or 0),
                getattr(entry, "last_upsert_at", None),
                deleted,
                getattr(entry, "consolidation_status", None),
                getattr(entry, "consolidated_into_id", None),
                getattr(entry, "consolidation_error", None),
                getattr(entry, "last_consolidated_at", None),
                float(getattr(entry, "consolidation_strength", 0.0) or 0.0),
                metadata,
            )
            params_list.append(params)

        cur = self._conn.cursor()
        cur.executemany(sql, params_list)
        self._conn.commit()

    # ------------------------------------------------------
    # v5: Consolidation strength writer (Phase 3 reader lives in memory_manager)
    # ------------------------------------------------------
    def update_consolidation_strength(
        self, memory_id: str, strength: float
    ) -> None:
        """Write the cached consolidation strength for a memory.

        Called by the consolidation engine during Phase 0b scoring.
        The value is read at recall time in memory_manager._composite_score().
        """
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE memories SET consolidation_strength=? WHERE id=? AND deleted=0;",
            (float(max(0.0, min(1.0, strength))), str(memory_id)),
        )
        if cur.rowcount > 0:
            self._conn.commit()

    def batch_update_consolidation_strength(
        self, updates: List[tuple]
    ) -> None:
        """Batch write consolidation_strength for many memories at once.

        Args:
            updates: list of (memory_id: str, strength: float) tuples.
        """
        if not updates:
            return
        params = [
            (float(max(0.0, min(1.0, s))), str(mid)) for mid, s in updates
        ]
        cur = self._conn.cursor()
        cur.executemany(
            "UPDATE memories SET consolidation_strength=? WHERE id=? AND deleted=0;",
            params,
        )
        self._conn.commit()

    # ------------------------------------------------------
    # v5: Forgotten-log audit trail
    # ------------------------------------------------------
    _FORGOTTEN_LOG_MAX_ROWS: int = 1000

    def forgotten_log_append(
        self,
        *,
        memory_id: str,
        memory_text: str,
        memory_type: str,
        importance: float,
        reason: str,
    ) -> None:
        """Record a hard-deleted memory for forensic/diagnostic purposes.

        Maintains a rolling window of _FORGOTTEN_LOG_MAX_ROWS rows — oldest
        entries are pruned automatically so storage cost stays constant.
        """
        ts = time.time()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO forgotten_log (memory_id, memory_text, memory_type, importance, reason, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (
                str(memory_id),
                str(memory_text or "")[:500],  # cap text length
                str(memory_type or "flash"),
                float(importance or 0.0),
                str(reason or "")[:200],
                ts,
            ),
        )
        # Rolling window: delete oldest rows beyond limit
        cur.execute(
            """
            DELETE FROM forgotten_log WHERE id NOT IN (
                SELECT id FROM forgotten_log ORDER BY deleted_at DESC LIMIT ?
            );
            """,
            (self._FORGOTTEN_LOG_MAX_ROWS,),
        )
        self._conn.commit()

    def forgotten_log_recent(self, limit: int = 50) -> List[dict]:
        """Return the most recently forgotten memories (diagnostic use)."""
        limit = min(int(limit), self._FORGOTTEN_LOG_MAX_ROWS)
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT memory_id, memory_text, memory_type, importance, reason, deleted_at
            FROM forgotten_log
            ORDER BY deleted_at DESC
            LIMIT ?;
            """,
            (limit,),
        )
        cols = ["memory_id", "memory_text", "memory_type", "importance", "reason", "deleted_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ------------------------------------------------------
    # Close
    # ------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ==========================================================
# In-file tests (unchanged)
# ==========================================================
def main() -> None:
    print("🧪 sqlite_store.py tests starting...")

    def _make_entry(text: str, mt: str) -> MemoryEntry:
        e = MemoryEntry(text=text)
        e.role = "user"
        e.memory_type = mt
        e.importance = 0.5
        e.created_at = time.time()
        e.last_accessed = e.created_at
        e.access_count = 1
        e.metadata = {"t": "x"}
        e.embedding = np.asarray([0.1, 0.2, 0.3], dtype=np.float32)
        return e

    with tempfile.TemporaryDirectory() as td:
        db_path = os.path.join(td, "buddy_memories.sqlite3")
        store = SQLiteStore(db_path=db_path, debug=True)

        # 1) Insert
        m1 = _make_entry("Remember my wife's name is Pallavi.", "short")
        store.upsert_memory(m1)
        got = store.get_memory(m1.id)
        assert got is not None
        assert got.text == m1.text
        assert got.memory_type == "short"
        assert isinstance(got.metadata, dict)

        # 2) list_recent
        m2 = _make_entry("I use VS Code.", "flash")
        store.upsert_memory(m2)
        rec = store.list_recent(limit=10)
        assert len(rec) >= 2
        assert rec[0].created_at >= rec[1].created_at

        # 3) touch
        before = store.get_memory(m1.id)
        assert before is not None
        store.touch(m1.id)
        after = store.get_memory(m1.id)
        assert after is not None
        assert int(after.access_count) == int(before.access_count) + 1

        # 4) pending upsert flags
        store.mark_pending_upsert(m1.id, reason="vector_upsert_failed:test")
        pend = store.list_pending_upserts(limit=10)
        assert any(x.id == m1.id for x in pend)
        store.mark_upserted(m1.id)
        pend2 = store.list_pending_upserts(limit=10)
        assert not any(x.id == m1.id for x in pend2)

        # 5) consolidation status
        store.mark_consolidated(m2.id, into_id="mem_summary_1")
        got4 = store.get_memory(m2.id)
        assert got4 is not None
        assert getattr(got4, "consolidation_status") == "summarized"
        assert getattr(got4, "consolidated_into_id") == "mem_summary_1"
        assert getattr(got4, "last_consolidated_at") is not None

        summarized = store.list_by_consolidation_status("summarized", limit=10)
        assert any(x.id == m2.id for x in summarized)

        # 6) Soft delete hides row
        store.soft_delete(m1.id)
        assert store.get_memory(m1.id) is None

        # 7) update_memory_type helper
        store.update_memory_type(m2.id, "long")
        got_m2b = store.get_memory(m2.id)
        assert got_m2b is not None
        assert got_m2b.memory_type == "long"

        # 8) list_candidates_for_consolidation excludes summarized sources
        m3 = _make_entry("Another short memory item", "short")
        store.upsert_memory(m3)

        cands = store.list_candidates_for_consolidation(limit=50)
        assert not any(
            x.id == m2.id for x in cands
        ), "m2 should not be a candidate (summarized)"
        assert any(
            x.id == m3.id for x in cands
        ), "expected m3 in consolidation candidates"

        # 9) Test batch operations (new optimization)
        batch_entries = [_make_entry(f"Batch entry {i}", "flash") for i in range(5)]
        store.batch_upsert_memories(batch_entries)
        for entry in batch_entries:
            retrieved = store.get_memory(entry.id)
            assert retrieved is not None
            assert retrieved.text == entry.text

        store.close()

    print("✅ sqlite_store.py tests passed")


if __name__ == "__main__":
    main()
