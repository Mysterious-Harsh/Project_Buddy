from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class MemoryKind(str, Enum):
    FACT = "FACT"
    PREFERENCE = "PREFERENCE"
    EPISODE = "EPISODE"
    DOC = "DOC"
    SKILL = "SKILL"
    OTHER = "OTHER"


class ContextKind(str, Enum):
    ENTITY_FOCUSED = "ENTITY_FOCUSED"
    TASK_FOCUSED = "TASK_FOCUSED"
    CHAT_FOCUSED = "CHAT_FOCUSED"


class Connectivity(str, Enum):
    OFFLINE = "OFFLINE"
    ONLINE = "ONLINE"


class SearchScope(str, Enum):
    NONE = "NONE"
    LOCAL = "LOCAL"
    WEB = "WEB"


class SystemState(BaseModel):
    connectivity: Connectivity
    allow_web_search: bool
    allow_local_search: bool
    allowed_tools: list[str] = Field(default_factory=list)

    now_iso: str  # e.g. "2025-12-28T00:12:00-04:00"
    timezone: str  # e.g. "America/Moncton"


class ConversationState(BaseModel):
    turn_id: str
    session_id: str

    last_user_query: Optional[str] = None
    last_selected_context_id: Optional[str] = None
    last_intent_hint: Optional[str] = None


class EntityRef(BaseModel):
    name: str
    type: str  # person/project/app/company/file/etc.
    id: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)


class ContextCandidate(BaseModel):
    context_id: str
    kind: ContextKind

    primary_entity: Optional[EntityRef] = None
    entities: list[EntityRef] = Field(default_factory=list)

    time_anchor: Optional[str] = None  # ISO date or natural anchor tag ("today")
    resolver_confidence: Optional[float] = None  # 0..1 if available

    summary: str
    raw: dict[str, Any] = Field(
        default_factory=dict
    )  # opaque payload from locked resolver


class MemoryCandidate(BaseModel):
    memory_id: str
    kind: MemoryKind

    entities: list[EntityRef] = Field(default_factory=list)
    semantic_score: float  # vector similarity only

    summary: str
    content: str

    source: Optional[str] = None
    created_at_iso: Optional[str] = None  # debug/display only (NOT for ranking)


class BrainInputs(BaseModel):
    user_query: str

    resolved_contexts: list[ContextCandidate] = Field(default_factory=list)
    memory_candidates: list[MemoryCandidate] = Field(default_factory=list)

    system: SystemState
    conversation: Optional[ConversationState] = None

    user_profile: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
