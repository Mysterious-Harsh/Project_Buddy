<p align="center">
  <img src="assets/banner.svg" width="900" alt="Buddy — Offline Cognitive AI Assistant"/>
</p>

<p align="center">
  <a href="#">
    <img src="https://img.shields.io/badge/PYTHON-3.10%2B-000000?style=for-the-badge&logo=python&logoColor=00e5ff&labelColor=000000&color=000000" alt="Python 3.10+"/>
  </a>
  <a href="#">
    <img src="https://img.shields.io/badge/LLM-FULLY%20LOCAL-000000?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0iIzAwZTVmZiIgZD0iTTEyIDJDNi40OCAyIDIgNi40OCAyIDEyczQuNDggMTAgMTAgMTAgMTAtNC40OCAxMC0xMFMxNy41MiAyIDEyIDJ6bTAgMThjLTQuNDIgMC04LTMuNTgtOC04czMuNTgtOCA4LTggOCAzLjU4IDggOC0zLjU4IDgtOCA4eiIvPjwvc3ZnPg==&logoColor=00e5ff&labelColor=000000&color=000000" alt="Local LLM"/>
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
  ▶  MEMORY SUBSYSTEM ONLINE
  ▶  CONSOLIDATION ENGINE READY
  ▶  ACTION MODE ARMED
  ▶  VOICE PIPELINE ONLINE  ·  always listening
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

| Problem       | What everyone else does          | What Buddy does                                       |
| ------------- | -------------------------------- | ----------------------------------------------------- |
| Memory        | Forgets after the context window | Persistent multi-tier memory across all sessions      |
| Quality       | Accumulates junk forever         | Self-consolidates during sleep — denser over time     |
| Privacy       | Cloud APIs, remote inference     | 100% local, air-gap capable                           |
| Understanding | Treats every message as new      | Builds a structured model of who you are              |
| Prompts       | One giant system prompt          | Purpose-built per-module prompts, minimal and precise |

Buddy was built to explore a different direction: **AI as a private, self-maintaining cognitive system that actually knows you.**

---

## `$ architecture --show-pipeline`

```
╔══════════════════════════════════════════════════════════════════╗
║                   BUDDY  ·  COGNITIVE  PIPELINE                  ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║   ┌─────────────┐     ┌──────────────────┐                       ║
║   │  User Input │────▶│  Retrieval Gate  │                       ║
║   └─────────────┘     └────────┬─────────┘                       ║
║                                │                                 ║
║                    ┌───────────▼───────────┐                     ║
║                    │   Memory Search       │                     ║
║                    │  (Vector + SQLite)    │                     ║
║                    └───────────┬───────────┘                     ║
║                                │                                 ║
║                    ┌───────────▼───────────┐                     ║
║                    │   Brain  /  LLM       │                     ║
║                    │  (Reasoning Engine)   │                     ║
║                    └────────┬──────────────┘                     ║
║                             │                                    ║
║              ┌──────────────┼──────────────┐                     ║
║              ▼                             ▼                     ║
║     ┌────────────────┐           ┌──────────────────┐            ║
║     │  ACT  Mode     │           │  Direct Response │            ║
║     │  (if needed)   │           └──────────────────┘            ║
║     └───────┬────────┘                                           ║
║             │                                                    ║
║     ┌───────▼────────┐                                           ║
║     │  Tool Executor │  ← terminal, filesystem, OS               ║
║     └───────┬────────┘                                           ║
║             │                                                    ║
║     ┌───────▼────────┐                                           ║
║     │    Analyzer    │  ← validates output, retries on failure   ║
║     └───────┬────────┘                                           ║
║             │                                                    ║
║     ┌───────▼────────┐                                           ║
║     │  Memory Update │  ← stores what matters, discards noise    ║
║     └────────────────┘                                           ║
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
│ Current turn  │ Recent sessions  │ Persistent knowledge │
│ Immediate ctx │ Recent facts     │ Core identity facts  │
│ Discardable   │ Consolidation ↑  │ Rarely modified      │
│               │ candidate        │                      │
├───────────────┴──────────────────┴──────────────────────┤
│  Storage: SQLite (metadata + text)  +  Qdrant (vectors) │
│  Retrieval: Semantic search  +  Reranking               │
└─────────────────────────────────────────────────────────┘
```

Every memory carries: an importance score, access count, recency weight, and a semantic embedding. Retrieval is context-aware — Buddy pulls what is _relevant_, not just what is _recent_. A fact you mentioned once six months ago surfaces when it matters, and fades when it doesn't.

---

## `$ consolidation --show-sleep-cycle`

When Buddy is idle, a background consolidation engine runs automatically — the same way the human brain consolidates memories during sleep.

