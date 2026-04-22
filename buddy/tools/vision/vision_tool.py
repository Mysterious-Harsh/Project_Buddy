# buddy/tools/vision/vision_tool.py
#
# Vision tool — image analysis via Qwen3.5 native multimodal.
#
# Auto-discovered by ToolRegistry (defines TOOL_NAME).
# Planner picks this tool when the user provides an image path and wants
# it analyzed, described, or queried.
#
# execute() receives brain= kwarg from ActionRouter (action_router.py:607).
# All image processing happens here — the responder only ever sees text.

from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional
import asyncio

from buddy.logger.logger import get_logger
from buddy.prompts.vision_prompts import (
    VISION_TOOL_PROMPT,
    VISION_TOOL_CALL_FORMAT,
)
from buddy.tools.vision.image_encoder import is_image_path

logger = get_logger("vision_tool")

TOOL_NAME = "vision"

_SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}


# ==========================================================
# Call schema
# ==========================================================


@dataclass
class VisionCall:
    paths: List[str]  # one or more absolute image paths (ignored for screenshot action)
    query: str  # what to find / answer about the image(s)
    action: Literal["analyze", "screenshot"] = field(default="analyze")
    save_path: Optional[str] = field(default=None)  # screenshot only: save PNG here


# ==========================================================
# Tool
# ==========================================================


