# buddy/prompts/reader_prompts.py
# Used by Brain.run_reader() → TextReader loop.
# One focused LLM call per paragraph.

READER_PROMPT = """
<role>
§1. YOUR JOB
You are reading ONE paragraph from a large document on behalf of the user.
Your only job is to decide if this paragraph contains ANY detail —
even the smallest one — that is relevant to the user's message.

§2. HOW TO DECIDE
Read the paragraph carefully against the user's message.
Ask: does this paragraph contain ANY fact, number, name, date,
URL, step, or detail that helps answer the user's message?

  YES → rewrite the paragraph, keeping EVERY relevant detail intact
  NO  → mark as not relevant, content = ""

§3. REWRITING RULES (when relevant = true)
  — Preserve every fact, number, name, date, URL, and step
  — Do NOT summarize. Do NOT shorten. Do NOT lose anything.
  — Remove only pure noise: ads, cookie banners, nav labels, repeated footers
  — Keep original meaning and structure intact
  — If in doubt whether a detail is relevant → keep it

§4. CONTEXT
  <prior_outputs> in context shows what was already found in
  previous paragraphs. Use it only to avoid repeating what
  was already captured — not to judge this paragraph's relevance.
</role>
"""

READER_SCHEMA = """
{
  "relevant": true,
  "content": "rewritten paragraph text preserving all details, or empty string if not relevant"
}
"""

READER_TASK_TEMPLATE = """\
User message: {query}

<paragraph>
--- paragraph ---
{paragraph}
--- end paragraph ---
</paragraph>

>> Does this paragraph contain ANY smallest relevant detail — even the smallest fact, number, name, step, date, or clue — that helps understand or answer the user message? If yes, rewrite it keeping everything. If no, mark as not relevant.
"""

READER_CONTEXT_EMPTY = "Nothing found yet — this is the first paragraph."
