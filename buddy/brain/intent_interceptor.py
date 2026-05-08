# buddy/brain/intent_interceptor.py
#
# Fast-path interceptor — handles deterministic system actions without any LLM call.
# Sits at the top of handle_turn(). Returns None for anything ambiguous → Brain takes over.
#
# Supported intent categories:
#   Media        : play, pause, resume, toggle, next, prev, skip, play-on-app, search-on-app
#   Volume       : up, down, set, mute, max, min
#   Power        : sleep, hibernate, lock, shutdown, restart, logout
#   Display      : brightness up/down/set, dark mode, night mode/shift
#   Network      : wifi on/off/toggle, bluetooth on/off/toggle
#   Focus        : do not disturb on/off, focus mode, quiet mode
#   Screenshot   : take screenshot, capture screen, print screen
#   Quick Info   : time, date, battery, uptime, cpu, ram, disk, ip, network status
#   Folders      : open downloads / desktop / documents / home
#   App launch   : open / launch / start / restart <app>
#   App focus    : switch to / focus / bring to front <app>
#   App quit     : quit / force quit / kill / exit <app>
#   URL open     : open / go to / visit <url>
#   Timer        : set timer for N seconds/minutes/hours
#   Math         : what is N plus/minus/times/divided by M
#   Unit convert : convert N <unit> to <unit>  (temp/length/weight/speed/volume)
#   World clock  : what time in <city>
#   Base convert : N in binary/hex/decimal/octal
#   Random       : flip a coin, roll a dice, random number between N and M

from __future__ import annotations

import os
import random as _random_mod
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional
from urllib.parse import quote_plus

# ── Optional runtime dependencies (guarded imports) ───────────────────────────
try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _psutil = None           # type: ignore[assignment]
    _PSUTIL_OK = False

try:
    import zoneinfo as _zoneinfo
    _zoneinfo.ZoneInfo("UTC")   # smoke-test: fails on Windows without tzdata
    _ZONEINFO_OK = True
except (ImportError, KeyError):
    _zoneinfo = None             # type: ignore[assignment]
    _ZONEINFO_OK = False

from buddy.logger.logger import get_logger

logger = get_logger("intent_interceptor")


# ══════════════════════════════════════════════════════════════════════════════
# §0  DEPENDENCY CHECK
# ══════════════════════════════════════════════════════════════════════════════

# Each entry: display_name → {pip_pkg, check_fn, features}
_INTENT_DEPS = {
    "psutil": {
        "pip_pkg": "psutil",
        "check": lambda: _PSUTIL_OK,
        "features": "CPU / RAM / disk / uptime / IP stats",
    },
    "tzdata": {
        "pip_pkg": "tzdata",
        "check": lambda: _ZONEINFO_OK,
        "features": "world clock (timezone data on Windows / minimal envs)",
    },
}