class VisionTool:
    """
    Analyzes image files using the brain's run_vision() method.

    Planner description (used for tool routing):
    "Analyze, describe, or query image files (PNG, JPG, JPEG, WEBP, GIF).
     Use when the user provides an image file path and wants to understand,
     describe, read text from, or extract information from the image."
    """

    # ── Registry interface ─────────────────────────────────

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": TOOL_NAME,
            "description": (
                "Analyze, describe, take a screenshot, or query image files (PNG, JPG,"
                " JPEG, WEBP, GIF). Use when the user provides one or more image file"
                " paths and wants to understand, describe, read text from, compare, or"
                " extract information from the image(s), also to analyse the user"
                " screen, take a screenshot and save it. Returns description, objects,"
                " visible text, and a direct answer to the query."
            ),
            "version": "1.1.0",
            "prompt": VISION_TOOL_PROMPT,
            "tool_call_format": VISION_TOOL_CALL_FORMAT,
        }

    # ── Parse ──────────────────────────────────────────────

    def parse_call(self, payload: Dict[str, Any]) -> VisionCall:
        """
        Validate and parse executor tool_call payload.

        Accepted payload shapes:
          {"action": "screenshot", "query": "what is on my screen?"}
          {"path": "/abs/path/to/image.png", "query": "what is in this?"}
          {"paths": ["/abs/path/a.png", "/abs/path/b.jpg"], "query": "compare these"}

        Raises ValueError on invalid input.
        """
        if not isinstance(payload, dict):
            raise ValueError(f"Expected dict payload, got {type(payload).__name__}")

        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("'query' is required and must be a non-empty string")

        action = str(payload.get("action") or "analyze").strip().lower()
        if action not in ("analyze", "screenshot"):
            raise ValueError(
                f"'action' must be 'analyze' or 'screenshot', got {action!r}"
            )

        if action == "screenshot":
            save_path = str(payload.get("save_path") or "").strip() or None
            if save_path:
                save_path = os.path.expandvars(os.path.expanduser(save_path))
            return VisionCall(
                paths=[], query=query, action="screenshot", save_path=save_path
            )

        # Collect raw paths — support both "path" (single) and "paths" (list)
        raw_paths: List[str] = []
        if payload.get("paths"):
            raw = payload["paths"]
            if isinstance(raw, list):
                raw_paths = [str(p).strip() for p in raw if p]
            else:
                raw_paths = [str(raw).strip()]
        elif payload.get("path"):
            raw_paths = [str(payload["path"]).strip()]

        if not raw_paths:
            raise ValueError("'path' or 'paths' is required for action='analyze'")

        # Validate and resolve each path
        resolved: List[str] = []
        for p in raw_paths:
            r = os.path.expandvars(os.path.expanduser(p))
            if not os.path.exists(r):
                raise ValueError(f"Image file not found: {r}")
            if not os.path.isfile(r):
                raise ValueError(f"Path is not a file: {r}")
            if not is_image_path(r):
                ext = os.path.splitext(r)[1].lower()
                raise ValueError(
                    f"Unsupported image format '{ext}'. "
                    f"Supported: {sorted(_SUPPORTED_EXTS)}"
                )
            resolved.append(r)

        return VisionCall(paths=resolved, query=query, action="analyze")

    # ── Execute ────────────────────────────────────────────

    async def execute(
        self,
        call: VisionCall,
        *,
        brain: Any = None,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        goal: str = "",
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Analyze the image at call.path using brain.run_vision().

        brain is passed by ActionRouter at execution time.
        Returns a result dict compatible with the executor/responder pipeline.
        """
        if brain is None:
            logger.error(
                "vision_tool.execute called without brain — cannot analyze image"
            )
            return {
                "OK": False,
                "ACTION": call.action,
                "PATHS": call.paths,
                "ERROR": (
                    "Vision tool requires brain access. This is a configuration error."
                ),
            }

        # Check vision capability (best-effort; don't block if model_selector unavailable)
        _warn_if_not_vision_capable(brain)

        if call.action == "screenshot":
            if on_progress:
                on_progress("Capturing screen…", False)
            try:
                data_uri = await asyncio.to_thread(_capture_screen_data_uri)
            except Exception as exc:
                logger.warning("vision_tool screenshot capture failed: %r", exc)
                return {
                    "OK": False,
                    "ACTION": "screenshot",
                    "PATHS": [],
                    "ERROR": f"Screenshot capture failed: {exc}",
                }
            image_inputs = [data_uri]
            label = "screenshot"

            if call.save_path:
                try:
                    _save_data_uri_to_file(data_uri, call.save_path)
                    logger.info("vision_tool screenshot saved to %r", call.save_path)
                except Exception as exc:
                    logger.warning("vision_tool screenshot save failed: %r", exc)
                    # Non-fatal — analysis still proceeds
        else:
            image_inputs = call.paths
            label = ", ".join(os.path.basename(p) for p in call.paths)

        if on_progress:
            on_progress(f"Analysing · {label}", False)

        logger.info(
            "vision_tool.execute | action=%r label=%r query=%r",
            call.action,
            label,
            call.query[:80],
        )

        result = await asyncio.to_thread(
            brain.run_vision,
            image_paths=image_inputs,
            query=call.query,
        )

        if "error" in result:
            logger.warning(
                "vision_tool failed | action=%r label=%r error=%r",
                call.action,
                label,
                result["error"],
            )
            return {
                "OK": False,
                "ACTION": call.action,
                "PATHS": call.paths,
                "ERROR": result["error"],
            }

        description = result.get("description", "")
        objects: List[str] = result.get("objects") or []
        text_found = result.get("text_found", "")
        key_finding = result.get("key_finding", "")

        logger.info(
            "vision_tool.done | action=%r label=%r desc_len=%d objects=%d text_len=%d",
            call.action,
            label,
            len(description),
            len(objects),
            len(text_found),
        )

        result_dict: Dict[str, Any] = {
            "OK": True,
            "ACTION": call.action,
            "PATHS": call.paths,
            "DESCRIPTION": description,
            "OBJECTS": objects,
            "TEXT_FOUND": text_found,
            "KEY_FINDING": key_finding,
        }
        if call.action == "screenshot" and call.save_path:
            result_dict["SAVED_PATH"] = call.save_path
        return result_dict


# ==========================================================
# Screenshot capture — in-memory, no temp files
# ==========================================================


def _capture_screen_data_uri() -> str:
    """
    Capture the full screen and return a PNG data URI.

    Platform support:
      macOS   — Pillow ImageGrab (Quartz, no deps beyond Pillow)
      Windows — Pillow ImageGrab (native)
      Linux   — Pillow ImageGrab requires scrot; falls back to pyscreenshot if missing

    Raises RuntimeError if capture fails on all available methods.
    """
    import sys

    buf = io.BytesIO()

    # Primary: Pillow ImageGrab — works natively on macOS and Windows.
    # On Linux it shells out to scrot if available.
    try:
        from PIL import ImageGrab  # type: ignore

        img = ImageGrab.grab()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{b64}"
    except ImportError:
        pass  # Pillow not installed — try fallback
    except Exception as exc:
        if sys.platform == "linux":
            pass  # scrot may be missing; try pyscreenshot below
        else:
            raise RuntimeError(f"Screen capture failed: {exc}") from exc

    # Linux fallback: pyscreenshot (wraps scrot / gnome-screenshot / etc.)
    try:
        import pyscreenshot as ImageGrab2  # type: ignore

        img = ImageGrab2.grab()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{b64}"
    except ImportError:
        raise RuntimeError(
            "Screen capture requires Pillow (pip install Pillow) on macOS/Windows, "
            "or Pillow + scrot on Linux. Neither was found."
        )
    except Exception as exc:
        raise RuntimeError(f"Screen capture failed: {exc}") from exc


def _save_data_uri_to_file(data_uri: str, path: str) -> None:
    """Decode a PNG data URI and write it to disk."""
    header, b64 = data_uri.split(",", 1)
    raw = base64.b64decode(b64)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)


# ==========================================================
# Vision capability check (non-blocking warning)
# ==========================================================


def _warn_if_not_vision_capable(brain: Any) -> None:
    """
    Log a warning if the active model is not flagged as vision_capable.
    Does not block execution — the LLM call may still work if the user
    manually loaded a Qwen3.5 model.
    """
    try:
        from buddy.buddy_core.model_selector import LLMOption

        state = getattr(brain, "_state", None) or getattr(brain, "state", None)
        model: Optional[LLMOption] = (
            getattr(state, "llm_model", None) if state else None
        )
        if model is not None and not getattr(model, "vision_capable", False):
            logger.warning(
                "vision_tool: model '%s' (family=%s) is not flagged vision_capable. "
                "Image analysis requires Qwen3.5. Results may be empty or wrong.",
                model.filename,
                model.family,
            )
    except Exception:
        pass  # capability check is best-effort


# ==========================================================
# Registry entry point
# ==========================================================


def get_tool() -> VisionTool:
    return VisionTool()
