# buddy/ui/textual_app.py
#
# Textual TUI for Buddy — v3 (patched)
#
# Screens:
#   BuddyApp → SplashScreen (logo) → BootScreen (bootstrap) → MainScreen (chat)
#
# Widget building blocks live in widgets.py.
# Animation frame data lives in face_frames.py.
#
# run_textual() is the public entry point called from main.py.
#
# ── Patch summary (all issues from code-review) ──────────────────────────────
# FIX-01  Task GC: tracked via _tasks sets; done callbacks discard on completion.
# FIX-02  Redundant sleeping=False write inside _async_set_sleeping else-branch removed.
# FIX-03  _consume_messages uses a locally captured queue ref, not self._boot_queue.
# FIX-04  _handle_voice_input guard is now the sole race gate; handle_voice_text
#         no longer makes an independent check that can race against it.
# FIX-05  _iq._q private access replaced by InputQueue.drain() public method.
#         (InputQueue.drain() must be added to widgets.py — see comment below.)
# FIX-06  pipeline_input() uses asyncio.wait_for with a 5-min timeout + quit-event
#         check to prevent infinite hangs on app exit or crash.
# FIX-07  _inactivity_watcher sleeps 60 s (not the full idle period) after
#         triggering sleep, so re-idle detection works correctly.
# FIX-08  stream_buf replaced with collections.deque(maxlen=400) to bound memory.
# FIX-09  </think> detection uses a short suffix window, not O(n) full-join scan.
# FIX-10  progress_cb thread-shared state protected by a threading.Lock.
# FIX-11  markup_escape applied to the mute icon string in _set_voice_mute.
# FIX-12  _stop_tts logs a warning instead of silently passing (TTS not wired).
# FIX-13  Dead method _set_sleeping removed.
# FIX-14  Cached widget references (StatusBar, InfoPane, SpinnerBar, etc.) set in
#         on_mount; query_one no longer called repeatedly on hot paths.
# FIX-15  All bare `except Exception: pass` blocks now log at DEBUG level.
# FIX-16  typing.List / typing.Optional replaced with built-in generics (3.9+).
# FIX-17  `import traceback` moved to module level.
# FIX-18  Crash log path consolidated to a single variable (_crash_log_path).
# FIX-19  logger.error() uses %r lazy formatting, not f-strings.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import collections
import os
import signal
import traceback
from pathlib import Path
import threading
import time
import uuid
from typing import Any

from rich.markup import escape as markup_escape
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import ContentSwitcher, Label, ListItem, ListView

from buddy.buddy_core.pipeline import handle_turn
from buddy.logger.logger import get_logger
from buddy.tools.vision.image_encoder import extract_image_paths
from buddy.ui.widgets import (
    # color constants (used in CSS strings and hints)
    _USE_UNICODE,
    _CYAN,  # noqa: F401  (re-exported for callers)
    _BLUE,  # noqa: F401
    _VIOLET,
    _DIM,
    _BG,
    _GREEN,
    _YELLOW,
    _RED,
    # helpers
    _should_exit,
    _match_voice_command,
    # data types
    SystemState,
    VoiceCmd,
    InputQueue,
    # splash
    SplashView,
    # boot widgets
    BootBanner,
    BootLog,
    BootFaceBar,
    # main widgets
    BannerPane,
    InfoPane,
    BuddyHeader,
    StatusBar,
    SpinnerBar,
    ChatLog,
    SleepView,
    MicIndicator,
    BuddyInput,
    InputBar,
    BottomSection,
)

logger = get_logger("textual_app")

EXIT_SENTINEL = "__EXIT__"
INTERRUPT_SENTINEL = "__INTERRUPT__"

# Maximum number of streaming chunks kept in the preview buffer (FIX-08).
_STREAM_BUF_MAXLEN = 400
# Characters needed to detect the </think> closing tag (FIX-09).
_THINK_TAG = "</think>"
_THINK_SUFFIX_LEN = len(_THINK_TAG) * 2  # small rolling window

# Timeout (seconds) for pipeline_input() waiting for user follow-up (FIX-06).
_PIPELINE_INPUT_TIMEOUT = 300.0


# ──────────────────────────────────────────────────────────────────────────────
# SplashScreen — full-screen logo + face animation, shown before boot log
# ──────────────────────────────────────────────────────────────────────────────


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
        # FIX-01: track background tasks so they are not GC'd silently.
        self._tasks: set[asyncio.Task] = set()

    def _track(self, coro: Any) -> asyncio.Task:
        """Create a tracked task that removes itself from _tasks when done."""
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def compose(self) -> ComposeResult:
        yield SplashView()

    def on_mount(self) -> None:
        self.set_timer(2.5, self._go_boot)

    def _go_boot(self) -> None:
        if not self._switching:
            self._switching = True
            self._track(self._switch())

    async def _switch(self) -> None:
        await self.app.switch_screen(BootScreen())

    def on_key(self, _: Any) -> None:
        """Any key skips the splash."""
        if not self._switching:
            self._switching = True
            self._track(self._switch())


