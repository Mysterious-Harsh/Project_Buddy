# buddy/memory/memory_entry.py
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class MemoryEntry:
    """
    Atomic durable memory unit.

    Contract:
    - SQLiteStore is the source of truth for text + metadata + lifecycle + flags.
    - VectorStore/Qdrant is an index for embeddings + filterable payload.
    """

    # --------------------------
    # Identity
    # --------------------------
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # --------------------------
    # Core content
    # --------------------------
    text: str = ""
    embedding: Optional[np.ndarray] = None  # (D,) float32; normalized preferred

    # --------------------------
    # Semantic / linguistic signals
    # --------------------------
    # LOCKED: role must be one of {"user","buddy","tool","system","llm","unknown"} (or None)
    role: Optional[str] = None

    # --------------------------
    # Lifecycle & access stats
    # --------------------------
    created_at: float = field(default_factory=time.time)
    last_accessed: Optional[float] = None
    access_count: int = 0

    # --------------------------
    # Memory classification
    # --------------------------
    memory_type: str = "flash"  # flash | short | long
    importance: float = 0.5  # 0..1 (promotion signal)

    # --------------------------
    # Relationships
    # --------------------------
    parent_id: Optional[str] = None
    source_turn: Optional[int] = None

    # --------------------------
    # Operational flags (Qdrant pipeline + deletion)
    # --------------------------
    pending_upsert: int = 0
    upsert_error: Optional[str] = None
    upsert_attempts: int = 0
    last_upsert_at: Optional[float] = None

    deleted: int = 0  # soft delete (SQLite truth)

    # --------------------------
    # Consolidation / summarization pipeline
    # --------------------------
    consolidation_status: str = "candidate"  # {"pending", "candidate", "summarized"}
    consolidated_into_id: Optional[str] = None
    consolidation_error: Optional[str] = None
    last_consolidated_at: Optional[float] = None
    # Cached strength from latest consolidation run — read at recall time (Phase 3).
    # 0.0 = never consolidated or too new. Written by consolidation_engine Phase 0b.
    consolidation_strength: float = 0.0

    # --------------------------
    # Free-form metadata
    # --------------------------
    metadata: Dict[str, Any] = field(default_factory=dict)

    _ALLOWED_CONSOLIDATION = {"pending", "candidate", "summarized"}
    _ALLOWED_ROLES = {"user", "buddy", "tool", "system", "llm", "unknown"}

    # ==================================================
    # Init normalization
    # ==================================================
    def __post_init__(self) -> None:
        # Normalize embedding
        if self.embedding is not None:
            try:
                self.embedding = self._as_np(self.embedding)
            except Exception:
                self.embedding = None

        # role guard (LOCKED set)
        if self.role is not None:
            s = str(self.role).strip().lower()
            self.role = s if s in self._ALLOWED_ROLES else None

        # numeric normalization
        try:
            self.importance = float(self.importance)
        except Exception:
            self.importance = 0.5

        self.access_count = int(self.access_count or 0)
        self.upsert_attempts = int(self.upsert_attempts or 0)

        # timestamps normalization (keep None if missing)
        try:
            self.created_at = float(self.created_at)
        except Exception:
            self.created_at = float(time.time())

        for attr in ("last_accessed", "last_upsert_at", "last_consolidated_at"):
            v = getattr(self, attr, None)
            if v is None:
                continue
            try:
                setattr(self, attr, float(v))
            except Exception:
                setattr(self, attr, None)

        # SQLite-friendly ints (0/1)
        self.pending_upsert = int(bool(self.pending_upsert))
        self.deleted = int(bool(self.deleted))

        # consolidation_strength clamp
        try:
            self.consolidation_strength = float(self.consolidation_strength or 0.0)
        except Exception:
            self.consolidation_strength = 0.0

        # consolidation_status guard (locked to 3 states)
        s = (self.consolidation_status or "candidate").strip().lower()
        self.consolidation_status = (
            s if s in self._ALLOWED_CONSOLIDATION else "candidate"
        )

    # ==================================================
    # Behavior
    # ==================================================
    def touch(self) -> None:
        """Update access metadata when memory is used.
        Also bumps consolidation_strength slightly (P17) — repeated recall
        reinforces a memory, consistent with ACT-R base-level learning.
        """
        now = time.time()
        self.last_accessed = float(now)
        self.access_count += 1
        self.consolidation_strength = min(1.0, self.consolidation_strength + 0.05)

    def promote(self, new_type: str) -> None:
        """Promote memory to a longer-lived store."""
        if new_type not in {"flash", "short", "long"}:
            raise ValueError(f"Invalid memory type: {new_type}")
        self.memory_type = new_type

    # ==================================================
    # Scoring helpers (light utilities)
    # ==================================================
    def recency_score(self, now: Optional[float] = None, decay: float = 30.0) -> float:
        """Exponential decay based on last access (fallback to created_at)."""
        base = self.last_accessed if self.last_accessed else self.created_at
        if not base:
            return 0.0
        if now is None:
            now = time.time()
        age = max(0.0, float(now - base))
        return float(np.exp(-age / float(decay)))

    def frequency_score(self, max_log: float = 3.0) -> float:
        """Log-scaled access frequency."""
        if self.access_count <= 0:
            return 0.0
        return float(min(1.0, np.log1p(self.access_count) / float(max_log)))

    # ==================================================
    # Serialization
    # ==================================================
    def to_dict(self, *, include_embedding: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "role": self.role,
            "created_at": float(self.created_at),
            "last_accessed": (
                float(self.last_accessed) if self.last_accessed is not None else None
            ),
            "access_count": int(self.access_count),
            "memory_type": self.memory_type,
            "importance": float(self.importance),
            "parent_id": self.parent_id,
            "source_turn": self.source_turn,
            # operational
            "pending_upsert": int(self.pending_upsert),
            "upsert_error": self.upsert_error,
            "upsert_attempts": int(self.upsert_attempts),
            "last_upsert_at": (
                float(self.last_upsert_at) if self.last_upsert_at is not None else None
            ),
            "deleted": int(self.deleted),
            # consolidation
            "consolidation_status": self.consolidation_status,
            "consolidated_into_id": self.consolidated_into_id,
            "consolidation_error": self.consolidation_error,
            "last_consolidated_at": (
                float(self.last_consolidated_at)
                if self.last_consolidated_at is not None
                else None
            ),
            "consolidation_strength": float(self.consolidation_strength),
            # metadata
            "metadata": dict(self.metadata or {}),
        }

        if include_embedding:
            out["embedding"] = (
                self.embedding.tolist() if self.embedding is not None else None
            )

        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        d = dict(data)

        emb = d.get("embedding", None)
        if emb is not None and not isinstance(emb, np.ndarray):
            try:
                d["embedding"] = np.asarray(emb, dtype=np.float32).reshape(-1)
            except Exception:
                d["embedding"] = None

        return cls(**d)

    # ==================================================
    # Internal helpers
    # ==================================================
    @staticmethod
    def _as_np(vec: Any) -> np.ndarray:
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            raise ValueError("Embedding vector is empty")
        return arr

    # ==================================================
    # Debug
    # ==================================================
    def __repr__(self) -> str:
        return (
            "MemoryEntry("
            f"id={self.id[:8]}, "
            f"text={self.text!r}, "
            f"role={self.role}, "
            f"type={self.memory_type}, "
            f"access={self.access_count}, "
            f"deleted={self.deleted}, "
            f"pending={self.pending_upsert}, "
            f"consolidation={self.consolidation_status}"
            ")"
        )


