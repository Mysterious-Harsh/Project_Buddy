from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from buddy.logger.logger import get_logger
from buddy.tools.registry import ToolRegistry

logger = get_logger("action_router")

# ==========================================================
# Success output projection — lean responder-friendly output
# ==========================================================
_ALWAYS_STRIP = {"STATUS", "TOOL"}

# Precompute FS fields for O(1) lookup
_FS_FIELDS = frozenset({
    "PATH",
    "ACTION",
    "FORMAT",
    "ENTRIES",
    "TREE_TEXT",
    "TOTAL",
    "CONTENT",
    "SIZE_BYTES",
    "MODIFIED",
    "CREATED",
    "LINE_COUNT",
    "START_LINE",
    "END_LINE",
    "ROWS_TOTAL",
    "ROWS_AFTER_FILTER",
    "COLUMNS",
    "SHEET",
    "EXISTS",
    "IS_FILE",
    "IS_DIR",
    "MIME",
    "OPENED",
    "RESULTS",
    "TOTAL_FOUND",
    "DESTINATION",
    "DIFF",
    "TRUNCATED",
    "NOTE",
    "NEEDS_CONFIRMATION",
    "PREVIEW",
    "GLOB_WARNINGS",
})


def _project_success(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Called only on OK=True. Strips fields the responder doesn't need.
    OK and TOOL move to the step wrapper — removed from all tool outputs.
    On failure the full result is kept so the executor can retry with full context.
    """
    if tool_name == "terminal":
        return {
            "CWD": result.get("CWD"),
            "COMMAND": result.get("COMMAND"),
            "EXIT_CODE": result.get("EXIT_CODE"),
            "STDOUT": result.get("STDOUT"),
            "STDERR": result.get("STDERR"),
            "TIMEOUT": result.get("TIMEOUT"),
            **({"IS_DAEMON": True} if result.get("IS_DAEMON") else {}),
            **({"PID": result["PID"]} if result.get("PID") is not None else {}),
        }

    if tool_name == "web_search":
        return {
            "QUERY": result.get("QUERY"),
            "RESULTS": result.get("RESULTS"),
        }

    if tool_name == "filesystem":
        # Set-based filtering replaces manual loop + repeated .get()
        return {k: v for k, v in result.items() if k in _FS_FIELDS and v is not None}

    if tool_name == "browser":
        return {
            k: v
            for k, v in result.items()
            if k not in _ALWAYS_STRIP
            and k
            in (
                "ACTION",
                "TASK",
                "URL",
                "STEPS",
                "SUMMARY",
                "FILLED",
                "FAILED",
                "DESCRIPTION",
                "KEY_FINDING",
                "TEXT_FOUND",
                "TITLE",
                "FORM_FIELDS",
                "BUTTONS",
                "HAS_CAPTCHA",
                "SESSIONS",
                "EXISTS",
                "DOMAIN",
                "ERROR",
            )
        }

    if tool_name == "clipboard":
        return {k: v for k, v in result.items() if k not in _ALWAYS_STRIP}

    if tool_name == "analyzer":
        if "ANALYSIS" in result:
            return {"ANALYSIS": result["ANALYSIS"]}
        if "SUMMARY" in result:
            return {"SUMMARY": result["SUMMARY"]}
        return {}

    # vision, unknown — strip OK/TOOL only
    return {k: v for k, v in result.items() if k not in _ALWAYS_STRIP}


# Maps tool name → action verb shown in the spinner during executor + tool execution.
_TOOL_VERB: Dict[str, str] = {
    "filesystem": "Tending to files...",
    "terminal": "Setting things in motion...",
    "web_search": "Wandering the web...",
    "web_fetch": "Pulling it close...",
    "vision": "Studying this...",
    "system_control": "Taking hold...",
    "browser": "Wandering through...",
    "clipboard": "Holding onto that...",
    "analyzer": "Thinking it through...",
}


@dataclass(frozen=True)
class PlanOutcome:
    kind: str  # "followup" | "plan" | "error" | "executed"
    message: str
    plan: Optional[Dict[str, Any]] = None


UiInputFn = Callable[[], Awaitable[str]]
UiPrintFn = Callable[[str], Awaitable[None]]


# ==========================================================
# Error Stack (per-step, full tool dict)
# ==========================================================
@dataclass
class ErrorEntry:
    ts: str
    attempt: int
    tool_result: Dict[str, Any]


class ErrorStack:
    """
    Per-step error history for executor retries.
    Rule:
      - ONLY store the FULL dict returned by the tool when ok == False.
      - No extraction, no summarization, no extra details.
    """

    def __init__(self, *, max_depth: int = 3) -> None:
        self._max_depth = max_depth
        self._entries: deque[ErrorEntry] = deque(maxlen=max_depth)
        self._cache: Optional[str] = None

    @property
    def depth(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._cache = None

    def add(self, *, tool_result: Dict[str, Any], attempt: int) -> None:
        if not isinstance(tool_result, dict):
            return

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._entries.append(
            ErrorEntry(ts=ts, attempt=attempt, tool_result=tool_result)
        )
        self._cache = None  # invalidate cache

    @property
    def appendix(self) -> str:
        if self._cache is not None:
            return self._cache
        if not self._entries:
            self._cache = ""
            return self._cache

        lines: List[str] = []
        for e in self._entries:
            lines.append(f"[{e.ts}] Attempt {e.attempt}:")
            try:
                # Removed indent=2 for performance. LLMs don't need whitespace.
                lines.append(json.dumps(e.tool_result, ensure_ascii=False))
            except Exception:
                # Prevent memory blowup on massive binary/class objects
                lines.append(repr(e.tool_result)[:500] + "...")
            lines.append("")

        self._cache = "\n".join(lines).strip()
        return self._cache


# ==========================================================
# Followup Stack (global)
# ==========================================================
@dataclass
class FollowupEntry:
    ts: str
    stage: str  # "planner" | "executor"
    question: str
    answer: str
    step_id: Optional[int] = None
    tool_name: Optional[str] = None


class FollowupStack:
    """
    Single FOLLOWUP block with timestamped Q/A lines.
    """

    def __init__(
        self,
        *,
        ui_output: UiPrintFn,
        ui_input: UiInputFn,
        max_depth: int = 3,
    ) -> None:
        self._ui_output = ui_output
        self._ui_input = ui_input
        self._max_depth = max_depth
        self._entries: deque[FollowupEntry] = deque(maxlen=max_depth)
        self._cache: Optional[str] = None

    @property
    def depth(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._cache = None

    @property
    def appendix(self) -> str:
        if self._cache is not None:
            return self._cache
        if not self._entries:
            self._cache = ""
            return self._cache

        blocks: List[str] = []
        for e in self._entries:
            # Cleaned token spacing for standard LLM template compatibility
            blocks.append(f"<|im_start|>assistant\n{e.question}\n<|im_end|>")
            blocks.append(f"<|im_start|>user\n{e.answer}\n<|im_end|>")

        self._cache = "\n".join(blocks)
        return self._cache

    async def handle(
        self,
        *,
        followup: bool,
        followup_question: str,
        stage: str,
        step_id: Optional[int] = None,
        tool_name: Optional[str] = None,
        skip_depth_check: bool = False,
    ) -> bool:
        if not followup:
            return False

        if not skip_depth_check and self.depth >= self._max_depth:
            await self._ui_output(
                "Too many follow-up questions. Please restate the task clearly."
            )
            return False

        q = (followup_question or "").strip() or "Can you clarify?"
        await self._ui_output(q)
        ans = (await self._ui_input()).strip()

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._entries.append(
            FollowupEntry(
                ts=ts,
                stage=stage,
                question=q,
                answer=ans,
                step_id=step_id,
                tool_name=tool_name,
            )
        )
        self._cache = None  # invalidate cache
        return True


# ==========================================================
# Action Router (plan + execute)
# ==========================================================
class ActionRouter:
    """
    v1 ActionRouter (PLAN + EXECUTE)
    Flow:
      1) Planner loop (may ask followups)
      2) For each step:
         - Build prior_outputs from StepExecutionMap
         - Per-step retry loop:
             (executor -> tool -> if error -> push ErrorStack -> rerun executor)
    """

    # Hard safety cap to prevent infinite planner followup loops
    _MAX_PLANNER_FOLLOWUPS = 5

    def __init__(
        self,
        *,
        brain: Any,
        ui_output: UiPrintFn,
        ui_input: UiInputFn,
        max_step_attempts: int = 3,
        memory_manager: Any = None,
    ) -> None:
        self.brain = brain
        self.memory_manager = memory_manager
        self._ui_output = ui_output
        self._ui_input = ui_input
        self.stack = FollowupStack(ui_output=self._ui_output, ui_input=self._ui_input)
        self.errors = ErrorStack(max_depth=3)
        self._max_step_attempts = max_step_attempts

        # Registry loaded once at init
        self._registry = ToolRegistry()
        _tools = self._registry.available_tools()
        self._available_tools_str = "\n".join(
            f"{t['name']}  –  {t['description']}" for t in _tools
        )
        self._registry_tools: List[str] = [t["name"] for t in _tools]

        logger.debug("ActionRouter initialized with brain=%s", type(brain).__name__)

    async def action(
        self,
        *,
        turn_id: str,
        session_id: str,
        planner_instructions: str,
        user_message: str,
        memories: str,
        on_token: Optional[Callable[[str, bool], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        logger.info(
            "ACTION start turn_id=%s session_id=%s planner_instructions=%s",
            turn_id,
            session_id,
            planner_instructions[:100],
        )
        now_iso, timezone = self._get_time_info()

        registry = self._registry
        available_tools_str = self._available_tools_str

        logger.info(
            "┌─ PLANNER INPUT ─────────────────────────────────────────\n"
            "│  tools (%d): %s\n"
            "│  intent: %s\n"
            "└─────────────────────────────────────────────────────────",
            len(self._registry_tools),
            ", ".join(self._registry_tools),
            planner_instructions[:120],
        )

        # ======================================================
        # 1) Planner loop
        # ======================================================
        t0 = time.perf_counter()
        planner_parsed: Dict[str, Any] = {}
        planner_followup_count = 0

        if on_token:
            on_token("Drawing up a plan...", False)

        while True:
            try:
                planner_payload = await asyncio.to_thread(
                    self.brain.run_planner,
                    active_task=self.stack.appendix,
                    planner_instructions=planner_instructions,
                    available_tools=available_tools_str,
                    memories=memories,
                    stream=True,
                    llm_options=llm_options,
                )
            except Exception as exc:
                logger.warning("planner_llm_exception: %r", exc)
                self.stack.clear()
                return {
                    "now_iso": now_iso,
                    "timezone": timezone,
                    "planner": {},
                    "step_execution_map": {},
                }

            planner_parsed = planner_payload.get("parsed") or planner_payload or {}
            _status = (planner_parsed.get("status") or "").strip().lower()

            rerun = await self.stack.handle(
                followup=(_status == "followup"),
                followup_question=str(planner_parsed.get("message") or ""),
                stage="planner",
                skip_depth_check=True,
            )
            if not rerun:
                break

            planner_followup_count += 1
            if planner_followup_count >= self._MAX_PLANNER_FOLLOWUPS:
                logger.warning("Planner followup safety cap reached. Breaking loop.")
                break

        planner_dt = time.perf_counter() - t0
        self.stack.clear()  # Reset for execution phase

        # ── log the plan the planner produced ────────────────────────────────
        _steps_raw = planner_parsed.get("steps") or []
        if _status == "refusal":
            logger.info(
                "┌─ PLAN: REFUSED (%.2fs) ──────────────────────────────────\n"
                "│  reason: %s\n"
                "└─────────────────────────────────────────────────────────",
                planner_dt,
                str(planner_parsed.get("message") or "")[:200],
            )
        elif _status == "followup":
            logger.info(
                "┌─ PLAN: FOLLOWUP (%.2fs) ─────────────────────────────────\n"
                "│  question: %s\n"
                "└─────────────────────────────────────────────────────────",
                planner_dt,
                str(planner_parsed.get("message") or "")[:200],
            )
        elif _steps_raw:
            step_lines = "\n".join(
                "│  step {:>2} │ {:<14} │ {}".format(
                    s.get("step_id", "?"),
                    str(s.get("tool") or "?")[:14],
                    str(s.get("goal") or s.get("instruction") or "")[:70],
                )
                for s in _steps_raw
            )
            logger.info(
                "┌─ PLAN: %d step(s) (%.2fs) ─────────────────────────────────\n"
                "%s\n"
                "└─────────────────────────────────────────────────────────",
                len(_steps_raw),
                planner_dt,
                step_lines,
            )
        else:
            logger.info(
                "planner returned no steps and no refusal/followup (%.2fs)", planner_dt
            )

        steps = (
            planner_parsed.get("steps") or []
            if isinstance(planner_parsed, dict)
            else []
        )

        if not steps:
            refusal_msg = str(planner_parsed.get("message") or "").strip()
            return {
                "now_iso": now_iso,
                "timezone": timezone,
                "planner": planner_parsed,
                "refusal_msg": refusal_msg,
                "step_execution_map": {},
            }

        # 🔒 LOCKED execution structure
        step_execution_map: Dict[str, Dict[str, Any]] = {}

        # ======================================================
        # 2) Execute steps sequentially
        # ======================================================
        for step in steps:
            step_id = int(step.get("step_id") or 0)
            tool_name = str(step.get("tool") or "").strip()
            goal = str(step.get("goal") or "").strip()
            instruction = str(step.get("instruction") or "").strip()
            hints = str(step.get("hints") or "").strip()

            instruction_dict = {
                "Current_Step": step_id,
                "Total_Steps": len(steps),
                "Goal": goal,
                "Instruction": instruction,
                "Hints": hints,
            }

            depends_on = step.get("depends_on", []) if isinstance(step, dict) else []
            depends_on = depends_on if isinstance(depends_on, list) else []

            # validate step
            if step_id < 1 or not tool_name or not instruction:
                sid = str(step_id or (len(step_execution_map) + 1))
                step_execution_map[sid] = {
                    "tool": tool_name or "unknown",
                    "goal": goal,
                    "status": "failed",
                    "output_data": None,
                    "error": {
                        "type": "invalid_step",
                        "message": (
                            "Planner produced invalid step (missing"
                            " step_id/tool/instruction)."
                        ),
                    },
                }
                logger.error("step invalid sid=%s tool=%s", sid, tool_name)
                break

            logger.info(
                "step %d start tool=%s deps=%s",
                step_id,
                tool_name,
                depends_on,
            )

            # Show tool-mapped action verb in the spinner
            if on_token:
                step_verb = _TOOL_VERB.get(tool_name, f"Executing · {tool_name}")
                try:
                    on_token(step_verb, False)
                except Exception:
                    pass

            # Build prior_outputs (DATA FLOW)
            # Each entry includes tool, goal, status, and output_data so the executor has
            # full context about what ran and what it produced.
            # If planner omitted depends_on, include all completed prior steps as a safety net.
            prior_outputs: Dict[str, Any] = {}
            dep_ids = depends_on if depends_on else list(step_execution_map.keys())
            for dep_id in dep_ids:
                dep_key = str(dep_id)
                dep_entry = step_execution_map.get(dep_key)
                if dep_entry:
                    prior_outputs["Step_" + dep_key] = {
                        "tool": dep_entry.get("tool"),
                        "goal": dep_entry.get("goal"),
                        "status": dep_entry.get("status"),
                        "output": dep_entry.get("output_data"),
                    }

            # Resolve tool
            tool = registry.get(tool_name)
            if not tool:
                step_execution_map[str(step_id)] = {
                    "tool": tool_name,
                    "goal": goal,
                    "status": "failed",
                    "error": {
                        "type": "tool_missing",
                        "message": f"Tool '{tool_name}' not found in registry",
                    },
                }
                logger.error("step %d tool missing: %s", step_id, tool_name)
                break

            tool_info = tool.get_info()
            tool_prompt = str(tool_info.get("prompt") or "")

            # reset per-step error and followup context
            self.errors.clear()
            self.stack.clear()

            # serialize once — neither changes between retry attempts
            instruction_json = json.dumps(instruction_dict, ensure_ascii=False)
            prior_outputs_json = json.dumps(prior_outputs, ensure_ascii=False)

            # ======================================================
            # Per-step attempt loop (executor -> tool -> retry on error)
            # ======================================================
            attempt = 0
            exec_result: Dict[str, Any] = {}
            tool_exec_result: Dict[str, Any] = {}

            while True:
                # Fixed off-by-one: runs exactly max_step_attempts times
                if attempt >= self._max_step_attempts:
                    break
                if attempt > 0 and on_token:
                    on_token("Something slipped... catching it 🫣", False)

                logger.info(
                    "step %d attempt %d/%d executor_call tool=%s",
                    step_id,
                    attempt,
                    self._max_step_attempts,
                    tool_name,
                )

                # 1) Ask executor for tool_call (or followup/abort)
                t0 = time.perf_counter()
                try:
                    exec_payload = await asyncio.to_thread(
                        self.brain.run_executor,
                        instruction=instruction_json,
                        prior_outputs=prior_outputs_json,
                        step_followups=self.stack.appendix,
                        step_errors=self.errors.appendix,
                        tool_prompt=tool_prompt,
                        stream=True,
                        llm_options=llm_options,
                    )
                except Exception as exc:
                    exec_ms = int((time.perf_counter() - t0) * 1000)
                    logger.warning(
                        "step %d attempt %d executor_llm_exception dt_ms=%d err=%r",
                        step_id,
                        attempt,
                        exec_ms,
                        exc,
                    )
                    self.errors.add(
                        tool_result={
                            "STATUS": "failed",
                            "ERROR": f"Executor LLM call raised: {exc}",
                        },
                        attempt=attempt,
                    )
                    attempt += 1
                    continue
                exec_ms = int((time.perf_counter() - t0) * 1000)

                exec_result = exec_payload.get("parsed") or {}
                status = str(exec_result.get("status") or "").strip().lower()

                logger.info(
                    "step %d attempt %d executor_status=%s dt_ms=%d",
                    step_id,
                    attempt,
                    status,
                    exec_ms,
                )

                # ---------------------------
                # FOLLOWUP path (NO attempt++)
                # ---------------------------
                if status == "followup":
                    fq = str(exec_result.get("message") or "").strip()
                    logger.warning("step %d followup asked: %r", step_id, fq[:140])

                    rerun = await self.stack.handle(
                        followup=True,
                        followup_question=fq,
                        stage="executor",
                        step_id=step_id,
                        tool_name=tool_name,
                    )
                    if rerun:
                        logger.info(
                            "step %d followup answered -> rerun executor (attempt"
                            " stays %d)",
                            step_id,
                            attempt,
                        )
                        continue

                    # followup depth limit hit
                    step_execution_map[str(step_id)] = {
                        "tool": tool_name,
                        "goal": goal,
                        "status": "failed",
                        "error": {
                            "type": "followup_limit",
                            "message": (
                                "Executor followup limit reached; cannot proceed."
                            ),
                        },
                    }
                    logger.error(
                        "step %d followup_limit reached -> abort step", step_id
                    )
                    break

                # ---------------------------
                # REFUSAL path (hard stop)
                # ---------------------------
                if status == "refusal":
                    reason = str(
                        exec_result.get("message") or "Executor refused step"
                    ).strip()
                    step_execution_map[str(step_id)] = {
                        "tool": tool_name,
                        "goal": goal,
                        "status": "failed",
                        "error": {
                            "type": "executor_refusal",
                            "message": reason,
                        },
                    }
                    logger.error("step %d executor_refusal: %r", step_id, reason[:200])
                    break

                # ---------------------------
                # Unknown status (hard stop)
                # ---------------------------
                if status != "success":
                    step_execution_map[str(step_id)] = {
                        "tool": tool_name,
                        "goal": goal,
                        "status": "failed",
                        "error": {
                            "type": "invalid_executor_output",
                            "message": (
                                f"Executor returned unrecognised status='{status}'"
                            ),
                        },
                    }
                    logger.error(
                        "step %d invalid_executor_output status=%r", step_id, status
                    )
                    break

                function = exec_result.get("function", "")
                arguments = exec_result.get("arguments", {})
                logger.info(
                    "┌─ EXECUTOR CALL  step=%d attempt=%d tool=%s ──────────────\n"
                    "│  fn=%s  args=%s\n"
                    "└─────────────────────────────────────────────────────────",
                    step_id,
                    attempt,
                    tool_name,
                    function,
                    json.dumps(arguments, ensure_ascii=False)[:300],
                )

                tool_exec_result = await tool.execute(
                    function=function,
                    arguments=arguments,
                    on_progress=on_token,
                    goal=planner_instructions,
                    brain=self.brain,
                    memory_manager=self.memory_manager,
                    ui_output=self._ui_output,
                    ui_input=self._ui_input,
                )

                # 4) Evaluate tool result
                ok = (
                    isinstance(tool_exec_result, dict)
                    and tool_exec_result.get("STATUS") == "success"
                )

                logger.info(
                    "step %d attempt %d tool_done status=%s error=%s",
                    step_id,
                    attempt,
                    tool_exec_result.get("STATUS") if isinstance(tool_exec_result, dict) else "unknown",
                    str(tool_exec_result.get("ERROR") or tool_exec_result.get("STDERR") or "")[:120]
                    if isinstance(tool_exec_result, dict) and not ok else "",
                )

                if ok:
                    # ✅ Step success — project to lean responder output
                    step_execution_map[str(step_id)] = {
                        "tool": tool_name,
                        "goal": goal,
                        "status": "success",
                        "output_data": _project_success(tool_name, tool_exec_result),
                    }
                    logger.info("step %d success tool=%s", step_id, tool_name)
                    break

                self.errors.add(tool_result=tool_exec_result, attempt=attempt)
                logger.warning(
                    "step %d attempt %d tool_ok_false -> retry evidence=%r",
                    step_id,
                    attempt,
                    str(tool_exec_result)[:200],
                )

                attempt += 1

            # if the while loop exited because attempts were exhausted
            if str(step_id) not in step_execution_map:
                last_error = str(
                    tool_exec_result.get("ERROR")
                    or tool_exec_result.get("STDERR")
                    or ""
                )[:200]
                step_execution_map[str(step_id)] = {
                    "tool": tool_name,
                    "goal": goal,
                    "status": "failed",
                    "error": {
                        "type": "max_attempts_exceeded",
                        "message": (
                            f"Step failed after {self._max_step_attempts} attempt(s)."
                            + (f" Last error: {last_error}" if last_error else "")
                        ),
                    },
                }
                logger.error(
                    "step %d max_attempts_exceeded tool=%s", step_id, tool_name
                )

            # if step failed, stop whole plan (v1)
            if step_execution_map[str(step_id)].get("status") == "failed":
                logger.error("plan halted at step %d", step_id)
                break

        if logger.isEnabledFor(10):  # DEBUG
            logger.debug("step_execution_map=%s", step_execution_map)

        return {
            "now_iso": now_iso,
            "timezone": timezone,
            "planner": planner_parsed,
            "responder_instruction": planner_instructions,
            "step_execution_map": step_execution_map,
        }

    # -------------------------
    # Time helper
    # -------------------------
    @staticmethod
    def _get_time_info() -> Tuple[str, str]:
        lt = time.localtime()
        # Fixed DST: tm_isdst can be -1 (unknown), 0 (no), or 1 (yes)
        tz_name = time.tzname[1] if lt.tm_isdst == 1 else time.tzname[0]
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z", lt)
        return now, tz_name
