# buddy/tools/os/system_control.py
#
# System control tool — media, volume, app launch, screen lock/sleep.
# Planner picks this tool for any system-level action that doesn't need reasoning.
# Execution delegates to _exec_action() in intent_interceptor — single source of truth.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional  # noqa: F401

from buddy.logger.logger import get_logger
from buddy.prompts.system_control_prompts import SYSTEM_CONTROL_TOOL_PROMPT

logger = get_logger("system_control_tool")

TOOL_NAME = "system_control"

# ==========================================================
# Call schema
# ==========================================================


@dataclass
class SystemControlCall:
    action: str  # plain command string — passed through the intent interceptor


# ==========================================================
# Tool
# ==========================================================


class SystemControlTool:
    """
    Planner description (used for tool routing):
    "Control system actions: media playback (play, pause, next, prev),
     volume (up, down, set level, mute), open an app by name,
     lock screen, or sleep. Use when Buddy needs to act on the system directly."
    """

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": TOOL_NAME,
            "version": "1.1.0",
            "description": "Control media playback, volume, app launching, and system state (lock/sleep).",
            "prompt": SYSTEM_CONTROL_TOOL_PROMPT,
        }

    # ── Parse ──────────────────────────────────────────────

    def parse_call(self, payload: Dict[str, Any]) -> SystemControlCall:
        if not isinstance(payload, dict):
            raise ValueError(f"Expected dict payload, got {type(payload).__name__}")
        action = str(payload.get("action") or "").strip()
        if not action:
            raise ValueError("'action' is required and must be a non-empty string")
        return SystemControlCall(action=action)

    # ── Execute ────────────────────────────────────────────

    async def execute(
        self,
        function: str = "",
        arguments: Dict[str, Any] = {},
        on_progress: Optional[Callable[[str, bool], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            call = self.parse_call(arguments)
        except Exception as e:
            return {"OK": False, "ACTION": "", "ERROR": str(e)}

        from buddy.brain.intent_interceptor import interceptor, normalize

        if on_progress:
            on_progress(call.action.capitalize() + "…", False)

        normalized = normalize(call.action)
        quick = interceptor.match(normalized)

        if quick is None:
            logger.warning("system_control no match | raw=%r normalized=%r", call.action, normalized)
            return {
                "OK": False,
                "ACTION": call.action,
                "ERROR": f"Could not interpret command: {call.action!r}",
            }

        reply, success = interceptor.execute(quick)
        logger.info("system_control | action=%r quick=%s ok=%s reply=%r", call.action, quick.name, success, reply)

        if success:
            return {"OK": True, "ACTION": call.action, "REPLY": reply}
        return {"OK": False, "ACTION": call.action, "ERROR": reply}


# ==========================================================
# Registry entry point
# ==========================================================


def get_tool() -> SystemControlTool:
    return SystemControlTool()
