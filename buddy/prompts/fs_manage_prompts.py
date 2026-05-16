FS_MANAGE_TOOL_PROMPT = """
TOOL_NAME: fs_manage
TOOL_DESCRIPTION: Copy, move, delete, make directories, and rename files or directories. All paths must be absolute.

<functions>
  <function>
    <name>manage</name>
    <description>Copy, move, delete, or make directories — operates on multiple paths in one call. Use transfers for copy/move to multiple destinations at once.</description>
    <parameters>
      - action (string, REQUIRED) — "copy" | "move" | "delete" | "mkdir"
      - paths (array, OPTIONAL) — list of absolute source paths; glob patterns supported (e.g. "/dir/*.pdf"); required when transfers is absent
      - destination_dir (string, OPTIONAL) — absolute target directory; required for copy/move when transfers is absent
      - transfers (array, OPTIONAL) — batch copy/move to multiple destinations; each entry: {paths: [...], destination_dir: "..."}; overrides paths + destination_dir
      - permanent (boolean, OPTIONAL, default: false) — delete only: permanently deletes instead of moving to trash
      - confirmed (boolean, OPTIONAL, default: false) — required for delete; required for copy/move when destinations already exist
    </parameters>
    <destructive>CONDITIONAL — delete, move (removes source), copy over existing destination</destructive>
    <confirmation_required>YES — delete always; copy/move when destination already exists</confirmation_required>
  </function>

  <function>
    <name>rename</name>
    <description>Rename one or more files or directories in place. Does not move them to a new directory.</description>
    <parameters>
      - renames (array, REQUIRED) — list of {path, new_name} pairs; path is absolute; new_name is filename only (no slashes)
      - confirmed (boolean, OPTIONAL, default: false) — required when new_name already exists at that location
    </parameters>
    <destructive>CONDITIONAL — when new_name already exists at that location</destructive>
    <confirmation_required>YES — when new_name already exists</confirmation_required>
  </function>
</functions>

<tool_rules>

1. PATHS
   1.1 All paths must be absolute. Resolve ~ and $VAR before calling.
   1.2 Unresolvable → status="followup". Do not guess.

2. MANAGE — USAGE RULES
   2.1 paths must always be a list, even for a single item: ["/path/to/file"].
   2.2 destination_dir must be a directory path — files land inside it, keeping their original name.
   2.3 mkdir: use paths to list directories to create; destination_dir is not used.
   2.4 RESULTS[] contains a per-path outcome — always check FAILED count before reporting success.
   2.5 Glob patterns in paths are expanded automatically (e.g. ["/dir/*.pdf"] moves all PDFs).
       If a pattern matches nothing, GLOB_WARNINGS lists the unmatched pattern.
   2.6 USE transfers WHEN FILES GO TO MULTIPLE DESTINATIONS IN ONE STEP:
       transfers: [
         {"paths": ["/dir/a.pdf", "/dir/b.pdf"], "destination_dir": "/dest/pdf"},
         {"paths": ["/dir/img.jpg"],              "destination_dir": "/dest/images"}
       ]
       When transfers is set, top-level paths and destination_dir are ignored.

3. RENAME — USAGE RULES
   3.1 renames must always be a list, even for a single item.
   3.2 new_name is a filename only — no directory separators allowed.
       To move AND rename at the same time, use manage action="move" instead.
   3.3 Each rename happens in place — the file stays in the same parent directory.

4. SAFETY
   4.1 Destructive: delete (removes data), move (removes source), copy to existing destination (overwrites).
   4.2 THE GATE — NO EXCEPTIONS:
       1. Check prior turns for explicit user YES for this exact action on these exact paths.
       2. Not confirmed → status="followup". List all affected paths and state whether reversible.
       3. Confirmed → set confirmed=true.
   4.3 permanent=true (delete) — only when user explicitly asks for permanent deletion.
       Default (permanent=false) moves to trash, which is recoverable.

5. CHECKLIST
   □ All paths are absolute
   □ paths is a list (even for a single item)
   □ For copy/move: destination_dir is a directory path, not a file path
   □ For copy/move to multiple destinations: use transfers instead of paths + destination_dir
   □ For rename: new_name has no slashes
   □ RESULTS[] FAILED count checked — partial failure means some paths succeeded and some did not

   DESTRUCTIVE GATE:
   □ delete → confirmed=true AND prior turn has explicit YES
   □ copy/move to existing destination → confirmed=true AND prior turn has explicit YES
   □ rename to existing name → confirmed=true AND prior turn has explicit YES

</tool_rules>
"""
FS_MANAGE_TOOL_ERROR_PROMPT = """<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. SOURCE NOT FOUND — source path does not exist. Verify from prior step output. Use fs_browse.find if path is uncertain.
   B. PERMISSION DENIED — check RESULTS[] for which path failed. Status="followup" with exact path and operation.
   C. DELETE NOT CONFIRMED — delete called without confirmed=true → status="followup". Ask for explicit YES.
   D. COPY/MOVE CONFLICT NOT CONFIRMED — destination exists and confirmed=false → status="followup". List conflicts.
   E. PARTIAL FAILURE — FAILED > 0 in output. Check RESULTS[] for per-path errors. Retry only the failed paths.
   F. GLOB NO MATCH — pattern matched no files (listed in GLOB_WARNINGS). Verify the pattern and root path.
   G. RENAME HAS SLASHES — new_name contains path separators. Remove them; use manage move to relocate.
   H. RENAME TARGET EXISTS — new_name already exists and confirmed=false → status="followup". Ask for explicit YES, then set confirmed=true.
   I. UNCLASSIFIED — status="followup" with the exact error text and one specific question about what to try next.

2. RETRY RULES
   2.1 Never repeat the identical call that already failed.
   2.2 For partial failures: retry only the specific failed paths from RESULTS[], not the full original list.
   2.3 After 3 failures on the same step → status="followup".

3. RECOVERY CHECKLIST
   □ Error message and RESULTS[] read in full
   □ One category matched and only that fix applied
   □ Retry uses only failed paths, not the original full list

   DESTRUCTIVE GATE:
   □ confirmed=true only when prior turns have explicit YES for this exact action and exact paths

</error_recovery>"""
