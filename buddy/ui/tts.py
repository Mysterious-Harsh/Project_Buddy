from __future__ import annotations

import re
import threading
import queue
import time
from typing import Any, Callable, Literal, Optional

import numpy as np
import sounddevice as sd


try:
    import pyttsx3
except ImportError:
    pyttsx3 = None


TTSMode = Literal["stream", "file"]
TTSEngine = Literal["kokoro", "pyttsx3"]

# Abbreviations that end with a period but are NOT sentence boundaries.
_ABBREVS = re.compile(
    r"\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|etc|vs|i\.e|e\.g|no|vol|approx|dept|est|fig|govt|"
    r"inc|ltd|corp|co|st|ave|blvd|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.",
    re.IGNORECASE,
)

# Sentence-end boundary: . ! ? followed by whitespace or end-of-string,
# but only when not preceded by an abbreviation (handled by _ABBREVS above).
_SENT_END = re.compile(r"([.!?])(\s+|$)")

# Markdown / noise patterns (applied in order in _sanitize_for_tts).
_MD_FENCED = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_MD_INLINE_CODE = re.compile(r"`[^`]+`")
_MD_BOLD_ITALIC = re.compile(r"\*{1,3}|_{1,3}")
_MD_HEADING = re.compile(r"^#+\s*", re.MULTILINE)
_MD_URL = re.compile(r"https?://\S+")
_MD_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)
_WHITESPACE = re.compile(r"[ \t\r\n]+")


def _sanitize_for_tts(text: str) -> str:
    text = _MD_FENCED.sub(" code block. ", text)
    text = _MD_INLINE_CODE.sub("", text)
    text = _MD_BOLD_ITALIC.sub("", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_URL.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    # Temporarily replace abbreviation dots so they survive the split.
    protected = _ABBREVS.sub(lambda m: m.group(0).replace(".", "\x00"), text)

    parts: list[str] = []
    last = 0
    for m in _SENT_END.finditer(protected):
        chunk = protected[last : m.end()].replace("\x00", ".")
        parts.append(chunk.strip())
        last = m.end()
    tail = protected[last:].replace("\x00", "").strip()
    if tail:
        parts.append(tail)

    # Rejoin very short fragments (< 25 chars) with the next sentence.
    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) < 25:
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)
    return [s for s in merged if s]


