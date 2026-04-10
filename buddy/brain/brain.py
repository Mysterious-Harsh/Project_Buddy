# buddy/brain/brain.py
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union

from buddy.logger.logger import get_logger
from buddy.brain.output_parser import OutputParser
from buddy.brain.prompt_builder import PromptBuilder
from buddy.prompts.base_system_prompts import (
    BUDDY_IDENTITY,
    BUDDY_BEHAVIOR,
    BUDDY_MEMORY,
)

import json
import threading

logger = get_logger("brain")


# ==========================================================
# Protocols
# ==========================================================
class LLM(Protocol):

    def generate(
        self,
        *,
        prompt: str,
        system: Optional[str] = None,
        stream: bool = False,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        n_predict: Optional[int] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        repeat_last_n: Optional[int] = None,
        seed: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        options: Optional[Dict[str, Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        # JSON extraction (streaming-optimized)
        json_extract: bool = False,
        json_validate: bool = False,
        json_root: str = "object",
        json_max_chars: int = 120_000,
        interrupt_event: Optional[threading.Event] = None,
    ) -> str: ...


def _render_system_prompt(*, os_profile: Dict[str, Any]) -> str:
    fallback = "You are Buddy — the user's trusted best friend and personal companion."
    try:
        compact = {
            "username": os_profile.get("username", "user"),
            "platform": os_profile.get("platform", {}),
            "os_hints": os_profile.get("os_hints", {}),
            "cpu": {
                "model": os_profile.get("cpu", {}).get("model"),
                "logical_cores": os_profile.get("cpu", {}).get("logical_cores"),
            },
            "ram": {"total_gb": os_profile.get("ram", {}).get("total_gb")},
            "gpu": {
                "backend": os_profile.get("gpu", {}).get("backend"),
                "name": os_profile.get("gpu", {}).get("name"),
            },
        }
        pref = (
            str(
                os_profile.get("user_preferred_name")
                or os_profile.get("username")
                or "User"
            ).strip()
            or "User"
        )
        rendered = BUDDY_IDENTITY.format(
            os_profile=json.dumps(
                compact, ensure_ascii=False, sort_keys=True, indent=2
            ),
            user_preferred_name=pref,
        ).strip()
        return rendered or fallback
    except Exception as ex:
        return fallback


# ==========================================================
# Brain
# ==========================================================
class Brain:
    """
    Buddy Brain (v1):
    - compose_context() contains context/memory formatting logic
    - PromptBuilder formats templates
    - OutputParser parses + validates
    - LLM calls go through generate() only
    """

    def __init__(
        self,
        *,
        llm: LLM,
        os_profile: dict[str, Any] = {},
        debug: bool = False,
    ) -> None:
        self.llm = llm
        self.prompts = PromptBuilder()
        self.parser = OutputParser()
        self.debug = bool(debug)
        self.system_prompt = _render_system_prompt(os_profile=os_profile)
        self._interrupt_event: Optional[threading.Event] = None

    def _build_system_prompt(self, skills: list = []):
        return (
            """<SYSTEM>\n"""
            + self.system_prompt
            + "\n".join(skills)
            + """\n</SYSTEM>"""
        )

    # ------------------------------------------------------
    # Public: Prompt runners
    # ------------------------------------------------------
    def set_interrupt(self, interrupt_event: threading.Event):
        self._interrupt_event = interrupt_event

    def run_memory_gate(
        self,
        *,
        user_current_message: str,
        recent_turns: str,
        temperature: float = 0.2,
        top_p: float = 0.98,
        repeat_penalty: float = 1.12,
        repeat_last_n: int = 128,
        stream: bool = True,
        on_token: Optional[Callable[[str], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Retrieval Gate prompt (JSON expected):
        { "ack_message": str, "search_queries": [], "deep_recall": bool }

        Strict validation via OutputParser.parse_retrieval_gate().
        """
        now_iso, timezone = self._get_time_info()

        try:
            prompt_text = self.prompts.build_retrieval_gate_prompt(
                now_iso=now_iso,
                timezone=timezone,
                user_query=user_current_message,
                recent_turns=recent_turns,
            )
        except Exception:
            logger.exception("Error building retrieval gate prompt")
            raise
        system_prompt = self._build_system_prompt()

        raw = self._call_llm_generate(
            prompt=prompt_text,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
            on_token=on_token,
            system=system_prompt,
            options=llm_options,
            # n_predict=256,  # tiny JSON: {ack_message, search_queries[], deep_recall}
            json_mode=True,  # ✅ MUST extract/validate JSON here
        )

        parsed = self.parser.parse_retrieval_gate(raw)

        return {
            "raw_text": raw,
            "parsed": parsed,
        }

    def run_brain(
        self,
        *,
        user_current_message: str,
        recent_turns: str,
        memories: str,
        temperature: float = 0.2,
        top_p: float = 0.98,
        repeat_penalty: float = 1.12,
        repeat_last_n: int = 128,
        stream: bool = True,
        on_token: Optional[Callable[[str], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Decision+Ingestion prompt (JSON expected).
        Strict validation via OutputParser.parse_brain().
        """
        now_iso, timezone = self._get_time_info()

        try:
            prompt_text = self.prompts.build_brain_prompt(
                now_iso=now_iso,
                timezone=timezone,
                user_query=user_current_message,
                recent_turns=recent_turns,
                memories=memories,
            )
        except Exception:
            logger.exception("Error building brain prompt")
            raise
        system_prompt = self._build_system_prompt([BUDDY_MEMORY, BUDDY_BEHAVIOR])

        raw = self._call_llm_generate(
            prompt=prompt_text,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
            on_token=on_token,
            options=llm_options,
            system=system_prompt,
            # n_predict=768,  # decision + memories JSON, CHAT response can be ~300 tokens
            json_mode=True,  # ✅ MUST extract/validate JSON here
        )

        parsed = self.parser.parse_brain(raw)

        return {
            "raw_text": raw,
            "parsed": parsed,
        }

    def run_planner(
        self,
        *,
        user_current_message: str,
        intent: str,
        memories: str,
        available_tools: str,
        temperature: float = 0.2,
        top_p: float = 1.0,
        repeat_penalty: float = 1.12,
        repeat_last_n: int = 86,
        stream: bool = True,
        on_token: Optional[Callable[[str], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Planner prompt (JSON expected).
        Strict validation via OutputParser.parse_planner().
        """
        now_iso, timezone = self._get_time_info()

        try:
            prompt_text = self.prompts.build_planner_prompt(
                now_iso=str(now_iso),
                timezone=str(timezone),
                memories=str(memories),
                intent=str(intent),
                user_query=user_current_message,
                available_tools=available_tools,
            )
        except Exception:
            logger.exception("Error building planner prompt")
            raise
        system_prompt = self._build_system_prompt([BUDDY_MEMORY])

        raw = self._call_llm_generate(
            prompt=prompt_text,
            system=system_prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
            on_token=on_token,
            options=llm_options,
            # n_predict=2048,  # steps[] with reasoning can be large for complex tasks
            json_mode=True,  # ✅ MUST extract/validate JSON here
        )

        parsed = self.parser.parse_planner(raw)

        return {
            "raw_text": raw,
            "parsed": parsed,
        }

    def run_executor(
        self,
        *,
        instruction: str,
        prior_outputs: str,
        step_followups: str,
        step_errors: str,
        tool_info: str,
        tool_call_format: str,
        temperature: float = 0.12,
        top_p: float = 1.0,
        repeat_penalty: float = 1.12,
        repeat_last_n: int = 64,
        stream: bool = True,
        on_token: Optional[Callable[[str], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Executor prompt (single-step execution).
        Strict validation via OutputParser.parse_executor().
        """
        now_iso, timezone = self._get_time_info()

        try:
            prompt_text = self.prompts.build_executor_prompt(
                now_iso=str(now_iso),
                timezone=str(timezone),
                instruction=str(instruction),
                prior_outputs=str(prior_outputs),
                step_followups=str(step_followups),
                step_errors=str(step_errors),
                tool_info=str(tool_info),
                tool_call_format=str(tool_call_format),
            )
        except Exception:
            logger.exception("Error building executor prompt")
            raise
        system_prompt = self._build_system_prompt()

        raw = self._call_llm_generate(
            prompt=prompt_text,
            system=system_prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
            on_token=on_token,
            options=llm_options,
            # n_predict=1024,  # THINK reasoning + JSON tool_call
            json_mode=True,  # executor MUST be strict JSON
        )

        parsed = self.parser.parse_executor(raw)

        return {
            "raw_text": raw,
            "parsed": parsed,
        }

    def run_memory_summary(
        self,
        *,
        memories: str,
        temperature: float = 0.2,
        top_p: float = 0.98,
        repeat_penalty: float = 1.12,
        repeat_last_n: int = 128,
        stream: bool = True,
        on_token: Optional[Callable[[str], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Memory Summary prompt (JSON expected).
        Strict validation via OutputParser.parse_memory_summary().
        """
        now_iso, timezone = self._get_time_info()

        try:
            prompt_text = self.prompts.build_memory_summary_prompt(
                now_iso=now_iso,
                timezone=timezone,
                memories=memories,
            )
        except Exception:
            logger.exception("Error building retrieval gate prompt")
            raise
        system_prompt = self._build_system_prompt([BUDDY_MEMORY])

        raw = self._call_llm_generate(
            prompt=prompt_text,
            system=system_prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
            on_token=on_token,
            options=llm_options,
            # n_predict=512,  # summary JSON
            json_mode=True,  # ✅ MUST extract/validate JSON here
        )

        parsed = self.parser.parse_memory_summary(raw)

        return {
            "raw_text": raw,
            "parsed": parsed,
        }

    def run_respond(
        self,
        *,
        user_current_message: str,
        memories: str,
        execution_results: str,
        temperature: float = 0.2,
        top_p: float = 0.98,
        repeat_penalty: float = 1.12,
        repeat_last_n: int = 128,
        stream: bool = True,
        on_token: Optional[Callable[[str], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Respond prompt (JSON expected).
        Strict validation via OutputParser.parse_respond().
        """
        now_iso, timezone = self._get_time_info()

        try:
            prompt_text = self.prompts.build_respond_prompt(
                now_iso=str(now_iso),
                timezone=str(timezone),
                memories=str(memories),
                execution_results=str(execution_results),
                user_query=user_current_message,
            )
        except Exception:
            logger.exception("Error building planner prompt")
            raise
        system_prompt = self._build_system_prompt([BUDDY_BEHAVIOR])

        raw = self._call_llm_generate(
            prompt=prompt_text,
            system=system_prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
            on_token=on_token,
            options=llm_options,
            # n_predict=1024,  # response + memory_candidates JSON
            json_mode=True,  # ✅ MUST extract/validate JSON here
        )

        parsed = self.parser.parse_respond(raw)

        return {
            "raw_text": raw,
            "parsed": parsed,
        }

    # ======================================================
    # Internal: LLM call (generate only)
    # ======================================================
    def _call_llm_generate(
        self,
        *,
        prompt: str,
        temperature: float,
        stream: bool,
        json_mode: bool,
        top_p: float = 0.98,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 64,
        n_predict: Optional[int] = None,
        on_token: Optional[Callable[[str], None]],
        options: Optional[Dict[str, Any]],
        system: Optional[str] = "",
    ) -> str:

        # Normalize options for predictable downstream handling
        opts: Dict[str, Any] = options if isinstance(options, dict) else {}

        if self.debug:
            logger.debug(
                "LLM call: stream=%s json_mode=%s sys_len=%d prompt_len=%d"
                " n_predict=%s",
                bool(stream),
                bool(json_mode),
                len(system) if system else 0,
                len(prompt),
                n_predict,
            )

        out = self.llm.generate(
            prompt=prompt,
            system=system,
            stream=bool(stream),
            temperature=float(temperature),
            top_p=float(top_p),
            repeat_penalty=float(repeat_penalty),
            repeat_last_n=int(repeat_last_n),
            n_predict=n_predict,
            options=opts,
            on_delta=(on_token if stream else None),
            # ✅ JSON pipeline toggles
            json_extract=bool(json_mode),
            json_validate=bool(json_mode),
            json_root="object",
            json_max_chars=120_000,
            stop=["<END_OUTPUT>", "</BEGIN_OUTPUT> ", "</BUDDY>", "</BEGIN_JSON>"],
            interrupt_event=self._interrupt_event,
        )

        text = "" if out is None else str(out)

        if self.debug:
            logger.debug(f"LLM output: \n {text}")

        return text

    # -------------------------
    # Helpers
    # -------------------------
    @staticmethod
    def _get_time_info() -> Tuple[str, str]:

        lt = time.localtime()
        tz_name = time.tzname[1] if lt.tm_isdst > 0 else time.tzname[0]
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z", lt)
        return now, tz_name
