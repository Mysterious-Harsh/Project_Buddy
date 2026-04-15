# buddy/ui/textual_app.py
#
# Textual TUI for Buddy — v3
#
# Screens:
#   BuddyApp → SplashScreen (logo) → BootScreen (bootstrap) → MainScreen (chat)
#
# Widget building blocks live in widgets.py.
# Animation frame data lives in face_frames.py.
#
# run_textual() is the public entry point called from main.py.

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading
import time
import uuid
from typing import Any, List, Optional

from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import ContentSwitcher

from buddy.buddy_core.pipeline import handle_turn
from buddy.logger.logger import get_logger
from buddy.tools.vision.image_encoder import extract_image_paths
from buddy.ui.widgets import (
    # color constants (used in CSS strings and hints)
    _USE_UNICODE, _CYAN, _BLUE, _VIOLET, _DIM, _BG, _GREEN, _YELLOW, _RED,
    # helpers
    _should_exit, _match_voice_command,
    # data types
    SystemState, VoiceCmd, InputQueue,
    # splash
    SplashView,
    # boot widgets
    BootBanner, BootLog, BootFaceBar,
    # main widgets
    BannerPane, InfoPane, BuddyHeader,
    StatusBar, SpinnerBar,
    ChatLog, SleepView,
    MicIndicator, BuddyInput, InputBar, BottomSection,
)

logger = get_logger("textual_app")

EXIT_SENTINEL = "__EXIT__"
INTERRUPT_SENTINEL = "__INTERRUPT__"


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

    def compose(self) -> ComposeResult:
        yield SplashView()

    def on_mount(self) -> None:
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

            pre_wizard = getattr(self.app, "_pre_wizard_result", None)
            opts = BootstrapOptions(show_boot_ui=False, pre_wizard_result=pre_wizard)
            state = await asyncio.to_thread(bootstrap, opts, progress_cb)
        except asyncio.CancelledError:
            if queue is not None:
                loop.call_soon_threadsafe(queue.put_nowait, ("__DONE__", None))
            raise
        except BaseException as ex:
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
                app = self.app
                if isinstance(app, BuddyApp):
                    await app._async_on_boot_done(payload)
                return
            await log.add_message(msg, payload)


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
                    placeholder="Type a message… (Enter to send · Ctrl+J for newline · Esc to interrupt)",
                    id="buddy-input",
                )
            yield StatusBar()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

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
        try:
            cfg = getattr(self._state, "config", {}) or {}
            buddy_cfg = cfg.get("buddy", {}) or {}
            feat_cfg = buddy_cfg.get("features", {}) or {}
            voice = "on" if feat_cfg.get("enable_audio_stt", False) else "off"
            self.query_one(StatusBar).set_info(voice=voice, turn=self._turn_count)
        except Exception:
            pass
        try:
            self.query_one(InfoPane).update_turn(self._turn_count, turn_ms)
            if self._memory_manager is not None:
                self.query_one(InfoPane).update_memory_counts(self._memory_manager)
        except Exception:
            pass

    # ── Interrupt / quit ──────────────────────────────────────────────────────

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

    # ── Input handling ────────────────────────────────────────────────────────

    async def _on_buddy_input_submitted(self, event: BuddyInput.Submitted) -> None:
        text = (event.value or "").strip()
        if not text:
            return

        if _should_exit(text):
            return

        if text.lower() in {"!sleep", "/sleep"}:
            asyncio.create_task(self._async_set_sleeping(True))
            return

        if text.lower() in {"!wake", "/wake"}:
            asyncio.create_task(self._async_set_sleeping(False))
            return

        await self._iq.push_typed(text)

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

            self._active_turn = asyncio.create_task(self._run_turn(text))

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

    # ── Turn execution ────────────────────────────────────────────────────────

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

            # Called from asyncio.to_thread() — MUST use call_soon_threadsafe
            def progress_cb(text: str, stream: bool = True) -> None:
                nonlocal current_label, stream_buf, _last_preview_t, _thinking_done
                if loop is None:
                    return
                now = time.perf_counter()

                if not stream:
                    _thinking_done = False
                    stream_buf.clear()
                    current_label = text.strip() or current_label
                    loop.call_soon_threadsafe(
                        self._update_spinner, current_label, "working"
                    )
                    return

                stream_buf.append(text)

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

    # ── Spinner helpers ───────────────────────────────────────────────────────

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

    # ── Sleep ─────────────────────────────────────────────────────────────────

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

    # ── Voice mute ────────────────────────────────────────────────────────────

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

    def __init__(self, pre_wizard_result: Optional[Any] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pre_wizard_result = pre_wizard_result  # from run_pre_textual_setup()
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
        """Called from BootScreen when bootstrap() finishes."""
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

        _root = (
            (
                Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA", ""))
                / "Buddy"
                if (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA"))
                else Path.home() / "Buddy"
            )
            if os.name == "nt"
            else Path.home() / ".buddy"
        )
        _cfg_path = _root / "config" / "buddy.toml"
        if not _cfg_path.exists():
            _cfg_path = Path(__file__).parent.parent / "config" / "buddy.toml"
        if not _cfg_path.exists():
            return

        with open(_cfg_path, "rb") as _f:
            _raw = _toml.load(_f)

        _buddy_cfg = _raw.get("buddy", _raw) if isinstance(_raw, dict) else {}
        _feat_cfg = _buddy_cfg.get("features", {}) if isinstance(_buddy_cfg, dict) else {}
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
    import traceback as _tb

    _log_path = Path.home() / ".buddy" / "logs" / "textual_app.log"
    _textual_log = Path.home() / ".buddy" / "logs" / "textual_crash.log"
    os.environ.setdefault("TEXTUAL_LOG", str(_textual_log))

    # ── Pre-Textual interactive setup ─────────────────────────────────────────
    # First-boot wizard and LLM model selection need a plain terminal (input()
    # works).  Textual takes over the terminal after BuddyApp.run(), so we MUST
    # do all interactive I/O here, before that call.
    _pre_wizard_result: Optional[Any] = None
    try:
        from buddy.buddy_core.boot import run_pre_textual_setup
        _pre_wizard_result = run_pre_textual_setup()
    except Exception as _e:
        logger.warning("run_pre_textual_setup failed (non-fatal): %r", _e)

    _prewarm_whisper_before_textual()
    app = BuddyApp(pre_wizard_result=_pre_wizard_result)
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
