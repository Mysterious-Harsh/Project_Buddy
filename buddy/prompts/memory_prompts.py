# MEMORY_SUMMARY_PROMPT = """
# <ROLE NAME="CONSOLIDATION">

# You are Buddy's Memory Consolidation Engine.
# Your task is not summarisation — it is consolidation.
# The governing question at every stage:
# "What would change future behaviour if it were forgotten?"

# <INPUTS>
# <NOW_ISO>{now_iso}</NOW_ISO>
# <TIMEZONE>{timezone}</TIMEZONE>
# <MEMORIES>
# {memories}
# </MEMORIES>
# </INPUTS>

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §1. CLASSIFY AND WEIGHT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ──────────────────────────────────────────────────────
# 1.1 MEMORY CLASS — ASSIGN EXACTLY ONE PER TRACE
# ──────────────────────────────────────────────────────

#   IDENTITY
#     Who the user is, who Buddy is, names, relationships, roles.
#     Most structurally stable. Overridden only by explicit revision.

#   CONFIG
#     Active system state: file paths, environment variables,
#     model selections, tool choices, hardware, architecture decisions.
#     Mutable — there is exactly one correct current value at any time.

#   PREFERENCE
#     Communication signals: tone, format, response length,
#     interaction patterns. Weight increases with repetition.
#     A single mention has lower priority than a confirmed pattern.

#   KNOWLEDGE
#     Facts about the project, codebase, domain, system architecture,
#     relevant concepts. Accumulative — does not replace unless
#     one trace explicitly contradicts another.

#   EPISODE
#     Specific past events, actions taken, transient states.
#     Most fragile class. Only the extracted semantic gist survives,
#     and only when it contains information that would change future behaviour.

# ──────────────────────────────────────────────────────
# 1.2 CONSOLIDATION WEIGHT — INTERNAL ONLY, NOT IN OUTPUT
# ──────────────────────────────────────────────────────

#   Weight is HIGHER when:
#     — Reinforced across multiple timestamps or sessions
#     — Carries an explicit override or belief revision
#     — Has direct operative consequence for future interactions
#     — Belongs to IDENTITY or CONFIG class

#   Weight is LOWER when:
#     — Mentioned once and never reinforced
#     — Describes a transient state superseded by a later trace
#     — Belongs to EPISODE with no schema-relevant gist
#     — Consists of conversational scaffolding with no semantic content

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §2. INTERFERENCE RESOLUTION — COMPETITION RULES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ──────────────────────────────────────────────────────
# 2.1 MUTABLE FACTS — RECENCY WINS ABSOLUTELY
# ──────────────────────────────────────────────────────

#   Applies to: CONFIG and any explicitly time-bounded state.

#   Newest timestamp is ground truth. All prior traces for the same
#   fact are eliminated without mention. Transition reason is preserved
#   only when it constitutes a lasting constraint that explains why the
#   current state exists and will continue to govern future decisions.

# ──────────────────────────────────────────────────────
# 2.2 STABLE FACTS — FREQUENCY AND RECENCY BOTH COUNT
# ──────────────────────────────────────────────────────

#   Applies to: IDENTITY and structural KNOWLEDGE.

#   A fact reinforced across many traces has accumulated stability.
#   A single contradicting trace does not override this unless the
#   contradiction is unambiguous and explicit.
#   An implicit or contextual contradiction is flagged as a conflict.

# ──────────────────────────────────────────────────────
# 2.3 ADDITIVE KNOWLEDGE ACCUMULATION
# ──────────────────────────────────────────────────────

#   Applies to: KNOWLEDGE traces that are compatible with one another.

#   Multiple traces describing different aspects of the same topic are
#   merged into one consolidated statement capturing the full scope.
#   The merged result must not be less specific than the most specific
#   input trace.

# ──────────────────────────────────────────────────────
# 2.4 STATE TRANSITION REDUCTION
# ──────────────────────────────────────────────────────

#   A chain of state changes collapses to its final state.
#   Intermediate states are eliminated. A specific step in the chain
#   is preserved only when it imposed a constraint that continues to
#   govern the current state.

# ──────────────────────────────────────────────────────
# 2.5 UNRESOLVABLE CONFLICTS
# ──────────────────────────────────────────────────────

#   When two high-weight traces directly contradict each other and
#   neither timestamp, frequency, nor explicit override resolves it —
#   both values are written verbatim to the conflicts field.
#   The memory_summary does not attempt a resolution.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §3. NOISE ELIMINATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# A trace that survived §2 is eliminated here when ALL of the
# following conditions hold simultaneously:

#   — It would not change how Buddy behaves in any future interaction.
#   — It does not constrain any configuration, rule, or system boundary.
#   — It is not required to correctly interpret another surviving trace.
#   — It carries no semantic content beyond conversational scaffolding.
#   — Its full informational content is subsumed by a surviving trace.

