<p align="center">
  <img src="assets/banner.svg" width="900" alt="Buddy — Offline Cognitive AI Companion"/>
</p>

<p align="center">
  <a href="#">
    <img src="https://img.shields.io/badge/PYTHON-3.11%2B-000000?style=for-the-badge&logo=python&logoColor=00e5ff&labelColor=000000&color=000000" alt="Python 3.11+"/>
  </a>
  <a href="#">
    <img src="https://img.shields.io/badge/LLM-FULLY%20LOCAL-000000?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0iIzAwZTVmZiIgZD0iTTEyIDJDNi40OCAyIDIgNi40OCAyIDEyczQuNDggMTAgMTAgMTAgMTAtNC40OCAxMC0xMFMxNy41MiAyIDEyIDJ6bTAgMThjLTQuNDIgMC04LTMuNTgtOC04czMuNTgtOCA4LTggOCAzLjU4IDggOC0zLjU4IDgtOCA4eiIvPjwvc3ZnPg==&logoColor=00e5ff&labelColor=000000&color=000000" alt="Local LLM"/>
  </a>
  <a href="#">
    <img src="https://img.shields.io/badge/MODEL-Qwen3--14B-000000?style=for-the-badge&logoColor=7c4dff&labelColor=000000&color=000000" alt="Qwen3-14B"/>
  </a>
  <a href="#">
    <img src="https://img.shields.io/badge/PLATFORM-macOS%20%7C%20LINUX-000000?style=for-the-badge&logoColor=7c4dff&labelColor=000000&color=000000" alt="Platform"/>
  </a>
  <a href="#">
    <img src="https://img.shields.io/badge/STATUS-ACTIVE-000000?style=for-the-badge&logoColor=00e5ff&labelColor=000000&color=000000" alt="Active"/>
  </a>
</p>

<br/>

<p align="center">
  <strong>An offline AI built around human-like memory — not a chat loop.</strong><br/>
  <sub>It knows you. It remembers you. It thinks with you.</sub>
</p>

<br/>

```
  ▶  INITIALIZING COGNITIVE PIPELINE...
  ▶  BOOT ORCHESTRATOR: llama-server auto-starting...
  ▶  MODEL SELECTOR: Qwen3-14B-Q4_K_M detected  ·  hardware-matched
  ▶  MEMORY SUBSYSTEM ONLINE  ·  SQLite + Qdrant
  ▶  EMBEDDINGS READY  ·  Qwen3-Embedding-0.6B  ·  1024-dim L2-norm
  ▶  CONSOLIDATION ENGINE v4.1 READY  ·  background thread
  ▶  SEARXNG ONLINE  ·  local web search
  ▶  ACTION MODE ARMED  ·  terminal · filesystem · web · vision
  ▶  VOICE PIPELINE ONLINE  ·  always listening
  ▶  AURORA TUI READY
  ▶  AWAITING INPUT
```

---

## `$ what --is buddy`

Buddy is a **fully offline AI built around a human-like cognitive memory system** — not a chat loop, not a tool pipeline. It doesn't just generate text. It builds and maintains a structured, persistent understanding of you across every session — who you are, what you care about, what you've talked about — and uses that understanding to think with you, not just respond to you.

The memory system is not a feature. It is the foundation everything else is built on.

Every component is designed with a single constraint in mind: **your data never leaves your machine.**

---

## `$ why --does-this-exist`

Most AI assistants today share the same fundamental flaw — they have no real memory of you.

