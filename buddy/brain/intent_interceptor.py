# buddy/brain/intent_interceptor.py
#
# Fast-path interceptor — handles deterministic system actions without any LLM call.
# Sits at the top of handle_turn(). Returns None for anything ambiguous → Brain takes over.

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

from buddy.logger.logger import get_logger

logger = get_logger("intent_interceptor")

_PLATFORM = sys.platform  # "darwin" | "linux" | "win32"


# ==========================================================
# Normalization — strip filler before matching
# ==========================================================

_PREFIX_RE = re.compile(
    r"^("
    r"(hey|hi|yo|okay|ok)\s+buddy[,\s]*|"
    r"buddy[,\s]*|"
    r"(can|could|would|will|shall)\s+you(\s+please)?\s*[,]?\s*|"
    r"please\s+|kindly\s+|"
    r"i\s+(want|need|would\s+like|'?d\s+like)\s+(you\s+to\s+|to\s+)|"
    r"i\s+just\s+want\s+to\s+|"
    r"help\s+me(\s+(to|with))?\s+|"
    r"(just\s+)?(go\s+ahead\s+and\s+|go\s+and\s+)|"
    r"just\s+|quickly\s+|actually\s+|basically\s+|"
    r"(um+|uh+|hmm+)[,\s]*"
    r")+",
    re.IGNORECASE,
)

_SUFFIX_RE = re.compile(
    r"[\s,]*(for\s+me|please|thanks|thank\s+you|cheers|"
    r"right\s+now|immediately|quickly|asap)[.!?]*$",
    re.IGNORECASE,
)

_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize(text: str) -> str:
    """Strip filler prefixes, suffixes, punctuation, and collapse whitespace."""
    t = text.strip()
    t = _PUNCT_RE.sub(" ", t)
    # Loop prefix strip — handles stacked fillers like "hey buddy can you please just"
    prev = None
    while prev != t:
        prev = t
        t = _PREFIX_RE.sub("", t).strip()
    t = _SUFFIX_RE.sub("", t).strip()
    return " ".join(t.split()).lower()


# ==========================================================
# Ambiguity detection — fall through to Brain if triggered
# ==========================================================

_COREF_RE = re.compile(
    r"\b(this|that|it|the\s+song|the\s+one|the\s+video|the\s+track)\b",
    re.IGNORECASE,
)

# Generic play targets that are NOT ambiguous
_GENERIC_PLAY = re.compile(
    r"^(music|something|anything|songs?|audio|some\s+music|a\s+song|some\s+songs?)$",
    re.IGNORECASE,
)


_ON_APP_RE = re.compile(r'\bon\s+\w+$', re.IGNORECASE)


def _play_is_ambiguous(after_play: str) -> bool:
    """True if 'play <after_play>' refers to specific content Brain should handle."""
    s = after_play.strip()
    if not s:
        return False  # bare "play" → toggle
    if _GENERIC_PLAY.match(s):
        return False
    if _ON_APP_RE.search(s):
        return False  # "Blinding Lights on Spotify" — app-targeted, handled by pattern
    return True  # specific content with no app → Brain


# ==========================================================
# QuickAction
# ==========================================================


@dataclass
class QuickAction:
    name: str
    params: dict = field(default_factory=dict)
    chain: List["QuickAction"] = field(default_factory=list)


# ==========================================================
# Pattern table
# ==========================================================
#
# Each entry: (compiled_re, builder_fn)
# builder_fn(match) -> QuickAction | None
# Return None to fall through even on a regex hit (e.g. ambiguous content).

