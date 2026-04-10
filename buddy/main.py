# buddy/main.py  —  Aurora Gradient Theme
"""
Terminal UI — Aurora theme, unified with boot_ui.py palette.

Architecture:
  run_terminal()  — main async runtime (signal handling, tasks, main loop, shutdown)
  TerminalUI      — spinner, bubble rendering, input/output
  RuntimeActions  — voice mute, sleep/wake, consolidation lifecycle
  InputMux        — multiplexes typed + voice input into one async stream
  typed_producer  — asyncio task that drives PT reads into InputMux
  _inactivity_watcher — asyncio task for idle-based auto-sleep

Signal handling:
  SIGINT first press  → request_interrupt() (interrupts current turn)
  SIGINT second press → quit (within SIGINT_EXIT_WINDOW_S = 1.25s)
  SIGWINCH            → invalidate terminal width cache + PT redraw

Interrupt path:
  _handle_sigint → interrupt_event.set() (immediate, stops LLM stream)
                 → loop.call_soon_threadsafe(request_interrupt)
  request_interrupt → _pt_submit_from_other_thread (abort PT prompt if active)
                    → mux.push(INTERRUPT_SENTINEL) (unblock pipeline_input)
                    → active_turn_task.cancel()
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, fields
from enum import Enum
from functools import lru_cache
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from buddy.buddy_core.bootstrap_llama import bootstrap
from buddy.buddy_core.pipeline import handle_turn
from buddy.logger.logger import get_logger
from buddy.ui.boot_ui import AURORA, _ANSI_RE, _is_tty  # single palette source
from buddy.ui.stt import SpeechToText

try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass

# Windows ANSI support
if sys.platform == "win32":
    try:
        import colorama  # type: ignore

        colorama.just_fix_windows_console()
    except ImportError:
        pass

logger = get_logger("main")


# ==========================================================
# Constants & Sentinels
# ==========================================================

EXIT_SENTINEL = "__EXIT__"
INTERRUPT_SENTINEL = "__INTERRUPT__"

PROMPTS = {
    "input": "▌ ",
    "voice": "◎ ",
    "thinking": "◌ ",
    "response": "◈ ",
}

_ANIMATIONS_ENABLED = not bool(os.environ.get("NO_MOTION"))

_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None

_BANNER_META_LOCK = threading.RLock()
_BANNER_META: str = ""

_TERM_CACHE_LOCK = threading.RLock()
_TERM_WIDTH_CACHE: Optional[int] = None
_TERM_COLOR_SUPPORT: Optional[int] = None


# ==========================================================
# Enums & State
# ==========================================================


class VoiceCmd(Enum):
    NONE = "none"
    STOP = "stop"
    SLEEP = "sleep"
    WAKE = "wake"
    MUTE = "mute"
    UNMUTE = "unmute"
    TOGGLE_MUTE = "toggle_mute"


class SpinnerState(Enum):
    THINKING = "thinking"
    WAITING = "waiting"
    WORKING = "working"


@dataclass
class SystemState:
    sleeping: bool = False
    consolidating: bool = False  # True while background consolidation is running
    voice_muted: bool = False
    pipeline_running: bool = False
    last_voice_cmd_ts: float = 0.0


# ==========================================================
# Command Matching
# ==========================================================


def match_voice_command(text: str) -> VoiceCmd:
    t = text.strip().lower()
    if not t:
        return VoiceCmd.NONE
    if t in {"stop", "buddy stop", "hey buddy stop", "cancel", "interrupt", "stop."}:
        return VoiceCmd.STOP
    if t in {"sleep", "buddy sleep", "go to sleep", "hey buddy sleep", "sleep."}:
        return VoiceCmd.SLEEP
    if t in {"wake", "wake up", "buddy wake", "buddy wake up", "hey buddy wake up"}:
        return VoiceCmd.WAKE
    if t in {"mute", "mute voice", "voice mute", "buddy mute", "mute."}:
        return VoiceCmd.MUTE
    if t in {"unmute", "unmute voice", "voice unmute", "buddy unmute"}:
        return VoiceCmd.UNMUTE
    if t in {"toggle mute", "toggle voice", "toggle voice mute"}:
        return VoiceCmd.TOGGLE_MUTE
    return VoiceCmd.NONE


def _should_exit(text: str) -> bool:
    return (text or "").strip().lower() in {"exit", "quit", "q", ":q"}


def _is_interrupt_cmd(text: str) -> bool:
    return (text or "").strip().lower() in {
        "!",
        "/stop",
        "stop",
        "cancel",
        "/cancel",
        "/interrupt",
        INTERRUPT_SENTINEL.lower(),
    }


# ==========================================================
# Terminal Capability Detection
# ==========================================================


def _detect_color_support() -> int:
    """Detect terminal color depth: 8 / 16 / 256 / 16 777 216 (truecolor)."""
    global _TERM_COLOR_SUPPORT

    with _TERM_CACHE_LOCK:
        if _TERM_COLOR_SUPPORT is not None:
            return _TERM_COLOR_SUPPORT

        colorterm = os.environ.get("COLORTERM", "").lower()
        if colorterm in ("truecolor", "24bit"):
            result = 16_777_216
        else:
            term = os.environ.get("TERM", "").lower()
            if "256color" in term or "kitty" in term:
                result = 256
            elif "color" in term:
                result = 16
            else:
                result = _probe_tput_colors()

        _TERM_COLOR_SUPPORT = result
        return result


def _probe_tput_colors() -> int:
    """Ask tput how many colors are available; fall back to 8."""
    if sys.platform == "win32":
        return 8
    try:
        import subprocess

        r = subprocess.run(
            ["tput", "colors"], capture_output=True, text=True, timeout=0.5
        )
        if r.returncode == 0:
            return int(r.stdout.strip())
    except Exception:
        pass
    return 8


def _detect_theme() -> str:
    """
    Auto-detect whether the terminal background is light or dark.
    Returns "light" or "dark".  Falls back to "dark".
    """
    # 1. COLORFGBG (set by many terminal emulators)
    cfb = os.environ.get("COLORFGBG")
    if cfb and ";" in cfb:
        try:
            _, bg = cfb.split(";", 1)
            return "dark" if int(bg) <= 6 else "light"
        except Exception:
            pass

    # 2. macOS system appearance
    if sys.platform == "darwin":
        try:
            import subprocess

            r = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            return "dark" if r.returncode == 0 else "light"
        except Exception:
            pass

    # 3. TERM env hint
    if "light" in os.environ.get("TERM", "").lower():
        return "light"

    return "dark"


def _invalidate_term_cache() -> None:
    """Invalidate terminal width cache on resize."""
    global _TERM_WIDTH_CACHE
    with _TERM_CACHE_LOCK:
        _TERM_WIDTH_CACHE = None


# ==========================================================
# Terminal Utilities
# ==========================================================


class ANSI:
    RESET = "\x1b[0m"
    BOLD = "\x1b[1m"
    DIM = "\x1b[2m"


_isatty = _is_tty  # single implementation lives in boot_ui.py


def _no_color() -> bool:
    return bool(os.environ.get("NO_COLOR"))


def _use_color() -> bool:
    return _isatty() and not _no_color()


def _c(code: str) -> str:
    """Return an ANSI escape code only when color is supported."""
    return code if _use_color() else ""


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s or "")


try:
    from wcwidth import wcswidth  # type: ignore
except Exception:

    def wcswidth(s: str) -> int:  # type: ignore[misc]
        return len(s)


@lru_cache(maxsize=2048)
def _cached_wcswidth(s: str) -> int:
    """Cached wcwidth with fallback for surrogate/unknown codepoints."""
    w = wcswidth(s)
    return w if w >= 0 else len(s)


def _disp_len(s: str) -> int:
    return max(0, int(_cached_wcswidth(_strip_ansi(s))))


def _pad_to_disp(s: str, width: int) -> str:
    cur = _disp_len(s)
    return s if cur >= width else s + (" " * (width - cur))


def _term_width(default: int = 96) -> int:
    global _TERM_WIDTH_CACHE
    with _TERM_CACHE_LOCK:
        if _TERM_WIDTH_CACHE is None:
            try:
                _TERM_WIDTH_CACHE = shutil.get_terminal_size((default, 20)).columns
            except Exception:
                _TERM_WIDTH_CACHE = default
        return _TERM_WIDTH_CACHE


def _clear_current_line() -> None:
    if not _isatty():
        return
    sys.stdout.write("\r\x1b[2K")
    sys.stdout.flush()


def _clear_prompt_area() -> None:
    if not _isatty():
        return
    sys.stdout.write("\r\x1b[2K\x1b[J")
    sys.stdout.flush()


def _echo_reset() -> None:
    if _use_color():
        sys.stdout.write(ANSI.RESET)
        sys.stdout.flush()


# ==========================================================
# Config Helpers
# ==========================================================


def _get_cfg(state: Any) -> Dict[str, Any]:
    cfg = getattr(state, "config", None)
    return cfg if isinstance(cfg, dict) else {}


def _get_nested(d: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(k, {})
    return cur if isinstance(cur, dict) else {}


# ==========================================================
# Banner Meta (toolbar status string)
# ==========================================================


def _set_banner_meta(s: str) -> None:
    global _BANNER_META
    with _BANNER_META_LOCK:
        _BANNER_META = (s or "").strip()


_HELP_TEXT = "Esc+Enter:submit  •  F2:mute  •  F3:sleep  •  /stop:interrupt  •  Ctrl+C×2:quit"


def _get_banner_meta() -> str:
    with _BANNER_META_LOCK:
        return _BANNER_META or _HELP_TEXT


# ==========================================================
# prompt_toolkit Invalidation
# ==========================================================

_PT_LOCK = threading.RLock()
_PT_ACTIVE_APP = None


def _pt_invalidate() -> None:
    with _PT_LOCK:
        app = _PT_ACTIVE_APP
    if app is None:
        return
    loop = _MAIN_LOOP
    if loop is None:
        try:
            app.invalidate()
        except Exception:
            pass
        return
    loop.call_soon_threadsafe(_safe_invalidate, app)


def _safe_invalidate(app) -> None:  # type: ignore[type-arg]
    try:
        app.invalidate()
    except Exception:
        pass


# ==========================================================
# Aurora Color Palette (mapped from AURORA dict)
# ==========================================================


def _aurora_palette(theme: str = "dark") -> Dict[str, str]:
    """
    Return ANSI color strings keyed by bubble role.
    Maps directly to the AURORA dict from boot_ui so the entire app
    shares one visual identity.
    """
    color_support = _detect_color_support()

    if color_support >= 256:
        if theme == "light":
            return {
                "user": AURORA["logo_r3"],  # blue/indigo
                "buddy": AURORA["logo_r5"],  # bright violet
                "meta": AURORA["dim"],
                "border": AURORA["logo_r2"],  # bright blue
            }
        else:
            return {
                "user": AURORA["logo_r0"],  # bright cyan  — user messages top of arc
                "buddy": AURORA[
                    "logo_r5"
                ],  # bright violet — buddy responses bottom of arc
                "meta": AURORA["dim"],
                "border": AURORA["logo_r2"],  # bright blue  — bubble borders mid-arc
            }
    elif color_support >= 16:
        return {
            "user": "\x1b[96m",  # bright cyan
            "buddy": "\x1b[95m",  # bright magenta/violet
            "meta": "\x1b[2m",
            "border": "\x1b[36m",  # cyan
        }
    else:
        return {
            "user": "\x1b[36m",  # cyan
            "buddy": "\x1b[35m",  # magenta
            "meta": "\x1b[2m",
            "border": "\x1b[37m",  # white
        }


# ==========================================================
# Toolbar
# ==========================================================


def _banner_toolbar_text(
    sys_state: SystemState, state_lock: threading.RLock
) -> FormattedText:
    """Compact aurora-styled toolbar with status indicators."""
    title = "▛▜▀▛"

    with state_lock:
        label_map = {
            "sleeping": "SLEP",
            "consolidating": "CNSL",
            "voice_muted": "VOIC",
            "pipeline_running": "PIPE",
        }
        bool_fields: List[Tuple[str, bool]] = [
            (label_map.get(f.name, f.name[:4].upper()), getattr(sys_state, f.name))
            for f in fields(sys_state)
            if isinstance(getattr(sys_state, f.name, None), bool)
        ]

    meta = _get_banner_meta()

    parts: List[Tuple[str, str]] = [("class:toolbar.title", f"{title} ")]
    active = [label for label, v in bool_fields if v]
    status_text = " ".join(f"●{label}" for label in active) if active else "ready"
    parts.append(("class:toolbar.status", status_text))
    if meta.strip():
        parts.append(("class:toolbar", " │ "))
        parts.append(("class:toolbar.meta", meta.strip()))
    return FormattedText(parts)


def _build_toolbar_style() -> Style:
    """
    Build a prompt_toolkit Style matching the Aurora palette.
    Auto-detects light vs dark terminal.
    """
    color_support = _detect_color_support()
    theme = _detect_theme()

    if color_support >= 256:
        if theme == "light":
            return Style.from_dict({
                "toolbar": "bg:#1a1a2e #e0e0e0",
                "toolbar.title": "bg:#1a1a2e #00e5ff bold",
                "toolbar.status": "bg:#1a1a2e #90caf9",
                "toolbar.meta": "bg:#1a1a2e #ce93d8",
            })
        else:
            return Style.from_dict({
                "toolbar": "bg:#0d0d1a #d0d0e0",
                "toolbar.title": "bg:#0d0d1a #00e5ff bold",
                "toolbar.status": "bg:#0d0d1a #81b4fa",
                "toolbar.meta": "bg:#0d0d1a #ce93d8",
            })
    else:
        return Style.from_dict({
            "toolbar": "bg:black white",
            "toolbar.title": "bg:black cyan bold",
            "toolbar.status": "bg:black white",
            "toolbar.meta": "bg:black magenta",
        })


# ==========================================================
# Text Wrapping
# ==========================================================


def _wrap_lines_display(text: str, width: int) -> List[str]:
    """Word-wrap plain text to display width, respecting full-width characters."""
    width = max(8, int(width))
    s = _strip_ansi(text or "")
    if not s:
        return [""]

    out: List[str] = []

    for para in s.splitlines() or [""]:
        if para == "":
            out.append("")
            continue

        tokens = re.split(r"(\s+)", para)
        current: List[str] = []
        line_w = 0

        def _flush() -> None:
            nonlocal current, line_w
            out.append("".join(current).rstrip("\n"))
            current = []
            line_w = 0

        for tok in tokens:
            if not tok:
                continue
            tok_w = _cached_wcswidth(tok)
            if tok_w < 0:
                tok_w = len(tok)

            if tok_w > width:
                # Token longer than width — split character by character
                if current:
                    _flush()
                chunk: List[str] = []
                chunk_w = 0
                for ch in tok:
                    ch_w = max(1, _cached_wcswidth(ch))
                    if chunk_w + ch_w > width:
                        out.append("".join(chunk))
                        chunk, chunk_w = [ch], ch_w
                    else:
                        chunk.append(ch)
                        chunk_w += ch_w
                if chunk:
                    out.append("".join(chunk))
                continue

            if line_w + tok_w <= width:
                current.append(tok)
                line_w += tok_w
                continue

            # Whitespace token that would push past width — flush without it
            if tok.isspace():
                if current:
                    _flush()
                continue

            _flush()
            current.append(tok)
            line_w = tok_w

        if current or not out:
            out.append("".join(current).rstrip("\n"))

    return out or [""]


# ==========================================================
# Bubble Rendering
# ==========================================================


@lru_cache(maxsize=512)
def _bubble_width_cached(term_w: int, align: str, ratio_int: int) -> int:
    """
    Memoised bubble width.
    ratio_int is ratio × 100 (int) to allow lru_cache with float inputs.
    """
    ratio = ratio_int / 100.0
    term_w = max(40, int(term_w))
    max_w = max(36, term_w - 2)
    base = int(term_w * ratio)
    cap = 84 if align == "right" else 90
    return max(36, min(cap, base, max_w))


def _bubble_width(term_w: int, *, align: str, ratio: float) -> int:
    return _bubble_width_cached(term_w, align, int(ratio * 100))


def _bubble_lines(text: str, *, bubble_width: int, title: str) -> List[str]:
    """Wrap text into a rounded-corner bubble with title."""
    bubble_width = max(28, int(bubble_width))
    inner_w = max(10, bubble_width - 4)

    lines = _wrap_lines_display(text, inner_w)

    title = (title or "").strip()
    top_prefix = f"╭─ {title} "
    fill = max(1, bubble_width - _disp_len(top_prefix) - 1)  # 1 for "╮"
    top = _pad_to_disp(top_prefix + ("─" * fill) + "╮", bubble_width)

    mid: List[str] = [
        _pad_to_disp(f"│ {_pad_to_disp(ln, inner_w)} │", bubble_width) for ln in lines
    ]
    bot = _pad_to_disp("╰" + ("─" * (bubble_width - 2)) + "╯", bubble_width)

    return [top] + mid + [bot]


def _contains_emoji(s: str) -> bool:
    """
    Detect complex emoji that may confuse wcwidth.

    Deliberately narrow: only sequences that genuinely cause rendering
    problems (ZWJ, skin tones, flags, Emoticon block).  Box-drawing
    characters and misc symbols (✓ • ─ ╭ etc.) are NOT treated as emoji.
    """
    if not s:
        return False

    # ZWJ sequences, variation selectors, ZWNJ
    if any(m in s for m in ("\u200d", "\ufe0f", "\u200c")):
        return True

    # Skin tone modifiers
    if any("\U0001f3fb" <= ch <= "\U0001f3ff" for ch in s):
        return True

    # Regional indicators (country flags)
    if any("\U0001f1e6" <= ch <= "\U0001f1ff" for ch in s):
        return True

    # Core Emoticons + Transport & Map + Misc Symbols & Pictographs
    # Deliberately excludes 0x2600–0x27FF (Misc Symbols, Dingbats, Box Drawing)
    for ch in s:
        if 0x1F300 <= ord(ch) <= 0x1F9FF:
            return True

    return False


def _colorize_bubble_line(ln: str, *, body_color: str, border_color: str) -> str:
    b = _c(border_color)
    t = _c(body_color)
    r = _c(ANSI.RESET)

    if not ln.startswith("│"):
        return f"{b}{ln}{r}"

    left, right = "│ ", " │"
    inner = ln[len(left) : -len(right)]
    return f"{b}{left}{t}{inner}{b}{right}{r}"


def _render_bubble_left(
    text: str,
    *,
    title: str,
    body_color: str,
    border_color: str,
    max_ratio: float = 0.72,
) -> List[str]:
    w = _term_width()
    bubble_w = _bubble_width(w, align="left", ratio=max_ratio)
    lines = _bubble_lines(text, bubble_width=bubble_w, title=title)
    return [
        _colorize_bubble_line(ln, body_color=body_color, border_color=border_color)
        for ln in lines
    ]


def _render_bubble_right(
    text: str,
    *,
    title: str,
    body_color: str,
    border_color: str,
    ratio: float = 0.66,
) -> List[str]:
    """Render a right-aligned chat bubble (instant, no animation)."""
    w = _term_width()
    bubble_w = _bubble_width(w, align="right", ratio=ratio)
    lines = _bubble_lines(text, bubble_width=bubble_w, title=title)
    pad = max(2, w - bubble_w - 2)
    prefix = " " * pad
    return [
        prefix
        + _colorize_bubble_line(ln, body_color=body_color, border_color=border_color)
        for ln in lines
    ]


async def _type_bubble_right_with_mutex(
    text: str,
    *,
    title: str,
    body_color: str,
    border_color: str,
    max_ratio: float,
    cps: float,
    min_margin: int,
    io_mutex: threading.RLock,
    animated: bool,
) -> None:
    """
    Render a right-aligned bubble, optionally animating character-by-character.

    Falls back to instant rendering when:
    • animated=False (per-instance flag from TerminalUI)
    • NO_MOTION env var is set
    • text contains complex emoji (would break column accounting)
    """
    if not animated or not _ANIMATIONS_ENABLED:
        lines = _render_bubble_right(
            text,
            title=title,
            body_color=body_color,
            border_color=border_color,
            ratio=max_ratio,
        )
        with io_mutex:
            for ln in lines:
                print(ln, flush=True)
        return

    w = _term_width()
    bubble_w = _bubble_width(w, align="right", ratio=max_ratio)
    lines = _bubble_lines(text, bubble_width=bubble_w, title=title)
    if not lines:
        return

    pad = max(min_margin, w - bubble_w - min_margin)
    prefix = " " * pad

    # Instant path when emoji are present (wcwidth unreliable for multi-codepoint)
    if any(_contains_emoji(ln) for ln in lines):
        with io_mutex:
            for ln in lines:
                print(
                    prefix
                    + _colorize_bubble_line(
                        ln, body_color=body_color, border_color=border_color
                    ),
                    flush=True,
                )
        return

    inner_w = bubble_w - 4
    do_anim = _isatty() and cps > 0
    delay = (1.0 / cps) if do_anim else 0.0

    b = _c(border_color)
    t = _c(body_color)
    r = _c(ANSI.RESET)

    # Top border — always instant
    with io_mutex:
        sys.stdout.write(prefix + f"{b}{lines[0]}{r}\n")
        sys.stdout.flush()

    for ln in lines[1:-1]:
        if not ln.startswith("│"):
            with io_mutex:
                sys.stdout.write(prefix + f"{b}{ln}{r}\n")
                sys.stdout.flush()
            continue

        inner = _pad_to_disp(ln[2:-2], inner_w)
        typed_part = inner.rstrip(" ")
        pad_part = inner[len(typed_part) :]

        with io_mutex:
            sys.stdout.write(prefix + f"{b}│ {r}")
            sys.stdout.flush()

        if not do_anim:
            with io_mutex:
                sys.stdout.write(f"{t}{typed_part}{r}")
                if pad_part:
                    sys.stdout.write(f"{t}{pad_part}{r}")
                sys.stdout.write(f"{b} │{r}\n")
                sys.stdout.flush()
            continue

        # Animate character by character with drift correction
        expected_time = time.perf_counter()

        for i, ch in enumerate(typed_part):
            expected_time += delay
            current_time = time.perf_counter()

            # Drop frames if more than 5 chars behind schedule
            if (expected_time - current_time) < -5 * delay:
                with io_mutex:
                    sys.stdout.write(f"{t}{typed_part[i:]}{r}")
                    sys.stdout.flush()
                break

            with io_mutex:
                sys.stdout.write(f"{t}{ch}{r}")
                sys.stdout.flush()

            sleep_for = expected_time - time.perf_counter()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

        if pad_part:
            with io_mutex:
                sys.stdout.write(f"{t}{pad_part}{r}")
                sys.stdout.flush()

        with io_mutex:
            sys.stdout.write(f"{b} │{r}\n")
            sys.stdout.flush()

    # Bottom border — always instant
    with io_mutex:
        sys.stdout.write(prefix + f"{b}{lines[-1]}{r}\n")
        sys.stdout.flush()


# ==========================================================
# prompt_toolkit Setup
# ==========================================================

_kb = KeyBindings()


@_kb.add("escape", "enter")
def _kb_submit(event) -> None:  # type: ignore[type-arg]
    event.current_buffer.validate_and_handle()


@_kb.add("c-l")
def _kb_clear(event) -> None:  # type: ignore[type-arg]
    event.app.renderer.clear()


# Hotkeys wired at runtime by _wire_hotkeys()
_RUNTIME_HOTKEYS: Dict[str, Optional[Callable[[], Any]]] = {
    "toggle_mute": None,
    "toggle_sleep": None,
    "mute": None,
    "unmute": None,
    "sleep": None,
    "wake": None,
}


@_kb.add("f2")
def _kb_f2(event) -> None:  # type: ignore[type-arg]
    fn = _RUNTIME_HOTKEYS.get("toggle_mute")
    if fn:
        fn()


@_kb.add("f3")
def _kb_f3(event) -> None:  # type: ignore[type-arg]
    fn = _RUNTIME_HOTKEYS.get("toggle_sleep")
    if fn:
        fn()


@_kb.add("f4")
def _kb_f4(event) -> None:  # type: ignore[type-arg]
    fn = _RUNTIME_HOTKEYS.get("wake")
    if fn:
        fn()


@_kb.add("f5")
def _kb_f5(event) -> None:  # type: ignore[type-arg]
    fn = _RUNTIME_HOTKEYS.get("sleep")
    if fn:
        fn()


@_kb.add("f6")
def _kb_f6(event) -> None:  # type: ignore[type-arg]
    fn = _RUNTIME_HOTKEYS.get("mute")
    if fn:
        fn()


@_kb.add("f7")
def _kb_f7(event) -> None:  # type: ignore[type-arg]
    fn = _RUNTIME_HOTKEYS.get("unmute")
    if fn:
        fn()


# Build style once at module load; reused by _PT_SESSION and _read_typed_blocking
_PT_STYLE = _build_toolbar_style()

try:
    _PT_SESSION = PromptSession(
        history=InMemoryHistory(),
        key_bindings=_kb,
        multiline=True,
        erase_when_done=True,
        style=_PT_STYLE,
    )
except TypeError:
    # Older prompt_toolkit versions don't have erase_when_done
    _PT_SESSION = PromptSession(
        history=InMemoryHistory(),
        key_bindings=_kb,
        multiline=True,
        style=_PT_STYLE,
    )


def _pt_submit_from_other_thread(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    with _PT_LOCK:
        app = _PT_ACTIVE_APP
        if app is None:
            return False
    loop = _MAIN_LOOP
    if loop is None:
        return False
    loop.call_soon_threadsafe(_safe_app_exit, app, t)
    return True


def _safe_app_exit(app, text: str) -> None:
    try:
        app.exit(result=text)
    except Exception:
        logger.exception("pt_submit: app.exit failed")


async def _read_typed_blocking(
    prompt_text: str,
    *,
    sys_state: SystemState,
    state_lock: threading.RLock,
) -> Optional[str]:

    def _blocking() -> Optional[str]:
        global _PT_ACTIVE_APP
        try:
            if not _isatty():
                return input(prompt_text)

            # Do NOT call _clear_prompt_area() here.
            # The spinner was already cleaned up by spinner_stop() → _clear_current_line()
            # before ui_input() called us.  An aggressive \x1b[J here would erase any
            # buddy response or user bubble that was just printed above the cursor.

            def _pre_run() -> None:
                global _PT_ACTIVE_APP
                with _PT_LOCK:
                    _PT_ACTIVE_APP = get_app()

            def _toolbar():
                return _banner_toolbar_text(sys_state, state_lock)

            prompt_continuation = lambda w, l, s: "> ".ljust(
                len(prompt_text)
            )  # noqa: E731

            with patch_stdout(raw=True):
                try:
                    text = _PT_SESSION.prompt(
                        prompt_text,
                        prompt_continuation=prompt_continuation,
                        vi_mode=False,
                        mouse_support=False,
                        pre_run=_pre_run,
                        bottom_toolbar=_toolbar,
                        refresh_interval=0.10,
                    )
                except TypeError:
                    # Older versions don't support refresh_interval
                    text = _PT_SESSION.prompt(
                        prompt_text,
                        prompt_continuation=prompt_continuation,
                        vi_mode=False,
                        mouse_support=False,
                        pre_run=_pre_run,
                        bottom_toolbar=_toolbar,
                    )
            # patch_stdout replays buffered writes here (spinner frames, meta messages
            # that arrived while PT was active).  Do NOT call _clear_prompt_area() after
            # this point — that \x1b[J would erase everything patch_stdout just replayed.
            return text

        except EOFError:
            # Ctrl+D — user wants to quit
            _echo_reset()
            return None
        except KeyboardInterrupt:
            # Ctrl+C — _handle_sigint already handles this via request_interrupt().
            # Return empty so mux.push discards it; do NOT push EXIT_SENTINEL.
            _echo_reset()
            return ""

        finally:
            with _PT_LOCK:
                _PT_ACTIVE_APP = None
            # Only clear the current cursor line — PT's erase_when_done already removed
            # the prompt itself; this just cleans up any cursor-position artifacts.
            _clear_current_line()

    return await asyncio.to_thread(_blocking)


# ==========================================================
# TerminalUI
# ==========================================================


class TerminalUI:
    def __init__(self, *, prompt_text: str, theme: str, animated: bool) -> None:
        self.prompt_text = prompt_text
        self.animated = animated and _ANIMATIONS_ENABLED
        self.pal = _aurora_palette(theme)

        self._io_mutex = threading.RLock()
        self._out_lock = asyncio.Lock()

        self._spinner_stop = threading.Event()
        self._spinner_pause = threading.Event()
        self._spinner_label = ""
        self._spinner_state = SpinnerState.THINKING
        self._spinner_thread: Optional[threading.Thread] = None
        self._spinner_last_update = 0.0

    # ------------------------------------------------------------------
    # Spinner control
    # ------------------------------------------------------------------

    def spinner_start(
        self, label: str, state: SpinnerState = SpinnerState.THINKING
    ) -> None:
        if not _isatty():
            return
        # Ensure any previous thread is fully stopped before starting a new one.
        # spinner_stop() uses a 0.25 s join timeout; if that elapsed and the thread
        # is still alive (e.g. stuck in a longer sleep frame), signal it again and
        # wait a little longer so we never run two spinner threads simultaneously.
        if self._spinner_thread and self._spinner_thread.is_alive():
            self._spinner_stop.set()
            self._spinner_thread.join(timeout=0.5)
        self._spinner_label = label
        self._spinner_state = state
        self._spinner_pause.clear()
        self._spinner_stop.clear()
        t = threading.Thread(target=self._spin, daemon=True)
        self._spinner_thread = t
        t.start()

    def spinner_update(self, label: str, state: Optional[SpinnerState] = None) -> None:
        if not _isatty():
            return
        now = time.monotonic()
        self._spinner_label = label
        if state:
            self._spinner_state = state
        if (now - self._spinner_last_update) >= 0.04:
            self._spinner_last_update = now

    def spinner_stop(self) -> None:
        self._spinner_stop.set()
        t = self._spinner_thread
        if t is not None:
            try:
                t.join(timeout=0.25)
            except Exception:
                logger.exception("spinner_stop: join failed")
        self._spinner_thread = None
        if _isatty():
            with self._io_mutex:
                _clear_current_line()

    def spinner_pause(self) -> None:
        self._spinner_pause.set()
        if _isatty():
            with self._io_mutex:
                _clear_current_line()

    def spinner_resume(self) -> None:
        self._spinner_pause.clear()

    # ------------------------------------------------------------------
    # Face animation frames (thinking / waiting / working states)
    # ------------------------------------------------------------------

    _THINKING_FRAMES = [
        # ╭─────────────────────────╮
        # │  BOOT SEQUENCE          │
        # ╰─────────────────────────╯
        "(        )",
        "(  . .   )",  # pixels flickering on
        "(  ' '   )",
        "(  · ·   )",
        "(  ·_·   )",  # fully loaded ✓
        "(  ·_·   )",
        # --- Hmm, let me think... ---
        "(  ·_·  )",
        "( ·_·   )",  # eyes drift left
        "(·_·    )",
        "(<_·    )",  # squinting left
        "(¬_¬    )",  # suspicious squint
        "(¬_¬    )",
        "(¬_¬    )",
        # --- Idea sparks ---
        "(°_¬    )",
        "(°_°    )",  # wide-eyed realization
        "( °_°   )",
        "(  °_°  )",
        "(  °_°  )",
        # ╭─────────────────────────╮
        # │  FIRST THOUGHT          │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "(  ·u·  )",  # tiny smile
        "(  ·‿·  )",  # bigger smile — ooh a question!
        "(  ^‿^  )",
        "(  ^‿^  )",
        "(  ·_·  )",  # composing self
        # ╭─────────────────────────╮
        # │  LOOK AROUND            │
        # ╰─────────────────────────╯
        "( ·_·   )",
        "(·_·    )",
        "(O_·    )",  # big eye scan left
        "(O_·    )",
        "(o_·    )",
        "(¬_¬    )",  # hmmmm suspicious
        "(¬_¬    )",
        "(¬ _¬   )",
        "(  ¬_¬  )",  # panning back
        "(  ·_·  )",
        # --- Sleep / idle dip ---
        "(   ·_· )",
        "(    ·_·)",
        "(     ·_)",
        "(      ·)",
        "(       )",
        "(       )",
        "(       )",
        # --- Wake again from other side ---
        "(·      )",
        "(_·     )",
        "(·_·    )",
        "( ·_·   )",
        "(  ·_·  )",
        "(  ·_·  )",
        # --- Container morphs — feels alive ---
        "[  ·_·  ]",
        "[  ·_·  ]",
        ">  ·_·  <",
        ">  >_<  <",  # squished!
        ">  v_v  <",  # overwhelmed
        ">  >_<  <",
        ">  ·_·  <",
        "[  ·_·  ]",
        "[  ·_·  ]",
        "(  ·_·  )",
        # ╭─────────────────────────╮
        # │  DEEP THINK MODE        │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "(  -_·  )",  # one eye closes
        "(  -_-  )",  # both close — intense focus
        "(  -_-  )",
        "(  -_-  )…",
        "(  -_-  )…",
        "(  =_=  )",  # ultra concentrate
        "(  =_=  )",
        "(  -_-  )",
        "(  ·_-  )",  # one eye opens
        # ╭─────────────────────────╮
        # │  CONFUSION SPIRAL       │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "(  o_·  )",
        "(  o_o  )",
        "(  O_o  )",
        "(  O_O  )",  # wait what
        "(  O_O  )",
        "(  @_@  )",  # full spiral
        "(  @_@  )",
        "(  @_@  )",
        "( (@_@) )",  # extra swirly
        "( (@_@) )",
        "(  @_@  )",
        "(  O_O  )",
        "(  o_o  )",
        "(  ·_·  )",  # okay recovered
        # ╭─────────────────────────╮
        # │  FRUSTRATION            │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "(  ò_·  )",
        "(  ò_ó  )",  # grumpy
        "(  ò_ó  )",
        "( (ò_ó) )",  # VERY grumpy
        "(  >_<  )",  # arghhh
        "(  >_<  )",
        "(  ×_×  )",  # exploded
        "(  ×_×  )",
        "(  >_<  )",
        "(  ò_ó  )",
        "(  -_-  )",  # sigh
        "(  -_- )~",  # exhale
        "(  ·_·  )",  # reset
        # ╭─────────────────────────╮
        # │  EUREKA MOMENT          │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "(  ·_°  )",  # something clicks...
        "(  °_°  )",
        "(  ★_★  )",  # STARS in eyes
        "(  ★_★  )",
        "(  ★‿★  )",  # starry smile!!
        "(  ★‿★  )",
        "(  ^‿^  )",
        "(  ^‿^  )",
        "(  ^.^  )",
        # ╭─────────────────────────╮
        # │  HAPPY WIGGLE           │
        # ╰─────────────────────────╯
        "\\( ^.^ )/",  # arms up!
        "\\( ^.^ )/",
        "\\( ^‿^ )/",
        " \\(^‿^)/",
        "  (^‿^) ",
        " /(^‿^)\\",
        "/(^‿^) \\",  # wiggle wiggle
        "\\( ^‿^ )/",
        " \\(^.^)/",
        "  (^.^) ",
        # ╭─────────────────────────╮
        # │  LOOK RIGHT / STARE     │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "(   ·_· )",
        "(    ·_·)",
        "(    °_°)",  # spotted something
        "(    °_°)",
        "(    ·_>)",  # staring...
        "(    ·_>)",
        "(    ·_>)",
        "(    -_>)",  # shifty
        "(    ·_;)",  # wink right
        "(    ·_;)",
        "(    ·_>)",
        "(    ·_·)",
        "(   ·_· )",
        "(  ·_·  )",
        # ╭─────────────────────────╮
        # │  CONTAINER MORPHS       │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "[  ·_·  ]",  # box mode
        "[  ·_·  ]",
        "|  ·_·  |",  # tall mode
        "|  ·_·  |",
        ">  ·_·  <",  # being squished
        ">  >_<  <",  # squished face
        ">  ·_·  <",
        "[  ·_·  ]",
        "(  ·_·  )",
        "{  ·_·  }",  # curly mode
        "{  ^.^  }",
        "{  ·_·  }",
        "(  ·_·  )",
        # ╭─────────────────────────╮
        # │  SLEEPY DRIFT           │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "(  -_·  )",
        "(  -_-  )",
        "(  =_=  )",
        "(  =_= )z",
        "(  =_= )zz",
        "(  -_- )zzz",
        "(  u_u  )",  # fully asleep
        "(  u_u  )",
        "(  u_u  )zzz",
        "(  u_u  )zzz",
        # ╭─────────────────────────╮
        # │  WAKE UP STARTLED       │
        # ╰─────────────────────────╯
        "(  O_O  )",  # jolted awake!
        "(  O_O  )",
        "(  o_o  )",
        "(  ·_·  )",
        "(  ·u·  )",  # phew
        "(  ·_·  )",
        # ╭─────────────────────────╮
        # │  PROCESSING PULSE       │
        # ╰─────────────────────────╯
        "·(  ·_·  )",
        " (  ·_·  )·",
        "·(  ·_·  ) ",
        " (  ·_·  )·",
        "·(  ·_·  ) ",
        "(  ·_·  )··",
        "(  ·_· )···",
        "(  ·_·  )··",
        "(  ·_·  )·",
        "(  ·_·  )",
        # ╭─────────────────────────╮
        # │  WINK SEQUENCE          │
        # ╰─────────────────────────╯
        "(  ·_·  )",
        "(  ^_·  )",  # left wink
        "(  ^_·  )",
        "(  ·_^  )",  # right wink
        "(  ·_^  )",
        "(  ^_^  )",  # both raised brows
        "(  ^‿^  )",  # big grin
        "(  ^‿^  )",
        "(  ·_·  )",
        # ╭─────────────────────────╮
        # │  FINAL HAPPY FADE OUT   │
        # ╰─────────────────────────╯
        # --- Animated ears/antennae ---
        "( <·_·> )",
        "(  ·_·  )",
        "( <·_·> )",
        "(  ·_·  )",
        # --- Happy bounce ---
        "(  ^.^  )",
        "( ^.^   )",
        "( ^.^   )",
        "(  ^.^  )",
        "(   ^.^ )",
        "(   ^.^ )",
        "(  ^.^  )",
        "(  ^‿^  )",
        "(  ^‿^  )",
        "(  ^‿^  )",
        "(  ^.^  )",
        "(  ·_·  )",
        # --- Wind-down / go to sleep ---
        "( ·_·   )",
        "(·_·    )",
        "(_·     )",
        "(_·     )",
        "(·      )",
        "(·      )",
        "(       )",
    ]

    _WAITING_FRAMES = [
        # Bored waiting loop with nap attempt
        "(  ·_·  )",
        "(  ·_·  )",
        "(  ·u·  )",
        "(  ·_·  )",
        "(  -_·  )",  # getting sleepy
        "(  -_-  )",
        "(  =_= )z",
        "(  u_u  )zz",
        "(  u_u  )zzz",
        "(  O_O  )",  # oh! something happening?
        "(  o_o  )",
        "(  ·_·  )",  # nope. back to waiting.
        "(  ·_·  )",
        "(  ·u·  )",
        "(  ·_·  )",
    ]

    _WORKING_FRAMES = [
        # Focused little worker with pulse dots
        "[  ·_·  ]·",
        "[ ·_·   ]··",
        "[·_·    ]···",
        "[ ·_·   ]··",
        "[  ·_·  ]·",
        "[  ·_·  ]",
        "[  ·_·  ]·",
        "[   ·_· ]··",
        "[    ·_·]···",
        "[   ·_· ]··",
        "[  ·_·  ]·",
        "[  °_°  ]",  # eyes wide mid-task
        "[  °_°  ]·",
        "[  ·_·  ]",
        "[> ·_·  ]",  # leaning in
        "[> ·_·  ]·",
        "[  ·_·< ]",  # leaning right
        "[  ·_·< ]·",
        "[  ·_·  ]",
        "[  ^.^  ]",  # mini celebrate
        "[  ·_·  ]",
    ]

    def _spin(self) -> None:
        all_frames = self._THINKING_FRAMES + self._WAITING_FRAMES + self._WORKING_FRAMES
        max_frame_w = max(len(f) for f in all_frames)

        face_color = _c(AURORA["accent"])
        dim_color = _c(AURORA["dim"])
        reset_code = _c(ANSI.RESET)

        i = 0
        next_t = time.monotonic()

        while not self._spinner_stop.is_set():
            if self._spinner_pause.is_set():
                time.sleep(0.05)
                next_t = time.monotonic()
                continue

            state = self._spinner_state
            if state == SpinnerState.THINKING:
                frames, dt = self._THINKING_FRAMES, 0.09
            elif state == SpinnerState.WAITING:
                frames, dt = self._WAITING_FRAMES, 0.15
            else:
                frames, dt = self._WORKING_FRAMES, 0.12

            frame = frames[i % len(frames)].ljust(max_frame_w)
            line = (
                f"\r\x1b[2K{face_color}{frame}{reset_code} {dim_color}{self._spinner_label}…{reset_code}"
                if _use_color()
                else f"\r\x1b[2K{frame} {self._spinner_label}…"
            )

            with self._io_mutex:
                sys.stdout.write(line)
                sys.stdout.flush()

            i += 1
            next_t += dt
            sleep_for = next_t - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_t = time.monotonic()

        with self._io_mutex:
            _clear_current_line()

    # ------------------------------------------------------------------
    # Output & input
    # ------------------------------------------------------------------

    async def ui_output(self, text: str, *, kind: str = "buddy") -> None:
        if not text:
            return

        self.spinner_pause()
        async with self._out_lock:
            with self._io_mutex:
                _clear_current_line()

            pal = self.pal
            try:
                if kind == "meta":
                    with self._io_mutex:
                        print(
                            f"{_c(pal['meta'])}{text}{_c(ANSI.RESET)}",
                            flush=True,
                        )
                    return

                if kind == "user":
                    lines = [""] + _render_bubble_left(
                        text,
                        title=f"{PROMPTS['input']} You",
                        body_color=pal["user"],
                        border_color=pal["border"],
                    )
                    with self._io_mutex:
                        for ln in lines:
                            print(ln, flush=True)
                    return

                # kind == "buddy"
                if self.animated:
                    await _type_bubble_right_with_mutex(
                        text=text,
                        title=f"{PROMPTS['response']} Buddy",
                        body_color=pal["buddy"],
                        border_color=pal["border"],
                        max_ratio=0.66,
                        cps=90.0,
                        min_margin=2,
                        io_mutex=self._io_mutex,
                        animated=self.animated,
                    )
                else:
                    lines = _render_bubble_right(
                        text,
                        title=f"{PROMPTS['response']} Buddy",
                        body_color=pal["buddy"],
                        border_color=pal["border"],
                    )
                    with self._io_mutex:
                        for ln in lines:
                            print(ln, flush=True)

            finally:
                self.spinner_resume()

    async def read_typed(
        self, *, sys_state: SystemState, state_lock: threading.RLock
    ) -> Optional[str]:
        return await _read_typed_blocking(
            self.prompt_text, sys_state=sys_state, state_lock=state_lock
        )


# ==========================================================
# Runtime Actions
# ==========================================================


class RuntimeActions:

    def __init__(
        self,
        *,
        sys_state: SystemState,
        state_lock: threading.RLock,
        post_meta: Callable[[str], None],
        request_interrupt: Callable[[], None],
        interrupt_event: threading.Event,
        memory_manager: Optional[Any] = None,
        notify_activity: Optional[Callable[[], None]] = None,
    ) -> None:
        self.sys_state = sys_state
        self.state_lock = state_lock
        self._post_meta = post_meta
        self._stt: Optional[SpeechToText] = None
        self._request_interrupt = request_interrupt
        self._memory_manager = memory_manager  # MemoryManager | None
        self._notify_activity = notify_activity  # resets idle timer
        self._interrupt_event = interrupt_event

    def set_stt_engine(self, stt: SpeechToText) -> None:
        self._stt = stt

    def set_memory_manager(self, mm: Any) -> None:
        """Wire the MemoryManager after bootstrap completes."""
        self._memory_manager = mm

    def set_pipeline_running(self, running: bool) -> None:
        with self.state_lock:
            self.sys_state.pipeline_running = bool(running)
        _pt_invalidate()

    def set_voice_mute(self, muted: bool) -> None:
        """Mute or unmute the STT engine and update state."""
        muted = bool(muted)
        with self.state_lock:
            if self.sys_state.voice_muted == muted:
                return
            if self._stt:
                if muted:
                    self._stt.mute()
                else:
                    self._stt.unmute()
            self.sys_state.voice_muted = muted
        self._post_meta("🔇 muted" if muted else "🔊 unmuted")
        _pt_invalidate()

    def toggle_voice_mute(self) -> None:
        with self.state_lock:
            new_muted = not self.sys_state.voice_muted
            if self._stt:
                if new_muted:
                    self._stt.mute()
                else:
                    self._stt.unmute()
            self.sys_state.voice_muted = new_muted
        self._post_meta("🔇 muted" if new_muted else "🔊 unmuted")
        _pt_invalidate()

    def set_sleeping(self, sleeping: bool) -> None:
        """
        Put buddy to sleep or wake it up.

        Sleep  → start background memory consolidation (if MemoryManager present).
        Wake   → signal consolidation to stop (non-blocking) then mark buddy awake
                 immediately so the main loop can respond to the user without
                 waiting for the consolidation thread to finish.  The thread will
                 exit on its own during the next checkpoint (within one LLM call,
                 typically 2–10 s).
        """
        sleeping = bool(sleeping)
        mm = self._memory_manager

        if sleeping:
            with self.state_lock:
                self.sys_state.sleeping = True
            self._post_meta("😴 sleeping… consolidating memories")
            _pt_invalidate()

            # Start consolidation in background — non-blocking
            if mm is not None:
                started = mm.start_consolidation(on_done=self._on_consolidation_done)
                if started:
                    with self.state_lock:
                        self.sys_state.consolidating = True
                    _pt_invalidate()
        else:
            # Signal consolidation to stop — NON-BLOCKING (wait=False).
            # Calling join() / wait=True here would freeze the asyncio event
            # loop for up to one full LLM summary call (2-10 s).  Instead we
            # set the cancel_event and let the thread exit by itself; the
            # on_done callback will clear sys_state.consolidating when done.
            if mm is not None and mm.is_consolidating:
                mm.stop_consolidation(wait=False)

            with self.state_lock:
                self.sys_state.sleeping = False
                # consolidating stays True until on_done fires (thread still
                # winding down), then gets cleared there.
            self._post_meta("🌅 awake")
            _pt_invalidate()

            # Reset the idle timer so the inactivity watcher doesn't
            # immediately re-trigger sleep (e.g. after a voice wake).
            if self._notify_activity is not None:
                self._notify_activity()

    def _on_consolidation_done(self, report: Any) -> None:
        """Callback fired by the consolidation thread on completion OR crash."""
        with self.state_lock:
            self.sys_state.consolidating = False

        if report is None:
            # Engine crashed before returning a report — just clear the flag.
            self._post_meta("🧠 consolidation ended (crashed)")
            _pt_invalidate()
            return

        errors = getattr(report, "errors", []) or []
        was_cancelled = any("cancelled" in str(e) for e in errors)
        if not was_cancelled:
            summarized = getattr(report, "summarized", 0)
            tier_updates = getattr(report, "tier_updates", 0)
            self._post_meta(
                f"🧠 consolidation done: {summarized} summaries, {tier_updates} tier"
                " updates"
            )
        _pt_invalidate()

    def toggle_sleep(self) -> None:
        with self.state_lock:
            sleeping = not self.sys_state.sleeping
        self.set_sleeping(sleeping)

    def handle_voice_text(self, text: str, mux_push: Callable[[str], None]) -> None:
        t = (text or "").strip()
        if not t:
            return

        cmd = match_voice_command(t)
        now = time.monotonic()

        if cmd != VoiceCmd.NONE:
            with self.state_lock:
                if (now - self.sys_state.last_voice_cmd_ts) < 0.75:
                    return
                self.sys_state.last_voice_cmd_ts = now

        with self.state_lock:
            sleeping = self.sys_state.sleeping
            muted = self.sys_state.voice_muted
            running = self.sys_state.pipeline_running

        if running and cmd == VoiceCmd.STOP:
            self._interrupt_event.set()
            self._request_interrupt()
            return
        if running and cmd == VoiceCmd.NONE:
            return
        if sleeping:
            if cmd == VoiceCmd.WAKE:
                self.set_sleeping(False)
            return
        if cmd == VoiceCmd.SLEEP:
            self.set_sleeping(True)
            return
        if cmd == VoiceCmd.MUTE:
            self.set_voice_mute(True)
            return
        if cmd == VoiceCmd.UNMUTE:
            self.set_voice_mute(False)
            return
        if cmd == VoiceCmd.TOGGLE_MUTE:
            self.toggle_voice_mute()
            return
        if muted:
            return
        if _pt_submit_from_other_thread(t):
            return
        mux_push(t)


# ==========================================================
# Input Mux
# ==========================================================


class InputMux:
    """Multiplexes typed input and voice input into one async stream."""

    def __init__(self, *, max_buffer: int = 25) -> None:
        self._buffer: Deque[str] = deque(maxlen=max_buffer)
        self._pending: Optional[asyncio.Future[str]] = None
        self._need_typed = asyncio.Event()

    def need_typed_event(self) -> asyncio.Event:
        return self._need_typed

    def push(self, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        fut = self._pending
        if fut is not None and not fut.done():
            self._pending = None
            self._need_typed.clear()
            fut.set_result(t)
            return
        self._buffer.append(t)

    async def ui_input(self) -> str:
        if self._buffer:
            return self._buffer.popleft()
        if self._pending is not None and not self._pending.done():
            result = await self._pending
            self._pending = None
            return result
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending = fut
        self._need_typed.set()
        return await fut


# ==========================================================
# Runtime
# ==========================================================


async def run_terminal() -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()

    # ── Signal handlers ──────────────────────────────────────────────
    if hasattr(signal, "SIGWINCH"):

        def _handle_resize(signum, frame) -> None:
            _invalidate_term_cache()
            _pt_invalidate()

        signal.signal(signal.SIGWINCH, _handle_resize)

    # ── Bootstrap ────────────────────────────────────────────────────
    state = bootstrap()

    cfg = _get_cfg(state)
    buddy_cfg = _get_nested(cfg, "buddy")
    runtime = _get_nested(cfg, "runtime")
    general_cfg = _get_nested(buddy_cfg, "general")

    cli_cfg = _get_nested(buddy_cfg, "cli")
    voice_cfg = _get_nested(buddy_cfg, "voice")
    whisper_dir = os.path.join(runtime["fs"]["models_dir"], "whisper")

    animated = bool(cli_cfg.get("stream", True))
    prompt = str(cli_cfg.get("prompt", PROMPTS["input"]))
    theme = str(cli_cfg.get("theme", "dark"))

    # ── Core objects ─────────────────────────────────────────────────
    ui = TerminalUI(prompt_text=prompt, theme=theme, animated=animated)
    mux = InputMux(max_buffer=25)

    quit_event = asyncio.Event()
    interrupt_event = threading.Event()

    sys_state = SystemState()
    state_lock = threading.RLock()

    active_turn_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
    stt: Optional[SpeechToText] = None
    turn_lock = asyncio.Lock()
    last_interrupt_ts = 0.0
    last_sigint_ts = 0.0
    SIGINT_EXIT_WINDOW_S = 1.25

    # ── Inactivity / auto-sleep config ───────────────────────────────
    # Read from config; default 20 minutes after last pipeline turn.
    # Set to 0 to disable.
    _idle_timeout_sec: float = float(general_cfg.get("sleep_after_idle_sec", 5.0) * 60)
    _last_activity_ts: float = time.monotonic()  # reset after every pipeline turn

    # ── Helpers ───────────────────────────────────────────────────────

    _META_CLEAR_DELAY = 6.0  # seconds before status msg is cleared back to help text

    def _post_meta(msg: str) -> None:
        _set_banner_meta(msg)
        _pt_invalidate()
        # Schedule auto-clear so help text returns after the status fades
        loop = _MAIN_LOOP
        if loop is not None:
            async def _clear_meta_after(text: str) -> None:
                await asyncio.sleep(_META_CLEAR_DELAY)
                with _BANNER_META_LOCK:
                    still_showing = (_BANNER_META == text)
                if still_showing:
                    _set_banner_meta("")
                    _pt_invalidate()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(_clear_meta_after(msg))
            )

    def request_interrupt() -> None:
        nonlocal active_turn_task, last_interrupt_ts, interrupt_event
        now = time.monotonic()
        if (now - last_interrupt_ts) < 0.75:
            return
        last_interrupt_ts = now

        logger.info("interrupt: requested")
        interrupt_event.set()

        # Try to abort a visible PT prompt first.  When the pipeline is
        # mid-turn there is no active PT session (_PT_ACTIVE_APP is None) so
        # _pt_submit_from_other_thread returns False.  In that case push the
        # sentinel directly into the mux so pipeline_input() unblocks.
        # Both paths must be tried — the PT path handles the "waiting for
        # typed input" state, the mux path handles the "LLM is streaming"
        # state where pipeline_input() is blocked on mux.ui_input().
        submitted_to_pt = _pt_submit_from_other_thread(INTERRUPT_SENTINEL)
        if not submitted_to_pt:
            loop = _MAIN_LOOP
            if loop is not None:
                loop.call_soon_threadsafe(mux.push, INTERRUPT_SENTINEL)

        t = active_turn_task
        if t and not t.done():
            t.cancel()

        ui.spinner_stop()
        _post_meta("⛔ interrupted")

    # ── SIGINT: first press interrupts turn, second press quits ──────
    #
    # IMPORTANT: We save the original handler (Python's default or whatever
    # was installed before us) and call it for non-interactive signals.
    # On macOS, PortAudio/CoreAudio uses SIGINT-adjacent kernel paths during
    # device enumeration.  Replacing the handler without chaining causes
    # "PaMacCore (AUHAL) Unspecified Audio Hardware Error" because the audio
    # subsystem's expected signal delivery is disrupted.
    # Saving + restoring the original handler keeps that path intact.
    _original_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(signum, frame) -> None:
        nonlocal last_sigint_ts, interrupt_event
        now = time.monotonic()
        loop = _MAIN_LOOP

        if (now - last_sigint_ts) < SIGINT_EXIT_WINDOW_S:
            # Second Ctrl+C within the window → quit
            logger.info("sigint: quit requested (double-tap)")
            if loop:
                loop.call_soon_threadsafe(quit_event.set)
                loop.call_soon_threadsafe(mux.push, EXIT_SENTINEL)
            return

        last_sigint_ts = now
        # Set interrupt_event immediately (signal-handler context, thread-safe).
        # This stops the LLM stream right away without waiting for the event loop.
        # request_interrupt() below also sets it — the redundancy is intentional:
        # the direct set here is synchronous; request_interrupt's set is deferred.
        interrupt_event.set()
        logger.info("sigint: interrupt requested (single tap)")
        if loop:
            loop.call_soon_threadsafe(request_interrupt)
        else:
            # No loop yet — fall back to default behaviour so the process
            # is still interruptible during early startup.
            if callable(_original_sigint):
                _original_sigint(signum, frame)

    # NOTE: signal.signal(SIGINT, _handle_sigint) is registered AFTER stt.start()
    # below — PortAudio/CoreAudio must initialise under the original handler to
    # avoid "PaMacCore (AUHAL) Unspecified Audio Hardware Error" on macOS.

    # ── Typed-input producer ─────────────────────────────────────────

    async def typed_producer() -> None:
        try:
            while not quit_event.is_set():
                await mux.need_typed_event().wait()
                if quit_event.is_set():
                    break
                s = await ui.read_typed(sys_state=sys_state, state_lock=state_lock)
                mux.push(EXIT_SENTINEL if s is None else s)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.exception("typed_producer crashed: %r", ex)
            mux.push(EXIT_SENTINEL)

    typed_task = asyncio.create_task(typed_producer())

    # ── Actions & hotkeys ────────────────────────────────────────────

    def _notify_activity() -> None:
        """Reset the idle timer.  Safe to call from any context."""
        nonlocal _last_activity_ts
        _last_activity_ts = time.monotonic()

    actions = RuntimeActions(
        sys_state=sys_state,
        state_lock=state_lock,
        post_meta=_post_meta,
        request_interrupt=request_interrupt,
        memory_manager=getattr(
            getattr(state, "artifacts", None), "memory_manager", None
        ),
        notify_activity=_notify_activity,
        interrupt_event=interrupt_event,
    )

    def _wire_hotkeys() -> None:
        loop = _MAIN_LOOP
        if loop is None:
            return
        _RUNTIME_HOTKEYS["toggle_mute"] = lambda: loop.call_soon_threadsafe(
            actions.toggle_voice_mute
        )
        _RUNTIME_HOTKEYS["toggle_sleep"] = lambda: loop.call_soon_threadsafe(
            actions.toggle_sleep
        )
        _RUNTIME_HOTKEYS["mute"] = lambda: loop.call_soon_threadsafe(
            actions.set_voice_mute, True
        )
        _RUNTIME_HOTKEYS["unmute"] = lambda: loop.call_soon_threadsafe(
            actions.set_voice_mute, False
        )
        _RUNTIME_HOTKEYS["sleep"] = lambda: loop.call_soon_threadsafe(
            actions.set_sleeping, True
        )
        _RUNTIME_HOTKEYS["wake"] = lambda: loop.call_soon_threadsafe(
            actions.set_sleeping, False
        )

    _wire_hotkeys()

    # ── Inactivity watcher ───────────────────────────────────────────
    # Polls every 30 s.  When the user has been idle for _idle_timeout_min
    # and buddy is not already sleeping or processing a turn, it triggers
    # set_sleeping(True) which starts background consolidation automatically.

    async def _inactivity_watcher() -> None:
        nonlocal actions
        try:
            while not quit_event.is_set():
                if _idle_timeout_sec <= 0:
                    # Auto-sleep disabled — check again in 60s in case config changes
                    await asyncio.sleep(60.0)
                    continue

                now = time.monotonic()
                elapsed = now - _last_activity_ts
                remaining = _idle_timeout_sec - elapsed

                if remaining <= 0:
                    with state_lock:
                        already_sleeping = sys_state.sleeping
                        running = sys_state.pipeline_running
                    if not already_sleeping and not running:
                        logger.info(
                            "inactivity_watcher: idle %.0fs >= %.0fs — sleeping",
                            elapsed,
                            _idle_timeout_sec,
                        )
                        actions.set_sleeping(True)
                    # After sleeping (or if already sleeping/running), wait a full
                    # cycle before checking again so we don't busy-loop.
                    await asyncio.sleep(_idle_timeout_sec)
                else:
                    # Sleep exactly until the timeout would fire.
                    # Cap at 60s so activity resets (_last_activity_ts) are noticed
                    # promptly without waiting a full timeout cycle.
                    await asyncio.sleep(min(remaining, 60.0))

        except asyncio.CancelledError:
            pass
        except Exception as ex:
            logger.exception("inactivity_watcher crashed: %r", ex)

    inactivity_task = asyncio.create_task(_inactivity_watcher())

    # ── STT ──────────────────────────────────────────────────────────

    def on_stt_text(text: str) -> None:
        nonlocal actions
        loop = _MAIN_LOOP
        if loop:
            loop.call_soon_threadsafe(actions.handle_voice_text, text, mux.push)

    try:
        if bool(voice_cfg.get("enabled", True)):
            mic_idx = voice_cfg.get("microphone_index", -1)
            stt = SpeechToText(
                whisper_model_size=str(voice_cfg.get("whisper_model_size", "base")),
                whisper_download_root=str(whisper_dir),
                calibration_sec=voice_cfg.get("calibration_sec", 0.0),
                language=str(voice_cfg.get("language", "en")),
                microphone_index=mic_idx if mic_idx >= 0 else None,
                silence_timeout=float(voice_cfg.get("silence_timeout", 1.4)),
                on_text=on_stt_text,
                beam_size=int(voice_cfg.get("beam_size", 5)),
                whisper_vad_filter=bool(voice_cfg.get("whisper_vad_filter", True)),
                speech_trigger_mult=float(voice_cfg.get("speech_trigger_mult", 3.0)),
                use_silero_vad=bool(voice_cfg.get("use_silero_vad", False)),
                enable_beep=bool(voice_cfg.get("enable_beep", True)),
                debug=bool(voice_cfg.get("debug", False)),
            )
            stt.start()
            actions.set_stt_engine(stt=stt)
            _post_meta("🎧 voice enabled")
        else:
            _post_meta("🎧 voice disabled")
    except Exception as ex:
        logger.exception("stt: failed to start: %r", ex)
        _post_meta(f"\u26a0\ufe0f stt failed: {ex}")
        stt = None

    # Register SIGINT handler NOW - after PortAudio/CoreAudio has fully
    # initialised inside stt.start(). On macOS, installing the handler
    # before audio device enumeration disrupts CoreAudio's internal kernel
    # notification path and causes:
    #   "PaMacCore (AUHAL) Unspecified Audio Hardware Error"
    # The _handle_sigint function was defined above; only the registration
    # is deferred to this point so audio init runs under the original handler.
    signal.signal(signal.SIGINT, _handle_sigint)

    # ── Turn executor ────────────────────────────────────────────────

    async def ui_input() -> str:
        ui.spinner_stop()
        return await mux.ui_input()

    async def run_one_turn(source: str, user_text: str) -> None:
        nonlocal active_turn_task, interrupt_event, actions
        async with turn_lock:
            interrupt_event.clear()  # cleared inside lock — no race with previous turn
            turn_id = f"turn-{uuid.uuid4().hex[:8]}"
            t0 = time.perf_counter()
            logger.info(
                "turn.start: id=%s source=%s chars=%d",
                turn_id,
                source,
                len(user_text or ""),
            )

            current_label = "Thinking"
            stream_buf: List[str] = []
            _PREVIEW_MIN_S = 0.04
            _PREVIEW_CHARS = 80
            _last_preview_t = 0.0
            _thinking_done = False

            ui.spinner_start(current_label, SpinnerState.THINKING)
            actions.set_pipeline_running(True)

            def progress_spinner(text: str, stream: bool = True) -> None:
                nonlocal current_label, stream_buf, _last_preview_t, _thinking_done
                now = time.perf_counter()
                if not stream:
                    _thinking_done = False
                    stream_buf.clear()
                    current_label = text.strip() or current_label
                    ui.spinner_update(current_label, SpinnerState.WORKING)
                    return
                if not _thinking_done:
                    stream_buf.append(text)
                    if now - _last_preview_t >= _PREVIEW_MIN_S:
                        _last_preview_t = now
                        preview = (
                            "".join(stream_buf)
                            .replace("\r", " ")
                            .replace("\n", " ")
                            .strip()[-_PREVIEW_CHARS:]
                        )
                        ui.spinner_update(
                            preview or current_label, SpinnerState.THINKING
                        )
                        if "</THINK>" in preview:
                            _thinking_done = True
                            stream_buf.clear()
                            ui.spinner_update("Thinking", SpinnerState.THINKING)

            async def pipeline_input() -> str:
                nonlocal actions
                _notify_activity()
                actions.set_pipeline_running(False)
                ans = await ui_input()
                actions.set_pipeline_running(True)
                # Interrupt arrived while pipeline was waiting for user input —
                # abort this turn cleanly instead of passing the sentinel string
                # to the planner as a followup answer.
                if ans == INTERRUPT_SENTINEL or _is_interrupt_cmd(ans):
                    raise asyncio.CancelledError("interrupted")
                if ans and not _should_exit(ans):
                    await ui.ui_output(ans, kind="user")
                if active_turn_task is not None and not active_turn_task.done():
                    ui.spinner_start(current_label, SpinnerState.WAITING)
                return ans

            async def pipeline_output(text: str) -> None:
                ui.spinner_stop()
                await ui.ui_output(text, kind="buddy")
                if active_turn_task is not None and not active_turn_task.done():
                    ui.spinner_start(current_label, SpinnerState.THINKING)

            active_turn_task = asyncio.create_task(
                handle_turn(
                    state=state,
                    source=source,
                    user_message=user_text,
                    ui_output=pipeline_output,
                    ui_input=pipeline_input,
                    progress_cb=progress_spinner,
                    interrupt_event=interrupt_event,
                )
            )

            try:
                _notify_activity()
                await active_turn_task
                logger.info(
                    "turn.done: id=%s dt=%.3fs", turn_id, time.perf_counter() - t0
                )
            except asyncio.CancelledError:
                logger.info(
                    "turn.cancelled: id=%s dt=%.3fs", turn_id, time.perf_counter() - t0
                )
            except Exception as ex:
                ui.spinner_stop()
                logger.exception(
                    "turn.crash: id=%s dt=%.3fs err=%r",
                    turn_id,
                    time.perf_counter() - t0,
                    ex,
                )
                _post_meta(f"⚠️ error: {ex}")
            finally:
                active_turn_task = None
                ui.spinner_stop()
                actions.set_pipeline_running(False)
                # Reset idle timer from end of pipeline turn so the 20-min
                # consolidation window starts after Buddy's last response.
                _notify_activity()

    # ── Main loop ─────────────────────────────────────────────────────

    try:
        while not quit_event.is_set():
            s = (await ui_input()).strip()
            if not s:
                continue
            if s == EXIT_SENTINEL or _should_exit(s):
                _post_meta("Bye 👋")
                return
            if _is_interrupt_cmd(s):
                request_interrupt()
                continue

            # ── Track activity for inactivity-based auto-sleep ─────────────
            _last_activity_ts = time.monotonic()

            # ── Wake buddy if sleeping (cancels consolidation then responds) ─
            with state_lock:
                is_sleeping = sys_state.sleeping
            if is_sleeping:
                actions.set_sleeping(False)

            await ui.ui_output(s, kind="user")
            await run_one_turn("mixed", s)

    finally:
        logger.info("shutdown: starting")
        try:
            typed_task.cancel()
            await typed_task
        except (asyncio.CancelledError, Exception) as ex:
            if not isinstance(ex, asyncio.CancelledError):
                logger.exception("shutdown: typed_task error: %r", ex)

        try:
            inactivity_task.cancel()
            await inactivity_task
        except (asyncio.CancelledError, Exception):
            pass

        ui.spinner_stop()

        if stt is not None:
            try:
                stt.stop()
            except Exception as ex:
                logger.exception("shutdown: stt.stop failed: %r", ex)

        if state:
            shutdown_fn = getattr(state, "shutdown", None)
            if callable(shutdown_fn):
                try:
                    shutdown_fn()
                except Exception as ex:
                    logger.exception("shutdown: state.shutdown failed: %r", ex)

        logger.info("shutdown: done")


def main() -> int:
    try:
        asyncio.run(run_terminal())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as ex:
        logger.exception("Main crashed: %r", ex)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