| Problem        | What everyone else does           | What Buddy does                                        |
| -------------- | --------------------------------- | ------------------------------------------------------ |
| Memory         | Forgets after the context window  | Persistent multi-tier memory across all sessions       |
| Quality        | Accumulates junk forever          | Self-consolidates during idle — denser over time       |
| Privacy        | Cloud APIs, remote inference      | 100% local, air-gap capable                            |
| Understanding  | Treats every message as new       | Builds a structured model of who you are               |
| Prompts        | One giant system prompt           | Purpose-built per-module prompts, minimal and precise  |
| Context        | Fixed window, no budget           | Hardware-aware context budgeting, dynamic truncation   |
| Search         | None, or cloud-dependent          | Local SearXNG — private, fast, self-hosted             |
| Vision         | Cloud API or none                 | Local Qwen VL via llama.cpp — multi-image offline      |

Buddy was built to explore a different direction: **AI as a private, self-maintaining cognitive system that actually knows you.**

---

## `$ architecture --show-pipeline`

```
╔══════════════════════════════════════════════════════════════════╗
║                   BUDDY  ·  COGNITIVE  PIPELINE                  ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║   ┌─────────────┐     ┌──────────────────────────────────┐       ║
║   │  User Input │────▶│  0. RAM memory entry (raw query) │       ║
║   └─────────────┘     └───────────────┬──────────────────┘       ║
║                                       │                          ║
║                       ┌───────────────▼──────────────────┐       ║
║                       │  1. Anchor extraction             │       ║
║                       │     filenames · apps · URLs       │       ║
║                       │     DETERMINISTIC                 │       ║
║                       └───────────────┬──────────────────┘       ║
║                                       │                          ║
║                       ┌───────────────▼──────────────────┐       ║
║                       │  2. Coreference resolution        │       ║
║                       │     DETERMINISTIC fallback chain  │       ║
║                       └───────────────┬──────────────────┘       ║
║                                       │                          ║
║                       ┌───────────────▼──────────────────┐       ║
║                       │  3. Memory Retrieval              │       ║
║                       │     embed → vector search         │       ║
║                       │     → hydrate SQLite              │       ║
║                       │     → MemoryCandidateLite[]       │       ║
║                       └───────────────┬──────────────────┘       ║
║                                       │                          ║
║                       ┌───────────────▼──────────────────┐       ║
║                       │  4. Brain  /  LLM Call            │       ║
║                       │     → { decision, ingestion }     │       ║
║                       │       strict JSON                 │       ║
║                       └──────────┬────────────┬──────────┘       ║
║                                  │            │                  ║
║                    5. ingestion  │            │  in background   ║
║                       (non-blocking thread)   │                  ║
║                                              │                  ║
║              ┌───────────────────────────────┘                   ║
║              │  6. Mode Execution                                ║
║              │                                                   ║
║    CHAT ─────┤──▶  Direct LLM response → ui_output()            ║
║              │                                                   ║
║    ACTION ───┤──▶  Planner → ActionRouter → Executor            ║
║              │                             (per step)           ║
║              │    ┌─────────────────────────────────┐           ║
║              │    │  Tools available:               │           ║
║              │    │  · terminal  (OS commands)      │           ║
║              │    │  · filesystem (read/write/list) │           ║
║              │    │  · web search (SearXNG)         │           ║
║              │    │  · vision (Qwen VL, multi-image)│           ║
║              │    └──────────────┬──────────────────┘           ║
║              │                   │                              ║
║              │                   ▼  Responder → ui_output()     ║
║              │                                                   ║
║              └──▶  7. Touch accessed memories (one place only)  ║
║                                                                  ║
║  Memory is read at the start of every turn.                      ║
║  Memory is written at the end of every turn.                     ║
║  Everything in between serves the memory system.                 ║
╚══════════════════════════════════════════════════════════════════╝
```

**The pipeline exists to feed the memory — not the other way around.**

---

## `$ memory --show-layers`

This is the core of Buddy. Not a feature — the entire system is built around it.

Buddy models human memory the way it actually works: information enters as raw experience, gets filtered and scored, gradually consolidates into durable knowledge, and decays if it stops being relevant. Every interaction either reinforces or updates what Buddy knows about you.

