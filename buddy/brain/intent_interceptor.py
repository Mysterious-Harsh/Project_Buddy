# buddy/brain/intent_interceptor.py
#
# Fast-path interceptor — handles deterministic system actions without any LLM call.
# Sits at the top of handle_turn(). Returns None for anything ambiguous → Brain takes over.
#
# Supported intent categories:
#   Media      : play, pause, resume, toggle, next, prev, skip, play-on-app, search-on-app
#   Volume     : up, down, set, mute, max, min
#   Power      : sleep, hibernate, lock, shutdown, restart, logout
#   Display    : brightness up/down/set, dark mode, night mode/shift
#   Network    : wifi on/off/toggle, bluetooth on/off/toggle
#   Focus      : do not disturb on/off, focus mode, quiet mode
#   Screenshot : take screenshot, capture screen, print screen
#   Quick Info : time, date, battery
#   Folders    : open downloads / desktop / documents / home
#   App launch : open / launch / start / restart <app>
#   App quit   : quit / force quit / kill / exit <app>
#   URL open   : open / go to / visit <url>
#   Timer      : set timer for N seconds/minutes/hours
#   Math       : what is N plus/minus/times/divided by M

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
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
# ══════════════════════════════════════════════════════════════════════════════

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
    "whats": "what is",
    "hows": "how is",
    "today's": "todays",   # FIX: preserve "todays date" through punct strip
    "today's": "todays",
}

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
    r"right\s+now|immediately|quickly|asap|now)[.!?]*$",
    re.IGNORECASE,
)

# \x00 is used as a temporary placeholder for protected dots — must not be stripped.
_PUNCT_RE = re.compile(r"[^\w\s\x00]")

# Protect dots flanked by word chars (URLs, decimals) before punct stripping.
# "youtube.com" → "youtube\x00com" → (punct strip skips \x00) → "youtube.com"
_WORD_DOT_RE = re.compile(r"(?<=\w)\.(?=\w)")

# Strip URL protocols before normalization so "https://google.com" → "google.com".
_URL_PROTO_RE = re.compile(r"https?://", re.IGNORECASE)

_APP_ARTICLE_RE = re.compile(r"^(the|my|an?)\s+", re.IGNORECASE)


def normalize(text: str) -> str:
    """Lowercase, Unicode-fold, expand contractions, strip filler, collapse spaces."""
    t = text.strip()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = t.lower()
    t = _URL_PROTO_RE.sub("", t)  # strip https:// / http:// before dot protection
    for src, dst in _CONTRACTIONS.items():
        t = t.replace(src, dst)
    # Protect intra-word dots (URLs, decimals) before stripping punctuation
    t = _WORD_DOT_RE.sub("\x00", t)
    t = _PUNCT_RE.sub(" ", t)
    t = t.replace("\x00", ".")   # restore protected dots
    prev = None
    while prev != t:
        prev = t
        t = _PREFIX_RE.sub("", t).strip()
    t = _SUFFIX_RE.sub("", t).strip()
    return " ".join(t.split())


# ══════════════════════════════════════════════════════════════════════════════
# §2  AMBIGUITY DETECTION
# ══════════════════════════════════════════════════════════════════════════════

# FIX: narrow coref so bare "this/that" only blocks when followed by a media noun.
# "log out of this computer" must NOT be blocked; "play this song" must be.
_COREF_RE = re.compile(
    r"\b(this|that)\s+(song|one|video|track|album|playlist|artist|file|app|thing)\b"
    r"|\b(these|those)\b"
    r"|\bthe\s+(song|one|video|track|album|playlist|artist|file|app|thing)\b",
    re.IGNORECASE,
)

_GENERIC_PLAY = re.compile(
    r"^(music|something|anything|songs?|audio|some\s+music|a\s+song|some\s+songs?)$",
    re.IGNORECASE,
)

_ON_APP_RE = re.compile(r"\bon\s+\w+$", re.IGNORECASE)

_AMBIGUOUS_APP_RE = re.compile(
    r"^(my\s+browser|a\s+new\s+window|a\s+file|a\s+folder|a\s+tab|"
    r"something|anything|an?\s+app)$",
    re.IGNORECASE,
)

# Guard against restart_app / quit_app swallowing device-level commands
_DEVICE_RE = re.compile(
    r"^(my\s+|the\s+)?(computer|pc|mac|machine|laptop|device)$", re.IGNORECASE
)


def _play_is_ambiguous(after_play: str) -> bool:
    s = after_play.strip()
    if not s:
        return False
    if _GENERIC_PLAY.match(s):
        return False
    if _ON_APP_RE.search(s):
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# §3  APP ALIAS TABLE
# ══════════════════════════════════════════════════════════════════════════════

