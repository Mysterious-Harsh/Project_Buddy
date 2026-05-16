FS_BROWSE_TOOL_PROMPT = """
TOOL_NAME: fs_browse
TOOL_DESCRIPTION: List directory contents and find files by name or text content. Read-only. All paths must be absolute.

<functions>
  <function>
    <name>ls</name>
    <description>List a directory flat or as a tree.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to a directory
      - depth (integer, OPTIONAL, default: 1) — 1 = flat listing; 2+ = tree view
      - show_hidden (boolean, OPTIONAL, default: false) — include dotfiles
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
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
      - file_types (array, OPTIONAL) — filter extensions e.g. ["py", "js"]; type=content only
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>
</functions>

<tool_rules>

1. PATHS
   1.1 All paths must be absolute. Resolve ~ and $VAR before calling.
   1.2 ls requires a directory path. Calling ls on a file → error, use fs_read.read instead.
   1.3 Unresolvable path → status="followup". Do not guess or construct paths.

2. LS — USAGE
   2.1 depth=1 returns ENTRIES[] (flat list with metadata). depth=2+ returns TREE_TEXT (tree string).
   2.2 ENTRIES[] each have: name, path, type, permissions, modified, created, size/item_count.

3. FIND — TYPE SELECTION
   3.1 type="name" (default) — matches file/dir names against a glob pattern (e.g. "*.py", "report*").
   3.2 type="content" — searches text inside each file (case-insensitive regex). Binary files are skipped.
       file_types filters which extensions to search (["py"] not [".py"]).
   3.3 context_lines adds N lines before/after each match line (type=content only).

4. CHECKLIST
   □ path is absolute and resolves to an existing directory
   □ ls is called on a directory path — not a file path
   □ find type matches the pattern style: glob for "name", text/regex for "content"
   □ find file_types does not include leading dots: ["py"] not [".py"]

</tool_rules>
"""

FS_BROWSE_TOOL_ERROR_PROMPT = """<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. PATH NOT FOUND — path does not exist. Verify from prior step output; status="followup" if still unknown.
   B. NOT A DIRECTORY — ls called on a file. Switch to fs_read.read for file contents.
   C. PERMISSION DENIED — status="followup". State path and required operation.
   D. NO RESULTS — TOTAL_FOUND=0 is a valid successful result. Report as "not found", do not retry.
   E. UNCLASSIFIED — status="followup" with the exact error text and one specific question about what to try next.

2. RETRY RULES
   2.1 Never repeat the identical call that already failed.
   2.2 After 3 failures on the same step → status="followup".

3. RECOVERY CHECKLIST
   □ Error message read in full
   □ One category matched and only that fix applied
   □ Call is meaningfully different from the failed attempt

</error_recovery>"""