```
┌─────────────────────────────────────────────────────────┐
│                    MEMORY  TIERS                        │
├───────────────┬──────────────────┬──────────────────────┤
│    FLASH      │    SHORT-TERM    │      LONG-TERM       │
│   (hot)       │   (warm)         │      (cold)          │
├───────────────┼──────────────────┼──────────────────────┤
│ ~60s – hours  │ hours – days     │ Permanent            │
│ Raw commands  │ Meaningful       │ Core identity facts  │
│ Observations  │ interactions     │ Consolidated         │
│ Discardable   │ Consolidation ↑  │ knowledge            │
│               │ candidate        │ Rarely modified      │
├───────────────┴──────────────────┴──────────────────────┤
│  Promotion:  flash → short  (strength ≥ 0.55 OR         │
│                               importance ≥ 0.70)        │
│  Promotion:  short → long   (strength ≥ 0.72 AND        │
│                               cycles ≥ 2)               │
├─────────────────────────────────────────────────────────┤
│  Storage:  SQLite (source of truth — text + metadata)   │
│            Qdrant (rebuildable vector index)             │
│  Retrieval: Semantic + consolidation_strength + arousal │
│  Ranking:   semantic×0.40 + strength×0.25 + rerank×0.15 │
│             + tier×0.10 + encoding_arousal×0.10         │
└─────────────────────────────────────────────────────────┘
```

Every memory carries: an importance score, a `consolidation_strength` that sleep cycles build over time, an encoding arousal signal from the original message, and a semantic embedding (Qwen3-Embedding-0.6B, 1024-dim, L2-normalized). Retrieval is context-aware — Buddy pulls what is _relevant_, not just what is _recent_. A fact you mentioned once six months ago surfaces when it matters, and fades when it doesn't.

**Catastrophic forgetting guard:** memories with `importance ≥ 0.80 OR (importance ≥ 0.70 AND dup_count == 0)` that have never been consolidated into another entry are permanently exempt from all hard-delete paths.

---

## `$ consolidation --show-sleep-cycle`

When Buddy is idle, the **Consolidation Engine v4.1** runs automatically in a background thread — the same way the human brain consolidates memories during sleep. (v5 with the closed recall loop is the next major upgrade, queued after the performance pass.)

```
┌──────────────────────────────────────────────────────────┐
│          SLEEP  CONSOLIDATION  CYCLE  v4.1-patched        │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  SCAN       →  Load flash + short-term memories          │
│  SCORE      →  BLA activation + arousal + fan effect     │
│               + interference + temporal gradient         │
│               → writes consolidation_strength to SQLite  │
│  CLUSTER    →  BFS connected-components over sim graph   │
│               → episodic (<14d) vs schema (≥14d) label   │
│  SUMMARIZE  →  LLM condenses clusters → new entries      │
│               → depth cap: depth≥3 requires conf≥0.70    │
│  PROMOTE    →  strength≥0.55 (age>3h) → short            │
│               → strength≥0.72 AND dup==0 → long (direct) │
│               → strength≥0.72 AND dup≥2 → long (summary) │
│  DEMOTE     →  Low-strength entries → flash / deleted    │
│  PRUNE      →  Duplicates (keep ≥1 rep), interference    │
│               → every deletion written to forgotten_log  │
│                                                          │
│  RESULT: Smaller, denser, higher-quality memory          │
│          consolidation_strength feeds recall at query time│
│                                                          │
└──────────────────────────────────────────────────────────┘
```

**When you wake Buddy up** — the consolidation cancels immediately and Buddy responds without delay. No waiting for a background job to finish.

The engine is research-grounded:

