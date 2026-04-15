# buddy/ui/textual_app.py
#
# Textual TUI for Buddy — v3
#
# Screens:
#   BuddyApp → BootScreen (bootstrap) → MainScreen (chat)
#
# Critical fixes (v3):
#   - Blocking LLM calls run via asyncio.to_thread() in pipeline.py/action_router.py
#   - progress_cb uses loop.call_soon_threadsafe() → no frozen UI
#   - BootScreen shows live boot messages under banner
#   - ContentSwitcher for Chat ↔ Sleep (no manual show/hide)
#   - SleepView: two independent timers (0.15s stars, 0.4s face) + progress bars
#   - StatusBar shows LLM · RAM · voice · turn count near header
#   - ASCII fallback for non-unicode terminals
#
# run_textual(state) is the public entry point called from main.py.

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import random
import threading
import time
import uuid
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple

from rich.align import Align
from rich.panel import Panel
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Input, Static, ContentSwitcher, Label

from buddy.buddy_core.pipeline import handle_turn
from buddy.logger.logger import get_logger
from buddy.tools.vision.image_encoder import extract_image_paths
from buddy.ui.boot_ui import (
    _supports_unicode,
    AURORA,
    _buddy_title_lines,  # canonical 6-row logo text (unicode + ASCII fallback)
    _logo_row_code,  # per-row 256-color ANSI code  →  we map to Rich hex
)

logger = get_logger("textual_app")

# ──────────────────────────────────────────────────────────────────────────────
# Sentinels
# ──────────────────────────────────────────────────────────────────────────────

EXIT_SENTINEL = "__EXIT__"
INTERRUPT_SENTINEL = "__INTERRUPT__"

# ──────────────────────────────────────────────────────────────────────────────
# Unicode capability (tested once at import time)
#
# boot_ui._supports_unicode() checks sys.stdout.encoding, which Textual
# replaces with its own internal buffer — making it always return False
# inside a running Textual app. We instead check the locale/env so the
# detection works correctly regardless of stdout redirection.
# ──────────────────────────────────────────────────────────────────────────────

import locale as _locale


