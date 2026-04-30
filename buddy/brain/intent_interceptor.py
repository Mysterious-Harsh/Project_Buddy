# buddy/brain/intent_interceptor.py
#
# Fast-path interceptor — handles deterministic system actions without any LLM call.
# Sits at the top of handle_turn(). Returns None for anything ambiguous → Brain takes over.
#
# Supported intent categories:
#   Media      : play, pause, resume, toggle, next, prev, skip, play-on-app
#   Volume     : up, down, set, mute, max, min
#   Power      : sleep, hibernate, lock, shutdown, restart, logout
#   Display    : brightness up/down/set, dark mode, night mode/shift
#   Network    : wifi on/off/toggle, bluetooth on/off/toggle
#   Focus      : do not disturb on/off, focus mode
#   Screenshot : take screenshot
#   Quick Info : time, date, battery
#   Folders    : open downloads / desktop / documents / home
#   App launch : open / launch / start / restart <app>

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional
from urllib.parse import quote_plus

from buddy.logger.logger import get_logger

logger = get_logger("intent_interceptor")

_PLATFORM = sys.platform  # "darwin" | "linux" | "win32"
_IS_MAC = _PLATFORM == "darwin"
_IS_WIN = _PLATFORM == "win32"
_IS_LIN = _PLATFORM.startswith("linux")


# ══════════════════════════════════════════════════════════════════════════════
# §1  TEXT NORMALISATION
#     Goal: collapse every surface variation into a clean, punctuation-free,
#     lower-case string so patterns stay short and unambiguous.
# ══════════════════════════════════════════════════════════════════════════════

# Expand common contractions before stripping punctuation so e.g.
# "don't disturb" → "do not disturb" rather than "dont disturb".
_CONTRACTIONS: dict[str, str] = {
    "can't": "cannot",
    "can't": "cannot",
    "won't": "will not",
    "don't": "do not",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "i'd": "i would",
    "i'd": "i would",
    "i'm": "i am",
    "i've": "i have",
    "i'll": "i will",
    "let's": "let us",
    "let's": "let us",
    "that's": "that is",
    "it's": "it is",
    "what's": "what is",
    "there's": "there is",
}

# Filler prefixes — stripped iteratively so stacked forms work:
#   "hey buddy can you please just open spotify" → "open spotify"
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
    """Lowercase, Unicode-fold, expand contractions, strip filler, collapse spaces."""
    t = text.strip()
    # Unicode: café → cafe, curly quotes → straight, etc.
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = t.lower()
    # Expand contractions before stripping punctuation
    for src, dst in _CONTRACTIONS.items():
        t = t.replace(src, dst)
    t = _PUNCT_RE.sub(" ", t)
    # Iteratively strip stacked filler prefixes
    prev = None
    while prev != t:
        prev = t
        t = _PREFIX_RE.sub("", t).strip()
    t = _SUFFIX_RE.sub("", t).strip()
    return " ".join(t.split())


# ══════════════════════════════════════════════════════════════════════════════
# §2  AMBIGUITY DETECTION
#     Co-referential pronouns → always fall through to Brain.
# ══════════════════════════════════════════════════════════════════════════════

_COREF_RE = re.compile(
    r"\b(this|that|it|these|those|"
    r"the\s+(song|one|video|track|album|playlist|artist|file|app|thing))\b",
    re.IGNORECASE,
)

_GENERIC_PLAY = re.compile(
    r"^(music|something|anything|songs?|audio|some\s+music|a\s+song|some\s+songs?)$",
    re.IGNORECASE,
)

_ON_APP_RE = re.compile(r"\bon\s+\w+$", re.IGNORECASE)


def _play_is_ambiguous(after_play: str) -> bool:
    s = after_play.strip()
    if not s:
        return False  # bare "play" → toggle
    if _GENERIC_PLAY.match(s):
        return False
    if _ON_APP_RE.search(s):
        return False  # "Blinding Lights on Spotify" — handled by play_on_app
    return True  # specific content without app → Brain


# ══════════════════════════════════════════════════════════════════════════════
# §3  APP ALIAS TABLE
#     Maps casual / mistyped names → canonical app names per platform.
# ══════════════════════════════════════════════════════════════════════════════

# Platform-agnostic aliases (resolved before platform dispatch)
_APP_ALIASES: dict[str, str] = {
    # Browsers
    "chrome": "Google Chrome",
    "firefox": "Firefox",
    "safari": "Safari",
    "edge": "Microsoft Edge",
    "brave": "Brave Browser",
    # Music / media
    "spotify": "Spotify",
    "yt": "YouTube",
    "youtube": "YouTube",
    "ytm": "YouTube Music",
    "youtubemusic": "YouTube Music",
    "apple music": "Music",
    "itunes": "Music",
    "vlc": "VLC",
    "plex": "Plex",
    # Productivity
    "vscode": "Visual Studio Code",
    "code": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint",
    "ppt": "Microsoft PowerPoint",
    "onenote": "Microsoft OneNote",
    "notion": "Notion",
    "obsidian": "Obsidian",
    # Communication
    "slack": "Slack",
    "discord": "Discord",
    "teams": "Microsoft Teams",
    "zoom": "Zoom",
    "mail": "Mail",
    "outlook": "Microsoft Outlook",
    # System (macOS)
    "finder": "Finder",
    "terminal": "Terminal",
    "iterm": "iTerm",
    "iterm2": "iTerm",
    "activity monitor": "Activity Monitor",
    "system prefs": "System Preferences",
    "system settings": "System Settings",
    # System (Windows)
    "explorer": "explorer.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "task manager": "taskmgr.exe",
}


# Common folder shortcuts → absolute paths resolved at runtime
def _folder_path(name: str) -> str:
    home = os.path.expanduser("~")
    mapping = {
        "downloads": os.path.join(home, "Downloads"),
        "desktop": os.path.join(home, "Desktop"),
        "documents": os.path.join(home, "Documents"),
        "home": home,
        "pictures": os.path.join(home, "Pictures"),
        "music": os.path.join(home, "Music"),
        "videos": os.path.join(home, "Videos"),
        "movies": os.path.join(home, "Movies"),  # macOS alias
    }
    return mapping.get(name.lower(), os.path.join(home, name.capitalize()))