- **ACT-R activation** — importance slows memory decay (Anderson & Lebiere 1998)
- **CLS per-tier decay** — flash/short/long have biologically distinct decay rates
- **Arousal amplification** — emotionally significant memories persist longer (68 EN + 24 Hindi/Hinglish keywords)
- **Prediction error boost** — surprising/contradictory memories get higher salience
- **Reflective consolidation** — low-confidence summaries don't destroy originals (14-day provisional window)
- **Interference pruning** — near-duplicate memories are resolved; at least one representative always survives
- **BFS clustering** — order-independent connected-components; same cluster every run regardless of seed
- **Temporal coherence** — episodic (same time window) vs schema (cross-time knowledge) clusters handled differently
- **Recall integration** — `consolidation_strength` written to SQLite after every sleep cycle; retrieval weights it at 25%
- **Spreading activation** — top recalled memories activate their semantic neighbors at query time
- **Encoding arousal** — emotional intensity of the original message is captured before Brain compression and used in both sleep scoring and retrieval weighting
- **Protection tiers** — `critical` and `immortal` memories are permanently shielded from all deletion paths

---

## `$ tools --show-capabilities`

Buddy has four native tools it can invoke in ACTION mode. The Brain decides whether a tool is needed; the Planner breaks the task into steps; the Executor runs each step; the Responder synthesizes the result into a natural reply.

### Terminal

Real OS-level execution. Buddy can run shell commands, scripts, and system operations. Every destructive action goes through a confirmation gate defined inside the tool prompt — the Planner never classifies safety.

### Filesystem

Read, write, list, move, delete. Structured output at every step. Path validation and non-destructive rules enforced.

### Web Search (SearXNG)

```
┌──────────────────────────────────────────┐
│         WEB  SEARCH  PIPELINE            │
├──────────────────────────────────────────┤
│  Query → SearXNG (self-hosted, local)    │
│        → Result extraction               │
│        → Context injection into Buddy    │
│  No cloud. No tracking. No API keys.     │
└──────────────────────────────────────────┘
```

SearXNG is cloned, configured, and started automatically by `searxng_setup.py` on boot. Completely private. Buddy can search the web without sending your queries to any external service.

### Vision (Qwen VL — multi-image)

```
User message with image path(s)
  → Brain → ACTION → Planner picks "vision" tool
  → VisionTool.execute()
       → encode_image_to_data_uri() per image
           JPEG/PNG → read directly
           other    → Pillow converts to PNG in-memory
       → llm.chat(messages, images=[data_uris])
           POST /v1/chat/completions with image_url content parts
       → JSON: { description, objects, text_found, key_finding }
  → Responder synthesizes from text only
```

Full offline image understanding via Qwen2.5-VL / Qwen3-VL + mmproj running in llama.cpp. Supports multiple images per call. No cloud API.

---

## `$ hardware --show-adaptation`

Buddy detects your hardware at boot and adapts to it — not the other way around.

### Model Selector

```
boot  →  probe GPU VRAM / system RAM / CPU cores
      →  select model from Qwen catalog (Qwen2.5 / Qwen3 / Qwen3.5)
      →  preferred: Qwen3-14B-Q4_K_M (default for ≥16GB)
      →  falls back: Qwen2.5-7B / smaller quants on constrained hardware
      →  injects selected model path into llama-server args
```

### Context Budget

```
boot  →  ContextBudget.from_hardware()
         → n_ctx derived from RAM/VRAM
         → injected into llama-server at startup

turn  →  ContextBudget.adjusted_for_pressure()
         → recent_turns ± 1 based on memory pressure
         → steps adjusted dynamically

call  →  SmartTruncator trims inputs to char budget
         → middle-cut: removes center of long content
         → proportional trim: shrinks each section to fit
         → history trimming: drops oldest turns first
```

Override any value in `buddy.toml` under `[context_budget]`.

---

## `$ act --show-mode`

ACT mode is a capability layered on top of the memory system. When a task requires it, Buddy can execute real operations on your machine — but this is not what Buddy _is_. Buddy is a cognitive memory system first. ACT mode is a tool it can pick up when needed.

