EDIT_FILE_PROMPT = """
<tool_description>
EDIT_FILE TOOL — create, overwrite, append to, or patch a file.
</tool_description>

<actions>
ACTIONS:
  write  → create a new file or fully overwrite an existing one (confirmed gate)
  append → add text to the end of a file (no confirmation needed — additive)
  patch  → find exact text and replace it in-place (confirmed gate)
</actions>

<call_schema>
SCHEMA:
  action    : required — "write" | "append" | "patch"
  path      : required
  content   : text to write or append (write / append)
  old_str   : exact text to find, including whitespace and indentation (patch)
  new_str   : replacement text — omit or set "" to delete the matched text (patch)
  replace_all: default false — patch replaces first match only; set true to replace all
  confirmed : default false — write and patch require two calls (see CONFIRMATION below)
</call_schema>

<examples>
EXAMPLES:
  {"action": "write",  "path": "/notes.txt", "content": "hello world", "confirmed": false}
  {"action": "append", "path": "/log.txt",   "content": "new entry\n"}
  {"action": "patch",  "path": "/app.py",    "old_str": "def foo():\n    pass", "new_str": "def foo():\n    return 42", "confirmed": false}
</examples>

<confirmation_gate>
CONFIRMATION GATE (write / patch):
  Call 1 — confirmed=false (default):
    Returns NEEDS_CONFIRMATION=true and PREVIEW showing exactly what will change.
    Use PREVIEW text as your followup_question to ask the user.
    Do NOT execute yet — status="followup".

  Call 2 — after user approves:
    Repeat the identical call with confirmed=true.
    Tool executes and returns OK=true.

  If user says no → status="abort".
  append does NOT need confirmation.
</confirmation_gate>

<patch_rules>
PATCH RULES:
  1. Always read_file first to get exact text including whitespace and indentation.
  2. old_str must match exactly — copy it character for character from the file.
  3. If old_str has 2+ occurrences → add more surrounding lines to make it unique,
     or set replace_all=true to replace all occurrences.
  4. new_str="" deletes the matched text entirely.
</patch_rules>

<result_fields>
RESULT FIELDS:
  OK, ACTION, PATH, ERROR
  NEEDS_CONFIRMATION, PREVIEW — confirmation gate
  SIZE_BYTES  — bytes written (write / append)
  OCCURRENCES — number of matches replaced (patch)
  NOTE        — actionable hint
</result_fields>
"""

EDIT_FILE_ERROR_PROMPT = """
EDIT_FILE ERROR RECOVERY — read ERROR carefully, fix before retrying.

  file not found (patch)     → file must exist for patch; use write to create it
  old_str not found          → read_file first, copy exact text with whitespace/indentation
  old_str 2+ occurrences     → add more surrounding lines to old_str, or set replace_all=true
  new_str empty/null         → omit new_str or set "" to delete matched text — this is valid
  path not found (write)     → parent dirs are created automatically — path is fine
  permission denied          → cannot fix — status="followup", tell user
  NEEDS_CONFIRMATION         → read PREVIEW, ask user via followup, then call with confirmed=true
  content empty (write)      → confirm with user if intentional before proceeding
  3 failures                 → status="followup", describe what was tried
"""

tool_call_format = '{"action": "write" | "append" | "patch", "path": "/abs/path", "content": "...", "confirmed": false}'