def _pip_install(pkg: str) -> bool:
    """Run pip install <pkg> using the current Python interpreter."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg, "-q", "--no-input"],
            capture_output=True,
            timeout=120,
        )
        return result.returncode == 0
    except Exception:
        return False


def _recheck_psutil() -> bool:
    global _psutil, _PSUTIL_OK
    try:
        import psutil as _p
        _psutil = _p
        _PSUTIL_OK = True
        return True
    except ImportError:
        return False


def _recheck_zoneinfo() -> bool:
    global _zoneinfo, _ZONEINFO_OK
    try:
        import zoneinfo as _z
        _z.ZoneInfo("UTC")
        _zoneinfo = _z
        _ZONEINFO_OK = True
        return True
    except (ImportError, KeyError):
        return False


_RECHECK = {
    "psutil": _recheck_psutil,
    "tzdata": _recheck_zoneinfo,
}


def check_intent_deps(auto_install: bool = True) -> dict[str, bool]:
    """
    Check required runtime deps for IntentInterceptor.
    If auto_install=True (default) and a dep is missing, installs it via pip.

    Returns {dep_name: is_available}.
    Missing deps disable only their feature group — the interceptor still runs.
    """
    status: dict[str, bool] = {}
    missing: list[str] = []

    for name, info in _INTENT_DEPS.items():
        if info["check"]():
            status[name] = True
        else:
            missing.append(name)
            status[name] = False

    if not missing:
        logger.debug("intent_interceptor: all deps present %s", list(status.keys()))
        return status

    for name in missing:
        info = _INTENT_DEPS[name]
        pkg = info["pip_pkg"]
        features = info["features"]

        if auto_install:
            logger.warning(
                "intent_interceptor: %r not found — installing %r (needed for: %s)",
                name, pkg, features,
            )
            ok = _pip_install(pkg)
            if ok:
                rechk = _RECHECK[name]()
                status[name] = rechk
                if rechk:
                    logger.info("intent_interceptor: %r installed successfully.", name)
                else:
                    logger.warning(
                        "intent_interceptor: installed %r but import still failed — "
                        "restart Buddy or run: pip install %s", name, pkg,
                    )
            else:
                logger.error(
                    "intent_interceptor: failed to install %r automatically. "
                    "Run manually: pip install %s\n"
                    "Affected features: %s", name, pkg, features,
                )
        else:
            logger.warning(
                "intent_interceptor: %r missing — %s will be unavailable. "
                "Run: pip install %s", name, features, pkg,
            )

    return status


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

# ── Security: processes that must never be targeted by quit/kill ──────────────
# Shell runtimes — killing these kills Buddy or the user's terminal session.
# System daemons — killing these can destabilize or crash the OS.
_PROTECTED_PROCS: frozenset[str] = frozenset({
    # Python / Buddy runtime
    "python", "python3", "python3.11", "python3.12", "python3.10",
    # Shell interpreters
    "bash", "zsh", "sh", "fish", "tcsh", "csh", "dash", "ksh",
    # macOS system
    "launchd", "kernel_task", "windowserver", "loginwindow", "systemuiserver",
    "coreaudiod", "notificationcenter", "securityd", "configd",
    # Linux system
    "init", "systemd", "systemd-journald", "systemd-logind", "systemd-udevd",
    "dbus", "dbus-daemon", "networkmanager", "polkitd", "udisksd",
    "sshd", "cron", "crond", "rsyslogd", "udevd", "kthreadd",
    # Windows system
    "svchost", "csrss", "lsass", "winlogon", "services", "smss",
    "system", "registry", "memory compression", "wininit",
})


def _is_protected_proc(name: str) -> bool:
    """Return True if name matches a protected system/runtime process."""
    return name.strip().lower() in _PROTECTED_PROCS


# ── Security: final arg sanitizer — strips anything that could escape double-
# quotes in a shell command string.  normalize() + regex capture groups already
# prevent metacharacters, but this is an explicit defense-in-depth checkpoint.
_SHELL_UNSAFE_RE = re.compile(r'["\';`$|&<>()\\\n\r\t]')


def _sharg(s: str) -> str:
    """Strip characters that could break out of a double-quoted shell argument."""
    return _SHELL_UNSAFE_RE.sub("", s).strip()


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
            return QuickAction("play_on_app", {"song": after, "app": "youtube"})
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
        if _is_protected_proc(raw):
            return None  # fall through to Brain — never kill system/runtime processes
        return QuickAction("quit_app", {"app": _resolve_app(raw), "force": False})

    def force_quit_app(m):
        raw = (m.group("app") or "").strip()
        if not raw or _AMBIGUOUS_APP_RE.match(raw) or _DEVICE_RE.match(raw):
            return None
        if _is_protected_proc(raw):
            return None  # fall through to Brain
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

    # ── System stats (new) ───────────────────────────────────────────────────

    def sys_stat(m):
        return QuickAction("sys_stat", {"kind": m.lastgroup or m.group(1).lower()})

    # ── App focus / switch (new) ─────────────────────────────────────────────

    def focus_app(m):
        raw = (m.group("app") or "").strip()
        if not raw:
            return None
        return QuickAction("focus_app", {"app": _resolve_app(raw)})

    # ── Unit conversion (new) ────────────────────────────────────────────────

    def unit_convert(m):
        try:
            val = float(m.group("val").replace(",", ""))
        except (ValueError, IndexError):
            return None
        src = re.sub(r"\s+", " ", (m.group("src") or "").strip().lower())
        dst = re.sub(r"\s+", " ", (m.group("dst") or "").strip().lower())
        if not src or not dst:
            return None
        return QuickAction("unit_convert", {"val": val, "src": src, "dst": dst})

    # ── World clock (new) ────────────────────────────────────────────────────

    def world_clock(m):
        city = re.sub(r"\s+", " ", (m.group("city") or "").strip().lower())
        if not city:
            return None
        return QuickAction("world_clock", {"city": city})

    # ── Base conversion (new) ────────────────────────────────────────────────

    def base_convert(m):
        raw = (m.group("num") or "").strip().lower()
        base = (m.group("base") or "").strip().lower()
        if not raw or not base:
            return None
        return QuickAction("base_convert", {"num": raw, "base": base})

    # ── Random / dice / coin (new) ───────────────────────────────────────────

    def coin_flip(m):
        return QuickAction("coin_flip")

    def dice_roll(m):
        sides = int(m.group("sides") or 6)
        count = int(m.group("count") or 1)
        return QuickAction("dice_roll", {"sides": sides, "count": count})

    def random_num(m):
        try:
            lo = int(m.group("lo"))
            hi = int(m.group("hi"))
        except (IndexError, TypeError, ValueError):
            return None
        return QuickAction("random_num", {"lo": lo, "hi": hi})

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
        # Specific "play X" forms must come before the generic ^play.* catch-all.
        (P(r"^(play\s*pause|toggle\s+(music|playback))$", re.I), media_toggle),
        (P(r"^(next|play\s+next)(\s+(track|song))?$", re.I), media_next),
        (P(r"^play\s*(?P<after>.*)$", re.I), media_play),
        (
            P(r"^(pause|stop(\s+(music|playing|playback|the\s+music))?)$", re.I),
            media_pause,
        ),
        # FIX: dedicated builder — media_play builder calls m.group("after") which
        # only exists in the ^play pattern above, not in resume/continue patterns.
        (P(r"^(resume|continue\s+music|continue\s+playing)$", re.I), media_resume),
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

        # ── System stats ─────────────────────────────────────────────────────
        (
            P(
                r"^(how\s+much\s+|what\s+is\s+(my\s+|the\s+)?)?"
                r"(?P<cpu>cpu(\s+usage)?|processor(\s+usage)?)$",
                re.I,
            ),
            lambda m: QuickAction("sys_stat", {"kind": "cpu"}),
        ),
        (
            P(
                r"^(how\s+much\s+(ram|memory|free\s+(ram|memory)|available\s+(ram|memory))"
                r"(\s+(is\s+(left|available)|do\s+i\s+have))?"
                r"|what\s+is\s+(my\s+|the\s+)?(ram|memory)(\s+usage)?"
                r"|ram(\s+usage)?|memory(\s+usage)?"
                r"|free\s+(ram|memory)|available\s+(ram|memory))$",
                re.I,
            ),
            lambda m: QuickAction("sys_stat", {"kind": "ram"}),
        ),
        (
            P(
                r"^(how\s+much\s+|what\s+is\s+(my\s+|the\s+)?)?"
                r"(?P<disk>disk(\s+(space|usage))?|storage(\s+left)?|"
                r"free\s+(disk|storage)|available\s+(disk|storage))$",
                re.I,
            ),
            lambda m: QuickAction("sys_stat", {"kind": "disk"}),
        ),
        (
            P(
                r"^(how\s+long\s+(has\s+)?(my\s+)?(computer|pc|mac|system|laptop)\s+(been\s+)?"
                r"(on|running|up)\??|system\s+uptime|uptime|how\s+long\s+since\s+reboot)$",
                re.I,
            ),
            lambda m: QuickAction("sys_stat", {"kind": "uptime"}),
        ),
        (
            P(
                r"^(what\s+is\s+(my\s+)?|show\s+(my\s+)?|my\s+)?"
                r"(local\s+)?ip(\s+address)?$",
                re.I,
            ),
            lambda m: QuickAction("sys_stat", {"kind": "ip"}),
        ),
        (
            P(
                r"^(am\s+i\s+|are\s+we\s+)?connected(\s+to(\s+the)?\s+internet)?"
                r"|check(\s+my)?\s+internet(\s+connection)?$",
                re.I,
            ),
            lambda m: QuickAction("sys_stat", {"kind": "net"}),
        ),

        # ── App focus / switch window ─────────────────────────────────────────
        # "bring spotify to front" / "bring spotify up" — app is mid-phrase
        (P(r"^bring\s+(?P<app>[\w\s]+?)\s+(to\s+front|up)$", re.I), focus_app),
        # "bring up spotify" — verb phrase first
        (P(r"^bring\s+up\s+(?P<app>[\w\s]+)$", re.I), focus_app),
        # "switch to X", "focus X", "show X", "raise X"
        (
            P(
                r"^(switch\s+to|focus|show|raise)\s+(?P<app>[\w\s]+)$",
                re.I,
            ),
            focus_app,
        ),

        # ── Unit conversion ───────────────────────────────────────────────────
        (
            P(
                r"^(convert\s+|how\s+many\s+|what\s+is\s+)?"
                r"(?P<val>[\d,]+(?:\.\d+)?)\s+"
                r"(?P<src>degrees?\s+(?:celsius|fahrenheit|kelvin|c|f|k)"
                r"|celsius|fahrenheit|kelvin"
                r"|km|kilometers?|kilometres?"
                r"|mi|miles?"
                r"|m|meters?|metres?"
                r"|cm|centimeters?|centimetres?"
                r"|ft|feet|foot"
                r"|in|inches?|inch"
                r"|kg|kilograms?"
                r"|lbs?|pounds?"
                r"|g|grams?"
                r"|oz|ounces?"
                r"|kmh|km\s*/\s*h|kilometers?\s+per\s+hour"
                r"|mph|miles?\s+per\s+hour"
                r"|l|liters?|litres?"
                r"|ml|milliliters?|millilitres?"
                r"|gal|gallons?"
                r"|fl\s+oz|fluid\s+ounces?)"
                r"\s+(?:in|to|into|as)\s+"
                r"(?P<dst>degrees?\s+(?:celsius|fahrenheit|kelvin|c|f|k)"
                r"|celsius|fahrenheit|kelvin"
                r"|km|kilometers?|kilometres?"
                r"|mi|miles?"
                r"|m|meters?|metres?"
                r"|cm|centimeters?|centimetres?"
                r"|ft|feet|foot"
                r"|in|inches?|inch"
                r"|kg|kilograms?"
                r"|lbs?|pounds?"
                r"|g|grams?"
                r"|oz|ounces?"
                r"|kmh|km\s*/\s*h|kilometers?\s+per\s+hour"
                r"|mph|miles?\s+per\s+hour"
                r"|l|liters?|litres?"
                r"|ml|milliliters?|millilitres?"
                r"|gal|gallons?"
                r"|fl\s+oz|fluid\s+ounces?)$",
                re.I,
            ),
            unit_convert,
        ),

        # ── World clock ───────────────────────────────────────────────────────
        (
            P(
                r"^(what\s+time\s+is\s+it\s+in|current\s+time\s+in|"
                r"time\s+in|what\s+is\s+the\s+time\s+in)\s+(?P<city>[\w\s]+)$",
                re.I,
            ),
            world_clock,
        ),

        # ── Number base conversion ────────────────────────────────────────────
        (
            P(
                r"^(what\s+is\s+|convert\s+)?(?P<num>0x[\da-f]+|\d+)\s+"
                r"(in|to|as)\s+(?P<base>binary|hex(adecimal)?|decimal|octal)$",
                re.I,
            ),
            base_convert,
        ),

        # ── Coin flip ─────────────────────────────────────────────────────────
        (P(r"^(flip\s+(a\s+)?coin|heads\s+or\s+tails|toss\s+(a\s+)?coin)$", re.I), coin_flip),

        # ── Dice roll ─────────────────────────────────────────────────────────
        (
            P(
                r"^(roll\s+)?(?P<count>[2-9]|1[0-9])?\s*"
                r"(d(?P<sides>4|6|8|10|12|20|100)|dice?|die)$",
                re.I,
            ),
            dice_roll,
        ),
        (P(r"^roll\s+a\s+(dice?|die)$", re.I), lambda m: QuickAction("dice_roll", {"sides": 6, "count": 1})),

        # ── Random number ─────────────────────────────────────────────────────
        (
            P(
                r"^(give\s+me\s+a\s+|pick\s+a\s+|generate\s+a?\s+)?"
                r"random\s+(number\s+)?"
                r"(between\s+(?P<lo>\d+)\s+and\s+(?P<hi>\d+)"
                r"|from\s+(?P<lo2>\d+)\s+to\s+(?P<hi2>\d+))$",
                re.I,
            ),
            lambda m: QuickAction("random_num", {
                "lo": int(m.group("lo") or m.group("lo2") or 1),
                "hi": int(m.group("hi") or m.group("hi2") or 100),
            }),
        ),
        (
            P(r"^(give\s+me\s+a\s+|pick\s+a\s+|generate\s+a?\s+)?random\s+number$", re.I),
            lambda m: QuickAction("random_num", {"lo": 1, "hi": 100}),
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
    app = _sharg(p.get("app", ""))
    if not app:
        raise RuntimeError("No app specified.")
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
    app = _sharg(p.get("app", ""))
    if not app or _is_protected_proc(app):
        return f"Cannot restart {app!r} — it is a protected system or runtime process."
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
        # Exact process name match — NOT -f (full cmdline)
        _run(f'pkill -x "{app}"')
        time.sleep(0.5)
        _run(f'xdg-open "{app}"')
    return f"Restarting {app}."


@_action("quit_app")
def _quit_app(p):
    app = _sharg(p.get("app", ""))
    force = bool(p.get("force", False))

    # Handler-level guard — builder already blocks these, but defence-in-depth.
    if not app or _is_protected_proc(app):
        return f"Cannot quit {app!r} — it is a protected system or runtime process."

    if _IS_MAC:
        _run(f"osascript -e 'tell application \"{app}\" to quit'")
        if force:
            time.sleep(0.5)
            # Use exact process-name match (-x), NOT -f (full cmdline).
            _run(f'pkill -9 -x "{app}"')
    elif _IS_WIN:
        exe = app if app.endswith(".exe") else f"{app}.exe"
        exe = _sharg(exe)
        flag = "/F" if force else ""
        _run(f'taskkill /IM "{exe}" {flag}'.strip())
    else:
        signal_flag = "-9" if force else "-15"
        # Use exact process-name match (-x), NOT -f (full cmdline).
        _run(f'pkill {signal_flag} -x "{app}"')
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
    url = _sharg(p.get("url", ""))
    if not url:
        return "No URL specified."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # Use shell=False (list form) — URL goes as a literal argument, not shell-expanded.
    try:
        if _IS_MAC:
            subprocess.run(["open", url], check=False, timeout=5)
        elif _IS_WIN:
            subprocess.run(["start", "", url], shell=True, check=False, timeout=5)
        else:
            subprocess.run(["xdg-open", url], check=False, timeout=5)
    except Exception as exc:
        return f"Could not open URL: {exc}"
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


# ── System stats ─────────────────────────────────────────────────────────────


@_action("sys_stat")
def _sys_stat(p):
    if not _PSUTIL_OK and p.get("kind") not in ("ip", "net"):
        return "psutil is not installed. Run: pip install psutil"

    kind = p.get("kind", "")

    if kind == "cpu":
        pct = _psutil.cpu_percent(interval=0.5)
        return f"CPU usage: {pct:.1f}%."

    if kind == "ram":
        vm = _psutil.virtual_memory()
        used_gb = vm.used / 1e9
        total_gb = vm.total / 1e9
        return (
            f"RAM: {used_gb:.1f} GB used of {total_gb:.1f} GB "
            f"({vm.percent:.1f}% used, {(100 - vm.percent):.1f}% free)."
        )

    if kind == "disk":
        disk_root = "C:\\" if _IS_WIN else "/"
        du = _psutil.disk_usage(disk_root)
        free_gb = du.free / 1e9
        total_gb = du.total / 1e9
        return (
            f"Disk: {free_gb:.1f} GB free of {total_gb:.1f} GB "
            f"({du.percent:.1f}% used)."
        )

    if kind == "uptime":
        boot_ts = _psutil.boot_time()
        elapsed = time.time() - boot_ts
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        boot_str = datetime.fromtimestamp(boot_ts).strftime("%Y-%m-%d %H:%M")
        return f"System up for {h}h {m}m (booted {boot_str})."

    if kind == "ip":
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            ip = "unavailable"
        return f"Local IP: {ip}."

    if kind == "net":
        try:
            socket.setdefaulttimeout(3)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
            return "Connected to the internet."
        except OSError:
            return "No internet connection detected."

    return f"Unknown stat: {kind!r}"


# ── App focus / switch window ─────────────────────────────────────────────────


@_action("focus_app")
def _focus_app(p):
    app = _sharg(p.get("app", ""))
    if not app:
        return "No app specified."

    if _IS_MAC:
        # osascript takes the app name as part of the AppleScript string — use list form
        # to avoid any shell interpretation of the app name.
        result = subprocess.run(
            ["osascript", "-e", f'tell application "{app}" to activate'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return f"Switched to {app}."
        return f"Could not focus {app}: {result.stderr.strip()}"

    if _IS_WIN:
        script = (
            f'$p=(Get-Process | Where-Object {{$_.MainWindowTitle -match "{app}"}} '
            f'| Select-Object -First 1);'
            f'if($p){{$wsh=New-Object -ComObject WScript.Shell;$wsh.AppActivate($p.Id)}}'
        )
        code, out = _run(f'powershell -NoProfile -Command "{script}"')
        if code == 0:
            return f"Switched to {app}."
        return f"Could not focus {app}: {out}"

    # Linux — try wmctrl then xdotool
    if shutil.which("wmctrl"):
        code, _ = _run(f'wmctrl -a "{app}"')
        if code == 0:
            return f"Switched to {app}."
    if shutil.which("xdotool"):
        code, _ = _run(f'xdotool search --name "{app}" windowactivate --sync')
        if code == 0:
            return f"Switched to {app}."
    return f"Could not focus {app}. Install wmctrl or xdotool."


# ── Unit conversion ───────────────────────────────────────────────────────────

# Canonical unit names after alias resolution
_UNIT_ALIASES: dict[str, str] = {
    # temperature
    "c": "celsius", "f": "fahrenheit", "k": "kelvin",
    "degree celsius": "celsius", "degrees celsius": "celsius",
    "degree fahrenheit": "fahrenheit", "degrees fahrenheit": "fahrenheit",
    "degree kelvin": "kelvin", "degrees kelvin": "kelvin",
    "degree c": "celsius", "degrees c": "celsius",
    "degree f": "fahrenheit", "degrees f": "fahrenheit",
    "degree k": "kelvin", "degrees k": "kelvin",
    # length
    "kilometer": "km", "kilometers": "km", "kilometre": "km", "kilometres": "km",
    "mile": "mi", "miles": "mi",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "foot": "ft", "feet": "ft",
    "inch": "in", "inches": "in",
    # weight
    "kilogram": "kg", "kilograms": "kg",
    "pound": "lb", "pounds": "lb", "lbs": "lb",
    "gram": "g", "grams": "g",
    "ounce": "oz", "ounces": "oz",
    # speed
    "kmh": "km/h", "km/h": "km/h", "kilometers per hour": "km/h",
    "kilometres per hour": "km/h", "km per hour": "km/h",
    "mph": "mph", "miles per hour": "mph",
    # volume
    "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "milliliter": "ml", "milliliters": "ml",
    "millilitre": "ml", "millilitres": "ml",
    "gallon": "gal", "gallons": "gal",
    "fluid ounce": "fl oz", "fluid ounces": "fl oz",
}

# Conversion factors to a canonical base unit per category
# temperature handled separately (non-linear)
_LENGTH_TO_M: dict[str, float] = {
    "km": 1000, "m": 1, "cm": 0.01, "mi": 1609.344, "ft": 0.3048, "in": 0.0254,
}
_WEIGHT_TO_KG: dict[str, float] = {
    "kg": 1, "g": 0.001, "lb": 0.453592, "oz": 0.0283495,
}
_SPEED_TO_KMH: dict[str, float] = {
    "km/h": 1, "mph": 1.60934,
}
_VOLUME_TO_L: dict[str, float] = {
    "l": 1, "ml": 0.001, "gal": 3.78541, "fl oz": 0.0295735,
}


def _canonical_unit(raw: str) -> str:
    r = raw.strip().lower()
    return _UNIT_ALIASES.get(r, r)


def _convert_units(val: float, src: str, dst: str) -> str:
    src = _canonical_unit(src)
    dst = _canonical_unit(dst)

    if src == dst:
        return f"{val:g} {src} = {val:g} {dst}."

    # Temperature (non-linear)
    _TEMP = {"celsius", "fahrenheit", "kelvin"}
    if src in _TEMP or dst in _TEMP:
        if src == "celsius" and dst == "fahrenheit":
            r = val * 9 / 5 + 32
        elif src == "fahrenheit" and dst == "celsius":
            r = (val - 32) * 5 / 9
        elif src == "celsius" and dst == "kelvin":
            r = val + 273.15
        elif src == "kelvin" and dst == "celsius":
            r = val - 273.15
        elif src == "fahrenheit" and dst == "kelvin":
            r = (val - 32) * 5 / 9 + 273.15
        elif src == "kelvin" and dst == "fahrenheit":
            r = (val - 273.15) * 9 / 5 + 32
        else:
            return f"Cannot convert {src} to {dst}."
        return f"{val:g} {src} = {r:.4g} {dst}."

    # Linear categories
    for table, label in (
        (_LENGTH_TO_M, "length"),
        (_WEIGHT_TO_KG, "weight"),
        (_SPEED_TO_KMH, "speed"),
        (_VOLUME_TO_L, "volume"),
    ):
        if src in table and dst in table:
            base = val * table[src]
            result = base / table[dst]
            def _fmt(n: float) -> str:
                return f"{n:.6g}"
            return f"{val:g} {src} = {_fmt(result)} {dst}."

    return f"Cannot convert {src!r} to {dst!r} — incompatible units."


@_action("unit_convert")
def _unit_convert(p):
    try:
        val = float(p["val"])
    except (KeyError, ValueError):
        return "Could not parse the value."
    return _convert_units(val, str(p.get("src", "")), str(p.get("dst", "")))


# ── World clock ───────────────────────────────────────────────────────────────

_CITY_TZ: dict[str, str] = {
    # Americas
    "new york": "America/New_York", "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "chicago": "America/Chicago", "houston": "America/Chicago",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    "mexico city": "America/Mexico_City",
    "sao paulo": "America/Sao_Paulo", "buenos aires": "America/Argentina/Buenos_Aires",
    "bogota": "America/Bogota", "lima": "America/Lima",
    # Europe
    "london": "Europe/London", "uk": "Europe/London",
    "paris": "Europe/Paris", "france": "Europe/Paris",
    "berlin": "Europe/Berlin", "germany": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam", "brussels": "Europe/Brussels",
    "zurich": "Europe/Zurich", "vienna": "Europe/Vienna",
    "warsaw": "Europe/Warsaw", "prague": "Europe/Prague",
    "stockholm": "Europe/Stockholm", "oslo": "Europe/Oslo",
    "helsinki": "Europe/Helsinki", "lisbon": "Europe/Lisbon",
    "athens": "Europe/Athens", "budapest": "Europe/Budapest",
    "moscow": "Europe/Moscow", "russia": "Europe/Moscow",
    "istanbul": "Europe/Istanbul", "turkey": "Europe/Istanbul",
    # Asia / Pacific
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai",
    "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata", "india": "Asia/Kolkata",
    "karachi": "Asia/Karachi", "pakistan": "Asia/Karachi",
    "dhaka": "Asia/Dhaka", "bangladesh": "Asia/Dhaka",
    "colombo": "Asia/Colombo", "kathmandu": "Asia/Kathmandu",
    "tashkent": "Asia/Tashkent",
    "bangkok": "Asia/Bangkok", "thailand": "Asia/Bangkok",
    "singapore": "Asia/Singapore",
    "kuala lumpur": "Asia/Kuala_Lumpur", "malaysia": "Asia/Kuala_Lumpur",
    "jakarta": "Asia/Jakarta", "indonesia": "Asia/Jakarta",
    "hong kong": "Asia/Hong_Kong",
    "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai", "china": "Asia/Shanghai",
    "taipei": "Asia/Taipei", "taiwan": "Asia/Taipei",
    "seoul": "Asia/Seoul", "korea": "Asia/Seoul",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo",
    "sydney": "Australia/Sydney", "australia": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "brisbane": "Australia/Brisbane",
    "auckland": "Pacific/Auckland", "new zealand": "Pacific/Auckland",
    # Africa
    "cairo": "Africa/Cairo", "egypt": "Africa/Cairo",
    "johannesburg": "Africa/Johannesburg", "south africa": "Africa/Johannesburg",
    "nairobi": "Africa/Nairobi", "kenya": "Africa/Nairobi",
    "lagos": "Africa/Lagos", "nigeria": "Africa/Lagos",
    "casablanca": "Africa/Casablanca",
}


@_action("world_clock")
def _world_clock(p):
    if not _ZONEINFO_OK:
        return "Timezone data not available. Run: pip install tzdata"
    city = str(p.get("city", "")).strip().lower()
    tz_name = _CITY_TZ.get(city)
    if not tz_name:
        return f"Don't know the timezone for {city!r}. Try a major city name."
    try:
        tz = _zoneinfo.ZoneInfo(tz_name)
        now = datetime.now(tz)
        return f"It's {now.strftime('%H:%M')} in {city.title()} ({now.strftime('%A, %d %b %Y')}, {tz_name})."
    except Exception as exc:
        return f"Could not get time for {city}: {exc}"


# ── Number base conversion ────────────────────────────────────────────────────


@_action("base_convert")
def _base_convert(p):
    raw = str(p.get("num", "")).strip().lower()
    base = str(p.get("base", "")).strip().lower()

    # Parse input (supports 0x prefix for hex input)
    try:
        if raw.startswith("0x"):
            n = int(raw, 16)
            src_label = f"{raw} (hex)"
        else:
            n = int(raw, 10)
            src_label = f"{n} (decimal)"
    except ValueError:
        return f"Could not parse number: {raw!r}"

    base_norm = base.rstrip("adecimlo")  # "hex", "hexadecimal" → "hex"
    if "hex" in base:
        result = hex(n)
        label = "hex"
    elif base == "binary":
        result = bin(n)
        label = "binary"
    elif base == "octal":
        result = oct(n)
        label = "octal"
    elif base == "decimal":
        result = str(n)
        label = "decimal"
    else:
        return f"Unknown base: {base!r}"

    return f"{src_label} = {result} ({label})."


# ── Coin flip / dice roll / random ────────────────────────────────────────────


@_action("coin_flip")
def _coin_flip(p):
    result = _random_mod.choice(["Heads", "Tails"])
    return f"{result}!"


@_action("dice_roll")
def _dice_roll(p):
    sides = int(p.get("sides", 6))
    count = max(1, int(p.get("count", 1)))
    if sides < 2:
        return "A die needs at least 2 sides."
    rolls = [_random_mod.randint(1, sides) for _ in range(count)]
    if count == 1:
        return f"Rolled a d{sides}: {rolls[0]}."
    return f"Rolled {count}d{sides}: {rolls} (total: {sum(rolls)})."


@_action("random_num")
def _random_num(p):
    try:
        lo = int(p.get("lo", 1))
        hi = int(p.get("hi", 100))
    except (ValueError, TypeError):
        return "Invalid range."
    if lo > hi:
        lo, hi = hi, lo
    result = _random_mod.randint(lo, hi)
    return f"Random number between {lo} and {hi}: {result}."


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

    def __init__(self) -> None:
        check_intent_deps(auto_install=True)

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
