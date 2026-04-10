# buddy/brain/prompt_builder.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from buddy.prompts.brain_prompts import BRAIN_PROMPT, RETRIEVAL_GATE_PROMPT
from buddy.prompts.planner_prompts import PLANNER_PROMPT
from buddy.prompts.executor_prompts import EXECUTOR_PROMPT
from buddy.prompts.respond_prompts import RESPOND_PROMPT
from buddy.prompts.memory_prompts import MEMORY_SUMMARY_PROMPT

# ==========================================================
# PromptBuilder (FINAL / MINIMAL)
# ==========================================================
# Rules:
# - ZERO logic (no context composition, no reasoning, no routing).
# - Only inject already-prepared strings/values into prompt templates.
# - Returns a small payload dict so Brain can pass it to the LLM.
# - Prompts are imported once at startup (no hot-reload).
# ==========================================================


@dataclass(frozen=True)
class PromptPayload:
    prompt: str
    system: Optional[str] = None
    temperature: float = 0.2

    def as_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "system": self.system,
            "temperature": float(self.temperature),
        }


def _fmt(template: str, **kwargs: Any) -> str:
    """
    Strict formatting helper.
    We do NOT "guess" missing keys. If Brain didn't provide required args,
    fail loudly so it's easy to debug.
    """
    try:
        return str(template).format(**kwargs)
    except KeyError as ex:
        missing = str(ex).strip("'")
        raise ValueError(f"Missing prompt var: {missing}") from ex


class PromptBuilder:
    """
    Minimal prompt builder:
    - uses prompt templates imported at startup
    - injects variables
    - returns PromptPayload (prompt/system/temp)
    """

    def __init__(
        self,
        *,
        retrieval_gate_prompt: Optional[str] = None,
        planner_prompt: Optional[str] = None,
    ):
        # Optional overrides for testing. Otherwise module-level constants are used.
        self._retrieval_gate_prompt = (
            str(retrieval_gate_prompt) if retrieval_gate_prompt is not None else None
        )
        self._planner_prompt_override = (
            str(planner_prompt) if planner_prompt is not None else None
        )

    # ------------------------------------------------------
    # Retrieval Gate prompt
    # ------------------------------------------------------
    def build_retrieval_gate_prompt(
        self,
        *,
        now_iso: str,
        timezone: str,
        user_query: str,
        recent_turns: str,
    ) -> str:
        template = (
            self._retrieval_gate_prompt
            if self._retrieval_gate_prompt is not None
            else RETRIEVAL_GATE_PROMPT
        )

        return _fmt(
            template,
            now_iso=str(now_iso),
            timezone=str(timezone),
            user_query=str(user_query),
            recent_turns=str(recent_turns),
        )

    # ------------------------------------------------------
    # Brain prompt (Decision + Ingestion)
    # ------------------------------------------------------
    def build_brain_prompt(
        self,
        *,
        now_iso: str,
        timezone: str,
        user_query: str,
        recent_turns: str,
        memories: str,
    ) -> str:
        return _fmt(
            BRAIN_PROMPT,
            now_iso=str(now_iso),
            timezone=str(timezone),
            user_query=str(user_query),
            recent_turns=str(recent_turns),
            memories=str(memories),
        )

    # ------------------------------------------------------
    # Planner prompt (Steps + Followup)
    # ------------------------------------------------------
    def build_planner_prompt(
        self,
        *,
        now_iso: str,
        timezone: str,
        memories: str,
        user_query: str,
        intent: str,
        available_tools: str,
    ) -> str:
        template = (
            self._planner_prompt_override
            if self._planner_prompt_override is not None
            else PLANNER_PROMPT
        )

        return _fmt(
            template,
            now_iso=str(now_iso),
            timezone=str(timezone),
            memories=str(memories),
            user_query=str(user_query),
            user_intent=str(intent),
            available_tools=str(available_tools),
        )

    # ------------------------------------------------------
    # Executor prompt (Single-step tool execution)
    # ------------------------------------------------------
    def build_executor_prompt(
        self,
        *,
        now_iso: str,
        timezone: str,
        instruction: str,
        prior_outputs: str,
        step_followups: str,
        step_errors: str,
        tool_info: str,
        tool_call_format: str,
    ) -> str:
        return _fmt(
            EXECUTOR_PROMPT,
            now_iso=str(now_iso),
            timezone=str(timezone),
            instruction=str(instruction),
            prior_outputs=str(prior_outputs),
            step_followups=str(step_followups),
            step_errors=str(step_errors),
            tool_info=str(tool_info),
            tool_call_format=str(tool_call_format),
        )

    # ------------------------------------------------------
    # Memory Summary Prompt
    # ------------------------------------------------------
    def build_memory_summary_prompt(
        self,
        *,
        now_iso: str,
        timezone: str,
        memories: str,
    ) -> str:
        return _fmt(
            MEMORY_SUMMARY_PROMPT,
            now_iso=str(now_iso),
            timezone=str(timezone),
            memories=str(memories),
        )

    # ------------------------------------------------------
    # Respond prompt
    # ------------------------------------------------------
    def build_respond_prompt(
        self,
        *,
        now_iso: str,
        timezone: str,
        user_query: str,
        memories: str,
        execution_results: str,
    ) -> str:
        return _fmt(
            RESPOND_PROMPT,
            now_iso=str(now_iso),
            timezone=str(timezone),
            memories=str(memories),
            execution_results=str(execution_results),
            user_query=str(user_query),
        )
