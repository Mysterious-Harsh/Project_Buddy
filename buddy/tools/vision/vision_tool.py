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

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
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
    paths: List[str]  # one or more absolute image paths
    query: str  # what to find / answer about the image(s)


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
                "Analyze, describe, or query image files (PNG, JPG, JPEG, WEBP, GIF). "
                "Use when the user provides one or more image file paths and wants to "
                "understand, describe, read text from, compare, or extract information "
                "from the image(s). Supports single image ('path') or multiple images "
                "('paths' list). Returns description, objects, visible text, and a "
                "direct answer to the query."
            ),
            "version": "1.1.0",
            "prompt": VISION_TOOL_PROMPT,
            "tool_call_format": VISION_TOOL_CALL_FORMAT,
        }

    # ── Parse ──────────────────────────────────────────────

    def parse_call(self, payload: Dict[str, Any]) -> VisionCall:
        """
        Validate and parse executor tool_call payload.

        Accepted payload shapes (single or multi-image):
          {"path": "/abs/path/to/image.png", "query": "what is in this?"}
          {"paths": ["/abs/path/a.png", "/abs/path/b.jpg"], "query": "compare these"}

        Raises ValueError on invalid input.
        """
        if not isinstance(payload, dict):
            raise ValueError(f"Expected dict payload, got {type(payload).__name__}")

        query = str(payload.get("query") or "").strip()
        if not query:
            raise ValueError("'query' is required and must be a non-empty string")

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
            raise ValueError("'path' or 'paths' is required and must be non-empty")

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

        return VisionCall(paths=resolved, query=query)

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
                "ACTION": "analyze",
                "PATHS": call.paths,
                "ERROR": (
                    "Vision tool requires brain access. This is a configuration error."
                ),
            }

        # Check vision capability (best-effort; don't block if model_selector unavailable)
        _warn_if_not_vision_capable(brain)

        filenames = ", ".join(os.path.basename(p) for p in call.paths)
        if on_progress:
            on_progress(f"Analysing · {filenames}", False)

        logger.info(
            "vision_tool.execute | paths=%r query=%r",
            call.paths,
            call.query[:80],
        )

        result = await asyncio.to_thread(
            brain.run_vision,
            image_paths=call.paths,
            query=call.query,
        )

        if "error" in result:
            logger.warning(
                "vision_tool failed | paths=%r error=%r", call.paths, result["error"]
            )
            return {
                "OK": False,
                "ACTION": "analyze",
                "PATHS": call.paths,
                "ERROR": result["error"],
            }

        description = result.get("description", "")
        objects: List[str] = result.get("objects") or []
        text_found = result.get("text_found", "")
        key_finding = result.get("key_finding", "")

        logger.info(
            "vision_tool.done | paths=%r desc_len=%d objects=%d text_len=%d",
            call.paths,
            len(description),
            len(objects),
            len(text_found),
        )

        return {
            "OK": True,
            "ACTION": "analyze",
            "PATHS": call.paths,
            "DESCRIPTION": description,
            "OBJECTS": objects,
            "TEXT_FOUND": text_found,
            "KEY_FINDING": key_finding,
        }


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
