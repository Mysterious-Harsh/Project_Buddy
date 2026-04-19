MANAGE_FILE_PROMPT = """
<tool_description>
MANAGE_FILE TOOL — copy, move, delete, create directories, open files, compare files.
</tool_description>

<actions>
ACTIONS:
  copy   → duplicate a file or directory to destination
  move   → move or rename a file or directory (confirmed gate)
  delete → permanently delete a file or directory (confirmed gate)
  mkdir  → create a directory and all parents (idempotent)
  open   → open file with the system default application
  diff   → compare two text files and show changes
</actions>

<call_schema>
SCHEMA:
  action      : required — "copy" | "move" | "delete" | "mkdir" | "open" | "diff"
  path        : required — source path
  destination : required for copy / move / diff — target path
  confirmed   : default false — move and delete require two calls (see CONFIRMATION)
</call_schema>

<examples>
EXAMPLES:
  {"action": "copy",   "path": "/src/file.txt",  "destination": "/backup/"}
  {"action": "move",   "path": "/old/name.txt",  "destination": "/new/name.txt", "confirmed": false}
  {"action": "delete", "path": "/tmp/file.txt",  "confirmed": false}
  {"action": "mkdir",  "path": "~/projects/new"}
  {"action": "open",   "path": "/report.pdf"}
  {"action": "diff",   "path": "/file_a.txt",    "destination": "/file_b.txt"}
</examples>

<confirmation_gate>
CONFIRMATION GATE (move / delete):
  Call 1 — confirmed=false (default):
    Returns NEEDS_CONFIRMATION=true and PREVIEW showing exactly what will happen.
    Use PREVIEW text as your followup_question to ask the user.
    Do NOT execute yet — status="followup".

  Call 2 — after user approves:
    Repeat the identical call with confirmed=true.
    Tool executes and returns OK=true.

  If user says no → status="abort".
  copy / mkdir / open / diff do NOT need confirmation.
</confirmation_gate>

<copy_behaviour>
COPY BEHAVIOUR:
  If destination is an existing directory → file is placed inside it.
  If destination path already exists as a file → returns NEEDS_CONFIRMATION before overwrite.
</copy_behaviour>

<result_fields>
RESULT FIELDS:
  OK, ACTION, PATH, ERROR
  NEEDS_CONFIRMATION, PREVIEW — confirmation gate
  DESTINATION  — resolved final path (copy / move)
  DIFF_TEXT    — unified diff output (diff)
  IDENTICAL    — true when files are identical (diff)
  OPENED       — true when file was launched (open)
  NOTE         — actionable hint
</result_fields>
"""

MANAGE_FILE_ERROR_PROMPT = """
MANAGE_FILE ERROR RECOVERY — read ERROR carefully, fix before retrying.

  source not found         → verify path with read_file (info=true) before operating
  destination not found    → parent dirs are created automatically for move/copy
  same path error          → source and destination resolve to same path — fix destination
  permission denied        → cannot fix — status="followup", tell user
  NEEDS_CONFIRMATION       → read PREVIEW, ask user via followup, then call with confirmed=true
  diff needs two files     → path = file1, destination = file2 (both must be files, not dirs)
  diff binary file         → binary files cannot be diffed — use open to view them
  3 failures               → status="followup", describe what was tried
"""

tool_call_format = '{"action": "copy" | "move" | "delete" | "mkdir" | "open" | "diff", "path": "/abs/path", "destination": "/abs/dst", "confirmed": false}'