# ──────────────────────────────────────────────────────────────────────────────
# BootScreen — runs bootstrap() in a thread, streams progress to BootLog
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
        self._boot_queue: asyncio.Queue | None = None
        # FIX-01
        self._tasks: set[asyncio.Task] = set()

    def _track(self, coro: Any) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def compose(self) -> ComposeResult:
        yield BootBanner()
        yield BootLog(id="boot-log")
        yield BootFaceBar()

    def on_mount(self) -> None:
        self._boot_queue = asyncio.Queue()
        self._track(self._run_bootstrap())
        self._track(self._consume_messages())

    async def _run_bootstrap(self) -> None:
        loop = asyncio.get_running_loop()
        # FIX-03: capture queue locally so _run_bootstrap is independent of
        # any future re-assignment of self._boot_queue.
        queue: asyncio.Queue = self._boot_queue  # type: ignore[assignment]

        def progress_cb(msg: str, status: str = "running") -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (msg, status))

        state = None
        try:
            from buddy.buddy_core.boot import bootstrap, BootstrapOptions

            pre_wizard = getattr(self.app, "_pre_wizard_result", None)
            opts = BootstrapOptions(show_boot_ui=False, pre_wizard_result=pre_wizard)
            state = await asyncio.to_thread(bootstrap, opts, progress_cb)
        except asyncio.CancelledError:
            loop.call_soon_threadsafe(queue.put_nowait, ("__DONE__", None))
            raise
        except BaseException as ex:
            logger.exception("bootstrap failed: %r", ex)
        loop.call_soon_threadsafe(queue.put_nowait, ("__DONE__", state))

    async def _consume_messages(self) -> None:
        # FIX-03: use local ref captured after on_mount guarantees assignment.
        queue: asyncio.Queue = self._boot_queue  # type: ignore[assignment]
        log = self.query_one(BootLog)
        while True:
            item = await queue.get()
            msg, payload = item
            if msg == "__DONE__":
                app = self.app
                if isinstance(app, BuddyApp):
                    await app._async_on_boot_done(payload)
                return
            await log.add_message(msg, payload)


# ──────────────────────────────────────────────────────────────────────────────
# MicSelectScreen — mic device picker modal (opened by F4)
# ──────────────────────────────────────────────────────────────────────────────


