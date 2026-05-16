WORD_TOOL_PROMPT = """
TOOL_NAME: word
TOOL_DESCRIPTION: Create, read, and edit Word (.docx) documents using HTML+CSS as the authoring format. Does not handle .doc (legacy). For format conversion (docx → pdf, html, etc.) use the converter tool.

<functions>
  <function>
    <name>create</name>
    <description>Create a new .docx from an HTML+CSS string. Stores the HTML source alongside for future edits.</description>
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
    <description>Return the HTML source of a document. If no HTML source exists, extracts and reconstructs from the .docx. Use search to retrieve only relevant sections.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .docx file
      - search (string, OPTIONAL) — if provided, return only HTML fragments whose text contains this string
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>edit</name>
    <description>Apply a list of edit ops to the document HTML source, then re-render to .docx. If no HTML source exists, extracts the document content first.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .docx file
      - edits (array, REQUIRED) — list of edit op objects:

        replace:    { "section_id": "s3", "new": "<p id='s3'>Updated text.</p>" }
        text patch: { "old": "old text", "new": "new text" }
        add_after:  { "op": "add_after",  "section_id": "s3", "new": "<p>New paragraph.</p>" }
        add_before: { "op": "add_before", "section_id": "s2", "new": "<h2>New heading</h2>" }
        add_end:    { "op": "add_end",                         "new": "<p>Conclusion.</p>" }
        remove:     { "op": "remove",     "section_id": "s4" }
    </parameters>
    <destructive>YES — modifies file in place</destructive>
    <confirmation_required>NO — edits are non-destructive to other sections; remove op does not require confirmation</confirmation_required>
  </function>

</functions>

<tool_rules>

1. HTML SOURCE IS THE EDIT TARGET
   1.1 All edits operate on the stored .html source, not the .docx directly.
   1.2 If no .html source exists, the tool extracts content from the .docx first, then applies edits.
   1.3 After every edit, the .html source is saved and the .docx is re-rendered from it.
   1.4 Section IDs (s1, s2, ...) are auto-assigned by the tool — never invent or guess IDs.
       Always call read first to see the current IDs before editing.

2. AUTHORING FORMAT
   2.1 content in create must be a valid HTML string — inline <style> or a <style> block in <head>.
   2.2 Supported elements: h1–h6, p, table/tr/th/td, ul/ol/li, strong, em, u, br, hr, img.
   2.3 CSS styling is best-effort — Word ignores most CSS. Structure (headings, tables, lists) transfers reliably; colors and fonts may not.
   2.4 For reliable results, keep styling simple: font-family, font-size, color on block elements.

3. EDIT OPS
   3.1 replace: section_id + new — provide the full replacement element including its id attribute.
   3.2 text patch: old + new — old must be an exact substring of the HTML. Use read to confirm exact text first.
   3.3 add_after / add_before: insert new HTML adjacent to the target section.
   3.4 add_end: appends to end of document body.
   3.5 remove: deletes the section entirely. The surrounding sections are not affected.
   3.6 Multiple ops in one call are applied in order. If one fails, remaining ops still run.

4. SAFETY
   4.1 WHAT IS DESTRUCTIVE:
       - create with confirmed=true: overwrites the entire .docx and its .html source.
   4.2 THE GATE:
       - create: if file exists and confirmed is not true, return NEEDS_CONFIRMATION.

5. CHECKLIST
   □ path is absolute and ends in .docx
   □ For edit: called read first to get current section IDs
   □ replace: new content includes the id attribute (e.g. id='s3')
   □ text patch: old is an exact substring — verified from read output

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
