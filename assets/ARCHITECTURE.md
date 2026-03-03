# Buddy Architecture (v1)

Buddy is an OS-level AI companion designed to run locally-first, with optional web access and tool execution.
The goal of v1 is a **clean, deterministic pipeline** with minimal moving parts:

-   deterministic context + memory retrieval
-   one primary Brain LLM call for decision + ingestion
-   optional planner LLM call for MULTI_STEP
-   optional responder LLM call to produce the final natural-language response
-   optional tool execution via a registry

Buddy stores durable memories in SQLite (source of truth) and indexes embeddings in a vector store.

---

## Goals (v1)

1. **Deterministic orchestration**: code decides what to fetch/call; LLM only fills structured decisions and language.
2. **Local-first**: prefer local tools + local search; web only when allowed and needed.
3. **Fast happy path**: common turns should be 1–2 LLM calls.
4. **Durable memory**: SQLite is truth; vector DB is an index.
5. **Clear boundaries**:
    - `context/*` resolves references
    - `memory/*` manages add/retrieve/touch/upsert
    - `brain/*` decides mode + ingestion and builds prompts
    - `tools/*` executes actions
    - `buddy_core/*` orchestrates the whole turn

---

## Non-goals (v1)

-   Complex agent frameworks
-   Background task scheduling
-   Long-running multi-turn plans with state machines
-   Online-only features as core dependency
-   “Magical” tool execution without explicit tool routing

---

## High-level Pipeline

### Entry: `BuddyCore.process_turn(user_query)`

**Inputs**

-   `user_query`
-   runtime state (conversation state, system state: connectivity, allowed tools/search)
-   stores/providers (SQLiteStore, VectorStore, EmbeddingProvider, etc.)

**Outputs**

-   final assistant response string
-   optional tool results
-   updated conversation state (last_selected_context_id, etc.)

---

## Core Turn Flow (v1)

### Step 0: Normalize + Turn bookkeeping

-   create `turn_id`
-   store raw user message as a _turn memory_ (usually `flash` unless ingestion says otherwise)
-   create a `TurnRef` record for conversation context store

> NOTE: Storing the “turn log” is allowed, but should not automatically become a durable memory unless ingestion says so.

---

### Step 1: Anchor extraction (cheap, deterministic)

**File**: `context/anchor_extractor.py`

-   Extract anchors from the user query and/or recent turns:
    -   filenames, app names, urls/domains, emails, folders, doc hints like “resume”
-   Produce `Anchor` objects (may have `memory_id` if anchor came from a stored memory/turn)

Anchors are used to resolve pronouns and “open it” style references.

---

### Step 2: Context candidate building (deterministic)

**Files**:

-   `context/conversation_context.py`
-   `context/coref_resolver.py`

1. Conversation context store returns:

-   recent `TurnRef`s (memory_id + speaker + turn_index)
-   recent anchors

2. CorefResolver runs:

-   input: `utterance`, `recent_turns`, `anchors`, optional `candidates`
-   output: a `ResolutionResult` containing:
    -   `memory_id` (best referent) or None
    -   reason + debug

3. ContextCandidate builder creates `resolved_contexts`:

-   At minimum:
    -   `context_id`
    -   `primary_entity` (if resolvable)
    -   `entities` (lightweight)
    -   `resolver_confidence` (heuristic based on resolver path)
    -   summary (short)
    -   raw/debug payload

**Important**

-   v1 coref resolver returns only a `memory_id`.
-   The ContextCandidate builder is where we convert that into “candidate contexts”.

---

### Step 3: Memory retrieval (vector index, no recency bias)

**Files**:

-   `memory/memory_manager.py`
-   `memory/sqlite_store.py` (truth)
-   `memory/vector_store.py` (index)

MemoryManager retrieves top-K similar memories using embeddings:

-   query embedding from `EmbeddingProvider`
-   vector search in `VectorStore`
-   hydrate with SQLite (by memory_id)
-   return `MemoryCandidate` list with `semantic_score`

**Rule**: ranking is by semantic score (plus optional entity overlap in decision engine). **No recency boost**.

---

### Step 4: Brain decision + ingestion (LLM call #1)

**Files**:

-   `brain/brain.py`
-   `prompts/brain_prompts.py` (brain_prompt)
-   `brain/output_parser.py`
-   `schema/models.py` (Decision+Ingestion)

Brain builds inputs:

-   `user_query`
-   `resolved_contexts` (small list)
-   `memory_candidates` (small list)
-   system state + conversation state (minimal)

LLM returns strict JSON:

-   `decision`: { mode, requires_memory, search_scope, confidence, entities[] }
-   `ingestion`: { should_store, memory_type, salience, tags, entities, reason }

---

### Step 5: Apply ingestion result (deterministic)

**Owner**: `memory/memory_manager.py`

If `ingestion.should_store = true`:

-   create/update a MemoryEntry in SQLite with:
    -   text
    -   memory_type, importance (may map from salience)
    -   tags
    -   entities stored in metadata (optional)
-   **Eager upsert** to vector store:
    -   embed memory (if needed)
    -   attempt vector upsert immediately
    -   if fails: mark `pending_upsert=1` in SQLite

If `should_store = false`:

-   do not create durable memory
-   still allow anchors to exist for the session

---

### Step 6: Mode execution

#### DIRECT

-   build response prompt (Responder prompt)
-   LLM call to write final answer
-   return response

#### SEARCH

-   choose LOCAL vs WEB using decision.search_scope
-   execute search tool(s)
-   then responder LLM call uses results to answer

#### TOOL

-   choose tool from registry using tool prompt or lightweight router
-   execute tool once
-   then responder LLM call summarizes result

#### MULTI_STEP

-   Planner LLM call builds plan steps
-   execute steps through tools/search
-   responder LLM call produces final response

#### FOLLOWUP

-   return followup question and choices (no tool execution)

#### LLM_ONLY

-   responder LLM call without any retrieval/tooling

---

### Step 7: Touch + update access (deterministic)

**Owner**: MemoryManager / BuddyCore (single place, choose one)

When Buddy uses a memory in the final answer or tool decision:

-   call `SQLiteStore.touch(memory_id)` for those memory IDs
-   optional: update anchor store to include that memory_id as most recent referent

ConversationState updates:

-   last_selected_context_id
-   last_user_query
-   last_intent_hint (optional)

---

## Example Turn (v1)

User: “do you know my wife can you open her resume from documents”

1. Anchor extraction:

-   anchors: ["wife" (relationship), "resume" (document), "Documents" (folder)]

2. Coref resolution:

-   pronoun “her” → resolve to memory about wife if present (memory_id = m_wife)
-   resolver_confidence depends on path (anchor match vs semantic fallback)

3. Memory retrieval:

-   vector search likely returns:
    -   memory about wife’s name
    -   memory about resume filename/location if previously stored

4. Brain decision+ingestion (LLM):

-   decision.mode likely MULTI_STEP (needs: identify which resume, locate file, open)
-   ingestion likely discard (command-only)

5. Planner:

-   step1: local search for “resume” in Documents
-   step2: if multiple, ask followup or pick best match using memory hints
-   step3: open_file tool

6. Tool execution:

-   filesystem.search_files → returns candidates
-   os.open_file → opens selected file

7. Response:

-   “Opened Pallavi’s resume from Documents: …”

If Buddy lacks prior memory tying “wife” to a person or resume:

-   it should ask FOLLOWUP:
    -   “What’s your wife’s name?” or “Which resume file should I open?”

---

## Where pipeline code lives

-   `buddy/buddy_core/buddy_core.py` is the canonical orchestrator:

    -   calls context + memory + brain + tools + responder
    -   applies ingestion + touches memories
    -   returns TurnResult

-   `brain/brain.py` handles:

    -   building the LLM inputs
    -   calling the Brain prompt
    -   parsing into Decision + Ingestion
    -   calling planner/responder prompts if needed

-   `memory/memory_manager.py` handles:

    -   add/store/touch/retrieve
    -   eager vector upsert
    -   maintenance triggers

-   `tools/` is isolated, registry-based execution.

---

## Determinism Rules

-   Context resolver is deterministic and offline-safe.
-   DecisionEngine is deterministic.
-   Only LLM calls are:
    -   brain (decision+ingestion)
    -   planner (MULTI_STEP only)
    -   responder (final output)
-   Vector similarity influences memory retrieval only; no time-based boosting.

---