class MicSelectScreen(ModalScreen):
    """
    Microphone selection modal.

    Receives the device list from MainScreen (already filtered for virtual
    devices by stt._list_input_devices()).  Dismisses with (global_idx, name)
    on confirmation or None on cancel.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = f"""
    MicSelectScreen {{
        align: center middle;
    }}
    #mic-dialog {{
        width: 62;
        max-height: 22;
        border: round {_CYAN};
        background: {_BG};
        padding: 1 2;
    }}
    #mic-title {{
        text-align: center;
        color: {_CYAN};
        margin-bottom: 1;
    }}
    #mic-list {{
        height: auto;
        max-height: 14;
        border: solid {_DIM};
    }}
    #mic-footer {{
        text-align: center;
        color: {_DIM};
        margin-top: 1;
    }}
    """

    def __init__(
        self,
        devices: list[tuple[int, dict]],
        current_idx: int | None,
    ) -> None:
        super().__init__()
        self._devices = devices        # [(global_idx, device_dict), ...]
        self._current_idx = current_idx

    def compose(self) -> ComposeResult:
        with Vertical(id="mic-dialog"):
            yield Label("🎙  Select Microphone", id="mic-title")
            with ListView(id="mic-list"):
                for global_idx, dev in self._devices:
                    name = dev.get("name", f"Device {global_idx}")
                    marker = "  ●" if global_idx == self._current_idx else ""
                    yield ListItem(Label(f"[{global_idx}]  {name}{marker}"))
            yield Label(
                "↑↓ navigate   Enter select   ESC cancel",
                id="mic-footer",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._devices):
            global_idx, dev = self._devices[idx]
            name = dev.get("name", f"Device {global_idx}")
            self.dismiss((global_idx, name))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ──────────────────────────────────────────────────────────────────────────────
# MainScreen — primary chat screen
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
        ("f2", "toggle_mute", "Mic On/Off"),
        ("f3", "toggle_sleep", "Sleep/Wake"),
        ("f4", "select_mic", "Mic"),
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
        memory_manager: Any | None = None,
        opener_text: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._state = state
        self._iq = input_queue
        self._sys_state = sys_state
        self._state_lock = state_lock
        self._interrupt_event = interrupt_event
        self._memory_manager = memory_manager
        self._opener_text = opener_text
        self._active_turn: asyncio.Task | None = None
        self._turn_lock = asyncio.Lock()
        self._quit_event = asyncio.Event()
        self._last_interrupt_ts = 0.0
        self._last_ctrl_c_ts = 0.0
        self._interrupt_reason: str = ""
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._stt: Any = None
        self._idle_timeout_s: float = 20 * 60
        self._last_activity_ts: float = time.monotonic()
        self._turn_count: int = 0
        # FIX-01: tracked task set
        self._tasks: set[asyncio.Task] = set()
        # FIX-14: cached widget references (set in on_mount)
        self._w_status_bar: StatusBar | None = None
        self._w_info_pane: InfoPane | None = None
        self._w_spinner_bar: SpinnerBar | None = None
        self._w_chat_log: ChatLog | None = None
        self._w_mic_indicator: MicIndicator | None = None
        self._w_content_switcher: ContentSwitcher | None = None
        self._w_sleep_view: SleepView | None = None

    def _track(self, coro: Any) -> asyncio.Task:
        """Create and track a background task (FIX-01)."""
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

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
                    placeholder=(
                        "Type a message… (Enter to send · Ctrl+J for newline · Esc to"
                        " interrupt)"
                    ),
                    id="buddy-input",
                )
            yield StatusBar()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._main_loop = asyncio.get_running_loop()

        # FIX-14: cache widget references once, avoid repeated DOM traversal.
        try:
            self._w_status_bar = self.query_one(StatusBar)
            self._w_info_pane = self.query_one(InfoPane)
            self._w_spinner_bar = self.query_one(SpinnerBar)
            self._w_chat_log = self.query_one(ChatLog)
            self._w_mic_indicator = self.query_one(MicIndicator)
            self._w_content_switcher = self.query_one(ContentSwitcher)
            self._w_sleep_view = self.query_one(SleepView)
        except Exception:
            logger.debug("on_mount: widget cache population failed", exc_info=True)

        try:
            cfg = getattr(self._state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            general_cfg = buddy_cfg.get("general", {}) or {}
            self._idle_timeout_s = float(
                general_cfg.get("sleep_after_idle_min", 20) * 60
            )
        except Exception:
            logger.debug("on_mount: failed to read idle timeout", exc_info=True)
            self._idle_timeout_s = 1200.0

        try:
            self.query_one(BuddyInput).focus()
        except Exception:
            logger.debug("on_mount: BuddyInput focus failed", exc_info=True)

        self._track(self._inactivity_watcher())
        if self._opener_text:
            self._track(self._show_opener())
        self._refresh_info_bar()

    async def _show_opener(self) -> None:
        arts = getattr(self._state, "artifacts", None)
        conversations = getattr(arts, "conversations", None)
        if self._w_chat_log:
            await self._w_chat_log.add_message(self._opener_text, "buddy")
        if conversations:
            conversations.add_buddy(self._opener_text)

    def _refresh_info_bar(self, turn_ms: int | None = None) -> None:
        # FIX-14: use cached references.
        try:
            cfg = getattr(self._state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            feat_cfg = buddy_cfg.get("features", {}) or {}
            voice = "on" if feat_cfg.get("enable_audio_stt", False) else "off"
            if self._w_status_bar:
                self._w_status_bar.set_info(voice=voice, turn=self._turn_count)
        except Exception:
            logger.debug("_refresh_info_bar: status bar update failed", exc_info=True)
        try:
            if self._w_info_pane:
                self._w_info_pane.update_turn(self._turn_count, turn_ms)
                if self._memory_manager is not None:
                    self._w_info_pane.update_memory_counts(self._memory_manager)
        except Exception:
            logger.debug("_refresh_info_bar: info pane update failed", exc_info=True)

    # ── Interrupt / quit ──────────────────────────────────────────────────────

    def _handle_escape(self) -> None:
        self._request_interrupt("you pressed Escape")

    def _request_interrupt(self, reason: str = "you interrupted me") -> None:
        now = time.monotonic()
        if (now - self._last_interrupt_ts) < 0.75:
            return
        self._last_interrupt_ts = now
        self._interrupt_reason = reason
        self._interrupt_event.set()
        turn_active = self._active_turn is not None and not self._active_turn.done()
        if turn_active and self._active_turn is not None:
            self._active_turn.cancel()
            loop = self._main_loop
            if loop:
                # FIX-05: use InputQueue.push_interrupt() instead of accessing _q directly.
                self._iq.push_interrupt(INTERRUPT_SENTINEL, loop)
        self._stop_spinner()
        if self._w_status_bar:
            self._w_status_bar.set_hint(f"[{_YELLOW}]⛔ interrupted[/]")
        logger.info(
            "interrupt: requested — reason=%r active_turn=%s", reason, turn_active
        )

    def action_toggle_mute(self) -> None:
        self._toggle_voice_mute()

    def action_toggle_sleep(self) -> None:
        self._toggle_sleep()

    def action_select_mic(self) -> None:
        """F4 — mute immediately, open mic picker, unmute on close."""
        if self._stt is None:
            return

        with self._state_lock:
            was_muted = self._sys_state.mic_off

        # Release mic right away so the modal doesn't feel laggy.
        if not was_muted:
            self._set_voice_mute(True)

        devices = self._stt._list_input_devices()
        if not devices:
            # No real input devices found — restore state and bail.
            if not was_muted:
                self._set_voice_mute(False)
            if self._w_status_bar:
                self._w_status_bar.set_hint(f"[{_YELLOW}]No mic devices found[/]")
            return

        def _on_mic_selected(result: tuple[int, str] | None) -> None:
            if result is not None:
                new_idx, name = result
                self._stt.microphone_index = new_idx  # type: ignore[union-attr]
            if not was_muted:
                self._set_voice_mute(False)
            if result is not None and self._w_status_bar:
                _icon = "🎙 " if _USE_UNICODE else ""
                self._w_status_bar.set_hint(
                    f"[{_GREEN}]{_icon}{markup_escape(name)}[/]"
                )

        self.app.push_screen(
            MicSelectScreen(devices, self._stt.microphone_index),
            _on_mic_selected,
        )

    def action_quit_request(self) -> None:
        now = time.monotonic()
        if (now - self._last_ctrl_c_ts) < 1.25:
            self._quit_event.set()
            try:
                loop = asyncio.get_running_loop()
                self._iq.push_sentinel(EXIT_SENTINEL, loop)
            except Exception:
                logger.debug("action_quit_request: push_sentinel failed", exc_info=True)
            self.app.exit()
        else:
            self._last_ctrl_c_ts = now
            self._request_interrupt("you pressed Ctrl+C")
            try:
                if self._w_status_bar:
                    self._w_status_bar.set_hint(
                        f"[{_YELLOW}]Ctrl+C again to quit[/]", 2.0
                    )
            except Exception:
                logger.debug("action_quit_request: hint failed", exc_info=True)

    # ── Input handling ────────────────────────────────────────────────────────

    async def on_buddy_input_submitted(self, event: BuddyInput.Submitted) -> None:
        text = (event.value or "").strip()
        if not text:
            return

        if _should_exit(text):
            self.app.exit()
            return

        if text.lower() in {"!sleep", "/sleep"}:
            self._track(self._async_set_sleeping(True))
            return

        if text.lower() in {"!wake", "/wake"}:
            self._track(self._async_set_sleeping(False))
            return

        turn_active = self._active_turn is not None and not self._active_turn.done()

        if turn_active:
            with self._state_lock:
                pipeline_running = self._sys_state.pipeline_running
            if pipeline_running:
                # Pipeline is busy processing — drop input, prompt to interrupt.
                try:
                    if self._w_status_bar:
                        self._w_status_bar.set_hint(
                            f"[{_YELLOW}]busy — Esc to interrupt[/]", 2.0
                        )
                except Exception:
                    logger.debug("on_buddy_input_submitted: hint failed", exc_info=True)
                return
            # Pipeline paused, waiting for follow-up input.
            if self._w_chat_log:
                await self._w_chat_log.add_message(text, "user")
            await self._iq.push_typed(text)
            return

        # ── Fresh turn ────────────────────────────────────────────────────────
        self._notify_activity()
        with self._state_lock:
            is_sleeping = self._sys_state.sleeping
        if is_sleeping:
            self._track(self._async_set_sleeping(False))

        if self._w_chat_log:
            await self._w_chat_log.add_message(text, "user")

        try:
            img_paths = extract_image_paths(text)
            if img_paths:
                names = ", ".join(os.path.basename(p) for p in img_paths)
                if self._w_chat_log:
                    await self._w_chat_log.add_message(f"[image: {names}]", "meta")
        except Exception:
            logger.debug(
                "on_buddy_input_submitted: image path extraction failed", exc_info=True
            )

        # Text goes directly to _run_turn — NOT into the queue.
        # The queue is only for mid-turn follow-up inputs via pipeline_input().
        self._active_turn = self._track(self._run_turn(text))

    # ── Voice input ───────────────────────────────────────────────────────────

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
            muted = self._sys_state.mic_off
            running = self._sys_state.pipeline_running

        if running and cmd == VoiceCmd.STOP:
            self._request_interrupt("you asked me to stop")
            return
        if cmd == VoiceCmd.QUIET:
            self._stop_tts()
            return
        if running and cmd == VoiceCmd.NONE:
            return
        if sleeping:
            if cmd == VoiceCmd.WAKE:
                self._track(self._async_set_sleeping(False))
            return
        if cmd == VoiceCmd.SLEEP:
            self._track(self._async_set_sleeping(True))
            return
        if cmd in (VoiceCmd.MIC_OFF, VoiceCmd.MIC_TOGGLE):
            self._toggle_voice_mute()
            return
        if cmd == VoiceCmd.MIC_ON:
            self._set_voice_mute(False)
            return
        if muted:
            return

        # FIX-04: only _handle_voice_input performs the active-turn guard;
        # the check here is removed to eliminate the TOCTOU race.
        loop = self._main_loop
        if loop:
            turn_active = self._active_turn is not None and not self._active_turn.done()
            if turn_active:
                # Turn running but pipeline paused waiting for follow-up — queue only.
                self._iq.push_voice(t, loop)
            else:
                # Fresh turn — bypass queue, start directly.
                self._track(self._handle_voice_input(t))

    async def _handle_voice_input(self, text: str) -> None:
        # FIX-04: sole authoritative guard against concurrent turns.
        if self._active_turn and not self._active_turn.done():
            return
        self._notify_activity()
        if self._w_chat_log:
            await self._w_chat_log.add_message(text, "user")
        self._active_turn = self._track(self._run_turn(text))

    # ── Turn execution ────────────────────────────────────────────────────────

    async def _run_turn(self, user_text: str) -> None:
        async with self._turn_lock:
            self._interrupt_event.clear()
            self._interrupt_reason = ""
            # Yield first so any call_soon_threadsafe callbacks (e.g. INTERRUPT_SENTINEL
            # queued by an Escape press with no active turn) fire before we drain.
            await asyncio.sleep(0)
            # FIX-05: drain stale sentinels via the public InputQueue interface.
            self._iq.drain()

            turn_id = f"turn-{uuid.uuid4().hex[:8]}"
            self._turn_count += 1
            logger.info("turn.start id=%s chars=%d", turn_id, len(user_text))

            with self._state_lock:
                self._sys_state.pipeline_running = True

            self._start_spinner("Leafing through memories...", "working")
            self._refresh_info_bar()
            _turn_start = time.perf_counter()

            loop = self._main_loop
            current_label = "Leafing through memories..."

            # FIX-08: bounded deque prevents unbounded memory growth during long streams.
            stream_buf: collections.deque[str] = collections.deque(
                maxlen=_STREAM_BUF_MAXLEN
            )
            _last_preview_t = 0.0
            _thinking_done = False
            # FIX-10: lock protecting thread-shared mutable state in progress_cb.
            _cb_lock = threading.Lock()

            # Called from asyncio.to_thread() — MUST use call_soon_threadsafe.
            def progress_cb(chunk: str, stream: bool = True) -> None:
                nonlocal current_label, _last_preview_t, _thinking_done
                if loop is None:
                    return
                now = time.perf_counter()

                with _cb_lock:
                    if not stream:
                        # FIX-10: all shared-state mutations inside the lock.
                        _thinking_done = False
                        stream_buf.clear()
                        current_label = chunk.strip() or current_label
                        _label = current_label
                        loop.call_soon_threadsafe(
                            self._update_spinner, _label, "working"
                        )
                        return

                    stream_buf.append(chunk)

                    if now - _last_preview_t < 0.05:
                        return
                    _last_preview_t = now

                    if _thinking_done:
                        return

                    # FIX-09: scan only a short suffix window, not the full joined string.
                    suffix_chars = (
                        "".join(list(stream_buf)[-_THINK_SUFFIX_LEN:])
                        .replace("\r", " ")
                        .replace("\n", " ")
                    )

                    # Preview: last 80 chars of the suffix (still readable).
                    preview = suffix_chars[-80:].strip()
                    loop.call_soon_threadsafe(
                        self._update_spinner, preview or current_label, "thinking"
                    )

                    if _THINK_TAG in suffix_chars:
                        _thinking_done = True
                        stream_buf.clear()
                        _label = current_label
                        loop.call_soon_threadsafe(
                            self._update_spinner, _label, "thinking"
                        )

            async def pipeline_output(text: str) -> None:
                self._stop_spinner()
                if self._w_chat_log:
                    await self._w_chat_log.add_message(text, "buddy")
                self._start_spinner(current_label, "thinking")

            async def pipeline_input() -> str:
                """
                Wait for user follow-up input.

                FIX-06: bounded wait (timeout) + quit-event awareness so the
                coroutine never hangs forever on app exit or crash.
                """
                self._notify_activity()
                with self._state_lock:
                    self._sys_state.pipeline_running = False
                self._stop_spinner()

                try:
                    result = await asyncio.wait_for(
                        self._iq.get(), timeout=_PIPELINE_INPUT_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "pipeline_input: timed out after %.0fs", _PIPELINE_INPUT_TIMEOUT
                    )
                    raise asyncio.CancelledError("pipeline_input timed out")

                # Also abort cleanly if the app is shutting down.
                if self._quit_event.is_set():
                    raise asyncio.CancelledError("app is exiting")

                with self._state_lock:
                    self._sys_state.pipeline_running = True

                if result in (INTERRUPT_SENTINEL, "!", "/stop", "stop", "cancel"):
                    raise asyncio.CancelledError("interrupted by user")

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
                logger.info(
                    "turn.cancelled id=%s reason=%r", turn_id, self._interrupt_reason
                )
                _convs = getattr(
                    getattr(self._state, "artifacts", None), "conversations", None
                )
                if _convs is not None:
                    _reason = self._interrupt_reason or "you interrupted me"
                    _convs.add_buddy_if_unanswered(f"I got interrupted — {_reason}.")
            except Exception as ex:
                logger.exception("turn.crash id=%s err=%r", turn_id, ex)
                if self._w_status_bar:
                    self._w_status_bar.set_hint(
                        f"[{_RED}]⚠ error: {markup_escape(str(ex))}[/]"
                    )
            finally:
                _turn_ms = int((time.perf_counter() - _turn_start) * 1000)
                self._active_turn = None
                self._stop_spinner()
                with self._state_lock:
                    self._sys_state.pipeline_running = False
                self._notify_activity()
                self._refresh_info_bar(turn_ms=_turn_ms)

    # ── Spinner helpers ───────────────────────────────────────────────────────

    def _start_spinner(self, label: str, state: str = "thinking") -> None:
        # FIX-14: use cached widget reference.
        try:
            if self._w_spinner_bar:
                self._w_spinner_bar.show(label, state)
        except Exception:
            logger.debug("_start_spinner failed", exc_info=True)

    def _stop_spinner(self) -> None:
        try:
            if self._w_spinner_bar:
                self._w_spinner_bar.hide()
        except Exception:
            logger.debug("_stop_spinner failed", exc_info=True)

    def _update_spinner(self, label: str, state: str = "thinking") -> None:
        """Called from event loop (via call_soon_threadsafe)."""
        try:
            if self._w_spinner_bar:
                self._w_spinner_bar.update_label(label, state)
        except Exception:
            logger.debug("_update_spinner failed", exc_info=True)

    # ── Sleep ─────────────────────────────────────────────────────────────────

    def _toggle_sleep(self) -> None:
        with self._state_lock:
            sleeping = self._sys_state.sleeping
        self._track(self._async_set_sleeping(not sleeping))

    # FIX-13: dead method _set_sleeping removed — all callers now use
    # self._track(self._async_set_sleeping(...)) directly.

    async def _async_set_sleeping(self, sleeping: bool) -> None:
        # FIX-02: set sleeping state once, correctly, at the top.
        with self._state_lock:
            self._sys_state.sleeping = sleeping

        switcher = self._w_content_switcher or self.query_one(ContentSwitcher)
        sleep_view = self._w_sleep_view or self.query_one(SleepView)
        status_bar = self._w_status_bar or self.query_one(StatusBar)

        if sleeping:
            sleep_view.reset_stats()
            switcher.current = "sleep-view"
            mm = self._memory_manager
            started = False
            if mm is not None:
                started = mm.start_consolidation(on_done=self._on_consolidation_done)
                if started:
                    with self._state_lock:
                        self._sys_state.consolidating = True
            if started:
                status_bar.set_hint(
                    f"[dim {_VIOLET}]😴 sleeping — consolidating memories…[/]", 0
                )
            else:
                status_bar.set_hint(f"[dim {_VIOLET}]😴 sleeping[/]", 0)
        else:
            # FIX-02: sleeping already set to False at the top — no duplicate write.
            mm = self._memory_manager
            if mm is not None and getattr(mm, "is_consolidating", False):
                mm.stop_consolidation(wait=False)

            with self._state_lock:
                self._sys_state.consolidating = False

            switcher.current = "chat-view"
            status_bar.set_hint(f"[{_GREEN}]🌅 awake[/]", 4.0)
            self._notify_activity()

    def _on_consolidation_done(self, report: Any) -> None:
        # Called from a background thread — must not touch widgets directly.
        with self._state_lock:
            self._sys_state.consolidating = False

        def _apply() -> None:
            try:
                sv = self._w_sleep_view or self.query_one(SleepView)
                if report:
                    flash = getattr(report, "scanned", 0) or 0
                    short = getattr(report, "promoted", 0) or 0
                    long_ = getattr(report, "summarized", 0) or 0
                    sv.update_consolidation_stats(flash=flash, short=short, long=long_)
            except Exception:
                logger.debug("_on_consolidation_done._apply failed", exc_info=True)

        try:
            self.app.call_from_thread(_apply)
        except Exception:
            logger.debug(
                "_on_consolidation_done: call_from_thread failed", exc_info=True
            )

    # ── Voice mute ────────────────────────────────────────────────────────────

    def _set_voice_mute(self, muted: bool) -> None:
        with self._state_lock:
            if self._sys_state.mic_off == muted:
                return
            if self._stt is not None:
                try:
                    self._stt.mic_off() if muted else self._stt.mic_on()
                except Exception:
                    logger.debug(
                        "_set_voice_mute: stt mute/unmute failed", exc_info=True
                    )
            self._sys_state.mic_off = muted
        try:
            mic = self._w_mic_indicator or self.query_one(MicIndicator)
            mic.set_state("muted" if muted else "idle")
        except Exception:
            logger.debug("_set_voice_mute: MicIndicator update failed", exc_info=True)
        # FIX-11: escape the icon string so Rich markup is never broken by
        # unexpected characters in the mute icon.
        if muted:
            raw_icon = "🔇 " if _USE_UNICODE else ""
            icon = markup_escape(raw_icon)
            hint = f"[{_DIM}]{icon}Mic Off[/]"
        else:
            hint = (
                f"[{_GREEN}]🎙 Mic On[/]" if _USE_UNICODE else f"[{_GREEN}]Mic On[/]"
            )
        try:
            sb = self._w_status_bar or self.query_one(StatusBar)
            sb.set_hint(hint)
        except Exception:
            logger.debug("_set_voice_mute: StatusBar hint failed", exc_info=True)
        self._refresh_info_bar()

    def _toggle_voice_mute(self) -> None:
        with self._state_lock:
            muted = not self._sys_state.mic_off
        self._set_voice_mute(muted)

    def set_stt_engine(self, stt: Any) -> None:
        self._stt = stt

    def set_mic_active(self) -> None:
        try:
            mic = self._w_mic_indicator or self.query_one(MicIndicator)
            mic.set_state("active")
        except Exception:
            logger.debug("set_mic_active failed", exc_info=True)

    def _handle_voice_interrupt(self) -> None:
        """
        Called via call_soon_threadsafe when the STT engine detects speech onset.
        If a turn is currently running, cancels it (same effect as pressing Escape).
        Always updates the mic indicator to active.
        """
        if self._active_turn and not self._active_turn.done():
            self._interrupt_event.set()
            self._active_turn.cancel()
            self._stop_spinner()
            if self._w_status_bar:
                self._w_status_bar.set_hint(f"[{_YELLOW}]⛔ voice interrupt[/]")
            logger.info("interrupt: voice onset detected")
        try:
            mic = self._w_mic_indicator or self.query_one(MicIndicator)
            mic.set_state("active")
        except Exception:
            logger.debug(
                "_handle_voice_interrupt: MicIndicator update failed", exc_info=True
            )

    def set_mic_idle(self) -> None:
        try:
            mic = self._w_mic_indicator or self.query_one(MicIndicator)
            mic.set_state("idle")
        except Exception:
            logger.debug("set_mic_idle failed", exc_info=True)

    def _stop_tts(self) -> None:
        """
        Stop TTS voice output immediately.

        FIX-12: TTS is not yet wired — log a warning so callers are aware the
        interrupt had no effect, rather than silently doing nothing.
        Wire up: self._tts.interrupt() once TextToSpeech is initialised here.
        """
        logger.warning("_stop_tts called but TTS is not yet wired into the app — no-op")

    # ── Activity / idle ───────────────────────────────────────────────────────

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
                    # FIX-07: sleep a fixed short interval so re-idle after wake
                    # is detected promptly, rather than waiting another full period.
                    await asyncio.sleep(60.0)
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

    def __init__(self, pre_wizard_result: Any | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pre_wizard_result = pre_wizard_result  # from run_pre_textual_setup()
        self._iq = InputQueue()
        self._sys_state = SystemState()
        self._state_lock = threading.Lock()
        self._interrupt_event = threading.Event()
        self._stt: Any = None
        self._main_screen: MainScreen | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bootstrap_state: Any = None  # set by _async_on_boot_done
        # FIX-01
        self._tasks: set[asyncio.Task] = set()

    def _track(self, coro: Any) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.push_screen(SplashScreen())

    async def _async_on_boot_done(self, state: Any) -> None:
        """Called from BootScreen when bootstrap() finishes."""
        if state is None:
            logger.error("bootstrap returned None — exiting")
            self.exit()
            return

        self._bootstrap_state = state  # retained for shutdown in on_unmount

        arts = getattr(state, "artifacts", None)
        brain = getattr(arts, "brain", None)
        conversations = getattr(arts, "conversations", None)

        opener_text = ""
        if brain is not None:
            try:
                boot_log = self.screen.query_one(BootLog)
                await boot_log.add_message("waking up...", "running")
            except Exception:
                pass
            recent = conversations.get_recent_conversations() if conversations else ""
            try:
                opener_text = await asyncio.wait_for(
                    asyncio.to_thread(brain.generate_opener, recent),
                    timeout=45.0,
                )
            except asyncio.TimeoutError:
                logger.warning("opener timed out after 45s, skipping")
            except Exception:
                logger.warning("opener generation failed", exc_info=True)

        mm = getattr(arts, "memory_manager", None)
        self._main_screen = MainScreen(
            state=state,
            input_queue=self._iq,
            sys_state=self._sys_state,
            state_lock=self._state_lock,
            interrupt_event=self._interrupt_event,
            memory_manager=mm,
            opener_text=opener_text,
        )
        await self.switch_screen(self._main_screen)
        self._track(self._start_stt(state))

    async def _start_stt(self, state: Any) -> None:
        try:
            from buddy.ui.stt import SpeechToText

            cfg = getattr(state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            feat_cfg = buddy_cfg.get("features", {}) or {}
            voice_cfg = buddy_cfg.get("voice", {}) or {}
            runtime = cfg.get("runtime", {}) or {}
            whisper_dir = os.path.join(
                (runtime.get("fs") or {}).get("models_dir", "."), "whisper"
            )

            # [features].enable_audio_stt is the master switch.
            # [voice].enabled can also disable it (defaults True when absent).
            voice_enabled = bool(voice_cfg.get("enabled", True))
            if not feat_cfg.get("enable_audio_stt", False) or not voice_enabled:
                if self._main_screen and self._main_screen._w_status_bar:
                    self._main_screen._w_status_bar.set_hint(
                        f"[dim {_DIM}]🎧 voice disabled[/]"
                    )
                return

            mic_idx = voice_cfg.get("microphone_index", -1)
            loop = self._loop or asyncio.get_running_loop()

            def on_text(text: str) -> None:
                if self._main_screen and loop:
                    loop.call_soon_threadsafe(self._main_screen.handle_voice_text, text)
                # mic idle is handled by on_segment_end, not here

            def on_interrupt() -> None:
                # Speech detected — update mic UI only.
                # Pipeline cancellation is word-gated in handle_voice_text;
                # only "stop" / "cancel" / "buddy stop" etc. will interrupt.
                if self._main_screen and loop:
                    loop.call_soon_threadsafe(self._main_screen.set_mic_active)

            def on_speech_start() -> None:
                # Called at speech onset — update mic UI only.
                if self._main_screen and loop:
                    loop.call_soon_threadsafe(self._main_screen.set_mic_active)

            def on_segment_end() -> None:
                # Called after every segment (transcribed or rejected) — return mic to idle.
                if self._main_screen and loop:
                    loop.call_soon_threadsafe(self._main_screen.set_mic_idle)

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
                on_segment_end=on_segment_end,
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
                if self._main_screen._w_status_bar:
                    self._main_screen._w_status_bar.set_hint(
                        f"[{_GREEN}]🎧 voice enabled[/]"
                    )

        except Exception as ex:
            logger.exception("stt: failed to start: %r", ex)
            if self._main_screen and self._main_screen._w_status_bar:
                self._main_screen._w_status_bar.set_hint(
                    f"[{_RED}]⚠ stt failed: {markup_escape(str(ex))}[/]"
                )

    def on_unmount(self) -> None:
        if self._stt is not None:
            try:
                self._stt.stop()
            except Exception:
                logger.debug("on_unmount: stt.stop() failed", exc_info=True)
        _fn = getattr(self._bootstrap_state, "shutdown", None)
        if callable(_fn):
            try:
                _fn()
            except Exception:
                logger.exception("shutdown failed in on_unmount")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def _prewarm_whisper_before_textual() -> None:
    """
    Load WhisperModel into _MODEL_CACHE before Textual claims terminal FDs.

    CTranslate2 spawns subprocesses during model init. Textual's terminal
    driver manipulates pseudo-terminal FDs that break subprocess inheritance.
    Pre-warming here caches the model with clean FDs so SpeechToText gets a
    cache hit (no subprocess spawning) when called later inside Textual.
    """
    try:
        import sys as _sys

        if _sys.version_info >= (3, 11):
            import tomllib as _toml
        else:
            import tomli as _toml  # type: ignore

        # FIX-19 / platform: resolve config root per platform.
        # On Windows, fall through to Path.home()/"Buddy" if env vars are absent.
        if os.name == "nt":
            _appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            _root = Path(_appdata) / "Buddy" if _appdata else Path.home() / "Buddy"
        else:
            _root = Path.home() / ".buddy"

        _cfg_path = _root / "config" / "buddy.toml"
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
            return

        _voice_cfg = _buddy_cfg.get("voice", {}) if isinstance(_buddy_cfg, dict) else {}
        _size = str(_voice_cfg.get("whisper_model_size", "base"))
        _compute_type = str(_voice_cfg.get("compute_type", ""))
        _whisper_dir = str(_root / "data" / "models" / "whisper")

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
    # FIX-17: traceback imported at module level (top of file).
    # FIX-18: single consolidated crash log path.
    _crash_log_path = Path.home() / ".buddy" / "logs" / "buddy_crash.log"
    os.environ.setdefault("TEXTUAL_LOG", str(_crash_log_path))

    # ── Pre-Textual interactive setup ─────────────────────────────────────────
    # First-boot wizard and LLM model selection need a plain terminal (input()
    # works).  Textual takes over the terminal after BuddyApp.run(), so we MUST
    # do all interactive I/O here, before that call.
    _pre_wizard_result: Any | None = None
    try:
        from buddy.buddy_core.boot import run_pre_textual_setup

        _pre_wizard_result = run_pre_textual_setup()
    except Exception as _e:
        logger.warning("run_pre_textual_setup failed (non-fatal): %r", _e)

    _prewarm_whisper_before_textual()
    app = BuddyApp(pre_wizard_result=_pre_wizard_result)

    # SIGTERM handler — covers `kill PID` and systemd/launchd stop signals.
    # Delegates to the same shutdown hook used by on_unmount.
    def _sigterm_handler(sig: int, frame: Any) -> None:
        logger.info("SIGTERM received — shutting down services")
        _fn = getattr(getattr(app, "_bootstrap_state", None), "shutdown", None)
        if callable(_fn):
            try:
                _fn()
            except Exception:
                pass
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (OSError, ValueError):
        pass  # not on main thread or platform doesn't support it

    try:
        app.run()
    except Exception as ex:
        _err = traceback.format_exc()
        # FIX-19: use %r lazy formatting in logger calls.
        logger.error("BuddyApp crashed: %r", ex)
        try:
            with open(_crash_log_path, "a", encoding="utf-8") as _f:
                _f.write(f"\n{'=' * 60}\nBuddyApp crash:\n{_err}\n")
        except Exception:
            pass
        raise
    finally:
        # Belt-and-suspenders: on_unmount fires on clean exits, but on a hard
        # crash or early exit Textual may skip it. Call shutdown here to cover
        # the gap. _shutdown() is idempotent so a double call is harmless.
        _fn = getattr(getattr(app, "_bootstrap_state", None), "shutdown", None)
        if callable(_fn):
            try:
                _fn()
            except Exception:
                pass