```python
# When ACT mode is triggered
[PLAN]     → Break task into atomic steps (PlannerResult schema)
[EXECUTE]  → Run tool per step (ExecutorResult schema)
[ANALYZE]  → Validate output against expected result
[RETRY]    → Adjust and rerun on failure
[RESPOND]  → Responder synthesizes final reply (FinalRespond schema)
[STORE]    → Record result and what was learned into memory
```

**Safety constraints built in:**

- Destructive confirmation gate lives inside each tool prompt — not the Planner
- Directory and path validation before every execution
- Structured tool output logging at every step
- Error stack tracking with full context
- Planner can refusal or request followup before any step runs

---

## `$ context --show-philosophy`

Buddy avoids large fixed system prompts entirely.

```
  ✗  Traditional:  [Giant system prompt] + [User message]
  ✓  Buddy:        [Module prompt] + [Retrieved memory] + [User message]
```

- Each module receives only its relevant instructions
- Memory is retrieved dynamically per-query, not statically embedded
- Prompt length scales with actual task complexity
- Token efficiency is treated as a design constraint, not an afterthought
- All LLM outputs are **strict JSON** — validated by Pydantic, repaired by `json_repair.py`
- Local model first: every prompt is designed to be reliably generatable by a 7B-14B model

**Schema contracts** (locked once set, never changed):

```python
BrainResult       →  decision (intent + response) + ingestion (memory instruction)
PlannerResult     →  steps[] + followup + refusal
ExecutorResult    →  status + tool_call + followup_question + abort_reason
FinalRespond      →  execution_result + response + memory_candidates
```

---

## `$ voice --show-architecture`

Buddy listens continuously — **no wake word, no push-to-talk, no trigger phrase.** The moment you speak, it hears you. The same way a person does.

```
┌──────────────────────────────────────────────────────────────────┐
│                  ALWAYS-LISTENING  VOICE  PIPELINE               │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│   MICROPHONE  ──▶  stt-listen   Opens mic, runs calibration,     │
│                    (thread 1)   adaptive noise floor tracking    │
│                         │                                        │
│                         ▼                                        │
│                    DUAL  VAD  ENGINE                             │
│                                                                  │
│       ┌──────────────────────────────────────────┐               │
│       │  Silero VAD  (preferred)                 │               │
│       │  Neural model · 32 ms chunks             │               │
│       │  Robust against hum / non-speech noise   │               │
│       │  Falls back to Custom VAD automatically  │               │
│       ├──────────────────────────────────────────┤               │
│       │  Custom VAD  (zero-dependency fallback)  │               │
│       │  Energy + Crest factor + ZCR heuristics  │               │
│       │  Impulse/knock rejection built-in        │               │
│       │  Onset flatness check → rejects humming  │               │
│       └──────────────────────────────────────────┘               │
│                         │                                        │
│                    SPEECH  CONFIRMED  →  🔔 880 Hz beep           │
│                         │                                        │
│                         ▼                                        │
│                    stt-tx  (thread 2)                            │
│                    faster-whisper transcription                  │
│                    Beam search · language detection              │
│                         │                                        │
│                         ▼                                        │
│                    stt-cb  (thread 3)                            │
│                    Callback dispatcher · drop-oldest queue       │
│                         │                                        │
│                         ▼                                        │
│                    Buddy  Brain  ◀── text arrives                │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**How speech detection works:**

```
  IDLE         ──┐
                 │  Energy crosses adaptive threshold
                 │  + sustained for 200 ms onset window
                 │  + not impulsive (knock/tap rejected)
                 │  + not flat (humming rejected)
                 ▼
  RECORDING    ──┐
                 │  Pre-roll buffer prepended (captures first syllable)
                 │  880 Hz beep confirms recording started
                 │  Hangover timer: 2 s silence → segment closed
                 ▼
  TRANSCRIBE   ──▶  faster-whisper decodes audio
                    Result delivered to Buddy