_APP_ALIASES: dict[str, str] = {
    # Browsers
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "firefox": "Firefox",
    "safari": "Safari",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "brave": "Brave Browser",
    # Music / media
    "spotify": "Spotify",
    "yt": "YouTube",
    "youtube": "YouTube",
    "ytm": "YouTube Music",
    "youtubemusic": "YouTube Music",
    "youtube music": "YouTube Music",
    "apple music": "Music",
    "itunes": "Music",
    "vlc": "VLC",
    "plex": "Plex",
    # Productivity
    "vscode": "Visual Studio Code",
    "code": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    "word": "Microsoft Word",
    "microsoft word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "microsoft excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint",
    "ppt": "Microsoft PowerPoint",
    "onenote": "Microsoft OneNote",
    "notion": "Notion",
    "obsidian": "Obsidian",
    # Communication
    "slack": "Slack",
    "discord": "Discord",
    "teams": "Microsoft Teams",
    "microsoft teams": "Microsoft Teams",
    "zoom": "Zoom",
    "mail": "Mail",
    "outlook": "Microsoft Outlook",
    "microsoft outlook": "Microsoft Outlook",
    # System (macOS)
    "finder": "Finder",
    "terminal": "Terminal",
    "iterm": "iTerm",
    "iterm2": "iTerm",
    "activity monitor": "Activity Monitor",
    "system prefs": "System Preferences",
    "system preferences": "System Preferences",
    "system settings": "System Settings",
    # System (Windows)
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "task manager": "taskmgr.exe",
}


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
        "movies": os.path.join(home, "Movies"),
    }
    return mapping.get(name.lower(), os.path.join(home, name.capitalize()))


def _resolve_app(raw: str) -> str:
    """Return canonical app name, stripping leading articles then looking up alias."""
    cleaned = _APP_ARTICLE_RE.sub("", raw.strip()).strip()
    return _APP_ALIASES.get(cleaned.lower(), cleaned)


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
#
#     ORDERING RULES (do not break):
#       1. Compound / more-specific patterns BEFORE their sub-patterns.
#       2. Focus/DND BEFORE app-launch (prevents "start focus" → open_app).
#       3. URL open BEFORE folder shortcuts and open_app.
#       4. play_on_app / search_on_app BEFORE generic media_play.
#       5. volume/brightness set (with number) BEFORE directional (up/down).
#       6. restart_app guards against _DEVICE_RE so restart_system still fires.
# ══════════════════════════════════════════════════════════════════════════════


