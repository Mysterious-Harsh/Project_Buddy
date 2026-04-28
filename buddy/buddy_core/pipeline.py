# buddy/buddy_core/pipeline.py
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Awaitable, Callable, Optional, Any, Dict, List, Tuple
import threading
from buddy.logger.logger import get_logger
from buddy.brain.action_router import ActionRouter
from buddy.brain.intent_interceptor import (
    interceptor as _interceptor,
    normalize as _normalize,
)
from buddy.buddy_core.smart_truncator import (
    truncate_history,
    truncate_memory,
    truncate_proportional,
)
import json

logger = get_logger("pipeline")

UiInputFn = Callable[[], Awaitable[str]]
UiPrintFn = Callable[[str], Awaitable[None]]

# ==========================================================
# Strict turn/session rules (v1)
# ==========================================================
#
# session_id:
#   - stable id for the process lifetime (until restart)
#   - owned by state._session_id
#
# turn_id:
#   - unique id for this handle_turn invocation (identity, correlation)
#   - NOT used for ordering
#
# turn_index (turn_seq):
#   - strictly increasing integer counter within this session
#   - owned by state._turn_counter
#   - MUST NOT be derived from time.time()
#
# source:
#   - input channel enum-like string: typed|voice|tool|system|unknown
#
# source_turn (on MemoryEntry):
#   - set to turn_index when memory is created
#


# ==========================================================
# Small deterministic helpers
# ==========================================================


def _new_turn_id() -> str:
    return f"t_{uuid.uuid4().hex[:10]}"


def _ensure_session_id(state: Any) -> str:
    sid = getattr(state, "_session_id", None)
    if isinstance(sid, str) and sid.strip():
        return sid
    sid = f"s_{uuid.uuid4().hex[:10]}"
    setattr(state, "_session_id", sid)
    return sid


def _next_turn_index(state: Any) -> int:
    """
    Strict monotonic per-session counter:
      1,2,3,...

    (Do NOT use time.time() here; it collides and is not monotonic.)
    """
    v = getattr(state, "_turn_counter", None)
    if not isinstance(v, int) or v < 0:
        v = 0
    v += 1
    setattr(state, "_turn_counter", v)
    return v


def _now_local_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _safe_source(source: str) -> str:
    s = (source or "").strip().lower()
    if s in {"typed", "voice", "tool", "system"}:
        return s
    return "unknown"


# ---------------------------------------------------------------------------
# Encoding arousal (P1 — Phase 2)
# ---------------------------------------------------------------------------
# Lightweight keyword-density arousal signal from the raw user message,
# captured BEFORE Brain compresses it. Stored as metadata.encoding_arousal.
_AROUSAL_KEYWORDS: frozenset = frozenset({
    # urgency / emphasis
    "urgent",
    "critical",
    "emergency",
    "asap",
    "important",
    "never forget",
    "remember always",
    "always remember",
    # high-arousal emotions
    "excited",
    "scared",
    "angry",
    "furious",
    "terrified",
    "thrilled",
    "amazing",
    "awful",
    "terrible",
    "horrible",
    "love",
    "hate",
    "devastated",
    "ecstatic",
    "panic",
    # medical / safety signals
    "allergy",
    "allergic",
    "medication",
    "diagnosis",
    "pain",
    "sick",
    "hospital",
    "surgery",
    "prescription",
    # legal / financial
    "contract",
    "lawsuit",
    "debt",
    "bankrupt",
    "fraud",
})


def _compute_encoding_arousal(text: str) -> float:
    """
    Return float [0.0, 1.0] — emotional/urgency intensity of raw user message.
    Uses word + bigram keyword matching; no LLM required.
    3 or more hits → 1.0 (saturated).
    """
    if not text:
        return 0.0
    lower = text.lower()
    words = re.findall(r"\w+", lower)
    if not words:
        return 0.0
    bigrams = [f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)]
    hits = sum(1 for w in words if w in _AROUSAL_KEYWORDS)
    hits += sum(1 for bg in bigrams if bg in _AROUSAL_KEYWORDS)
    return min(1.0, hits / 3.0)


def _preview(s: str, n: int = 120) -> str:
    t = (s or "").replace("\n", " ").strip()
    if len(t) <= n:
        return t
    return t[:n].rstrip() + "…"


# ==========================================================
# Memory retrieval -> compact LLM context
# ==========================================================


