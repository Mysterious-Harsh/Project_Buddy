FS_WRITE_TOOL_PROMPT = """
TOOL_NAME: fs_write
TOOL_DESCRIPTION: Create, append to, or patch plain text files. All paths must be absolute.

<functions>
  <function>
    <name>write</name>
    <description>Create a new file, append content to one, or replace exact text in-place.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to the file
      - action (string, REQUIRED) — "create" | "append" | "patch"
      - content (string, OPTIONAL) — text to write; required for create and append
      - old_str (string, OPTIONAL) — exact text to replace; required for patch
      - new_str (string, OPTIONAL) — replacement text; required for patch
      - confirmed (boolean, OPTIONAL, default: false) — must be true when overwriting an existing file
    </parameters>
    <destructive>CONDITIONAL — overwriting an existing file with action="create"</destructive>
    <confirmation_required>YES — when action="create" and the file already exists</confirmation_required>
  </function>
</functions>

<tool_rules>

1. PATHS
   1.1 All paths must be absolute. Parent directories are created automatically.
   1.2 Unresolvable path → status="followup". Do not guess.

2. ACTIONS
   2.1 action="create" — creates new file. If file already exists and confirmed=false → returns NEEDS_CONFIRMATION.
       Set confirmed=true only after explicit user YES in prior turns.
   2.2 action="append" — adds content to end of file. Creates the file if it does not exist.
   2.3 action="patch" — replaces old_str with new_str exactly once.
       old_str MUST match exactly once. Read the file first with fs_read.read to get the exact current text.
       If old_str matches 0 times → read the file, retry with the correct text.
       If old_str matches N>1 times → add more surrounding lines to make it unique.

3. TEXT FILES — USE fs_write FOR ANY PLAIN TEXT FORMAT
   .txt .md .csv .json .yaml .yml .toml .xml .html .htm .css .js .ts .py .sh .bat
   .log .env .ini .cfg .conf .rst .tex and any other text-based file.
   No structured tool needed — fs_write handles all of these directly.

4. SAFETY
   4.1 THE GATE — action="create" on existing file:
       1. Check prior turns for explicit user YES for this exact path.
       2. Not confirmed → status="followup". State the path and current file size.
       3. Confirmed → set confirmed=true.

5. CHECKLIST
   □ path is absolute
   □ action is exactly one of: create, append, patch
   □ For create on existing file: confirmed=true AND prior turn has explicit YES for this path
   □ For patch: read the file first if old_str content is uncertain
   □ For patch: old_str appears exactly once (verified by reading the file)

   DESTRUCTIVE GATE:
   □ action="create" on existing file → confirmed=true AND prior turn has explicit YES

</tool_rules>
"""
FS_WRITE_TOOL_ERROR_PROMPT = """
<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. FILE EXISTS (create) — not confirmed → status="followup". State path and current file size.
   B. PATCH NOT FOUND — old_str matched 0 times → read the file first, retry with the exact current text.
   C. PATCH AMBIGUOUS — old_str matched N>1 times → add more surrounding lines to make it unique.
   D. PERMISSION DENIED — status="followup". State path and which operation was denied.
   E. FILE NOT FOUND (patch) — file to patch does not exist. Verify the path.
   F. UNCLASSIFIED — status="followup" with the exact error text and one specific question about what to try next.

2. RETRY RULES
   2.1 Never repeat the identical call that already failed.
   2.2 For patch failures: ALWAYS call fs_read.read on the file before retrying.
   2.3 After 3 failures on the same step → status="followup".

3. RECOVERY CHECKLIST
   □ Error message read in full
   □ For patch failures: file was re-read and old_str confirmed to appear exactly once
   □ confirmed=true only when prior turns have explicit YES for this exact action and path

   DESTRUCTIVE GATE:
   □ For create overwrite: explicit YES present in prior turns for this exact path

</error_recovery>
"""
