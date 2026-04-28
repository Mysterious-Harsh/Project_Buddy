VISION_TOOL_PROMPT = """
TOOL_NAME: vision
TOOL_DESCRIPTION: Analyze image files or capture the current screen. Returns description, objects, visible text, and a direct answer.

<functions>
  <function>
    <name>vision</name>
    <description>Analyze one or more images, or capture and analyze the screen.</description>
    <parameters>
      - action    (string,  OPTIONAL, default: "analyze") — "analyze" | "screenshot"
      - paths     (array,   OPTIONAL) — absolute paths to image files; required when action="analyze"
      - query     (string,  REQUIRED) — what to find, answer, or reason about
      - save_path (string,  OPTIONAL) — absolute path to save the screenshot PNG; action="screenshot" only
    </parameters>
    <returns>OK, ACTION, PATHS, DESCRIPTION, OBJECTS, TEXT_FOUND, KEY_FINDING, [SAVED_PATH], ERROR</returns>
    <destructive>YES — if save_path is provided, writes a PNG file to disk</destructive>
    <confirmation_required>YES — if save_path is provided and the file already exists</confirmation_required>
  </function>
</functions>

<tool_rules>

1. ACTION SELECTION
   1.1 action="screenshot" — user asks to look at the screen, "what's on my screen", "take a screenshot".
       No paths needed. save_path is optional.
   1.2 action="analyze" (default) — user provides image path(s) and wants analysis, description, OCR, or comparison.
       paths is required.
   1.3 Do NOT use if:
       - No image path provided and user did NOT ask for a screenshot.
       - File is not an image (use filesystem tool for documents, code, etc.).

2. PATHS
   2.1 All paths must be absolute. Resolve ~ before passing.
   2.2 Supported formats: PNG, JPG, JPEG, WEBP, GIF, BMP, TIFF, TIF.
   2.3 Multiple images → pass all paths in the array. Compare or combine as the query demands.

3. QUERY
   3.1 Write a specific, direct question or instruction — not a vague "describe this".
   3.2 For text extraction: "extract all visible text verbatim".
   3.3 For comparison: "compare Image 1 and Image 2 — list differences".

</tool_rules>

<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. PATH NOT FOUND — image file does not exist at the given path.
      Verify the path from <prior_step_outputs>. If still missing → status="followup". Report exact path.

   B. UNSUPPORTED FORMAT — file extension not in supported set.
      status="followup". State the file type and ask the user to convert it.

   C. SCREENSHOT FAILED — screen capture dependency missing or permission denied.
      status="followup". Report the exact error. Do not retry silently.

   D. VISION MODEL NOT AVAILABLE — brain returned an error about model capability.
      status="followup". Report exactly: vision requires a Qwen VL model.

   E. UNCLASSIFIED — Return status="followup" with the exact ERROR value and one specific question.

2. RETRY RULES
   2.1 Never retry a path that does not exist — it will not appear on retry.
   2.2 After 2 failures → status="followup".

</error_recovery>
"""

# ---------------------------------------------------------------------------
# System prompt injected when brain.run_vision() is called.
# Not shown to the executor — used for the actual vision LLM call.
# ---------------------------------------------------------------------------
VISION_PROMPT = """
VISION ANALYSIS

Examine the image(s) carefully in very in details. Answer the user's query using only what you can see.

RULES
1. Return valid JSON only — no markdown fences, no prose outside the JSON object.
2. All four fields are required. Use "" or [] when a field has no content.
3. Never invent or guess — only report what is clearly visible.
4. key_finding: most important field — give a specific, direct answer to the query.
5. description: one dense end to end detailed paragraph — cover subject, setting, colors, layout, relationships.
6. objects: list the all meaningful visible items/elements, most important first.
7. text_found: copy ALL the visible and readable text verbatim (signs, labels, UI text, code, captions) and must try to keep format, tags, markup exactly. "" if none.

MULTI-IMAGE
If multiple images are provided:
  - key_finding: give a comparative or combined answer as the query demands.
  - description: describe each image briefly (Image 1: ..., Image 2: ...) then summarize.
  - objects / text_found: merge across all images; note which image if ambiguous.
""".strip()

# ---------------------------------------------------------------------------
# JSON schema shown as the expected output shape for run_vision().
# ---------------------------------------------------------------------------
VISION_SCHEMA = """{
  "description": "Dense paragraph: subject, setting, colors, layout, key relationships. For multiple images: 'Image 1: ... Image 2: ... Overall: ...'",
  "objects": ["most important visible item", "second item", "...up to 20 items"],
  "text_found": "All readable text copied verbatim with format, tags, and markup preserved. Empty string if no text is visible.",
  "key_finding": "Specific, direct answer to the user's query. This is the primary output."
}"""
