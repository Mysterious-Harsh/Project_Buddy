# buddy/ui/widgets.py
#
# All Textual widget classes and supporting helpers.
# Screens and the app live in textual_app.py.
# Animation frame data lives in face_frames.py.

from __future__ import annotations

import asyncio
import locale as _locale
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import random
import threading
import time
from typing import Any, Callable, List, Optional

from rich.align import Align
from rich.panel import Panel
from rich.text import Text
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.widget import Widget
from textual.message import Message
from textual.widgets import Static, TextArea

from buddy.logger.logger import get_logger
from buddy.ui.boot_ui import (
    _supports_unicode,
    AURORA,
    _buddy_title_lines,
    _logo_row_code,
)
from buddy.ui.face_frames import (
    _THINKING_FRAMES,
    _WAITING_FRAMES,
    _WORKING_FRAMES,
    _BOOT_FACE_FRAMES,
    _SLEEP_FACE_FRAMES,
    _SPLASH_FACE_FRAMES,
)

logger = get_logger("widgets")

# ──────────────────────────────────────────────────────────────────────────────
# Unicode capability (tested once at import time)
#
# boot_ui._supports_unicode() checks sys.stdout.encoding, which Textual
# replaces with its own internal buffer — making it always return False
# inside a running Textual app. We instead check the locale/env so the
# detection works correctly regardless of stdout redirection.
# ──────────────────────────────────────────────────────────────────────────────


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
    """Build Rich-markup logo from boot_ui._buddy_title_lines()."""
    rows = _buddy_title_lines()  # unicode block-art OR ASCII fallback
    if _USE_UNICODE:
        return "\n".join(
            f"[bold {_LOGO_ROW_HEX[i % len(_LOGO_ROW_HEX)]}]{row}[/]"
            for i, row in enumerate(rows)
        )
    return "\n".join(f"[bold cyan]{row}[/]" for row in rows)


def _banner_markup(*, compact: bool = False) -> str:
    logo = _logo_markup()
    tagline = f"[dim {_VIOLET}]Cognitive AI  ·  Offline-first  ·  Memory-driven[/]"
    if compact:
        return logo + "\n" + tagline
    hint = f"[dim {_DIM}]type or speak  ·  ESC to interrupt  ·  F2 mute  ·  F3 sleep[/]"
    return logo + "\n" + tagline + "\n" + hint


# ──────────────────────────────────────────────────────────────────────────────
# SystemState, VoiceCmd, helpers
# ──────────────────────────────────────────────────────────────────────────────


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

        def _safe_put() -> None:
            try:
                self._q.put_nowait(text)
            except asyncio.QueueFull:
                logger.debug("InputQueue: voice dropped (queue full)")

        loop.call_soon_threadsafe(_safe_put)

    async def get(self) -> str:
        return await self._q.get()

    def push_sentinel(self, sentinel: str, loop: asyncio.AbstractEventLoop) -> None:
        loop.call_soon_threadsafe(self._q.put_nowait, sentinel)


# ──────────────────────────────────────────────────────────────────────────────
# SplashView — full-screen logo + face animation (used by SplashScreen)
# ──────────────────────────────────────────────────────────────────────────────


class SplashView(Static):
    """
    Full-screen splash: BUDDY logo centred vertically + animated face below.
    Shown for ~2.5 s before transitioning to BootScreen.
    """

    DEFAULT_CSS = f"""
    SplashView {{
        height: 1fr;
        background: {_BG};
        content-align: center middle;
        text-align: center;
    }}
    """

    def on_mount(self) -> None:
        self._frame = 0
        self._cached_logo = _logo_markup()  # build once — never changes
        self._face_timer = self.set_interval(0.12, self._tick)
        self._redraw()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_SPLASH_FACE_FRAMES)
        self._redraw()

    def _redraw(self) -> None:
        tagline = f"[dim {_VIOLET}]Cognitive AI  ·  Offline-first  ·  Memory-driven[/]"
        face = _SPLASH_FACE_FRAMES[self._frame]
        face_line = f"[{_CYAN}]{face}[/]"
        hint = f"[dim {_DIM}]starting up…[/]"
        self.update("\n".join([self._cached_logo, "", tagline, "", face_line, "", hint]))


