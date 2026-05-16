# buddy/prompts/compactor_prompts.py
# Used by Brain.run_compact() — one focused LLM call per oversized step output.
# Compresses tool results relative to the user's task before they reach the responder.

COMPACTOR_PROMPT = """
<your_current_job>
§1. YOUR JOB
You have received one tool result and the user's task.
Compress the tool result so the responder can complete the task without losing any useful detail.

§2. RULES
  — Keep EVERY fact, number, name, date, URL, path, and step that helps complete the task
  — Copy exact values — never paraphrase numbers, names, or identifiers
  — Remove noise, redundant text, irrelevant information, status lines, empty lines.
  — Preserve structure if it carries meaning (numbered steps, key-value pairs, tables)
  — If in doubt whether a detail is useful → keep it

§3. OUTPUT
  Return only the compressed content, with important and relevant details preserved.
</your_current_job>
"""