def _get_memory_context_multi(
    mm: Any,
    queries: List[str],
    *,
    top_k: int,
    include_deleted: bool = False,
) -> Tuple[List[Any], str]:
    """
    Run one search per query, merge results by memory_id keeping highest score,
    sort descending, return top_k total.

    Returns:
      (retrieved_list, compact_text_for_llm)
    """
    if mm is None:
        return [], "None"

    queries = [q.strip() for q in (queries or []) if str(q).strip()]
    if not queries:
        return [], "None"

    t0 = time.perf_counter()

    # Collect results from all queries — deduplicate by memory_id, keep max score
    seen: dict = {}  # memory_id → candidate with highest composite_score
    for query in queries:
        try:
            hits = (
                mm.search_candidates(
                    query_text=query,
                    top_k=int(top_k),
                    mode="auto",
                    rerank_mode="auto",
                    include_deleted=include_deleted,
                )
                or []
            )
        except Exception as e:
            logger.debug("mem_search failed for query=%r err=%r", query, e)
            continue

        for candidate in hits:
            mid = getattr(candidate, "memory_id", None)
            if mid is None:
                continue
            # composite_score is the authoritative ranking signal;
            # fall back to semantic_score for any older code paths.
            score = float(
                getattr(candidate, "composite_score", None)
                or getattr(candidate, "semantic_score", 0.0)
            )
            existing = seen.get(mid)
            if existing is None:
                seen[mid] = candidate
            else:
                existing_score = float(
                    getattr(existing, "composite_score", None)
                    or getattr(existing, "semantic_score", 0.0)
                )
                if score > existing_score:
                    seen[mid] = candidate

    if not seen:
        logger.info("mem_search_multi | dt=%.3fs retrieved=0", time.perf_counter() - t0)
        return [], "None"

    # Sort by composite_score descending, trim to top_k
    merged = sorted(
        seen.values(),
        key=lambda c: float(
            getattr(c, "composite_score", None) or getattr(c, "semantic_score", 0.0)
        ),
        reverse=True,
    )[:top_k]

    lines: List[str] = []
    for x in merged:
        memory_text = str(getattr(x, "content", "") or "").strip()
        created = str(getattr(x, "created_at_iso", "") or "").strip()
        tier = str(getattr(x, "memory_type", "flash") or "flash")
        if created:
            lines.append(f"[{tier} | {created}] {memory_text}")
        else:
            lines.append(f"[{tier}] {memory_text}")

    text = "\n".join(lines).strip()
    logger.info(
        "mem_search_multi | dt=%.3fs queries=%d retrieved=%d",
        time.perf_counter() - t0,
        len(queries),
        len(merged),
    )
    return merged, text


# ==========================================================
# Public API
# ==========================================================


