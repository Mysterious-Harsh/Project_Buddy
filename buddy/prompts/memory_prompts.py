MEMORY_SUMMARY_PROMPT = """
<task>
§1. TASK
Consolidate the memories below into a single minimal summary.
Output strict JSON only.
</task>

<input_format>
§2. INPUT FORMAT
Each memory line has four fields separated by " | ":

  TIMESTAMP | TIER | imp=IMPORTANCE | TEXT

  TIMESTAMP  — ISO-8601 creation time. Use this to determine which fact is newer.
  TIER       — flash (hours) | short (days) | long (permanent). Higher tier = higher authority.
  IMPORTANCE — 0.00–1.00. Higher value = more protected from removal.
  TEXT       — the actual memory content.

These four fields are metadata for your reasoning. Do NOT copy them into the output.
</input_format>

<conflict_resolution>
§3. CONFLICT RESOLUTION
When two memories say different things about the same fact, resolve in this order:

  1. MUTABLE STATE (preferences, settings, current status, plans):
       Newer TIMESTAMP wins. Discard the older version entirely. No hedging.

  2. TIMESTAMP TIE:
       Higher TIER wins: long > short > flash.

  3. PAST EVENTS vs CURRENT STATE:
       Keep both with clear temporal markers — "previously X, now Y".

  4. IMPORTANCE GUARD:
       A memory with imp >= 0.80 must survive unless directly contradicted
       by a newer entry of equal or higher tier. Never silently discard it.

  5. UNRESOLVABLE CONFLICT:
       Write both verbatim, separated by a semicolon.
</conflict_resolution>

<rules>
§4. RULES

KEEP — facts that change future behavior:
  Names, relationships, preferences, aversions, habits, routines
  Stated goals, plans, commitments
  Sensitive context (health, family, work situations)
  Anything the user may expect to be recalled

DISCARD — facts with no forward value:
  Reasoning chains, justifications, explanations of why something was said
  Narrative framing ("user mentioned", "at some point", "it was noted that")
  Redundant restatements of the same fact
  Pleasantries, filler

MERGE — when the same fact appears multiple times:
  Write one statement — the most complete version.

COLLAPSE — chains of state transitions:
  Reduce to a single present-tense truth. Intermediate states never survive.
</rules>

<encoding_perspective>
§5. ENCODING PERSPECTIVE
Write memory as if storing notes for your future self.
The voice is internal, concise, and operational.
Do not narrate about the user — encode what *I must remember*.

Rules:
- Use present tense.
- No storytelling.
- No reference to "the user said".
- No meta explanation.
- No archive tone.

Write as internal memory, not documentation.
Write what is true about the subject — not what was said or exchanged in this turn.
</encoding_perspective>

<dominance_rules>
§6. DOMINANCE RULES

  6.1 STATE DOMINANCE LAW (ABSOLUTE)
  - Applies to CONFIG and mutable state.
  - Newest value is ground truth.
  - All older values are deleted.
  - Never mention overrides.

  6.2 STABLE FACTS
  - IDENTITY and structural KNOWLEDGE accumulate.
  - Explicit contradiction required to replace.

  6.3 ADDITIVE MERGE
  - Compatible KNOWLEDGE traces merge.
  - Merged result must not lose specificity.

  6.4 REQUEST CANONICALIZATION
  For requests or questions:
  - Remove "asked", "requested", "action plan".
  - Extract operative intent pattern only.
  - Merge variations into parametric capability.
  - Store as behavioural pattern, not dialogue history.

  6.5 STATE TRANSITION COLLAPSE
  Chains of change reduce to a single present-tense truth.
  Intermediate states never survive.

  6.6 UNRESOLVABLE CONFLICT
  If two high-weight traces directly contradict and cannot be resolved:
  write both verbatim to memory_summary.
</dominance_rules>

<episodic_transform>
§7. EPISODIC TRANSFORM
Strip:
- RELATIVE time references (today, yesterday, last week, next month, etc.) — these rot after storage
- First-person framing
- Hedging language
- Narrative sequencing

KEEP:
- Absolute dates (YYYY-MM-DD or "April 2026") — intrinsic, irreplaceable, must survive consolidation

Rewrite as:
Timeless, present-tense declarative statements. Where a date is factually significant, keep it.
</episodic_transform>

<salience_guide>
§8. SALIENCE GUIDE
salience [0,1] — how strongly this consolidated memory should influence future responses.
  0.70–1.00 → LONG tier  — stable, permanent, guides behavior always
  0.30–0.69 → SHORT tier — relevant for days to weeks
  0.00–0.29 → FLASH tier — ephemeral, discard soon

confidence [0,1] — how certain you are the consolidation is accurate and complete.
  Low confidence → compress less, keep more, err toward preserving.
</salience_guide>
"""

MEMORY_SUMMARY_PROMPT_SCHEMA = """
{{
  "memory_summary": "string",
  "salience": 0.0,
  "confidence": 0.0
}}
"""