def _build_patterns() -> list[tuple]:
    P = re.compile

    def _pct(m, group="n") -> int:
        return max(0, min(100, int(m.group(group))))

    # ── Media ────────────────────────────────────────────────────────────────

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

    # FIX: dedicated builder for resume/continue — media_play builder needs "after" group
    def media_resume(m):
        return QuickAction("media_play")

    def play_on_app(m):
        song = (m.group("song") or "").strip()
        app = (m.group("app") or "").strip().lower()
        if not song or not app:
            return None
        return QuickAction("play_on_app", {"song": song, "app": app})

    def search_on_app(m):
        query = (m.group("query") or "").strip()
        app = (m.group("app") or "").strip().lower()
        if not query or not app:
            return None
        return QuickAction("search_on_app", {"query": query, "app": app})

    # ── App launch / restart / quit ──────────────────────────────────────────

    def restart_app(m):
        raw = (m.group("app") or "").strip()
        if not raw:
            return None
        # FIX: guard — "restart my computer" must not steal from restart_system
        if _DEVICE_RE.match(raw):
            return None
        return QuickAction("restart_app", {"app": _resolve_app(raw)})

    def open_app(m):
        raw = (m.group("app") or "").strip()
        if not raw:
            return None
        if _AMBIGUOUS_APP_RE.match(raw):
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

    def open_url(m):
        url = (m.group("url") or "").strip()
        if not url:
            return None
        return QuickAction("open_url", {"url": url})

    def quit_app(m):
        raw = (m.group("app") or "").strip()
        if not raw or _AMBIGUOUS_APP_RE.match(raw) or _DEVICE_RE.match(raw):
            return None
        return QuickAction("quit_app", {"app": _resolve_app(raw), "force": False})

    def force_quit_app(m):
        raw = (m.group("app") or "").strip()
        if not raw or _AMBIGUOUS_APP_RE.match(raw) or _DEVICE_RE.match(raw):
            return None
        return QuickAction("quit_app", {"app": _resolve_app(raw), "force": True})

    # ── Volume ───────────────────────────────────────────────────────────────

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

    # ── Power ────────────────────────────────────────────────────────────────

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

    # ── Display / Brightness ─────────────────────────────────────────────────

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

    # ── Network ──────────────────────────────────────────────────────────────

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

    # ── Focus / DND ──────────────────────────────────────────────────────────

    def dnd_on(m):
        return QuickAction("do_not_disturb", {"state": "on"})

    def dnd_off(m):
        return QuickAction("do_not_disturb", {"state": "off"})

    # ── Screenshot ───────────────────────────────────────────────────────────

    def screenshot(m):
        return QuickAction("screenshot")

    # ── Quick info ───────────────────────────────────────────────────────────

    def tell_time(m):
        return QuickAction("tell_time")

    def tell_date(m):
        return QuickAction("tell_date")

    def tell_battery(m):
        return QuickAction("tell_battery")

    # ── Timer (new) ──────────────────────────────────────────────────────────

    def timer_builder(m):
        return QuickAction("timer", {
            "n": int(m.group("n")),
            "unit": (m.group("unit") or "minutes").lower(),
        })

    # ── Math (new) ───────────────────────────────────────────────────────────

    def math_calc(m):
        a = m.group("a")
        op = re.sub(r"\s+", " ", (m.group("op") or "").strip().lower())
        b = m.group("b")
        if not a or not b or not op:
            return None
        return QuickAction("math_calculate", {"a": a, "op": op, "b": b})

    # ── Shared sub-expressions ────────────────────────────────────────────────

    _WIFI = r"(wi\s*fi|wifi|wireless|wi-fi)"
    _BT   = r"(bluetooth|bt)"
    _MY   = r"(my\s+|the\s+)?"
    _DEV  = r"(computer|pc|mac|machine|laptop|device)"

    # ── Pattern table ─────────────────────────────────────────────────────────

    return [
        # ── Compound: open X and play  [before plain open X] ─────────────────
        (P(r"^open\s+(?P<app>[\w\s]+?)\s+and\s+play$", re.I), open_and_play),

        # ── Restart app  [device guard inside builder; before open/launch] ───
        (P(r"^restart\s+(?P<app>[\w\s]+)$", re.I), restart_app),

        # ── URL open  [before folders/app; dots survive normalize] ───────────
        (
            P(
                r"^(open|go\s+to|visit|navigate\s+to|browse\s+to)\s+"
                r"(?P<url>(?:https?://)?[\w\-]+(?:\.[\w\-]+)*\.[\w\-]{2,})$",
                re.I,
            ),
            open_url,
        ),

        # ── Folder shortcuts  [before generic open_app] ───────────────────────
        (
            P(
                r"^open\s+(?:my\s+)?(?P<folder>"
                r"downloads?|desktop|documents?|home(\s+folder)?|"
                r"pictures?|music|videos?|movies?)$",
                re.I,
            ),
            open_folder,
        ),

        # ── Focus / DND  [BEFORE app-launch — fixes "start focus" → open_app] ─
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
            P(r"^do\s+not\s+disturb(\s+(on|off))?$", re.I),
            lambda m: dnd_off(m) if (m.group(2) or "on").lower() == "off" else dnd_on(m),
        ),
        (
            P(
                r"^(quiet\s+(mode|hours)|silence\s+(notifications|alerts)|"
                r"turn\s+on\s+(quiet\s+(mode|hours)|silence))$",
                re.I,
            ),
            dnd_on,
        ),

        # ── App launch ────────────────────────────────────────────────────────
        (P(r"^open\s+(?P<app>[\w\s]+)$", re.I), open_app),
        (P(r"^launch\s+(?P<app>[\w\s]+)$", re.I), open_app),
        (P(r"^start\s+(?P<app>[\w\s]+)$", re.I), open_app),

        # ── App quit / force-quit / kill ──────────────────────────────────────
        (P(r"^quit\s+(?P<app>[\w\s]+)$", re.I), quit_app),
        (P(r"^force\s+quit\s+(?P<app>[\w\s]+)$", re.I), force_quit_app),
        (P(r"^kill\s+(?P<app>[\w\s]+)$", re.I), quit_app),
        (P(r"^exit\s+(?P<app>[\w\s]+)$", re.I), quit_app),

        # ── Media: search/find on app  [before play_on_app] ──────────────────
        (
            P(
                r"^(search(\s+for)?|find|look\s+up)\s+(?P<query>.+?)\s+on\s+(?P<app>\w+)$",
                re.I,
            ),
            search_on_app,
        ),

        # ── Media: play on app  [before generic media_play] ──────────────────
        (P(r"^play\s+(?P<song>.+?)\s+on\s+(?P<app>\w+)$", re.I), play_on_app),

        # ── Media: play / pause / resume / toggle / next / prev ──────────────
        (P(r"^play\s*(?P<after>.*)$", re.I), media_play),
        (
            P(r"^(pause|stop(\s+(music|playing|playback|the\s+music))?)$", re.I),
            media_pause,
        ),
        # FIX: dedicated builder — media_play builder calls m.group("after") which
        # only exists in the ^play pattern above, not in resume/continue patterns.
        (P(r"^(resume|continue\s+music|continue\s+playing)$", re.I), media_resume),
        (P(r"^(play\s*pause|toggle\s+(music|playback))$", re.I), media_toggle),
        (P(r"^(next|play\s+next)(\s+(track|song))?$", re.I), media_next),
        (P(r"^go\s+(to\s+)?(next|forward)(\s+(track|song))?$", re.I), media_next),
        (P(r"^(previous|prev)(\s+(track|song))?$", re.I), media_prev),
        (P(r"^go\s+(to\s+)?(previous|prev|back)(\s+(track|song))?$", re.I), media_prev),
        (P(r"^skip(\s+(track|song))?$", re.I), media_next),

        # ── Volume: set (number)  [before directional] ────────────────────────
        (P(r"^volume\s+(?P<n>\d{1,3})(%)?$", re.I), volume_set),
        (P(r"^set\s+(the\s+)?volume\s+(to\s+)?(?P<n>\d{1,3})(%)?$", re.I), volume_set),
        (
            P(
                r"^((volume|set\s+(the\s+)?volume)\s+(to\s+)?(max|maximum|full)"
                r"|(max|maximum|full)\s+volume)$",
                re.I,
            ),
            volume_max,
        ),
        (
            P(
                r"^((volume|set\s+(the\s+)?volume)\s+(to\s+)?(min|minimum|zero|silent)"
                r"|(min|minimum|zero|silent)\s+volume)$",
                re.I,
            ),
            volume_min,
        ),

        # ── Volume: directional ───────────────────────────────────────────────
        (P(r"^volume\s+(up|louder|increase)$", re.I), volume_up),
        (P(r"^volume\s+(down|lower|quieter|decrease|softer)$", re.I), volume_down),
        (
            P(
                r"^(turn\s+(the\s+)?volume\s+up"
                r"|turn\s+up(\s+the)?\s+volume"
                r"|louder"
                r"|increase\s+(the\s+)?volume"
                r"|raise\s+(the\s+)?volume"
                r"|boost\s+(the\s+)?volume)$",
                re.I,
            ),
            volume_up,
        ),
        (
            P(
                r"^(turn\s+(the\s+)?volume\s+down"
                r"|turn\s+down(\s+the)?\s+volume"
                r"|lower\s+(the\s+)?volume"
                r"|decrease\s+(the\s+)?volume"
                r"|quieter)$",
                re.I,
            ),
            volume_down,
        ),
        (P(r"^(mute|unmute|toggle\s+mute)$", re.I), mute),

        # ── Power ─────────────────────────────────────────────────────────────
        (P(r"^lock(\s+(screen|my\s+screen|the\s+screen))?$", re.I), lock),
        (
            P(
                rf"^(sleep(\s+mode)?"
                rf"|put\s+{_MY}{_DEV}\s+to\s+sleep"
                rf"|send\s+{_MY}{_DEV}\s+to\s+sleep)$",
                re.I,
            ),
            sleep_sys,
        ),
        (P(r"^(hibernate|suspend\s+to\s+disk)$", re.I), hibernate),
        (
            P(
                rf"^(shut\s+down|shutdown|power\s+off"
                rf"|turn\s+off(\s+{_MY}{_DEV})?"
                rf"|shut\s+{_MY}{_DEV}\s+off"
                rf"|{_MY}{_DEV}\s+off)$",
                re.I,
            ),
            shutdown,
        ),
        (P(rf"^(restart|reboot)(\s+{_MY}{_DEV})?$", re.I), restart),
        (
            P(
                r"^(log\s+out|logout|sign\s+out)(\s+(of\s+)?(this\s+)?(computer|session))?$",
                re.I,
            ),
            logout,
        ),

        # ── Brightness: set (number)  [before directional] ────────────────────
        (
            P(r"^set\s+(the\s+)?(screen\s+)?brightness\s+(to\s+)?(?P<n>\d{1,3})(%)?$", re.I),
            brightness_set,
        ),
        (P(r"^brightness\s+(?P<n>\d{1,3})(%)?$", re.I), brightness_set),
        (P(r"^(screen\s+)?brightness\s+(to\s+)?(?P<n>\d{1,3})(%)?$", re.I), brightness_set),

        # ── Brightness: directional ───────────────────────────────────────────
        (
            P(
                r"^("
                r"(screen\s+)?brightness\s+(up|increase|higher|more|boost)"
                r"|(increase|raise|boost|bump\s+up)\s+(the\s+)?(screen\s+)?brightness"
                r"|turn\s+(the\s+)?(screen\s+)?brightness\s+up"
                r"|turn\s+up\s+(the\s+)?(screen\s+)?brightness"
                r"|make\s+(the\s+)?screen\s+brighter"
                r"|make\s+it\s+brighter"
                r"|brighter"
                r")$",
                re.I,
            ),
            brightness_up,
        ),
        (
            P(
                r"^("
                r"(screen\s+)?brightness\s+(down|decrease|lower|less|dim)"
                r"|(decrease|lower|dim|reduce)\s+(the\s+)?(screen\s+)?brightness"
                r"|turn\s+(the\s+)?(screen\s+)?brightness\s+down"
                r"|turn\s+down\s+(the\s+)?(screen\s+)?brightness"
                r"|make\s+(the\s+)?screen\s+dimmer"
                r"|make\s+it\s+dimmer"
                r"|dim\s+the\s+screen"
                r")$",
                re.I,
            ),
            brightness_down,
        ),

        # ── Dark / Night mode ─────────────────────────────────────────────────
        (P(r"^(enable|turn\s+on|switch\s+to|activate)\s+dark\s+mode$", re.I), dark_mode_on),
        (P(r"^(disable|turn\s+off|switch\s+off|deactivate)\s+dark\s+mode$", re.I), dark_mode_off),
        (P(r"^(toggle|switch)\s+dark\s+mode$", re.I), dark_mode_toggle),
        (P(r"^(enable|turn\s+on|activate)\s+light\s+mode$", re.I), dark_mode_off),
        (P(r"^(enable|turn\s+on|activate)\s+(night\s+mode|night\s+shift)$", re.I), night_mode_on),
        (P(r"^(disable|turn\s+off|deactivate)\s+(night\s+mode|night\s+shift)$", re.I), night_mode_off),

        # ── Wi-Fi ─────────────────────────────────────────────────────────────
        (P(rf"^(turn\s+on|enable|connect(\s+to)?)\s+{_MY}{_WIFI}$", re.I), wifi_on),
        (P(rf"^(turn\s+off|disable|disconnect(\s+from)?)\s+{_MY}{_WIFI}$", re.I), wifi_off),
        (P(rf"^(toggle|switch)\s+{_MY}{_WIFI}$", re.I), wifi_toggle),
        # FIX: "wifi on/off/toggle" — use correct group index (group 2, not 4)
        (
            P(rf"^{_WIFI}\s+(on|off|toggle)$", re.I),
            lambda m: {"on": wifi_on, "off": wifi_off, "toggle": wifi_toggle}[
                m.group(2).lower()
            ](m),
        ),
        # FIX: "turn my wifi on/off" — use correct group index (group 3, not 4)
        (
            P(rf"^turn\s+{_MY}{_WIFI}\s+(on|off)$", re.I),
            lambda m: wifi_on(m) if m.group(3).lower() == "on" else wifi_off(m),
        ),

        # ── Bluetooth ─────────────────────────────────────────────────────────
        (P(rf"^(turn\s+on|enable)\s+{_MY}{_BT}$", re.I), bt_on),
        (P(rf"^(turn\s+off|disable|disconnect)\s+{_MY}{_BT}$", re.I), bt_off),
        (P(rf"^(toggle|switch)\s+{_MY}{_BT}$", re.I), bt_toggle),
        # FIX: "bluetooth on/off/toggle" — use correct group index (group 2, not 3)
        (
            P(rf"^{_BT}\s+(on|off|toggle)$", re.I),
            lambda m: {"on": bt_on, "off": bt_off, "toggle": bt_toggle}[
                m.group(2).lower()
            ](m),
        ),
        (
            P(rf"^turn\s+{_MY}{_BT}\s+(on|off)$", re.I),
            lambda m: bt_on(m) if m.group(3).lower() == "on" else bt_off(m),
        ),

        # ── Screenshot ────────────────────────────────────────────────────────
        (
            P(
                r"^(take\s+(a\s+)?|grab\s+(a\s+)?|snap\s+(a\s+)?)?"
                r"screenshot(\s+(now|the\s+screen))?$",
                re.I,
            ),
            screenshot,
        ),
        (
            P(
                r"^(capture\s+(the\s+)?screen"
                r"|screen\s+capture"
                r"|print\s+screen"
                r"|screengrab"
                r"|screen\s+shot)$",
                re.I,
            ),
            screenshot,
        ),

        # ── Quick info ────────────────────────────────────────────────────────
        (
            P(
                r"^(what\s+(is\s+)?(the\s+)?time"
                r"|what\s+time\s+is\s+it"
                r"|current\s+time"
                r"|tell\s+me\s+(the\s+)?time)$",
                re.I,
            ),
            tell_time,
        ),
        (
            P(
                r"^(what(\s+is)?\s+(today|the\s+date)|what\s+day\s+is\s+it|todays\s+date)$",
                re.I,
            ),
            tell_date,
        ),
        (
            P(
                r"^(battery(\s+(level|status|percentage|life|charge))?"
                r"|check\s+battery(\s+(level|status))?"
                r"|how\s+much\s+battery(\s+(is\s+left|do\s+i\s+have))?"
                r"|what\s+is\s+(my\s+)?battery(\s+percentage)?)$",
                re.I,
            ),
            tell_battery,
        ),

        # ── Timer ─────────────────────────────────────────────────────────────
        (
            P(
                r"^(set\s+)?(a\s+|an\s+)?timer\s+(for\s+)?(?P<n>\d+)\s+"
                r"(?P<unit>seconds?|minutes?|hours?)$",
                re.I,
            ),
            timer_builder,
        ),

        # ── Simple math ───────────────────────────────────────────────────────
        (
            P(
                r"^(what\s+is\s+|calculate\s+|calc\s+)?"
                r"(?P<a>\d+(?:\.\d+)?)\s+"
                r"(?P<op>plus|minus|times|divided\s+by|multiplied\s+by|modulo|mod)\s+"
                r"(?P<b>\d+(?:\.\d+)?)$",
                re.I,
            ),
            math_calc,
        ),
    ]


