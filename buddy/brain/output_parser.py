# buddy/brain/output_parser.py
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

from buddy.logger.logger import get_logger
from buddy.schema.models import (
    BrainResult,
    PlannerResult,
    RetrievalGateResult,
    ExecutorResult,
    MemorySummaryResult,
    FinalRespond,
)

logger = get_logger("output_parser")

TModel = TypeVar("TModel")


class OutputParser:
    """
    OutputParser (v1)

    Public parsers:
      - parse_brain():          validates BrainResult
      - parse_planner():        validates PlannerResult
      - parse_retrieval_gate(): validates RetrievalGateResult

    Design:
      - Extract a JSON object from raw LLM text
      - Apply *minimal* normalization for common drift
      - Validate against schema/models.py
      - Always returns: (validated_model_or_None, payload_dict)
    """

    # ==========================================================
    # Public APIs
    # ==========================================================

    def parse_brain(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate BrainResult from the Brain prompt output."""
        data = self._extract_json_object(raw_text)

        # Normalize memories: single object → list (LLM drift safety)
        if isinstance(data.get("memories"), dict):
            data["memories"] = [data["memories"]]

        # 1) strict validate
        model = self._validate(BrainResult, data)
        if model:
            return model.clean_dict()

        return {}

    def parse_planner(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate PlannerResult from the Planner prompt output."""
        data = self._extract_json_object(raw_text)

        # 1) strict validate
        model = self._validate(PlannerResult, data)
        if model:
            return model.clean_dict()

        # 2) normalize to locked shape, then validate again
        norm = self._normalize_planner(data)
        model = self._validate(PlannerResult, norm)
        if model:
            return model.clean_dict()

        return {}

    def parse_retrieval_gate(self, raw_text: str) -> Dict[str, Any]:
        """
        Parse/validate RetrievalGateResult from the Retrieval Gate prompt output.

        Returns:
            (validated_model_or_None, payload_dict)
        """
        data = self._extract_json_object(raw_text)

        # Normalize: old single search_query string → search_queries list
        if "search_query" in data and "search_queries" not in data:
            sq = data.pop("search_query")
            data["search_queries"] = [sq] if sq else []
        # Normalize: single string instead of list
        if isinstance(data.get("search_queries"), str):
            data["search_queries"] = [data["search_queries"]]
        # Strip empty strings from list
        if isinstance(data.get("search_queries"), list):
            data["search_queries"] = [q for q in data["search_queries"] if str(q).strip()]

        # 1) strict validate
        model = self._validate(RetrievalGateResult, data)
        if model:
            return model.clean_dict()

        return {}

    def parse_executor(self, raw_text: str) -> Dict[str, Any]:
        """
        Parse/validate ExecutorResult from the Executor prompt output.

        Executor output is STRICT by design.
        No normalization is applied beyond JSON extraction.
        """
        data = self._extract_json_object(raw_text)

        model = self._validate(ExecutorResult, data)
        if model:
            return model.clean_dict()

        return {}

    def parse_memory_summary(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate MemorySummaryResult from the Memory Summary prompt output."""
        data = self._extract_json_object(raw_text)

        # 1) strict validate
        model = self._validate(MemorySummaryResult, data)
        if model:
            return model.clean_dict()

        return {}

    def parse_respond(self, raw_text: str) -> Dict[str, Any]:
        """Parse/validate FinalRespond from the Respond prompt output."""
        data = self._extract_json_object(raw_text)

        # 1) strict validate
        model = self._validate(FinalRespond, data)
        if model:
            return model.clean_dict()

        return {}

    # ==========================================================
    # Validation
    # ==========================================================

    def _validate(self, cls: Type[TModel], payload: Dict[str, Any]) -> Optional[TModel]:
        """
        Validate payload against a Pydantic model class.

        Supports both Pydantic v2 (model_validate) and legacy v1 (parse_obj).
        """
        try:
            if hasattr(cls, "model_validate"):  # pydantic v2
                return cls.model_validate(payload)  # type: ignore[attr-defined]
            return cls.parse_obj(payload)  # type: ignore[no-any-return]  # pragma: no cover
        except Exception as e:
            logger.debug(
                "Validation failed for %s: %s", getattr(cls, "__name__", str(cls)), e
            )
            return None

    # ==========================================================
    # JSON extraction
    # ==========================================================

    _FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)

    def _extract_json_object(self, text: str) -> Dict[str, Any]:
        """
        Extract the first JSON object from raw text.

        Extraction strategy (in order):
          1) fenced ```json { ... } ```
          2) raw text is a JSON object
          3) scan for first balanced {...} block

        Raises:
            ValueError: if no JSON object can be extracted
        """
        t = (text or "").strip()
        if not t:
            raise ValueError("empty_llm_output")

        # 1) fenced JSON
        m = self._FENCE_RE.search(t)
        if m:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
            raise ValueError("json_not_object")

        # 2) pure JSON
        try:
            obj = json.loads(t)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        # 3) first balanced {...} block
        start = t.find("{")
        if start < 0:
            raise ValueError("no_json_object_found")

        s = t[start:]
        depth = 0
        in_str = False
        esc = False

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
                    obj_str = s[: i + 1]
                    obj_str = self._cleanup_json_like(obj_str)
                    obj = json.loads(obj_str)
                    if not isinstance(obj, dict):
                        raise ValueError("json_not_object")
                    return obj

        raise ValueError("unbalanced_json_braces")

    def _cleanup_json_like(self, s: str) -> str:
        """
        Clean up common JSON-ish artifacts:
          - stray markdown fences
          - curly quotes
        """
        s = s.strip()
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
        s = re.sub(r"\s*```$", "", s)
        return s.replace("“", '"').replace("”", '"').replace("’", "'").strip()

    def _normalize_planner(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize planner payload to the locked schema:
        { status, message, responder_note, steps[] }
        """
        if not isinstance(data, dict):
            data = {}

        status = self._to_str(data.get("status", "success")).strip().lower()
        if status not in ("success", "followup", "refusal"):
            status = "success"

        message = self._to_str(data.get("message", "")).strip()
        responder_note = self._to_str(data.get("responder_note", "")).strip()

        raw_steps = data.get("steps", [])
        steps: List[Dict[str, Any]] = raw_steps if isinstance(raw_steps, list) else []

        norm_steps: List[Dict[str, Any]] = []
        for idx, s in enumerate(steps, start=1):
            if not isinstance(s, dict):
                continue
            norm_steps.append({
                "step_id": s.get("step_id", idx),
                "tool": self._to_str(s.get("tool", "")).strip(),
                "goal": self._to_str(s.get("goal", "")).strip(),
                "instruction": self._to_str(s.get("instruction", "")).strip(),
                "hints": self._to_str(s.get("hints", "")).strip(),
                "input_steps": self._to_int_list(s.get("input_steps")),
                "output": self._to_str(s.get("output", "")).strip(),
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

    # ==========================================================
    # Tiny coercion helpers
    # ==========================================================

    def _to_bool(self, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return False

    def _to_str(self, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    def _to_str_list(self, v: Any) -> List[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    def _to_int_list(self, v: Any) -> List[int]:
        if not isinstance(v, list):
            return []
        out: List[int] = []
        for x in v:
            try:
                i = int(x)
                if i >= 1:
                    out.append(i)
            except Exception:
                continue
        return out

    def _clamp01(self, v: Any) -> float:
        try:
            x = float(v)
        except Exception:
            return 0.0
        if x < 0.0:
            return 0.0
        if x > 1.0:
            return 1.0
        return x
