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
            "description": (
                "WHEN: controlling system-level state — media playback, volume, brightness, applications, or machine power state.\n\n"
                "FUNCTIONS:\n"
                "  action(command)   — execute a system command. Valid commands:\n"
                "    media:  play, pause, next, prev, stop\n"
                "    volume: set_volume(0–100), mute, unmute\n"
                "    screen: set_brightness(0–100)\n"
                "    apps:   open_app(name), close_app(name), focus_app(name)\n"
                "    power:  lock, sleep\n\n"
                "CHAIN: typically a terminal step — output is a confirmation; no downstream step needs it.\n"
                "NOT: files → fs_browse/fs_read/fs_write/fs_manage | clipboard text → clipboard | web → web_fetch/browser"
            ),
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
            return {"STATUS": "failed", "ACTION": "", "ERROR": str(e)}

        from buddy.brain.intent_interceptor import interceptor, normalize

        if on_progress:
            on_progress(call.action.capitalize() + "…", False)

        normalized = normalize(call.action)
        quick = interceptor.match(normalized)

        if quick is None:
            logger.warning("system_control no match | raw=%r normalized=%r", call.action, normalized)
            return {
                "STATUS": "failed",
                "ACTION": call.action,
                "ERROR": f"Could not interpret command: {call.action!r}",
            }

        reply, success = interceptor.execute(quick)
        logger.info("system_control | action=%r quick=%s ok=%s reply=%r", call.action, quick.name, success, reply)

        if success:
            return {"STATUS": "success", "ACTION": call.action, "REPLY": reply}
        return {"STATUS": "failed", "ACTION": call.action, "ERROR": reply}


# ==========================================================
# Registry entry point
# ==========================================================


def get_tool() -> SystemControlTool:
    return SystemControlTool()
