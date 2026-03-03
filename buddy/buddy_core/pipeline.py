# buddy/buddy_core/pipeline.py
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Any, Dict, List, Tuple
import threading
from buddy.logger.logger import get_logger
import importlib
import json

logger = get_logger("pipeline")

UiInputFn = Callable[[], Awaitable[str]]
UiPrintFn = Callable[[str], Awaitable[None]]

# ==========================================================
# Return DTO
# ==========================================================


@dataclass(frozen=True)
class PipelineResult:
    reply: str
    brain_payload: Dict[str, Any]
    decided_context_ids: List[str]
    decided_memory_ids: List[str]
    trace: Optional[Dict[str, Any]] = None


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


def _preview(s: str, n: int = 120) -> str:
    t = (s or "").replace("\n", " ").strip()
    if len(t) <= n:
        return t
    return t[:n].rstrip() + "…"


def _cfg_nested(cfg: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    cur: Any = cfg
    for k in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(k, {})
    return cur if isinstance(cur, dict) else {}


def _system_state_from_boot_state(state: Any) -> Dict[str, Any]:
    """
    Keep this lightweight. Brain.run_brain_prompt already gets timezone/now_iso,
    so we return minimal flags for future expansion.
    """
    cfg = getattr(state, "config", None)
    cfg = cfg if isinstance(cfg, dict) else {}
    buddy_cfg = _cfg_nested(cfg, "buddy")

    allow_web = bool(buddy_cfg.get("allow_web_search", False))
    allow_local = bool(buddy_cfg.get("allow_local_search", True))
    tz = str(buddy_cfg.get("timezone", "America/Moncton"))

    return {
        "allow_web_search": allow_web,
        "allow_local_search": allow_local,
        "timezone": tz,
        "now_iso": _now_local_iso(),
    }


def load_action_router(
    brain: Any,
    ui_output: UiPrintFn,
    ui_input: UiInputFn,
):
    import buddy.brain.action_router as ar_mod

    importlib.reload(ar_mod)  # pick up code changes
    return ar_mod.ActionRouter(brain=brain, ui_output=ui_output, ui_input=ui_input)


# ==========================================================
# Memory retrieval -> compact LLM context
# ==========================================================


def _get_memory_context(
    mm: Any,
    query: str,
    *,
    top_k: int,
    include_deleted: bool = False,
) -> Tuple[List[Any], str]:
    """
    Returns:
      (retrieved_list, compact_text_for_llm)

    Policy:
      - Never dump huge memory blobs.
      - Include short content + created_at + score.
      - If retrieval fails, return safe "None".
    """
    if mm is None:
        return [], "None"

    qt = (query or "").strip()
    if not qt:
        return [], "None"

    t0 = time.perf_counter()
    try:
        retrieved = (
            mm.search_candidates(
                query_text=qt,
                top_k=int(top_k),
                mode="auto",
                rerank_mode="auto",
                include_deleted=include_deleted,
            )
            or []
        )
    except Exception as e:
        logger.debug("mem_search failed: %r", e)
        return [], "None"
    dt = time.perf_counter() - t0

    lines: List[str] = []
    n = 0
    for x in retrieved:

        memory_text = str(getattr(x, "content", "") or "").strip()
        created = str(getattr(x, "created_at_iso", "") or "").strip()
        score = getattr(x, "rerank_score", None)
        score_s = f"{float(score):.3f}" if score is not None else "-"

        if created:
            lines.append(f"[{created}] {memory_text}")
        else:
            lines.append(f"{memory_text}")
        n += 1

    text = "\n".join(lines).strip()
    # text = _cap_head(text, max_chars)

    logger.info("mem_search | dt=%.3fs retrieved=%d", dt, len(retrieved))
    return retrieved, text


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
    """
    Strict control flow:

    - Run Brain once.
    - If mode == FOLLOWUP:
        - print question
        - wait for user answer
        - loop (same turn_id, same turn_index)
    - Else:
        - print response
        - store ONE conversation turn (original user_message + final reply)
        - return reply

    Notes:
    - EXECUTE: Brain should NOT ask questions; Planner handles missing info.
      But we still support mode=EXECUTE here and return the friendly response.
    """
    t_total = time.perf_counter()

    src = _safe_source(source)
    user_message = (user_message or "").strip()

    if not user_message:
        logger.warning("handle_turn | empty user_message | src=%s", src)
        return None

    artifacts = getattr(state, "artifacts", None)
    if artifacts is None or getattr(artifacts, "brain", None) is None:
        logger.warning("handle_turn | missing brain in state.artifacts")
        return None

    brain = artifacts.brain
    brain.set_interrupt(interrupt_event=interrupt_event)
    mm = getattr(artifacts, "memory_manager", None)
    conversations = getattr(artifacts, "conversations", None)
    if conversations is None:
        logger.warning("handle_turn | missing conversations buffer in artifacts")
        return None

    session_id = _ensure_session_id(state)
    turn_id = _new_turn_id()
    turn_index = _next_turn_index(state)
    sys_state = _system_state_from_boot_state(state)

    logger.info(
        "\nHANDLE_TURN_START | sid=%s tid=%s turn=%d src=%s text_len=%d preview=%r",
        session_id,
        turn_id,
        int(turn_index),
        src,
        len(user_message),
        _preview(user_message, 120),
    )

    # Single-turn: original user message remains the “turn owner”

    """
    Run one Brain turn.

    Pipeline:
      1) Fetch recent conversation context (RAM snapshot)
      2) Run retrieval gate -> (needs_memory, search_query)
      3) If needed, retrieve memory context
      4) Run brain prompt (decision + ingestion)
      5) Optionally ingest memory via MemoryManager

    Returns:
      decision dict (parsed)
    """

    mem_text = "none"
    recent_conversations = "none"

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

    t0 = time.perf_counter()
    try:
        rg_payload = brain.run_memory_gate(
            user_current_message=user_message,
            recent_turns=recent_conversations,
            on_token=progress_cb,
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
    search_query = str(rg.get("search_query") or "").strip()
    ack_message = str(rg.get("ack_message") or "").strip()
    deep_recall = bool(rg.get("deep_recall"))

    logger.info(
        "retrieval_gate | sid=%s tid=%s turn=%d src=%s qlen=%d dt=%.3fs",
        session_id,
        turn_id,
        turn_index,
        src,
        len(search_query),
        dt_rg,
    )
    if logger:
        logger.debug(
            "retrieval_gate_query | sid=%s tid=%s turn=%d query=%r",
            session_id,
            turn_id,
            turn_index,
            search_query,
        )

    # ------------------------------------------------------
    # 3) Memory retrieval (only if needed)
    # ------------------------------------------------------
    t0 = time.perf_counter()
    if search_query:
        progress_cb(ack_message, False)

        try:
            if deep_recall:

                retrieved, mem_text = _get_memory_context(
                    mm, search_query, top_k=top_k_memories * 2, include_deleted=True
                )

            else:
                retrieved, mem_text = _get_memory_context(
                    mm, search_query, top_k=top_k_memories, include_deleted=False
                )

        except Exception as ex:
            logger.warning(
                "memory_retrieval_failed | sid=%s tid=%s turn=%d q=%r err=%r",
                session_id,
                turn_id,
                turn_index,
                search_query,
                ex,
            )
            retrieved, mem_text = None, "none"
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
    if logger:
        logger.debug(
            "brain_context_preview | sid=%s tid=%s"
            " turn=%d\nmemories:\n%s\n\nrecent_turns:\n%s",
            session_id,
            turn_id,
            turn_index,
            mem_text,
            recent_conversations,
        )
    progress_cb("Thinking", False)

    t0 = time.perf_counter()
    payload = brain.run_brain(
        user_current_message=user_message,
        recent_turns=recent_conversations,
        memories=mem_text,
        temperature=0.2,
        stream=True,
        on_token=progress_cb,
    )
    dt_llm = time.perf_counter() - t0

    logger.info(
        "brain_llm | sid=%s tid=%s turn=%d dt=%.3fs",
        session_id,
        turn_id,
        turn_index,
        dt_llm,
    )

    parsed = payload.get("parsed") or {}
    decision = parsed.get("decision") or {}
    ingestion = parsed.get("ingestion") or {}

    # ------------------------------------------------------
    # 5) Memory ingestion (best-effort)
    # ------------------------------------------------------
    mem_type = str(ingestion.get("memory_type", "discard")).strip().lower()
    if mm is not None and mem_type and mem_type != "discard":
        try:
            entry = mm.create_memory_entry(
                ingestion=ingestion,
                source=src,
                source_turn=turn_index,
                role="buddy",
            )
            if entry is not None:
                mm.add_entry(entry)
                logger.info(
                    "memory_ingested | sid=%s tid=%s turn=%d mem_id=%s mem_type=%s",
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
    elif mode == "EXECUTE":
        action_rounter = load_action_router(
            brain=brain, ui_output=ui_output, ui_input=ui_input
        )
        action_result = await action_rounter.action(
            turn_id=turn_id,
            session_id=session_id,
            intent=str(decision.get("intent")),
            user_message=user_message,
            memories=mem_text,
            on_token=progress_cb,
            llm_options={},
        )

        execution_results = action_result.get("step_execution_map")
        payload = brain.run_respond(
            user_current_message=user_message,
            memories=mem_text,
            execution_results=json.dumps(
                execution_results, ensure_ascii=False, indent=2
            ),
            temperature=0.2,
            stream=True,
            on_token=progress_cb,
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
            for mem in memory_candidates:
                try:
                    entry = mm.create_memory_entry(
                        ingestion=mem,
                        source=src,
                        source_turn=turn_index,
                        role="buddy",
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
