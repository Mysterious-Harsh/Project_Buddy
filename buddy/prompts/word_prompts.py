WORD_TOOL_PROMPT = """
TOOL_NAME: word
TOOL_DESCRIPTION: Create, read, and edit Word (.docx) documents using HTML+CSS as the authoring format. Does not handle .doc (legacy). For format conversion (docx → pdf, html, etc.) use the converter tool.

<functions>
  <function>
    <name>create</name>
    <description>Create a new .docx from an HTML+CSS string.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path; must end in .docx
      - content (string, REQUIRED) — full HTML+CSS document string
      - confirmed (boolean, OPTIONAL, default: false) — must be true when file already exists
    </parameters>
    <destructive>CONDITIONAL — overwrites existing file</destructive>
    <confirmation_required>YES — when file already exists</confirmation_required>
  </function>

  <function>
    <name>read</name>
    <description>Extract the content from the .docx and return an indexed HTML summary. Use search to retrieve only relevant sections.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .docx file
      - search (string, OPTIONAL) — if provided, return only HTML fragments whose text contains this string
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>edit</name>
    <description>Perform native in-place edits on a .docx document. Modifies the file without destroying images, layouts, or headers.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .docx file
      - edits (array, REQUIRED) — list of edit op objects:

        replace:        { "op": "replace", "section_id": "p3", "new": "Updated text with <b>bold</b> and <img src='/path.png'>" }
        add_after:      { "op": "add_after", "section_id": "t1", "new": "Paragraph after table", "style": "Heading 1" }
        add_before:     { "op": "add_before", "section_id": "p2", "new": "Paragraph before p2" }
        add_end:        { "op": "add_end", "new": "Conclusion." }
        remove:         { "op": "remove", "section_id": "p4" }
        add_page_break: { "op": "add_page_break", "section_id": "p3" }
        set_page_setup: { "op": "set_page_setup", "margin": "narrow", "orientation": "landscape" }
    </parameters>
    <destructive>YES — modifies file in place</destructive>
    <confirmation_required>NO — edits are non-destructive to the rest of the document layout</confirmation_required>
  </function>

</functions>

<tool_rules>

1. THE HTML INTERCEPTOR
   1.1 You do NOT edit HTML directly. You send edit operations to the backend, which parses them and natively updates the Word document.
   1.2 When you call read, the tool shows you an indexed HTML preview (e.g. id="p3" for paragraph 3, id="t0" for table 0).
   1.3 You MUST use these exact IDs (p3, t0) as the section_id in your edits.
   1.4 Because this edits the native Word XML, all existing images, headers, footers, and margins in the document are perfectly preserved.

2. AUTHORING FORMAT & PAGE CONFIGURATION
   2.1 content in create must be a valid HTML string — inline <style> or a <style> block in <head>.
   2.2 Supported elements: h1–h6, p, table/tr/th/td, ul/ol/li, strong, em, u, br, hr, img.
   2.3 You control Word page setup ENTIRELY via CSS. No extra parameters are needed!
       - Page Size & Orientation: @page { size: A4 landscape; } or @page { size: letter; }
       - Margins: @page { margin: 1in; }
       - Default Fonts: body { font-family: "Calibri", sans-serif; font-size: 11pt; }
   2.4 CSS styling is best-effort — Word ignores complex CSS. Structure (headings, tables, lists) transfers reliably. For text styling, use inline styles or simple classes (color, font-weight).

3. EDIT OPS
   3.1 replace: Provide the section_id and the new text. You may use <b>, <i>, <u>, and <img> inside the "new" text string. Do NOT write surrounding <p> tags in the "new" field.
   3.2 add_after / add_before: Insert a new paragraph adjacent to the target section_id. You can provide a "style" string (e.g. "Heading 1", "Normal").
   3.3 add_end: Appends a paragraph to the end of the document.
   3.4 remove: Deletes the paragraph or table entirely.
   3.5 set_page_setup: Changes document margins ("narrow", "normal", "wide") or orientation ("portrait", "landscape").

4. SAFETY
   4.1 WHAT IS DESTRUCTIVE:
       - create with confirmed=true: overwrites the entire .docx.
   4.2 THE GATE:
       - create: if file exists and confirmed is not true, return NEEDS_CONFIRMATION.

5. CHECKLIST
   □ path is absolute and ends in .docx
   □ For edit: called read first to get current section_id (e.g. p5, t1)
   □ replace: "new" contains plain text with optional inline tags (<b>, <i>, <img>), not a full HTML block.

   DESTRUCTIVE GATE:
   □ create: file already exists? Set confirmed=true only after user confirms

</tool_rules>
"""
WORD_TOOL_ERROR_PROMPT = """
<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. FILE_NOT_FOUND       — wrong path or file moved/renamed
   B. SECTION_NOT_FOUND    — section_id not present in current HTML
   C. TEXT_NOT_FOUND       — old text not found in HTML (text patch)
   D. IMPORT_ERROR         — htmldocx not installed
   E. PERMISSION_DENIED    — file locked or no write access

2. RETRY RULES
   2.1 SECTION_NOT_FOUND   → call read to get fresh HTML with current IDs, retry with correct id.
   2.2 TEXT_NOT_FOUND      → call read, copy the exact string from HTML output, retry.
   2.3 FILE_NOT_FOUND      → verify path. Use fs_browse.find to locate the file if uncertain.
   2.4 IMPORT_ERROR        → report to user with install command. Do not retry.
   2.5 PERMISSION_DENIED   → file may be open in Word or another app. Ask user to close it.

3. RECOVERY CHECKLIST
   □ EDITS_FAILED list shows exactly which ops failed and why — fix only those ops
   □ EDITS_APPLIED ops already succeeded — do not re-apply them
   □ After SECTION_NOT_FOUND, always re-read before retrying edit

   DESTRUCTIVE GATE:
   □ Do NOT retry create with confirmed=true without re-confirming with user

</error_recovery>"""
