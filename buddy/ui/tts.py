from __future__ import annotations

import threading
import queue
import time
import os
from typing import Literal, Optional

import numpy as np
import sounddevice as sd

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    from TTS.api import TTS as CoquiTTS
except ImportError:
    CoquiTTS = None


TTSMode = Literal["stream", "file"]
TTSEngine = Literal["pyttsx3", "coqui"]


class TextToSpeech:
    """
    Buddy's mouth.

    Design goals:
    - Interruptible
    - Non-blocking
    - Safe (no destructive side effects)
    - Deterministic shutdown
    """

    def __init__(
        self,
        engine: TTSEngine = "coqui",
        mode: TTSMode = "stream",
        coqui_model: Optional[str] = None,
        output_dir: str = "buddy/data/cache/tts",
        max_queue_size: int = 10,
    ):
        self.engine_name = engine
        self.mode = mode
        self.output_dir = output_dir

        self._speak_queue: queue.Queue[Optional[str]] = queue.Queue(
            maxsize=max_queue_size
        )
        self._interrupt_event = threading.Event()
        self._running = True
        self._is_speaking = False

        os.makedirs(self.output_dir, exist_ok=True)

        # -----------------------
        # Init engine
        # -----------------------
        if engine == "pyttsx3":
            if not pyttsx3:
                raise RuntimeError("pyttsx3 not installed")

            self.engine = pyttsx3.init()
            self.engine.setProperty("rate", 160)
            self.is_multispeaker = False
            self.default_speaker = None

        elif engine == "coqui":
            if not CoquiTTS:
                raise RuntimeError("coqui-tts not installed")

            self.engine = CoquiTTS(
                model_name=coqui_model or "tts_models/en/vctk/vits",
                progress_bar=False,
                gpu=False,
            )

            self.is_multispeaker = getattr(self.engine, "is_multi_speaker", False)
            speakers = getattr(self.engine, "speakers", [])
            self.default_speaker = speakers[0] if speakers else None
        else:
            raise ValueError(f"Unknown TTS engine: {engine}")

        # Speaker thread
        self._thread = threading.Thread(target=self._speaker_loop, daemon=True)
        self._thread.start()

    # ==================================================
    # Public API
    # ==================================================

    def speak(self, text: str) -> None:
        """
        Queue text to be spoken.
        Drops input if queue is full to avoid memory pressure.
        """
        text = text.strip()
        if not text:
            return

        try:
            self._speak_queue.put_nowait(text)
        except queue.Full:
            # Drop oldest + enqueue newest
            self._clear_queue()
            self._speak_queue.put_nowait(text)

    def interrupt(self) -> None:
        """
        Immediately stop current speech and clear pending speech.
        """
        self._interrupt_event.set()
        self._clear_queue()

    def is_speaking(self) -> bool:
        return self._is_speaking

    def stop(self) -> None:
        """
        Graceful shutdown.
        """
        self._running = False
        self._interrupt_event.set()
        self._clear_queue()
        self._speak_queue.put(None)

        self._thread.join(timeout=1.0)

        if self.engine_name == "pyttsx3":
            try:
                self.engine.stop()  # type: ignore
            except Exception:
                pass

        print("🛑 TTS shutdown successfully.")

    # ==================================================
    # Speaker loop
    # ==================================================

    def _speaker_loop(self) -> None:
        while self._running:
            text = self._speak_queue.get()
            if text is None:
                continue

            self._interrupt_event.clear()
            self._is_speaking = True

            try:
                if self.engine_name == "pyttsx3":
                    self._speak_pyttsx3(text)
                else:
                    self._speak_coqui(text)
            finally:
                self._is_speaking = False

    # ==================================================
    # Engines
    # ==================================================

    def _speak_pyttsx3(self, text: str) -> None:
        if self._interrupt_event.is_set():
            return
        self.engine.say(text)  # type: ignore
        self.engine.runAndWait()  # type: ignore

    def _speak_coqui(self, text: str) -> None:
        kwargs = {"text": text}
        if self.is_multispeaker and self.default_speaker:
            kwargs["speaker"] = self.default_speaker  # type: ignore

        wav = self.engine.tts(**kwargs)  # type: ignore
        audio = np.asarray(wav, dtype=np.float32)
        sr = self.engine.synthesizer.output_sample_rate  # type: ignore

        if self.mode == "file":
            self._write_wav(audio, sr)
        else:
            self._play_stream(audio, sr)

    # ==================================================
    # Playback
    # ==================================================

    def _play_stream(self, audio: np.ndarray, sr: int) -> None:
        idx = 0
        block_size = 1024

        with sd.OutputStream(
            samplerate=sr,
            channels=1,
            dtype="float32",
        ) as stream:
            while idx < len(audio):
                if self._interrupt_event.is_set():
                    self._fade_out(stream, audio, idx, block_size)
                    break

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
            chunk = (audio[idx:end] * fade).reshape(-1, 1)
            stream.write(chunk)

    # ==================================================
    # File output
    # ==================================================

    def _write_wav(self, audio: np.ndarray, sr: int) -> None:
        ts = int(time.time() * 1000)
        path = os.path.join(self.output_dir, f"tts_{ts}.wav")
        audio_int16 = np.int16(np.clip(audio, -1.0, 1.0) * 32767)

        import wave

        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(audio_int16.tobytes())

    # ==================================================
    # Utils
    # ==================================================

    def _clear_queue(self) -> None:
        while not self._speak_queue.empty():
            try:
                self._speak_queue.get_nowait()
            except queue.Empty:
                break


# ==================================================
# Standalone test
# ==================================================


def main():
    tts = TextToSpeech(engine="coqui")
    print("🗣️ Buddy speaking...")
    tts.speak("Hello. I am Buddy. You can interrupt me by speaking.")

    time.sleep(4)
    print("✋ Interrupting...")
    tts.interrupt()

    time.sleep(2)
    print("🗣️ Buddy speaking again...")
    tts.speak("I stopped. Now I am speaking again.")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        tts.stop()


if __name__ == "__main__":
    main()
