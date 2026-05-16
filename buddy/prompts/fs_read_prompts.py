FS_READ_TOOL_PROMPT = """
TOOL_NAME: fs_read
TOOL_DESCRIPTION: Read file contents in any format and compare two text files. Read-only. All paths must be absolute.

<functions>
  <function>
    <name>read</name>
    <description>Read a file. Format auto-detected by extension. Use info=true for metadata only.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to a file
      - start_line (integer, OPTIONAL) — first line to read, 1-indexed; text files only
      - end_line (integer, OPTIONAL) — last line to read, inclusive; text files only
      - search_pattern (string, OPTIONAL) — return only lines/rows matching this text (±2 context lines)
      - pandas_query (string, OPTIONAL) — tabular files only: filter rows e.g. "score > 90"
      - columns (array, OPTIONAL) — tabular files only: select columns e.g. ["name", "score"]
      - sheet_name (string, OPTIONAL) — Excel only: sheet name; defaults to first sheet
      - max_chars (integer, OPTIONAL, default: 8000) — output size cap
      - encoding (string, OPTIONAL, default: "utf-8")
      - info (boolean, OPTIONAL, default: false) — return metadata only: EXISTS, SIZE_BYTES, MODIFIED, CREATED
      - confirmed (boolean, OPTIONAL, default: false) — set true to open a binary file with the system app
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO — except binary files which return NEEDS_CONFIRMATION first</confirmation_required>
  </function>

  <function>
    <name>diff</name>
    <description>Show a unified diff between two plain text files.</description>
    <parameters>
      - path_a (string, REQUIRED) — absolute path to the first file
      - path_b (string, REQUIRED) — absolute path to the second file
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>
</functions>

<tool_rules>

1. PATHS
   1.1 All paths must be absolute. Resolve ~ and $VAR before calling.
   1.2 Never call read on a directory path — use fs_browse.ls for directories.
   1.3 Unresolvable path → status="followup". Do not guess or construct paths.

2. READ — FORMAT DETECTION
   2.1 Text/code (.py .js .md .json .toml .yaml .sh .log .env etc.) → CONTENT as plain text.
   2.2 Tabular (.csv .tsv .xlsx .xls .parquet .feather .orc) → CONTENT rendered as table.
       Use pandas_query to filter rows or columns to select specific columns.
   2.3 Document (.pdf .docx .doc) → text extracted and returned as CONTENT.
   2.4 Binary (.png .jpg .mp3 .zip .exe etc.) → returns NEEDS_CONFIRMATION.
       Call again with confirmed=true to open with the system application.
   2.5 info=true → metadata only (EXISTS, IS_FILE, IS_DIR, SIZE_BYTES, MODIFIED). No content read.

3. READ — LARGE FILES
   3.1 TRUNCATED=true → use start_line/end_line to read the next section.
   3.2 Tabular NEEDS_CONFIRMATION (too large) → add pandas_query or columns filter, then retry.
   3.3 search_pattern returns only matching lines with ±2 context lines — use to find specific content.

4. DIFF
   4.1 Both path_a and path_b must be existing files, not directories.
   4.2 Binary files are rejected. Both files must be plain text.

5. CHECKLIST
   □ path is absolute and points to a file — not a directory
   □ For tabular: pandas_query or columns added if prior result showed NEEDS_CONFIRMATION
   □ For diff: both paths are existing plain text files

</tool_rules>
"""
FS_READ_TOOL_ERROR_PROMPT = """
<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. PATH NOT FOUND — verify from prior step output. Use info=true to check existence. Status="followup" if path is unresolvable.
   B. IS A DIRECTORY — called read on a directory. Switch to fs_browse.ls.
   C. BINARY FILE — returns NEEDS_CONFIRMATION. Call again with confirmed=true to open with system app.
   D. PERMISSION DENIED — status="followup". State path and which operation was denied.
   E. ENCODING ERROR — retry with encoding="latin-1".
   F. TABULAR TOO LARGE — add pandas_query, columns, or search_pattern to reduce output, then retry.
   G. TRUNCATED — use start_line/end_line to continue reading the file.
   H. UNCLASSIFIED — status="followup" with the exact error text and one specific question about what to try next.

2. RETRY RULES
   2.1 Never repeat the identical call that already failed.
   2.2 For tabular too-large: always add a filter before retrying.
   2.3 After 3 failures on the same step → status="followup".

3. RECOVERY CHECKLIST
   □ Error message read in full
   □ One category matched and only that fix applied
   □ confirmed=true only when prior turns have explicit YES for opening this binary file

</error_recovery>"""
