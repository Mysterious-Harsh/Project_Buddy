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
    <name>diff</name>
    <description>Show a unified diff between two text files.</description>
    <parameters>
      - path_a (string, REQUIRED) — absolute path to the first file
      - path_b (string, REQUIRED) — absolute path to the second file
    </parameters>
    <returns>OK, PATH_A, PATH_B, DIFF, TRUNCATED</returns>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>manage</name>
    <description>Copy, move, delete, or make directories — operates on multiple paths in one call. Use transfers for copy/move to multiple destinations at once.</description>
    <parameters>
      - action (string, REQUIRED) — "copy" | "move" | "delete" | "mkdir"
      - paths (array, OPTIONAL) — list of absolute source paths; glob patterns supported (e.g. "/dir/*.pdf"); not applicable to mkdir; required when transfers is absent
      - destination_dir (string, OPTIONAL) — absolute target directory; required for copy/move when transfers is absent
      - transfers (array, OPTIONAL) — batch copy/move to multiple destinations in one call; each entry: {paths: [...], destination_dir: "..."}; overrides paths + destination_dir
      - permanent (boolean, OPTIONAL, default: false) — delete only: permanently deletes instead of moving to trash; use only when user explicitly asks for permanent deletion
      - confirmed (boolean, OPTIONAL, default: false) — must be true for delete, and for copy/move when destinations already exist
    </parameters>
    <returns>OK, ACTION, TOTAL, SUCCEEDED, FAILED, RESULTS[]</returns>
    <destructive>CONDITIONAL — delete, move, copy over existing destination</destructive>
    <confirmation_required>YES — delete always; copy/move when destination_dir contents would be overwritten</confirmation_required>
  </function>

  <function>
    <name>rename</name>
    <description>Rename one or more files or directories in place — does not move them to a different directory.</description>
    <parameters>
      - renames (array, REQUIRED) — list of {path, new_name} pairs; path is absolute; new_name is filename only (no slashes)
      - confirmed (boolean, OPTIONAL, default: false) — must be true when new_name already exists at that location
    </parameters>
    <returns>OK, ACTION, TOTAL, SUCCEEDED, FAILED, RESULTS[]</returns>
    <destructive>CONDITIONAL — when new_name already exists at that location</destructive>
    <confirmation_required>YES — when new_name already exists</confirmation_required>
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
   5.1 Destructive actions: write create on existing file, delete, move, copy to existing destination_dir.
   5.2 THE GATE — NO EXCEPTIONS:
       1. Check prior turns for explicit confirmation of this exact action on these exact paths.
       2. Not confirmed → status="followup". State all paths affected and whether it is reversible.
       3. Confirmed → set confirmed=true and construct the call.
   5.3 Only an explicit YES in prior turns counts. Implied intent, goal necessity, or reasoning does not.

6. MANAGE — USAGE RULES
   6.1 paths must always be a list, even for a single item: ["path/to/file"].
   6.2 destination_dir must be a directory path — files are placed inside it by name.
   6.3 For mkdir: paths is the list of directories to create; destination_dir is not used.
   6.4 RESULTS[] in the response contains a per-path outcome — check FAILED count before reporting success.
   6.5 Glob patterns in paths are expanded automatically (e.g. ["/dir/*.pdf"] moves all PDFs).
       If a pattern matches nothing, GLOB_WARNINGS lists it. TOTAL reflects only the matched files.
   6.6 USE transfers WHEN FILES GO TO MULTIPLE DESTINATIONS IN ONE STEP:
       transfers: [
         {"paths": ["/dir/a.pdf", "/dir/b.pdf"], "destination_dir": "/dest/pdf"},
         {"paths": ["/dir/img.jpg"],              "destination_dir": "/dest/images"}
       ]
       When transfers is set, paths and destination_dir at the top level are ignored.

7. RENAME — USAGE RULES
   7.1 renames must always be a list, even for a single item.
   7.2 new_name is a filename only — no directory separators allowed.
       To move AND rename, use manage with action="move" instead.
   7.3 Each rename happens in place — the file stays in the same directory.

8. CHECKLIST
   □ Path is absolute and resolved from inputs — not constructed or guessed
   □ ls for directories, read for files — never mixed
   □ action is one of the exact allowed values for that function
   □ For read tabular: filter added if prior result showed NEEDS_CONFIRMATION or large row count
   □ For patch: old_str will match exactly once — if unsure, read the file first
   □ For write create on existing: confirmed=true and prior turn has explicit YES
   □ For manage delete/move/copy to existing: confirmed=true and prior turn has explicit YES
   □ For manage copy/move single destination: destination_dir is a directory path, paths is a list
   □ For manage copy/move multiple destinations: use transfers array instead of paths + destination_dir
   □ For rename: new_name has no slashes; confirmed=true only when target already exists
   □ For diff: both path_a and path_b must be existing files, not directories

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
   J. MANAGE PARTIAL FAILURE — check RESULTS[] for per-path errors; fix and retry only the failed paths.
   K. RENAME TARGET EXISTS — not confirmed → status="followup". Confirmed → set confirmed=true.
   L. RENAME new_name HAS SLASHES — remove directory separators; use manage move if relocation is needed.
   M. UNCLASSIFIED — status="followup" with the exact error and one specific question.

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