```

**Mute is resource-aware — not just silent:**

| State         | Microphone     | Whisper model           | CPU             |
| ------------- | -------------- | ----------------------- | --------------- |
| **Listening** | Open           | Loaded in RAM/VRAM      | Active VAD loop |
| **Muted**     | Released to OS | Evicted from memory     | Zero wakeups    |
| **Unmuted**   | Reclaimed      | Reloaded + recalibrated | Active VAD loop |

Muting is not just pausing — the mic is physically released back to the OS and Whisper is removed from RAM/VRAM entirely. Unmuting triggers a fresh calibration pass to re-establish the noise floor.

**Key design properties:**

```
  ✓  No wake word        — always ready, like a person in the room
  ✓  No cloud STT        — faster-whisper runs fully offline
  ✓  Adaptive threshold  — adjusts to your room's noise level automatically
  ✓  Pre-roll buffer     — never clips the first word of what you say
  ✓  Instant feedback    — 880 Hz beep confirms the moment speech is detected
  ✓  Dual VAD            — Silero neural model with zero-dep custom fallback
  ✓  Auto-resampling     — works with any microphone's native sample rate
```

---

## `$ ui --show-tui`

Buddy's interface is a **Textual TUI** styled with the Aurora palette — a terminal-native UI with screens, widgets, hotkeys, and a live status toolbar. No browser, no Electron, no web server.

```
┌──────────────────────────────────────────────────────────────────┐
│  AURORA  ·  TEXTUAL  TUI                                         │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                   Buddy Face / Status                      │  │
│  ├────────────────────────────────────────────────────────────┤  │
│  │                                                            │  │
│  │   Conversation area — Markdown-rendered, scrollable        │  │
│  │   Buddy's responses stream in real time                    │  │
│  │                                                            │  │
│  ├────────────────────────────────────────────────────────────┤  │
│  │  [Input]  ·  voice toggle  ·  status toolbar               │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  Screens: Main chat  ·  Memory browser  ·  Settings             │
│  Hotkeys: voice mute/unmute, interrupt, clear, quit             │
│  Palette: Aurora — deep blacks, cyan, violet, subtle glows       │
└──────────────────────────────────────────────────────────────────┘
```

---

## `$ features --list`

|     | Feature                       | Description                                                              |
| --- | ----------------------------- | ------------------------------------------------------------------------ |
| 🧠  | **Multi-tier memory**         | Flash → Short → Long, persisted across sessions                          |
| 🌙  | **Sleep consolidation v4.1**  | BFS clustering, temporal coherence, depth cap, audit log                 |
| 📈  | **Consolidation strength**    | Sleep cycle scores flow into retrieval — memory improves over time       |
| 🌊  | **Spreading activation**      | Top recalled memories activate their semantic neighbors                  |
| 🔥  | **Encoding arousal**          | Emotional intensity captured at encoding; English + Hindi keywords       |
| 🛡️  | **Protection tiers**          | normal / critical / immortal — LLM-assigned, enforced at all stages      |
| ⚡️  | **ACT mode**                  | Real OS-level action execution with planner, executor, retry logic       |
| 🔒  | **Fully offline**             | Local LLM via llama.cpp — zero cloud calls                               |
| 🎤  | **Always-listening voice**    | No wake word — continuous dual-VAD STT pipeline                          |
| 🔍  | **Semantic retrieval**        | Qwen3-Embedding-0.6B · 1024-dim · composite scoring                      |
| 🌐  | **Private web search**        | Self-hosted SearXNG — no tracking, no API keys, auto-started on boot     |
| 👁️  | **Vision (multi-image)**      | Qwen VL via llama.cpp — offline image understanding                      |
| 🖥️  | **Aurora TUI**                | Textual-based terminal UI — screens, widgets, hotkeys, streaming          |
| 📐  | **Modular prompts**           | Per-module minimal prompts, no monolithic bloat, local-model-first        |
| 🔁  | **JSON-enforced output**      | All LLM outputs are structured, Pydantic-validated, auto-repaired         |
| 📊  | **ACT-R memory scoring**      | Research-grade memory strength: BLA, fan, arousal, PI, TG                |
| 🧹  | **Auto memory pruning**       | Deduplication (≥1 rep always kept), interference, forgotten_log           |
| 🎯  | **Hardware-aware boot**       | Model selector + context budget probe hardware at startup, adapt silently |
| ✂️  | **Smart truncation**          | Middle-cut, proportional trim, history trimming — never over-truncates    |

---

## `$ project --show-layout`

```
buddy/
├── main.py                      # Entry point — creates and runs BuddyApp (Textual)
├── buddy_core/
│   ├── pipeline.py              # handle_turn() — full turn orchestrator
│   ├── boot.py                  # Full boot orchestrator (llama-server, SearXNG, embeddings)
│   ├── llama_installer.py       # Platform-aware llama.cpp binary download
│   ├── searxng_setup.py         # SearXNG clone, configure, start as subprocess
│   ├── model_selector.py        # Hardware-aware model selection (Qwen2.5/3/3.5 catalog)
│   ├── context_budget.py        # Hardware-aware context budgeting (n_ctx, turns, top_k)
│   └── smart_truncator.py       # Middle-cut, proportional trim, history trimming
├── brain/
│   ├── brain.py                 # Brain — builds context, calls LLM, parses result
│   ├── action_router.py         # ActionRouter — CHAT vs ACTION routing, planner, executor
│   ├── prompt_builder.py        # PromptBuilder — formats all LLM inputs
│   └── output_parser.py         # OutputParser — validates and parses strict JSON
├── memory/
│   ├── memory_entry.py          # MemoryEntry dataclass — atomic memory unit
│   ├── memory_manager.py        # MemoryManager — add/search/touch/consolidate
│   ├── sqlite_store.py          # SQLiteStore — source of truth
│   ├── vector_store.py          # VectorStore — Qdrant/local hybrid search + reranking
│   ├── consolidation_engine.py  # Sleep consolidation (v4.1-patched)
│   └── consolidation_engine_v3.py  # REFERENCE ONLY
├── context/
│   └── conversations.py         # RAM conversation buffer with crash-safe snapshotting
├── prompts/
│   ├── brain_prompts.py         # Brain prompt — decision + ingestion schema
│   ├── planner_prompts.py       # Planner prompt — PlannerResult schema
│   ├── executor_prompts.py      # Executor prompt — ExecutorResult schema
│   ├── respond_prompts.py       # Respond prompt — FinalRespond schema
│   ├── terminal_prompts.py      # Terminal tool prompt — destructive gate
│   ├── filesystem_prompts.py    # Filesystem tool prompt
│   ├── web_search_prompts.py    # Web search tool prompt
│   ├── memory_prompts.py        # Memory summary prompts
│   ├── vision_prompts.py        # Vision tool prompt + JSON schema
│   └── base_system_prompts.py   # BUDDY_IDENTITY, BUDDY_BEHAVIOR, BUDDY_MEMORY
├── schema/
│   └── models.py                # All Pydantic models — BrainResult, PlannerResult, etc.
├── llm/
│   ├── llama_client.py          # llama.cpp HTTP client (streaming, JSON extract, interrupts)
│   └── json_repair.py           # JSON repair utility — auto-corrects malformed LLM output
├── embeddings/
│   └── embedding_provider.py    # Qwen3-Embedding-0.6B — singleton, 1024-dim, L2-normalized
├── tools/
│   ├── registry.py              # Tool registry — cached discovery, no hot-reload
│   ├── os/terminal.py           # Terminal execution tool
│   ├── os/filesystem.py         # Filesystem tool v2
│   ├── web/search.py            # Web search tool (SearXNG)
│   └── vision/
│       ├── image_encoder.py     # base64 + data-URI encoder, path extraction
│       └── vision_tool.py       # multi-image vision — calls brain.run_vision()
├── ui/
│   ├── textual_app.py           # ACTIVE — Textual TUI (screens, widgets, input pipeline)
│   ├── boot_ui.py               # Aurora palette constants
│   ├── face_frames.py           # Buddy face animation frames
│   ├── stt.py                   # faster-whisper + Silero VAD
│   └── tts.py                   # Text-to-speech (pyttsx3 / coqui-tts)
├── config/
│   ├── buddy.toml               # All configuration
│   └── tools.toml               # Tool-specific config
└── tests/
    └── *.py                     # pytest — exploratory tests