# ==========================================================
# In-file tests
# ==========================================================
if __name__ == "__main__":
    print("🧪 memory_entry.py tests starting...")

    e = MemoryEntry(text="hello buddy")
    assert e.id
    assert e.text == "hello buddy"
    assert e.embedding is None
    assert e.memory_type == "flash"
    assert e.deleted == 0
    assert e.pending_upsert == 0
    assert e.consolidation_status == "candidate"
    assert e.role is None

    # Role normalization (valid)
    e_role = MemoryEntry(text="x", role="USER")
    assert e_role.role == "user"

    # Role normalization (invalid -> None)
    e_bad_role = MemoryEntry(text="x", role="admin")  # not allowed
    assert e_bad_role.role is None

    # Embedding normalize
    e2 = MemoryEntry(text="x", embedding=[0.1, 0.2, 0.3])  # type: ignore
    assert isinstance(e2.embedding, np.ndarray)
    assert e2.embedding.dtype == np.float32
    assert e2.embedding.shape == (3,)

    # Touch
    before = e.access_count
    e.touch()
    assert e.access_count == before + 1
    assert e.last_accessed is not None

    # Promote
    e.promote("short")
    assert e.memory_type == "short"

    # Serialization
    d = e2.to_dict()
    assert isinstance(d["embedding"], list)
    e3 = MemoryEntry.from_dict(d)
    assert isinstance(e3.embedding, np.ndarray)

    # Consolidation fields exist
    assert e3.consolidation_status in {"candidate", "pending", "summarized"}

    print("✅ memory_entry.py tests passed")