_PATTERNS = _build_patterns()


# ══════════════════════════════════════════════════════════════════════════════
# §6  PLATFORM HELPERS
# ══════════════════════════════════════════════════════════════════════════════


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


def _win_sendkey(vk: int) -> None:
    _run(
        f"powershell -NoProfile -Command "
        f'"(New-Object -ComObject WScript.Shell).SendKeys([char]{vk})"'
    )


def _linux_playerctl(cmd: str) -> tuple[int, str]:
    if not shutil.which("playerctl"):
        return -1, "playerctl not installed (apt/pacman/dnf install playerctl)"
    return _run(f"playerctl {cmd}")


_MAC_MEDIA_APPS: dict[str, dict[str, str]] = {
    "Spotify": {
        "play": "play", "pause": "pause", "toggle": "playpause",
        "next": "next track", "prev": "previous track",
    },
    "Music": {
        "play": "play", "pause": "pause", "toggle": "playpause",
        "next": "next track", "prev": "previous track",
    },
    "TV": {
        "play": "play", "pause": "pause", "toggle": "play",
        "next": "next chapter", "prev": "previous chapter",
    },
    "Plex Media Player": {
        "play": "play", "pause": "pause", "toggle": "play",
        "next": "next chapter", "prev": "previous chapter",
    },
}

