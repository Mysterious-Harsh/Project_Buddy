# buddy/actions/action_router.py
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from buddy.logger.logger import get_logger

logger = get_logger("action_router")


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

        lines: List[str] = ["\n<ERROR_HISTORY>"]
        for e in self._entries:
            lines.append(f"[{e.ts}] Attempt {e.attempt}:")
            try:
                lines.append(json.dumps(e.tool_result, ensure_ascii=False, indent=2))
            except Exception:
                # fallback if something inside isn't JSON serializable
                lines.append(str(e.tool_result))
            lines.append("")
        lines.append("</ERROR_HISTORY>\n")
        return "\n".join(lines)


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
        lines: List[str] = ["\n<FOLLOWUP>"]
        for e in self._entries:
            prefix = e.stage.upper()
            lines.append(f"[{e.ts}] {prefix} QUESTION: {e.question}")
            lines.append(f"[{e.ts}] {prefix} ANSWER: {e.answer}")
            lines.append("")
        lines.append("</FOLLOWUP>\n")
        return "\n".join(lines)

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
    ) -> None:
        self.brain = brain
        self._ui_output = ui_output
        self._ui_input = ui_input
        self.stack = FollowupStack(ui_output=self._ui_output, ui_input=self._ui_input)
        self.errors = ErrorStack(max_depth=3)
        self._max_step_attempts = int(max_step_attempts)
        logger.debug("ActionRouter initialized with brain=%s", type(brain).__name__)

    async def action(
        self,
        *,
        turn_id: str,
        session_id: str,
        intent: str,
        user_message: str,
        memories: str,
        on_token: Optional[Callable[[str, bool], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        logger.info(
            "ACTION start turn_id=%s session_id=%s intent=%s",
            turn_id,
            session_id,
            intent,
        )
        now_iso, timezone = self._get_time_info()

        # Import registry directly (your requirement)
        from buddy.tools.registry import ToolRegistry

        registry = ToolRegistry()
        available_tools = registry.available_tools()
        available_tools_str = json.dumps(available_tools, ensure_ascii=False)

        # ======================================================
        # 1) Planner loop
        # ======================================================
        t0 = time.perf_counter()
        planner_parsed: Dict[str, Any] = {}
        if on_token:
            on_token(f"Planning {intent[:80]}", False)
        while True:

            planner_payload = self.brain.run_planner(
                user_current_message=user_message + self.stack.appendix,
                intent=intent,
                available_tools=available_tools_str,
                memories=memories,
                stream=True,
                on_token=on_token,
                llm_options=llm_options,
            )
            planner_parsed = planner_payload.get("parsed") or planner_payload or {}

            rerun = await self.stack.handle(
                followup=bool(planner_parsed.get("followup")),
                followup_question=str(planner_parsed.get("followup_question", "")),
                stage="planner",
            )
            if not rerun:
                self.stack.clear()
                break

        logger.info("planner LLM finished in %.3fs", time.perf_counter() - t0)

        steps = (
            (planner_parsed.get("steps") or [])
            if isinstance(planner_parsed, dict)
            else []
        )
        if not steps:
            return {
                "now_iso": now_iso,
                "timezone": timezone,
                "planner": planner_parsed,
                "step_execution_map": {},
            }

        # 🔒 LOCKED execution structure
        step_execution_map: Dict[str, Dict[str, Any]] = {}
        # ======================================================
        # 2) Execute steps sequentially
        # ======================================================
        for step in steps:
            step_id = int(step.get("step_id", 0) or 0)
            tool_name = str(step.get("tool", "") or "").strip()
            ack = str(step.get("ack_message", "") or "").strip()
            output_name = str(step.get("output", "") or "").strip()
            instruction = str(step.get("instruction", "") or "").strip()
            input_steps = step.get("input_steps", []) if isinstance(step, dict) else []
            input_steps = input_steps if isinstance(input_steps, list) else []

            # validate step
            if step_id < 1 or not tool_name or not output_name or not instruction:
                sid = str(step_id or (len(step_execution_map) + 1))
                step_execution_map[sid] = {
                    "step_id": step_id or (len(step_execution_map) + 1),
                    "tool": tool_name or "unknown",
                    "output_name": output_name or "unknown_output",
                    "ok": False,
                    "output_data": None,
                    "error": {
                        "type": "invalid_step",
                        "message": (
                            "Planner produced invalid step (missing"
                            " step_id/tool/output/instruction)."
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
            # Optional: show executor acknowledgement

            if ack:
                try:
                    if on_token:
                        on_token(ack, False)
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
                    "step_id": step_id,
                    "tool": tool_name,
                    "output_name": output_name,
                    "ok": False,
                    "output_data": None,
                    "error": {
                        "type": "tool_missing",
                        "message": f"Tool '{tool_name}' not found in registry",
                    },
                }
                logger.error("step %d tool missing: %s", step_id, tool_name)
                break

            tool_info = tool.get_info()
            tool_prompt = str(tool_info.get("prompt", "") or "")
            tool_call_format = str(tool_info.get("tool_call_format", "") or "")

            # reset per-step error context
            self.errors.clear()

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
                    tool_prompt = tool_info.get("error_prompt", tool_prompt)

                logger.info(
                    "step %d attempt %d/%d executor_call tool=%s",
                    step_id,
                    attempt,
                    self._max_step_attempts,
                    tool_name,
                )

                # 1) Ask executor for tool_call (or followup/abort)
                t0 = time.perf_counter()
                exec_payload = self.brain.run_executor(
                    instruction=f"Executing Step {step_id}\n" + instruction,
                    prior_outputs=json.dumps(prior_outputs, ensure_ascii=False),
                    step_followups=self.stack.appendix,
                    step_errors=self.errors.appendix,
                    tool_info=tool_prompt,
                    tool_call_format=tool_call_format,
                    stream=True,
                    on_token=on_token,
                    llm_options=llm_options,
                )
                exec_ms = int((time.perf_counter() - t0) * 1000)

                exec_result = exec_payload.get("parsed") or {}
                status = str(exec_result.get("status", "") or "").strip().lower()

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
                    fq = str(exec_result.get("followup_question", "") or "").strip()
                    logger.warning("step %d followup asked: %r", step_id, fq[:140])

                    rerun = await self.stack.handle(
                        followup=True,
                        followup_question=str(exec_result.get("followup_question", "")),
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
                        "step_id": step_id,
                        "tool": tool_name,
                        "output_name": output_name,
                        "ok": False,
                        "output_data": exec_result,
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
                # ABORT path (hard stop)
                # ---------------------------
                if status == "abort":
                    reason = str(
                        exec_result.get("abort_reason", "") or "Executor aborted"
                    ).strip()
                    step_execution_map[str(step_id)] = {
                        "step_id": step_id,
                        "tool": tool_name,
                        "output_name": output_name,
                        "ok": False,
                        "output_data": exec_result,
                        "error": {
                            "type": "executor_abort",
                            "message": reason,
                        },
                    }
                    logger.error("step %d executor_abort: %r", step_id, reason[:200])
                    break

                # ---------------------------
                # Invalid status (hard stop)
                # ---------------------------
                if status != "success":
                    step_execution_map[str(step_id)] = {
                        "step_id": step_id,
                        "tool": tool_name,
                        "output_name": output_name,
                        "ok": False,
                        "output_data": exec_result,
                        "error": {
                            "type": "invalid_executor_output",
                            "message": f"Executor returned invalid status='{status}'",
                        },
                    }
                    logger.error(
                        "step %d invalid_executor_output status=%r", step_id, status
                    )
                    break

                # 2) Parse tool call (invalid => ErrorStack, attempt++)
                tool_call_payload = exec_result.get("tool_call", {})
                try:
                    call_obj = tool.parse_call(tool_call_payload)
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    self.errors.add(
                        tool_result={
                            "error_type": "invalid_tool_call",
                            "message": "Tool call schema validation failed",
                            "evidence": msg,
                        },
                        attempt=attempt,
                    )
                    logger.warning(
                        "step %d attempt %d invalid_tool_call -> retry: %s",
                        step_id,
                        attempt,
                        msg[:200],
                    )
                    attempt += 1
                    continue

                tool_exec_result = tool.execute(call_obj, on_progress=on_token)

                # 4) Evaluate tool result
                ok = (
                    bool(tool_exec_result.get("OK", False))
                    if isinstance(tool_exec_result, dict)
                    else False
                )

                logger.info(
                    "step %d attempt %d tool_done ok=%s",
                    step_id,
                    attempt,
                    ok,
                )

                if ok:
                    # ✅ Step success
                    step_execution_map[str(step_id)] = {
                        "step_id": step_id,
                        "tool": tool_name,
                        "output_name": output_name,
                        "ok": True,
                        "output_data": tool_exec_result,
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
                    json.dumps(tool_exec_result, ensure_ascii=False, indent=2),
                )

                attempt += 1

            # if step failed, stop whole plan (v1)
            if (
                str(step_id) in step_execution_map
                and step_execution_map[str(step_id)].get("ok") is False
            ):
                logger.error("plan halted at step %d", step_id)
                break

        logger.debug(
            "step_execution_map=%s",
            json.dumps(step_execution_map, indent=2, ensure_ascii=False),
        )
        return {
            "now_iso": now_iso,
            "timezone": timezone,
            "planner": planner_parsed,
            "step_execution_map": step_execution_map,
        }

    # # ======================================================
    # # Heuristics for retry classification (minimal)
    # # ======================================================
    # @staticmethod
    # def _extract_tool_error_evidence(
    #     tool_exec_result: Any,
    # ) -> Tuple[str, str]:
    #     """
    #     Pull compact stderr + cmd/cmd from typical TerminalResult shapes.
    #     Returns (evidence, argv_preview)
    #     """
    #     if not isinstance(tool_exec_result, dict):
    #         return ("", "")

    #     evidence = ""
    #     cmd: str = ""

    #     outputs = tool_exec_result.get("outputs")
    #     if isinstance(outputs, list) and outputs:
    #         first = outputs[0] if isinstance(outputs[0], dict) else {}

    #         evidence = str(first.get("error", "") or "")

    #         cmd = str(first.get("command", "") or "").strip()

    #     return (evidence, cmd)

    # -------------------------
    # Time helper
    # -------------------------
    @staticmethod
    def _get_time_info() -> Tuple[str, str]:
        lt = time.localtime()
        tz_name = time.tzname[1] if lt.tm_isdst > 0 else time.tzname[0]
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z", lt)
        return now, tz_name
