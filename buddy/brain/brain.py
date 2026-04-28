# buddy/brain/brain.py
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, Union

from buddy.logger.logger import get_logger
from buddy.brain.output_parser import OutputParser
from buddy.brain.prompt_builder import (
    build_retrieval_prompt,
    build_brain_prompt,
    build_planner_prompt,
    build_executor_prompt,
    build_responder_prompt,
    build_reader_prompt,
    build_memory_summary_prompt,
)
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
)
from buddy.prompts.browser_prompts import (
    BROWSER_ACTION_PROMPT,
    BROWSER_ACTION_SCHEMA,
)

import json
import threading
from datetime import datetime as _datetime

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
        think: bool = True,
        gate_marker: Optional[str] = None,
    ) -> str: ...
    def chat(
        self,
        *,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        stream: bool = True,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        repeat_last_n: Optional[int] = None,
        seed: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        options: Optional[Dict[str, Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        interrupt_event: Optional[threading.Event] = None,
        # Vision: data URIs for multimodal input ["data:image/jpeg;base64,...", ...]
        # Injected into the last user message as OAI content array (image_url entries).
        # Supports multiple images — one entry per image.
        images: Optional[List[str]] = None,
        # JSON extraction (streaming-optimized, mirrors generate())
        json_extract: bool = False,
        json_validate: bool = False,
        json_root: str = "object",
        json_max_chars: int = 120_000,
        # Think + gate: wait for </think> before JSON capture; look for gate_marker
        # before scanning for JSON (None = scan directly after think/start).
        think: bool = True,
        gate_marker: Optional[str] = None,
    ) -> str: ...


def _render_system_prompt(*, username: str, os_profile: Dict[str, Any]) -> str:
    fallback = "You are Buddy — the user's trusted best friend and personal companion."
    try:
        compact = {
            "platform": os_profile.get("platform", {}),
            "hardware": os_profile.get("hardware", {}),
            "runtime": os_profile.get("runtime", {}),
            "paths": os_profile.get("paths", {}),
        }

        rendered = BUDDY_IDENTITY.format(
            os_profile=json.dumps(
                compact, ensure_ascii=False, sort_keys=True, indent=2
            ),
            user_preferred_name=username,
        ).strip()
        return rendered or fallback
    except Exception as ex:
        logger.warning(
            "Failed to render system prompt with os_profile=%r: %r", os_profile, ex
        )
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
    - Text calls go through generate() via _call_llm_generate()
    - Vision calls go through chat() with image_url content parts
    """

    # Location cache — shared across all Brain instances, refreshed every hour
    _location: str = ""
    _location_ts: float = 0.0
    _LOCATION_TTL: float = 3600.0

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
        identity = os_profile.get("identity", {})
        self.username = str(
            identity.get("preferred_name") or identity.get("username") or "User"
        ).strip()
        self.system_prompt = _render_system_prompt(
            username=self.username, os_profile=os_profile
        )
        self._interrupt_event: Optional[threading.Event] = None
        self._on_token: Optional[Callable[[str], None]] = None

    def _build_system_prompt(self, skills: list = []):
        return self.system_prompt + "\n" + "\n".join(skills)

    def _build_context(
        self,
        *,
        datetime_block: str,
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
            "<context>",
            f"<datetime>\n{datetime_block}\n</datetime>",
        ]
        if memories is not None:
            context_parts.append(f"<memories>\n{memories}\n</memories>")
        if recent_turns is not None:
            context_parts.append(
                f"<conversation_history>\n{recent_turns}\n</conversation_history>"
            )
        if available_tools is not None:
            context_parts.append(
                f"<available_tools>\n{available_tools}\n</available_tools>"
            )
        if prior_outputs is not None:
            context_parts.append(f"<prior_outputs>\n{prior_outputs}\n</prior_outputs>")
        if step_followups is not None:
            context_parts.append(
                f"<step_followups>\n{step_followups}\n</step_followups>"
            )
        if step_errors is not None:
            context_parts.append(f"<step_errors>\n{step_errors}\n</step_errors>")
        if tool_info is not None:
            context_parts.append(
                f"<tool_instructions>\n{tool_info}\n</tool_instructions>"
            )
        if execution_results is not None:
            context_parts.append(
                f"<execution_results>\n{execution_results}\n</execution_results>"
            )
        if responder_note:
            context_parts.append(f"<planner_note>\n{responder_note}\n</planner_note>")
        context_parts.append("</context>")
        return "\n".join(context_parts)

    # ------------------------------------------------------
    # Public: Prompt runners
    # ------------------------------------------------------
    def set_interrupt(self, interrupt_event: threading.Event):
        self._interrupt_event = interrupt_event

    def set_on_token(self, on_token: Callable[[str], None]):
        self._on_token = on_token

    def run_memory_gate(
        self,
        *,
        active_task: str,
        recent_turns: str,
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Retrieval Gate prompt (JSON expected):
        { "ack_message": str, "search_queries": [], "deep_recall": bool }

        Strict validation via OutputParser.parse_retrieval_gate().
        """
        system_prompt = self._build_system_prompt([
            RETRIEVAL_GATE_PROMPT,
            BUDDY_OUTPUT.format(schema=RETRIEVAL_GATE_PROMPT_SCHEMA),
        ])
        prompt = build_retrieval_prompt(
            system=system_prompt,
            chat_history=recent_turns,
            datetime_block=self._get_time_info(),
            current_message=active_task,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
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
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Decision+Ingestion prompt (JSON expected).
        Strict validation via OutputParser.parse_brain().
        """
        system_prompt = self._build_system_prompt([
            BUDDY_MEMORY,
            BUDDY_BEHAVIOR,
            BRAIN_PROMPT,
            BUDDY_OUTPUT.format(schema=BRAIN_PROMPT_SCHEMA),
        ])
        prompt = build_brain_prompt(
            system=system_prompt,
            chat_history=recent_turns,
            datetime_block=self._get_time_info(),
            current_message=active_task,
            memories=memories,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
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
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Planner prompt (JSON expected).
        Strict validation via OutputParser.parse_planner().
        """
        system_prompt = self._build_system_prompt([
            BUDDY_MEMORY,
            PLANNER_PROMPT,
            BUDDY_OUTPUT.format(schema=PLANNER_PROMPT_SCHEMA),
        ])
        prompt = build_planner_prompt(
            system=system_prompt,
            datetime_block=self._get_time_info(),
            available_tools=available_tools,
            planner_instructions=planner_instructions,
            memories=memories,
            followups=active_task,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
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
        tool_prompt: str,
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Executor prompt (single-step execution).
        Strict validation via OutputParser.parse_executor().
        """

        system_prompt = self._build_system_prompt([
            EXECUTOR_PROMPT.format(tool_instructions=tool_prompt),
            BUDDY_OUTPUT.format(schema=EXECUTOR_PROMPT_SCHEMA),
        ])
        prompt = build_executor_prompt(
            system=system_prompt,
            datetime_block=self._get_time_info(),
            instruction=instruction,
            prior_outputs=prior_outputs or "",
            step_errors=step_errors or "",
            followups=step_followups or "",
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
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
        now: Optional[float] = None,
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Memory Summary prompt (JSON expected).
        Strict validation via OutputParser.parse_memory_summary().
        """
        system_prompt = self._build_system_prompt([
            BUDDY_MEMORY,
            MEMORY_SUMMARY_PROMPT,
            BUDDY_OUTPUT.format(schema=MEMORY_SUMMARY_PROMPT_SCHEMA),
        ])
        import datetime as _dt

        _ts = now if now is not None else _dt.datetime.now().timestamp()
        today_str = _dt.datetime.fromtimestamp(_ts).strftime("%Y-%m-%d %H:%M")
        prompt = build_memory_summary_prompt(
            system=system_prompt,
            memories=memories,
            today=today_str,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
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
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs the Respond prompt (JSON expected).
        Strict validation via OutputParser.parse_respond().
        """
        system_prompt = self._build_system_prompt([
            BUDDY_BEHAVIOR,
            RESPOND_PROMPT,
            BUDDY_OUTPUT.format(schema=RESPOND_PROMPT_SCHEMA),
        ])
        prompt = build_responder_prompt(
            system=system_prompt,
            datetime_block=self._get_time_info(),
            memories=memories,
            execution_results=execution_results,
            responder_instruction=active_task,
        )

        raw = self._call_llm_generate(
            prompt=prompt,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            stream=bool(stream),
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
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Runs one paragraph through the reader prompt.
        Called in a loop by TextReader for large text processing.

        Returns dict with keys: relevant (bool), content (str).
        """

        system_prompt = self._build_system_prompt([
            READER_PROMPT,
            BUDDY_OUTPUT.format(schema=READER_SCHEMA),
        ])

        prompt = build_reader_prompt(
            system=system_prompt,
            datetime_block=self._get_time_info(),
            rolling_context=(
                rolling_context if rolling_context else READER_CONTEXT_EMPTY
            ),
            task=READER_TASK_TEMPLATE.format(
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
            stream=stream,
            options=llm_options,
            json_mode=True,
        )

        return self.parser.parse_reader(raw)

    def run_vision(
        self,
        *,
        image_paths: "Union[str, List[str]]",
        query: str,
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze one or more images using the vision-capable LLM (Qwen VL).

        Uses /v1/chat/completions with image_url content parts (OAI multimodal format).
        Returns a dict with keys: description, objects, text_found, key_finding.
        On failure returns {"error": str}.

        Called by VisionTool.execute() via the brain kwarg.
        """
        from buddy.tools.vision.image_encoder import (
            encode_image_to_data_uri,
            is_image_path,
        )

        if isinstance(image_paths, str):
            image_paths = [image_paths]

        if not image_paths:
            return {"error": "No image paths provided"}

        # Encode all images to data URIs — fail fast on first error
        # Pre-encoded data URIs (from in-memory capture) are passed through directly.
        data_uris: List[str] = []
        for path in image_paths:
            if not path:
                return {"error": "Empty image path or data URI"}
            if str(path).startswith("data:"):
                data_uris.append(path)
                continue
            if not is_image_path(path):
                return {"error": f"Not a recognized image file: {path}"}
            try:
                data_uris.append(encode_image_to_data_uri(path))
            except (FileNotFoundError, ValueError, OSError, ImportError) as exc:
                logger.warning("run_vision encode failed path=%r err=%r", path, exc)
                return {"error": str(exc)}

        system_prompt = self._build_system_prompt([
            VISION_PROMPT,
            BUDDY_OUTPUT.format(schema=VISION_SCHEMA),
        ])
        messages = [{
            "role": "user",
            "content": (
                f"User Query: {query}\n\nRespond with the JSON schema. key_finding must"
                " directly answer the query."
            ),
        }]

        try:
            raw = self.llm.chat(
                messages=messages,
                system=system_prompt,
                images=data_uris,
                temperature=temperature,
                top_p=top_p,
                repeat_penalty=repeat_penalty,
                repeat_last_n=repeat_last_n,
                stream=stream,
                options=llm_options,
                on_delta=self._on_token,
                json_extract=True,
                json_validate=True,
                json_root="object",
                think=True,
                interrupt_event=self._interrupt_event,
            )
        except Exception as exc:
            logger.warning(
                "run_vision LLM call failed paths=%r err=%r", image_paths, exc
            )
            return {"error": str(exc)}

        # Parse result
        try:
            result = self.parser.parse_vision(raw)

            if isinstance(result, dict):
                return result
        except Exception:
            pass

        # Fallback: raw text as key_finding
        return {
            "description": str(raw).strip(),
            "objects": [],
            "text_found": "",
            "key_finding": str(raw).strip(),
        }

    def run_browser_action(
        self,
        *,
        screenshot_uri: str,
        task: str,
        progress: str = "",
        memory_context: str = "",
        dom_hints: str = "",
        ask_history: Optional[List[Dict[str, Any]]] = None,
        last_error: str = "",
        temperature: float = 0.6,
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 256,
        stream: bool = True,
        llm_options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Combined vision+action call for the browser micro-planner.

        Sees the current page screenshot and decides the next BrowserAction.
        Called once per iteration of the run_task loop in browser.py.

        Returns a dict with keys: function, arguments, summary.
        On failure returns {"function": "error", "arguments": {}, "summary": str}.

        screenshot_uri — JPEG data URI (data:image/jpeg;base64,...)
        task           — original user task description
        progress       — cumulative summary written by the model at the previous step
        memory_context — resolved memory values injected after fetch_memory, e.g. "email=harsh@x.com"
        dom_hints      — newline-separated list of visible interactive elements extracted from the DOM
        """

        system_prompt = self._build_system_prompt([
            BROWSER_ACTION_PROMPT,
            BUDDY_OUTPUT.format(schema=BROWSER_ACTION_SCHEMA),
        ])

        # Turn 1 — user: end goal (the full task, never changes across iterations)
        goal_msg = f"<goal>\n{task}\n</goal>"

        # Turn 2 — assistant: acknowledgment merged with recalled memory + cumulative progress
        # Merged into ONE assistant turn to prevent consecutive same-role messages which
        # cause Qwen3 to emit EOS immediately and return a blank response.
        mem_parts: List[str] = [
            "Understood, I will stick to the goal and accomplish it fully."
        ]
        if memory_context:
            mem_parts.append(f"<memory>\n{memory_context}\n</memory>")
        if progress:
            mem_parts.append(f"<progress>\n{progress}\n</progress>")
        assistant_block = "\n".join(mem_parts)

        # Turn 3 — user: current page context + screenshot
        final_parts: List[str] = []
        if last_error:
            final_parts.append(f"<last_error>\n{last_error}\n</last_error>")
        if dom_hints:
            final_parts.append(f"<page_elements>\n{dom_hints}\n</page_elements>")
        final_parts.append(
            "Analyze the screenshot, page_elements, and last_error if present. Decide"
            " the next best action and produce the full function call in valid JSON."
        )

        page_msg = "\n\n".join(final_parts)

        # Build messages with strict user/assistant alternation.
        # ask_history items: Buddy asked Q (assistant), user replied A (user).
        # After the last ask_history pair (user: A), the next turn is user: page_msg,
        # so we merge A + page_msg into one user turn to avoid consecutive user turns.
        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": goal_msg},
            {"role": "assistant", "content": assistant_block},
        ]
        history = ask_history or []
        for i, qa in enumerate(history):
            q = str(qa.get("q", ""))
            a = str(qa.get("a", ""))
            messages.append({"role": "assistant", "content": q})
            if i < len(history) - 1:
                messages.append({"role": "user", "content": a})
            else:
                # Last answer: merge with page context to avoid consecutive user turns
                messages.append({"role": "user", "content": f"{a}\n\n{page_msg}"})
        if not history:
            messages.append({"role": "user", "content": page_msg})

        logger.debug(
            "run_browser_action | task=%r progress=%r memory=%r dom_hints_lines=%d"
            " last_error=%r",
            task[:60],
            progress[:80],
            memory_context[:80],
            dom_hints.count("\n") + 1 if dom_hints else 0,
            last_error[:60] if last_error else "",
        )

        try:
            raw = self.llm.chat(
                messages=messages,
                system=system_prompt,
                images=[screenshot_uri],
                temperature=temperature,
                top_p=top_p,
                repeat_penalty=repeat_penalty,
                repeat_last_n=repeat_last_n,
                stream=stream,
                on_delta=self._on_token,
                options=llm_options,
                json_extract=True,
                json_validate=True,
                json_root="object",
                think=True,
                interrupt_event=self._interrupt_event,
            )
        except Exception as exc:
            logger.warning("run_browser_action LLM call failed: %r", exc)
            return {"function": "error", "arguments": {}, "summary": str(exc)}

        if self.debug:
            logger.debug(f"LLM output: \n {raw}")

        try:
            result = self.parser.parse_browser_action(raw)
            if isinstance(result, dict):
                return result
        except Exception:
            pass

        return {"function": "error", "arguments": {}, "summary": str(raw).strip()}

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
        top_p: float = 0.96,
        repeat_penalty: float = 1.0,
        repeat_last_n: int = 64,
        n_predict: Optional[int] = None,
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
            on_delta=(self._on_token if self._on_token and stream else None),
            json_extract=bool(json_mode),
            json_validate=bool(json_mode),
            json_root="object",
            json_max_chars=120_000,
            interrupt_event=self._interrupt_event,
            think=True,
            stop=["<|im_end|>", "<|endoftext|>"],
        )

        text = "" if out is None else str(out)
        # ── Detect missing JSON and re-prompt ────────────────────────────
        # Failure mode A: model output only the <think> block and stopped.
        #   Qwen3 emits lowercase </think>; handle both cases for safety.
        # Failure mode B (json_mode only): model emitted think + prose but
        #   forgot <json>. Common when execution_results are very long and
        #   the think block exhausts the model's attention budget.
        # Fix: trim to the think portion and inject <json> so the follow-up
        #   call skips re-thinking and goes straight to JSON emission.
        _text_lower = text.lower()
        _think_only = _text_lower.rstrip().endswith("</think>")

        _prose_instead_of_json = False
        if json_mode and not _think_only and "</think>" in _text_lower:
            try:
                json.loads(text)
            except Exception:
                _prose_instead_of_json = True

        if _think_only or _prose_instead_of_json:
            _idx = _text_lower.rfind("</think>")
            _think_part = text[: _idx + len("</think>")] if _idx >= 0 else text
            prompt = prompt + f"\n{_think_part}\n" + "{"
            text = self.llm.generate(
                prompt=prompt,
                system=system,
                stream=bool(stream),
                temperature=float(temperature),
                top_p=float(top_p),
                repeat_penalty=float(repeat_penalty),
                repeat_last_n=int(repeat_last_n),
                n_predict=n_predict,
                options=opts,
                on_delta=None,
                json_extract=False,
                json_validate=False,
                stop=["<|im_end|>", "<|endoftext|>"],
                interrupt_event=self._interrupt_event,
                think=False,
            )
            text = "{\n" + text

        if self.debug:
            logger.debug(f"LLM output: \n {text}")

        return text

    # -------------------------
    # Helpers
    # -------------------------
    @classmethod
    def _get_location(cls) -> str:
        import time as _t

        now_mono = _t.monotonic()
        if cls._location and (now_mono - cls._location_ts) < cls._LOCATION_TTL:
            return cls._location
        try:
            import urllib.request
            import json as _json

            with urllib.request.urlopen("http://ip-api.com/json", timeout=3) as r:
                data = _json.loads(r.read())
            if data.get("status") == "success":
                parts = [data.get(k, "") for k in ("city", "regionName", "country")]
                cls._location = ", ".join(p for p in parts if p) or "Unknown"
            else:
                cls._location = cls._location or "Unknown"
        except Exception:
            cls._location = cls._location or "Unknown"
        cls._location_ts = now_mono
        return cls._location

    @classmethod
    def _get_time_info(cls) -> str:
        now = _datetime.now().astimezone()

        tz_name = now.tzname() or "UTC"
        offset = now.utcoffset()
        if offset is None:
            total_sec = 0
        else:
            total_sec = int(offset.total_seconds())
        sign = "+" if total_sec >= 0 else "-"
        abs_sec = abs(total_sec)
        utc_offset = f"UTC{sign}{abs_sec // 3600:02d}:{(abs_sec % 3600) // 60:02d}"

        return (
            f"Day:      {now.strftime('%A')}\n"
            f"Date:     {now.strftime('%B %d, %Y')}\n"
            f"Time:     {now.strftime('%I:%M %p')}\n"
            f"Timezone: {tz_name} ({utc_offset})\n"
            f"ISO:      {now.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            f"Location: {cls._get_location()}"
        )