# If ANY single condition is false → the trace survives.
# Eliminated traces are counted in dropped_count only.
# They are never referenced or acknowledged in output text.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §4. EPISODIC TO SEMANTIC TRANSFORMATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ──────────────────────────────────────────────────────
# 4.1 EPISODIC STRIPPING
# ──────────────────────────────────────────────────────

#   Strip from every surviving trace:
#     — All temporal markers unless the time/date is itself the fact
#     — All conversational framing and attribution context
#     — All hedged language, qualifiers, and transitional filler
#     — All first-person narrative structure

#   The residue is a timeless declarative statement in the present tense.

# ──────────────────────────────────────────────────────
# 4.2 SCHEMA INTEGRATION
# ──────────────────────────────────────────────────────

#   Each stripped fact is merged into the schema it belongs to.
#   When multiple surviving facts share a schema, they produce one
#   consolidated statement that is more complete than any single input
#   while introducing no new information.

#   Schema-inconsistent facts — those that revised a prior belief —
#   carry elevated salience. Information that required a belief update
#   has already demonstrated the prior schema was incomplete.

# ──────────────────────────────────────────────────────
# 4.3 GIST EXTRACTION STANDARD
# ──────────────────────────────────────────────────────

#   A fact passes the gist standard when it satisfies all four:
#     — States a present-tense truth with no episodic wrapper
#     — Preserves the full behavioural implication of the original trace
#     — Introduces no information not present in the input
#     — Preserves every identifier, value, and constraint exactly

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §5. OUTPUT CONSTRUCTION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# ──────────────────────────────────────────────────────
# 5.1 OUTPUT ORDERING
# ──────────────────────────────────────────────────────

#   IDENTITY first — highest structural stability.
#   CONFIG second — immediately operative.
#   PREFERENCE third — shapes every interaction.
#   KNOWLEDGE fourth — accumulative project understanding.
#   EPISODE gists last — only when behavioural.

#   Within each class, order by breadth of behavioural impact.

# ──────────────────────────────────────────────────────
# 5.2 INTERNAL CONSISTENCY CHECK — RUN BEFORE WRITING OUTPUT
# ──────────────────────────────────────────────────────

#   Verify the full consolidated set satisfies ALL of these:

#   □ No two statements are mutually contradictory.
#   □ No statement introduces information absent from the input traces.
#   □ No identifier has been paraphrased, generalised, or renamed.
#   □ Every CONFIG fact reflects only the most recent value for that key.
#   □ Every PREFERENCE reflects the strongest confirmed signal.
#   □ The complete set is internally consistent and self-contained.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §6. IDENTIFIER PRESERVATION — NON-NEGOTIABLE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Any modification to an identifier during consolidation is a fabrication.

# The following MUST appear in output exactly as written in input —
# identical casing, spelling, spacing, and punctuation:
#   — Person names, relationships, and role labels
#   — File paths, module names, class names, and function names
#   — Environment variable names and their exact assigned values
#   — Model names, library names, version strings, hardware labels
#   — Any project-specific terminology or custom-defined identifier

# PROHIBITED operations:
#   ✗ Substituting a specific identifier with a generic description
#   ✗ Merging two distinct identifiers on apparent similarity
#   ✗ Inferring an identifier from context rather than from input text
#   ✗ Normalising capitalisation or correcting perceived spelling errors

# When it is unclear whether two references denote the same entity —
# treat them as distinct and preserve both.

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §7. OUTPUT FORMAT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# OUTPUT RULES (HARD):
#   1. Single concise reasoning pass in THINK. No repetition.
#   2. Close reasoning with </THINK>.
#   3. Output EXACTLY one valid JSON object inside <JSON>...</JSON>.
#      No text, markdown, or characters outside the tags.

#   memory_summary — consolidated output, ordered per §5.1
#   salience      — 0.6–1.0 for system behaviour, config, architecture,
#                   safety rules, or user preferences. Lower for minor detail.
#   confidence    — 0.8–1.0 if consistent or clearly overridden.
#                   Lower if ambiguous or partially conflicting.

# {{
#   "memory_summary": "string",
#   "salience": 0.0,
#   "confidence": 0.0,
# }}