def _build_patterns():
    P = re.compile

    def media_toggle(m):
        return QuickAction("media_toggle")

    def media_play(m):
        after = (m.group("after") or "").strip()
        if _play_is_ambiguous(after):
            return None  # specific song → Brain
        return QuickAction("media_play")

    def media_pause(m):
        return QuickAction("media_pause")

    def media_next(m):
        return QuickAction("media_next")

    def media_prev(m):
        return QuickAction("media_prev")

    def volume_up(m):
        return QuickAction("volume_step", {"delta": +10})

    def volume_down(m):
        return QuickAction("volume_step", {"delta": -10})

    def volume_set(m):
        return QuickAction("volume_set", {"level": int(m.group("n"))})

    def mute(m):
        return QuickAction("mute_toggle")

    def open_app(m):
        app = (m.group("app") or "").strip()
        if not app:
            return None
        return QuickAction("open_app", {"app": app})

    def play_on_app(m):
        song = (m.group("song") or "").strip()
        app  = (m.group("app") or "").strip().lower()
        if not song or not app:
            return None
        return QuickAction("play_on_app", {"song": song, "app": app})

    def open_and_play(m):
        app = (m.group("app") or "").strip()
        if not app:
            return None
        return QuickAction(
            "open_app",
            {"app": app},
            chain=[QuickAction("media_play")],
        )

    def lock(m):
        return QuickAction("lock_screen")

    def sleep_sys(m):
        return QuickAction("sleep_system")

    return [
        # ── Compound: open X and play ──────────────────────────
        (P(r"^open\s+(?P<app>[\w\s]+?)\s+and\s+play$", re.I), open_and_play),

        # ── App launch ─────────────────────────────────────────
        (P(r"^open\s+(?P<app>[\w\s]+)$", re.I), open_app),
        (P(r"^launch\s+(?P<app>[\w\s]+)$", re.I), open_app),
        (P(r"^start\s+(?P<app>[\w\s]+)$", re.I), open_app),

        # ── Media ──────────────────────────────────────────────
        (P(r"^play\s+(?P<song>.+?)\s+on\s+(?P<app>\w+)$", re.I), play_on_app),
        (P(r"^play\s*(?P<after>.*)$", re.I), media_play),
        (P(r"^(pause|stop\s+music|stop\s+playback)$", re.I), media_pause),
        (P(r"^(resume|continue\s+music|continue\s+playing)$", re.I), media_play),
        (P(r"^(play\s*pause|toggle\s+music|toggle\s+playback)$", re.I), media_toggle),
        (P(r"^next(\s+(track|song))?$", re.I), media_next),
        (P(r"^(previous|prev)(\s+(track|song))?$", re.I), media_prev),
        (P(r"^skip(\s+(track|song))?$", re.I), media_next),

        # ── Volume ─────────────────────────────────────────────
        (P(r"^volume\s+(up|louder|increase)$", re.I), volume_up),
        (P(r"^volume\s+(down|lower|quieter|decrease|softer)$", re.I), volume_down),
        (P(r"^(turn\s+up|louder|increase\s+volume)$", re.I), volume_up),
        (P(r"^(turn\s+down|lower\s+volume|decrease\s+volume|quieter)$", re.I), volume_down),
        (P(r"^volume\s+(?P<n>\d{1,3})(%)?$", re.I), volume_set),
        (P(r"^set\s+volume\s+(to\s+)?(?P<n>\d{1,3})(%)?$", re.I), volume_set),
        (P(r"^(mute|unmute|toggle\s+mute)$", re.I), mute),

        # ── System ─────────────────────────────────────────────
        (P(r"^lock(\s+(screen|my\s+screen|the\s+screen))?$", re.I), lock),
        (P(r"^(sleep|put\s+(the\s+)?computer\s+to\s+sleep)$", re.I), sleep_sys),
    ]


_PATTERNS = _build_patterns()


# ==========================================================
# Platform command execution
# ==========================================================

def _run(cmd: str, timeout: int = 5) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as exc:
        return -1, str(exc)


_IS_MAC = _PLATFORM == "darwin"
_IS_WIN = _PLATFORM == "win32"
_IS_LIN = _PLATFORM.startswith("linux")

# Windows: send a virtual media/volume key via PowerShell + WScript.Shell.
# VK codes: play/pause=179, next=176, prev=177, vol_up=175, vol_down=174, mute=173
def _win_sendkey(vk: int) -> None:
    _run(f'powershell -NoProfile -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]{vk})"')