class TextToSpeech:
    """
    Buddy's voice output.

    Design goals:
    - Interruptible mid-sentence with fade-out
    - Non-blocking speak() call
    - STT-TTS interlock via on_speaking_start / on_idle callbacks
    - Sentence-level streaming with Kokoro for low perceived latency
    """

    def __init__(
        self,
        engine: TTSEngine = "kokoro",
        voice: str = "af_heart",
        speed: float = 1.0,
        lang_code: str = "a",
        mode: TTSMode = "stream",
        max_queue_size: int = 10,
        on_speaking_start: Optional[Callable[[], None]] = None,
        on_idle: Optional[Callable[[], None]] = None,
    ):
        self.engine_name = engine
        self.mode = mode
        self._on_speaking_start = on_speaking_start
        self._on_idle = on_idle

        self._speak_queue: queue.Queue[Optional[str]] = queue.Queue(
            maxsize=max_queue_size
        )
        self._interrupt_event = threading.Event()
        self._running = True
        self._is_speaking = False
        self._muted = False

        if engine == "pyttsx3":
            if not pyttsx3:
                raise RuntimeError("pyttsx3 not installed")
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", 160)

        elif engine == "kokoro":
            try:
                from kokoro import KPipeline  # type: ignore[import]
            except ImportError:
                raise RuntimeError(
                    "kokoro not installed — run: pip install kokoro soundfile"
                )
            self._KPipeline = KPipeline          # keep class ref for reload
            self._lang_code = lang_code
            self._voice = voice
            self._speed = speed
            self._kokoro: Any = KPipeline(lang_code=lang_code)

        else:
            raise ValueError(f"Unknown TTS engine: {engine!r}")

        self._thread = threading.Thread(target=self._speaker_loop, daemon=True)
        self._thread.start()

    # ── Public API ────────────────────────────────────────────────────────────

    def speak(self, text: str) -> None:
        if self._muted:
            return
        text = text.strip()
        if not text:
            return
        try:
            self._speak_queue.put_nowait(text)
        except queue.Full:
            self._clear_queue()
            self._speak_queue.put_nowait(text)

    def interrupt(self) -> None:
        self._interrupt_event.set()
        self._clear_queue()

    def is_speaking(self) -> bool:
        return self._is_speaking

    def is_muted(self) -> bool:
        return self._muted

    def mute(self) -> None:
        """Stop speech, offload the TTS model, and free RAM."""
        if self._muted:
            return
        self._muted = True
        self.interrupt()
        if self.engine_name == "kokoro":
            self._kokoro = None      # release reference → GC frees model RAM

    def unmute(self) -> None:
        """Reload the TTS model and resume normal operation."""
        if not self._muted:
            return
        if self.engine_name == "kokoro":
            self._kokoro = self._KPipeline(lang_code=self._lang_code)
        self._muted = False

    def stop(self) -> None:
        self._running = False
        self._interrupt_event.set()
        self._clear_queue()
        self._speak_queue.put(None)
        self._thread.join(timeout=2.0)
        if self.engine_name == "pyttsx3":
            try:
                self._engine.stop()  # type: ignore[union-attr]
            except Exception:
                pass

    # ── Speaker loop ──────────────────────────────────────────────────────────

    def _speaker_loop(self) -> None:
        while self._running:
            text = self._speak_queue.get()
            if text is None:
                continue

            self._interrupt_event.clear()
            self._is_speaking = True

            if self._on_speaking_start:
                try:
                    self._on_speaking_start()
                except Exception:
                    pass

            try:
                if self.engine_name == "pyttsx3":
                    self._speak_pyttsx3(text)
                else:
                    self._speak_kokoro(text)
            finally:
                self._is_speaking = False
                if self._speak_queue.empty() and self._on_idle:
                    try:
                        self._on_idle()
                    except Exception:
                        pass

    # ── Engines ───────────────────────────────────────────────────────────────

    def _speak_pyttsx3(self, text: str) -> None:
        if self._interrupt_event.is_set():
            return
        self._engine.say(text)  # type: ignore[union-attr]
        self._engine.runAndWait()  # type: ignore[union-attr]

    def _speak_kokoro(self, text: str) -> None:
        if self._kokoro is None:
            return
        sanitized = _sanitize_for_tts(text)
        sentences = _split_sentences(sanitized)

        for sentence in sentences:
            if self._interrupt_event.is_set():
                return
            sentence = sentence.strip()
            if not sentence:
                continue
            try:
                generator = self._kokoro(
                    sentence, voice=self._voice, speed=self._speed
                )
                for _gs, _ps, audio in generator:
                    if self._interrupt_event.is_set():
                        return
                    self._play_stream(np.asarray(audio, dtype=np.float32), sr=24000)
            except Exception:
                # Skip bad sentence rather than crashing the speaker thread.
                pass

    # ── Playback ──────────────────────────────────────────────────────────────

    def _play_stream(self, audio: np.ndarray, sr: int) -> None:
        idx = 0
        block_size = 1024
        with sd.OutputStream(samplerate=sr, channels=1, dtype="float32") as stream:
            while idx < len(audio):
                if self._interrupt_event.is_set():
                    self._fade_out(stream, audio, idx, block_size)
                    return
                chunk = audio[idx : idx + block_size]
                stream.write(chunk.reshape(-1, 1))
                idx += block_size

    def _fade_out(
        self,
        stream: sd.OutputStream,
        audio: np.ndarray,
        idx: int,
        block_size: int,
    ) -> None:
        end = min(idx + block_size, len(audio))
        fade_len = end - idx
        if fade_len > 0:
            fade = np.linspace(1.0, 0.0, fade_len, dtype=np.float32)
            stream.write((audio[idx:end] * fade).reshape(-1, 1))

    # ── Utils ─────────────────────────────────────────────────────────────────

    def _clear_queue(self) -> None:
        while not self._speak_queue.empty():
            try:
                self._speak_queue.get_nowait()
            except queue.Empty:
                break


# ── Standalone test ───────────────────────────────────────────────────────────


def main() -> None:
    tts = TextToSpeech(engine="kokoro")
    print("Speaking...")
    tts.speak(
        "Hello. I am Buddy, your local companion. "
        "I remember things, reason about your world, and grow alongside you."
    )
    time.sleep(2)
    print("Interrupting...")
    tts.interrupt()
    time.sleep(1)
    tts.speak("I was interrupted. Now I speak again.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        tts.stop()


if __name__ == "__main__":
    main()
