# buddy/brain/brain.py
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union

from buddy.logger.logger import get_logger
from buddy.brain.output_parser import OutputParser
from buddy.brain.prompt_builder import build_prompt
from buddy.prompts.base_system_prompts import (
    BUDDY_IDENTITY,
    BUDDY_BEHAVIOR,
    BUDDY_MEMORY,
    BUDDY_OUTPUT,
)
from buddy.prompts.brain_prompts import (
    BRAIN_PROMPT,
    RETRIEVAL_GATE_PROMPT,
    RETRIEVAL_GATE_PROMPT_SCHEMA,
    BRAIN_PROMPT_SCHEMA,
)
from buddy.prompts.planner_prompts import PLANNER_PROMPT, PLANNER_PROMPT_SCHEMA
from buddy.prompts.executor_prompts import EXECUTOR_PROMPT, EXECUTOR_PROMPT_SCHEMA
from buddy.prompts.respond_prompts import RESPOND_PROMPT, RESPOND_PROMPT_SCHEMA
from buddy.prompts.memory_prompts import (
    MEMORY_SUMMARY_PROMPT,
    MEMORY_SUMMARY_PROMPT_SCHEMA,
)
from buddy.prompts.reader_prompts import (
    READER_PROMPT,
    READER_SCHEMA,
    READER_TASK_TEMPLATE,
    READER_CONTEXT_EMPTY,
)
from buddy.prompts.vision_prompts import (
    VISION_PROMPT,
    VISION_SCHEMA,
    VISION_TASK_TEMPLATE,
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
        # Vision: list of {"data": "<base64>", "id": N} dicts
        image_data: Optional[List[Dict[str, Any]]] = None,
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
        self.parser = OutputParser()
        self.debug = bool(debug)
        self.system_prompt = _render_system_prompt(os_profile=os_profile)
        self._interrupt_event: Optional[threading.Event] = None

    def _build_system_prompt(self, skills: list = []):
        return self.system_prompt + "\n" + "\n".join(skills)

    def _build_context(
        self,
        *,
        now_iso: str,
        timezone: str,
        recent_turns: Optional[str] = None,
        memories: Optional[str] = None,
        available_tools: Optional[str] = None,
        prior_outputs: Optional[str] = None,
        step_followups: Optional[str] = None,
        step_errors: Optional[str] = None,
        tool_info: Optional[str] = None,
        execution_results: Optional[str] = None,
        responder_note: Optional[str] = None,
    ) -> str:
        """
        Build the shared CONTEXT section for all prompts.
        This is the only place where we define the formatting of the context.
        """
        context_parts = [
            "<CONTEXT>",
            f"<NOW_ISO>\n{now_iso}\n</NOW_ISO>",
            f"<TIMEZONE>\n{timezone}\n</TIMEZONE>",
        ]
        if memories is not None:
            context_parts.append(f"<MEMORIES>\n{memories}\n</MEMORIES>")
        if recent_turns is not None:
            context_parts.append(
                f"<CONVERSATION_HISTORY>\n{recent_turns}\n</CONVERSATION_HISTORY>"
            )
        if available_tools is not None:
            context_parts.append(
                f"<AVAILABLE_TOOLS>\n{available_tools}\n</AVAILABLE_TOOLS>"
            )
        if prior_outputs is not None:
            context_parts.append(f"<PRIOR_OUTPUTS>\n{prior_outputs}\n</PRIOR_OUTPUTS>")
        if step_followups is not None:
            context_parts.append(
                f"<STEP_FOLLOWUPS>\n{step_followups}\n</STEP_FOLLOWUPS>"
            )
        if step_errors is not None:
            context_parts.append(f"<STEP_ERRORS>\n{step_errors}\n</STEP_ERRORS>")
        if tool_info is not None:
            context_parts.append(
                f"<TOOL_INSTRUCTIONS>\n{tool_info}\n</TOOL_INSTRUCTIONS>"
            )
        if execution_results is not None:
            context_parts.append(
                f"<EXECUTION_RESULTS>\n{execution_results}\n</EXECUTION_RESULTS>"
            )
        if responder_note:
            context_parts.append(f"<PLANNER_NOTE>\n{responder_note}\n</PLANNER_NOTE>")
        context_parts.append("</CONTEXT>")
        return "\n".join(context_parts)

    # ------------------------------------------------------
    # Public: Prompt runners
    # ------------------------------------------------------
    def set_interrupt(self, interrupt_event: threading.Event):
        self._interrupt_event = interrupt_event

    def run_memory_gate(
        self,
        *,
        active_task: str,
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
        context = self._build_context(
            now_iso=now_iso,
            timezone=timezone,
            recent_turns=recent_turns,
        )

        system_prompt = self._build_system_prompt([
            RETRIEVAL_GATE_PROMPT,
            BUDDY_OUTPUT.format(schema=RETRIEVAL_GATE_PROMPT_SCHEMA),
        ])
        prompt = build_prompt(
            system=system_prompt,
            context=context,
            task_input=active_task,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
            on_token=on_token,
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
        active_task: str,
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
        context = self._build_context(
            now_iso=now_iso,
            timezone=timezone,
            recent_turns=recent_turns,
            memories=memories,
        )

        system_prompt = self._build_system_prompt([
            BUDDY_MEMORY,
            BUDDY_BEHAVIOR,
            BRAIN_PROMPT,
            BUDDY_OUTPUT.format(schema=BRAIN_PROMPT_SCHEMA),
        ])
        prompt = build_prompt(
            system=system_prompt,
            context=context,
            task_input=active_task,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
            on_token=on_token,
            options=llm_options,
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
        active_task: str,
        planner_instructions: str,
        memories: str,
        available_tools: str,
        temperature: float = 0.4,
        top_p: float = 1.0,
        repeat_penalty: float = 1.08,
        repeat_last_n: int = 128,
        stream: bool = True,
        on_token: Optional[Callable[[str], None]] = None,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Planner prompt (JSON expected).
        Strict validation via OutputParser.parse_planner().
        """
        now_iso, timezone = self._get_time_info()
        context = self._build_context(
            now_iso=now_iso,
            timezone=timezone,
            memories=memories,
            available_tools=available_tools,
        )

        system_prompt = self._build_system_prompt([
            BUDDY_MEMORY,
            PLANNER_PROMPT,
            BUDDY_OUTPUT.format(schema=PLANNER_PROMPT_SCHEMA),
        ])
        prompt = build_prompt(
            system=system_prompt,
            context=context,
            task_input=(planner_instructions + "\n\n" + active_task),
        )

        raw = self._call_llm_generate(
            prompt=prompt,
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
        step_followups: Optional[str] = "",
        step_errors: Optional[str] = "",
        tool_info: str,
        tool_call_format: str,
        temperature: float = 0.2,
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
        if step_followups == "":
            step_followups = None
        if step_errors == "":
            step_errors = None
        context = self._build_context(
            now_iso=now_iso,
            timezone=timezone,
            prior_outputs=prior_outputs,
            step_followups=step_followups,
            step_errors=step_errors,
            tool_info=tool_info,
        )

        system_prompt = self._build_system_prompt([
            EXECUTOR_PROMPT,
            BUDDY_OUTPUT.format(
                schema=EXECUTOR_PROMPT_SCHEMA.format(tool_call_format=tool_call_format)
            ),
        ])
        prompt = build_prompt(
            system=system_prompt,
            context=context,
            task_input=instruction,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
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
        temperature: float = 0.4,
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
        context = self._build_context(
            now_iso=now_iso,
            timezone=timezone,
            memories=memories,
        )

        system_prompt = self._build_system_prompt([
            BUDDY_MEMORY,
            MEMORY_SUMMARY_PROMPT,
            BUDDY_OUTPUT.format(schema=MEMORY_SUMMARY_PROMPT_SCHEMA),
        ])
        prompt = build_prompt(
            system=system_prompt,
            context=context,
            task_input=(
                "Summarize your current memories into a concise summary that captures"
                " key information and insights. Focus on what would change future"
                " behavior if forgotten."
            ),
        )

        raw = self._call_llm_generate(
            prompt=prompt,
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
        active_task: str,
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
        context = self._build_context(
            now_iso=now_iso,
            timezone=timezone,
            memories=memories,
            execution_results=execution_results,
        )

        system_prompt = self._build_system_prompt([
            BUDDY_BEHAVIOR,
            RESPOND_PROMPT,
            BUDDY_OUTPUT.format(schema=RESPOND_PROMPT_SCHEMA),
        ])
        prompt = build_prompt(
            system=system_prompt,
            context=context,
            task_input=active_task,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
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

    def run_reader(
        self,
        *,
        paragraph: str,
        query: str,
        rolling_context: str = "",
        temperature: float = 0.2,
        top_p: float = 0.98,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 64,
    ) -> Dict[str, Any]:
        """
        Runs one paragraph through the reader prompt.
        Called in a loop by TextReader for large text processing.

        Returns dict with keys: relevant (bool), content (str).
        """
        now_iso, timezone = self._get_time_info()

        context = self._build_context(
            now_iso=now_iso,
            timezone=timezone,
            prior_outputs=rolling_context if rolling_context else READER_CONTEXT_EMPTY,
        )

        system_prompt = self._build_system_prompt([
            READER_PROMPT,
            BUDDY_OUTPUT.format(schema=READER_SCHEMA),
        ])

        prompt = build_prompt(
            system=system_prompt,
            context=context,
            task_input=READER_TASK_TEMPLATE.format(
                query=query,
                paragraph=paragraph,
            ),
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=False,
            on_token=None,
            options=None,
            json_mode=True,
        )

        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(result, dict):
                return {
                    "relevant": bool(result.get("relevant", False)),
                    "content": str(result.get("content", "")),
                }
        except Exception:
            pass

        return {"relevant": False, "content": ""}

    def run_vision(
        self,
        *,
        image_paths: "Union[str, List[str]]",
        query: str,
    ) -> Dict[str, Any]:
        """
        Analyze one or more images using the vision-capable LLM (Qwen3.5).

        Uses native llama.cpp /completion endpoint with image_data + [img-N] tokens.
        Returns a dict with keys: description, objects, text_found, key_finding.
        On failure returns {"error": str}.

        Called by VisionTool.execute() via the brain kwarg.
        """
        import os as _os

        from buddy.tools.vision.image_encoder import encode_image, is_image_path

        # Normalize to list
        if isinstance(image_paths, str):
            image_paths = [image_paths]

        if not image_paths:
            return {"error": "No image paths provided"}

        # Encode all images — fail fast on first error
        encoded: List[Dict[str, Any]] = []
        for idx, path in enumerate(image_paths, start=1):
            if not path or not is_image_path(path):
                return {"error": f"Not a recognized image file: {path}"}
            try:
                b64 = encode_image(path)
            except (FileNotFoundError, ValueError, OSError) as exc:
                logger.warning("run_vision encode failed path=%r err=%r", path, exc)
                return {"error": str(exc)}
            encoded.append(
                {"id": idx, "data": b64, "filename": _os.path.basename(path)}
            )

        # Build image_data list and [img-N] prompt tokens
        image_data = [{"data": img["data"], "id": img["id"]} for img in encoded]
        img_tokens = " ".join(f"[img-{img['id']}]" for img in encoded)
        filenames = ", ".join(img["filename"] for img in encoded)

        # Build prompt
        system_prompt = self._build_system_prompt([
            VISION_PROMPT,
            BUDDY_OUTPUT.format(schema=VISION_SCHEMA),
        ])
        now_iso, timezone = self._get_time_info()
        context = self._build_context(now_iso=now_iso, timezone=timezone)
        task_input = VISION_TASK_TEMPLATE.format(
            img_tokens=img_tokens,
            filename=filenames,
            query=query,
        )
        prompt = build_prompt(
            system=system_prompt,
            context=context,
            task_input=task_input,
        )

        try:
            raw = self.llm.generate(
                prompt=prompt,
                stream=False,
                temperature=0.2,
                top_p=0.98,
                repeat_penalty=1.0,
                repeat_last_n=64,
                stop=["<|im_end|>"],
                image_data=image_data,
                json_extract=True,
                json_validate=True,
                json_root="object",
                json_max_chars=32_000,
            )
        except Exception as exc:
            logger.warning(
                "run_vision LLM call failed paths=%r err=%r", image_paths, exc
            )
            return {"error": str(exc)}

        # Parse result
        try:
            result = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(result, dict):
                return {
                    "description": str(result.get("description", "")).strip(),
                    "objects": result.get("objects") or [],
                    "text_found": str(result.get("text_found", "")).strip(),
                    "key_finding": str(result.get("key_finding", "")).strip(),
                }
        except Exception:
            pass

        # Fallback: raw text as description
        return {
            "description": str(raw).strip(),
            "objects": [],
            "text_found": "",
            "key_finding": str(raw).strip(),
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
        system: Optional[str] = None,
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
            stop=["<|im_end|>"],
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
