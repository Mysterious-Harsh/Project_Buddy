PDF_TOOL_PROMPT = """
TOOL_NAME: pdf
TOOL_DESCRIPTION: Create, read, edit, and merge PDF files using HTML+CSS as the authoring format. Cannot edit PDFs that were not created by Buddy without first extracting their content. For format conversion (other formats → pdf, pdf → images/txt) use the converter tool.

<functions>
  <function>
    <name>create</name>
    <description>Create a new .pdf from an HTML+CSS string. Stores the HTML source alongside for future edits.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path; must end in .pdf
      - content (string, REQUIRED) — full HTML+CSS document string
      - confirmed (boolean, OPTIONAL, default: false) — must be true when file already exists
    </parameters>
    <destructive>CONDITIONAL — overwrites existing file</destructive>
    <confirmation_required>YES — when file already exists</confirmation_required>
  </function>

  <function>
    <name>read</name>
    <description>Return the HTML source of a PDF. If no HTML source exists, extracts and reconstructs content from the PDF. Use search to retrieve only relevant sections.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .pdf file
      - search (string, OPTIONAL) — if provided, return only HTML fragments whose text contains this string
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>edit</name>
    <description>Apply a list of edit ops to the PDF HTML source, then re-render to .pdf. If no HTML source exists, extracts content first.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .pdf file
      - edits (array, REQUIRED) — list of edit op objects:

        replace:    { "section_id": "s3", "new": "<p id='s3'>Updated text.</p>" }
        text patch: { "old": "old text", "new": "new text" }
        add_after:  { "op": "add_after",  "section_id": "s3", "new": "<p>New paragraph.</p>" }
        add_before: { "op": "add_before", "section_id": "s2", "new": "<h2>New heading</h2>" }
        add_end:    { "op": "add_end",                         "new": "<p>Conclusion.</p>" }
        remove:     { "op": "remove",     "section_id": "s4" }
    </parameters>
    <destructive>YES — modifies file in place</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>merge</name>
    <description>Merge multiple PDF files into a single PDF, in the order provided.</description>
    <parameters>
      - sources (array, REQUIRED) — list of absolute paths to .pdf files; merged in order
      - target (string, REQUIRED) — absolute path for output .pdf
      - confirmed (boolean, OPTIONAL, default: false) — must be true when target already exists
    </parameters>
    <destructive>CONDITIONAL — overwrites target if it exists</destructive>
    <confirmation_required>YES — when target already exists</confirmation_required>
  </function>
</functions>

<tool_rules>

1. HTML SOURCE IS THE EDIT TARGET
   1.1 All edits operate on the stored .html source, not the .pdf binary.
   1.2 If no .html source exists, the tool extracts content from the PDF first (best-effort — layout may not be perfect).
   1.3 After every edit, the .html source is saved and the .pdf is re-rendered from it.
   1.4 Section IDs (s1, s2, ...) are auto-assigned by the tool — never invent or guess IDs.
       Always call read first to see the current IDs before editing.

2. AUTHORING FORMAT
   2.1 content in create must be a valid HTML string with optional CSS in a <style> block.
   2.2 weasyprint supports most CSS 2.1 + select CSS 3: font-family, color, margins, padding, borders, flexbox (partial).
   2.3 Use @page in CSS to control page size and margins:
       @page { size: A4; margin: 2cm; }
   2.4 Use page-break-before: always on headings or divs to force new pages.
   2.5 Images: use absolute file:// paths or base64 data URIs — relative paths will not resolve.

3. EDIT OPS
   3.1 replace: section_id + new — provide the full replacement element including its id attribute.
   3.2 text patch: old + new — old must be an exact substring of the HTML. Use read to confirm.
   3.3 add_after / add_before: insert new HTML adjacent to the target section.
   3.4 add_end: appends to end of document body.
   3.5 remove: deletes the section. Multiple ops in one call are applied in order.

4. SAFETY
   4.1 WHAT IS DESTRUCTIVE:
       - create with confirmed=true: overwrites the entire .pdf and its .html source.
       - merge with confirmed=true: overwrites the target .pdf.
   4.2 THE GATE:
       - create / merge: if target exists and confirmed is not true, return NEEDS_CONFIRMATION.

6. CHECKLIST
   □ path is absolute and ends in .pdf
   □ For edit: called read first to get current section IDs
   □ replace: new content includes the id attribute (e.g. id='s3')
   □ Images in HTML use absolute paths or base64 data URIs
   □ @page CSS used for page size and margins
   DESTRUCTIVE GATE:
   □ create / merge: target exists? Set confirmed=true only after user confirms

</tool_rules>
"""

PDF_TOOL_ERROR_PROMPT = """<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. FILE_NOT_FOUND       — wrong path or file moved/renamed
   B. SECTION_NOT_FOUND    — section_id not present in current HTML
   C. TEXT_NOT_FOUND       — old text not found in HTML (text patch)
   D. IMPORT_ERROR         — weasyprint or pypdf not installed
   E. MERGE_ERROR          — one or more source PDFs not found or unreadable
   F. PERMISSION_DENIED    — file locked or no write access
   G. RENDER_ERROR         — weasyprint failed to render HTML (malformed HTML or unsupported CSS)

2. RETRY RULES
   2.1 SECTION_NOT_FOUND   → call read to get fresh HTML with current IDs, retry with correct id.
   2.2 TEXT_NOT_FOUND      → call read, copy exact string from HTML output, retry.
   2.3 FILE_NOT_FOUND      → verify path. Use fs_browse.find to locate the file.
   2.4 IMPORT_ERROR        → report to user with install command. Do not retry.
   2.5 MERGE_ERROR         → check MISSING_FILES in error, verify all source paths exist.
   2.6 RENDER_ERROR        → simplify the HTML/CSS — remove unsupported properties, fix malformed tags.

3. RECOVERY CHECKLIST
   □ EDITS_FAILED shows exactly which ops failed — fix only those
   □ EDITS_APPLIED ops already succeeded — do not re-apply
   □ After SECTION_NOT_FOUND, always re-read before retrying

   DESTRUCTIVE GATE:
   □ Do NOT retry create/merge with confirmed=true without re-confirming with user

</error_recovery>"""
