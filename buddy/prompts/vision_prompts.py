# buddy/prompts/vision_prompts.py
#
# Vision tool prompts — image analysis schema for the LLM.
# Follows local-model-first rule: schema first, minimal prose, flat JSON.

# ---------------------------------------------------------------------------
# System prompt injected when brain.run_vision() is called.
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
# JSON schema shown as the expected output shape.
# ---------------------------------------------------------------------------
VISION_SCHEMA = """{
  "description": "Dense paragraph: subject, setting, colors, layout, key relationships. For multiple images: 'Image 1: ... Image 2: ... Overall: ...'",
  "objects": ["most important visible item", "second item", "...up to 20 items"],
  "text_found": "All readable text copied verbatim with format, tags, and markup preserved. Empty string if no text is visible.",
  "key_finding": "Specific, direct answer to the user's query. This is the primary output."
}"""

# ---------------------------------------------------------------------------
# Tool prompt shown to the executor when it selects this tool.
# ---------------------------------------------------------------------------
VISION_TOOL_PROMPT = """
<tool_description>
VISION TOOL

Two actions:
  screenshot — capture the current screen and reason over it (no path needed)
  analyze    — analyze one or more image files provided by the user

Supports PNG, JPG, JPEG, WEBP, GIF, BMP. Single or multiple images.
</tool_description>

<when_to_use>
§1. WHEN TO USE
action="screenshot":
  - User asks "what's on my screen?", "look at my screen", "take a screenshot"
  - User wants Buddy to observe the current state of the display

action="analyze" (default):
  - User provides an image path and asks what is in it
  - User wants a photo, diagram, or chart analyzed
  - User wants text extracted from an image (OCR)
  - User wants to compare two or more images

DO NOT use if:
  - No image path provided and user did NOT ask for a screenshot
  - The file is not an image (use filesystem tool for documents, code, etc.)
</when_to_use>

<call_schema>
§2. CALL SCHEMA
  action    : "screenshot" | "analyze" (default: "analyze")
  paths     : list of absolute paths — required for action="analyze", omit for screenshot
  query     : required — what to find, answer, or reason about
  save_path : optional — absolute path to save the screenshot PNG (screenshot only)
</call_schema>

<result_fields>
§3. RESULT FIELDS (returned as text to responder)
  DESCRIPTION  — full image description paragraph
  OBJECTS      — list of key visible items/elements
  TEXT_FOUND   — verbatim text visible in image (empty string if none)
  KEY_FINDING  — direct answer to the query — the primary output
  PATHS        — image path(s) analyzed (empty for screenshot)
</result_fields>
""".strip()

VISION_TOOL_CALL_FORMAT = (
    'screenshot:       {"action": "screenshot", "query": "what is on the'
    ' screen?"}\nscreenshot+save:  {"action": "screenshot", "save_path":'
    ' "/absolute/path/out.png", "query": "what is on the screen?"}\nanalyze:         '
    ' {"action": "analyze", "paths": ["/absolute/path/a.png"], "query": "what is in'
    ' this image?"}'
)