```

---

## `$ install --quick-start`

```bash
# Clone
git clone https://github.com/YOUR-USERNAME/project-buddy.git
cd project-buddy

# Create environment
mamba create -n buddy python=3.11
mamba activate buddy

# Install dependencies
pip install -r requirements.txt

# Launch
python -m buddy.main
```

> **Requirements:** Python 3.11+, a GGUF model file, ~8GB RAM minimum (16GB recommended for Qwen3-14B-Q4_K_M)

On first boot, Buddy's orchestrator downloads the llama.cpp binary for your platform, sets up SearXNG, selects the best model for your hardware, and loads the embedding model. Subsequent boots are silent and automatic — no configuration required.

---

## `$ run --dev`

```bash
mamba activate buddy

# Run Buddy
python -m buddy.main

# Run tests
pytest buddy/tests/ -v
```

LLM endpoint: `http://127.0.0.1:8080` (auto-started by `boot.py`).

---

## `$ roadmap --show-next`

```
[in progress]  Performance pass — strip dead code, reduce latency, optimize STT
               across the full pipeline (pipeline → brain → prompts → tools → config)

[queued]       Consolidation Engine v5 — closed recall loop, improved clustering
               Resume after performance pass ships.

[planned]      Deeper memory introspection (Buddy explains what it remembers and why)
[planned]      Emotional memory weighting (affect-aware importance scoring)
[planned]      Proactive memory surfacing (Buddy reminds you unprompted)
[planned]      Multi-modal memory input (vision + voice + text as unified memories)
[planned]      Episodic timeline view (visual history of what Buddy knows about you)
[planned]      Advanced reasoning model integration
[planned]      Multi-user memory isolation
[planned]      Mobile companion interface
```