```
┌──────────────────────────────────────────────────────┐
│               SLEEP  CONSOLIDATION  CYCLE            │
├──────────────────────────────────────────────────────┤
│                                                      │
│  SCAN      →  Load flash + short-term memories       │
│  CLUSTER   →  Group semantically related entries     │
│  SCORE     →  ACT-R activation + arousal + salience  │
│  SUMMARIZE →  LLM condenses clusters → new entries   │
│  PROMOTE   →  High-strength entries → long-term      │
│  DEMOTE    →  Low-strength entries → flash / deleted │
│  PRUNE     →  Duplicates, dead traces, interference  │
│                                                      │
│  RESULT: Smaller, denser, higher-quality memory      │
│                                                      │
└──────────────────────────────────────────────────────┘
```

**When you wake Buddy up** — the consolidation cancels immediately and Buddy responds without delay. No waiting for a background job to finish.

The engine is research-grounded:

- **ACT-R activation** — importance slows memory decay (Anderson & Lebiere 1998)
- **CLS per-tier decay** — flash/short/long have biologically distinct decay rates
- **Arousal amplification** — emotionally significant memories persist longer
- **Prediction error boost** — surprising/contradictory memories get higher salience
- **Reflective consolidation** — low-confidence summaries don't destroy originals
- **Interference pruning** — near-duplicate memories that compete in retrieval are resolved

---

## `$ act --show-mode`

ACT mode is a capability layered on top of the memory system. When a task requires it, Buddy can execute real operations on your machine — but this is not what Buddy _is_. Buddy is a cognitive memory system first. ACT mode is a tool it can pick up when needed.

```python
# When ACT mode is triggered
[PLAN]     → Break task into atomic steps
[EXECUTE]  → Run terminal commands / OS operations
[ANALYZE]  → Validate output against expected result
[RETRY]    → Adjust and rerun on failure
[STORE]    → Record result and what was learned into memory
```

**Safety constraints built in:**

- Directory and path validation before execution
- Non-destructive operation rules by default
- Structured tool output logging at every step
- Error stack tracking with full context

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
│                    SPEECH  CONFIRMED  →  🔔  beep                │
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

## `$ features --list`

|     | Feature                    | Description                                             |
| --- | -------------------------- | ------------------------------------------------------- |
| 🧠  | **Multi-tier memory**      | Flash → Short → Long, persisted across sessions         |
| 🌙  | **Sleep consolidation**    | Auto-runs on idle, cancels instantly on wake            |
| ⚡️  | **ACT mode**               | Real OS-level action execution with retry logic         |
| 🔒  | **Fully offline**          | Local LLM via llama.cpp — zero cloud calls              |
| 🎤  | **Always-listening voice** | No wake word — continuous dual-VAD STT pipeline         |
| 🔍  | **Semantic retrieval**     | Vector embeddings + reranking for memory search         |
| 📐  | **Modular prompts**        | Per-module minimal prompts, no monolithic bloat         |
| 🔁  | **JSON-enforced output**   | All LLM outputs are structured and validated            |
| 📊  | **ACT-R memory scoring**   | Research-grade memory strength calculation              |
| 🧹  | **Auto memory pruning**    | Deduplication, interference removal, dead trace cleanup |
| 🛡️  | **Integrity checks**       | Boot-time prompt hash verification                      |
| 🖥️  | **Terminal UI**            | Aurora-themed CLI with voice, hotkeys, status toolbar   |

---

## `$ install --quick-start`

```bash
# Clone
git clone https://github.com/YOUR-USERNAME/YOUR-REPO.git
cd YOUR-REPO

# Install dependencies
pip install -r requirements.txt

# Configure (copy and edit the template)
cp config/buddy.example.toml ~/.buddy/config/buddy.toml

# Launch
python -m buddy.main
```

> **Requirements:** Python 3.10+, a GGUF model file, ~8GB RAM minimum (depends on model)

On first boot, Buddy runs an interactive setup to select your LLM model, download embeddings, and configure your profile. Subsequent boots are silent and automatic.

---

## `$ roadmap --show-next`

```
[ ]  Deeper memory introspection (Buddy explains what it remembers and why)
[ ]  Emotional memory weighting (affect-aware importance scoring)
[ ]  Proactive memory surfacing (Buddy reminds you of relevant things unprompted)
[ ]  Multi-modal memory input (vision + voice + text stored as unified memories)
[ ]  Episodic timeline view (visual history of what Buddy knows about you)
[ ]  Advanced reasoning model integration
[ ]  Multi-user memory isolation
[ ]  Mobile companion interface
```

---

## `$ contribute --how`

Contributions, ideas, and technical discussions are welcome.

If you are interested in any of:

- Cognitive architectures and human-like memory systems
- Memory consolidation, retrieval-augmented generation, and semantic search
- Context engineering and minimal prompt design
- Offline / privacy-preserving AI infrastructure

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
