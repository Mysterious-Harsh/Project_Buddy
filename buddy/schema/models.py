# schema/models.py
from __future__ import annotations


from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union


from pydantic import BaseModel, Field, model_validator

# ==========================================================
# ENUMS
# ==========================================================


class ModeType(str, Enum):
    """
    Prompt-aligned decision.mode (Brain prompt v1).
    """

    CHAT = "CHAT"
    ACTION = "ACTION"


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

    mode: ModeType
    planner_instructions: str = Field(default="")
    response: str = Field(default="")
    afterthought: str = Field(default="")
    model_config = {"populate_by_name": True}


# ==========================================================
# MEMORY PROMPTS (locked outputs)
# ==========================================================

# ADD: Retrieval gate output schema (before brain prompt)


class RetrievalGateResult(BuddyBaseModel):

    search_queries: List[str] = Field(default_factory=list)
    deep_recall: bool = Field(default=False)


class MemoryIngestionResult(BuddyBaseModel):
    """
    Prompt-aligned Brain memory output (brain_prompt v1).
    Matches memories[] JSON in the Brain prompt.
    """

    memory_type: Literal["flash", "short", "long", "discard"]
    memory_text: str
    salience: float = Field(ge=0.0, le=1.0)
    protection_tier: str = "normal"  # normal | critical | immortal


class MemorySummaryResult(BuddyBaseModel):
    """
    Prompt-aligned Brain memory summary output (brain_prompt v1).
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
    { "decision": {...}, "memories": [{...}, ...] }
    """

    decision: Decision
    memories: List[MemoryIngestionResult]


# ==========================================================
# PLANNER PROMPT (locked prompt outputs)
# ==========================================================


class PlannerStep(BuddyBaseModel):
    step_id: int = Field(ge=1)
    tool: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    hints: str = Field(default="")
    input_steps: List[int] = Field(default_factory=list)
    output: Optional[str] = Field(default=None)


class PlannerResult(BuddyBaseModel):
    """
    LOCKED Planner output schema:

    {
      "status": "success" | "followup" | "refusal",
      "message": "",        // followup question or refusal reason; "" on success
      "responder_instruction": "", // briefing for Responder; populated on success only
      "steps": [ ... ]
    }

    Invariants:
    - status="success"  => steps non-empty, message="", responder_instruction non-empty
    - status="followup" => steps=[], message non-empty, responder_instruction=""
    - status="refusal"  => steps=[], message non-empty, responder_instruction=""
    """

    status: Literal["success", "followup", "refusal"] = "success"
    message: str = ""
    responder_instruction: str = ""

    steps: List[PlannerStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_planner_contract(self) -> "PlannerResult":
        self.message = (self.message or "").strip()
        self.responder_instruction = (self.responder_instruction or "").strip()

        if self.status == "followup":
            if not self.message:
                raise ValueError("status=followup requires non-empty message")
            if self.steps:
                raise ValueError("status=followup requires steps=[]")
            self.responder_instruction = ""
            return self

        if self.status == "refusal":
            if not self.message:
                raise ValueError("status=refusal requires non-empty message")
            if self.steps:
                raise ValueError("status=refusal requires steps=[]")
            self.responder_instruction = ""
            return self

        # success branch
        self.message = ""

        # validate input_steps reference earlier steps only
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
    status: Literal["success", "followup", "refusal"]
    message: str = Field(default="")
    function: Optional[str] = Field(default=None)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class FinalRespond(BuddyBaseModel):
    execution_result: Literal["error", "success", "partial"]
    response: str
    memory_candidates: List[MemoryIngestionResult] = Field(default_factory=list)


# ==========================================================
# BROWSER TOOL (micro-planner action)
# ==========================================================


class BrowserAction(BuddyBaseModel):
    """
    Single action output from the browser micro-planner.
    Produced by brain.run_browser_action() each loop iteration.

    function meanings:
      fill(selector, value)       — type text into a field
      click(selector)             — click an element
      scroll(px)                  — scroll page (positive=down, negative=up)
      wait(selector, timeout_ms)  — wait for element (selector optional)
      fetch_memory(query)         — retrieve stored personal data value
      ask_user(question)          — need human input (CAPTCHA / 2FA)
      done(summary)               — task completed successfully
      error(reason)               — cannot proceed
    """

    function: Literal[
        "navigate",
        "fill",
        "click",
        "scroll",
        "wait",
        "fetch_memory",
        "ask_user",
        "done",
        "error",
    ]
    arguments: Dict[str, Any] = Field(default_factory=dict)
    summary: str


class ReaderResult(BuddyBaseModel):
    relevant: bool = Field(default=False)
    content: str = Field(default="")


class VisionResult(BuddyBaseModel):
    description: str = Field(default="")
    objects: List[str] = Field(default_factory=list)
    text_found: str = Field(default="")
    key_finding: str = Field(default="")
