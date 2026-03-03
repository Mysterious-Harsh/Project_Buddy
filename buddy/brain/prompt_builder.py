# buddy/brain/prompt_builder.py
from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Dict, Optional

# ==========================================================
# PromptBuilder (FINAL / MINIMAL)
# ==========================================================
# Rules:
# - ZERO logic (no context composition, no reasoning, no routing).
# - Only inject already-prepared strings/values into prompt templates.
# - Returns a small payload dict so Brain can pass it to the LLM.
# - Prompts are loaded dynamically at runtime (no restart needed).
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


def _load_prompt_var(module_path: str, var_name: str) -> str:
    """
    Dynamically import + reload the module each time, then read var_name.
    This allows prompt edits to take effect at runtime without restart.
    """
    try:
        mod: ModuleType = importlib.import_module(module_path)
        mod = importlib.reload(mod)
        val = getattr(mod, var_name, "")
        return str(val or "")
    except Exception:
        return ""


class PromptBuilder:
    """
    Minimal prompt builder:
    - loads prompt templates (strings) dynamically at runtime
    - injects variables
    - returns PromptPayload (prompt/system/temp)
    """

    def __init__(
        self,
        *,
        brain_prompt: Optional[str] = None,
        retrieval_gate_prompt: Optional[str] = None,
        followup_prompt: Optional[str] = None,
        llm_only_prompt: Optional[str] = None,
        planner_prompt: Optional[str] = None,
    ):
        # Store overrides (if provided). Otherwise load dynamically at build-time.
        self._brain_prompt_override = (
            str(brain_prompt) if brain_prompt is not None else None
        )
        self._retrieval_gate_prompt = (
            str(retrieval_gate_prompt) if retrieval_gate_prompt is not None else None
        )
        self._followup_prompt_override = (
            str(followup_prompt) if followup_prompt is not None else None
        )
        self._llm_only_prompt_override = (
            str(llm_only_prompt) if llm_only_prompt is not None else None
        )
        self._planner_prompt_override = (
            str(planner_prompt) if planner_prompt is not None else None
        )

    # ------------------------------------------------------
    # Brain prompt (Decision + Ingestion)
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
            else _load_prompt_var(
                "buddy.prompts.brain_prompts", "RETRIEVAL_GATE_PROMPT"
            )
        )

        prompt = _fmt(
            template,
            now_iso=str(now_iso),
            timezone=str(timezone),
            user_query=str(user_query),
            recent_turns=str(recent_turns),
        )

        return prompt

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
        template = _load_prompt_var("buddy.prompts.brain_prompts", "BRAIN_PROMPT")

        prompt = _fmt(
            template,
            now_iso=str(now_iso),
            timezone=str(timezone),
            user_query=str(user_query),
            recent_turns=str(recent_turns),
            memories=str(memories),
        )

        return prompt

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
        template = _load_prompt_var("buddy.prompts.planner_prompts", "PLANNER_PROMPT")

        prompt = _fmt(
            template,
            now_iso=str(now_iso),
            timezone=str(timezone),
            memories=str(memories),
            user_query=str(user_query),
            user_intent=str(intent),
            available_tools=str(available_tools),
        )

        return prompt

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
        template = _load_prompt_var("buddy.prompts.executor_prompts", "EXECUTOR_PROMPT")

        prompt = _fmt(
            template,
            now_iso=str(now_iso),
            timezone=str(timezone),
            instruction=str(instruction),
            prior_outputs=str(prior_outputs),
            step_followups=str(step_followups),
            step_errors=str(step_errors),
            tool_info=str(tool_info),
            tool_call_format=str(tool_call_format),
        )

        return prompt

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
        template = _load_prompt_var(
            "buddy.prompts.memory_prompts", "MEMORY_SUMMARY_PROMPT"
        )

        prompt = _fmt(
            template,
            now_iso=str(now_iso),
            timezone=str(timezone),
            memories=str(memories),
        )

        return prompt

    # ------------------------------------------------------
    # Executor prompt (Single-step tool execution)
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
        template = _load_prompt_var("buddy.prompts.respond_prompts", "RESPOND_PROMPT")

        prompt = _fmt(
            template,
            now_iso=str(now_iso),
            timezone=str(timezone),
            memories=str(memories),
            execution_results=str(execution_results),
            user_query=str(user_query),
        )

        return prompt