_MAC_MEDIA_KEY: dict[str, int] = {
    "play": 100, "pause": 100, "toggle": 100, "next": 101, "prev": 98,
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
# ══════════════════════════════════════════════════════════════════════════════

_ACTION_REGISTRY: dict[str, Callable[[dict], str]] = {}


def _action(name: str):
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
        _win_sendkey(176)
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
        _win_sendkey(177)
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


@_action("search_on_app")
def _search_on_app(p):
    query = p.get("query", "")
    app = p.get("app", "").lower()
    encoded = quote_plus(query)
    search_urls = {
        "youtube": f"https://www.youtube.com/results?search_query={encoded}",
        "yt": f"https://www.youtube.com/results?search_query={encoded}",
        "spotify": f"https://open.spotify.com/search/{encoded}",
        "google": f"https://www.google.com/search?q={encoded}",
        "amazon": f"https://www.amazon.com/s?k={encoded}",
        "netflix": f"https://www.netflix.com/search?q={encoded}",
        "reddit": f"https://www.reddit.com/search/?q={encoded}",
        "twitter": f"https://twitter.com/search?q={encoded}",
        "x": f"https://twitter.com/search?q={encoded}",
        "github": f"https://github.com/search?q={encoded}",
    }
    url = search_urls.get(app)
    if url:
        if _IS_MAC:
            _run(f'open "{url}"')
        elif _IS_WIN:
            _run(f'start "" "{url}"')
        else:
            _run(f'xdg-open "{url}"')
        return f"Searching for '{query}' on {app.title()}."
    return f"Opened {app.title()} — please search for '{query}' manually."


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
        vk = 175 if up else 174
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
            'osascript -e "set volume output muted not (output muted of (get volume settings))"'
        )
    elif _IS_WIN:
        _win_sendkey(173)
    else:
        _run("pactl set-sink-mute @DEFAULT_SINK@ toggle")
    return "Mute toggled."