# ──────────────────────────────────────────────────────────────────────────────
# Boot screen widgets
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
            pass

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
        self._pending: Optional[BootLogLine] = None

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
        self.update(f"[{_CYAN}]{face}[/]  [{_DIM}]booting…[/]")


# ──────────────────────────────────────────────────────────────────────────────
# Main screen widgets
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
        self._turn = 0
        self._last_turn_ms: Optional[int] = None
        self._mem_flash = 0
        self._mem_short = 0
        self._mem_long = 0
        self._last_memory = ""
        self._user_name = "—"
        self._llm_label = "—"
        self._n_ctx = "—"
        self._hw_line = "—"
        self._web = "—"
        self._voice_enabled = False
        self._n_gpu_layers = "—"
        self._kv_cache = "—"
        self._flash_attn = "—"

    def on_mount(self) -> None:
        self._load_static()
        self.set_interval(1.0, self._tick)
        self._redraw()
        asyncio.create_task(self._async_fetch_server_props())

    def _load_static(self) -> None:
        try:
            cfg = getattr(self._state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            runtime = cfg.get("runtime", {}) or {}
            fs = runtime.get("fs", {}) or {}

            op_path = fs.get("os_profile_file")
            if op_path and Path(op_path).exists():
                import json as _json

                with open(op_path, "r", encoding="utf-8") as _f:
                    op = _json.load(_f)
                hw = op.get("hardware") or {}
                self._user_name = (op.get("identity") or {}).get("preferred_name") or "—"
                ram_gb = (hw.get("ram") or {}).get("total_gb", "?")
                cores = (hw.get("cpu") or {}).get("logical_cores", "?")
                gpu = hw.get("gpu") or {}
                gpu_name = (gpu.get("name") or "")[:14]
                vram = gpu.get("vram_gb")
                hw_parts = [f"{ram_gb}GB", f"{cores}c"]
                if gpu_name:
                    hw_parts.append(f"{gpu_name}" + (f"·{vram}GB" if vram else ""))
                self._hw_line = " · ".join(hw_parts)

            llama_cfg = buddy_cfg.get("llama", {}) or {}
            model = (
                llama_cfg.get("model_gguf", "")
                or llama_cfg.get("model_name", "")
                or "—"
            )
            self._llm_label = model.replace(".gguf", "")[:22]

            cb = getattr(self._state, "context_budget", None)
            if cb:
                self._n_ctx = str(getattr(cb, "n_ctx", "—"))

            # llama_props is stored by boot.py — contains n_gpu_layers, kv_cache,
            # flash_attn parsed from the launch command (not available via /props).
            lp = runtime.get("llama_props") or {}
            if lp.get("model_file"):
                self._llm_label = lp["model_file"].replace(".gguf", "")[:28]
            if lp.get("n_ctx"):
                self._n_ctx = str(lp["n_ctx"])
            if lp.get("n_gpu_layers") is not None:
                self._n_gpu_layers = str(lp["n_gpu_layers"])
            if lp.get("kv_cache"):
                self._kv_cache = str(lp["kv_cache"])
            if lp.get("flash_attn"):
                self._flash_attn = str(lp["flash_attn"])

            web_cfg = buddy_cfg.get("web_search", {}) or {}
            self._web = str(web_cfg.get("engine", "duckduckgo"))
            feat_cfg = buddy_cfg.get("features", {}) or {}
            self._voice_enabled = bool(feat_cfg.get("enable_audio_stt", False))
        except Exception:
            pass

    async def _async_fetch_server_props(self) -> None:
        try:
            cfg = getattr(self._state, "config", {}) or {}
            runtime = cfg.get("runtime", {}) or {}
            llama = runtime.get("llama", {}) or {}
            base_url = (llama.get("base_url") or "http://127.0.0.1:8080").rstrip("/")

            def _fetch() -> dict:
                import requests as _req  # noqa: PLC0415

                out: dict = {}
                try:
                    r = _req.get(base_url + "/props", timeout=(1.5, 5.0))
                    if r.status_code == 200:
                        p = r.json()
                        raw = p.get("model_path") or p.get("model") or ""
                        if raw:
                            out["model_file"] = Path(raw).name
                        n_ctx = p.get("n_ctx")
                        if n_ctx is not None:
                            out["n_ctx"] = str(n_ctx)
                        n_gpu = p.get("n_gpu_layers")
                        if n_gpu is not None:
                            out["n_gpu_layers"] = str(n_gpu)
                        kv_k = p.get("cache_type_k") or p.get("kv_cache_type")
                        kv_v = p.get("cache_type_v")
                        if kv_k and kv_v and kv_k != kv_v:
                            out["kv_cache"] = f"{kv_k}/{kv_v}"
                        elif kv_k:
                            out["kv_cache"] = str(kv_k)
                        fa = p.get("flash_attn")
                        if fa is not None:
                            out["flash_attn"] = "on" if fa else "off"
                except Exception:
                    pass
                try:
                    r = _req.get(base_url + "/v1/models", timeout=(1.5, 3.0))
                    if r.status_code == 200:
                        data = r.json().get("data") or []
                        if data:
                            mid = data[0].get("id") or ""
                            if mid and not out.get("model_file"):
                                out["model_file"] = mid
                except Exception:
                    pass
                return out

            props = await asyncio.to_thread(_fetch)

            if props.get("model_file"):
                self._llm_label = props["model_file"].replace(".gguf", "")[:28]
            if props.get("n_ctx"):
                self._n_ctx = props["n_ctx"]
            if props.get("n_gpu_layers"):
                self._n_gpu_layers = props["n_gpu_layers"]
            if props.get("kv_cache"):
                self._kv_cache = props["kv_cache"]
            if props.get("flash_attn"):
                self._flash_attn = props["flash_attn"]

            self._redraw()
        except Exception:
            pass

    def _tick(self) -> None:
        self._redraw()

    def _redraw(self) -> None:
        try:
            self.update(self._build())
        except Exception:
            logger.exception("InfoPane._redraw failed")

    def _build(self) -> str:
        now = time.localtime()
        date_s = time.strftime("%a %b %d", now)
        time_s = time.strftime("%H:%M:%S", now)

        elapsed = int(time.monotonic() - self._session_start)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        uptime = f"{h:02d}:{m:02d}:{s:02d}"

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

        if not self._voice_enabled:
            voice_s = f"[{_DIM}]off[/]"
        elif voice_muted:
            voice_s = f"[{_YELLOW}]muted[/]"
        else:
            voice_s = f"[{_GREEN}]on[/]"

        lat_s = (
            f"[{_DIM}]{self._last_turn_ms}ms[/]"
            if self._last_turn_ms is not None
            else f"[{_DIM}]—[/]"
        )

        mem_s = (
            f"[{_CYAN}]{self._mem_flash}[/]"
            f"[{_DIM}]·[/]"
            f"[{_BLUE}]{self._mem_short}[/]"
            f"[{_DIM}]·[/]"
            f"[{_VIOLET}]{self._mem_long}[/]"
        )

        last_mem = (self._last_memory or "").strip()
        if len(last_mem) > 50:
            last_mem = last_mem[:47] + "…"
        last_mem_s = f"[{_DIM}]{last_mem or '—'}[/]"

        try:
            _sep_w = max(20, self.size.width - 4)
        except Exception:
            _sep_w = 44
        D = f"[{_DIM}]{'─' * _sep_w}[/]"

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
                f"[{_DIM}]     GPU {self._n_gpu_layers}L"
                f"  ·  KV {self._kv_cache}"
                f"  ·  FA {self._flash_attn}[/]"
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

    _SHORTCUTS = f"[dim {_DIM}]ESC:stop  F2:mute  F3:sleep  Ctrl+C×2:quit[/]"

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
        if not self.has_class("visible"):
            return
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


# ──────────────────────────────────────────────────────────────────────────────
# Chat widgets
# ──────────────────────────────────────────────────────────────────────────────


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

        _longest = max((len(line) for line in text.splitlines()), default=0)
        _w = min(72, max(32, _longest + 8))

        if self._kind == "user":
            panel = Panel(
                Text(display, style=_WHITE),
                title=f"[{_CYAN}]▌ You[/]",
                title_align="left",
                border_style=_CYAN,
                width=_w,
                padding=(0, 1),
            )
            self.update(panel)
        else:
            panel = Panel(
                Text(display, style=_WHITE),
                title=f"[{_VIOLET}]◈ Buddy[/]",
                title_align="left",
                border_style=_VIOLET,
                width=_w,
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
        if not self.display:
            return
        self._face_idx = (self._face_idx + 1) % len(_SLEEP_FACE_FRAMES)
        self.refresh()

    def _tick_stars(self) -> None:
        if not self.display:
            return
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
        try:
            w = max(50, self.size.width)
            h = max(12, self.size.height)
        except Exception:
            w, h = 60, 20

        star_h = max(6, h - 7)
        grid = [[" "] * w for _ in range(star_h)]

        for s in self._stars:
            sx = int(s["x"] * (w - 1))
            sy = int(s["y"] * (star_h - 1))
            grid[sy][sx] = s["ch"]

        face = _SLEEP_FACE_FRAMES[self._face_idx]
        cy = star_h // 2
        cx = (w - len(face)) // 2
        for i, ch in enumerate(face):
            if 0 <= cx + i < w:
                grid[cy][cx + i] = ch

        hdr = "· · ·  rest mode  · · ·" if _USE_UNICODE else "- - -  rest mode  - - -"
        hx = max(0, (w - len(hdr)) // 2)
        grid[1] = list(" " * hx + hdr)[:w] + [" "] * max(0, w - hx - len(hdr))

        star_lines = ["".join(row) for row in grid]

        elapsed = max(0.0, time.monotonic() - self.sleep_start_ts)
        em, es = int(elapsed // 60), int(elapsed % 60)
        total = max(1, self.stats_flash + self.stats_short + self.stats_long)
        bar_w = min(20, w // 4)

        sep = "─" * min(w - 4, 44) if _USE_UNICODE else "-" * min(w - 4, 44)
        sep_line = " " * max(0, (w - len(sep)) // 2) + sep

        stats = [
            sep_line,
            f"  flash  {self._make_bar(self.stats_flash, total, bar_w)}  {self.stats_flash}",
            f"  short  {self._make_bar(self.stats_short, total, bar_w)}  {self.stats_short}",
            f"  long   {self._make_bar(self.stats_long,  total, bar_w)}  {self.stats_long}",
            f"  sleeping {em:02d}:{es:02d}",
            f"  [ F3 to wake  ·  or say 'wake up' ]",
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
        height: 5;
        padding: 2 0;
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


class BuddyInput(TextArea):
    """
    Multiline text input.

    Enter       → submit (posts BuddyInput.Submitted, clears the field)
    Shift+Enter → newline
    Escape      → interrupt
    Ctrl+C      → quit request (forwarded to screen)
    """

    class Submitted(Message):
        """Posted when the user presses Enter to submit."""
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    DEFAULT_CSS = f"""
    BuddyInput {{
        width: 1fr;
        height: 5;
        border: tall {_CYAN};
        background: {_BG};
        color: {_WHITE};
        padding: 0 0;
    }}
    BuddyInput:focus {{
        border: tall {_CYAN};
        background: {_BG};
    }}
    BuddyInput > .text-area--cursor {{
        background: {_CYAN};
        color: {_BG};
    }}
    BuddyInput > .text-area--selection {{
        background: {_BLUE};
    }}
    """

    def __init__(self, on_escape: Callable[[], None], **kwargs: Any) -> None:
        super().__init__(
            show_line_numbers=False,
            soft_wrap=True,
            tab_behavior="focus",
            **kwargs,
        )
        self._on_escape = on_escape

    def _on_key(self, event: Any) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = (self.text or "").strip()
            if text:
                self.post_message(BuddyInput.Submitted(text))
                self.clear()
        elif event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
        elif event.key == "escape":
            event.prevent_default()
            event.stop()
            self._on_escape()
        elif event.key == "ctrl+c":
            event.prevent_default()
            event.stop()
            screen = self.screen
            if hasattr(screen, "action_quit_request"):
                screen.action_quit_request()  # type: ignore


class InputBar(Horizontal):
    """Bottom input row: MicIndicator + BuddyInput."""

    DEFAULT_CSS = f"""
    InputBar {{
        height: 5;
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
    """

    DEFAULT_CSS = f"""
    BottomSection {{
        dock: bottom;
        height: auto;
        background: {_BG};
    }}
    """
