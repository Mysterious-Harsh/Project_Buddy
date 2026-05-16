PPT_TOOL_PROMPT = """
TOOL_NAME: ppt
TOOL_DESCRIPTION: Create, read, and edit PowerPoint presentations (.pptx). Does not handle .ppt (legacy). For format conversion (pptx → pdf, png, etc.) use the converter tool.

<functions>
  <function>
    <name>create</name>
    <description>Create a new .pptx presentation with one or more slides.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path; must end in .pptx
      - slides (array, REQUIRED) — list of slide objects (see SLIDE SCHEMA below)
      - aspect_ratio (string, OPTIONAL, default: "16:9") — "16:9" | "4:3" | "portrait"
      - theme (string, OPTIONAL, default: "light") — "light" | "dark" | "minimal" | "corporate"
      - confirmed (boolean, OPTIONAL, default: false) — must be true when file already exists
    </parameters>
    <destructive>CONDITIONAL — overwrites existing file</destructive>
    <confirmation_required>YES — when file already exists</confirmation_required>
  </function>

  <function>
    <name>read</name>
    <description>Read presentation structure. Always call before edit to get correct slide_numbers.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .pptx file
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>edit</name>
    <description>Apply an ordered list of operations to an existing presentation. File saved only if ALL ops succeed.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .pptx file
      - theme (string, OPTIONAL) — theme to use for new/updated slides; defaults to "light"
      - operations (array, REQUIRED) — ordered list of op objects:

        add_slide:      { "op": "add_slide",      "slide": {slide_object}, "at": 3 }            — at is optional (appends if omitted)
        update_slide:   { "op": "update_slide",   "slide_number": 2, "slide": {slide_object} }  — clears and re-renders slide
        delete_slide:   { "op": "delete_slide",   "slide_number": 2, "confirmed": true }
        reorder:        { "op": "reorder",         "from": 3, "to": 1 }                         — 1-based numbers
        set_background: { "op": "set_background", "slide_number": 2, "color": "#1a1a2e" }       — use "all" for every slide
        add_element:    { "op": "add_element",    "slide_number": 2, "element": {element} }     — appends element to body zone
    </parameters>
    <destructive>YES — modifies file in place</destructive>
    <confirmation_required>YES — delete_slide requires "confirmed": true inside the op object</confirmation_required>
  </function>

</functions>

SLIDE SCHEMA:
  {
    "layout":        "title_slide" | "title_content" | "two_column" | "blank" | "section_header",
    "title":         "Slide title text",
    "subtitle":      "Subtitle text",           — title_slide only
    "content":       [ {element}, ... ],        — body zone; omit for two_column
    "content_left":  [ {element}, ... ],        — two_column only
    "content_right": [ {element}, ... ],        — two_column only
    "background":    "#1a1a2e",                 — overrides theme background for this slide
    "notes":         "Speaker notes text"
  }

ELEMENT TYPES:
  text:    { "type": "text",    "value": "Body paragraph",                  "style": {style} }
  bullets: { "type": "bullets", "items": ["Point 1", "Point 2"],            "style": {style} }
  image:   { "type": "image",   "path": "/abs/path/img.png",
             "position": "center",        — center | left | right | full | top_left | top_right | bottom_left | bottom_right
             "size":     "large",         — small (25%) | medium (50%) | large (75%) | fill (100%)
             "background": "#000000",     — color behind image (6-char hex)
             "overlay":    "#00000066" }  — color on top of image (8-char hex; last 2 = opacity, 00=invisible ff=opaque)
  table:   { "type": "table",   "headers": ["Col1", "Col2"], "rows": [["A", "B"]], "style": {style} }

STYLE (all fields optional):
  { "size": "small"|"medium"|"large"|"heading", "bold": true, "italic": false, "color": "#ffffff", "align": "left"|"center"|"right" }

  Font sizes: small=14pt  medium=20pt  large=28pt  heading=36pt

THEMES:
  light:     white bg, dark text     dark:      dark navy bg, light text
  minimal:   off-white bg, near-black text      corporate: deep blue bg, white text

<tool_rules>

1. READ BEFORE EDIT
   1.1 Always call read before edit to get current slide_numbers.
   1.2 slide_numbers shift after delete or reorder — re-read before targeting specific slides.
   1.3 slide_number is 1-based and matches SLIDES[] in read output exactly.

2. LAYOUTS
   2.1 title_slide:    title (centered large) + subtitle (centered below). content[] is ignored.
   2.2 title_content:  title (top strip) + content[].
   2.3 two_column:     title (top strip) + content_left[] + content_right[]. Do NOT use content[].
   2.4 blank:          no title strip. content[] fills the whole slide.
   2.5 section_header: title (centered large). content[] is ignored.

3. SLIDE NUMBERS
   3.1 All ops use slide_number (1-based).
   3.2 reorder: from/to are both 1-based. "move slide 3 to position 1" → from: 3, to: 1.
   3.3 add_slide: at is a 1-based insertion position. Omit to append.
   3.4 set_background: slide_number accepts an integer or the string "all".

4. IMAGE RULES
   4.1 path must be absolute and point to an existing file.
   4.2 position "full" ignores size and stretches the image to cover the entire slide.
   4.3 overlay uses 8-char hex (#RRGGBBAA): last two hex digits control opacity.
       Example: "#00000080" = black at ~50% opacity.
   4.4 Supported formats: JPEG, PNG, GIF, BMP. WebP is NOT supported.

5. SAFETY
   5.1 WHAT IS DESTRUCTIVE:
       - create with confirmed=true: overwrites the entire .pptx.
       - delete_slide: permanently removes the slide.
   5.2 THE GATE:
       - create: file exists and confirmed not true → return NEEDS_CONFIRMATION.
       - delete_slide: requires "confirmed": true inside the op object.

6. CHECKLIST
   □ path is absolute and ends in .pptx
   □ Called read before edit to confirm slide_numbers
   □ two_column: using content_left + content_right, not content
   □ image path is absolute and file exists
   □ overlay is 8-char hex; background is 6-char hex

   DESTRUCTIVE GATE:
   □ create: file exists? Set confirmed=true only after user explicitly confirms overwrite
   □ delete_slide: user explicitly requested delete? Add "confirmed": true in the op

</tool_rules>
"""
PPT_TOOL_ERROR_PROMPT = """
<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. FILE_NOT_FOUND      — wrong path or file moved/renamed
   B. SLIDE_NOT_FOUND     — slide_number out of range
   C. IMAGE_NOT_FOUND     — image path wrong or file missing
   D. IMPORT_ERROR        — python-pptx not installed
   E. PERMISSION_DENIED   — file locked or no write access
   F. INVALID_OP          — unknown op name or missing required field

2. RETRY RULES
   2.1 FILE_NOT_FOUND     → verify path. Use fs_browse.find to locate the file if uncertain.
   2.2 SLIDE_NOT_FOUND    → call read to get SLIDE_COUNT, then use a valid slide_number.
   2.3 IMAGE_NOT_FOUND    → confirm the image path is absolute and the file exists.
   2.4 IMPORT_ERROR       → report: "python-pptx is required. Install: pip install python-pptx". Do not retry.
   2.5 PERMISSION_DENIED  → file may be open in PowerPoint. Ask user to close it, then retry.
   2.6 INVALID_OP         → check VALID_OPS in error. Fix op name or add missing required field.

3. RECOVERY CHECKLIST
   □ SAVED: false means file was NOT modified — safe to fix and retry
   □ slide_numbers shift after delete/reorder — re-read before targeting specific slides
   □ Never retry delete_slide without re-confirming with user

   DESTRUCTIVE GATE:
   □ Do NOT retry delete_slide or create(confirmed=true) without re-confirming with user

</error_recovery>"""