# ── App launch / restart / quit / folders ─────────────────────────────────────


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
    return f"Opening {app}."


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
        _run(f'pkill -f "{app}"')
        time.sleep(0.5)
        _run(f'xdg-open "{app}"')
    return f"Restarting {app}."


@_action("quit_app")
def _quit_app(p):
    app = p.get("app", "")
    force = bool(p.get("force", False))
    if _IS_MAC:
        _run(f"osascript -e 'tell application \"{app}\" to quit'")
        if force:
            time.sleep(0.5)
            _run(f'pkill -9 -f "{app}"')
    elif _IS_WIN:
        exe = app if app.endswith(".exe") else f"{app}.exe"
        flag = "/F" if force else ""
        _run(f'taskkill /IM "{exe}" {flag}'.strip())
    else:
        signal_flag = "-9" if force else "-15"
        _run(f'pkill {signal_flag} -f "{app}"')
    label = "Force quit" if force else "Quit"
    return f"{label} {app}."


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


@_action("open_url")
def _open_url(p):
    url = p.get("url", "")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if _IS_MAC:
        _run(f'open "{url}"')
    elif _IS_WIN:
        _run(f'start "" "{url}"')
    else:
        _run(f'xdg-open "{url}"')
    return f"Opening {url}."


# ── Power ─────────────────────────────────────────────────────────────────────


