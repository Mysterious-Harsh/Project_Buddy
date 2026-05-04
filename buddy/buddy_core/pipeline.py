from __future__ import annotations

import asyncio
import re
import time
import threading
import uuid
import json
from datetime import datetime as _dt, timedelta, date
from typing import Awaitable, Callable, Optional, Any, Dict, List

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

logger = get_logger("pipeline")

UiInputFn = Callable[[], Awaitable[str]]
UiPrintFn = Callable[[str], Awaitable[None]]

# ==============================================================================
# Module-level constants & precompiled patterns (Performance Fix)
# ==============================================================================
_VALID_SOURCES = frozenset({"typed", "voice", "tool", "system"})
_WORD_RE = re.compile(r"\w+")
_NULL_MEM_TEXT = frozenset({"none", "null", "", " "})

# Precompile static regex patterns for date resolution (Performance Fix)
_DATE_PATTERNS_MULTI = [
    (re.compile(r"\bthis morning\b", re.I), "morning of {today}"),
    (re.compile(r"\bthis afternoon\b", re.I), "afternoon of {today}"),
    (re.compile(r"\bthis evening\b", re.I), "evening of {today}"),
    (re.compile(r"\blast night\b", re.I), "night of {yesterday}"),
    (re.compile(r"\bthis week\b", re.I), "week of {week_start}"),
    (re.compile(r"\blast week\b", re.I), "week of {last_week_start}"),
    (re.compile(r"\bnext week\b", re.I), "week of {next_week_start}"),
    (re.compile(r"\bthis month\b", re.I), "{month_year}"),
    (re.compile(r"\blast month\b", re.I), "{last_month_year}"),
    (re.compile(r"\bnext month\b", re.I), "{next_month_year}"),
    (re.compile(r"\bthis year\b", re.I), "{year}"),
    (re.compile(r"\blast year\b", re.I), "{last_year}"),
    (re.compile(r"\bnext year\b", re.I), "{next_year}"),
    (re.compile(r"\ba week ago\b", re.I), "{week_ago}"),
    (re.compile(r"\ba month ago\b", re.I), "{month_ago}"),
    (re.compile(r"\ba year ago\b", re.I), "{year_ago}"),
    (re.compile(r"\b(\d+)\s+days?\s+ago\b", re.I), None),  # Handled separately
    (re.compile(r"\bin\s+(\d+)\s+days?\b", re.I), None),
    (re.compile(r"\b(\d+)\s+days?\s+from\s+now\b", re.I), None),
    (re.compile(r"\b(\d+)\s+weeks?\s+ago\b", re.I), None),
    (re.compile(r"\bin\s+(\d+)\s+weeks?\b", re.I), None),
]

_DATE_PATTERNS_SINGLE = [
    (re.compile(r"\btonight\b", re.I), "evening of {today}"),
    (re.compile(r"\btoday\b", re.I), "{today}"),
    (re.compile(r"\byesterday\b", re.I), "{yesterday}"),
    (re.compile(r"\btomorrow\b", re.I), "{tomorrow}"),
]

