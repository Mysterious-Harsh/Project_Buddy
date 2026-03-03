# schema/models.py
from __future__ import annotations


from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union


from pydantic import BaseModel, Field, model_validator

# ==========================================================
# ENUMS
# ==========================================================


class DecisionMode(str, Enum):
    """
    Prompt-aligned decision.mode (Brain prompt v1).
    """

    CHAT = "CHAT"
    EXECUTE = "EXECUTE"


class MemoryType(str, Enum):
    FLASH = "flash"
    SHORT = "short"
    LONG = "long"


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    PARTIAL = "partial"


# --- Prompt-aligned enums (locked prompts) ---


class MemoryGateType(str, Enum):
    FLASH = "flash"
    SHORT = "short"
    LONG = "long"
    DISCARD = "discard"

    @classmethod
    def from_str(cls, v: Any) -> "MemoryGateType":
        s = str(v or "").strip().lower()
        return cls(s) if s in cls._value2member_map_ else cls.DISCARD


class RecommendedMemoryType(str, Enum):
    SHORT = "short"
    LONG = "long"

    @classmethod
    def from_str(cls, v: Any) -> "RecommendedMemoryType":
        s = str(v or "").strip().lower()
        return cls(s) if s in cls._value2member_map_ else cls.SHORT


# ==========================================================
# BASE MODEL (HELPERS)
# ==========================================================


class BuddyBaseModel(BaseModel):
    """Base model with safe serialization helpers (Pydantic v2)."""

    def clean_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True)

    def clean_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


class Decision(BuddyBaseModel):
    """
    Prompt-aligned Brain Decision (brain_prompt v1).
    Matches the strict JSON output of the Brain prompt.
    """

    mode: DecisionMode
    intent: str
    response: str
    afterthought: str
    model_config = {"populate_by_name": True}


# ==========================================================
# MEMORY PROMPTS (locked outputs)
# ==========================================================

# ADD: Retrieval gate output schema (before brain prompt)


class RetrievalGateResult(BuddyBaseModel):

    search_query: str
    ack_message: str
    deep_recall: bool


class MemoryIngestionResult(BuddyBaseModel):
    """
    Prompt-aligned Brain ingestion output (brain_prompt v1).
    Matches ingestion JSON in the Brain prompt.
    """

    memory_type: Literal["flash", "short", "long", "discard"]
    memory_text: str
    salience: float = Field(ge=0.0, le=1.0)
    reason: str


class MemorySummaryResult(BuddyBaseModel):
    """
    Prompt-aligned Brain ingestion output (brain_prompt v1).
    Matches ingestion JSON in the Brain prompt.
    """

    memory_summary: str
    salience: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


# ==========================================================
# BRAIN PROMPT (locked prompt outputs)
# ==========================================================


class BrainResult(BuddyBaseModel):
    """
    Strict output of brain_prompt:
    { "decision": {...}, "ingestion": {...} }
    """

    decision: Decision
    ingestion: MemoryIngestionResult


# ==========================================================
# PLANNER PROMPT (locked prompt outputs)
# ==========================================================


class PlannerStep(BuddyBaseModel):
    step_id: int = Field(ge=1)
    tool: str = Field(min_length=1)
    ack_message: str = Field(default="")
    instruction: str = Field(min_length=1)
    input_steps: List[int] = Field(default_factory=list)
    output: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


class PlannerResult(BuddyBaseModel):
    """
    LOCKED Planner output schema:

    {
      "refusal": true|false,
      "refusal_reason": "",
      "followup": true|false,
      "followup_question": "",
      "steps": [ ... ]
    }

    Invariants:
    - refusal=True  => steps=[], refusal_reason non-empty,
                      followup=False, followup_question=""
    - followup=True => steps=[], followup_question non-empty,
                      refusal=False, refusal_reason=""
    - refusal=False and followup=False => steps allowed,
                      refusal_reason="", followup_question=""
    """

    refusal: bool = False
    refusal_reason: str = ""

    followup: bool = False
    followup_question: str = ""

    steps: List[PlannerStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_planner_contract(self) -> "PlannerResult":
        # Canonicalize strings
        self.refusal_reason = (self.refusal_reason or "").strip()
        self.followup_question = (self.followup_question or "").strip()

        # Mutual exclusivity
        if self.refusal and self.followup:
            raise ValueError(
                "Invalid planner output: refusal=true and followup=true cannot both be"
                " true"
            )

        # Refusal branch
        if self.refusal:
            if not self.refusal_reason:
                raise ValueError("refusal=true requires non-empty refusal_reason")
            if self.steps:
                raise ValueError("refusal=true requires steps=[]")
            if self.followup:
                raise ValueError("refusal=true requires followup=false")
            if self.followup_question:
                raise ValueError("refusal=true requires followup_question=''")
            # enforce canonical empties
            self.followup = False
            self.followup_question = ""
            return self

        # Followup branch
        if self.followup:
            if not self.followup_question:
                raise ValueError("followup=true requires non-empty followup_question")
            if self.steps:
                raise ValueError("followup=true requires steps=[]")
            if self.refusal_reason:
                raise ValueError("followup=true requires refusal_reason=''")
            # enforce canonical empties
            self.refusal = False
            self.refusal_reason = ""
            return self

        # Normal planning branch
        # No refusal/followup => message fields must be empty
        self.refusal_reason = ""
        self.followup_question = ""

        # Optional strictness: validate input_steps references earlier steps
        seen = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(f"Duplicate step_id: {step.step_id}")
            seen.add(step.step_id)
            for dep in step.input_steps:
                if dep >= step.step_id:
                    raise ValueError(
                        f"step {step.step_id} input_steps invalid step_id"
                        f" {dep} (must reference earlier step)"
                    )

        return self


# ==========================================================
# EXECUTOR PROMPT (locked prompt outputs)
# ==========================================================
class ExecutorResult(BuddyBaseModel):
    status: Literal["success", "followup", "abort"]

    followup_question: str = Field(default="")
    abort_reason: str = Field(default="")

    tool_call: Dict[str, Any] = Field(default_factory=dict)


class FinalRespond(BuddyBaseModel):
    execution_result: Literal["error", "success", "partial"]
    response: str
    memory_candidates: List[MemoryIngestionResult] = Field(default_factory=list)