@_action("lock_screen")
def _lock_screen(p):
    if _IS_MAC:
        _run(
            "osascript -e 'tell application \"System Events\" to keystroke \"q\""
            " using {command down, control down}'"
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
        _run("loginctl terminate-user $USER 2>/dev/null || gnome-session-quit --no-prompt")
    return "Logging out."


# ── Display / Brightness ──────────────────────────────────────────────────────


@_action("brightness_step")
def _brightness_step(p):
    delta = int(p.get("delta", 10))
    up = delta > 0
    if _IS_MAC:
        key = "144" if up else "145"
        steps = max(1, abs(delta) // 10)
        for _ in range(steps):
            _run(f"osascript -e 'tell application \"System Events\" to key code {key}'")
    elif _IS_WIN:
        sign = "+" if up else "-"
        code, _ = _run(f"nircmd.exe changebrightness {sign}{abs(delta)}")
        if code != 0:
            _run(
                'powershell -NoProfile -Command "'
                "$b=(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness;"
                "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)"
                f'.WmiSetBrightness(1,[Math]::Min(100,[Math]::Max(0,$b{("+" if up else "-")}{abs(delta)})))"'
            )
    else:
        if shutil.which("brightnessctl"):
            sign = "+" if up else "-"
            _run(f"brightnessctl set {abs(delta)}%{sign}")
        else:
            code, out = _run("xrandr --verbose | awk '/Brightness/{print $2; exit}'")
            current = float(out) if code == 0 and out else 1.0
            new_val = max(0.0, min(1.0, current + (delta / 100.0)))
            _, display_name = _run("xrandr | awk '/ connected/{print $1; exit}'")
            if display_name:
                _run(f"xrandr --output {display_name} --brightness {new_val:.2f}")
    return f"Brightness {'up' if up else 'down'}."


@_action("brightness_set")
def _brightness_set(p):
    level = max(0, min(100, int(p.get("level", 50))))
    if _IS_MAC:
        val = level / 100.0
        _run(
            f"osascript -e 'do shell script \"brightness {val:.2f}\"' 2>/dev/null || "
            f"osascript -e 'tell application \"System Events\" to set brightness of screen 1 to {val}'"
        )
    elif _IS_WIN:
        code, _ = _run(f"nircmd.exe setbrightness {level}")
        if code != 0:
            _run(
                f'powershell -NoProfile -Command "(Get-WmiObject -Namespace root/wmi'
                f' -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})"'
            )
    else:
        if shutil.which("brightnessctl"):
            _run(f"brightnessctl set {level}%")
        else:
            val = level / 100.0
            _, display_name = _run("xrandr | awk '/ connected/{print $1; exit}'")
            if display_name:
                _run(f"xrandr --output {display_name} --brightness {val:.2f}")
    return f"Brightness set to {level}%."


@_action("dark_mode")
def _dark_mode(p):
    state = p.get("state", "toggle")
    if _IS_MAC:
        if state == "toggle":
            _run(
                "osascript -e 'tell application \"System Events\" to "
                "tell appearance preferences to set dark mode to not dark mode'"
            )
            return "Dark mode toggled."
        val = "true" if state == "on" else "false"
        _run(
            f"osascript -e 'tell application \"System Events\" to "
            f"tell appearance preferences to set dark mode to {val}'"
        )
    elif _IS_WIN:
        reg_val = "0" if state == "on" else "1"
        if state == "toggle":
            code, out = _run(
                'reg query "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion'
                '\\Themes\\Personalize" /v AppsUseLightTheme'
            )
            current = "1" in (out or "")
            reg_val = "0" if current else "1"
        _run(
            'reg add "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion'
            f'\\Themes\\Personalize" /v AppsUseLightTheme /t REG_DWORD /d {reg_val} /f'
        )
    else:
        if state in ("on", "toggle"):
            _run(
                "gsettings set org.gnome.desktop.interface color-scheme 'prefer-dark'"
                " 2>/dev/null || lookandfeeltool -a org.kde.breezedark.desktop 2>/dev/null"
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
            f"osascript -e 'tell application \"System Events\" to "
            f"tell appearance preferences to set night shift enabled to {val}' 2>/dev/null"
        )
    elif _IS_WIN:
        _run(
            'powershell -NoProfile -Command "'
            "$p='HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\CloudStore\\Store\\"
            "DefaultAccount\\Current\\default$windows.data.bluelightreduction.bluelightreductionstate\\"
            "windows.data.bluelightreduction.bluelightreductionstate';"
            'if(Test-Path $p){Remove-Item $p -Force}"'
        )
    else:
        if on:
            _run("redshift -O 4500 2>/dev/null || gammastep -O 4500 2>/dev/null")
        else:
            _run("redshift -x 2>/dev/null || gammastep -x 2>/dev/null")
    return f"Night mode {'enabled' if on else 'disabled'}."


# ── Network ───────────────────────────────────────────────────────────────────


@_action("wifi")
def _wifi(p):
    state = p.get("state", "toggle")
    if _IS_MAC:
        if state == "toggle":
            code, out = _run("networksetup -getairportpower en0")
            current_on = "on" in out.lower()
            state = "off" if current_on else "on"
        _run(f"networksetup -setairportpower en0 {state}")
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
        if not shutil.which("blueutil"):
            raise RuntimeError("blueutil not found. Install with: brew install blueutil")
        if state == "toggle":
            _run("blueutil --toggle")
        else:
            _run(f"blueutil --{'power 1' if state == 'on' else 'power 0'}")
    elif _IS_WIN:
        action = "Enable" if state in ("on", "toggle") else "Disable"
        _run(
            f'powershell -NoProfile -Command "Get-PnpDevice -Class Bluetooth | '
            f'{action}-PnpDevice -Confirm:$false"'
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
        val = "true" if on else "false"
        _run(
            f"defaults -currentHost write com.apple.notificationcenterui doNotDisturb"
            f" -bool {val} && killall NotificationCenter 2>/dev/null;"
            f" osascript -e 'tell application \"System Events\" to"
            f" set doNotDisturb of appearance preferences to {val}' 2>/dev/null"
        )
    elif _IS_WIN:
        reg_val = "1" if on else "0"
        _run(
            'reg add "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\CloudStore\\'
            'Store\\DefaultAccount\\Current\\default$windows.data.notifications.quiethours\\'
            f'windows.data.notifications.quiethours" /v Data /t REG_BINARY /d {reg_val} /f 2>nul'
        )
    else:
        _run(
            f"gsettings set org.gnome.desktop.notifications show-banners"
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
        elif shutil.which("import"):
            code, out = _run(f'import -window root "{dest}"')
        else:
            raise RuntimeError("No screenshot tool found (install scrot or gnome-screenshot)")
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
        m = re.search(r"(\d+)%", out)
        if m:
            pct = int(m.group(1))
            charging = "charging" in out.lower() or "ac power" in out.lower()
            return f"Battery is at {pct}% ({'charging' if charging else 'discharging'})."
        raise RuntimeError("Could not read battery level.")
    elif _IS_WIN:
        code, out = _run(
            "wmic path Win32_Battery get EstimatedChargeRemaining /value"
        )
        m = re.search(r"=(\d+)", out)
        if m:
            return f"Battery is at {m.group(1)}%."
        raise RuntimeError("Could not read battery level.")
    else:
        for path in ("/sys/class/power_supply/BAT0/capacity",
                     "/sys/class/power_supply/BAT1/capacity"):
            if os.path.exists(path):
                with open(path) as f:
                    return f"Battery is at {f.read().strip()}%."
        if shutil.which("upower"):
            code, out = _run("upower -i $(upower -e | grep BAT) | grep percentage")
            m = re.search(r"(\d+)%", out)
            if m:
                return f"Battery is at {m.group(1)}%."
        raise RuntimeError("Could not read battery level.")


# ── Timer ─────────────────────────────────────────────────────────────────────


@_action("timer")
def _timer(p):
    n = int(p.get("n", 1))
    unit = str(p.get("unit", "minutes")).lower()
    seconds_map = {
        "second": 1, "seconds": 1,
        "minute": 60, "minutes": 60,
        "hour": 3600, "hours": 3600,
    }
    total_seconds = n * seconds_map.get(unit, 60)

    def _fire():
        time.sleep(total_seconds)
        msg = f"Timer done: {n} {unit}."
        if _IS_MAC:
            _run(f'osascript -e \'display notification "{msg}" with title "Buddy"\'')
        elif _IS_WIN:
            _run(
                f'powershell -NoProfile -Command "'
                f'[System.Windows.MessageBox]::Show(\'{msg}\', \'Buddy\')"'
            )
        else:
            _run(f'notify-send "Buddy" "{msg}" 2>/dev/null')
        logger.info("timer fired: %s", msg)

    threading.Thread(target=_fire, daemon=True).start()
    return f"Timer set for {n} {unit}."


# ── Simple math ───────────────────────────────────────────────────────────────


@_action("math_calculate")
def _math_calculate(p):
    try:
        a = float(p["a"])
        b = float(p["b"])
        op = str(p["op"]).strip().lower()
    except (KeyError, ValueError):
        return "Could not parse the calculation."

    if op == "plus":
        result = a + b
    elif op == "minus":
        result = a - b
    elif op in ("times", "multiplied by"):
        result = a * b
    elif op == "divided by":
        if b == 0:
            return "Cannot divide by zero."
        result = a / b
    elif op in ("modulo", "mod"):
        if b == 0:
            return "Cannot modulo by zero."
        result = a % b
    else:
        return f"Unknown operator: {op!r}"

    def _fmt(n: float) -> str:
        return str(int(n)) if n == int(n) else f"{n:.6g}"

    return f"{_fmt(a)} {op} {_fmt(b)} = {_fmt(result)}."


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
