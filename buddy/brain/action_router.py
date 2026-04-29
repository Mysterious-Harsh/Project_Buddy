# buddy/actions/action_router.py
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from buddy.logger.logger import get_logger

logger = get_logger("action_router")

# ==========================================================
# Success output projection — lean responder-friendly output
# ==========================================================

_ALWAYS_STRIP = {"OK", "TOOL"}


def _project_success(tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Called only on OK=True. Strips fields the responder doesn't need.
    OK and TOOL move to the step wrapper — removed from all tool outputs.
    On failure the full result is kept so the executor can retry with full context.
    """
    if tool_name == "terminal":
        projected: Dict[str, Any] = {
            "CWD": result.get("CWD"),
            "COMMAND": result.get("COMMAND"),
            "EXIT_CODE": result.get("EXIT_CODE"),
            "STDOUT": result.get("STDOUT"),
            "STDERR": result.get("STDERR"),
            "TIMEOUT": result.get("TIMEOUT"),
        }
        if result.get("IS_DAEMON"):  # only when True
            projected["IS_DAEMON"] = True
        if result.get("PID") is not None:  # only when set
            projected["PID"] = result["PID"]
        return projected

    if tool_name == "web_search":
        return {
            "QUERY": result.get("QUERY"),
            "RESULTS": result.get("RESULTS"),
        }

    if tool_name == "filesystem":
        projected: Dict[str, Any] = {}
        for field in (
            # common
            "PATH", "ACTION", "FORMAT",
            # ls
            "ENTRIES", "TREE_TEXT", "TOTAL",
            # read
            "CONTENT", "SIZE_BYTES", "MODIFIED", "CREATED",
            "LINE_COUNT", "START_LINE", "END_LINE",
            "ROWS_TOTAL", "ROWS_AFTER_FILTER", "COLUMNS", "SHEET",
            "EXISTS", "IS_FILE", "IS_DIR",
            "MIME", "OPENED",
            # find
            "RESULTS", "TOTAL_FOUND",
            # manage
            "DESTINATION", "DIFF",
            # shared
            "TRUNCATED", "NOTE",
            "NEEDS_CONFIRMATION", "PREVIEW",
        ):
            if result.get(field) is not None:
                projected[field] = result[field]
        return projected

    if tool_name == "browser":
        return {
            k: v for k, v in result.items()
            if k not in _ALWAYS_STRIP and k in (
                "ACTION", "TASK", "URL", "STEPS", "SUMMARY",
                "FILLED", "FAILED", "DESCRIPTION", "KEY_FINDING",
                "TEXT_FOUND", "TITLE", "FORM_FIELDS", "BUTTONS",
                "HAS_CAPTCHA", "SESSIONS", "EXISTS", "DOMAIN", "ERROR",
            )
        }

    if tool_name == "clipboard":
        return {k: v for k, v in result.items() if k not in _ALWAYS_STRIP}

    # vision, unknown — strip OK/TOOL only
    return {k: v for k, v in result.items() if k not in _ALWAYS_STRIP}


# Maps tool name → action verb shown in the spinner during executor + tool execution.
# Tools may override this with a more specific label via their own on_progress call.
_TOOL_VERB: Dict[str, str] = {
    "filesystem": "Tending to files...",
    "terminal": "Setting things in motion...",
    "web_search": "Wandering the web...",
    "web_fetch": "Pulling it close...",
    "vision": "Studying this...",
    "system_control": "Taking hold...",
    "browser": "Wandering through...",
    "clipboard": "Holding onto that...",
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
        self._max_depth = int(max_depth)
        self._entries: List[ErrorEntry] = []

    @property
    def depth(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def add(self, *, tool_result: Dict[str, Any], attempt: int) -> None:
        # store exactly what tool returned (full dict)
        if not isinstance(tool_result, dict):
            return

        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._entries.append(
            ErrorEntry(ts=ts, attempt=int(attempt), tool_result=tool_result)
        )

        # hard cap
        if len(self._entries) > self._max_depth:
            self._entries = self._entries[-self._max_depth :]

    @property
    def appendix(self) -> str:
        if not self._entries:
            return ""

        lines: List[str] = []
        for e in self._entries:
            lines.append(f"[{e.ts}] Attempt {e.attempt}:")
            try:
                lines.append(json.dumps(e.tool_result, ensure_ascii=False, indent=2))
            except Exception:
                lines.append(str(e.tool_result))
            lines.append("")
        return "\n".join(lines).strip()


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
        self._max_depth = int(max_depth)
        self._entries: List[FollowupEntry] = []

    @property
    def depth(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def appendix(self) -> str:
        if not self._entries:
            return ""
        blocks: List[str] = []
        for e in self._entries:
            blocks.append(f"<|im_start|>assistant\n{e.question}\n<|im_end|>")
            blocks.append(f"<|im_start|>user\n{e.answer}\n<|im_end|>")
        return "\n".join(blocks)

    async def handle(
        self,
        *,
        followup: bool,
        followup_question: str,
        stage: str,
        step_id: Optional[int] = None,
        tool_name: Optional[str] = None,
    ) -> bool:
        if not followup:
            return False

        if self.depth >= self._max_depth:
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
        self._max_step_attempts = int(max_step_attempts)

        from buddy.tools.registry import ToolRegistry

        self._registry = ToolRegistry()
        _tools = self._registry.available_tools()
        self._available_tools_str = json.dumps(_tools, ensure_ascii=False, indent=2)
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
            planner_instructions,
        )
        now_iso, timezone = self._get_time_info()

        registry = self._registry
        available_tools_str = self._available_tools_str

        # ── log what the planner will see ────────────────────────────────────
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
        if on_token:
            on_token("Drawing up a plan...", False)
        while True:

            planner_payload = await asyncio.to_thread(
                self.brain.run_planner,
                active_task=self.stack.appendix,
                planner_instructions=planner_instructions,
                available_tools=available_tools_str,
                memories=memories,
                stream=True,
                llm_options=llm_options,
            )
            planner_parsed = planner_payload.get("parsed") or planner_payload or {}
            _status = str(planner_parsed.get("status") or "").strip().lower()

            rerun = await self.stack.handle(
                followup=(_status == "followup"),
                followup_question=str(planner_parsed.get("message") or ""),
                stage="planner",
            )
            if not rerun:
                self.stack.clear()
                break

        planner_dt = time.perf_counter() - t0

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

        responder_instruction = str(
            planner_parsed.get("responder_instruction") or ""
        ).strip()
        steps = (
            (planner_parsed.get("steps") or [])
            if isinstance(planner_parsed, dict)
            else []
        )
        if not steps:
            if _status == "refusal":
                refusal_msg = str(planner_parsed.get("message") or "").strip()
                responder_instruction = (
                    f"Could not do this — capability not available: {refusal_msg}. "
                    "Tell the user plainly what can't be done and suggest the nearest alternative."
                )
            elif _status not in ("followup",):
                # parse failure or success with empty steps — something broke internally
                responder_instruction = (
                    "Buddy failed to plan this action due to an internal error. "
                    "Tell the user something went wrong and offer to retry."
                )
            return {
                "now_iso": now_iso,
                "timezone": timezone,
                "planner": planner_parsed,
                "responder_instruction": responder_instruction,
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
            output_name = str(step.get("output") or "").strip()
            goal = str(step.get("goal") or "").strip()
            instruction = str(step.get("instruction") or "").strip()
            hints = str(step.get("hints") or "").strip()
            ack = goal
            instruction = {
                "Execution_Step_Id": step_id,
                "Goal": goal,
                "Instruction": instruction,
                "Hints": hints,
            }

            input_steps = step.get("input_steps", []) if isinstance(step, dict) else []
            input_steps = input_steps if isinstance(input_steps, list) else []

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
                logger.error(
                    "step invalid sid=%s tool=%s output=%s", sid, tool_name, output_name
                )
                break

            logger.info(
                "step %d start tool=%s output=%s deps=%s",
                step_id,
                tool_name,
                output_name,
                input_steps,
            )
            # Show tool-mapped action verb in the spinner
            if on_token:
                step_verb = _TOOL_VERB.get(tool_name, f"Executing · {tool_name}")
                try:
                    on_token(step_verb, False)
                except Exception:
                    pass

            # Build prior_outputs (DATA FLOW)
            prior_outputs: Dict[str, Any] = {}
            for dep_id in input_steps:
                try:
                    dep_key = str(int(dep_id))
                except Exception:
                    continue
                dep_entry = step_execution_map.get(dep_key)
                if dep_entry and dep_entry.get("output_name"):
                    prior_outputs[str(dep_entry["output_name"])] = dep_entry.get(
                        "output_data"
                    )

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
            instruction_json = json.dumps(instruction, indent=2, ensure_ascii=False)
            prior_outputs_json = json.dumps(prior_outputs, indent=2, ensure_ascii=False)

            # ==================================================
            # Per-step attempt loop (executor -> tool -> retry on error)
            # ==================================================
            attempt = 0
            exec_result: Dict[str, Any] = {}

            while True:
                # Stop condition: too many tool attempts (NOT followups)
                if attempt > self._max_step_attempts:
                    break
                if attempt > 0:
                    if on_token:
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
                        followup_question=str(fq),
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
                    reason = str(exec_result.get("message") or "Executor refused step").strip()
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
                            "message": f"Executor returned unrecognised status='{status}'",
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
                    step_id, attempt, tool_name,
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
                    bool(tool_exec_result.get("OK", False))
                    if isinstance(tool_exec_result, dict)
                    else False
                )

                # compact result summary for the log
                if isinstance(tool_exec_result, dict):
                    _summary_parts = []
                    if "ACTION" in tool_exec_result:
                        _summary_parts.append(f"action={tool_exec_result['ACTION']}")
                    if "TOTAL_FOUND" in tool_exec_result:
                        _summary_parts.append(
                            f"found={tool_exec_result['TOTAL_FOUND']}"
                        )
                    if (
                        "SIZE_BYTES" in tool_exec_result
                        and tool_exec_result["SIZE_BYTES"] is not None
                    ):
                        _summary_parts.append(f"size={tool_exec_result['SIZE_BYTES']}B")
                    if "EXIT_CODE" in tool_exec_result:
                        _summary_parts.append(f"exit={tool_exec_result['EXIT_CODE']}")
                    if not ok and tool_exec_result.get("ERROR"):
                        _summary_parts.append(
                            f"error={str(tool_exec_result['ERROR'])[:120]}"
                        )
                    elif not ok and tool_exec_result.get("STDERR"):
                        _summary_parts.append(
                            f"stderr={str(tool_exec_result['STDERR'])[:120]}"
                        )
                    _result_summary = "  ".join(_summary_parts) or "(no summary fields)"
                else:
                    _result_summary = str(tool_exec_result)[:120]

                logger.info(
                    "step %d attempt %d tool_done ok=%s  %s",
                    step_id,
                    attempt,
                    ok,
                    _result_summary,
                )

                if ok:
                    # ✅ Step success — project to lean responder output
                    step_execution_map[str(step_id)] = {
                        "tool": tool_name,
                        "goal": goal,
                        "status": "success",
                        "output_name": output_name,
                        "output_data": _project_success(tool_name, tool_exec_result),
                    }
                    logger.info("step %d success tool=%s", step_id, tool_name)
                    break

                self.errors.add(
                    tool_result=tool_exec_result,
                    attempt=attempt,
                )
                logger.warning(
                    "step %d attempt %d tool_ok_false -> retry evidence=%r ",
                    step_id,
                    attempt,
                    str(tool_exec_result)[:200],
                )

                attempt += 1

            # if the while loop exited because attempts were exhausted (no break from
            # success/refusal/followup paths), record the step as failed now
            if str(step_id) not in step_execution_map:
                last_error = str(tool_exec_result.get("ERROR") or tool_exec_result.get("STDERR") or "")[:200]
                step_execution_map[str(step_id)] = {
                    "tool": tool_name,
                    "goal": goal,
                    "status": "failed",
                    "error": {
                        "type": "max_attempts_exceeded",
                        "message": f"Step failed after {self._max_step_attempts} attempt(s)."
                        + (f" Last error: {last_error}" if last_error else ""),
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
            "responder_instruction": responder_instruction,
            "step_execution_map": step_execution_map,
        }

    # -------------------------
    # Time helper
    # -------------------------
    @staticmethod
    def _get_time_info() -> Tuple[str, str]:
        lt = time.localtime()
        tz_name = time.tzname[1] if lt.tm_isdst > 0 else time.tzname[0]
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z", lt)
        return now, tz_name