_WEEKDAY_RE = re.compile(
    r"\b(?:last|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)

_AROUSAL_KEYWORDS: frozenset = frozenset({
    "urgent",
    "critical",
    "emergency",
    "asap",
    "important",
    "never forget",
    "remember always",
    "always remember",
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
    "allergy",
    "allergic",
    "medication",
    "diagnosis",
    "pain",
    "sick",
    "hospital",
    "surgery",
    "prescription",
    "contract",
    "lawsuit",
    "debt",
    "bankrupt",
    "fraud",
})


# ==============================================================================
# Deterministic Helpers
# ==============================================================================
def new_turn_id() -> str:
    return f"t{uuid.uuid4().hex[:10]}"


def _ensure_session_id(state: Any) -> str:
    # FIX: Consistent attribute naming (_session_id everywhere)
    sid = getattr(state, "_session_id", None)
    if isinstance(sid, str) and sid.strip():
        return sid
    sid = f"s{uuid.uuid4().hex[:10]}"
    setattr(state, "_session_id", sid)
    return sid


def _next_turn_index(state: Any) -> int:
    v = getattr(state, "_turn_counter", None)
    if not isinstance(v, int) or v < 0:
        v = 0
    v += 1
    setattr(state, "_turn_counter", v)
    return v


def _safe_source(source: str) -> str:
    s = (source or "").strip().lower()
    return s if s in _VALID_SOURCES else "unknown"


def _shift_year(d: date, delta: int) -> date:
    # FIX: Leap-year safe shift
    try:
        return d.replace(year=d.year + delta)
    except ValueError:
        return (d + timedelta(days=366)).replace(year=d.year + delta)


def _compute_encoding_arousal(text: str) -> float:
    if not text:
        return 0.0
    lower = text.lower()
    words = _WORD_RE.findall(lower)
    if not words:
        return 0.0
    # Single pass bigram generation & counting
    hits = sum(1 for w in words if w in _AROUSAL_KEYWORDS)
    bigrams = (f"{words[i]} {words[i+1]}" for i in range(len(words) - 1))
    hits += sum(1 for bg in bigrams if bg in _AROUSAL_KEYWORDS)
    return min(1.0, hits / 3.0)


def _resolve_relative_dates(text: str, now: _dt) -> str:
    if not text:
        return text

    today = now.date()

    def iso(d: date) -> str:
        return d.strftime("%Y-%m-%d")

    def mon_year(d: date) -> str:
        return d.strftime("%B %Y")

    replacements = {
        "today": iso(today),
        "yesterday": iso(today - timedelta(days=1)),
        "tomorrow": iso(today + timedelta(days=1)),
        "week_start": iso(today - timedelta(days=today.weekday())),
        "last_week_start": iso(today - timedelta(days=today.weekday() + 7)),
        "next_week_start": iso(today + timedelta(days=7 - today.weekday())),
        "month_year": mon_year(today),
        "last_month_year": mon_year(today.replace(day=1) - timedelta(days=1)),
        "next_month_year": mon_year(
            (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        ),
        "year": str(today.year),
        "last_year": str(today.year - 1),
        "next_year": str(today.year + 1),
        "week_ago": iso(today - timedelta(weeks=1)),
        "month_ago": iso(today - timedelta(days=30)),
        "year_ago": iso(_shift_year(today, -1)),
    }

    result = text
    for pat, repl_template in _DATE_PATTERNS_MULTI:
        if repl_template:
            result = pat.sub(repl_template.format(**replacements), result)

    # Dynamic "N days/weeks" patterns
    result = _DATE_PATTERNS_MULTI[15][0].sub(
        lambda m: iso(today - timedelta(days=int(m.group(1)))), result
    )
    result = _DATE_PATTERNS_MULTI[16][0].sub(
        lambda m: iso(today + timedelta(days=int(m.group(1)))), result
    )
    result = _DATE_PATTERNS_MULTI[17][0].sub(
        lambda m: iso(today + timedelta(days=int(m.group(1)))), result
    )
    result = _DATE_PATTERNS_MULTI[18][0].sub(
        lambda m: iso(today - timedelta(weeks=int(m.group(1)))), result
    )
    result = _DATE_PATTERNS_MULTI[19][0].sub(
        lambda m: iso(today + timedelta(weeks=int(m.group(1)))), result
    )

    # Weekday shifts
    day_names = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )

    def _weekday_sub(m: re.Match) -> str:
        day = m.group(1)
        idx = day_names.index(day)
        back = (today.weekday() - idx) % 7 or 7
        fwd = (idx - today.weekday()) % 7 or 7
        if m.group(0).startswith("last"):
            return iso(today - timedelta(days=back))
        return iso(today + timedelta(days=fwd))

    result = _WEEKDAY_RE.sub(_weekday_sub, result)

    # Single words last
    for pat, repl_template in _DATE_PATTERNS_SINGLE:
        result = pat.sub(repl_template.format(**replacements), result)

    return result


def _preview(s: str, n: int = 120) -> str:
    t = (s or "").replace("\n", " ").strip()
    return t if len(t) <= n else t[:n].rstrip() + "…"


# ==============================================================================
# Public API
# ==============================================================================
async def handle_turn(
    *,
    state: Any,
    source: str,
    user_message: str,
    top_k_memories: int = 12,
    ui_output: UiPrintFn,
    ui_input: UiInputFn,
    interrupt_event: Optional[threading.Event] = None,
    progress_cb: Callable[[str, bool], None],
) -> str | None:
    t_total = time.perf_counter()
    src = _safe_source(source)
    # FIX: Removed redundant space injection
    user_message = (user_message or "").strip()
    _turn_now = _dt.now()

    if not user_message:
        logger.warning("handle_turn | empty user_message | src=%s", src)
        return None

    # FIX: Record user input ONCE at the very top, before any branching
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

    conversations.add_user(text=user_message)

    # ── Fast-path interceptor ──────────────────────────────────────────────
    normalized_message = _normalize(user_message)
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
            # FIX: Only add buddy on success. add_user already called at top.
            conversations.add_buddy(text=reply)
            return reply
        logger.info(
            "interceptor_fast_path_failed | src=%s action=%s err=%r — falling through"
            " to pipeline",
            src,
            quick.name,
            reply,
        )

    # ── Context budget ─────────────────────────────────────────────────────
    _base_budget = getattr(state, "context_budget", None)
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

    _top_k = _adjusted.top_k_memories if _adjusted else top_k_memories
    _max_history_chars = _adjusted.max_history_chars if _adjusted else 14_000
    _max_memory_chars = _adjusted.max_memory_chars if _adjusted else 8_000
    _max_exec_results_chars = _adjusted.max_exec_chars if _adjusted else 16_000
    _max_tool_output_chars = _adjusted.max_tool_chars if _adjusted else 10_000

    session_id = _ensure_session_id(state)
    turn_id = new_turn_id()
    turn_index = _next_turn_index(state)

    logger.info(
        "\nHANDLE_TURN_START | sid=%s tid=%s turn=%d src=%s text_len=%d preview=%r",
        session_id,
        turn_id,
        turn_index,
        src,
        len(user_message),
        _preview(user_message, 120),
    )

    # ── 1) Recent conversation context ─────────────────────────────────────
    t0 = time.perf_counter()
    try:
        recent_conversations = conversations.get_recent_conversations() or " "
    except Exception as ex:
        logger.warning(
            "conv_fetch_failed | sid=%s tid=%s turn=%d err=%r",
            session_id,
            turn_id,
            turn_index,
            ex,
        )
        recent_conversations = " "
    dt_conv = time.perf_counter() - t0

    # ── 2) Retrieval gate ──────────────────────────────────────────────────
    progress_cb("Leafing through memories...", False)
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

    # FIX: Walrus operator avoids double strip()
    search_queries = [s for q in search_queries if (s := str(q).strip())]
    search_queries = [_resolve_relative_dates(q, _turn_now) for q in search_queries]
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

    # ── 3) Memory retrieval ────────────────────────────────────────────────
    t0 = time.perf_counter()
    mem_text = "none"
    retrieved: List[Any] = []
    if mm and search_queries:
        try:
            retrieved, mem_text = await asyncio.to_thread(
                mm.get_memory_context_multi,
                search_queries,
                top_k=_top_k * 2 if deep_recall else _top_k,
                include_deleted=deep_recall,
            )
        except Exception as ex:
            logger.warning(
                "memory_retrieval_failed | sid=%s tid=%s turn=%d err=%r",
                session_id,
                turn_id,
                turn_index,
                ex,
            )

    dt_mem = time.perf_counter() - t0

    # ── 4) Run Brain prompt ────────────────────────────────────────────────
    logger.debug(
        "brain_context | sid=%s tid=%s turn=%d conv_chars=%d mem_chars=%d",
        session_id,
        turn_id,
        turn_index,
        len(recent_conversations),
        len(mem_text or ""),
    )

    recent_conversations = truncate_history(recent_conversations, _max_history_chars)
    mem_text = truncate_memory(mem_text or " ", _max_memory_chars)

    # FIX: Set lookup is O(1) and faster than tuple
    if not mem_text or mem_text.strip().lower() in _NULL_MEM_TEXT:
        mem_text = (
            "No memories yet — I'm starting fresh. I'll pay close attention to who this"
            " person is and what matters to them."
        )

    progress_cb("Lost in thought...", False)
    t0 = time.perf_counter()
    payload = await asyncio.to_thread(
        brain.run_brain,
        active_task=user_message,
        recent_turns=recent_conversations,
        memories=mem_text,
        stream=True,
    )
    dt_llm = time.perf_counter() - t0

    # ── Touch retrieved memories (Non-blocking) ────────────────────────────
    if mm is not None and retrieved:
        # FIX: Avoid double getattr & filter None properly
        _touch_ids = [
            str(mid)
            for c in retrieved
            if (mid := getattr(c, "memory_id", None)) is not None
        ]
        if _touch_ids:

            async def _touch_bg():
                try:
                    await asyncio.to_thread(mm.sqlite.batch_touch, _touch_ids)
                    logger.info(
                        "touch_done | sid=%s tid=%s turn=%d count=%d",
                        session_id,
                        turn_id,
                        turn_index,
                        len(_touch_ids),
                    )
                except Exception as _te:
                    logger.debug(
                        "touch_failed | sid=%s tid=%s turn=%d err=%r",
                        session_id,
                        turn_id,
                        turn_index,
                        _te,
                    )

            asyncio.create_task(_touch_bg())

    # ── Parse Brain Output ─────────────────────────────────────────────────
    parsed = payload.get("parsed") or {}
    decision = parsed.get("decision") or {}
    memories_raw = parsed.get("memories") or []
    if isinstance(memories_raw, dict):
        memories_raw = [memories_raw]

    memories_list = [
        m
        for m in memories_raw
        if isinstance(m, dict)
        and str(m.get("memory_type", "discard")).strip().lower() != "discard"
    ]

    # ── 5) Memory storage (Background, Non-blocking) ───────────────────────
    if mm and memories_list:
        _enc_arousal = _compute_encoding_arousal(user_message)
        # Pre-resolve dates synchronously (fast string ops)
        for _m in memories_list:
            if isinstance(_m.get("memory_text"), str):
                _m["memory_text"] = _resolve_relative_dates(
                    _m["memory_text"], _turn_now
                )

        async def _ingest_bg():
            for _mem in memories_list:
                try:
                    entry = await asyncio.to_thread(
                        mm.create_memory_entry,
                        memory=_mem,
                        source=src,
                        source_turn=turn_index,
                        role="buddy",
                        metadata={"encoding_arousal": _enc_arousal},
                    )
                    if entry is not None:
                        await asyncio.to_thread(mm.add_entry, entry)
                except Exception as ex:
                    logger.warning(
                        "memory_ingest_failed | sid=%s tid=%s turn=%d err=%r",
                        session_id,
                        turn_id,
                        turn_index,
                        ex,
                    )

        asyncio.create_task(_ingest_bg())

    # ── Route Response ─────────────────────────────────────────────────────
    mode = decision.get("mode")
    response = str(decision.get("response") or "").strip()
    afterthought = str(decision.get("afterthought") or "").strip()

    if mode == "CHAT":
        conversations.add_buddy(text=response)
        if afterthought:
            conversations.add_buddy(text=afterthought)
        await ui_output(response)
        if afterthought:
            await ui_output(afterthought)
    elif mode not in ("CHAT", "ACTION"):
        fallback = "I got a bit confused there. Could you try again?"
        logger.warning(
            "handle_turn | invalid mode=%r sid=%s tid=%s turn=%d",
            mode,
            session_id,
            turn_id,
            turn_index,
        )
        conversations.add_buddy(text=fallback)
        await ui_output(fallback)

    if mode == "ACTION":
        await ui_output(response)
        action_router = ActionRouter(
            brain=brain, ui_output=ui_output, ui_input=ui_input, memory_manager=mm
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
        progress_cb("Putting it into words...", False)
        responder_instruction = str(
            action_result.get("responder_instruction") or ""
        ).strip()
        execution_results = action_result.get("step_execution_map")

        execution_results = truncate_proportional(
            execution_results or {},
            _max_exec_results_chars,
            max_per_step_chars=_max_tool_output_chars,
        )

        payload = await asyncio.to_thread(
            brain.run_respond,
            active_task=responder_instruction,
            memories=mem_text,
            # FIX: Compact JSON saves CPU & network overhead
            execution_results=json.dumps(
                execution_results, ensure_ascii=False, separators=(",", ":")
            ),
            stream=True,
        )
        parsed_respond = (payload.get("parsed") or {}) if payload else {}
        response = parsed_respond.get("response") or " "
        memory_candidates = parsed_respond.get("memory_candidates") or []

        if response:
            conversations.add_buddy(text=response)
            await ui_output(response)

        if memory_candidates and mm:
            progress_cb("Etching it in... ✍️", False)
            _action_arousal = _compute_encoding_arousal(user_message)
            for _m in memory_candidates:
                if isinstance(_m.get("memory_text"), str):
                    _m["memory_text"] = _resolve_relative_dates(
                        _m["memory_text"], _turn_now
                    )

            async def _ingest_action_bg():
                for mem in memory_candidates:
                    try:
                        entry = await asyncio.to_thread(
                            mm.create_memory_entry,
                            memory=mem,
                            source=src,
                            source_turn=turn_index,
                            role="buddy",
                            metadata={"encoding_arousal": _action_arousal},
                        )
                        if entry is not None:
                            await asyncio.to_thread(mm.add_entry, entry)
                    except Exception as ex:
                        logger.warning(
                            "memory_ingest_failed | sid=%s tid=%s turn=%d err=%r",
                            session_id,
                            turn_id,
                            turn_index,
                            ex,
                        )

            asyncio.create_task(_ingest_action_bg())

    dt_total = time.perf_counter() - t_total
    logger.info(
        "HANDLE_TURN_END | sid=%s tid=%s turn=%d mode=%s reply_len=%d total=%.3fs",
        session_id,
        turn_id,
        turn_index,
        mode,
        len(response),
        dt_total,
    )
    return response