async def handle_turn(
    *,
    state: Any,
    source: str,
    user_message: str,
    top_k_memories: int = 12,
    ui_output: UiPrintFn,
    ui_input: UiInputFn,
    interrupt_event: Optional[threading.Event] = None,
    progress_cb: Callable[[str, bool]],
) -> str | None:

    t_total = time.perf_counter()

    src = _safe_source(source)
    user_message = (user_message or "").strip()

    if not user_message:
        logger.warning("handle_turn | empty user_message | src=%s", src)
        return None

    # ── Normalize filler words (applied to all turns) ─────────
    normalized_message = _normalize(user_message)

    # ── Fast-path interceptor (zero LLM calls) ─────────────────
    quick = _interceptor.match(normalized_message)
    if quick is not None:
        reply, success = _interceptor.execute(quick)
        if success:
            logger.info(
                "interceptor_fast_path | src=%s action=%s reply=%r",
                src,
                quick.name,
                reply,
            )
            await ui_output(reply)
            _convs = getattr(getattr(state, "artifacts", None), "conversations", None)
            if _convs is not None:
                _convs.add_user(text=user_message)
                _convs.add_buddy(text=reply)
            return reply
        logger.info(
            "interceptor_fast_path_failed | src=%s action=%s err=%r — falling through"
            " to pipeline",
            src,
            quick.name,
            reply,
        )
        # fall through: full pipeline handles it
    # ─────────────────────────────────────────────────────────

    artifacts = getattr(state, "artifacts", None)
    if artifacts is None or getattr(artifacts, "brain", None) is None:
        logger.warning("handle_turn | missing brain in state.artifacts")
        return None

    brain = artifacts.brain
    brain.set_interrupt(interrupt_event=interrupt_event)
    brain.set_on_token(progress_cb)
    mm = getattr(artifacts, "memory_manager", None)
    conversations = getattr(artifacts, "conversations", None)
    if conversations is None:
        logger.warning("handle_turn | missing conversations buffer in artifacts")
        return None

    # ── Context budget ────────────────────────────────────────
    _base_budget = getattr(state, "context_budget", None)

    # Live pressure adjustment (±1 turn, never half-cut)
    _live_turns = getattr(state, "_live_recent_turns", None)
    if _base_budget is not None:
        if _live_turns is None:
            _live_turns = _base_budget.recent_turns
        try:
            _adjusted = _base_budget.adjusted_for_pressure(current_turns=_live_turns)
            _live_turns = _adjusted.recent_turns
        except Exception:
            _adjusted = _base_budget
    else:
        _adjusted = None

    # Effective per-turn values
    _top_k = _adjusted.top_k_memories if _adjusted else top_k_memories
    _max_history_chars = _adjusted.max_history_chars if _adjusted else 14_000
    _max_memory_chars = _adjusted.max_memory_chars if _adjusted else 8_000
    _max_exec_results_chars = _adjusted.max_exec_chars if _adjusted else 16_000
    _max_tool_output_chars = _adjusted.max_tool_chars if _adjusted else 10_000

    logger.info(
        "handle_turn | budget: tier=%s turns=%d top_k=%d "
        "hist_chars=%d mem_chars=%d exec_chars=%d tool_chars=%d",
        _adjusted.tier if _adjusted else "fallback",
        _live_turns if isinstance(_live_turns, int) else 0,
        _top_k,
        _max_history_chars,
        _max_memory_chars,
        _max_exec_results_chars,
        _max_tool_output_chars,
    )
    # ─────────────────────────────────────────────────────────

    session_id = _ensure_session_id(state)
    turn_id = _new_turn_id()
    turn_index = _next_turn_index(state)

    logger.info(
        "\nHANDLE_TURN_START | sid=%s tid=%s turn=%d src=%s text_len=%d preview=%r",
        session_id,
        turn_id,
        int(turn_index),
        src,
        len(user_message),
        _preview(user_message, 120),
    )

    mem_text = "none"
    recent_conversations = "none"
    retrieved: List[Any] = []

    # ------------------------------------------------------
    # 1) Recent conversation context
    # ------------------------------------------------------
    t0 = time.perf_counter()
    try:
        recent_conversations = (
            conversations.get_recent_conversations() if conversations else ""
        )
    except Exception as ex:
        logger.warning(
            "conv_fetch_failed | sid=%s tid=%s turn=%d err=%r",
            session_id,
            turn_id,
            turn_index,
            ex,
        )
        recent_conversations = ""
    dt_conv = time.perf_counter() - t0

    # ------------------------------------------------------
    # 2) Retrieval gate
    # ------------------------------------------------------

    progress_cb("Remembering", False)
    t0 = time.perf_counter()
    try:
        rg_payload = await asyncio.to_thread(
            brain.run_memory_gate,
            active_task=user_message,
            recent_turns=recent_conversations,
            stream=True,
        )
    except Exception as ex:
        logger.warning(
            "retrieval_gate_failed | sid=%s tid=%s turn=%d err=%r",
            session_id,
            turn_id,
            turn_index,
            ex,
        )
        rg_payload = {"parsed": {}}
    dt_rg = time.perf_counter() - t0

    rg = rg_payload.get("parsed") or {}
    search_queries = rg.get("search_queries") or []
    if isinstance(search_queries, str):
        search_queries = [search_queries]
    search_queries = [str(q).strip() for q in search_queries if str(q).strip()]
    deep_recall = bool(rg.get("deep_recall"))

    logger.info(
        "retrieval_gate | sid=%s tid=%s turn=%d src=%s queries=%d dt=%.3fs",
        session_id,
        turn_id,
        turn_index,
        src,
        len(search_queries),
        dt_rg,
    )
    logger.debug(
        "retrieval_gate_queries | sid=%s tid=%s turn=%d queries=%r",
        session_id,
        turn_id,
        turn_index,
        search_queries,
    )

    # ------------------------------------------------------
    # 3) Memory retrieval (multi-query, merged by max score)
    # ------------------------------------------------------
    t0 = time.perf_counter()
    if search_queries:
        try:
            retrieved, mem_text = await asyncio.to_thread(
                _get_memory_context_multi,
                mm,
                search_queries,
                top_k=_top_k * 2 if deep_recall else _top_k,
                include_deleted=deep_recall,
            )
        except Exception as ex:
            logger.warning(
                "memory_retrieval_failed | sid=%s tid=%s turn=%d queries=%r err=%r",
                session_id,
                turn_id,
                turn_index,
                search_queries,
                ex,
            )
            retrieved, mem_text = [], "none"
    dt_mem = time.perf_counter() - t0

    # ------------------------------------------------------
    # 4) Run Brain prompt
    # ------------------------------------------------------
    logger.info(
        "brain_context | sid=%s tid=%s turn=%d src=%s conv_chars=%d mem_chars=%d"
        " conv_dt=%.3fs rg_dt=%.3fs mem_dt=%.3fs",
        session_id,
        turn_id,
        turn_index,
        src,
        len(recent_conversations or ""),
        len(mem_text or ""),
        dt_conv,
        dt_rg,
        dt_mem,
    )
    logger.debug(
        "brain_context_preview | sid=%s tid=%s"
        " turn=%d\nmemories:\n%s\n\nrecent_turns:\n%s",
        session_id,
        turn_id,
        turn_index,
        mem_text,
        recent_conversations,
    )
    # ── Trim inputs to fit context budget before brain call ──
    recent_conversations = truncate_history(
        recent_conversations or "", _max_history_chars
    )
    mem_text = truncate_memory(mem_text or "", _max_memory_chars)
    if not mem_text or mem_text.strip().lower() in ("none", "null", ""):
        mem_text = (
            "No memories yet — I'm starting fresh. "
            "I'll pay close attention to who this person is and what matters to them."
        )
    # ─────────────────────────────────────────────────────────

    progress_cb("Thinking", False)

    t0 = time.perf_counter()
    payload = await asyncio.to_thread(
        brain.run_brain,
        active_task=user_message,
        recent_turns=recent_conversations,
        memories=mem_text,
        stream=True,
    )
    dt_llm = time.perf_counter() - t0

    logger.info(
        "brain_llm | sid=%s tid=%s turn=%d dt=%.3fs",
        session_id,
        turn_id,
        turn_index,
        dt_llm,
    )

    # ── Touch retrieved memories (invariant #4) ───────────────
    # One place only: bump access_count + consolidation_strength
    # for every memory the Brain actually received this turn.
    # Uses batch_touch — single SQLite commit for all ids.
    if mm is not None and retrieved:
        _touch_ids = [
            str(getattr(c, "memory_id", None))
            for c in retrieved
            if getattr(c, "memory_id", None) is not None
        ]
        if _touch_ids:

            def _touch_all(
                _ids=_touch_ids,
                _store=mm.sqlite,
                _sid=session_id,
                _tid=turn_id,
                _turn=turn_index,
            ):
                try:
                    _store.batch_touch(_ids)
                    logger.info(
                        "touch_done | sid=%s tid=%s turn=%d count=%d",
                        _sid,
                        _tid,
                        _turn,
                        len(_ids),
                    )
                except Exception as _te:
                    logger.debug(
                        "touch_failed | sid=%s tid=%s turn=%d err=%r",
                        _sid,
                        _tid,
                        _turn,
                        _te,
                    )

            threading.Thread(target=_touch_all, daemon=True).start()
    # ─────────────────────────────────────────────────────────

    parsed = payload.get("parsed") or {}
    decision = parsed.get("decision") or {}
    memories_raw = parsed.get("memories") or []

    # Normalize: single dict → list (backward compat with old outputs)
    if isinstance(memories_raw, dict):
        memories_raw = [memories_raw]

    memories_list = [
        m
        for m in memories_raw
        if isinstance(m, dict)
        and str(m.get("memory_type", "discard")).strip().lower() != "discard"
    ]

    # ------------------------------------------------------
    # 5) Memory storage (best-effort, background thread)
    # ------------------------------------------------------
    if mm is not None and memories_list:
        _enc_arousal = _compute_encoding_arousal(user_message)

        def _ingest(
            _mm=mm,
            _items=memories_list,
            _src=src,
            _turn=turn_index,
            _sid=session_id,
            _tid=turn_id,
            _arousal=_enc_arousal,
        ):
            for _mem in _items:
                try:
                    entry = _mm.create_memory_entry(
                        memory=_mem,
                        source=_src,
                        source_turn=_turn,
                        role="buddy",
                        metadata={"encoding_arousal": _arousal},
                    )
                    if entry is not None:
                        _mm.add_entry(entry)
                        logger.info(
                            "memory_ingested | sid=%s tid=%s turn=%d mem_id=%s"
                            " mem_type=%s",
                            _sid,
                            _tid,
                            _turn,
                            getattr(entry, "id", "?"),
                            getattr(entry, "memory_type", "?"),
                        )
                    else:
                        logger.info(
                            "memory_ingested | sid=%s tid=%s turn=%d skipped"
                            " (create_memory_entry returned None)",
                            _sid,
                            _tid,
                            _turn,
                        )
                except Exception as ex:
                    logger.warning(
                        "memory_ingest_failed | sid=%s tid=%s turn=%d err=%r",
                        _sid,
                        _tid,
                        _turn,
                        ex,
                    )

        threading.Thread(target=_ingest, daemon=True).start()

    mode = decision.get("mode")
    response = str(decision.get("response") or "")
    afterthought = str(decision.get("afterthought") or "")
    await ui_output(response)

    if mode == "CHAT":
        conversations.add_user(
            text=user_message,
        )
        conversations.add_buddy(
            text=response,
        )
        if afterthought:
            conversations.add_buddy(text=afterthought)
            await ui_output(afterthought)
    elif mode == "ACTION":
        action_router = ActionRouter(
            brain=brain,
            ui_output=ui_output,
            ui_input=ui_input,
            memory_manager=mm,
        )
        action_result = await action_router.action(
            turn_id=turn_id,
            session_id=session_id,
            planner_instructions=str(decision.get("planner_instructions")),
            user_message=user_message,
            on_token=progress_cb,
            memories=mem_text,
            llm_options={},
        )
        progress_cb("Responding", False)
        responder_instruction = str(
            action_result.get("responder_instruction") or ""
        ).strip()
        execution_results = action_result.get("step_execution_map")

        # ── Trim execution results before responder call ──────
        execution_results = truncate_proportional(
            execution_results or {},
            _max_exec_results_chars,
            max_per_step_chars=_max_tool_output_chars,
        )
        # ─────────────────────────────────────────────────────

        payload = await asyncio.to_thread(
            brain.run_respond,
            active_task=responder_instruction,
            memories=mem_text,
            execution_results=json.dumps(
                execution_results, ensure_ascii=False, indent=2
            ),
            stream=True,
        )
        parsed_respond = payload.get("parsed")
        response = parsed_respond.get("response")
        memory_candidates = parsed_respond.get("memory_candidates", [])
        if response:
            await ui_output(response)
            conversations.add_user(
                text=user_message,
            )
            conversations.add_buddy(
                text=response,
            )
        if memory_candidates and mm:
            progress_cb("Creating Some New Memories 🧠", False)
            _action_arousal = _compute_encoding_arousal(user_message)

            for mem in memory_candidates:
                try:
                    entry = mm.create_memory_entry(
                        memory=mem,
                        source=src,
                        source_turn=turn_index,
                        role="buddy",
                        metadata={"encoding_arousal": _action_arousal},
                    )
                    if entry is not None:
                        mm.add_entry(entry)
                        logger.info(
                            "memory_ingested | sid=%s tid=%s turn=%d mem_id=%s"
                            " mem_type=%s",
                            session_id,
                            turn_id,
                            turn_index,
                            getattr(entry, "id", "?"),
                            getattr(entry, "memory_type", "?"),
                        )
                    else:
                        logger.info(
                            "memory_ingested | sid=%s tid=%s turn=%d skipped"
                            " (create_memory_entry returned None)",
                            session_id,
                            turn_id,
                            turn_index,
                        )
                except Exception as ex:
                    logger.warning(
                        "memory_ingest_failed | sid=%s tid=%s turn=%d err=%r",
                        session_id,
                        turn_id,
                        turn_index,
                        ex,
                    )

    dt_total = time.perf_counter() - t_total
    logger.info(
        "HANDLE_TURN_END | sid=%s tid=%s turn=%d mode=%s reply_len=%d total=%.3fs",
        session_id,
        turn_id,
        int(turn_index),
        mode,
        len(response),
        dt_total,
    )

    return response