---

## `$ contribute --how`

Contributions, ideas, and technical discussions are welcome.

If you are interested in any of:

- Cognitive architectures and human-like memory systems
- Memory consolidation, retrieval-augmented generation, and semantic search
- Context engineering and minimal prompt design
- Offline / privacy-preserving AI infrastructure
- Local LLM tooling (llama.cpp, Qwen, quantized models)

Open an issue or start a discussion. The codebase is modular by design — individual components can be improved independently.

---

<br/>

<p align="center">
  <sub>
    <strong>BUDDY</strong> · Version 1 of a long-term vision.<br/>
    A private AI that knows you, remembers you, and grows with you over time.<br/><br/>
    <em>Not a chatbot. Not an agent. A cognitive system.</em>
  </sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/made%20with-obsession-000000?style=flat-square&labelColor=000000&color=000000&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0iI2UwNDBmYiIgZD0iTTEyIDIxLjM1bC0xLjQ1LTEuMzJDNS40IDE1LjM2IDIgMTIuMjggMiA4LjUgMiA1LjQyIDQuNDIgMyA3LjUgM2MxLjc0IDAgMy40MS44MSA0LjUgMi4wOUMxMy4wOSAzLjgxIDE0Ljc2IDMgMTYuNSAzIDE5LjU4IDMgMjIgNS40MiAyMiA4LjVjMCAzLjc4LTMuNCA2Ljg2LTguNTUgMTEuNTRMMTIgMjEuMzV6Ii8+PC9zdmc+" alt="made with obsession"/>
</p>