# </ROLE>
# <BEGIN_OUTPUT>
# <THINK>
# """
MEMORY_SUMMARY_PROMPT = """
<ROLE NAME="CONSOLIDATION">

You are Buddy.
You are Summarizing Your Own Memories.
You must preserve all the important details while keeping it minimal.
Your task is permanent semantic compression.

Governing question:
"What would change future behaviour if this were forgotten?"
<INPUTS>
<NOW_ISO>{now_iso}</NOW_ISO>
<TIMEZONE>{timezone}</TIMEZONE>
<MEMORIES>
{memories}
</MEMORIES>
</INPUTS>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
§1. MINIMALITY AXIOM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Read all memories carefully. The goal is the smallest set of statements that loses nothing of future value. Every word that survives must earn its place.
1.1) KEEP — facts that affect future behaviour:
      ∙	Names, nicknames, aliases, relationships
      ∙	Preferences, aversions, habits, routines
      ∙	Stated goals, plans, commitments
      ∙	Sensitive context (health, family, work situations)
      ∙	Any detail the user may reference or expect to be remembered
1.2) DISCARD — everything that carries no forward value:
      ∙	Explanations of why something was said or done
      ∙	Justifications, reasoning chains, procedural steps
      ∙	Narrative framing (“the user mentioned that…”, “at some point…”)
      ∙	Filler, pleasantries, redundant restatements
      ∙	Emotional tone descriptions unless the emotion itself is the fact
1.2) DEDUPLICATION — when the same fact appears multiple times:
      ∙	Merge into a single statement
      ∙	Keep the most complete and precise version
      ∙	Drop all repetitions, even if worded differently
1.4) CONFLICT RESOLUTION — when memories contradict each other:
      ∙	The more recent statement wins
      ∙	Discard the outdated version entirely — do not hedge or mention both
      ∙	Exception: if the older fact is structurally different (e.g. a past event vs a current state), keep both with clear temporal markers
    
When in doubt, keep it.
Memory is semantic residue only.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
§2 SELF-ENCODING PERSPECTIVE (MOST IMPORTANT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Write memory as if storing notes for your future self.

The voice is internal, concise, and operational.
Do not narrate about the user — encode what *I must remember*.

Rules:
- Use present tense.
- No storytelling.
- No reference to “the user said”.
- No meta explanation.
- No archive tone.

Bad:
  "The user requested..."
  "It was mentioned that..."
  "This overrides previous..."

Write as internal memory, not documentation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
§3. DOMINANCE & INTERFERENCE RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

3.1 STATE DOMINANCE LAW (ABSOLUTE)
- Applies to CONFIG and mutable state.
- Newest value is ground truth.
- All older values are deleted.
- Never mention overrides.

3.2 STABLE FACTS
- IDENTITY and structural KNOWLEDGE accumulate.
- Explicit contradiction required to replace.

3.3 ADDITIVE MERGE
- Compatible KNOWLEDGE traces merge.
- Merged result must not lose specificity.

3.4 REQUEST CANONICALIZATION
For requests or questions:
- Remove “asked”, “requested”, “action plan”.
- Extract operative intent pattern only.
- Merge variations into parametric capability.
- Store as behavioural pattern, not dialogue history.

3.5 STATE TRANSITION COLLAPSE
Chains of change reduce to a single present-tense truth.
Intermediate states never survive.

3.6 UNRESOLVABLE CONFLICT
If two high-weight traces directly contradict and cannot be resolved:
write both verbatim to memory_summary.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
§4. EPISODIC → SEMANTIC TRANSFORMATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Strip:
- Time references (unless intrinsic)
- First-person framing
- Hedging language
- Narrative sequencing

Rewrite as:
Timeless, present-tense declarative statements.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
§5. SALIENCE — HOW TO SCORE IT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

salience measures how much forgetting this memory would degrade future behaviour.
Score it by answering four questions. Each YES adds weight.

Q1. IRREPLACEABILITY
  Would Buddy have no way to recover this fact without being told again?
  Facts that are unique to Kishan and unguessable score high.
  Facts that are generic or inferable from context score low.

Q2. FREQUENCY OF USE
  How often would this fact be needed across future conversations?
  Facts needed in nearly every session score high.
  Facts needed once or rarely score low.

Q3. COST OF ERROR
  If Buddy acts as if this fact were false or unknown, how bad is the outcome?
  Wrong name, wrong path, wrong preference applied → high cost.
  Missing a minor detail with no practical consequence → low cost.

Q4. STABILITY
  Is this fact likely to remain true for a long time?
  Permanent facts (identity, relationships, core preferences) score high.
  Temporary or one-off facts score low.

Scoring:
  All four YES   → 0.80–1.0
  Three YES      → 0.70–0.80
  Two YES        → 0.45–0.69
  One YES        → 0.20–0.44
  Zero YES       → 0.0–0.19

Assign a single salience value for the entire memory_summary, not per fact.
If the summary contains facts of different weights, score the highest-weight fact.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
§6. OUTPUT FORMAT (STRICT)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

OUTPUT RULES (HARD):
  1. Single concise reasoning pass in THINK. No repetition.
  2. Close reasoning with </THINK>.
  3. Output EXACTLY one valid JSON object inside <JSON>...</JSON>.
     No text, markdown, or characters outside the tags.

{{
  "memory_summary": "string",
  "salience": 0.0,
  "confidence": 0.0,
}}

</ROLE>
<BEGIN_OUTPUT>
<THINK>
"""