def _textual_supports_unicode() -> bool:
    # 1. Explicit NO_COLOR / dumb terminal opt-out
    if os.getenv("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    # 2. Check locale encoding (reliable even when stdout is redirected)
    try:
        if "utf" in (_locale.getpreferredencoding(False) or "").lower():
            return True
    except Exception:
        pass
    # 3. Check env vars (LANG / LC_ALL / LC_CTYPE)
    lang_env = (
        os.environ.get("LANG", "")
        + os.environ.get("LC_ALL", "")
        + os.environ.get("LC_CTYPE", "")
    ).lower()
    if "utf" in lang_env:
        return True
    # 4. Fallback to the original stdout check
    return _supports_unicode()


_USE_UNICODE: bool = _textual_supports_unicode()

# ──────────────────────────────────────────────────────────────────────────────
# Colors
# ──────────────────────────────────────────────────────────────────────────────

_CYAN = "#00e5ff"
_BLUE = "#5fafff"
_VIOLET = "#875fff"
_DIM = "#606080"
_BG = "#080818"
_BG2 = "#050510"
_WHITE = "#e0e8ff"
_GREEN = "#00ff88"
_YELLOW = "#ffcc00"
_RED = "#ff5555"

# ──────────────────────────────────────────────────────────────────────────────
# BUDDY banner markup — built from boot_ui.py as the single source of truth
#
# boot_ui._buddy_title_lines() → canonical logo rows (unicode or ASCII fallback)
# boot_ui._logo_row_code(i)    → 256-color ANSI code for row i
#
# ANSI codes can't be used inside Textual/Rich markup, so we map each row's
# ANSI code to the closest Rich hex color using the same 6-step aurora arc
# defined in boot_ui.AURORA.
# ──────────────────────────────────────────────────────────────────────────────

# Aurora gradient hex values — match boot_ui AURORA logo_r0..r5 exactly
_LOGO_ROW_HEX: List[str] = [
    "#00ffff",  # logo_r0  \033[38;5;51m   bright cyan (aurora peak)
    "#00d7ff",  # logo_r1  \033[38;5;45m   light cyan
    "#5fafff",  # logo_r2  \033[38;5;75m   cyan-blue
    "#8787ff",  # logo_r3  \033[38;5;105m  blue-indigo
    "#af5fff",  # logo_r4  \033[38;5;135m  violet
    "#875fff",  # logo_r5  \033[38;5;99m   deep violet (aurora base)
]


def _logo_markup() -> str:
    """
    Build Rich-markup logo from boot_ui._buddy_title_lines().
    Colors mirror the 6-step aurora gradient in boot_ui._logo_row_code().
    """
    rows = _buddy_title_lines()  # unicode block-art OR ASCII fallback
    if _USE_UNICODE:
        return "\n".join(
            f"[bold {_LOGO_ROW_HEX[i % len(_LOGO_ROW_HEX)]}]{row}[/]"
            for i, row in enumerate(rows)
        )
    # ASCII fallback — basic cyan (matches boot_ui's logo_r0_basic / logo_r2_basic)
    return "\n".join(f"[bold cyan]{row}[/]" for row in rows)


def _banner_markup(*, compact: bool = False) -> str:
    logo = _logo_markup()
    tagline = f"[dim {_VIOLET}]Cognitive AI  ·  Offline-first  ·  Memory-driven[/]"
    if compact:
        return logo + "\n" + tagline
    hint = f"[dim {_DIM}]type or speak  ·  ESC to interrupt  ·  F2 mute  ·  F3 sleep[/]"
    return logo + "\n" + tagline + "\n" + hint


# ──────────────────────────────────────────────────────────────────────────────
# Face animation frames
# ──────────────────────────────────────────────────────────────────────────────

_THINKING_FRAMES: List[str] = [
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

_WAITING_FRAMES: List[str] = [
    "(  ·_·  )",
    "(  ·_·  )",
    "(  ·u·  )",
    "(  ·_·  )",
    "(  -_·  )",
    "(  -_-  )",
    "(  =_= )z",
    "(  u_u  )zz",
    "(  u_u  )zzz",
    "(  O_O  )",
    "(  o_o  )",
    "(  ·_·  )",
    "(  ·_·  )",
    "(  ·u·  )",
    "(  ·_·  )",
]

_WORKING_FRAMES: List[str] = [
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
    "[  °_°  ]",
    "[  °_°  ]·",
    "[  ·_·  ]",
    "[> ·_·  ]",
    "[> ·_·  ]·",
    "[  ·_·< ]",
    "[  ·_·< ]·",
    "[  ·_·  ]",
    "[  ^.^  ]",
    "[  ·_·  ]",
]

_BOOT_FACE_FRAMES: List[str] = [
    # ── void / no signal ──
    "(        )",
    "(   .    )",
    "(  ._.   )",
    "(  ._.   )",
    # ── electrical noise / glitch ──
    "(  .-._  )",
    "(  _.-.  )",
    "(  ·_.   )",
    "(   ._·  )",
    "(  ·_·   )",
    # ── unstable wake ──
    "(  -_-   )",
    "(  -_·   )",
    "(  ·_-   )",
    "(  -_-   )",
    # ── sensor sweep (left ↔ right illusion) ──
    "(  o_·   )",
    "(  ·_o   )",
    "(  o_·   )",
    "(  ·_o   )",
    "(  o_o   )",
    # ── sudden awareness spike ──
    "(  °_°   )",
    "(  O_O   )",
    "(  O_O   )",
    # ── overload / confusion ──
    "(  x_x   )",
    "(  x_x   )",
    "(  °_°   )",
    "(  -_-   )",
    # ── self-check / calibration ──
    "(  ·_·   )",
    "(  ·-·   )",
    "(  ·_·   )",
    "(  =_=   )",
    "(  =_=   )",
    # ── curiosity kicks in ──
    "(  ·o·   )",
    "(  o_o   )",
    "(  ·o·   )",
    # ── personality forming ──
    "(  ^_^   )",
    "(  ^.^   )",
    "(  ^o^   )",
    "(  ^‿^   )",
    "(  ^‿^   )",
    # ── playful glitch (like “thinking”) ──
    "(  ^‿^  )~",
    "( ~^‿^  )",
    "(  ^‿^~ )",
    "(  ^‿^  )",
    # ── micro-expression loop ──
    "(  ·u·   )",
    "(  ^‿^   )",
    "(  ·_·   )",
    "(  ^‿^   )",
    # ── spatial shift (feels like movement) ──
    "( ·_·    )",
    "(   ·_·  )",
    "(    ·_· )",
    "(   ·_·  )",
    "( ·_·    )",
    # ── final stabilization ──
    "(  -_-   )",
    "(  ·_·   )",
    "(  ·_·   )",
    # ── READY (confident, alive) ──
    "(  ^‿^   )",
    "(  ^‿^   )",
]
_SLEEP_FACE_FRAMES: List[str] = [
    "(  ·_·  )",
    "(  -_·  )",
    "(  -_-  )",
    "(  =_=  )",
    "(  =_= )z",
    "(  =_= )zz",
    "(  =_= )zzz",
    "(  u_u  )",
    "(  u_u  )z",
    "(  u_u  )zz",
    "(  u_u  )zzz",
    "(  -_- )zzz",
    "(  -_- )zzz",
    "(  -_- )zz",
    "(  =_= )z",
    "(  u_u  )",
    "(  ·_·  )",
]
# ──────────────────────────────────────────────────────────────────────────────
# SystemState, VoiceCmd, helpers
# ──────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass


@dataclass
class SystemState:
    sleeping: bool = False
    consolidating: bool = False
    voice_muted: bool = False
    pipeline_running: bool = False
    last_voice_cmd_ts: float = 0.0


class VoiceCmd(Enum):
    NONE = "none"
    STOP = "stop"
    SLEEP = "sleep"
    WAKE = "wake"
    MUTE = "mute"
    UNMUTE = "unmute"
    TOGGLE_MUTE = "toggle_mute"


def _match_voice_command(text: str) -> VoiceCmd:
    t = text.strip().lower()
    if not t:
        return VoiceCmd.NONE
    if t in {"stop", "buddy stop", "cancel", "interrupt"}:
        return VoiceCmd.STOP
    if t in {"sleep", "buddy sleep", "go to sleep"}:
        return VoiceCmd.SLEEP
    if t in {"wake", "wake up", "buddy wake", "buddy wake up"}:
        return VoiceCmd.WAKE
    if t in {"mute", "mute voice", "buddy mute"}:
        return VoiceCmd.MUTE
    if t in {"unmute", "unmute voice", "buddy unmute"}:
        return VoiceCmd.UNMUTE
    if t in {"toggle mute", "toggle voice"}:
        return VoiceCmd.TOGGLE_MUTE
    return VoiceCmd.NONE


def _should_exit(text: str) -> bool:
    return (text or "").strip().lower() in {"exit", "quit", "q", ":q"}


# ──────────────────────────────────────────────────────────────────────────────
# InputQueue — collision-safe asyncio.Queue
# ──────────────────────────────────────────────────────────────────────────────


class InputQueue:
    """
    Thread-safe input queue that prevents STT/typed collision.

    When typed input is submitted a 1.5 s source-lock is set.
    Any voice text arriving within that window is silently dropped.
    """

    SOURCE_LOCK_S = 1.5

    def __init__(self) -> None:
        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self._source_lock_until = 0.0

    async def push_typed(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._source_lock_until = time.monotonic() + self.SOURCE_LOCK_S
        await self._q.put(text)

    def push_voice(self, text: str, loop: asyncio.AbstractEventLoop) -> None:
        text = (text or "").strip()
        if not text:
            return
        if time.monotonic() < self._source_lock_until:
            logger.debug("InputQueue: voice suppressed (source lock active)")
            return
        loop.call_soon_threadsafe(self._q.put_nowait, text)

    async def get(self) -> str:
        return await self._q.get()

    def push_sentinel(self, sentinel: str, loop: asyncio.AbstractEventLoop) -> None:
        loop.call_soon_threadsafe(self._q.put_nowait, sentinel)


# ──────────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────────
# SplashScreen — full-screen logo + face animation, shown before boot log
# ──────────────────────────────────────────────────────────────────────────────

_SPLASH_FACE_FRAMES: List[str] = [
    "(        )",
    "(  ·  ·  )",
    "(  · ·   )",
    "(  ·_·   )",
    "(  ·_·   )",
    "(  ·u·   )",
    "(  ^‿^   )",
    "(  ^‿^   )",
    "(  ★‿★   )",
    "(  ★‿★   )",
    "(  ^.^   )",
    "\\( ^.^ )/",
    "\\( ^‿^ )/",
    " \\(^‿^)/",
    "  (^‿^)  ",
    " \\(^‿^)/",
    "\\( ^‿^ )/",
    "\\( ^.^ )/",
    "(  ^.^   )",
    "(  ·‿·   )",
    "(  ·_·   )",
    "(  ·_·   )",
]


class SplashView(Static):
    """
    Full-screen splash: BUDDY logo centred vertically + animated face below.
    Shown for ~2.5 s before transitioning to BootScreen.

    Uses Static + update() so content-align and text-align work correctly.
    (Widget.render() strings bypass Textual's alignment CSS.)
    """

    DEFAULT_CSS = f"""
    SplashView {{
        height: 1fr;
        background: {_BG};
        content-align: center middle;
        text-align: center;
    }}
    """

    _frame: int = 0

    def on_mount(self) -> None:
        self._face_timer = self.set_interval(0.12, self._tick)
        self._redraw()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_SPLASH_FACE_FRAMES)
        self._redraw()

    def _redraw(self) -> None:
        logo = _logo_markup()
        tagline = f"[dim {_VIOLET}]Cognitive AI  ·  Offline-first  ·  Memory-driven[/]"
        face = _SPLASH_FACE_FRAMES[self._frame]
        face_line = f"[{_CYAN}]{face}[/]"
        hint = f"[dim {_DIM}]starting up…[/]"
        self.update("\n".join([logo, "", tagline, "", face_line, "", hint]))


class SplashScreen(Screen):
    """Shown for ~2.5 s before BootScreen."""

    DEFAULT_CSS = f"""
    SplashScreen {{
        background: {_BG};
    }}
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._switching = False

    def compose(self) -> ComposeResult:
        yield SplashView()

    def on_mount(self) -> None:
        # After 2.5 s transition to BootScreen; any key skips the wait
        self.set_timer(2.5, self._go_boot)

    def _go_boot(self) -> None:
        if not self._switching:
            self._switching = True
            asyncio.create_task(self._switch())

    async def _switch(self) -> None:
        await self.app.switch_screen(BootScreen())

    def on_key(self, _: Any) -> None:
        """Any key skips the splash."""
        if not self._switching:
            self._switching = True
            asyncio.create_task(self._switch())


# BootScreen widgets
# ──────────────────────────────────────────────────────────────────────────────


class BootBanner(Static):
    """Full AURORA banner shown during bootstrap."""

    DEFAULT_CSS = f"""
    BootBanner {{
        height: auto;
        background: {_BG};
        padding: 1 2;
        border-bottom: heavy {_CYAN};
        color: {_CYAN};
        text-align: center;
    }}
    """

    def on_mount(self) -> None:
        self.update(_banner_markup(compact=False))


class BootLogLine(Static):
    """
    A single boot step line.

    While status is "running" it cycles through a spinner animation.
    Call set_result(msg, status) to lock the icon to ✓ / ✗ / !
    """

    # _SPIN = ["◜", "◠", "◝", "◞", "◡", "◟"] if _USE_UNICODE else ["|", "/", "-", "\\"]
    _SPIN = (
        ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        if _USE_UNICODE
        else ["|", "/", "-", "\\"]
    )

    _ICONS = (
        {"ok": "✓", "warn": "!", "fail": "✗"}
        if _USE_UNICODE
        else {"ok": "+", "warn": "!", "fail": "X"}
    )
    _COLORS = {"ok": _GREEN, "warn": _YELLOW, "fail": _RED}

    DEFAULT_CSS = f"""
    BootLogLine {{
        height: 1;
        background: {_BG};
        padding: 0 2;
        color: {_WHITE};
    }}
    """

    def __init__(self, msg: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._msg = msg
        self._done = False
        self._frame = 0
        self._timer: Any = None

    def on_mount(self) -> None:
        self._render_boot_log_line()
        self._timer = self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if not self._done:
            self._frame = (self._frame + 1) % len(self._SPIN)
            self._render_boot_log_line()

    def _render_boot_log_line(self) -> None:
        if self._done:
            return
        try:
            spin = self._SPIN[self._frame]
            self.update(f"[{_CYAN}]{spin}[/]  [{_DIM}]{self._msg}[/]")
        except Exception:
            logger.exception("BootLogLine._render_boot_log_line failed")

    def set_result(self, msg: str, status: str) -> None:
        """Replace spinner with final icon. Safe to call from event loop."""
        self._done = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        icon = self._ICONS.get(status, "·")
        color = self._COLORS.get(status, _DIM)
        text_color = _WHITE if status in ("ok", "warn", "fail") else _DIM
        self.update(f"[{color}]{icon}[/]  [{text_color}]{msg}[/]")


class BootLog(ScrollableContainer):
    """
    Scrollable log of bootstrap steps.

    Protocol (mirrors boot.py's _pcb / _ui_ok/_warn/_fail pattern):
      add_message(msg, "running") → appends a new spinning BootLogLine
      add_message(msg, "ok"|"warn"|"fail") → resolves the last pending line
    """

    DEFAULT_CSS = f"""
    BootLog {{
        height: 1fr;
        background: {_BG};
        padding: 0 1;
        border: none;
        scrollbar-color: {_CYAN};
    }}
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pending: Optional[BootLogLine] = None  # last unresolved running line

    async def add_message(self, msg: str, status: str = "running") -> None:
        if status == "running":
            line = BootLogLine(msg)
            await self.mount(line)
            self._pending = line
        else:
            if self._pending is not None:
                self._pending.set_result(msg, status)
                self._pending = None
            else:
                # Result with no prior running line — add standalone
                line = BootLogLine(msg)
                await self.mount(line)
                line.set_result(msg, status)
        self.scroll_end(animate=False)


class BootFaceBar(Static):
    """Cycling face animation at the bottom of BootScreen."""

    DEFAULT_CSS = f"""
    BootFaceBar {{
        height: 1;
        dock: bottom;
        background: {_BG2};
        color: {_CYAN};
        text-align: center;
    }}
    """

    _frame_idx: reactive[int] = reactive(0)

    def on_mount(self) -> None:
        self.set_interval(0.4, self._advance)

    def _advance(self) -> None:
        self._frame_idx = (self._frame_idx + 1) % len(_BOOT_FACE_FRAMES)

    def watch__frame_idx(self, _: int) -> None:
        face = _BOOT_FACE_FRAMES[self._frame_idx]
        self.update(f"[{_CYAN}]{face}[/]  [dim {_DIM}]booting…[/]")


# ──────────────────────────────────────────────────────────────────────────────
# BootScreen
# ──────────────────────────────────────────────────────────────────────────────


class BootScreen(Screen):
    """Shown while bootstrap() runs in a thread."""

    DEFAULT_CSS = f"""
    BootScreen {{
        background: {_BG};
    }}
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._boot_queue: Optional[asyncio.Queue] = None

    def compose(self) -> ComposeResult:
        yield BootBanner()
        yield BootLog(id="boot-log")
        yield BootFaceBar()

    def on_mount(self) -> None:
        self._boot_queue = asyncio.Queue()
        asyncio.create_task(self._run_bootstrap())
        asyncio.create_task(self._consume_messages())

    async def _run_bootstrap(self) -> None:
        loop = asyncio.get_running_loop()
        queue = self._boot_queue

        def progress_cb(msg: str, status: str = "running") -> None:
            if queue is not None:
                loop.call_soon_threadsafe(queue.put_nowait, (msg, status))

        state = None
        try:
            from buddy.buddy_core.boot import bootstrap, BootstrapOptions

            opts = BootstrapOptions(show_boot_ui=False)
            state = await asyncio.to_thread(bootstrap, opts, progress_cb)
        except asyncio.CancelledError:
            # App is shutting down during boot — signal consumer and propagate
            if queue is not None:
                loop.call_soon_threadsafe(queue.put_nowait, ("__DONE__", None))
            raise
        except BaseException as ex:
            # Catch SystemExit too (raised by sys.exit() in bootstrap)
            logger.exception("bootstrap failed: %r", ex)
        if queue is not None:
            loop.call_soon_threadsafe(queue.put_nowait, ("__DONE__", state))

    async def _consume_messages(self) -> None:
        log = self.query_one(BootLog)
        while True:
            if self._boot_queue is None:
                await asyncio.sleep(0.1)
                continue
            item = await self._boot_queue.get()
            msg, payload = item
            if msg == "__DONE__":
                # Must await the transition — switch_screen needs async context
                app = self.app
                if isinstance(app, BuddyApp):
                    await app._async_on_boot_done(payload)
                return
            await log.add_message(msg, payload)


# ──────────────────────────────────────────────────────────────────────────────
# MainScreen widgets
# ──────────────────────────────────────────────────────────────────────────────


class BannerPane(Static):
    """Left side of the header — static AURORA logo + tagline."""

    DEFAULT_CSS = f"""
    BannerPane {{
        width: auto;
        background: {_BG};
        padding: 0 2;
    }}
    """

    def on_mount(self) -> None:
        self.update(_banner_markup(compact=True))


class InfoPane(Static):
    """
    Right side of the header — live system info panel.

    Static fields (HW, LLM, web) loaded once from state on mount.
    Clock, uptime, state, voice refresh every second via set_interval.
    Memory counts + last retrieved memory updated via public methods
    called from MainScreen after each turn.
    """

    DEFAULT_CSS = f"""
    InfoPane {{
        width: 1fr;
        background: {_BG};
        padding: 1 2 0 3;
        border-left: heavy {_CYAN};
    }}
    """

    def __init__(
        self,
        state: Any,
        sys_state: "SystemState",
        state_lock: "threading.Lock",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._state = state
        self._sys_state = sys_state
        self._state_lock = state_lock
        self._session_start = time.monotonic()
        # turn / perf
        self._turn = 0
        self._last_turn_ms: Optional[int] = None
        # memory
        self._mem_flash = 0
        self._mem_short = 0
        self._mem_long = 0
        self._last_memory = ""
        # static (loaded on mount)
        self._user_name = "—"
        self._llm_label = "—"
        self._n_ctx = "—"
        self._hw_line = "—"
        self._web = "—"
        self._voice_enabled = False

    def on_mount(self) -> None:
        self._load_static()
        self.set_interval(1.0, self._tick)
        self._redraw()

    # ── Static info ───────────────────────────────────────────────────

    def _load_static(self) -> None:
        try:
            cfg = getattr(self._state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            runtime = cfg.get("runtime", {}) or {}
            fs = runtime.get("fs", {}) or {}

            # User name from os_profile JSON
            op_path = fs.get("os_profile_file")
            if op_path and Path(op_path).exists():
                import json as _json

                with open(op_path, "r", encoding="utf-8") as _f:
                    op = _json.load(_f)
                self._user_name = op.get("user_preferred_name", "—")
                ram_gb = (op.get("ram") or {}).get("total_gb", "?")
                cores = (op.get("cpu") or {}).get("logical_cores", "?")
                gpu = op.get("gpu") or {}
                gpu_name = (gpu.get("name") or "")[:14]
                vram = gpu.get("total_vram_gb")
                hw_parts = [f"{ram_gb}GB", f"{cores}c"]
                if gpu_name:
                    hw_parts.append(f"{gpu_name}" + (f"·{vram}GB" if vram else ""))
                self._hw_line = " · ".join(hw_parts)

            # LLM
            llama_cfg = buddy_cfg.get("llama", {}) or {}
            model = (
                llama_cfg.get("model_gguf", "")
                or llama_cfg.get("model_name", "")
                or "—"
            )
            self._llm_label = model.replace(".gguf", "")[:22]

            # Context budget
            cb = getattr(self._state, "context_budget", None)
            if cb:
                self._n_ctx = str(getattr(cb, "n_ctx", "—"))

            # Web + voice
            web_cfg = buddy_cfg.get("web_search", {}) or {}
            self._web = str(web_cfg.get("engine", "duckduckgo"))
            feat_cfg = buddy_cfg.get("features", {}) or {}
            self._voice_enabled = bool(feat_cfg.get("enable_audio_stt", False))
        except Exception:
            pass

    # ── Live refresh ──────────────────────────────────────────────────

    def _tick(self) -> None:
        self._redraw()

    def _redraw(self) -> None:
        try:
            self.update(self._build())
        except Exception:
            logger.exception("InfoPane._redraw failed")

    def _build(self) -> str:
        # ── clock + date ────────────────────────────────────────────
        now = time.localtime()
        date_s = time.strftime("%a %b %d", now)
        time_s = time.strftime("%H:%M:%S", now)

        # ── uptime ──────────────────────────────────────────────────
        elapsed = int(time.monotonic() - self._session_start)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        uptime = f"{h:02d}:{m:02d}:{s:02d}"

        # ── state ────────────────────────────────────────────────────
        with self._state_lock:
            sleeping = self._sys_state.sleeping
            pipeline = self._sys_state.pipeline_running
            voice_muted = self._sys_state.voice_muted

        if sleeping:
            state_s = f"[{_VIOLET}]sleeping[/]"
        elif pipeline:
            state_s = f"[{_CYAN}]thinking[/]"
        else:
            state_s = f"[{_GREEN}]idle[/]"

        # ── voice ────────────────────────────────────────────────────
        if not self._voice_enabled:
            voice_s = f"[{_DIM}]off[/]"
        elif voice_muted:
            voice_s = f"[{_YELLOW}]muted[/]"
        else:
            voice_s = f"[{_GREEN}]on[/]"

        # ── last turn time ───────────────────────────────────────────
        lat_s = (
            f"[{_DIM}]{self._last_turn_ms}ms[/]"
            if self._last_turn_ms is not None
            else f"[{_DIM}]—[/]"
        )

        # ── memory counts ────────────────────────────────────────────
        mem_s = (
            f"[{_CYAN}]{self._mem_flash}[/]"
            f"[{_DIM}]·[/]"
            f"[{_BLUE}]{self._mem_short}[/]"
            f"[{_DIM}]·[/]"
            f"[{_VIOLET}]{self._mem_long}[/]"
        )

        # ── last memory ──────────────────────────────────────────────
        last_mem = (self._last_memory or "").strip()
        if len(last_mem) > 50:
            last_mem = last_mem[:47] + "…"
        last_mem_s = f"[{_DIM}]{last_mem or '—'}[/]"

        D = f"[{_DIM}]{'─' * 44}[/]"

        lines = [
            (
                f"[{_CYAN}]◈[/] [{_WHITE}]{self._user_name}[/]"
                f"    [{_DIM}]{date_s}  {time_s}[/]"
            ),
            D,
            (
                f"[{_DIM}]LLM  [/][{_BLUE}]{self._llm_label}[/]"
                f"[{_DIM}] · {self._n_ctx}t[/]"
                f"    [{_DIM}]State [/]{state_s}"
            ),
            (
                f"[{_DIM}]HW   {self._hw_line}[/]"
                f"    [{_DIM}]Turn  [/][{_WHITE}]{self._turn}[/]"
                f"  [{_DIM}]Up [/][{_WHITE}]{uptime}[/]"
            ),
            (
                f"[{_DIM}]Mem  [/]{mem_s}"
                f"[{_DIM}] (f·s·l)   Web [/][{_BLUE}]{self._web}[/]"
                f"  [{_DIM}]Voice [/]{voice_s}"
            ),
            f"[{_DIM}]Last turn [/]{lat_s}",
            D,
            f"[{_DIM}]❝  [/]{last_mem_s}",
        ]
        return "\n".join(lines)

    # ── Public update API (called from MainScreen) ────────────────────

    def update_turn(self, n: int, turn_ms: Optional[int] = None) -> None:
        self._turn = n
        if turn_ms is not None:
            self._last_turn_ms = turn_ms
        self._redraw()

    def update_memory_counts(self, mm: Any) -> None:
        """Pull fresh tier counts + last retrieved text from MemoryManager."""
        try:
            counts = mm.tier_counts()
            self._mem_flash = counts.get("flash", 0)
            self._mem_short = counts.get("short", 0)
            self._mem_long = counts.get("long", 0)
            lrt = getattr(mm, "last_retrieved_text", "")
            if lrt:
                self._last_memory = lrt
        except Exception:
            pass
        self._redraw()


class BuddyHeader(Horizontal):
    """Full-width header: BannerPane (left) | InfoPane (right)."""

    DEFAULT_CSS = f"""
    BuddyHeader {{
        height: auto;
        dock: top;
        background: {_BG};
        border-bottom: heavy {_CYAN};
    }}
    """


class StatusBar(Static):
    """
    Bottom status bar: live info on the left, shortcuts on the right.
    Temporary hint messages replace the entire line for a few seconds.
    """

    DEFAULT_CSS = f"""
    StatusBar {{
        height: 1;
        background: {_BG2};
        color: {_DIM};
        padding: 0 2;
    }}
    """

    _SHORTCUTS = f"[dim {_DIM}]ESC:interrupt  F2:mute  F3:sleep  Ctrl+C×2:quit[/]"

    _info: reactive[str] = reactive("")
    _hint: reactive[str] = reactive("")

    def render(self) -> str:
        if self._hint:
            return self._hint
        left = self._info or f"[dim {_DIM}]Voice: —  ·  Turn: 0[/]"
        return f"{left}    {self._SHORTCUTS}"

    def set_info(self, *, voice: str = "", turn: int = 0) -> None:
        parts = [
            f"[{_DIM}]Voice: {voice or '—'}[/]",
            f"[{_DIM}]Turn: {turn}[/]",
        ]
        self._info = f"  [{_DIM}]·[/]  ".join(parts)

    def set_hint(self, msg: str, clear_after: float = 5.0) -> None:
        self._hint = msg
        if clear_after > 0:
            self.set_timer(clear_after, self._clear_hint)

    def _clear_hint(self) -> None:
        self._hint = ""


class SpinnerBar(Static):
    """One-line face + label — shown when pipeline is processing."""

    DEFAULT_CSS = f"""
    SpinnerBar {{
        height: 1;
        background: {_BG};
        color: {_CYAN};
        display: none;
    }}
    SpinnerBar.visible {{
        display: block;
    }}
    """

    _label: reactive[str] = reactive("Thinking")
    _state: reactive[str] = reactive("thinking")
    _frame: reactive[int] = reactive(0)

    def on_mount(self) -> None:
        self.set_interval(0.09, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % max(
            len(_THINKING_FRAMES), len(_WAITING_FRAMES), len(_WORKING_FRAMES)
        )

    def render(self) -> str:
        st = self._state
        frames = (
            _WAITING_FRAMES
            if st == "waiting"
            else _WORKING_FRAMES if st == "working" else _THINKING_FRAMES
        )
        face = frames[self._frame % len(frames)]
        return f" [{_CYAN}]{face:<12}[/]  [{_WHITE}]{self._label}…[/]"

    def show(self, label: str = "Thinking", state: str = "thinking") -> None:
        self._label = label
        self._state = state
        self.add_class("visible")

    def hide(self) -> None:
        self.remove_class("visible")

    def update_label(self, label: str, state: str = "thinking") -> None:
        self._label = label[:120]
        self._state = state


class ChatBubble(Static):
    """A single chat message rendered as a rounded bubble."""

    DEFAULT_CSS = f"""
    ChatBubble {{
        margin: 0 0 1 0;
        padding: 0 1;
    }}
    ChatBubble.user {{
        color: {_CYAN};
    }}
    ChatBubble.buddy {{
        color: {_VIOLET};
    }}
    ChatBubble.meta {{
        color: {_DIM};
        text-align: center;
    }}
    """

    def __init__(self, text: str, kind: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text = text
        self._kind = kind
        self.add_class(kind)

    def on_mount(self) -> None:
        self._render_bubble_content(self._text)

    def _render_bubble_content(self, text: str, cursor: bool = False) -> None:
        if self._kind == "meta":
            self.update(f"[dim {_DIM}]  ◦ {text}[/]")
            return

        display = text + ("▋" if cursor else "")

        if self._kind == "user":
            panel = Panel(
                Text(display, style=_WHITE),
                title=f"[{_CYAN}]▌ You[/]",
                title_align="left",
                border_style=_CYAN,
                width=min(72, max(32, len(text) + 8)),
                padding=(0, 1),
            )
            self.update(panel)
        else:
            panel = Panel(
                Text(display, style=_WHITE),
                title=f"[{_VIOLET}]◈ Buddy[/]",
                title_align="left",
                border_style=_VIOLET,
                width=min(72, max(32, len(text) + 8)),
                padding=(0, 1),
            )
            self.update(Align(panel, align="right"))

    def stream_update(self, text: str, done: bool = False) -> None:
        self._text = text
        self._render_bubble_content(text, cursor=not done)


class ChatLog(ScrollableContainer):
    """Scrollable container for chat bubbles."""

    DEFAULT_CSS = f"""
    ChatLog {{
        height: 1fr;
        background: {_BG};
        padding: 0 1;
        scrollbar-color: {_CYAN};
        scrollbar-color-hover: {_BLUE};
    }}
    """

    async def add_message(self, text: str, kind: str) -> None:
        bubble = ChatBubble(text, kind)
        await self.mount(bubble)
        self.scroll_end(animate=False)

    async def add_streaming_bubble(self) -> ChatBubble:
        bubble = ChatBubble("", "buddy")
        await self.mount(bubble)
        self.scroll_end(animate=False)
        return bubble


# ──────────────────────────────────────────────────────────────────────────────
# SleepView — two independent timers, progress bars, Esc hint
# ──────────────────────────────────────────────────────────────────────────────


class SleepView(Widget):
    """
    Sleep/consolidation visualization.

    Stars drift at 0.15s intervals; face cycles at 0.4s independently.
    Stats show flash/short/long counts as ASCII progress bars.
    """

    DEFAULT_CSS = f"""
    SleepView {{
        height: 1fr;
        background: {_BG2};
        color: {_CYAN};
    }}
    """

    stats_flash: reactive[int] = reactive(0)
    stats_short: reactive[int] = reactive(0)
    stats_long: reactive[int] = reactive(0)
    sleep_start_ts: reactive[float] = reactive(0.0)

    def on_mount(self) -> None:
        self._face_idx = 0
        self._stars = self._make_stars(40)
        self._face_timer = self.set_interval(0.4, self._tick_face)
        self._star_timer = self.set_interval(0.15, self._tick_stars)

    def _make_stars(self, n: int) -> List[dict]:
        chars = (
            ["·", "·", "·", "*", "✦", "✧", "·"]
            if _USE_UNICODE
            else [".", ".", ".", "*", "+", "*", "."]
        )
        return [
            {
                "x": random.random(),
                "y": random.random(),
                "dx": (random.random() - 0.5) * 0.005,
                "dy": (random.random() - 0.5) * 0.003,
                "ch": random.choice(chars),
            }
            for _ in range(n)
        ]

    def _tick_face(self) -> None:
        self._face_idx = (self._face_idx + 1) % len(_SLEEP_FACE_FRAMES)
        self.refresh()

    def _tick_stars(self) -> None:
        for s in self._stars:
            s["x"] = (s["x"] + s["dx"]) % 1.0
            s["y"] = (s["y"] + s["dy"]) % 1.0
        self.refresh()

    def _make_bar(self, count: int, total: int, width: int = 20) -> str:
        ratio = count / max(1, total)
        filled = int(ratio * width)
        if _USE_UNICODE:
            return "█" * filled + "░" * (width - filled)
        return "#" * filled + "-" * (width - filled)

    def render(self) -> str:
        # Adapt to actual widget size
        try:
            w = max(50, self.size.width)
            h = max(12, self.size.height)
        except Exception:
            w, h = 60, 20

        # Reserve bottom rows for stats
        star_h = max(6, h - 7)
        grid = [[" "] * w for _ in range(star_h)]

        # Draw stars
        for s in self._stars:
            sx = int(s["x"] * (w - 1))
            sy = int(s["y"] * (star_h - 1))
            grid[sy][sx] = s["ch"]

        # Draw face in centre
        face = _SLEEP_FACE_FRAMES[self._face_idx]
        cy = star_h // 2
        cx = (w - len(face)) // 2
        for i, ch in enumerate(face):
            if 0 <= cx + i < w:
                grid[cy][cx + i] = ch

        # Header line
        hdr = "· · ·  rest mode  · · ·" if _USE_UNICODE else "- - -  rest mode  - - -"
        hx = max(0, (w - len(hdr)) // 2)
        grid[1] = list(" " * hx + hdr)[:w] + [" "] * max(0, w - hx - len(hdr))

        star_lines = ["".join(row) for row in grid]

        # Stats section
        elapsed = max(0.0, time.monotonic() - self.sleep_start_ts)
        em, es = int(elapsed // 60), int(elapsed % 60)
        total = max(1, self.stats_flash + self.stats_short + self.stats_long)
        bar_w = min(20, w // 4)

        sep = "─" * min(w - 4, 44) if _USE_UNICODE else "-" * min(w - 4, 44)
        sep_line = " " * max(0, (w - len(sep)) // 2) + sep

        stats = [
            sep_line,
            (
                f"  flash  {self._make_bar(self.stats_flash, total, bar_w)} "
                f" {self.stats_flash}"
            ),
            (
                f"  short  {self._make_bar(self.stats_short, total, bar_w)} "
                f" {self.stats_short}"
            ),
            (
                f"  long   {self._make_bar(self.stats_long,  total, bar_w)} "
                f" {self.stats_long}"
            ),
            f"  sleeping {em:02d}:{es:02d}",
            f"  [ Press Esc to wake ]",
        ]

        star_block = "\n".join(star_lines)
        stats_block = "\n".join(stats)
        return f"[{_CYAN}]{star_block}[/]\n[dim {_DIM}]{stats_block}[/]"

    def reset_stats(self) -> None:
        self.stats_flash = 0
        self.stats_short = 0
        self.stats_long = 0
        self.sleep_start_ts = time.monotonic()

    def update_consolidation_stats(
        self, flash: int = 0, short: int = 0, long: int = 0
    ) -> None:
        self.stats_flash = flash
        self.stats_short = short
        self.stats_long = long


# ──────────────────────────────────────────────────────────────────────────────
# Input widgets
# ──────────────────────────────────────────────────────────────────────────────


class MicIndicator(Static):
    """Shows mic state: idle / active / muted."""

    DEFAULT_CSS = f"""
    MicIndicator {{
        width: 4;
        height: 3;
        padding: 1 0;
        color: {_DIM};
        text-align: center;
        content-align: center middle;
    }}
    MicIndicator.active {{
        color: {_CYAN};
    }}
    MicIndicator.muted {{
        color: {_DIM};
    }}
    """

    _state: reactive[str] = reactive("idle")

    def render(self) -> str:
        if self._state == "muted":
            return "[dim]🔇[/]" if _USE_UNICODE else "[dim]M[/]"
        if self._state == "active":
            return f"[{_CYAN}]◎[/]" if _USE_UNICODE else f"[{_CYAN}]O[/]"
        return f"[dim {_DIM}]◎[/]" if _USE_UNICODE else f"[dim {_DIM}]o[/]"

    def set_state(self, state: str) -> None:
        self._state = state
        self.remove_class("active", "muted")
        if state == "active":
            self.add_class("active")
        elif state == "muted":
            self.add_class("muted")


class BuddyInput(Input):
    """Text input — ESC triggers interrupt instead of clearing."""

    DEFAULT_CSS = f"""
    BuddyInput {{
        width: 1fr;
        height: 3;
        border: tall {_CYAN};
        background: {_BG};
        color: {_WHITE};
        padding: 0 1;
    }}
    BuddyInput:focus {{
        border: tall {_CYAN};
        background: {_BG};
    }}
    """

    def __init__(self, on_escape: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._on_escape = on_escape

    def _on_key(self, event: Any) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self._on_escape()
        elif event.key == "ctrl+c":
            # Input widget may consume ctrl+c for copy — explicitly forward to screen.
            event.prevent_default()
            event.stop()
            screen = self.screen
            if hasattr(screen, "action_quit_request"):
                screen.action_quit_request()  # type: ignore


class InputBar(Horizontal):
    """Bottom input row: MicIndicator + BuddyInput."""

    DEFAULT_CSS = f"""
    InputBar {{
        height: 3;
        background: {_BG};
        padding: 0 0;
    }}
    """


class BottomSection(Vertical):
    """
    Docked bottom container that guarantees the visual order:
      SpinnerBar  (thinking indicator — hidden when idle)
      InputBar    (text input)
      StatusBar   (live info + shortcuts)
    Keeping all three in one docked Vertical avoids Textual dock-order ambiguity.
    """

    DEFAULT_CSS = f"""
    BottomSection {{
        dock: bottom;
        height: auto;
        background: {_BG};
    }}
    """


# ──────────────────────────────────────────────────────────────────────────────
# MainScreen
# ──────────────────────────────────────────────────────────────────────────────


class MainScreen(Screen):
    """
    Primary chat screen.

    Layout (top → bottom):
      BuddyHeader   (dock top, auto height)
      ContentSwitcher (1fr — fills remaining)
        #chat-view  → ChatLog
        #sleep-view → SleepView
      BottomSection  (dock bottom, auto height)
        SpinnerBar   (1 line — hidden when idle)
        InputBar     (3 lines)
        StatusBar    (1 line — live info + shortcuts)
    """

    BINDINGS = [
        ("f2", "toggle_mute", "Mute/Unmute"),
        ("f3", "toggle_sleep", "Sleep/Wake"),
        ("ctrl+c", "quit_request", "Quit"),
    ]

    CSS = f"""
    MainScreen {{
        background: {_BG};
    }}

    ContentSwitcher {{
        height: 1fr;
        background: {_BG};
    }}
    """

    def __init__(
        self,
        state: Any,
        input_queue: InputQueue,
        sys_state: SystemState,
        state_lock: threading.Lock,
        interrupt_event: threading.Event,
        memory_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._state = state
        self._iq = input_queue
        self._sys_state = sys_state
        self._state_lock = state_lock
        self._interrupt_event = interrupt_event
        self._memory_manager = memory_manager
        self._active_turn: Optional[asyncio.Task] = None
        self._turn_lock = asyncio.Lock()
        self._quit_event = asyncio.Event()
        self._last_interrupt_ts = 0.0
        self._ctrl_c_count = 0
        self._last_ctrl_c_ts = 0.0
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._stt: Any = None
        self._idle_timeout_s: float = 20 * 60
        self._last_activity_ts: float = time.monotonic()
        self._turn_count: int = 0

    # ── Compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with BuddyHeader():
            yield BannerPane()
            yield InfoPane(self._state, self._sys_state, self._state_lock)
        with ContentSwitcher(initial="chat-view"):
            yield ChatLog(id="chat-view")
            yield SleepView(id="sleep-view")
        with BottomSection():
            yield SpinnerBar()
            with InputBar():
                yield MicIndicator()
                yield BuddyInput(
                    on_escape=self._handle_escape,
                    placeholder="Type a message… (Enter to send, Esc to interrupt)",
                    id="buddy-input",
                )
            yield StatusBar()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._main_loop = asyncio.get_running_loop()

        try:
            cfg = getattr(self._state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            general_cfg = buddy_cfg.get("general", {}) or {}
            self._idle_timeout_s = float(
                general_cfg.get("sleep_after_idle_sec", 20) * 60
            )
        except Exception:
            self._idle_timeout_s = 1200.0

        try:
            self.query_one(BuddyInput).focus()
        except Exception:
            pass

        asyncio.create_task(self._inactivity_watcher())
        self._refresh_info_bar()

    def _refresh_info_bar(self, turn_ms: Optional[int] = None) -> None:
        # StatusBar (bottom) — voice + turn count
        try:
            cfg = getattr(self._state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            feat_cfg = buddy_cfg.get("features", {}) or {}
            voice = "on" if feat_cfg.get("enable_audio_stt", False) else "off"
            self.query_one(StatusBar).set_info(voice=voice, turn=self._turn_count)
        except Exception:
            pass
        # InfoPane (header) — turn count, timing, memory counts
        try:
            self.query_one(InfoPane).update_turn(self._turn_count, turn_ms)
            if self._memory_manager is not None:
                self.query_one(InfoPane).update_memory_counts(self._memory_manager)
        except Exception:
            pass

    # ── Signal / interrupt helpers ─────────────────────────────────────────────

    def _handle_escape(self) -> None:
        self._request_interrupt()

    def _request_interrupt(self) -> None:
        now = time.monotonic()
        if (now - self._last_interrupt_ts) < 0.75:
            return
        self._last_interrupt_ts = now
        self._interrupt_event.set()
        if self._active_turn and not self._active_turn.done():
            self._active_turn.cancel()
        self._stop_spinner()
        self.query_one(StatusBar).set_hint(f"[{_YELLOW}]⛔ interrupted[/]")
        logger.info("interrupt: requested via ESC")
        loop = self._main_loop
        if loop:
            loop.call_soon_threadsafe(self._iq._q.put_nowait, INTERRUPT_SENTINEL)

    def action_toggle_mute(self) -> None:
        self._toggle_voice_mute()

    def action_toggle_sleep(self) -> None:
        self._toggle_sleep()

    def action_quit_request(self) -> None:
        now = time.monotonic()
        if (now - self._last_ctrl_c_ts) < 1.25:
            # Second Ctrl+C within 1.25s — exit for real.
            self._quit_event.set()
            try:
                loop = asyncio.get_running_loop()
                self._iq.push_sentinel(EXIT_SENTINEL, loop)
            except Exception:
                pass
            self.app.exit()
        else:
            self._last_ctrl_c_ts = now
            self._request_interrupt()
            try:
                self.query_one(StatusBar).set_hint(
                    f"[{_YELLOW}]Ctrl+C again to quit[/]", 2.0
                )
            except Exception:
                pass

    # ── Input handling ─────────────────────────────────────────────────────────

    @on(Input.Submitted)
    async def _on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        event.input.clear()
        if not text:
            return

        if _should_exit(text):
            ...
            return

        if text.lower() in {"!sleep", "/sleep"}:
            asyncio.create_task(self._async_set_sleeping(True))
            return

        if text.lower() in {"!wake", "/wake"}:
            asyncio.create_task(self._async_set_sleeping(False))
            return

        await self._iq.push_typed(text)

        # Only start a new turn if none is running
        if self._active_turn is None or self._active_turn.done():
            self._notify_activity()
            with self._state_lock:
                is_sleeping = self._sys_state.sleeping
            if is_sleeping:
                asyncio.create_task(self._async_set_sleeping(False))

            await self.query_one(ChatLog).add_message(text, "user")

            try:
                img_paths = extract_image_paths(text)
                if img_paths:
                    names = ", ".join(os.path.basename(p) for p in img_paths)
                    await self.query_one(ChatLog).add_message(
                        f"[image: {names}]", "meta"
                    )
            except Exception:
                pass

            # ✅ Schedule as a background task — handler returns immediately
            self._active_turn = asyncio.create_task(self._run_turn(text))

    # ── Voice input ────────────────────────────────────────────────────────────

    def handle_voice_text(self, text: str) -> None:
        t = (text or "").strip()
        if not t:
            return

        cmd = _match_voice_command(t)
        now = time.monotonic()

        if cmd != VoiceCmd.NONE:
            with self._state_lock:
                if (now - self._sys_state.last_voice_cmd_ts) < 0.75:
                    return
                self._sys_state.last_voice_cmd_ts = now

        with self._state_lock:
            sleeping = self._sys_state.sleeping
            muted = self._sys_state.voice_muted
            running = self._sys_state.pipeline_running

        if running and cmd == VoiceCmd.STOP:
            self._request_interrupt()
            return
        if running and cmd == VoiceCmd.NONE:
            return
        if sleeping:
            if cmd == VoiceCmd.WAKE:
                asyncio.create_task(self._async_set_sleeping(False))
            return
        if cmd == VoiceCmd.SLEEP:
            asyncio.create_task(self._async_set_sleeping(True))
            return
        if cmd in (VoiceCmd.MUTE, VoiceCmd.TOGGLE_MUTE):
            self._toggle_voice_mute()
            return
        if cmd == VoiceCmd.UNMUTE:
            self._set_voice_mute(False)
            return
        if muted:
            return

        loop = self._main_loop
        if loop:
            self._iq.push_voice(t, loop)
            asyncio.create_task(self._handle_voice_input(t))

    async def _handle_voice_input(self, text: str) -> None:
        if self._active_turn and not self._active_turn.done():
            return
        self._notify_activity()
        await self.query_one(ChatLog).add_message(text, "user")
        await self._run_turn(text)

    # ── Turn execution ─────────────────────────────────────────────────────────

    async def _run_turn(self, user_text: str) -> None:
        async with self._turn_lock:
            self._interrupt_event.clear()
            turn_id = f"turn-{uuid.uuid4().hex[:8]}"
            self._turn_count += 1
            logger.info("turn.start id=%s chars=%d", turn_id, len(user_text))

            with self._state_lock:
                self._sys_state.pipeline_running = True

            self._start_spinner("Thinking", "thinking")
            self._refresh_info_bar()
            _turn_start = time.perf_counter()

            loop = self._main_loop
            current_label = "Thinking"
            stream_buf: List[str] = []
            _last_preview_t = 0.0
            _thinking_done = False

            # ── Thread-safe progress_cb ────────────────────────────────────
            # Called from asyncio.to_thread() worker — MUST NOT call widget
            # methods directly. Use loop.call_soon_threadsafe() to schedule
            # updates on the event loop.
            def progress_cb(text: str, stream: bool = True) -> None:
                nonlocal current_label, stream_buf, _last_preview_t, _thinking_done
                if loop is None:
                    return
                now = time.perf_counter()

                if not stream:
                    # Non-streaming label (step description)
                    _thinking_done = False
                    stream_buf.clear()
                    current_label = text.strip() or current_label
                    loop.call_soon_threadsafe(
                        self._update_spinner, current_label, "working"
                    )
                    return

                # Streaming token — buffer and throttle
                if now - _last_preview_t < 0.05:
                    return
                _last_preview_t = now

                if not _thinking_done:
                    joined = (
                        "".join(stream_buf)
                        .replace("\r", " ")
                        .replace("\n", " ")
                        .strip()
                    )
                    preview = joined[-80:]
                    loop.call_soon_threadsafe(
                        self._update_spinner, preview or current_label, "thinking"
                    )

                    if "</THINK>" in joined:
                        _thinking_done = True
                        stream_buf.clear()
                        loop.call_soon_threadsafe(
                            self._update_spinner, "Thinking", "thinking"
                        )
                        return

            # ── Async pipeline callbacks (run on event loop) ───────────────
            async def pipeline_output(text: str) -> None:
                self._stop_spinner()
                await self.query_one(ChatLog).add_message(text, "buddy")
                self._start_spinner(current_label, "thinking")

            async def pipeline_input() -> str:
                self._notify_activity()
                with self._state_lock:
                    self._sys_state.pipeline_running = False
                self._stop_spinner()

                result = await self._iq.get()

                with self._state_lock:
                    self._sys_state.pipeline_running = True

                if result in (INTERRUPT_SENTINEL, "!", "/stop", "stop", "cancel"):
                    raise asyncio.CancelledError("interrupted by user")

                if result and not _should_exit(result):
                    await self.query_one(ChatLog).add_message(result, "user")

                self._start_spinner(current_label, "waiting")
                return result

            try:
                await handle_turn(
                    state=self._state,
                    source="mixed",
                    user_message=user_text,
                    ui_output=pipeline_output,
                    ui_input=pipeline_input,
                    progress_cb=progress_cb,
                    interrupt_event=self._interrupt_event,
                )
                logger.info("turn.done id=%s", turn_id)
            except asyncio.CancelledError:
                logger.info("turn.cancelled id=%s", turn_id)
            except Exception as ex:
                logger.exception("turn.crash id=%s err=%r", turn_id, ex)
                self.query_one(StatusBar).set_hint(f"[{_RED}]⚠ error: {ex}[/]")
            finally:
                _turn_ms = int((time.perf_counter() - _turn_start) * 1000)
                self._active_turn = None
                self._stop_spinner()
                with self._state_lock:
                    self._sys_state.pipeline_running = False
                self._notify_activity()
                self._refresh_info_bar(turn_ms=_turn_ms)

    # ── Spinner helpers ────────────────────────────────────────────────────────

    def _start_spinner(self, label: str, state: str = "thinking") -> None:
        try:
            self.query_one(SpinnerBar).show(label, state)
        except Exception:
            pass

    def _stop_spinner(self) -> None:
        try:
            self.query_one(SpinnerBar).hide()
        except Exception:
            pass

    def _update_spinner(self, label: str, state: str = "thinking") -> None:
        """Called from event loop (via call_soon_threadsafe)."""
        try:
            self.query_one(SpinnerBar).update_label(label, state)
        except Exception:
            pass

    # ── Sleep ──────────────────────────────────────────────────────────────────

    def _toggle_sleep(self) -> None:
        with self._state_lock:
            sleeping = self._sys_state.sleeping
        asyncio.create_task(self._async_set_sleeping(not sleeping))

    def _set_sleeping(self, sleeping: bool) -> None:
        asyncio.create_task(self._async_set_sleeping(sleeping))

    async def _async_set_sleeping(self, sleeping: bool) -> None:
        with self._state_lock:
            self._sys_state.sleeping = sleeping

        switcher = self.query_one(ContentSwitcher)
        sleep_view = self.query_one(SleepView)

        if sleeping:
            sleep_view.reset_stats()
            switcher.current = "sleep-view"
            self.query_one(StatusBar).set_hint(
                f"[dim {_VIOLET}]😴 sleeping — consolidating memories…[/]", 0
            )
            mm = self._memory_manager
            if mm is not None:
                started = mm.start_consolidation(on_done=self._on_consolidation_done)
                if started:
                    with self._state_lock:
                        self._sys_state.consolidating = True
        else:
            mm = self._memory_manager
            if mm is not None and getattr(mm, "is_consolidating", False):
                mm.stop_consolidation(wait=False)

            with self._state_lock:
                self._sys_state.sleeping = False
                self._sys_state.consolidating = False

            switcher.current = "chat-view"
            self.query_one(StatusBar).set_hint(f"[{_GREEN}]🌅 awake[/]", 4.0)
            self._notify_activity()

    def _on_consolidation_done(self, report: Any) -> None:
        with self._state_lock:
            self._sys_state.consolidating = False
        try:
            sv = self.query_one(SleepView)
            if report:
                flash = getattr(report, "flash_processed", 0) or 0
                short = getattr(report, "short_processed", 0) or 0
                long_ = (
                    getattr(report, "long_processed", 0)
                    or getattr(report, "summarized", 0)
                    or 0
                )
                sv.update_consolidation_stats(flash=flash, short=short, long=long_)
        except Exception:
            pass

    # ── Voice mute ─────────────────────────────────────────────────────────────

    def _set_voice_mute(self, muted: bool) -> None:
        with self._state_lock:
            if self._sys_state.voice_muted == muted:
                return
            if self._stt is not None:
                try:
                    self._stt.mute() if muted else self._stt.unmute()
                except Exception:
                    pass
            self._sys_state.voice_muted = muted
        try:
            self.query_one(MicIndicator).set_state("muted" if muted else "idle")
        except Exception:
            pass
        icon = "🔇" if (muted and _USE_UNICODE) else ("M" if muted else "")
        self.query_one(StatusBar).set_hint(
            f"[{_DIM}]{icon} muted[/]" if muted else f"[{_GREEN}]🔊 unmuted[/]"
        )
        self._refresh_info_bar()

    def _toggle_voice_mute(self) -> None:
        with self._state_lock:
            muted = not self._sys_state.voice_muted
        self._set_voice_mute(muted)

    def set_stt_engine(self, stt: Any) -> None:
        self._stt = stt

    def set_mic_active(self) -> None:
        try:
            self.query_one(MicIndicator).set_state("active")
        except Exception:
            pass

    def set_mic_idle(self) -> None:
        try:
            self.query_one(MicIndicator).set_state("idle")
        except Exception:
            pass

    # ── Activity / idle ────────────────────────────────────────────────────────

    def _notify_activity(self) -> None:
        self._last_activity_ts = time.monotonic()

    async def _inactivity_watcher(self) -> None:
        try:
            while not self._quit_event.is_set():
                if self._idle_timeout_s <= 0:
                    await asyncio.sleep(60.0)
                    continue
                elapsed = time.monotonic() - self._last_activity_ts
                remaining = self._idle_timeout_s - elapsed
                if remaining <= 0:
                    with self._state_lock:
                        already_sleeping = self._sys_state.sleeping
                        running = self._sys_state.pipeline_running
                    if not already_sleeping and not running:
                        logger.info("inactivity: %.0fs idle — sleeping", elapsed)
                        await self._async_set_sleeping(True)
                    await asyncio.sleep(self._idle_timeout_s)
                else:
                    await asyncio.sleep(min(remaining, 60.0))
        except asyncio.CancelledError:
            pass
        except Exception as ex:
            logger.exception("inactivity_watcher crashed: %r", ex)


# ──────────────────────────────────────────────────────────────────────────────
# BuddyApp
# ──────────────────────────────────────────────────────────────────────────────


class BuddyApp(App):
    """Top-level Textual application."""

    TITLE = "Buddy"
    CSS = f"""
    Screen {{
        background: {_BG};
    }}
    """
    BINDINGS = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._iq = InputQueue()
        self._sys_state = SystemState()
        self._state_lock = threading.Lock()
        self._interrupt_event = threading.Event()
        self._stt: Any = None
        self._main_screen: Optional[MainScreen] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.push_screen(SplashScreen())

    async def _async_on_boot_done(self, state: Any) -> None:
        """Async handler called from BootScreen when bootstrap() finishes."""
        if state is None:
            logger.error("bootstrap returned None — exiting")
            self.exit()
            return

        mm = getattr(getattr(state, "artifacts", None), "memory_manager", None)
        self._main_screen = MainScreen(
            state=state,
            input_queue=self._iq,
            sys_state=self._sys_state,
            state_lock=self._state_lock,
            interrupt_event=self._interrupt_event,
            memory_manager=mm,
        )
        # switch_screen must be awaited in Textual async context
        await self.switch_screen(self._main_screen)
        asyncio.create_task(self._start_stt(state))

    async def _start_stt(self, state: Any) -> None:
        try:
            from buddy.ui.stt import SpeechToText

            cfg = getattr(state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            voice_cfg = buddy_cfg.get("voice", {}) or {}
            runtime = cfg.get("runtime", {}) or {}
            whisper_dir = os.path.join(
                (runtime.get("fs") or {}).get("models_dir", "."), "whisper"
            )

            if not bool(voice_cfg.get("enabled", True)):
                if self._main_screen:
                    self._main_screen.query_one(StatusBar).set_hint(
                        f"[dim {_DIM}]🎧 voice disabled[/]"
                    )
                return

            mic_idx = voice_cfg.get("microphone_index", -1)
            loop = self._loop or asyncio.get_running_loop()

            def on_text(text: str) -> None:
                if self._main_screen and loop:
                    loop.call_soon_threadsafe(self._main_screen.handle_voice_text, text)
                    loop.call_soon_threadsafe(self._main_screen.set_mic_idle)

            def on_interrupt() -> None:
                if self._main_screen and loop:
                    loop.call_soon_threadsafe(self._main_screen.set_mic_active)

            def on_speech_start() -> None:
                if self._main_screen and loop:
                    loop.call_soon_threadsafe(self._main_screen.set_mic_active)

            self._stt = SpeechToText(
                whisper_model_size=str(voice_cfg.get("whisper_model_size", "base")),
                whisper_download_root=str(whisper_dir),
                calibration_sec=float(voice_cfg.get("calibration_sec", 0.0)),
                language=str(voice_cfg.get("language", "en")),
                microphone_index=mic_idx if mic_idx >= 0 else None,
                silence_timeout=float(voice_cfg.get("silence_timeout", 1.4)),
                on_text=on_text,
                on_interrupt=on_interrupt,
                on_speech_start=on_speech_start,
                beam_size=int(voice_cfg.get("beam_size", 5)),
                whisper_vad_filter=bool(voice_cfg.get("whisper_vad_filter", True)),
                speech_trigger_mult=float(voice_cfg.get("speech_trigger_mult", 3.0)),
                use_silero_vad=bool(voice_cfg.get("use_silero_vad", False)),
                enable_beep=bool(voice_cfg.get("enable_beep", True)),
                debug=bool(voice_cfg.get("debug", False)),
            )
            self._stt.start()

            if self._main_screen:
                self._main_screen.set_stt_engine(self._stt)
                self._main_screen.query_one(StatusBar).set_hint(
                    f"[{_GREEN}]🎧 voice enabled[/]"
                )

        except Exception as ex:
            logger.exception("stt: failed to start: %r", ex)
            if self._main_screen:
                self._main_screen.query_one(StatusBar).set_hint(
                    f"[{_RED}]⚠ stt failed: {ex}[/]"
                )

    def on_unmount(self) -> None:
        if self._stt is not None:
            try:
                self._stt.stop()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def _prewarm_whisper_before_textual() -> None:
    """
    Load WhisperModel into _MODEL_CACHE before Textual claims terminal FDs.

    CTranslate2 (faster-whisper backend) spawns subprocesses during model
    initialisation. Textual's terminal driver opens/manipulates pseudo-terminal
    FDs that become invalid for subprocess inheritance. Pre-warming here caches
    the model with clean FDs so that SpeechToText.__init__ gets a cache hit
    (no subprocess spawning at all) when called later inside Textual.

    The _MODEL_CACHE key is  "{size}|{download_root}|{compute_type}".
    We must use the exact same size and download_root that _start_stt() will
    use, otherwise we get a cache miss and the problem recurs.
    """
    try:
        import sys as _sys

        if _sys.version_info >= (3, 11):
            import tomllib as _toml
        else:
            import tomli as _toml  # type: ignore

        # Mirror boot.py's _runtime_root() / _layout() logic so paths match.
        _root = (
            (
                (
                    Path(
                        os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA", "")
                    )
                    / "Buddy"
                )
                if (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA"))
                else (Path.home() / "Buddy")
            )
            if os.name == "nt"
            else Path.home() / ".buddy"
        )
        _config_dir = _root / "config"
        _models_dir = _root / "data" / "models"

        # Try user config first, then fall back to package default.
        _cfg_path = _config_dir / "buddy.toml"
        if not _cfg_path.exists():
            _cfg_path = Path(__file__).parent.parent / "config" / "buddy.toml"
        if not _cfg_path.exists():
            return

        with open(_cfg_path, "rb") as _f:
            _raw = _toml.load(_f)

        _buddy_cfg = _raw.get("buddy", _raw) if isinstance(_raw, dict) else {}
        _feat_cfg = (
            _buddy_cfg.get("features", {}) if isinstance(_buddy_cfg, dict) else {}
        )
        if not _feat_cfg.get("enable_audio_stt", False):
            return  # STT disabled — nothing to pre-warm

        _voice_cfg = _buddy_cfg.get("voice", {}) if isinstance(_buddy_cfg, dict) else {}
        _size = str(_voice_cfg.get("whisper_model_size", "base"))
        _compute_type = str(_voice_cfg.get("compute_type", ""))
        _whisper_dir = str(_models_dir / "whisper")

        from buddy.ui.stt import _load_whisper  # noqa: PLC0415

        logger.info("STT: pre-warming WhisperModel '%s' before Textual starts", _size)
        _load_whisper(_size, _whisper_dir, _compute_type)
        logger.info("STT: WhisperModel cached — Textual init will be a cache hit")

    except Exception as _ex:
        logger.warning("STT pre-warm skipped (non-fatal): %r", _ex)


def run_textual() -> None:
    """
    Create and run the Textual app.
    Bootstrap happens inside BootScreen; no state arg needed at startup.
    Called from main.py.
    """
    import traceback as _tb

    _log_path = Path.home() / ".buddy" / "logs" / "textual_app.log"

    # Textual writes its own crash report here — readable without running the app.
    _textual_log = Path.home() / ".buddy" / "logs" / "textual_crash.log"

    # Point Textual's internal debug log at our log dir
    os.environ.setdefault("TEXTUAL_LOG", str(_textual_log))

    _prewarm_whisper_before_textual()
    app = BuddyApp()
    try:
        app.run()
    except Exception:
        _err = _tb.format_exc()
        logger.exception("BuddyApp crashed")
        try:
            with open(_log_path, "a", encoding="utf-8") as _f:
                _f.write(f"\n{'='*60}\nBuddyApp crash:\n{_err}\n")
        except Exception:
            pass
        raise
