Buddy v1 – Execution Flow (Final)

This document defines the exact runtime execution flow of Buddy v1 from a user query to a final response.

This flow is operational, deterministic, and binding for v1.

⸻

Entry Point

buddy_core/main.py → process_turn(user_query: str)

This is the only public entry point for a user turn.

Responsibilities:
• Orchestrate the full pipeline
• Pass data between subsystems
• Execute decisions safely
• Apply memory side effects
• Return the final assistant response

BuddyCore does not decide what to do.
It executes decisions produced by the Brain.

⸻

Full Turn Flow

⸻

0️⃣ Turn Initialization (RAM-first)

Files
• buddy_core/main.py
• context/conversation_context.py 🔒

Actions: 1. Create a RAM MemoryEntry for the raw user_query 2. Store it in ConversationContext:
• ram dictionary (memory_id → MemoryEntry)
• recent_ids list (most-recent-first) 3. Append the user message to in-memory conversation state

Rules:
• Every turn creates a RAM memory entry
• No turn_id, no ephemeral IDs
• Durability is decided later

Purpose:
• Enable coreference immediately
• Preserve conversational continuity

⸻

1️⃣ Anchor Extraction (Deterministic)

File
• context/anchor_extractor.py

Input:
• user_query

Output:
• List of Anchor objects

Anchors detect:
• Filenames, folders, extensions
• Applications
• URLs, domains, emails
• Document hints (resume, pdf, invoice)
• Capitalized surface entities

Rules:
• Anchors are session-scoped
• Anchors are not memories

Anchors are stored in:
• ConversationContext

⸻

2️⃣ Coreference Resolution

Files
• context/coref_resolver.py 🔒
• context/conversation_context.py

Inputs:
• user_query
• Recent RAM memories
• Recent anchors
• Optional memory candidates

Output:
• ResolutionResult(memory_id | None, reason, debug)

Rules:
• Coref resolver never stores memory
• Returns references only
• Deterministic fallback order: 1. Explicit anchor match 2. RAM memory match 3. Heuristic noun/intent match 4. Anchor fallback 5. Semantic fallback

⸻

3️⃣ ContextCandidate Construction

Owner
• brain/prompt_builder.py

Inputs:
• ResolutionResult
• Anchors
• Lightweight entity hints

Produces:
• resolved_contexts: list[ContextCandidate]

Each ContextCandidate contains:
• context_id
• primary_entity (optional)
• entities
• resolver_confidence
• Summary + debug payload

Rules:
• Ephemeral (per-turn only)
• Never stored

⸻

4️⃣ Memory Retrieval (Read-only)

Files
• memory/memory_manager.py
• memory/vector_store.py
• memory/sqlite_store.py 🔒

Steps: 1. Embed user_query 2. Semantic vector search (top-K) 3. Hydrate from SQLite 4. Return MemoryCandidate list

Rules:
• No recency boosting
• Semantic similarity only
• SQLite is source of truth

⸻

5️⃣ Brain Decision + Ingestion (LLM Call #1)

Files
• brain/prompt_builder.py
• prompts/brain_prompts.py
• brain/output_parser.py
• schemas/models.py

LLM input:
• user_query
• resolved_contexts
• memory_candidates
• system state
• Minimal conversation info

LLM returns strict JSON:
{
"decision": {
"mode": "DIRECT | TOOL | SEARCH | MULTI_STEP | FOLLOWUP | LLM_ONLY",
"intent": "string",
"response": "string",
"search_scopes": ["LOCAL", "WEB"],
"confidence": 0.0,
"entities": []
},
"ingestion": {
"memory_type": "flash | short | long | discard",
"pin": false,
"salience": 0.0,
"confidence": 0.0,
"tags": [],
"entities": [],
"reason": "string"
}
}

6️⃣ Apply Ingestion Result

Owner
• memory/memory_manager.py 🔒

Rules:
• RAM memory already exists
• flash | short | long → durable storage + vector upsert
• discard → RAM only
• Vector failures are marked pending_upsert

LLM never touches storage directly.

⸻

7️⃣ Mode Execution

🟢 DIRECT
• Build responder prompt
• LLM Call #2
• Return response

⸻

🟠 TOOL

Files
• tools/registry.py
• tools/os/\*

Steps: 1. Select tool 2. Execute exactly one action 3. Capture result 4. Responder summarizes outcome

⸻

🔵 SEARCH
• Execute search for each scope in search_scopes
• Aggregate results
• Pass to responder LLM

⸻

🟣 MULTI_STEP

Files
• prompts/planner_prompts.py
• buddy_core/controller.py

Steps: 1. Planner builds ordered steps 2. Execute sequentially 3. Accumulate results 4. Final responder LLM call

⸻

🟡 FOLLOWUP
• Return one clarification question
• No tools
• No durable memory writes

⸻

⚪ LLM_ONLY
• Pure conversation
• No tools
• No durable memory writes

⸻

8️⃣ Memory Access Updates

Owner
• memory/memory_manager.py

If memories were used:
• Increment access_count
• Update last_accessed

Rules:
• Explicit only
• No implicit touches

⸻

9️⃣ Final Response

BuddyCore returns:
• Assistant response text
• Optional tool metadata
• Updated conversation state