def _resolve_app(raw: str) -> str:
    """Return canonical app name, falling back to title-cased raw input."""
    return _APP_ALIASES.get(raw.strip().lower(), raw.strip())


# ══════════════════════════════════════════════════════════════════════════════
# §4  QuickAction
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class QuickAction:
    name: str
    params: dict = field(default_factory=dict)
    chain: List["QuickAction"] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════════
# §5  PATTERN TABLE
#     Ordered list of (compiled_re, builder_fn).
#     builder_fn(match) -> QuickAction | None
#     Return None to fall through even on a regex hit (ambiguous content).
#
#     ORDERING RULES (do not break):
#       1. Compound / more-specific patterns BEFORE their sub-patterns.
#          e.g. "open X and play" must come before plain "open X".
#       2. play_on_app BEFORE generic media_play.
#       3. volume/brightness set (with number) BEFORE directional (up/down).
# ══════════════════════════════════════════════════════════════════════════════


def _build_patterns() -> list[tuple]:
    P = re.compile

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _pct(m, group="n") -> int:
        """Parse percentage group, clamped 0–100."""
        return max(0, min(100, int(m.group(group))))

    # ── Builder functions ────────────────────────────────────────────────────

    # Media
    def media_toggle(m):
        return QuickAction("media_toggle")

    def media_pause(m):
        return QuickAction("media_pause")

    def media_next(m):
        return QuickAction("media_next")

    def media_prev(m):
        return QuickAction("media_prev")

    def media_play(m):
        after = (m.group("after") or "").strip()
        if _play_is_ambiguous(after):
            return None
        return QuickAction("media_play")

    def play_on_app(m):
        song = (m.group("song") or "").strip()
        app = (m.group("app") or "").strip().lower()
        if not song or not app:
            return None
        return QuickAction("play_on_app", {"song": song, "app": app})

    def restart_app(m):
        raw = (m.group("app") or "").strip()
        if not raw:
            return None
        return QuickAction("restart_app", {"app": _resolve_app(raw)})

    # Volume
    def volume_up(m):
        return QuickAction("volume_step", {"delta": +10})

    def volume_down(m):
        return QuickAction("volume_step", {"delta": -10})

    def volume_max(m):
        return QuickAction("volume_set", {"level": 100})

    def volume_min(m):
        return QuickAction("volume_set", {"level": 0})

    def volume_set(m):
        return QuickAction("volume_set", {"level": _pct(m)})

    def mute(m):
        return QuickAction("mute_toggle")

    # App launch / folders
    def open_app(m):
        raw = (m.group("app") or "").strip()
        if not raw:
            return None
        return QuickAction("open_app", {"app": _resolve_app(raw)})

    def open_folder(m):
        name = (m.group("folder") or "").strip().lower()
        return QuickAction("open_folder", {"path": _folder_path(name), "name": name})

    def open_and_play(m):
        raw = (m.group("app") or "").strip()
        if not raw:
            return None
        return QuickAction(
            "open_app",
            {"app": _resolve_app(raw)},
            chain=[QuickAction("media_play")],
        )

    # Power
    def lock(m):
        return QuickAction("lock_screen")

    def sleep_sys(m):
        return QuickAction("sleep_system")

    def hibernate(m):
        return QuickAction("hibernate_system")

    def shutdown(m):
        return QuickAction("shutdown_system")

    def restart(m):
        return QuickAction("restart_system")

    def logout(m):
        return QuickAction("logout_system")

    # Display / Brightness
    def brightness_up(m):
        return QuickAction("brightness_step", {"delta": +10})

    def brightness_down(m):
        return QuickAction("brightness_step", {"delta": -10})

    def brightness_set(m):
        return QuickAction("brightness_set", {"level": _pct(m)})

    def dark_mode_on(m):
        return QuickAction("dark_mode", {"state": "on"})

    def dark_mode_off(m):
        return QuickAction("dark_mode", {"state": "off"})

    def dark_mode_toggle(m):
        return QuickAction("dark_mode", {"state": "toggle"})

    def night_mode_on(m):
        return QuickAction("night_mode", {"state": "on"})

    def night_mode_off(m):
        return QuickAction("night_mode", {"state": "off"})

    # Network
    def wifi_on(m):
        return QuickAction("wifi", {"state": "on"})

    def wifi_off(m):
        return QuickAction("wifi", {"state": "off"})

    def wifi_toggle(m):
        return QuickAction("wifi", {"state": "toggle"})

    def bt_on(m):
        return QuickAction("bluetooth", {"state": "on"})

    def bt_off(m):
        return QuickAction("bluetooth", {"state": "off"})

    def bt_toggle(m):
        return QuickAction("bluetooth", {"state": "toggle"})

    # Focus / DND
    def dnd_on(m):
        return QuickAction("do_not_disturb", {"state": "on"})

    def dnd_off(m):
        return QuickAction("do_not_disturb", {"state": "off"})

    # Screenshot
    def screenshot(m):
        return QuickAction("screenshot")

    # Quick info
    def tell_time(m):
        return QuickAction("tell_time")

    def tell_date(m):
        return QuickAction("tell_date")

    def tell_battery(m):
        return QuickAction("tell_battery")

    # ── Pattern table (ordered — do NOT reorder without reading §5 header) ──

    return [
        # ── Compound: open X and play  [must precede plain open X] ──────────
        (P(r"^open\s+(?P<app>[\w\s]+?)\s+and\s+play$", re.I), open_and_play),
        # ── Restart app  [must precede open/launch] ──────────────────────────
        (P(r"^restart\s+(?P<app>[\w\s]+)$", re.I), restart_app),
        # ── Folder shortcuts  [must precede generic open_app] ────────────────
        (
            P(
                r"^open\s+(?:my\s+)?(?P<folder>"
                r"downloads?|desktop|documents?|home(\s+folder)?|"
                r"pictures?|music|videos?|movies?)$",
                re.I,
            ),
            open_folder,
        ),
        # ── App launch ───────────────────────────────────────────────────────
        (P(r"^open\s+(?P<app>[\w\s]+)$", re.I), open_app),
        (P(r"^launch\s+(?P<app>[\w\s]+)$", re.I), open_app),
        (P(r"^start\s+(?P<app>[\w\s]+)$", re.I), open_app),
        # ── Media: play on app  [must precede generic media_play] ────────────
        (P(r"^play\s+(?P<song>.+?)\s+on\s+(?P<app>\w+)$", re.I), play_on_app),
        # ── Media: play / pause / resume / toggle / next / prev ──────────────
        (P(r"^play\s*(?P<after>.*)$", re.I), media_play),
        (P(r"^(pause|stop\s+music|stop\s+playback)$", re.I), media_pause),
        (P(r"^(resume|continue\s+music|continue\s+playing)$", re.I), media_play),
        (P(r"^(play\s*pause|toggle\s+(music|playback))$", re.I), media_toggle),
        (P(r"^next(\s+(track|song))?$", re.I), media_next),
        (P(r"^(previous|prev)(\s+(track|song))?$", re.I), media_prev),
        (P(r"^skip(\s+(track|song))?$", re.I), media_next),
        # ── Volume: set (number)  [must precede directional] ─────────────────
        (P(r"^volume\s+(?P<n>\d{1,3})(%)?$", re.I), volume_set),
        (P(r"^set\s+volume\s+(to\s+)?(?P<n>\d{1,3})(%)?$", re.I), volume_set),
        (P(r"^(volume|set\s+volume)\s+(to\s+)?(max|maximum|full)$", re.I), volume_max),
        (
            P(r"^(volume|set\s+volume)\s+(to\s+)?(min|minimum|zero|silent)$", re.I),
            volume_min,
        ),
        # ── Volume: directional ───────────────────────────────────────────────
        (P(r"^volume\s+(up|louder|increase)$", re.I), volume_up),
        (P(r"^volume\s+(down|lower|quieter|decrease|softer)$", re.I), volume_down),
        (P(r"^(turn\s+up|louder|increase\s+volume)$", re.I), volume_up),
        (
            P(
                r"^(turn\s+(the\s+)?volume\s+down|lower\s+volume|decrease\s+volume|quieter)$",
                re.I,
            ),
            volume_down,
        ),
        (P(r"^(mute|unmute|toggle\s+mute)$", re.I), mute),
        # ── Power ─────────────────────────────────────────────────────────────
        (P(r"^lock(\s+(screen|my\s+screen|the\s+screen))?$", re.I), lock),
        (P(r"^(sleep|put\s+(the\s+)?computer\s+to\s+sleep)$", re.I), sleep_sys),
        (P(r"^(hibernate|suspend\s+to\s+disk)$", re.I), hibernate),
        (
            P(
                r"^(shut\s+down|shutdown|power\s+off|"
                r"turn\s+off(\s+(the\s+)?(computer|pc|mac|machine))?)$",
                re.I,
            ),
            shutdown,
        ),
        (
            P(r"^(restart|reboot)(\s+(the\s+)?(computer|pc|mac|machine))?$", re.I),
            restart,
        ),
        (
            P(
                r"^(log\s+out|logout|sign\s+out)(\s+(of\s+)?(this\s+)?(computer|session))?$",
                re.I,
            ),
            logout,
        ),
        # ── Brightness: set (number)  [must precede directional] ──────────────
        (P(r"^set\s+brightness\s+(to\s+)?(?P<n>\d{1,3})(%)?$", re.I), brightness_set),
        (P(r"^brightness\s+(?P<n>\d{1,3})(%)?$", re.I), brightness_set),
        # ── Brightness: directional ───────────────────────────────────────────
        (P(r"^brightness\s+(up|increase|higher|more)$", re.I), brightness_up),
        (P(r"^brightness\s+(down|decrease|lower|less|dim)$", re.I), brightness_down),
        (P(r"^(increase|raise)\s+brightness$", re.I), brightness_up),
        (
            P(r"^(decrease|lower|dim|reduce)\s+(the\s+)?brightness$", re.I),
            brightness_down,
        ),
        (
            P(r"^(dim\s+the\s+screen|make\s+(it|screen)\s+dimmer)$", re.I),
            brightness_down,
        ),
        (P(r"^(make\s+(it|screen)\s+brighter|brighter)$", re.I), brightness_up),
        # ── Dark / Night mode ─────────────────────────────────────────────────
        (
            P(r"^(enable|turn\s+on|switch\s+to|activate)\s+dark\s+mode$", re.I),
            dark_mode_on,
        ),
        (
            P(r"^(disable|turn\s+off|switch\s+off|deactivate)\s+dark\s+mode$", re.I),
            dark_mode_off,
        ),
        (P(r"^(toggle|switch)\s+dark\s+mode$", re.I), dark_mode_toggle),
        (
            P(r"^(enable|turn\s+on|switch\s+to|activate)\s+light\s+mode$", re.I),
            dark_mode_off,
        ),
        (
            P(r"^(enable|turn\s+on|activate)\s+(night\s+mode|night\s+shift)$", re.I),
            night_mode_on,
        ),
        (
            P(
                r"^(disable|turn\s+off|deactivate)\s+(night\s+mode|night\s+shift)$",
                re.I,
            ),
            night_mode_off,
        ),
        # ── Wi-Fi ─────────────────────────────────────────────────────────────
        (P(r"^(turn\s+on|enable|connect)\s+(wi\s*fi|wifi|wireless)$", re.I), wifi_on),
        (
            P(r"^(turn\s+off|disable|disconnect)\s+(wi\s*fi|wifi|wireless)$", re.I),
            wifi_off,
        ),
        (P(r"^(toggle|switch)\s+(wi\s*fi|wifi|wireless)$", re.I), wifi_toggle),
        (
            P(r"^(wi\s*fi|wifi)\s+(on|off|toggle)$", re.I),
            lambda m: {"on": wifi_on, "off": wifi_off, "toggle": wifi_toggle}[
                m.group(2).lower()
            ](m),
        ),
        # ── Bluetooth ─────────────────────────────────────────────────────────
        (P(r"^(turn\s+on|enable)\s+(bluetooth|bt)$", re.I), bt_on),
        (P(r"^(turn\s+off|disable)\s+(bluetooth|bt)$", re.I), bt_off),
        (P(r"^(toggle|switch)\s+(bluetooth|bt)$", re.I), bt_toggle),
        (
            P(r"^(bluetooth|bt)\s+(on|off|toggle)$", re.I),
            lambda m: {"on": bt_on, "off": bt_off, "toggle": bt_toggle}[
                m.group(2).lower()
            ](m),
        ),
        # ── Focus / Do Not Disturb ────────────────────────────────────────────
        (
            P(
                r"^(enable|turn\s+on|activate|start)\s+"
                r"(do\s+not\s+disturb|dnd|focus(\s+mode)?)$",
                re.I,
            ),
            dnd_on,
        ),
        (
            P(
                r"^(disable|turn\s+off|deactivate|stop)\s+"
                r"(do\s+not\s+disturb|dnd|focus(\s+mode)?)$",
                re.I,
            ),
            dnd_off,
        ),
        (
            P(r"^do\s+not\s+disturb\s+(on|off)$", re.I),
            lambda m: dnd_on(m) if m.group(1).lower() == "on" else dnd_off(m),
        ),
        # ── Screenshot ────────────────────────────────────────────────────────
        (
            P(
                r"^(take\s+(a\s+)?|grab\s+(a\s+)?)?screenshot(\s+(now|the\s+screen))?$",
                re.I,
            ),
            screenshot,
        ),
        (P(r"^screenshot(\s+the\s+screen)?$", re.I), screenshot),
        # ── Quick info ────────────────────────────────────────────────────────
        (
            P(
                r"^(what(\s+is|'s)\s+(the\s+)?time|what\s+time\s+is\s+it|current\s+time)$",
                re.I,
            ),
            tell_time,
        ),
        (
            P(
                r"^(what(\s+is|'s)\s+(today|the\s+date)|what\s+day\s+is\s+it|today'?s\s+date)$",
                re.I,
            ),
            tell_date,
        ),
        (
            P(
                r"^(battery(\s+level)?|how\s+much\s+battery(\s+(is\s+left|do\s+i\s+have))?)$",
                re.I,
            ),
            tell_battery,
        ),
    ]


