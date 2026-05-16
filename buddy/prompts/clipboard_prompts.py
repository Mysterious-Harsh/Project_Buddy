CLIPBOARD_TOOL_PROMPT = """
TOOL_NAME: clipboard
TOOL_DESCRIPTION: Read from or write to the system clipboard. Works on macOS, Windows, and Linux (requires xclip, xsel, or wl-clipboard).

<functions>
  <function>
    <name>read</name>
    <description>Return the current text content of the clipboard.</description>
    <parameters>
      (none)
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>write</name>
    <description>Place text into the clipboard, replacing its current content.</description>
    <parameters>
      - text (string, REQUIRED) — text to copy to the clipboard
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>
</functions>

<tool_rules>

1. USE
   1.1 Use read to retrieve what the user currently has copied.
   1.2 Use write to place a result on the clipboard so the user can paste it.
   1.3 Never read and write in the same step unless explicitly asked.

2. CONTENT
   3.1 Only plain text is supported. Non-text clipboard data (images, files) returns CONTENT="".
   3.2 An empty clipboard returns CONTENT="" and OK=True — this is not an error.

3. CHECKLIST
   □ function is exactly "read" or "write"
   □ write: text parameter provided and is a non-empty string

</tool_rules>
"""

CLIPBOARD_TOOL_ERROR_PROMPT = """
<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. NO BACKEND (Linux) — xclip, xsel, and wl-clipboard are all missing.
      status="followup". Tell user: sudo apt install xclip  (or  sudo pacman -S xclip).
   B. NO DISPLAY (Linux) — DISPLAY or WAYLAND_DISPLAY not set.
      status="followup". Clipboard requires a running graphical session.
   C. POWERSHELL UNAVAILABLE (Windows) — status="followup". PowerShell is required.
   D. UNCLASSIFIED — status="followup" with the exact ERROR string and one specific fix.

2. RETRY RULES
   2.1 Never retry the same call that already failed.
   2.2 Linux backend errors require user action — no retry helps.

3. RECOVERY CHECKLIST
   □ Error message read fully
   □ One category matched — applying only that fix
   □ status="followup" with exact error and what the user must do

</error_recovery>"""
