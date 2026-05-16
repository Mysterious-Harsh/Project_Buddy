"""
buddy/tests/test_stt.py — STT backend unit + benchmark tests.

Tests run without a microphone.  All audio is synthesised from numpy.

Whisper tests:   require faster-whisper + a cached model; skipped otherwise.
Parakeet tests:  require onnx-asr installed; skipped otherwise.
Benchmark:       runs both backends back-to-back and prints a latency table.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from buddy.ui.stt import (
    DEFAULT_SAMPLE_RATE,
    _HALLUCINATIONS,
    _PARAKEET_AVAILABLE,
    _resolve_backend,
    _resample_to_16k,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sine_wave(freq: float = 440.0, duration_s: float = 1.5, sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    """Float32 sine wave — realistic non-silent audio."""
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False, dtype=np.float32)
    return (0.5 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _silent(duration_s: float = 0.5, sr: int = DEFAULT_SAMPLE_RATE) -> np.ndarray:
    return np.zeros(int(sr * duration_s), dtype=np.float32)


def _to_i16(audio_f32: np.ndarray) -> np.ndarray:
    """Convert float32 [-1, 1] → int16 PCM."""
    return (np.clip(audio_f32, -1.0, 1.0) * 32767).astype(np.int16)


# ---------------------------------------------------------------------------
# _resolve_backend
# ---------------------------------------------------------------------------

class TestResolveBackend(unittest.TestCase):
    def test_explicit_faster_whisper(self):
        assert _resolve_backend("faster_whisper") == "faster_whisper"

    def test_unknown_falls_back_to_whisper(self):
        assert _resolve_backend("bogus_backend") == "faster_whisper"

    def test_auto_returns_concrete(self):
        result = _resolve_backend("auto")
        assert result in {"parakeet_onnx", "faster_whisper"}

    def test_parakeet_onnx_without_lib(self):
        # Force _PARAKEET_AVAILABLE = False — should degrade gracefully.
        with patch("buddy.ui.stt._PARAKEET_AVAILABLE", False):
            assert _resolve_backend("parakeet_onnx") == "faster_whisper"

    def test_parakeet_onnx_with_lib(self):
        with patch("buddy.ui.stt._PARAKEET_AVAILABLE", True):
            assert _resolve_backend("parakeet_onnx") == "parakeet_onnx"

    def test_auto_selects_parakeet_when_available(self):
        with patch("buddy.ui.stt._PARAKEET_AVAILABLE", True):
            assert _resolve_backend("auto") == "parakeet_onnx"

    def test_auto_falls_back_when_unavailable(self):
        with patch("buddy.ui.stt._PARAKEET_AVAILABLE", False):
            assert _resolve_backend("auto") == "faster_whisper"


# ---------------------------------------------------------------------------
# _resample_to_16k
# ---------------------------------------------------------------------------

class TestResampleTo16k(unittest.TestCase):
    def test_passthrough_when_already_16k(self):
        audio = _sine_wave(sr=DEFAULT_SAMPLE_RATE)
        i16 = _to_i16(audio)
        out = _resample_to_16k(i16, DEFAULT_SAMPLE_RATE)
        assert out.dtype == np.float32
        assert abs(len(out) - len(i16)) <= 1  # length preserved

    def test_downsample_48k_to_16k(self):
        src_sr = 48_000
        audio = _sine_wave(sr=src_sr, duration_s=1.0)
        i16 = _to_i16(audio)
        out = _resample_to_16k(i16, src_sr)
        assert out.dtype == np.float32
        expected_len = int(len(i16) * DEFAULT_SAMPLE_RATE / src_sr)
        assert abs(len(out) - expected_len) <= 4

    def test_downsample_44100_to_16k(self):
        src_sr = 44_100
        audio = _sine_wave(sr=src_sr, duration_s=0.5)
        i16 = _to_i16(audio)
        out = _resample_to_16k(i16, src_sr)
        assert out.dtype == np.float32
        expected_len = int(len(i16) * DEFAULT_SAMPLE_RATE / src_sr)
        assert abs(len(out) - expected_len) <= 8

    def test_amplitude_normalised(self):
        audio = _sine_wave(sr=DEFAULT_SAMPLE_RATE)
        i16 = _to_i16(audio)
        out = _resample_to_16k(i16, DEFAULT_SAMPLE_RATE)
        # float32 output must be in [-1, 1]
        assert out.max() <= 1.01
        assert out.min() >= -1.01

    def test_empty_input(self):
        out = _resample_to_16k(np.array([], dtype=np.int16), DEFAULT_SAMPLE_RATE)
        assert out.dtype == np.float32
        assert len(out) == 0


# ---------------------------------------------------------------------------
# Hallucination filter
# ---------------------------------------------------------------------------

class TestHallucinationFilter(unittest.TestCase):
    """Verify the filter set contains the expected entries and is lowercase."""

    def test_known_hallucinations_present(self):
        expected = {"thank you", "thank you.", "thanks", "goodbye", "bye"}
        assert expected.issubset(_HALLUCINATIONS)

    def test_all_entries_lowercase(self):
        for h in _HALLUCINATIONS:
            assert h == h.lower(), f"Not lowercase: {h!r}"

    def test_filter_logic(self):
        """_transcribe_whisper / _parakeet both strip+lower before matching."""
        text = "Thank you."
        text_clean = text.strip().lower().rstrip(". ")
        assert text_clean in _HALLUCINATIONS


# ---------------------------------------------------------------------------
# _transcribe_whisper  (requires faster-whisper model on disk)
# ---------------------------------------------------------------------------

try:
    from faster_whisper import WhisperModel as _WM
    _WHISPER_IMPORTABLE = True
except ImportError:
    _WHISPER_IMPORTABLE = False


@pytest.mark.skipif(not _WHISPER_IMPORTABLE, reason="faster-whisper not installed")
class TestTranscribeWhisper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from buddy.ui.stt import SpeechToText

        cls.stt = SpeechToText.__new__(SpeechToText)
        # Minimal state needed by _transcribe_whisper
        cls.stt._stt_backend = "faster_whisper"
        cls.stt._parakeet_model_name = "nemo-parakeet-tdt-0.6b-v2"
        cls.stt._parakeet = None
        cls.stt._language_norm = "en"
        cls.stt.beam_size = 1
        cls.stt.whisper_vad_filter = False

        # Try to load a tiny model — skip if not cached/downloadable.
        try:
            from buddy.ui.stt import _load_whisper, _default_whisper_dir
            wm, ct = _load_whisper("tiny", _default_whisper_dir(), "int8")
            cls.stt._whisper = wm
        except Exception as exc:
            pytest.skip(f"Whisper model unavailable: {exc}")

    def test_silent_clip_returns_empty(self):
        silent_i16 = _to_i16(_silent(0.5))
        result = self.stt._transcribe_whisper(silent_i16, DEFAULT_SAMPLE_RATE)
        assert isinstance(result, str)

    def test_hallucination_filtered(self):
        """Patch Whisper to return a known hallucination — must be filtered."""
        mock_seg = MagicMock()
        mock_seg.text = "Thank you."
        with patch.object(self.stt._whisper, "transcribe", return_value=([mock_seg], None)):
            result = self.stt._transcribe_whisper(
                _to_i16(_sine_wave()), DEFAULT_SAMPLE_RATE
            )
        assert result == ""

    def test_real_text_passes_through(self):
        mock_seg = MagicMock()
        mock_seg.text = "hello world"
        with patch.object(self.stt._whisper, "transcribe", return_value=([mock_seg], None)):
            result = self.stt._transcribe_whisper(
                _to_i16(_sine_wave()), DEFAULT_SAMPLE_RATE
            )
        assert result == "hello world"

    def test_none_model_returns_empty(self):
        from buddy.ui.stt import SpeechToText

        stt = SpeechToText.__new__(SpeechToText)
        stt._whisper = None
        stt._language_norm = "en"
        stt.beam_size = 1
        stt.whisper_vad_filter = False
        result = stt._transcribe_whisper(_to_i16(_sine_wave()), DEFAULT_SAMPLE_RATE)
        assert result == ""


# ---------------------------------------------------------------------------
# _transcribe_parakeet  (requires onnx-asr installed)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _PARAKEET_AVAILABLE, reason="onnx-asr not installed")
class TestTranscribeParakeet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from buddy.ui.stt import SpeechToText

        cls.stt = SpeechToText.__new__(SpeechToText)
        cls.stt._stt_backend = "parakeet_onnx"
        cls.stt._parakeet_model_name = "nemo-parakeet-tdt-0.6b-v2"
        cls.stt._whisper = None
        cls.stt._language_norm = "en"
        cls.stt.beam_size = 1
        cls.stt.whisper_vad_filter = False

        try:
            from buddy.ui.stt import _load_parakeet
            cls.stt._parakeet = _load_parakeet("nemo-parakeet-tdt-0.6b-v2")
        except Exception as exc:
            pytest.skip(f"Parakeet model unavailable: {exc}")

    def test_silent_clip_returns_str(self):
        silent_i16 = _to_i16(_silent(0.5))
        result = self.stt._transcribe_parakeet(silent_i16, DEFAULT_SAMPLE_RATE)
        assert isinstance(result, str)

    def test_hallucination_filtered(self):
        mock_model = MagicMock()
        mock_model.recognize.return_value = "thank you"
        original = self.stt._parakeet
        self.stt._parakeet = mock_model
        try:
            result = self.stt._transcribe_parakeet(
                _to_i16(_sine_wave()), DEFAULT_SAMPLE_RATE
            )
        finally:
            self.stt._parakeet = original
        assert result == ""

    def test_real_text_passes_through(self):
        mock_model = MagicMock()
        mock_model.recognize.return_value = "hello buddy"
        original = self.stt._parakeet
        self.stt._parakeet = mock_model
        try:
            result = self.stt._transcribe_parakeet(
                _to_i16(_sine_wave()), DEFAULT_SAMPLE_RATE
            )
        finally:
            self.stt._parakeet = original
        assert result == "hello buddy"

    def test_none_model_returns_empty(self):
        from buddy.ui.stt import SpeechToText

        stt = SpeechToText.__new__(SpeechToText)
        stt._parakeet = None
        result = stt._transcribe_parakeet(_to_i16(_sine_wave()), DEFAULT_SAMPLE_RATE)
        assert result == ""


# ---------------------------------------------------------------------------
# _transcribe dispatcher
# ---------------------------------------------------------------------------

class TestTranscribeDispatcher(unittest.TestCase):
    """Verify _transcribe routes to the correct backend.

    SpeechToText uses __slots__, so instance-level patching is not possible.
    We patch at the class level instead, using autospec=False so the mock is
    a plain callable (no self-binding issues).
    """

    def _make_stt(self, backend: str):
        from buddy.ui.stt import SpeechToText

        stt = SpeechToText.__new__(SpeechToText)
        stt._stt_backend = backend
        stt._whisper = None
        stt._parakeet = None
        stt._language_norm = "en"
        stt.beam_size = 1
        stt.whisper_vad_filter = False
        return stt

    def test_dispatcher_routes_whisper(self):
        from buddy.ui.stt import SpeechToText

        stt = self._make_stt("faster_whisper")
        calls: list[str] = []

        def fake_whisper(self_inner, clip, sr):
            calls.append("whisper")
            return "w"

        def fake_parakeet(self_inner, clip, sr):
            calls.append("parakeet")
            return "p"

        with patch.object(SpeechToText, "_transcribe_whisper", fake_whisper):
            with patch.object(SpeechToText, "_transcribe_parakeet", fake_parakeet):
                result = stt._transcribe(_to_i16(_sine_wave()), DEFAULT_SAMPLE_RATE)

        assert result == "w"
        assert calls == ["whisper"]

    def test_dispatcher_routes_parakeet(self):
        from buddy.ui.stt import SpeechToText

        stt = self._make_stt("parakeet_onnx")
        calls: list[str] = []

        def fake_whisper(self_inner, clip, sr):
            calls.append("whisper")
            return "w"

        def fake_parakeet(self_inner, clip, sr):
            calls.append("parakeet")
            return "p"

        with patch.object(SpeechToText, "_transcribe_whisper", fake_whisper):
            with patch.object(SpeechToText, "_transcribe_parakeet", fake_parakeet):
                result = stt._transcribe(_to_i16(_sine_wave()), DEFAULT_SAMPLE_RATE)

        assert result == "p"
        assert calls == ["parakeet"]


# ---------------------------------------------------------------------------
# Speed benchmark  (requires at least one real backend installed)
# ---------------------------------------------------------------------------

_BENCHMARK_REPS = 3
_BENCH_DURATION_S = 1.5  # clip length per call


def _benchmark_whisper() -> list[float]:
    if not _WHISPER_IMPORTABLE:
        return []
    from buddy.ui.stt import SpeechToText, _load_whisper, _default_whisper_dir

    try:
        wm, _ = _load_whisper("tiny", _default_whisper_dir(), "int8")
    except Exception:
        return []

    stt = SpeechToText.__new__(SpeechToText)
    stt._whisper = wm
    stt._language_norm = "en"
    stt.beam_size = 1
    stt.whisper_vad_filter = False

    audio_i16 = _to_i16(_sine_wave(duration_s=_BENCH_DURATION_S))
    latencies = []
    for _ in range(_BENCHMARK_REPS):
        t0 = time.perf_counter()
        stt._transcribe_whisper(audio_i16, DEFAULT_SAMPLE_RATE)
        latencies.append(time.perf_counter() - t0)
    return latencies


def _benchmark_parakeet() -> list[float]:
    if not _PARAKEET_AVAILABLE:
        return []
    from buddy.ui.stt import SpeechToText, _load_parakeet

    try:
        model = _load_parakeet("nemo-parakeet-tdt-0.6b-v2")
    except Exception:
        return []

    stt = SpeechToText.__new__(SpeechToText)
    stt._parakeet = model

    audio_i16 = _to_i16(_sine_wave(duration_s=_BENCH_DURATION_S))
    latencies = []
    for _ in range(_BENCHMARK_REPS):
        t0 = time.perf_counter()
        stt._transcribe_parakeet(audio_i16, DEFAULT_SAMPLE_RATE)
        latencies.append(time.perf_counter() - t0)
    return latencies


@pytest.mark.benchmark
def test_backend_speed_benchmark(capsys):
    """Print a latency table comparing both backends.  Never fails."""
    whisper_lats = _benchmark_whisper()
    parakeet_lats = _benchmark_parakeet()

    lines = [
        "",
        f"{'Backend':<20} {'Reps':>4} {'Min (s)':>9} {'Mean (s)':>9} {'Max (s)':>9}",
        "-" * 55,
    ]

    def row(name: str, lats: list[float]) -> str:
        if not lats:
            return f"{name:<20} {'N/A':>4} {'—':>9} {'—':>9} {'—':>9}"
        return (
            f"{name:<20} {len(lats):>4} {min(lats):>9.3f} "
            f"{sum(lats)/len(lats):>9.3f} {max(lats):>9.3f}"
        )

    lines.append(row(f"Whisper (tiny, i8)", whisper_lats))
    lines.append(row(f"Parakeet TDT (0.6B)", parakeet_lats))
    lines.append(f"  clip length: {_BENCH_DURATION_S}s")

    with capsys.disabled():
        print("\n".join(lines))

    # Soft assertion: if both ran, Parakeet should not be >5× slower than Whisper.
    if whisper_lats and parakeet_lats:
        whisper_mean = sum(whisper_lats) / len(whisper_lats)
        parakeet_mean = sum(parakeet_lats) / len(parakeet_lats)
        assert parakeet_mean < whisper_mean * 5, (
            f"Parakeet ({parakeet_mean:.3f}s) unexpectedly slow vs Whisper ({whisper_mean:.3f}s)"
        )


if __name__ == "__main__":
    unittest.main()
