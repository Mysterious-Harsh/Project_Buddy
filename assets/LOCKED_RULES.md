# Buddy Locked Rules (v1)

This document defines what is locked, what can change, and the non-negotiable invariants for v1.

---

## Absolute Invariants (v1)

1. **SQLite is the source of truth** for durable memories.
2. Vector store is an **index only** (rebuildable).
3. **DecisionEngine is deterministic**:
    - no I/O, no LLM calls, no tool calls
4. **No recency boosting for memory ranking**:
    - memory selection uses semantic_score + entity overlap only
5. **CorefResolver is deterministic and lightweight**:
    - no hard dependency on heavy libraries
    - can optionally use fastcoref if available
6. **Ingestion controls durability**:
    - whether to store, memory_type, tags, salience
    - command-only interactions should be discarded
7. **Eager upsert**:
    - when a memory is stored, attempt vector upsert immediately
    - only mark pending_upsert if upsert fails
8. **Touch is explicit**:
    - only memories that were actually used should have access_count incremented
    - touching happens in exactly one place (MemoryManager or BuddyCore)

---

## Locked Files (current v1 baseline)

These files are considered locked unless a breaking bug is found.
Schema changes require migrations + tests.

### Context

-   `buddy/context/coref_resolver.py` 🔒
    -   returns memory_id only
    -   deterministic behavior with strong fallbacks

### Memory

-   `buddy/memory/sqlite_store.py` 🔒

    -   schema semantics are stable
    -   no column meaning changes without migration plan

-   `buddy/memory/maintenance.py` 🔒
    -   orchestration only
    -   no LLM calls

### Brain

-   `buddy/brain/decision_engine.py` 🔒
    -   deterministic scoring + routing only
    -   MUST NOT use recency

---

## Allowed Changes (v1)

-   Add new files/modules as long as invariants are preserved.
-   Add tests.
-   Performance improvements that preserve outputs.
-   Tool additions under `buddy/tools/*`.
-   Add new prompt files under `buddy/prompts/*` if they follow strict JSON contracts.

---

## Schema Rules

-   Any change to `SQLiteStore.memories` schema:

    -   must include explicit migration plan
    -   must include updated tests
    -   must preserve column meanings

-   The Brain JSON contracts must be stable once locked:
    -   decision + ingestion output shape must not change without version bump

---

## Prompt Rules

-   Brain prompt (`buddy/prompts/brain_prompts.py::brain_prompt`) is the canonical decision+ingestion prompt.
-   Output must be **valid JSON only** and match schema.
-   If uncertain:
    -   decision should prefer FOLLOWUP
    -   ingestion should prefer discard

---

## Tooling Rules

-   Tools must be registered through `buddy/tools/registry.py`.
-   Tools must return a structured `ToolResult` or `ToolError`.
-   Tool calls must be explicit:
    -   no implicit side effects without recording outcome in response.
-   Tool execution is gated by:
    -   Decision.mode == TOOL or MULTI_STEP
    -   system.allowed_tools includes that tool

---

## Where responsibilities live (do not blur)

-   `buddy_core` orchestrates a turn.
-   `brain` decides mode + optionally plans + builds prompts.
-   `memory_manager` stores/retrieves/touches + vector sync.
-   `context` resolves referents and builds context candidates.
-   `tools` execute system actions.

---

## v1.5 / v2 Allowed Evolution

These are permitted only after v1 is stable:

-   hybrid consolidation grouping using vector similarity
-   allow flash consolidation only when high salience/importance
-   more robust planner + action routing
-   richer tool result formatting and retries
-   background maintenance scheduling

---
