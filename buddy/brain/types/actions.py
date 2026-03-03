from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from buddy.brain.types.signals import SearchScope
from buddy.brain.types.traces import DecisionTrace


class DecisionMode(str, Enum):
    DIRECT = "DIRECT"
    FOLLOWUP = "FOLLOWUP"
    SEARCH = "SEARCH"
    TOOL = "TOOL"
    LLM_ONLY = "LLM_ONLY"


class StepType(str, Enum):
    LOCAL_SEARCH = "LOCAL_SEARCH"
    WEB_SEARCH = "WEB_SEARCH"
    TOOL_CALL = "TOOL_CALL"
    LLM_CALL = "LLM_CALL"
    MEMORY_WRITE = "MEMORY_WRITE"
    NOOP = "NOOP"


class ActionStep(BaseModel):
    step_id: str
    type: StepType
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class ActionPlan(BaseModel):
    mode: DecisionMode

    selected_context_ids: list[str] = Field(default_factory=list)
    selected_memory_ids: list[str] = Field(default_factory=list)

    search_scope: SearchScope = SearchScope.NONE
    steps: list[ActionStep] = Field(default_factory=list)

    requires_llm: bool = True


class BrainDecisionResult(BaseModel):

    final_context_ids: list[str] = Field(default_factory=list)
    final_memory_ids: list[str] = Field(default_factory=list)


# ----------------------------------------------------------
# Pydantic v2: ensure schema is fully resolved
# ----------------------------------------------------------
ActionStep.model_rebuild()
ActionPlan.model_rebuild()
BrainDecisionResult.model_rebuild()