def _exec_action(action: QuickAction) -> str:
    name = action.name
    p = action.params

    # ── Media ──────────────────────────────────────────────────────

    if name == "media_play":
        if _IS_MAC:
            _run('osascript -e \'tell application "System Events" to key code 100\'')
        elif _IS_WIN:
            _win_sendkey(179)   # VK_MEDIA_PLAY_PAUSE
        else:
            _run("playerctl play")
        return "Playing."

    if name == "media_pause":
        if _IS_MAC:
            _run('osascript -e \'tell application "System Events" to key code 100\'')
        elif _IS_WIN:
            _win_sendkey(179)   # VK_MEDIA_PLAY_PAUSE (toggle)
        else:
            _run("playerctl pause")
        return "Paused."

    if name == "media_toggle":
        if _IS_MAC:
            _run('osascript -e \'tell application "System Events" to key code 100\'')
        elif _IS_WIN:
            _win_sendkey(179)
        else:
            _run("playerctl play-pause")
        return "Toggled playback."

    if name == "media_next":
        if _IS_MAC:
            _run('osascript -e \'tell application "System Events" to key code 101\'')
        elif _IS_WIN:
            _win_sendkey(176)   # VK_MEDIA_NEXT_TRACK
        else:
            _run("playerctl next")
        return "Skipped to next track."

    if name == "play_on_app":
        song = p.get("song", "")
        app  = p.get("app", "").lower()
        song_encoded = song.replace(" ", "+")
        deeplinks = {
            "spotify":      f"spotify:search:{song}",
            "music":        f"music://search?term={song_encoded}",
            "youtubemusic": f"https://music.youtube.com/search?q={song_encoded}",
            "youtube":      f"https://www.youtube.com/results?search_query={song_encoded}",
        }
        link = deeplinks.get(app)
        if _IS_MAC:
            if link:
                _run(f'open "{link}"')
            else:
                _run(f'open -a "{app}"')
                time.sleep(1.5)
                _run('osascript -e \'tell application "System Events" to key code 100\'')
        elif _IS_WIN:
            _run(f'start "" "{link or app}"')
        else:
            _run(f'xdg-open "{link or app}"')
        return f'Playing "{song}" on {app.title()}.'

    if name == "media_prev":
        if _IS_MAC:
            _run('osascript -e \'tell application "System Events" to key code 98\'')
        elif _IS_WIN:
            _win_sendkey(177)   # VK_MEDIA_PREV_TRACK
        else:
            _run("playerctl previous")
        return "Back to previous track."

    # ── Volume ─────────────────────────────────────────────────────

    if name == "volume_step":
        delta = int(p.get("delta", 10))
        up = delta > 0
        if _IS_MAC:
            # Each key press = 1 notch (~6.25%). For ±10 we fire once.
            key = "111" if up else "103"
            steps = max(1, abs(delta) // 10)
            for _ in range(steps):
                _run(f'osascript -e \'tell application "System Events" to key code {key}\'')
        elif _IS_WIN:
            # Each SendKeys press ≈ 2%. For ±10 we fire 5 times.
            vk = 175 if up else 174   # VK_VOLUME_UP / VK_VOLUME_DOWN
            steps = max(1, abs(delta) // 2)
            for _ in range(steps):
                _win_sendkey(vk)
        else:
            sign = "+" if up else "-"
            _run(f"pactl set-sink-volume @DEFAULT_SINK@ {sign}{abs(delta)}%")
        return f"Volume {'up' if up else 'down'}."

    if name == "volume_set":
        level = max(0, min(100, int(p.get("level", 50))))
        if _IS_MAC:
            _run(f'osascript -e "set volume output volume {level}"')
        elif _IS_WIN:
            # nircmd gives exact control (nircmd.exe setsysvolume N, 0-65535).
            # Fall back to step-wise approximation if nircmd not in PATH.
            win_val = round(65535 * level / 100)
            code, _ = _run(f"nircmd.exe setsysvolume {win_val}")
            if code != 0:
                # Rough approximation: mute, then press vol-up N/2 times
                _win_sendkey(173)   # mute on
                _win_sendkey(173)   # mute off (reset to some level)
                for _ in range(level // 2):
                    _win_sendkey(175)
                logger.warning("volume_set: nircmd not found, used step approximation")
        else:
            _run(f"pactl set-sink-volume @DEFAULT_SINK@ {level}%")
        return f"Volume set to {level}%."

    if name == "mute_toggle":
        if _IS_MAC:
            _run('osascript -e "set volume output muted not (output muted of (get volume settings))"')
        elif _IS_WIN:
            _win_sendkey(173)   # VK_VOLUME_MUTE
        else:
            _run("pactl set-sink-mute @DEFAULT_SINK@ toggle")
        return "Mute toggled."

    # ── App launch ─────────────────────────────────────────────────

    if name == "open_app":
        app = p.get("app", "")
        if _IS_MAC:
            code, out = _run(f'open -a "{app}"')
        elif _IS_WIN:
            code, out = _run(f'start "" "{app}"')
        else:
            code, out = _run(f'xdg-open "{app}" 2>/dev/null || gtk-launch "{app}"')
        if code != 0:
            logger.warning("open_app failed app=%r out=%r", app, out)
            raise RuntimeError(f"open_app failed: {app!r} (code={code})")
        return f"Opening {app.title()}."

    # ── System ─────────────────────────────────────────────────────

    if name == "lock_screen":
        if _IS_MAC:
            _run('osascript -e \'tell application "System Events" to keystroke "q" using {command down, control down}\'')
        elif _IS_WIN:
            _run("rundll32.exe user32.dll,LockWorkStation")
        else:
            _run("loginctl lock-session 2>/dev/null || xdg-screensaver lock")
        return "Screen locked."

    if name == "sleep_system":
        if _IS_MAC:
            _run("pmset sleepnow")
        elif _IS_WIN:
            _run("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        else:
            _run("systemctl suspend 2>/dev/null || pm-suspend")
        return "Going to sleep."

    return f"Unknown action: {name}"


# ==========================================================
# IntentInterceptor
# ==========================================================


class IntentInterceptor:

    def match(self, normalized: str) -> Optional[QuickAction]:
        """
        Try to match a normalized command string against the pattern table.
        Returns None if no match or if the match is flagged ambiguous.
        Coref pronouns always fall through regardless of pattern match.
        """
        if not normalized:
            return None

        # Coref → Brain always
        if _COREF_RE.search(normalized):
            return None

        for pattern, builder in _PATTERNS:
            m = pattern.match(normalized)
            if m:
                action = builder(m)
                if action is not None:
                    logger.info("interceptor.match | cmd=%r action=%s", normalized, action.name)
                    return action

        return None

    def execute(self, action: QuickAction) -> tuple[str, bool]:
        """
        Execute action (and any chained actions).
        Returns (reply, success). On any exception returns (error_msg, False)
        so the caller can fall through to the full pipeline.
        """
        try:
            reply = _exec_action(action)
            for chained in action.chain:
                time.sleep(1.5)  # allow app to launch before sending media command
                reply = f"{reply} {_exec_action(chained)}"
            return reply.strip(), True
        except Exception as exc:
            logger.warning("interceptor.execute failed action=%s err=%r", action.name, exc)
            return str(exc), False


# Module-level singleton
interceptor = IntentInterceptor()
