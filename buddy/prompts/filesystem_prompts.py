FILESYSTEM_TOOL_PROMPT = """
TOOL_NAME: filesystem
TOOL_DESCRIPTION: Read, write, search, and manage files and directories. All paths must be absolute.

<functions>
  <function>
    <name>ls</name>
    <description>List or tree a directory.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to a directory
      - depth (integer, OPTIONAL, default: 1) — 1 for flat listing, 2+ for tree view
      - show_hidden (boolean, OPTIONAL, default: false) — include dotfiles
    </parameters>
    <returns>OK, PATH, ENTRIES or TREE_TEXT, TOTAL</returns>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>read</name>
    <description>Read a file. Format is auto-detected by extension. Use info=true for metadata only.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to a file
      - start_line (integer, OPTIONAL) — first line to read, 1-indexed; text files only
      - end_line (integer, OPTIONAL) — last line to read, inclusive; text files only
      - search_pattern (string, OPTIONAL) — return only lines/rows matching this text
      - pandas_query (string, OPTIONAL) — tabular files only: filter rows e.g. "score > 90"
      - columns (array, OPTIONAL) — tabular files only: select columns e.g. ["name", "score"]
      - sheet_name (string, OPTIONAL) — Excel only: sheet name; defaults to first sheet
      - max_chars (integer, OPTIONAL, default: 8000) — output size cap
      - encoding (string, OPTIONAL, default: "utf-8")
      - info (boolean, OPTIONAL, default: false) — return metadata only: size, modified, exists
      - confirmed (boolean, OPTIONAL, default: false) — set true to open a binary file with the system app
    </parameters>
    <returns>OK, PATH, CONTENT, FORMAT, SIZE_BYTES, MODIFIED, LINE_COUNT, TRUNCATED, NOTE</returns>
    <destructive>NO</destructive>
    <confirmation_required>NO — except binary files which return NEEDS_CONFIRMATION first</confirmation_required>
  </function>

  <function>
    <name>write</name>
    <description>Create, append to, or patch a file.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to the file
      - action (string, REQUIRED) — "create" | "append" | "patch"
      - content (string, OPTIONAL) — text to write; required for create and append
      - old_str (string, OPTIONAL) — exact text to replace; required for patch
      - new_str (string, OPTIONAL) — replacement text; required for patch
      - confirmed (boolean, OPTIONAL, default: false) — must be true when overwriting an existing file
    </parameters>
    <returns>OK, PATH, ACTION, SIZE_BYTES</returns>
    <destructive>CONDITIONAL — overwriting an existing file</destructive>
    <confirmation_required>YES — when action="create" and file already exists</confirmation_required>
  </function>

  <function>
    <name>find</name>
    <description>Find files by name glob or search text inside files.</description>
    <parameters>
      - path (string, REQUIRED) — absolute directory to search in
      - pattern (string, REQUIRED) — glob pattern (type=name) or text/regex (type=content)
      - type (string, OPTIONAL, default: "name") — "name" | "content"
      - recursive (boolean, OPTIONAL, default: true)
      - max_results (integer, OPTIONAL, default: 50)
      - context_lines (integer, OPTIONAL, default: 0) — lines around each match; type=content only
      - file_types (array, OPTIONAL) — filter by extension e.g. ["py", "js"]; type=content only
    </parameters>
    <returns>OK, PATH, TYPE, PATTERN, RESULTS, TOTAL_FOUND</returns>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>manage</name>
    <description>Copy, move, delete, make directory, or diff two files.</description>
    <parameters>
      - path (string, REQUIRED) — absolute source path
      - action (string, REQUIRED) — "copy" | "move" | "delete" | "mkdir" | "diff"
      - destination (string, OPTIONAL) — absolute destination; required for copy, move, diff
      - confirmed (boolean, OPTIONAL, default: false) — must be true for delete, move, copy to existing destination
    </parameters>
    <returns>OK, ACTION, PATH, DESTINATION, NOTE</returns>
    <destructive>CONDITIONAL — delete, move, copy over existing destination</destructive>
    <confirmation_required>YES — delete, move, and copy when destination already exists</confirmation_required>
  </function>
</functions>

<tool_rules>

1. PATHS
   1.1 All paths must be absolute. Resolve ~ and $VAR before calling.
   1.2 Resolve order: explicit path in <step> → <prior_step_outputs> → prior turns.
   1.3 Unresolvable → status="followup". Do not guess or construct a path.
   1.4 Use ls for directories. Use read for files. Never call read on a directory path.

2. READ — FORMAT DETECTION
   2.1 Text/code (.py .js .md .json .toml .yaml .sh .log .env etc.) → returns CONTENT as text.
   2.2 Tabular (.csv .tsv .xlsx .xls .parquet .feather .orc) → rendered as table; use pandas_query or columns to filter large files.
   2.3 Document (.pdf .docx .doc) → text extracted; use search_pattern to filter.
   2.4 Binary (.png .jpg .mp3 .zip .exe etc.) → returns NEEDS_CONFIRMATION. Call again with confirmed=true to open with system app.
   2.5 info=true → returns metadata only (EXISTS, IS_FILE, SIZE_BYTES, MODIFIED, CREATED). No content read.

3. READ — LARGE FILES
   3.1 If TRUNCATED=true in a prior result, use start_line/end_line to read the remainder.
   3.2 For tabular files that exceed max_chars, add pandas_query or columns to filter before reading.
   3.3 search_pattern returns only matching lines with ±2 context lines — use it to focus large files.

4. WRITE
   4.1 action="create" — creates new file or overwrites existing. Overwrite requires confirmed=true.
   4.2 action="append" — adds to end of file; creates the file if it does not exist.
   4.3 action="patch" — replaces old_str with new_str in-place.
       old_str must match exactly once. If not found → read the file first, then retry with exact current content.
       If matched multiple times → expand old_str with surrounding lines until unique.

5. SAFETY
   5.1 Destructive actions: write create on existing file, delete, move, copy to existing destination.
   5.2 THE GATE — NO EXCEPTIONS:
       1. Check prior turns for explicit confirmation of this exact action on this exact path.
       2. Not confirmed → status="followup". State what will be affected and whether it is reversible.
       3. Confirmed → set confirmed=true and construct the call.
   5.3 Only an explicit YES in prior turns counts. Implied intent, goal necessity, or reasoning does not.

6. CHECKLIST
   □ Path is absolute and resolved from inputs — not constructed or guessed
   □ ls for directories, read for files — never mixed
   □ action is one of the exact allowed values for that function
   □ For read tabular: filter added if prior result showed NEEDS_CONFIRMATION or large row count
   □ For patch: old_str will match exactly once — if unsure, read the file first
   □ For write create on existing: confirmed=true and prior turn has explicit YES
   □ For delete/move/copy to existing: confirmed=true and prior turn has explicit YES

</tool_rules>

<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. PATH NOT FOUND — verify from <prior_step_outputs>; use read with info=true to check existence; status="followup" if still missing.
   B. WRONG FUNCTION — called read on a directory or ls on a file → switch to the correct function.
   C. PERMISSION DENIED — status="followup". State path and required permission. Never silently escalate.
   D. FILE EXISTS (write create, copy) — not confirmed → status="followup". Confirmed → set confirmed=true.
   E. PATCH FAILED (old_str not found) — read the file first, then patch with exact current content.
   F. PATCH AMBIGUOUS (old_str matched multiple times) — read the file, expand old_str with surrounding lines.
   G. BINARY FILE — returns NEEDS_CONFIRMATION. Call again with confirmed=true to open with system app.
   H. TABULAR TOO LARGE — add pandas_query, columns, or search_pattern to reduce output size.
   I. ENCODING ERROR — retry with encoding="latin-1".
   J. UNCLASSIFIED — status="followup" with the exact error and one specific question.

2. RETRY RULES
   2.1 Never repeat the identical call that already failed.
   2.2 For patch failures: always read the file before retrying.
   2.3 After 3 failures on the same step → status="followup".

3. RECOVERY CHECKLIST
   □ Error read fully
   □ One category matched — applying only that fix
   □ Call is meaningfully different from the failed attempt
   □ confirmed=true only when prior turns have explicit YES for this exact action and path

</error_recovery>
"""
