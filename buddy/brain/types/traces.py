from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class AmbiguityReason(str, Enum):
    CONTEXT_TIE = "CONTEXT_TIE"
    ENTITY_CONFLICT = "ENTITY_CONFLICT"
    TIME_CONFLICT = "TIME_CONFLICT"
    INTENT_CONFLICT = "INTENT_CONFLICT"
    LOW_EVIDENCE = "LOW_EVIDENCE"


class SignalName(str, Enum):
    QUERY_ENTITY_MATCH = "QUERY_ENTITY_MATCH"
    MEMORY_SUPPORT = "MEMORY_SUPPORT"
    CONTINUITY = "CONTINUITY"
    RESOLVER_CONFIDENCE = "RESOLVER_CONFIDENCE"
    CONFLICT_PENALTY = "CONFLICT_PENALTY"


class ScoreBreakdown(BaseModel):
    total: float
    components: dict[str, float] = Field(
        default_factory=dict
    )  # SignalName -> contribution
    notes: list[str] = Field(default_factory=list)


class DecisionTrace(BaseModel):
    selected_mode: str  # mirror DecisionMode string for logging

    context_scores: dict[str, ScoreBreakdown] = Field(default_factory=dict)
    best_context_id: Optional[str] = None
    second_best_context_id: Optional[str] = None

    ambiguity: bool = False
    ambiguity_reasons: list[AmbiguityReason] = Field(default_factory=list)

    memory_used: bool = False
    memory_support: dict[str, float] = Field(
        default_factory=dict
    )  # context_id -> support score

    notes: list[str] = Field(default_factory=list)
