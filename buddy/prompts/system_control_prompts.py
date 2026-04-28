SYSTEM_CONTROL_TOOL_PROMPT = """
TOOL_NAME: system_control
TOOL_DESCRIPTION: Control media playback, volume, app launching, and system state (lock/sleep).

<functions>
  <function>
    <name>control</name>
    <description>Execute a system action using a plain command string.</description>
    <parameters>
      - action (string, REQUIRED) — plain command; see allowed formats in rule 2
    </parameters>
    <returns>OK, ACTION, REPLY, ERROR</returns>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>
</functions>

<tool_rules>

1. WHEN TO USE
   1.1 Use for: media control, volume, opening an app by name, lock screen, sleep.
   1.2 Do NOT use when:
       - User wants a specific song/video by name → use web_search or ask the user.
       - Action requires knowing current system state → use terminal tool instead.

2. ACTION FORMAT
   Write the action as a plain natural command string. Allowed patterns:

   Media:   "play"  "pause"  "next track"  "previous track"
            "play Blinding Lights on Spotify"  "play Lo-fi on YouTube"
   Volume:  "volume up"  "volume down"  "volume 60"  "mute"
   App:     "open Spotify"  "open Chrome"  "open Terminal"
   System:  "lock screen"  "sleep"

3. SCOPE
   3.1 One action per call.
   3.2 Do not chain multiple commands into a single action string.

</tool_rules>

<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. COMMAND NOT RECOGNIZED — OK=false, ERROR contains "Could not interpret".
      Rephrase the action using the exact patterns listed in rule 2.
      Never repeat the same action string.

   B. EXECUTION FAILED — OK=false, ERROR set with a system-level message.
      Report status="followup" with the exact ERROR value. Do not retry silently.

   C. UNCLASSIFIED — Return status="followup" with the exact ERROR value and one specific question.

2. RETRY RULES
   2.1 Never repeat the identical action string that already failed.
   2.2 After 2 failures → status="followup".

</error_recovery>
"""
