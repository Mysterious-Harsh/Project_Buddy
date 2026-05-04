from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Type, TypeVar, Protocol

from buddy.logger.logger import get_logger
from buddy.schema.models import (
    BrainResult,
    PlannerResult,
    RetrievalGateResult,
    ExecutorResult,
    MemorySummaryResult,
    FinalRespond,
    ReaderResult,
    VisionResult,
    BrowserAction,
)

logger = get_logger("output_parser")


# 1. Define a structural contract for .clean_dict()
class _HasCleanDict(Protocol):
    def clean_dict(self) -> Dict[str, Any]: ...


# 2. Bind TModel to it
TModel = TypeVar("TModel", bound=_HasCleanDict)

# Cache Pydantic version detection at module load (avoids hasattr() on every call)
_PYDANTIC_V2 = hasattr(BrainResult, "model_validate")

# Pre-compiled regex for cleanup (used only in fallback path)
_FENCE_CLEAN_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


class OutputParser:
    """
    OutputParser (v2 - Optimized)

    Public parsers return: Dict[str, Any]
    Design:
      - Extract JSON object from raw LLM text
      - Apply minimal, targeted normalization for LLM drift
      - Validate once against Pydantic models
    """

    # ==========================================================
    # Public APIs
    # ==========================================================

    def parse_brain(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate BrainResult from the Brain prompt output."""
        data = self._extract_json_object(raw_text)
        if isinstance(data.get("memories"), dict):
            data["memories"] = [data["memories"]]
        model = self._validate(BrainResult, data)
        return model.clean_dict() if model else {}

    def parse_planner(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate PlannerResult. Normalizes BEFORE validation to avoid double-pass."""
        data = self._extract_json_object(raw_text)
        norm = self._normalize_planner(data)
        model = self._validate(PlannerResult, norm)
        return model.clean_dict() if model else {}

    def parse_retrieval_gate(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate RetrievalGateResult."""
        data = self._extract_json_object(raw_text)
        if "search_query" in data and "search_queries" not in data:
            sq = data.pop("search_query")
            data["search_queries"] = [sq] if sq else []
        if isinstance(data.get("search_queries"), str):
            data["search_queries"] = [data["search_queries"]]
        if isinstance(data.get("search_queries"), list):
            data["search_queries"] = [
                q for q in data["search_queries"] if str(q).strip()
            ]
        model = self._validate(RetrievalGateResult, data)
        return model.clean_dict() if model else {}

    def parse_executor(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate ExecutorResult. STRICT schema."""
        return self._parse_strict(raw_text, ExecutorResult)

    def parse_memory_summary(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate MemorySummaryResult."""
        return self._parse_strict(raw_text, MemorySummaryResult)

    def parse_respond(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate FinalRespond."""
        data = self._extract_json_object(raw_text)
        if isinstance(data.get("memory_candidates"), dict):
            data["memory_candidates"] = [data["memory_candidates"]]

        candidates = data.get("memory_candidates")
        if isinstance(candidates, list):
            norm = []
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                mt = str(c.get("memory_type", "flash") or "flash").strip().lower()
                if mt not in ("flash", "short", "long", "discard"):
                    mt = "flash"
                c["memory_type"] = mt
                try:
                    c["salience"] = max(
                        0.0, min(1.0, float(c.get("salience", 0.3) or 0.3))
                    )
                except Exception:
                    c["salience"] = 0.3
                norm.append(c)
            data["memory_candidates"] = norm

        model = self._validate(FinalRespond, data)
        return model.clean_dict() if model else {}

    def parse_reader(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate ReaderResult. STRICT schema."""
        return self._parse_strict(raw_text, ReaderResult)

    def parse_vision(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate VisionResult. STRICT schema."""
        return self._parse_strict(raw_text, VisionResult)

    def parse_browser_action(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate BrowserAction. STRICT schema."""
        return self._parse_strict(raw_text, BrowserAction)

    # ==========================================================
    # Internal Helpers
    # ==========================================================

    def _parse_strict(self, raw_text: str, model_cls: Type[TModel]) -> Dict[str, Any]:
        """Generic parser for strict-schema models. Eliminates 4 duplicate methods."""
        data = self._extract_json_object(raw_text)
        model = self._validate(model_cls, data)
        return model.clean_dict() if model else {}

    def _validate(self, cls: Type[TModel], payload: Dict[str, Any]) -> Optional[TModel]:
        """Validate payload against Pydantic model (v1/v2 compatible)."""
        try:
            if _PYDANTIC_V2:
                return cls.model_validate(payload)  # type: ignore[attr-defined]
            return cls.parse_obj(payload)  # type: ignore[no-any-return]
        except Exception as e:
            logger.debug("Validation failed for %s: %s", cls.__name__, e)
            return None

    def _extract_json_object(self, text: str) -> Dict[str, Any]:
        """Extract the first JSON object from raw text."""
        t = (text or "").strip()
        if not t:
            raise ValueError("empty_llm_output")

        # Fast path: pure JSON
        try:
            obj = json.loads(t)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # Fallback: balanced brace scanner (fixes nested JSON truncation bug)
        start = t.find("{")
        if start < 0:
            raise ValueError("no_json_object_found")

        s = t[start:]
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(s):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj_str = self._cleanup_json_like(s[: i + 1])
                    obj = json.loads(obj_str)
                    if isinstance(obj, dict):
                        return obj
                    raise ValueError("json_not_object")
        raise ValueError("unbalanced_json_braces")

    @staticmethod
    def _cleanup_json_like(s: str) -> str:
        """Strip markdown fences and replace curly quotes."""
        s = s.strip()
        s = _FENCE_CLEAN_RE.sub("", s)
        return s.replace("“", '"').replace("”", '"').replace("’", "'").strip()

    def _normalize_planner(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize planner payload to locked schema. Runs BEFORE validation."""
        if not isinstance(data, dict):
            data = {}
        else:
            data = data.copy()  # Prevent caller-side mutation

        status = str(data.get("status", "success")).strip().lower()
        if status not in ("success", "followup", "refusal"):
            status = "success"

        message = str(data.get("message", "")).strip()
        responder_note = str(data.get("responder_note", "")).strip()

        raw_steps = data.get("steps", [])
        steps = raw_steps if isinstance(raw_steps, list) else []

        norm_steps = []
        for idx, s in enumerate(steps, start=1):
            if not isinstance(s, dict):
                continue
            norm_steps.append({
                "step_id": s.get("step_id", idx),
                "tool": str(s.get("tool", "")).strip(),
                "goal": str(s.get("goal", "")).strip(),
                "instruction": str(s.get("instruction", "")).strip(),
                "hints": str(s.get("hints", "")).strip(),
                "input_steps": self._to_int_list(s.get("input_steps")),
                "output": str(s.get("output", "")).strip() or None,
            })

        if status in ("followup", "refusal"):
            norm_steps = []
            responder_note = ""
            if not message:
                message = (
                    "I need more information to proceed. What is missing?"
                    if status == "followup"
                    else "Task is not achievable with the available tools."
                )
        else:
            message = ""

        return {
            "status": status,
            "message": message,
            "responder_note": responder_note,
            "steps": norm_steps,
        }

    def _to_int_list(self, v: Any) -> List[int]:
        """Coerce values to a list of positive integers."""
        if not isinstance(v, list):
            return []
        out = []
        for x in v:
            try:
                i = int(x)
                if i >= 1:
                    out.append(i)
            except Exception:
                continue
        return out
