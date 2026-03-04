# sleep_maintenance.py
from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence

from buddy.logger.logger import get_logger
from buddy.memory.consolidation_engine import (
    SleepBudget,
    SleepReport,
    run_consolidation,
)

logger = get_logger("sleep_maintenance")


def run_sleep_maintenance(
    *,
    sqlite_store: Any,  # locked SQLiteStore
    vector_store: Any,  # locked VectorStore
    brain: Any,
    embed: Callable[[str], List[float]],
    budget: Optional[SleepBudget] = None,
    dry_run: bool = False,
) -> SleepReport:
    """
    Call ONLY when Buddy is sleeping/idle.

    Flow:
      1) summarize clusters -> originals soft-deleted immediately
      2) promote/demote tiers
      3) hard delete (purge old soft-deleted summarized + dead traces)

    Operates ONLY on already-stored DB memories.
    """
    b = budget or SleepBudget()

    logger.info(
        "sleep_maintenance.start dry_run=%s max_candidates=%d max_summaries=%d"
        " max_deletes=%d",
        dry_run,
        b.max_candidates,
        b.max_summaries,
        b.max_hard_deletes,
    )

    rep = run_consolidation(
        sqlite_store=sqlite_store,
        vector_store=vector_store,
        brain=brain,
        embed=embed,
        budget=b,
        dry_run=dry_run,
    )

    logger.info(
        "sleep_maintenance.done scanned=%d clusters=%d summarized=%d soft_deleted=%d"
        " tier_updates=%d hard_deleted=%d errors=%d",
        rep.scanned,
        rep.clusters_found,
        rep.summarized,
        rep.soft_deleted_after_summary,
        rep.tier_updates,
        rep.hard_deleted,
        len(rep.errors),
    )

    # Optional: only log errors if present (keeps logs clean)
    if rep.errors:
        for e in rep.errors[:10]:
            logger.warning("sleep_maintenance.error %s", e)

        if len(rep.errors) > 10:
            logger.warning("sleep_maintenance.error more=%d", len(rep.errors) - 10)

    return rep


if __name__ == "__main__":
    from buddy.memory.vector_store import VectorStore
    from buddy.memory.sqlite_store import SQLiteStore
    from buddy.embeddings.embedding_provider import EmbeddingProvider
    from buddy.llm.llama_client import LlamaClient
    from buddy.brain.brain import Brain

    vector_store = VectorStore(
        backend="local",
        local_path=str("/Users/kishan/.buddy/data/qdrant"),
        server=None,
        # keep these stable defaults unless you decide otherwise
        collection="buddy_memories",
        dense_name="dense",
        sparse_name="sparse",
        distance="Cosine",
        prefer_grpc=False,
        debug=True,
    )
    sqlite_store = SQLiteStore(
        db_path=str("/Users/kishan/.buddy/data/mem.sqlite3"),
        debug=True,
    )
    client = LlamaClient(
        model="local-model",
        base_url="http://127.0.0.1:8080",
        timeout=(3.0, 300.0),
        max_retries=1,
        backoff_base=0.35,
        stream_idle_timeout=300.0,
        debug=True,
        session_pool_maxsize=16,
        api_key=None,
    )
    brain = Brain(llm=client, debug=True)

    emb = EmbeddingProvider()

    report = run_sleep_maintenance(
        sqlite_store=sqlite_store,
        vector_store=vector_store,
        embed=lambda text: emb.embed_passage(
            text
        ).tolist(),  # ✅ convert ndarray to list
        brain=brain,
        dry_run=True,  # preview only
    )
    print(report)
    pass
