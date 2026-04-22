# buddy/prompts/system_control_prompts.py
#
# System control tool prompt — media, volume, apps, screen.
# Schema-first, local-model-first.

SYSTEM_CONTROL_TOOL_PROMPT = """
<tool_description>
SYSTEM CONTROL TOOL

Controls media playback, volume, app launching, and system state.
Write the action as a plain natural command — the system interprets it.
</tool_description>

<when_to_use>
§1. WHEN TO USE
Use this tool when Buddy needs to:
  - Control media: play, pause, skip, previous
  - Adjust volume: up, down, set exact level, mute
  - Open an application by name
  - Lock the screen or sleep the system

DO NOT use if:
  - The user wants a specific song/video by name (use web_search or ask user)
  - The action requires knowing current system state (use terminal tool instead)
</when_to_use>

<call_schema>
§2. CALL SCHEMA
  action : required — a plain command string, exactly as you would say it

  media:   "play"  "pause"  "next track"  "previous track"
           "play Blinding Lights on Spotify"  "play Lo-fi on YouTube"
  volume:  "volume up"  "volume down"  "volume 60"  "mute"
  app:     "open Spotify"  "open Chrome"  "open Terminal"
  system:  "lock screen"  "sleep"
</call_schema>

<result_fields>
§3. RESULT FIELDS
  OK      — true if action succeeded
  ACTION  — the command that was run
  REPLY   — human-readable result
  ERROR   — set if OK is false
</result_fields>
""".strip()

SYSTEM_CONTROL_TOOL_CALL_FORMAT = (
    '{"action": "plain command, e.g. play / volume up / open Spotify / lock screen"}'
)