_PATTERNS = _build_patterns()


# ══════════════════════════════════════════════════════════════════════════════
# §6  PLATFORM HELPERS
# ══════════════════════════════════════════════════════════════════════════════


def _run(cmd: str, timeout: int = 5) -> tuple[int, str]:
    """Run a shell command; return (returncode, combined_output)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as exc:
        return -1, str(exc)


def _win_sendkey(vk: int) -> None:
    """Send a virtual key via PowerShell + WScript.Shell."""
    _run(
        f"powershell -NoProfile -Command "
        f'"(New-Object -ComObject WScript.Shell).SendKeys([char]{vk})"'
    )


def _linux_playerctl(cmd: str) -> tuple[int, str]:
    if not shutil.which("playerctl"):
        return -1, "playerctl not installed (apt/pacman/dnf install playerctl)"
    return _run(f"playerctl {cmd}")


# macOS media app registry (app_name → cmd_map)
_MAC_MEDIA_APPS: dict[str, dict[str, str]] = {
    "Spotify": {
        "play": "play",
        "pause": "pause",
        "toggle": "playpause",
        "next": "next track",
        "prev": "previous track",
    },
    "Music": {
        "play": "play",
        "pause": "pause",
        "toggle": "playpause",
        "next": "next track",
        "prev": "previous track",
    },
    "TV": {
        "play": "play",
        "pause": "pause",
        "toggle": "play",
        "next": "next chapter",
        "prev": "previous chapter",
    },
    "Plex Media Player": {
        "play": "play",
        "pause": "pause",
        "toggle": "play",
        "next": "next chapter",
        "prev": "previous chapter",
    },
}

# key codes: play=100, toggle=100, next=101, prev=98
_MAC_MEDIA_KEY: dict[str, int] = {
    "play": 100,
    "pause": 100,
    "toggle": 100,
    "next": 101,
    "prev": 98,
}


def _mac_running_media_app() -> str | None:
    for app_name in _MAC_MEDIA_APPS:
        code, out = _run(f"osascript -e 'application \"{app_name}\" is running'")
        if code == 0 and "true" in out.lower():
            return app_name
    return None


def _mac_media(cmd: str) -> None:
    app = _mac_running_media_app()
    if app:
        app_cmd = _MAC_MEDIA_APPS[app].get(cmd, cmd)
        _run(f"osascript -e 'tell application \"{app}\" to {app_cmd}'")
        return
    key = _MAC_MEDIA_KEY.get(cmd, 100)
    _run(f"osascript -e 'tell application \"System Events\" to key code {key}'")


def _wait_for_app(app_name: str, timeout: float = 5.0, interval: float = 0.3) -> bool:
    """Poll until app_name is running or timeout elapses. macOS only."""
    if not _IS_MAC:
        time.sleep(1.5)
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, out = _run(f"osascript -e 'application \"{app_name}\" is running'")
        if code == 0 and "true" in out.lower():
            return True
        time.sleep(interval)
    return False


def _yt_resolve(song: str) -> str | None:
    if not shutil.which("yt-dlp"):
        return None
    code, out = _run(f'yt-dlp "ytsearch1:{song}" --get-id --no-playlist', timeout=15)
    vid = out.strip().split()[0] if code == 0 and out.strip() else ""
    return f"https://www.youtube.com/watch?v={vid}&autoplay=1" if vid else None


# ══════════════════════════════════════════════════════════════════════════════
# §7  ACTION REGISTRY
#     Each handler: (params: dict) -> str
#     Registered with @_action("name").  _exec_action dispatches via dict.
# ══════════════════════════════════════════════════════════════════════════════

_ACTION_REGISTRY: dict[str, Callable[[dict], str]] = {}


def _action(name: str):
    """Decorator: register a function as the handler for action `name`."""

    def decorator(fn: Callable[[dict], str]):
        _ACTION_REGISTRY[name] = fn
        return fn

    return decorator


# ── Media ────────────────────────────────────────────────────────────────────


@_action("media_play")
def _media_play(p):
    if _IS_MAC:
        _mac_media("play")
    elif _IS_WIN:
        _win_sendkey(179)
    else:
        code, err = _linux_playerctl("play")
        if code != 0:
            raise RuntimeError(err)
    return "Playing."


@_action("media_pause")
def _media_pause(p):
    if _IS_MAC:
        _mac_media("pause")
    elif _IS_WIN:
        _win_sendkey(179)
    else:
        code, err = _linux_playerctl("pause")
        if code != 0:
            raise RuntimeError(err)
    return "Paused."


@_action("media_toggle")
def _media_toggle(p):
    if _IS_MAC:
        _mac_media("toggle")
    elif _IS_WIN:
        _win_sendkey(179)
    else:
        code, err = _linux_playerctl("play-pause")
        if code != 0:
            raise RuntimeError(err)
    return "Toggled playback."


@_action("media_next")
def _media_next(p):
    if _IS_MAC:
        _mac_media("next")
    elif _IS_WIN:
        _win_sendkey(176)  # VK_MEDIA_NEXT_TRACK
    else:
        code, err = _linux_playerctl("next")
        if code != 0:
            raise RuntimeError(err)
    return "Skipped to next track."


@_action("media_prev")
def _media_prev(p):
    if _IS_MAC:
        _mac_media("prev")
    elif _IS_WIN:
        _win_sendkey(177)  # VK_MEDIA_PREV_TRACK
    else:
        code, err = _linux_playerctl("previous")
        if code != 0:
            raise RuntimeError(err)
    return "Back to previous track."


@_action("play_on_app")
def _play_on_app(p):
    song = p.get("song", "")
    app = p.get("app", "").lower()
    song_encoded = quote_plus(song)
    deeplinks = {
        "spotify": f"spotify:search:{song_encoded}",
        "music": f"music://search?term={song_encoded}",
        "youtubemusic": f"https://music.youtube.com/search?q={song_encoded}",
        "youtube music": f"https://music.youtube.com/search?q={song_encoded}",
    }
    if app in ("youtube", "yt"):
        link = (
            _yt_resolve(song)
            or f"https://www.youtube.com/results?search_query={song_encoded}"
        )
    else:
        link = deeplinks.get(app)
    if _IS_MAC:
        if link:
            code, err = _run(f'open "{link}"')
            if code != 0:
                raise RuntimeError(f"Could not open {app.title()}: {err}")
        else:
            canonical = _resolve_app(app)
            code, err = _run(f'open -a "{canonical}"')
            if code != 0:
                raise RuntimeError(f"App not found: {canonical!r}")
            _wait_for_app(canonical)
            _mac_media("play")
    elif _IS_WIN:
        target = link if link else app
        _run(f'start "" "{target}"')
    else:
        target = link if link else app
        code, err = _run(f'xdg-open "{target}"')
        if code != 0:
            raise RuntimeError(f"xdg-open failed: {err}")
    return f'Playing "{song}" on {app.title()}.'


# ── Volume ────────────────────────────────────────────────────────────────────


@_action("volume_step")
def _volume_step(p):
    delta = int(p.get("delta", 10))
    up = delta > 0
    if _IS_MAC:
        key = "111" if up else "103"
        steps = max(1, abs(delta) // 10)
        for _ in range(steps):
            _run(f"osascript -e 'tell application \"System Events\" to key code {key}'")
    elif _IS_WIN:
        vk = 175 if up else 174  # VK_VOLUME_UP / VK_VOLUME_DOWN
        steps = max(1, abs(delta) // 2)
        for _ in range(steps):
            _win_sendkey(vk)
    else:
        sign = "+" if up else "-"
        _run(f"pactl set-sink-volume @DEFAULT_SINK@ {sign}{abs(delta)}%")
    return f"Volume {'up' if up else 'down'}."


@_action("volume_set")
def _volume_set(p):
    level = max(0, min(100, int(p.get("level", 50))))
    if _IS_MAC:
        _run(f'osascript -e "set volume output volume {level}"')
    elif _IS_WIN:
        win_val = round(65535 * level / 100)
        code, _ = _run(f"nircmd.exe setsysvolume {win_val}")
        if code != 0:
            logger.warning("volume_set: nircmd not found, using step approximation")
            _win_sendkey(173)
            _win_sendkey(173)
            for _ in range(level // 2):
                _win_sendkey(175)
    else:
        _run(f"pactl set-sink-volume @DEFAULT_SINK@ {level}%")
    return f"Volume set to {level}%."


@_action("mute_toggle")
def _mute_toggle(p):
    if _IS_MAC:
        _run(
            'osascript -e "set volume output muted not (output muted of (get volume'
            ' settings))"'
        )
    elif _IS_WIN:
        _win_sendkey(173)  # VK_VOLUME_MUTE
    else:
        _run("pactl set-sink-mute @DEFAULT_SINK@ toggle")
    return "Mute toggled."


# ── App launch / restart / folders ───────────────────────────────────────────


@_action("open_app")
def _open_app(p):
    app = p.get("app", "")
    if _IS_MAC:
        code, out = _run(f'open -a "{app}"')
    elif _IS_WIN:
        code, out = _run(f"powershell -NoProfile -Command \"Start-Process '{app}'\"")
    else:
        code, out = _run(f'xdg-open "{app}"')
        if code != 0:
            code, out = _run(f'gtk-launch "{app}"')
    if code != 0:
        logger.warning("open_app failed app=%r out=%r", app, out)
        raise RuntimeError(f"App not found: {app!r}")
    return f"Opening {app.title()}."


@_action("restart_app")
def _restart_app(p):
    app = p.get("app", "")
    if _IS_MAC:
        _run(f"osascript -e 'tell application \"{app}\" to quit'")
        time.sleep(1.0)
        code, out = _run(f'open -a "{app}"')
        if code != 0:
            raise RuntimeError(f"Could not relaunch {app!r}: {out}")
    elif _IS_WIN:
        _run(f'taskkill /IM "{app}.exe" /F')
        time.sleep(0.5)
        _run(f"powershell -NoProfile -Command \"Start-Process '{app}'\"")
    else:
        # Try playerctl stop then xdg-open as best-effort
        _run(f'pkill -f "{app}"')
        time.sleep(0.5)
        _run(f'xdg-open "{app}"')
    return f"Restarting {app.title()}."


@_action("open_folder")
def _open_folder(p):
    path = p.get("path", os.path.expanduser("~"))
    name = p.get("name", "folder").title()
    if _IS_MAC:
        code, out = _run(f'open "{path}"')
    elif _IS_WIN:
        code, out = _run(f'explorer "{path}"')
    else:
        code, out = _run(f'xdg-open "{path}"')
    if code != 0:
        raise RuntimeError(f"Could not open {name}: {out}")
    return f"Opening {name}."


# ── Power ─────────────────────────────────────────────────────────────────────


@_action("lock_screen")
def _lock_screen(p):
    if _IS_MAC:
        _run(
            'osascript -e \'tell application "System Events" to keystroke "q" using'
            " {command down, control down}'"
        )
    elif _IS_WIN:
        _run("rundll32.exe user32.dll,LockWorkStation")
    else:
        _run("loginctl lock-session 2>/dev/null || xdg-screensaver lock")
    return "Screen locked."


@_action("sleep_system")
def _sleep_system(p):
    if _IS_MAC:
        _run("pmset sleepnow")
    elif _IS_WIN:
        _run("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
    else:
        _run("systemctl suspend 2>/dev/null || pm-suspend")
    return "Going to sleep."


@_action("hibernate_system")
def _hibernate_system(p):
    if _IS_MAC:
        # macOS doesn't have true hibernate; use Safe Sleep mode 25 (hibernation)
        _run("pmset -a hibernatemode 25 && pmset sleepnow")
    elif _IS_WIN:
        _run("shutdown /h")
    else:
        _run("systemctl hibernate 2>/dev/null || pm-hibernate")
    return "Hibernating."


@_action("shutdown_system")
def _shutdown_system(p):
    if _IS_MAC:
        _run("osascript -e 'tell application \"System Events\" to shut down'")
    elif _IS_WIN:
        _run("shutdown /s /t 0")
    else:
        _run("systemctl poweroff 2>/dev/null || shutdown -h now")
    return "Shutting down."


@_action("restart_system")
def _restart_system(p):
    if _IS_MAC:
        _run("osascript -e 'tell application \"System Events\" to restart'")
    elif _IS_WIN:
        _run("shutdown /r /t 0")
    else:
        _run("systemctl reboot 2>/dev/null || shutdown -r now")
    return "Restarting."


@_action("logout_system")
def _logout_system(p):
    if _IS_MAC:
        _run("osascript -e 'tell application \"System Events\" to log out'")
    elif _IS_WIN:
        _run("shutdown /l")
    else:
        _run(
            "loginctl terminate-user $USER 2>/dev/null || gnome-session-quit"
            " --no-prompt"
        )
    return "Logging out."


# ── Display / Brightness ──────────────────────────────────────────────────────


@_action("brightness_step")
def _brightness_step(p):
    delta = int(p.get("delta", 10))
    up = delta > 0
    if _IS_MAC:
        key = "144" if up else "145"  # F2 up / F1 down (key codes)
        steps = max(1, abs(delta) // 10)
        for _ in range(steps):
            _run(f"osascript -e 'tell application \"System Events\" to key code {key}'")
    elif _IS_WIN:
        # Requires NirCmd: nircmd.exe changebrightness +10 / -10
        sign = "+" if up else "-"
        code, _ = _run(f"nircmd.exe changebrightness {sign}{abs(delta)}")
        if code != 0:
            # Fallback: PowerShell WMI (slower but no deps)
            _run(
                'powershell -NoProfile -Command "'
                "$b=(Get-WmiObject -Namespace root/wmi -Class"
                " WmiMonitorBrightness).CurrentBrightness;"
                "(Get-WmiObject -Namespace root/wmi -Class"
                f' WmiMonitorBrightnessMethods).WmiSetBrightness(1,[Math]::Min(100,[Math]::Max(0,$b{("+" if up else "-")}{abs(delta)})))"'
            )
    else:
        # Requires brightnessctl (preferred) or xrandr fallback
        if shutil.which("brightnessctl"):
            sign = "+" if up else "-"
            _run(f"brightnessctl set {abs(delta)}%{sign}")
        else:
            code, out = _run("xrandr --verbose | awk '/Brightness/{print $2; exit}'")
            current = float(out) if code == 0 and out else 1.0
            new_val = max(0.0, min(1.0, current + (delta / 100.0)))
            display_name_code, display_name = _run(
                "xrandr | awk '/ connected/{print $1; exit}'"
            )
            if display_name:
                _run(f"xrandr --output {display_name} --brightness {new_val:.2f}")
    return f"Brightness {'up' if up else 'down'}."


@_action("brightness_set")
def _brightness_set(p):
    level = max(0, min(100, int(p.get("level", 50))))
    if _IS_MAC:
        # AppleScript brightness: 0.0–1.0
        val = level / 100.0
        _run(
            "osascript -e 'tell application \"System Preferences\" to quit'"
            ' 2>/dev/null; osascript -e \'tell application "System Events" to set'
            f" brightness of screen 1 to {val}' 2>/dev/null || osascript -e 'do shell"
            f' script "brightness {val:.2f}"\''
        )
    elif _IS_WIN:
        win_val = round(65535 * level / 100)
        code, _ = _run(f"nircmd.exe setbrightness {level}")
        if code != 0:
            _run(
                'powershell -NoProfile -Command "(Get-WmiObject -Namespace root/wmi'
                f' -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})"'
            )
    else:
        if shutil.which("brightnessctl"):
            _run(f"brightnessctl set {level}%")
        else:
            val = level / 100.0
            code, display_name = _run("xrandr | awk '/ connected/{print $1; exit}'")
            if display_name:
                _run(f"xrandr --output {display_name} --brightness {val:.2f}")
    return f"Brightness set to {level}%."


@_action("dark_mode")
def _dark_mode(p):
    state = p.get("state", "toggle")
    if _IS_MAC:
        if state == "toggle":
            _run(
                'osascript -e \'tell application "System Events" to '
                "tell appearance preferences to set dark mode to not dark mode'"
            )
            return "Dark mode toggled."
        val = "true" if state == "on" else "false"
        _run(
            f'osascript -e \'tell application "System Events" to '
            f"tell appearance preferences to set dark mode to {val}'"
        )
    elif _IS_WIN:
        reg_val = "0" if state == "on" else "1"  # 0=dark, 1=light in registry
        if state == "toggle":
            code, out = _run(
                "reg query"
                ' "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"'
                " /v AppsUseLightTheme"
            )
            current = "1" in (out or "")
            reg_val = "0" if current else "1"
        _run(
            "reg add"
            ' "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"'
            f" /v AppsUseLightTheme /t REG_DWORD /d {reg_val} /f"
        )
    else:
        # GNOME / KDE best-effort
        if state in ("on", "toggle"):
            _run(
                "gsettings set org.gnome.desktop.interface color-scheme 'prefer-dark'"
                " 2>/dev/null || lookandfeeltool -a org.kde.breezedark.desktop"
                " 2>/dev/null"
            )
        else:
            _run(
                "gsettings set org.gnome.desktop.interface color-scheme 'default'"
                " 2>/dev/null || lookandfeeltool -a org.kde.breeze.desktop 2>/dev/null"
            )
    label = {"on": "enabled", "off": "disabled", "toggle": "toggled"}[state]
    return f"Dark mode {label}."


@_action("night_mode")
def _night_mode(p):
    state = p.get("state", "on")
    on = state == "on"
    if _IS_MAC:
        val = "true" if on else "false"
        _run(
            f'osascript -e \'tell application "System Events" to '
            f"tell appearance preferences to set night shift enabled to {val}'"
            " 2>/dev/null"
        )
    elif _IS_WIN:
        # Night light via registry (requires sign-out to fully apply on some builds)
        # Best available without third-party tools: toggle via PowerShell
        action = "Enable" if on else "Disable"
        _run(
            f'powershell -NoProfile -Command "'
            f"$p='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CloudStore\\Store\\"
            f"DefaultAccount\\Current\\default$windows.data.bluelightreduction.bluelightreductionstate\\"
            f"windows.data.bluelightreduction.bluelightreductionstate';"
            f'if(Test-Path $p){{Remove-Item $p -Force}}"'
        )
    else:
        if on:
            _run("redshift -O 4500 2>/dev/null || gammastep -O 4500 2>/dev/null")
        else:
            _run("redshift -x 2>/dev/null || gammastep -x 2>/dev/null")
    label = "enabled" if on else "disabled"
    return f"Night mode {label}."


# ── Network ───────────────────────────────────────────────────────────────────


@_action("wifi")
def _wifi(p):
    state = p.get("state", "toggle")
    if _IS_MAC:
        if state == "toggle":
            code, out = _run("networksetup -getairportpower en0")
            current_on = "on" in out.lower()
            state = "off" if current_on else "on"
        val = "on" if state == "on" else "off"
        _run(f"networksetup -setairportpower en0 {val}")
    elif _IS_WIN:
        if state == "toggle":
            code, out = _run('netsh interface show interface | findstr /i "wi-fi"')
            current_on = "connected" in out.lower() or "enabled" in out.lower()
            state = "disable" if current_on else "enable"
        else:
            state = "enable" if state == "on" else "disable"
        _run(f'netsh interface set interface "Wi-Fi" {state}')
    else:
        if state == "toggle":
            code, out = _run("nmcli radio wifi")
            current_on = "enabled" in out.lower()
            state = "off" if current_on else "on"
        _run(f"nmcli radio wifi {state}")
    return f"Wi-Fi {'on' if state in ('on', 'enable') else 'off'}."


@_action("bluetooth")
def _bluetooth(p):
    state = p.get("state", "toggle")
    if _IS_MAC:
        # Requires blueutil: brew install blueutil
        if shutil.which("blueutil"):
            if state == "toggle":
                _run("blueutil --toggle")
            else:
                _run(f"blueutil --{'power 1' if state == 'on' else 'power 0'}")
        else:
            logger.warning("bluetooth: blueutil not installed (brew install blueutil)")
            raise RuntimeError(
                "blueutil not found. Install with: brew install blueutil"
            )
    elif _IS_WIN:
        action = "1" if state in ("on", "toggle") else "0"
        _run(
            f'powershell -NoProfile -Command "'
            f'$bt=[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime];"'
        )
        # Simpler: use DevCon or bttoggle
        _run(
            'powershell -NoProfile -Command "'
            "Get-PnpDevice -Class Bluetooth | "
            f'{"Enable" if state == "on" else "Disable"}-PnpDevice -Confirm:$false"'
        )
    else:
        if shutil.which("bluetoothctl"):
            if state == "toggle":
                code, out = _run("bluetoothctl show | grep 'Powered'")
                current_on = "yes" in out.lower()
                state = "off" if current_on else "on"
            _run(f"bluetoothctl power {'on' if state == 'on' else 'off'}")
        else:
            _run(f"rfkill {'unblock' if state == 'on' else 'block'} bluetooth")
    return f"Bluetooth {'on' if state == 'on' else 'off'}."


# ── Focus / DND ───────────────────────────────────────────────────────────────


@_action("do_not_disturb")
def _do_not_disturb(p):
    on = p.get("state", "on") == "on"
    if _IS_MAC:
        # macOS 12+: use Focus via shortcuts / AppleScript (limited API)
        # Best available: toggle via defaults + killall
        val = "true" if on else "false"
        _run(
            "defaults -currentHost write com.apple.notificationcenterui doNotDisturb"
            f" -bool {val} && "
            f"killall NotificationCenter 2>/dev/null; "
            f'osascript -e \'tell application "System Events" to '
            f"set doNotDisturb of appearance preferences to {val}' 2>/dev/null"
        )
    elif _IS_WIN:
        # Windows Focus Assist via registry
        reg_val = "1" if on else "0"
        _run(
            "reg add"
            ' "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CloudStore\\Store\\DefaultAccount\\Current\\default$windows.data.notifications.quiethours\\"windows.data.notifications.quiethours"'
            f" /v Data /t REG_BINARY /d {reg_val} /f 2>nul"
        )
    else:
        # GNOME: toggle via dconf
        val = "true" if on else "false"
        _run(
            "gsettings set org.gnome.desktop.notifications show-banners"
            f" {('false' if on else 'true')} 2>/dev/null"
        )
    return f"Do Not Disturb {'enabled' if on else 'disabled'}."


# ── Screenshot ────────────────────────────────────────────────────────────────


@_action("screenshot")
def _screenshot(p):
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = os.path.join(os.path.expanduser("~"), "Desktop", f"screenshot_{ts}.png")
    if _IS_MAC:
        code, out = _run(f'screencapture -x "{dest}"')
        if code != 0:
            raise RuntimeError(f"screencapture failed: {out}")
    elif _IS_WIN:
        # Use PowerShell + .NET (no external deps)
        code, out = _run(
            f'powershell -NoProfile -Command "'
            f"Add-Type -AssemblyName System.Windows.Forms;"
            f"$bmp=[System.Drawing.Bitmap]::new([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,"
            f"[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height);"
            f"$g=[System.Drawing.Graphics]::FromImage($bmp);"
            f"$g.CopyFromScreen(0,0,0,0,$bmp.Size);"
            f"$bmp.Save('{dest}');\""
        )
        if code != 0:
            raise RuntimeError(f"Screenshot failed: {out}")
    else:
        if shutil.which("scrot"):
            code, out = _run(f'scrot "{dest}"')
        elif shutil.which("gnome-screenshot"):
            code, out = _run(f'gnome-screenshot -f "{dest}"')
        elif shutil.which("import"):  # ImageMagick
            code, out = _run(f'import -window root "{dest}"')
        else:
            raise RuntimeError(
                "No screenshot tool found (install scrot or gnome-screenshot)"
            )
        if code != 0:
            raise RuntimeError(f"Screenshot failed: {out}")
    return f"Screenshot saved to Desktop as screenshot_{ts}.png."


# ── Quick Info ────────────────────────────────────────────────────────────────


@_action("tell_time")
def _tell_time(p):
    return f"It's {datetime.now().strftime('%I:%M %p')}."


@_action("tell_date")
def _tell_date(p):
    return f"Today is {datetime.now().strftime('%A, %B %-d, %Y')}."


@_action("tell_battery")
def _tell_battery(p):
    if _IS_MAC:
        code, out = _run("pmset -g batt")
        # e.g. "Now drawing from 'AC Power'\n -InternalBattery-0 (id=...) 87%; charging; ..."
        m = re.search(r"(\d+)%", out)
        if m:
            pct = int(m.group(1))
            charging = "charging" in out.lower() or "ac power" in out.lower()
            status = "charging" if charging else "discharging"
            return f"Battery is at {pct}% ({status})."
        raise RuntimeError("Could not read battery level.")
    elif _IS_WIN:
        code, out = _run(
            "powershell -NoProfile -Command"
            ' "$b=[Windows.System.Power.PowerManager,Windows.System,ContentType=WindowsRuntime];echo'
            ' $b::RemainingChargePercent"'
        )
        if code == 0 and out.strip().isdigit():
            return f"Battery is at {out.strip()}%."
        # Fallback: WMIC
        code2, out2 = _run(
            "wmic path Win32_Battery get EstimatedChargeRemaining /value"
        )
        m = re.search(r"=(\d+)", out2)
        if m:
            return f"Battery is at {m.group(1)}%."
        raise RuntimeError("Could not read battery level.")
    else:
        # /sys/class/power_supply — works on most Linux distros
        bat_paths = [
            "/sys/class/power_supply/BAT0/capacity",
            "/sys/class/power_supply/BAT1/capacity",
        ]
        for path in bat_paths:
            if os.path.exists(path):
                with open(path) as f:
                    return f"Battery is at {f.read().strip()}%."
        # Fallback: upower
        if shutil.which("upower"):
            code, out = _run("upower -i $(upower -e | grep BAT) | grep percentage")
            m = re.search(r"(\d+)%", out)
            if m:
                return f"Battery is at {m.group(1)}%."
        raise RuntimeError("Could not read battery level.")


# ══════════════════════════════════════════════════════════════════════════════
# §8  ACTION EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════


def _exec_action(action: QuickAction) -> str:
    handler = _ACTION_REGISTRY.get(action.name)
    if handler is None:
        return f"Unknown action: {action.name}"
    return handler(action.params)


# ══════════════════════════════════════════════════════════════════════════════
# §9  IntentInterceptor
# ══════════════════════════════════════════════════════════════════════════════


class IntentInterceptor:
    """
    Fast-path intent matcher.

    Usage:
        norm   = normalize(raw_text)
        action = interceptor.match(norm)
        if action:
            reply, ok = interceptor.execute(action)
    """

    def match(self, normalized: str) -> Optional[QuickAction]:
        """
        Match a normalized command string against the pattern table.
        Returns None if no match OR if the match is flagged ambiguous.
        Co-referential pronouns always fall through regardless of pattern.
        """
        if not normalized:
            return None
        if _COREF_RE.search(normalized):
            return None

        for pattern, builder in _PATTERNS:
            m = pattern.match(normalized)
            if m:
                action = builder(m)
                if action is not None:
                    logger.info(
                        "interceptor.match | cmd=%r action=%s params=%s",
                        normalized,
                        action.name,
                        action.params,
                    )
                    return action

        return None

    def execute(self, action: QuickAction) -> tuple[str, bool]:
        """
        Execute action (+ any chained actions).
        Returns (reply, success).  On failure returns (error_msg, False)
        so the caller can fall through to the full LLM pipeline.
        """
        try:
            reply = _exec_action(action)
            for chained in action.chain:
                _wait_for_app(chained.params.get("app", ""))
                reply = f"{reply} {_exec_action(chained)}"
            return reply.strip(), True
        except Exception as exc:
            logger.warning(
                "interceptor.execute failed action=%s err=%r", action.name, exc
            )
            return str(exc), False


# Module-level singleton
interceptor = IntentInterceptor()
