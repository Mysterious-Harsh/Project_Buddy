# buddy/ui/stt.py  —  v1.7  (2025-02-24)
"""
Real-time, wake-word-free speech-to-text with dual VAD back-ends.

VAD back-ends
─────────────
  Silero VAD   Neural model; robust against humming / non-speech.
               Optional: pip install silero-vad torch
               Falls back to Custom VAD automatically when unavailable.

  Custom VAD   Energy + crest-factor + ZCR + attack-spike heuristics.
               Zero extra dependencies; works on every platform.

Architecture
────────────
  stt-listen   Opens / closes the microphone, drives calibration,
               parks (zero CPU) while muted.
  stt-tx       ASR transcription worker (Parakeet or Whisper); parks while muted.
  stt-cb       User-callback dispatcher; one thread, no spam.

Mute behaviour
──────────────
  mute()   → releases the microphone to the OS,
             offloads the ASR model from RAM,
             parks stt-listen and stt-tx (zero wakeups / zero CPU).
  unmute() → reloads the ASR model, reclaims the microphone,
             runs a fresh calibration pass.

Beep
────
  A short 880 Hz tone plays the instant real speech is confirmed,
  giving the user immediate feedback that recording has started.
  Disable with enable_beep=False.  Plays through the output device;
  completely independent from the microphone input stream.
"""

from __future__ import annotations

import functools
import logging
import platform
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, List, Optional, Tuple

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

from buddy.logger.logger import get_logger

logger = get_logger("stt")

# ── Optional Silero VAD ───────────────────────────────────────────────────────
_SILERO_AVAILABLE = False
try:
    import torch  # type: ignore
    from silero_vad import load_silero_vad  # type: ignore

    _SILERO_AVAILABLE = True
except ImportError:
    pass

# ── Optional scipy resampling (polyphase, anti-aliased) ──────────────────────
# Preferred over linear interpolation: applies a low-pass filter before
# decimation so frequencies above the output Nyquist (8 kHz) don't alias
# back into the speech band. Matters for fricatives (s, f, sh, z) at 48kHz.
_SCIPY_RESAMPLE_AVAILABLE = False
try:
    from math import gcd as _gcd
    from scipy.signal import resample_poly as _resample_poly  # type: ignore

    _SCIPY_RESAMPLE_AVAILABLE = True
except ImportError:
    pass

# ── Optional Parakeet TDT (onnx-asr) ─────────────────────────────────────────
_PARAKEET_AVAILABLE = False
try:
    import onnx_asr as _onnx_asr  # type: ignore

    _PARAKEET_AVAILABLE = True
except ImportError:
    pass

# ── Parakeet model registry (mirrors Whisper registry) ───────────────────────
_PARAKEET_CACHE: dict = {}
_PARAKEET_CACHE_LOCK = threading.Lock()
_PARAKEET_LOAD_LOCKS: dict = {}
_PARAKEET_LOAD_LOCKS_LOCK = threading.Lock()


@functools.lru_cache(maxsize=1)
def _parakeet_providers() -> list:
    """
    Return the best available ONNX Runtime provider list for Parakeet.

    CoreML EP in onnxruntime ≤ 1.23.x crashes on Parakeet's ONNX graph due to
    a model_path tracking bug in the CoreML graph partitioner.  Skip it and use
    CPU until a fixed onnxruntime ships.  CUDA is used when available (NVIDIA).
    """
    try:
        import onnxruntime as _ort

        avail = set(_ort.get_available_providers())
        if "CUDAExecutionProvider" in avail:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    except Exception:
        pass
    return ["CPUExecutionProvider"]


def _load_parakeet(model_name: str):
    """Load (or return cached) Parakeet model. Thread-safe; per-key lock prevents double-load."""
    with _PARAKEET_CACHE_LOCK:
        if model_name in _PARAKEET_CACHE:
            return _PARAKEET_CACHE[model_name]

    with _PARAKEET_LOAD_LOCKS_LOCK:
        if model_name not in _PARAKEET_LOAD_LOCKS:
            _PARAKEET_LOAD_LOCKS[model_name] = threading.Lock()
        key_lock = _PARAKEET_LOAD_LOCKS[model_name]

    with key_lock:
        with _PARAKEET_CACHE_LOCK:
            if model_name in _PARAKEET_CACHE:
                return _PARAKEET_CACHE[model_name]
        providers = _parakeet_providers()
        logger.info(
            "[STT] Loading Parakeet %s with providers=%s", model_name, providers
        )
        model = _onnx_asr.load_model(model_name, providers=providers)
        with _PARAKEET_CACHE_LOCK:
            _PARAKEET_CACHE[model_name] = model
        logger.debug("[STT] Parakeet loaded: %s", model_name)
        return model


def _evict_parakeet(model_name: str) -> None:
    with _PARAKEET_CACHE_LOCK:
        _PARAKEET_CACHE.pop(model_name, None)


def _resolve_backend(requested: str) -> str:
    """
    Resolve the requested backend name to a concrete, runnable backend.
    Never returns 'auto'.  Falls back to 'faster_whisper' when Parakeet
    is requested but onnx-asr is not installed.
    """
    # "auto" is a legacy alias — treat identically to "parakeet_onnx"
    if requested in ("parakeet_onnx", "auto"):
        if not _PARAKEET_AVAILABLE:
            logger.warning(
                "[STT] parakeet_onnx requested but onnx-asr not installed "
                "— falling back to faster_whisper. pip install onnx-asr"
            )
            return "faster_whisper"
        return "parakeet_onnx"
    return "faster_whisper"


# ── Common Whisper hallucinations on short/silent clips ──────────────────────
# Lowercase; matched against the lowercased, stripped transcription result.
_HALLUCINATIONS = frozenset({
    "thank you.",
    "thank you",
    "thanks.",
    "thanks",
    "thank you for watching.",
    "thank you for watching",
    "thanks for watching.",
    "thanks for watching",
    "goodbye.",
    "goodbye",
    "bye.",
    "bye",
    "...",
    "[ silence ]",
    "[silence]",
    "[ music ]",
    "[music]",
})


def _apply_hallucination_filter(text: str) -> str:
    """Return '' if text is a known hallucination pattern, else return text unchanged."""
    if text.strip().lower().rstrip(". ") in _HALLUCINATIONS:
        logger.debug("[STT] Hallucination filtered: %r", text)
        return ""
    return text


# =============================================================================
# Tuning constants
# =============================================================================
#
# HOW TO READ THESE ANNOTATIONS
# ──────────────────────────────
# Each constant is annotated with the symptom that tells you to change it and
# the effect the change has. "RAISE" and "LOWER" always refer to the numeric
# value. Changes take effect on the next start() / unmute() — no restart needed
# for most unless noted.
#
# speech_trigger_mult is the most commonly tuned parameter and is a constructor
# argument, not a constant — raise it for noisy rooms, lower for quiet ones.
# =============================================================================

# ── Audio pipeline ────────────────────────────────────────────────────────────

DEFAULT_SAMPLE_RATE = 16_000
# Internal processing rate. Silero requires 16 kHz; Whisper expects 16 kHz.
# Audio from the mic is automatically resampled to this rate.
# ⚠ DO NOT CHANGE — modifying this breaks both VAD back-ends.

DEFAULT_BLOCKSIZE = 512
# Frames delivered per mic callback at 16 kHz = 32 ms per block.
# Silero requires exactly 512 samples @16 kHz per inference call.
# RAISE → fewer CPU wakeups; useful on slow/embedded systems.
# LOWER → not useful; the Silero staging buffer handles any incoming blocksize.
# Effect on timing: changes _frame_sec granularity (below_for, above_for steps).

AUDIO_QUEUE_TIMEOUT = 0.10
# Seconds the VAD loop waits on queue.get() before checking _running/_mic_off.
# RAISE (e.g. 0.25) → lower idle CPU at the cost of slower stop() response.
# DO NOT lower below 0.05 — causes wasted wakeups with no new audio.

MAX_QUEUE_FRAMES = 256
# Raw audio queue depth (frames) between mic callback and VAD loop.
# 256 frames × 32 ms = ~8 s of buffered audio before drops occur.
# RAISE → if debug logs show queue-drop warnings on a slow system.
# LOWER → reduces memory; minimum practical value is ~32 (≈1 second).

# ── Silero VAD ────────────────────────────────────────────────────────────────

SILERO_CHUNK = 512
# Samples fed per Silero inference call. Trained on exactly 512 @16 kHz = 32 ms.
# ⚠ DO NOT CHANGE — the model's RNN timing is calibrated to this window size.

SILERO_THRESH_START = 0.60
# Silero speech probability needed to count a chunk as "voiced" (idle → recording).
# RAISE (0.60–0.75) → if false triggers occur on TV / background music / conversation.
#   Effect: misses soft or distant voices; needs stronger speech to start.
# LOWER (0.30–0.45) → catches whispers and far-field voices.
#   Effect: may trigger on breath sounds, fans, or sustained background noise.
# Recommended range: 0.50–0.70. Keep at least 0.05 above SILERO_THRESH_END.
# Raised from 0.50 → 0.60: music vocals score 0.50–0.60; clear speech scores 0.85+.

SILERO_THRESH_END = 0.30
# Silero probability below which a chunk counts as silence during recording.
# Kept lower than START to provide hysteresis — prevents rapid start/stop flicker.
# RAISE → recording ends sooner after speech stops; shorter segments.
# LOWER → recording holds open through longer mid-sentence pauses.
# Rule: always keep SILERO_THRESH_END < SILERO_THRESH_START by at least 0.05.

SILERO_ONSET_CHUNKS = 4
# Consecutive voiced Silero chunks needed before recording starts (debounce).
# 4 × 32 ms = 128 ms — still fast for any speech, rejects music and noise bursts.
# RAISE (5–6) → more robust; adds 32 ms latency per step.
# LOWER to 2 → fastest response; may trigger on music vocals or brief non-speech.
# Raised from 2 → 4: 64 ms was too short to distinguish music from speech onset.

# ── Segmentation (both VAD back-ends) ────────────────────────────────────────

HANGOVER_SEC = 2.0
# Seconds of silence after the last voiced frame before a segment is closed.
# This is the most noticeable latency in the STT pipeline.
# RAISE (2.5–4.0) → captures long thinking pauses ("I want… hmm… a coffee").
#   Effect: higher transcription latency; you hear the response later.
# LOWER (1.0–1.5) → faster response after you stop speaking.
#   Effect: may split one utterance into two segments if you pause naturally.
# Also tunable per-instance via silence_timeout= constructor argument.

MIN_SPEECH_SEC = 0.30
# Clip duration below which Whisper is never called (silent discard).
# 0.20 s is the shortest reliable monosyllable ("yes", "no", "go", "stop").
# RAISE (0.35–0.50) → aggressively filters mic noise; risks losing short commands.
# LOWER (0.10) → sends very short clips; may waste Whisper on pops and breath.

COOLDOWN_SEC = 0.55
# Dead-time after each segment ends before the VAD can trigger again.
# Prevents immediate re-trigger on the tail energy of a just-finished segment.
# RAISE → longer mandatory gap between utterances; useful in echoey rooms.
# LOWER → system ready to listen again sooner; risk of double-triggering on echo.

MAX_RECORD_SEC = 30.0
# Hard ceiling on segment length. Segment is force-closed and sent to Whisper
# regardless of whether speech is still ongoing.
# RAISE → allows very long dictation without interruption.
# LOWER → shorter Whisper chunks; note: Whisper accuracy degrades above ~30 s.

# ── Onset / offset stability gates ───────────────────────────────────────────

START_SUSTAIN_SEC = 0.20
# Seconds of continuous above-threshold energy needed before recording starts.
# Protects against knocks and pops which drop back to silence in < 80 ms.
# RAISE (0.12–0.20) → more conservative; first syllable may only arrive via preroll.
# LOWER (0.04) → near-instant trigger; may activate on brief impacts or chair scrapes.
# At 32 ms/frame: 0.08 s ≈ 3 frames, 0.16 s ≈ 5 frames.

STOP_SUSTAIN_SEC = 0.30
# Minimum accumulated below-threshold time (below_for) before the
# silence_timeout clock is allowed to fire and close the segment.
# Prevents a single quiet frame mid-sentence from arming the hangover timer.
# RAISE (0.40–0.50) → more resistant to brief drops in volume during speech.
# LOWER (0.15) → hangover fires sooner after silence starts; risk of early cutoff.

# ── Noise-floor tracker ───────────────────────────────────────────────────────

NOISE_ALPHA = 0.04
# EMA coefficient for the adaptive noise baseline (applied each idle quiet frame).
# RAISE (0.06–0.10) → adapts faster to room noise changes.
#   Effect: recovers quickly after a noise event inflates the baseline.
# LOWER (0.01–0.02) → very stable; good for quiet rooms with constant noise floor.
#   Effect: slow to recover if a noise burst permanently inflates the baseline.
# Only updates when not recording AND energy < baseline × CEILING_MULT.

ABS_FLOOR_ENERGY = 0.00018
# Absolute minimum for adaptive_floor regardless of how quiet the room is.
# Prevents the trigger threshold from collapsing to near-zero in silent rooms.
# RAISE → requires louder speech to trigger; use with very sensitive microphones.
# LOWER → allows triggering in very quiet rooms; risk of mic self-noise triggering.

FLOOR_MULT = 4.0
# adaptive_floor = baseline × FLOOR_MULT.
# The trigger threshold is max(adaptive_floor, baseline × speech_trigger_mult).
# RAISE → higher floor relative to baseline; more clearance above steady noise.
# LOWER → floor tracks closer to baseline; easier to trigger in quiet rooms.

CEILING_MULT = 1.6
# Baseline only updates when e < baseline × CEILING_MULT.
# Keeps baseline from being inflated by near-speech sounds during idle.
# RAISE → baseline adapts even in the presence of moderate background sound.
# LOWER (1.2) → only true silence updates the baseline; very stable threshold.

# ── Pre-roll ─────────────────────────────────────────────────────────────────

PREROLL_SEC = 0.50
# Audio buffered before the trigger fires, prepended to every segment.
# Captures the onset of the first word which arrives before enough evidence
# has accumulated to fire START_SUSTAIN_SEC.
# RAISE (0.60–0.80) → more onset captured; minimal memory cost.
# LOWER (0.25) → saves memory; risks clipping the very beginning of the first word.

# ── Custom VAD — impulse / knock rejection ────────────────────────────────────

IMPULSE_CREST = 12
# Crest factor (peak / RMS) above which a frame is classified as impulsive.
# Knocks, key taps, and pops have very high peak-to-RMS ratio.
# RAISE (13–15) → only sharpest knocks rejected; plosive consonants safer.
# LOWER (9–10) → more aggressive rejection; may incorrectly classify loud /p/ /b/.

IDLE_CREST_MULT = 0.90
# During idle, crest threshold = IMPULSE_CREST × this (stricter than during recording).
# Values < 1.0 make the VAD harder to start than to keep recording.
# RAISE toward 1.0 → same impulse threshold idle vs recording.
# LOWER (0.80) → very conservative idle; fewer false triggers from desk noise.

ATTACK_RATIO = 6.0
# Energy ratio e/prev_e above which a frame is flagged as "attacky" (sudden spike).
# Used together with ATTACK_CREST_MULT to catch impulses via energy rise shape.
# RAISE (8–10) → only catches very abrupt spikes; gradual knocks pass through.
# LOWER (4) → catches more gradual rises; risk of flagging normal speech onset.

ATTACK_CREST_MULT = 0.85
# An attacky frame is also classified impulsive only if crest ≥ IMPULSE_CREST × this.
# RAISE toward 1.0 → attacky frames need near-full crest to be flagged.
# LOWER (0.70) → looser; more attacky frames get flagged as impulsive.

# ── Custom VAD — onset flatness (hum / drone rejection) ──────────────────────

ONSET_WIN_FRAMES = 8
# Number of above-threshold frames inspected for energy flatness at onset.
# At 32 ms/frame, 6 frames = 192 ms of onset audio.
# RAISE (8–10) → more frames → more reliable flatness estimate; slower rejection.
# LOWER (4) → faster decision; less reliable for borderline cases.

ONSET_FLATNESS_MAX = 1.6
# Energy max/min ratio below which an onset is rejected as too flat (humming/drone).
# Humming: nearly constant energy → ratio ≈ 1.05–1.30 → rejected.
# Real speech: dynamic onset envelope → ratio ≈ 1.6–5.0+ → passes.
# RAISE (2.0–2.5) → stricter; rejects more sustained sounds including some speech.
#   Risk: blocks slow monotone starters whose energy ramps up gradually.
# LOWER (1.3) → more permissive; only very flat sounds rejected.
#   Risk: some sustained humming passes if it happens to vary slightly.
# Only enforced once onset_e_win is full AND above_for ≥ START_SUSTAIN_SEC.

# ── Calibration ───────────────────────────────────────────────────────────────

CALIB_FRAMES = 60
# Frames for background calibration at startup (≈1.6 s at 32 ms/frame).
# RAISE (80–100) → more accurate baseline from longer sampling; slightly slower start.
# LOWER (25) → faster ready; less accurate baseline in variable noise environments.

CALIB_GATE = 12
# VAD will not trigger until this many frames have been processed (≈384 ms).
# Prevents spurious triggers from mic startup transients.
# RAISE → longer mandatory silence before the system can trigger.
# LOWER → faster readiness; risk of triggering on startup noise.

# ── Whisper ───────────────────────────────────────────────────────────────────

DEFAULT_BEAM_SIZE = 4
# Whisper beam search width. Higher = more accurate but slower transcription.
# 1 = greedy decoding — fastest, ~2–5 % WER penalty on clean speech vs beam=5.
# RAISE to 3–5 → better accuracy on noisy / accented / fast speech.
#   Effect: adds ~20–40 % to transcription time per segment.
# Keep at 1 for command-style short utterances where speed matters more than WER.

DEFAULT_VAD_FILTER = False
# Pass Whisper's built-in VAD filter over the clip before decoding.
# False → rely on our VAD entirely; Whisper decodes the full clip.
# True → Whisper also silences non-speech regions before decoding.
# ENABLE if: Whisper halluccinates text on silent or very short clips.
# DISABLE if: Whisper's filter clips legitimate soft speech at the edges.

# ── Beep ─────────────────────────────────────────────────────────────────────

BEEP_HZ = 880
# Frequency of the confirmation tone in Hz (880 = A5).
# RAISE (1200+) → higher pitch, more piercing; easier to hear in noisy rooms.
# LOWER (440) → lower pitch, less intrusive; may blend into voice range.

BEEP_MS = 80
# Duration of the beep in milliseconds.
# RAISE (150) → longer, easier to notice across the room.
# LOWER (40) → very brief click; may be inaudible on small speakers.

BEEP_AMPLITUDE = 0.20
# Beep volume (0–1 linear, relative to full output scale).
# RAISE (0.35) → louder; use in noisy environments.
# LOWER (0.08) → very quiet; use with headphones or sensitive output devices.

BEEP_FADE_MS = 16
# Cosine fade-in and fade-out duration in milliseconds.
# Prevents audible clicks from abrupt start/stop of the sine tone.
# ⚠ DO NOT lower below 5 ms — shorter fades produce audible clicks.
# RAISE (20) → smoother fade; slightly softer attack.

# ── Dual-alpha EMA: fast attack, slow decay ──────────────────────────────
NOISE_ALPHA_FAST = 0.15  # adapts quickly to noise spikes
NOISE_ALPHA_SLOW = 0.02  # slowly stabilizes baseline
# =============================================================================
# Module-level helpers  (lru_cache'd — zero cost after first call)
# =============================================================================


@functools.lru_cache(maxsize=1)
def _is_apple_silicon() -> bool:
    try:
        return platform.system().lower() == "darwin" and platform.machine().lower() in {
            "arm64",
            "aarch64",
        }
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _default_whisper_dir() -> str:
    """Walk up from this file to find the project root, then return data/models/whisper."""
    here = cur = Path(__file__).resolve().parent
    for _ in range(6):
        if (
            (cur / "data").exists()
            or (cur / "pyproject.toml").exists()
            or (cur / ".git").exists()
        ):
            here = cur
            break
        cur = cur.parent
    return str((here / "data" / "models" / "whisper").resolve())


def _normalize_language(lang: str) -> str:
    s = (lang or "").strip()
    if not s:
        return "en"
    return s.split("-", 1)[0].lower() if "-" in s else s.lower()


def _compute_type_candidates(user_ct: str) -> List[str]:
    """Return compute-type strings to try, in preference order, with safe fallbacks."""
    user_ct = (user_ct or "").strip()
    preferred: List[str] = []
    if user_ct:
        preferred.append(user_ct)
    else:
        if _is_apple_silicon():
            preferred.append("int8_float16")
        preferred.append("int8")
    # Deduplicate while preserving order, then append safe fallbacks
    seen: set = set()
    out: List[str] = []
    for ct in preferred + ["float16", "float32"]:
        if ct and ct not in seen:
            seen.add(ct)
            out.append(ct)
    return out


def _log_audio_devices() -> None:
    """Dump available audio devices to DEBUG (never ERROR)."""
    try:
        default = getattr(sd, "default", None)
        logger.debug("sounddevice default.device=%s", getattr(default, "device", None))
    except Exception:
        pass
    try:
        devices = sd.query_devices()
        if isinstance(devices, list):
            for idx, d in enumerate(devices):
                if isinstance(d, dict):
                    logger.debug(
                        "DEV %d: name=%r in=%s out=%s hostapi=%s",
                        idx,
                        d.get("name"),
                        d.get("max_input_channels"),
                        d.get("max_output_channels"),
                        d.get("hostapi"),
                    )
    except Exception:
        logger.debug("Failed to list audio devices", exc_info=True)


def _resample_to_16k(pcm_i16: np.ndarray, src_sr: int) -> np.ndarray:
    """
    Resample int16 PCM from src_sr to 16000 Hz.
    Returns a float32 array in [-1, 1] ready for Silero / Whisper.

    Uses scipy polyphase resampling when available (preferred):
      • Applies a proper anti-alias low-pass filter before decimation.
      • Prevents high-frequency speech content (fricatives: s, f, sh, z)
        from folding back into the 0–8 kHz band at 48→16 kHz ratios.
    Falls back to linear interpolation when scipy is not installed.
    If src_sr == 16000 the array is just normalised and returned.
    """
    audio = pcm_i16.astype(np.float32) / 32768.0
    if src_sr == DEFAULT_SAMPLE_RATE:
        return audio
    if _SCIPY_RESAMPLE_AVAILABLE:
        # Polyphase resampling: integer up/down ratios computed from GCD.
        # 48kHz→16kHz: gcd=16000, up=1, down=3  (fast: tiny filter).
        # 44.1kHz→16kHz: gcd=200, up=160, down=441 (still sub-ms for 32ms clips).
        g = _gcd(DEFAULT_SAMPLE_RATE, int(src_sr))
        up = DEFAULT_SAMPLE_RATE // g
        down = int(src_sr) // g
        return _resample_poly(audio, up, down).astype(np.float32)
    # Fallback: linear interpolation — fast, no deps, acceptable quality at 16kHz.
    n_src = len(audio)
    n_dst = int(round(n_src * DEFAULT_SAMPLE_RATE / src_sr))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32)
    src_idx = np.linspace(0, n_src - 1, n_dst, dtype=np.float64)
    lo = src_idx.astype(np.int64)
    hi = np.minimum(lo + 1, n_src - 1)
    frac = (src_idx - lo).astype(np.float32)
    return audio[lo] + frac * (audio[hi] - audio[lo])


def _make_beep_pcm(
    hz: float = BEEP_HZ,
    ms: int = BEEP_MS,
    amplitude: float = BEEP_AMPLITUDE,
    fade_ms: int = BEEP_FADE_MS,
    sr: int = DEFAULT_SAMPLE_RATE,
) -> np.ndarray:
    """
    Synthesise a short pure tone with cosine fade-in and fade-out.
    Returns float32 samples in [-1, 1] ready for sd.play().
    Called once at init; zero allocation at trigger time.
    """
    n = max(1, int(sr * ms / 1000))
    fade_n = max(1, min(int(sr * fade_ms / 1000), n // 2))
    t = np.linspace(0.0, ms / 1000.0, n, endpoint=False, dtype=np.float32)
    wave = amplitude * np.sin(2.0 * np.pi * hz * t)
    # Cosine envelope: ramps 0→1 at start, 1→0 at end
    fade_in = 0.5 * (1.0 - np.cos(np.pi * np.arange(fade_n, dtype=np.float32) / fade_n))
    fade_out = fade_in[::-1]
    wave[:fade_n] *= fade_in
    wave[-fade_n:] *= fade_out
    return wave


# =============================================================================
# Whisper model registry  (module-level, thread-safe)
# =============================================================================

_MODEL_CACHE: dict = {}
_MODEL_CACHE_LOCK = threading.Lock()
# Per-key locks prevent two threads from loading the same model simultaneously.
_MODEL_LOAD_LOCKS: dict = {}
_MODEL_LOAD_LOCKS_LOCK = threading.Lock()


def _load_whisper(
    size: str, download_root: str, user_ct: str
) -> Tuple[WhisperModel, str]:
    """
    Load (or return cached) WhisperModel, trying compute-types in preference order.
    Returns (model, effective_compute_type).
    Thread-safe: per-key locks prevent two threads from double-loading the same model.
    """
    for ct in _compute_type_candidates(user_ct):
        key = f"{size}|{download_root}|{ct}"

        # Fast path: already cached.
        with _MODEL_CACHE_LOCK:
            if key in _MODEL_CACHE:
                return _MODEL_CACHE[key], ct

        # Acquire a per-key load lock so only one thread loads each key.
        with _MODEL_LOAD_LOCKS_LOCK:
            if key not in _MODEL_LOAD_LOCKS:
                _MODEL_LOAD_LOCKS[key] = threading.Lock()
            key_lock = _MODEL_LOAD_LOCKS[key]

        with key_lock:
            # Re-check: another thread may have loaded while we waited.
            with _MODEL_CACHE_LOCK:
                if key in _MODEL_CACHE:
                    return _MODEL_CACHE[key], ct

            try:
                try:
                    wm = WhisperModel(
                        size,
                        device="auto",
                        compute_type=ct,
                        download_root=download_root,
                        num_workers=1,
                        cpu_threads=4,
                    )
                except TypeError:
                    # Older faster-whisper doesn't accept num_workers / cpu_threads
                    wm = WhisperModel(
                        size,
                        device="auto",
                        compute_type=ct,
                        download_root=download_root,
                    )

                with _MODEL_CACHE_LOCK:
                    _MODEL_CACHE[key] = wm
                logger.info("[STT] Whisper loaded: size=%s compute_type=%s", size, ct)
                return wm, ct

            except Exception as exc:
                logger.debug("[STT] Whisper compute_type=%s failed: %r", ct, exc)

    raise RuntimeError(f"WhisperModel '{size}' failed for all compute-type candidates")


def _evict_whisper(size: str, download_root: str, compute_type: str) -> None:
    """Remove a model from the cache so its memory can be freed by the GC."""
    key = f"{size}|{download_root}|{compute_type}"
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.pop(key, None)


# =============================================================================
# Callback worker
# =============================================================================


class _CallbackWorker:
    """
    Dispatches user callbacks on one dedicated daemon thread.
    Uses a bounded queue with drop-oldest semantics so slow callbacks
    never stall the VAD loop.
    """

    _Q_SIZE = 16

    def __init__(self) -> None:
        self._q: "queue.Queue[Tuple[Callable, tuple]]" = queue.Queue(
            maxsize=self._Q_SIZE
        )
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True, name="stt-cb")
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait((lambda: None, ()))
        except queue.Full:
            pass
        self._t.join(timeout=1.5)

    def submit(self, cb: Optional[Callable], *args: object) -> None:
        if cb is None:
            return
        # Drop-oldest retry loop — the new item always lands
        while True:
            try:
                self._q.put_nowait((cb, args))
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                cb, args = self._q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                cb(*args)
            except Exception:
                logger.exception("[STT] Callback raised: %r", cb)


# =============================================================================
# SpeechToText  v1.0
# =============================================================================


class SpeechToText:
    """
    Continuous, real-time STT.  No wake word required.

    Parameters
    ──────────
    whisper_model_size   faster-whisper model name ("tiny","base","small","medium","large-v3")
    whisper_download_root  local directory for model weights (auto-detected if omitted)
    language             BCP-47 language code, e.g. "en", "fr", "zh-CN"
    sample_rate          audio sample rate in Hz (default 16 000)
    microphone_index     sounddevice device index; None = OS default
    calibration_sec      > 0 → blocking silence window before listening starts
                         0   → fast background calibration (default)
    beam_size            Whisper beam size (1 = fastest, 5 = more accurate)
    speech_trigger_mult  start_threshold = baseline × this  (raise for noisy rooms)
    silence_timeout      seconds of silence that ends a recording segment
    on_text              callback(text: str) — called with each transcription
    on_interrupt         callback() — called when speech is first detected
    max_queue_frames     audio queue depth (frames)
    debug                emit per-frame meter logs
    compute_type         CTranslate2 compute type ("int8", "float16", …); auto if empty
    whisper_vad_filter   pass Whisper's built-in VAD filter over audio before decode
    use_silero_vad       True → Silero neural VAD (falls back to Custom if unavailable)
    enable_beep          True → play a short 880 Hz tone when speech is confirmed
    min_clip_snr         clip energy must be ≥ this × noise baseline to be sent to STT.
                         0.0 = disabled.  Raise to 4–8 to reject far-field / ambient speech.
    silero_thresh_start  Silero speech probability needed to count a chunk as voiced.
                         Raise to 0.75–0.85 for noisy rooms (default 0.65).
    silero_onset_chunks  consecutive voiced Silero chunks (32 ms each) before recording
                         starts.  Raise to 6–8 to filter brief far-field utterances.
    """

    __slots__ = (
        # ── public config ────────────────────────────────────────────────────
        "debug",
        "on_text",
        "on_interrupt",
        "on_speech_start",
        "language",
        "sample_rate",
        "microphone_index",
        "beam_size",
        "speech_trigger_mult",
        "silence_timeout",
        "calibration_sec",
        "whisper_vad_filter",
        "use_silero_vad",
        # ── resolved at init, needed for reload ─────────────────────────────
        "_whisper_model_size",
        "_whisper_download_root",
        "_compute_type",  # effective compute type chosen during load
        "_language_norm",  # normalised once; re-used every transcription
        # ── backend selection ────────────────────────────────────────────────
        "_stt_backend",  # "faster_whisper" | "parakeet_onnx"
        "_parakeet_model_name",
        # ── models (None while muted / offloaded or backend not active) ─────
        "_whisper",
        "_parakeet",
        # ── audio pipeline ──────────────────────────────────────────────────
        "_audio_q",  # queue.Queue[bytes]
        "_frame_sec",  # seconds per audio frame (constant per stream)
        # ── lifecycle ───────────────────────────────────────────────────────
        "_running",
        "_listen_thread",
        # ── transcription worker ────────────────────────────────────────────
        "_tx_q",  # queue.Queue[Optional[np.ndarray]]
        "_tx_stop",  # threading.Event
        "_tx_thread",
        # ── mute ────────────────────────────────────────────────────────────
        "_mic_off",  # bool
        "_mic_on_event",  # threading.Event — SET when mic is ON, CLEAR when off
        # ── callbacks ───────────────────────────────────────────────────────
        "_cbw",
        "enable_beep",
        "_beep_pcm",
        "_beep_q",
        "_beep_thread",
        "_last_speech_t",  # monotonic time of last _on_speech_detected — debounce guard
        "on_segment_end",  # called after every segment (transcribed or rejected)
        "_audio_paused",
        # ── noise / proximity gates ──────────────────────────────────────────
        "min_clip_snr",  # float: reject clips below this × noise baseline
        "_silero_thresh_start",  # float: Silero onset probability threshold
        "_silero_onset_chunks",  # int:   consecutive voiced chunks to start recording
    )

    # ─────────────────────────────────────────────────────────────────────────
    def __init__(
        self,
        *,
        whisper_model_size: str = "small",
        whisper_download_root: Optional[str] = None,
        language: str = "en",
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        microphone_index: Optional[int] = None,
        calibration_sec: float = 0.0,
        beam_size: int = DEFAULT_BEAM_SIZE,
        speech_trigger_mult: float = 3.0,
        silence_timeout: float = HANGOVER_SEC,
        on_text: Optional[Callable[[str], None]] = None,
        on_interrupt: Optional[Callable[[], None]] = None,
        on_speech_start: Optional[Callable[[], None]] = None,
        on_segment_end: Optional[Callable[[], None]] = None,
        max_queue_frames: int = MAX_QUEUE_FRAMES,
        debug: bool = False,
        compute_type: str = "",
        whisper_vad_filter: bool = DEFAULT_VAD_FILTER,
        use_silero_vad: bool = True,
        enable_beep: bool = True,
        stt_backend: str = "parakeet_onnx",
        parakeet_model: str = "nemo-parakeet-tdt-0.6b-v2",
        min_clip_snr: float = 4.0,
        silero_thresh_start: float = SILERO_THRESH_START,
        silero_onset_chunks: int = SILERO_ONSET_CHUNKS,
    ) -> None:
        # ── public fields ────────────────────────────────────────────────────
        self.debug = bool(debug)
        self.on_text = on_text
        self.on_interrupt = on_interrupt
        self.on_speech_start = on_speech_start
        self.on_segment_end = on_segment_end
        self.language = language
        self.sample_rate = int(sample_rate)
        self.beam_size = int(beam_size)
        self.speech_trigger_mult = float(speech_trigger_mult)
        self.silence_timeout = float(silence_timeout)
        self.calibration_sec = float(calibration_sec)
        self.whisper_vad_filter = bool(whisper_vad_filter)
        self.use_silero_vad = bool(use_silero_vad)
        self.enable_beep = bool(enable_beep)

        # Normalise mic index: negatives → None (OS default)
        if microphone_index is not None:
            try:
                mi = int(microphone_index)
                microphone_index = None if mi < 0 else mi
            except (TypeError, ValueError):
                microphone_index = None
        self.microphone_index = microphone_index

        # ── resolved fields ──────────────────────────────────────────────────
        self._whisper_model_size = whisper_model_size
        self._whisper_download_root = whisper_download_root or _default_whisper_dir()
        self._compute_type = (compute_type or "").strip()
        self._language_norm = _normalize_language(language)

        # ── audio pipeline ───────────────────────────────────────────────────
        self._audio_q: "queue.Queue[bytes]" = queue.Queue(
            maxsize=int(min(max_queue_frames, MAX_QUEUE_FRAMES))
        )
        self._frame_sec: float = DEFAULT_BLOCKSIZE / float(
            self.sample_rate or DEFAULT_SAMPLE_RATE
        )

        # ── transcription worker ─────────────────────────────────────────────
        self._tx_q: "queue.Queue[Optional[Tuple[np.ndarray, int]]]" = queue.Queue(
            maxsize=8
        )
        self._tx_stop: threading.Event = threading.Event()
        self._tx_thread: Optional[threading.Thread] = None

        # ── lifecycle ────────────────────────────────────────────────────────
        self._running = False
        self._listen_thread: Optional[threading.Thread] = None

        # ── mic on/off ────────────────────────────────────────────────────────
        # _mic_on_event is SET when the mic is ON (active).
        # Both worker threads wait on it when mic is off → zero CPU while parked.
        self._mic_off = False
        self._mic_on_event = threading.Event()
        self._mic_on_event.set()  # start with mic on
        # When set, _audio_callback discards frames before they hit the queue.
        # Used by TTS interlock — mic stays open, model stays loaded, zero VAD/ASR overhead.
        self._audio_paused = threading.Event()

        # ── callbacks ────────────────────────────────────────────────────────
        self._cbw = _CallbackWorker()

        # ── Beep — synthesised once; zero allocation at trigger time ────
        self._beep_pcm = _make_beep_pcm() if enable_beep else None
        self._beep_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=4)
        self._beep_thread = threading.Thread(
            target=self._beep_worker, daemon=True, name="stt-beep"
        )
        self._beep_thread.start()
        self._last_speech_t: float = 0.0

        # ── Noise / proximity gates ──────────────────────────────────────────
        self.min_clip_snr: float = float(min_clip_snr)
        self._silero_thresh_start: float = float(silero_thresh_start)
        self._silero_onset_chunks: int = max(1, int(silero_onset_chunks))

        # ── Backend resolution ────────────────────────────────────────────────
        self._stt_backend: str = _resolve_backend(stt_backend)
        self._parakeet_model_name: str = parakeet_model

        # ── Models (only one is ever non-None at a time) ──────────────────────
        self._whisper: Optional[WhisperModel] = None
        self._parakeet = None
        try:
            self._load_asr_model()
        except Exception:
            logger.warning(
                "[STT] ASR model load failed at init — STT will be silent until"
                " mic_on().",
                exc_info=True,
            )

        # Silence noisy CTranslate2 / faster-whisper internal loggers
        for name in ("faster_whisper", "ctranslate2"):
            try:
                logging.getLogger(name).setLevel(logging.WARNING)
            except Exception:
                pass

        if self.use_silero_vad and not _SILERO_AVAILABLE:
            logger.warning(
                "[STT] use_silero_vad=True but silero-vad/torch not found — "
                "falling back to Custom VAD.  pip install silero-vad torch"
            )

        vad_backend = (
            "silero" if (self.use_silero_vad and _SILERO_AVAILABLE) else "custom"
        )
        logger.info(
            "[STT] init | backend=%s sr=%s mic=%s calib=%.1fs silence=%.1fs "
            "mult=%.1f vad=%s beep=%s",
            self._stt_backend,
            self.sample_rate,
            self.microphone_index if self.microphone_index is not None else "default",
            self.calibration_sec,
            self.silence_timeout,
            self.speech_trigger_mult,
            vad_backend,
            self.enable_beep,
        )

    # =========================================================================
    # Whisper load / offload
    # =========================================================================

    def _load_whisper_model(self) -> None:
        """Load Whisper into RAM/VRAM and warm it up."""
        wm, ct = _load_whisper(
            self._whisper_model_size,
            self._whisper_download_root,
            self._compute_type,
        )
        self._whisper = wm
        self._compute_type = ct
        self._warmup_whisper()

    def _offload_whisper_model(self) -> None:
        """
        Delete the Whisper model from this instance and evict it from the
        module cache so Python's GC can free RAM and VRAM.

        CTranslate2 has no explicit unload API, but releasing all references
        (instance + cache) allows the GC to collect the model and its tensors.
        GPU memory is returned to the driver when CUDA tensors are freed.
        """
        if self._whisper is None:
            return
        self._whisper = None
        _evict_whisper(
            self._whisper_model_size,
            self._whisper_download_root,
            self._compute_type,
        )
        logger.info("[STT] Whisper offloaded — RAM/VRAM released.")

    def _warmup_whisper(self) -> None:
        """
        Run one silent inference pass so CTranslate2 JIT-compiles its kernels.
        Eliminates the multi-second stall on the very first real utterance.
        Non-fatal if it fails.
        """
        if self._whisper is None:
            return
        try:
            silent = np.zeros(DEFAULT_SAMPLE_RATE // 2, dtype=np.float32)
            list(
                self._whisper.transcribe(
                    silent,
                    language=self._language_norm,
                    beam_size=self.beam_size,  # must match production to JIT the beam-search path
                    vad_filter=False,
                )[0]
            )
            logger.debug("[STT] Whisper warm-up done.")
        except Exception:
            pass

    # =========================================================================
    # ASR dispatcher  (backend-agnostic load / offload)
    # =========================================================================

    def _load_asr_model(self) -> None:
        """Load whichever backend is active."""
        if self._stt_backend == "parakeet_onnx":
            self._load_parakeet_model()
        else:
            self._load_whisper_model()

    def _offload_asr_model(self) -> None:
        """Offload whichever backend is active."""
        if self._stt_backend == "parakeet_onnx":
            self._offload_parakeet_model()
        else:
            self._offload_whisper_model()

    # =========================================================================
    # Parakeet load / offload
    # =========================================================================

    def _load_parakeet_model(self) -> None:
        """Load Parakeet ONNX model into cache and warm it up."""
        model = _load_parakeet(self._parakeet_model_name)
        self._parakeet = model
        self._warmup_parakeet()

    def _offload_parakeet_model(self) -> None:
        """Release instance reference and evict cache so GC can free RAM."""
        if self._parakeet is None:
            return
        self._parakeet = None
        _evict_parakeet(self._parakeet_model_name)
        logger.info("[STT] Parakeet offloaded — RAM released.")

    def _warmup_parakeet(self) -> None:
        """One silent inference pass — triggers ONNX Runtime JIT compilation."""
        model = self._parakeet
        if model is None:
            return
        try:
            silent = np.zeros(DEFAULT_SAMPLE_RATE // 2, dtype=np.float32)
            model.recognize(silent)
            logger.debug("[STT] Parakeet warm-up done.")
        except Exception:
            pass

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def start(self) -> None:
        """Start listening.  No-op if already running."""
        if self._running:
            return
        self._running = True
        self._tx_stop.clear()
        self._mic_on_event.set()  # ensure active on start

        self._tx_thread = threading.Thread(
            target=self._transcribe_worker, daemon=True, name="stt-tx"
        )
        self._tx_thread.start()

        self._listen_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name="stt-listen"
        )
        self._listen_thread.start()

    def stop(self) -> None:
        """Shut down all threads and release all resources."""
        self._running = False
        self._mic_on_event.set()  # unblock any parked threads so they can exit

        # Unblock VAD queue.get
        try:
            self._audio_q.put_nowait(b"")
        except queue.Full:
            pass

        # Unblock and stop transcription worker
        self._tx_stop.set()
        self._drain_queue(self._tx_q)
        try:
            self._tx_q.put_nowait(None)
        except queue.Full:
            pass

        self._drain_queue(self._audio_q)

        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=2.0)
        if self._tx_thread and self._tx_thread.is_alive():
            self._tx_thread.join(timeout=3.0)

        self._cbw.stop()
        self._offload_asr_model()
        logger.info("[STT] Shutdown complete.")

    # =========================================================================
    # Mic on / off
    # =========================================================================

    @property
    def is_mic_off(self) -> bool:
        """True while the microphone is released and the model is offloaded."""
        return self._mic_off

    def mic_off(self) -> None:
        """Release the mic, offload the ASR model, park worker threads (zero CPU)."""
        if not self._running or self._mic_off:
            return

        self._mic_off = True
        self._mic_on_event.clear()

        try:
            self._audio_q.put_nowait(b"")
        except queue.Full:
            pass

        self._drain_queue(self._audio_q)
        self._drain_queue(self._tx_q)
        self._offload_asr_model()
        logger.info(
            "[STT] Mic off — mic released, model offloaded, stale audio discarded."
        )

    def mic_on(self) -> None:
        """
        • Reload the ASR model.
        • Reclaim the microphone.
        • Run a fresh calibration pass (uses the same calibration_sec as __init__).

        Thread-safe.  No-op if mic is already on.
        """
        if not self._running or not self._mic_off:
            return

        # Reload model before unparking threads (warm-up happens inside)
        self._load_asr_model()

        self._mic_off = False
        self._mic_on_event.set()  # wake both worker threads

        logger.info("[STT] Mic on — model loaded, mic reclaimed, recalibrating…")

    def pause_audio(self) -> None:
        """
        Gate audio at the callback level — mic stays open, model stays loaded.
        Frames are discarded before VAD or ASR runs. Zero CPU overhead.
        Used by TTS interlock so Buddy's voice is never transcribed.
        """
        if self._audio_paused.is_set():
            return
        self._audio_paused.set()
        self._drain_queue(self._audio_q)  # flush any frames already queued

    def resume_audio(self) -> None:
        """Re-open the audio gate after TTS finishes speaking."""
        self._audio_paused.clear()

    # =========================================================================
    # Beep
    # =========================================================================

    def _beep_worker(self) -> None:
        """Single thread drains beep requests sequentially. Prevents thread explosion."""
        while True:
            pcm = self._beep_q.get()
            if pcm is None:  # Poison pill for shutdown
                break
            try:
                sd.play(pcm, DEFAULT_SAMPLE_RATE, blocking=True)
            except Exception:
                pass

    def _play_beep(self) -> None:
        """
        Play the pre-synthesised confirmation tone through the output device.
        Runs in a fire-and-forget daemon thread so the VAD hot path is never
        stalled by audio-driver latency.  sd.play() uses the output device and
        is completely independent from the microphone input stream.
        """
        if self._beep_pcm is None:
            return
        try:
            self._beep_q.put_nowait(self._beep_pcm)
        except queue.Full:
            pass  # Drop beep if output driver is busy; never block VAD

    def _on_speech_detected(self) -> None:
        """
        Called by both VAD back-ends the instant real speech is confirmed.
        Plays the confirmation beep immediately so the user knows Buddy is
        listening, then submits on_interrupt and on_speech_start callbacks.
        A pure-tone beep at recording onset is not transcribed by Whisper
        (vad_filter strips it; even without it, a brief sine tone produces
        no text output).
        """
        # Debounce: if a stream drops and reopens mid-utterance the new VAD
        # instance would fire again immediately.  Gate on COOLDOWN_SEC — any
        # legitimate new speech event is always preceded by silence_timeout
        # (2 s) + COOLDOWN_SEC (0.55 s), so this window never suppresses real
        # successive speech.
        now = time.monotonic()
        if now - self._last_speech_t < COOLDOWN_SEC:
            return
        self._last_speech_t = now
        self._play_beep()
        self._cbw.submit(self.on_interrupt)
        self._cbw.submit(self.on_speech_start)

    # =========================================================================
    # Audio callback  (sounddevice thread — keep minimal)
    # =========================================================================

    def _audio_callback(self, indata, frames: int, time_info, status) -> None:
        if status:
            logger.debug("[STT] sounddevice status: %s", status)

        if self._audio_paused.is_set():
            return  # TTS is speaking — discard before VAD/ASR sees it

        # sample_rate is always a positive int after __init__ — no guard needed
        if frames > 0:
            self._frame_sec = max(0.001, min(frames / float(self.sample_rate), 0.25))

        b = bytes(indata)
        try:
            self._audio_q.put_nowait(b)
        except queue.Full:
            # Drop the oldest quarter to stay real-time
            drop = max(1, (self._audio_q.maxsize or MAX_QUEUE_FRAMES) // 4)
            for _ in range(drop):
                try:
                    self._audio_q.get_nowait()
                except queue.Empty:
                    break
            try:
                self._audio_q.put_nowait(b)
            except queue.Full:
                pass

    # =========================================================================
    # Microphone open loop  (stt-listen thread)
    # =========================================================================

    def _listen_loop(self) -> None:
        candidates = self._device_candidates()
        if self.debug:
            logger.info("[STT] Input candidates: %s", candidates)

        fail_counts: dict = {}  # key: device_id or "default" -> int
        cand_i = 0

        base_kw: dict = dict(dtype="int16", channels=1, callback=self._audio_callback)

        _req_sr = self.sample_rate

        while self._running:
            if self._mic_off:
                self._mic_on_event.wait(timeout=1.0)
                continue

            device_id = candidates[cand_i % len(candidates)]
            cand_i += 1

            # query native SR for that device (or default)
            device_id, dev_sr = self._query_device_sr(device_id)

            attempt_cfgs = [
                dict(samplerate=dev_sr, blocksize=0),
                dict(samplerate=dev_sr, blocksize=DEFAULT_BLOCKSIZE),
                dict(samplerate=_req_sr, blocksize=0),
                dict(samplerate=_req_sr, blocksize=DEFAULT_BLOCKSIZE),
            ]

            kw_base = dict(base_kw)
            if device_id is not None:
                kw_base["device"] = device_id

            opened = False
            for i, cfg in enumerate(attempt_cfgs, 1):
                kw = {**kw_base, **cfg}
                try:
                    sd.check_input_settings(
                        device=kw.get("device"),
                        channels=1,
                        dtype="int16",
                        samplerate=kw["samplerate"],
                    )

                    sr = kw.get("samplerate", self.sample_rate)
                    if isinstance(sr, (int, float)) and sr > 0:
                        self.sample_rate = int(sr)

                    logger.info(
                        "[STT] Opening mic dev=%s attempt %d/%d sr=%s blocksize=%s",
                        (
                            kw.get("device")
                            if kw.get("device") is not None
                            else "default"
                        ),
                        i,
                        len(attempt_cfgs),
                        kw["samplerate"],
                        kw["blocksize"],
                    )

                    with sd.RawInputStream(**kw):
                        logger.info(
                            "[STT] Listening… dev=%s", kw.get("device", "default")
                        )
                        # reset fail count on successful open
                        key = "default" if device_id is None else device_id
                        fail_counts[key] = 0
                        self._run_vad()

                    opened = True
                    break

                except Exception:
                    logger.exception(
                        "[STT] Mic open failed dev=%s attempt %d/%d",
                        ("default" if device_id is None else device_id),
                        i,
                        len(attempt_cfgs),
                    )
                    _log_audio_devices()
                    time.sleep(0.35)

            if opened:
                continue

            # device failed all attempts
            key = "default" if device_id is None else device_id
            fail_counts[key] = fail_counts.get(key, 0) + 1
            logger.warning("[STT] Device %s failed (%d/2).", key, fail_counts[key])

            # After 2 full failures on this device, rotate to next candidate
            if fail_counts[key] < 2:
                # try same device again next loop by rewinding cand_i one step
                cand_i -= 1

            # If everything is failing repeatedly, don’t hard-exit immediately.
            # Only exit if ALL candidates have failed twice recently.
            if len(candidates) > 0:
                all_bad = True
                for c in candidates:
                    k = "default" if c is None else c
                    if fail_counts.get(k, 0) < 2:
                        all_bad = False
                        break
                if all_bad and not self._mic_off:
                    self._running = False
                    logger.error("[STT] Cannot open any microphone device — giving up.")
                    return

    # =========================================================================
    # VAD dispatch
    # =========================================================================

    def _run_vad(self) -> None:
        """Dispatch to the appropriate VAD back-end for this session."""
        if self.use_silero_vad and _SILERO_AVAILABLE:
            self._silero_loop()
        else:
            self._custom_loop()

    # =========================================================================
    # Silero VAD back-end
    # =========================================================================

    def _silero_loop(self) -> None:
        """
        Silero neural VAD back-end.

        Architecture
        ────────────
        • All audio is staged into 512-sample chunks before being fed to Silero,
          including frames that arrive during cooldown — this keeps the RNN state
          coherent so the model is never handed audio that jumps forward in time.
        • The energy pre-gate is intentionally low (5 % of start_thr) so that soft
          or distant voices reach the model rather than being silently discarded.
        • Onset debounce uses a consecutive-voiced-chunk counter instead of a wall-
          clock sustain timer.  This is more accurate for Silero because the model
          already integrates context across its RNN state.
        • below_for only accumulates on frames where inference actually ran, so
          staging latency cannot cause premature segment end.
        • stage and vad state are both cleared at segment end so each segment starts
          completely clean.
        """
        # ── Load Silero ───────────────────────────────────────────────────────
        try:
            torch.set_num_threads(1)
            vad = load_silero_vad()
            vad.reset_states()
            logger.info("[STT] Silero VAD active.")
        except Exception:
            logger.exception(
                "[STT] Silero failed to load — falling back to Custom VAD."
            )
            self.use_silero_vad = False  # Prevents infinite fallback recursion
            return  # _run_vad() will dispatch correctly on next call

        # ── Calibration ───────────────────────────────────────────────────────
        blocking_calib = self.calibration_sec > 0.0
        calib_end = time.time() + self.calibration_sec if blocking_calib else 0.0
        baseline = 0.0006
        calib_energies: List[float] = []
        calib_frames = 0
        calibrated = False

        # Snapshot sample_rate — may have been updated by stream open to dev_sr
        vad_sr = self.sample_rate  # actual stream sample rate
        # Silero requires 16kHz. If stream opened at a different rate, each frame
        # must be resampled before staging. The SILERO_CHUNK (512) is in 16kHz
        # samples; at 48kHz we need 512*(48000/16000)=1536 raw samples per chunk.
        silero_raw_chunk = int(SILERO_CHUNK * vad_sr / DEFAULT_SAMPLE_RATE)

        # ── Pre-roll — stores raw int16 OUTER FRAMES (not Silero chunks) ────────
        # preroll_cap must be based on outer-frame size (DEFAULT_BLOCKSIZE), not
        # silero_raw_chunk. At 48kHz with blocksize=512: each outer frame = 10.67ms
        # but one Silero chunk = 32ms (1536 raw samples). Using silero_raw_chunk
        # would cap the deque at only ~181ms instead of the intended PREROLL_SEC.
        preroll_cap = max(2, int(PREROLL_SEC * vad_sr / DEFAULT_BLOCKSIZE) + 2)
        preroll: Deque[np.ndarray] = deque(maxlen=preroll_cap)

        # ── Staging buffer — always consumed, never left stale ────────────────
        stage: List[np.ndarray] = []
        stage_len = 0

        # ── Recording state ───────────────────────────────────────────────────
        recording = False
        speech_start: Optional[float] = None
        last_voiced: Optional[float] = None
        audio_buf: List[np.ndarray] = []
        onset_chunks = 0
        onset_e_win: Deque[float] = deque(maxlen=ONSET_WIN_FRAMES)
        below_for = 0.0
        cooldown_until = 0.0
        speech_prob = 0.0
        recent_probs: Deque[float] = deque(maxlen=3)  # Smoothing buffer
        seg_max_prob = 0.0  # peak smooth_prob seen during the current segment
        voiced = False
        dbg_last = 0.0

        if blocking_calib:
            logger.info(
                "[STT/Silero] Calibrating for %.2fs — stay quiet ", self.calibration_sec
            )
        else:
            logger.info("[STT/Silero] Starting (background calibration)… ")

        while self._running and not self._mic_off:
            try:
                data = self._audio_q.get(timeout=AUDIO_QUEUE_TIMEOUT)
            except queue.Empty:
                continue
            if not data or self._mic_off:
                break

            pcm = np.frombuffer(data, dtype=np.int16)
            if pcm.size == 0:
                continue

            now = time.time()
            frame_sec = max(0.001, min(self._frame_sec, 0.25))

            # ── 2. Energy ────────────────────────────────────────────────────
            # Must use int64: int32 overflows at sample amplitude > 2048
            # (6% of full scale for 512 samples). Overflow → negative e →
            # energy gate always False → Silero never triggers.
            pcm64s = pcm.astype(np.int64)
            e = float(np.dot(pcm64s, pcm64s)) / pcm64s.size / (32768.0**2)

            # ── 3. Calibration ────────────────────────────────────────────────
            if not calibrated:
                calib_energies.append(e)
                baseline = baseline * (1.0 - NOISE_ALPHA_FAST) + e * NOISE_ALPHA_FAST
                calib_frames += 1
                if blocking_calib:
                    if now >= calib_end:
                        baseline = self._finalize_baseline(baseline, calib_energies)
                        calibrated = True
                        logger.info("[STT/Silero] Calibrated. baseline=%.6f ", baseline)
                    continue
                else:
                    if calib_frames >= CALIB_FRAMES:
                        baseline = self._finalize_baseline(baseline, calib_energies)
                        calibrated = True
                        logger.info("[STT/Silero] Calibrated. baseline=%.6f ", baseline)
                    if calib_frames < CALIB_GATE:
                        continue

            # ── 4. Baseline adaptation (idle + quiet only) ────────────────────
            if not recording and e < baseline * CEILING_MULT:
                alpha = NOISE_ALPHA_FAST if e > baseline else NOISE_ALPHA_SLOW
                baseline = baseline * (1.0 - alpha) + e * alpha

            adaptive_floor = max(ABS_FLOOR_ENERGY, baseline * FLOOR_MULT)
            start_thr = max(adaptive_floor, baseline * self.speech_trigger_mult)
            in_cooldown = now < cooldown_until

            # ── 5. Stage audio — ALWAYS, even during cooldown ─────────────────
            # Feeding frames to the staging buffer during cooldown keeps the RNN
            # state temporally coherent.  We run inference but ignore the result.
            stage.append(pcm)
            stage_len += pcm.size
            ran_inference = False
            any_voiced = False
            chunks_run = 0

            while stage_len >= silero_raw_chunk:
                if len(stage) == 1:
                    arr = stage[0]
                else:
                    arr = np.concatenate(stage)
                chunk_i16 = arr[:silero_raw_chunk]
                leftover = arr[silero_raw_chunk:]
                stage = [leftover] if leftover.size else []
                stage_len = leftover.size

                chunk_f32 = _resample_to_16k(chunk_i16, vad_sr)
                if len(chunk_f32) < SILERO_CHUNK:
                    chunk_f32 = np.pad(chunk_f32, (0, SILERO_CHUNK - len(chunk_f32)))
                else:
                    chunk_f32 = chunk_f32[:SILERO_CHUNK]

                if e >= start_thr * 0.25:
                    try:
                        speech_prob = float(
                            vad(torch.from_numpy(chunk_f32), DEFAULT_SAMPLE_RATE).item()
                        )
                    except Exception:
                        speech_prob = 0.0
                else:
                    # Energy too low for speech — feed to Silero for RNN continuity
                    # but force speech_prob to 0 (no trigger possible).
                    try:
                        vad(torch.from_numpy(chunk_f32), DEFAULT_SAMPLE_RATE)
                    except Exception:
                        pass
                    speech_prob = 0.0

                recent_probs.append(speech_prob)
                smooth_prob = sum(recent_probs) / len(recent_probs)  # 3-chunk smoothing

                if not in_cooldown:
                    thr = SILERO_THRESH_END if recording else self._silero_thresh_start
                    if smooth_prob >= thr:
                        any_voiced = True
                    ran_inference = True
                    chunks_run += 1
                # During cooldown: inference ran (state updated) but result ignored

            # Single voiced/unvoiced verdict for this outer frame.
            # Using any_voiced (not just the last chunk) means that if chunks 1–2
            # are voiced and chunk 3 is unvoiced, the frame correctly counts as
            # voiced — earlier results are no longer discarded.
            voiced = any_voiced

            if in_cooldown:
                continue

            # ── 6. Pre-roll + onset energy tracking (idle frames only) ──────────
            if not recording:
                preroll.append(pcm.copy())
                # Track energy variance over onset window for flatness check.
                # Mirrors Custom VAD: music has flat amplitude; speech is dynamic.
                if e >= start_thr * 0.20:
                    onset_e_win.append(e)
                elif e < start_thr * 0.10:
                    onset_e_win.clear()

            # ── 7. Debug meter ────────────────────────────────────────────────
            if self.debug and not recording and (now - dbg_last) >= 1.0:
                dbg_last = now
                logger.info(
                    "[STT/Silero] e=%.6f baseline=%.6f start_thr=%.6f prob=%.3f"
                    " voiced=%s",
                    e,
                    baseline,
                    start_thr,
                    speech_prob,
                    voiced,
                )

            # ── 8. IDLE → RECORDING ───────────────────────────────────────────
            if not recording:
                if ran_inference:
                    if voiced:
                        onset_chunks += 1
                    else:
                        onset_chunks = max(0, onset_chunks - 1)

                if onset_chunks >= self._silero_onset_chunks:
                    # Onset flatness gate — mirrors Custom VAD to reject music/hum.
                    # Speech has a dynamic amplitude envelope (ratio ≥ 1.6).
                    # Music / sustained noise has flat energy (ratio < 1.6).
                    flat_ok = True
                    oe_len = len(onset_e_win)
                    if oe_len >= ONSET_WIN_FRAMES:
                        e_max = max(onset_e_win)
                        e_min = min(onset_e_win)
                        ratio = e_max / (e_min + 1e-12)
                        flat_ok = ratio >= ONSET_FLATNESS_MAX

                    if flat_ok:
                        recording = True
                        onset_chunks = 0
                        onset_e_win.clear()
                        below_for = 0.0
                        audio_buf.clear()
                        audio_buf.extend(preroll)
                        preroll.clear()
                        speech_start = now
                        last_voiced = now
                        self._on_speech_detected()
                        if self.debug:
                            logger.info(
                                "[STT/Silero] RECORD start | e=%.6f prob=%.3f ",
                                e,
                                smooth_prob,
                            )
                    else:
                        # Flat onset → music/hum. Hard decay to prevent backlog.
                        onset_chunks = max(0, onset_chunks - 2)
                        if self.debug:
                            oe = list(onset_e_win)
                            ratio_dbg = (max(oe) / (min(oe) + 1e-12)) if oe else 0.0
                            logger.info(
                                "[STT/Silero] Onset flat (music/hum rejected)"
                                " | e=%.6f ratio=%.2f",
                                e,
                                ratio_dbg,
                            )
                continue

            # ── 9. RECORDING ──────────────────────────────────────────────────
            audio_buf.append(pcm.copy())
            if ran_inference:
                smooth_prob_now = (
                    sum(recent_probs) / len(recent_probs) if recent_probs else 0.0
                )
                seg_max_prob = max(seg_max_prob, smooth_prob_now)
                if voiced:
                    last_voiced = now
                    below_for = 0.0
                else:
                    below_for += chunks_run * (SILERO_CHUNK / DEFAULT_SAMPLE_RATE)

            time_rec = now - speech_start if speech_start else 0.0
            time_silent = now - last_voiced if last_voiced else 0.0

            if (
                time_silent >= self.silence_timeout and below_for >= STOP_SUSTAIN_SEC
            ) or time_rec >= MAX_RECORD_SEC:
                do_tx = time_rec >= MIN_SPEECH_SEC
                if do_tx:
                    clip = self._concat_i16(audio_buf)
                    do_tx = self._clip_passes_snr(clip, baseline)
                    if do_tx:
                        self._enqueue(clip, vad_sr)
                    else:
                        self._cbw.submit(self.on_segment_end)
                else:
                    self._cbw.submit(self.on_segment_end)
                if self.debug:
                    logger.info(
                        "[STT/Silero] RECORD end | dur=%.2fs max_prob=%.3f → %s ",
                        time_rec,
                        seg_max_prob,
                        "transcribe" if do_tx else "discard",
                    )
                try:
                    vad.reset_states()
                except Exception:
                    pass
                stage.clear()
                stage_len = 0
                audio_buf.clear()
                recording = False
                speech_start = None
                last_voiced = None
                onset_chunks = 0
                onset_e_win.clear()
                below_for = 0.0
                seg_max_prob = 0.0
                cooldown_until = now + COOLDOWN_SEC

    # =========================================================================
    # Custom VAD back-end
    # =========================================================================

    def _custom_loop(self) -> None:
        """
        Energy + crest-factor + ZCR + onset-flatness VAD.
        Zero extra dependencies.

        Humming / non-speech rejection
        ────────────────────────────────
        Two conditions must both hold before recording starts:

        1. Sustain gate  above_for ≥ START_SUSTAIN_SEC (80ms, ≈3 frames).
                         Energy must stay above start_thr continuously.
                         Single knocks/pops drop back to silence in <80ms so
                         above_for decays to zero before the gate can fire.

        2. Onset flatness gate  Energy max/min ratio over ONSET_WIN_FRAMES.
                         Humming has nearly constant energy → ratio < 1.6 → reject.
                         Real speech has a dynamic onset envelope → ratio ≥ 1.6.
                         Only enforced once the window is full and above_for has
                         reached START_SUSTAIN_SEC so transients are not blocked
                         by an incomplete window.

        NOTE: ZCR gate and Window gate were removed (v1.5). Both structurally
        blocked vowel-initial words (/a/, /i/, /o/) because peak energy always
        lands on vowels (ZCR 0.010–0.025 ). ZCR is still computed for
        debug logging only and has no effect on triggering.
        """
        # ── Calibration ───────────────────────────────────────────────────────
        blocking_calib = self.calibration_sec > 0.0
        calibrated = False
        calib_energies: List[float] = []
        calib_frames = 0
        calib_end = 0.0
        baseline = 0.0006

        # Snapshot: self.sample_rate was set to dev_sr when the stream opened.
        # Passing it explicitly to _enqueue avoids a race if the device reconnects
        # with a different native SR before the clip is transcribed.
        vad_sr = self.sample_rate

        if blocking_calib:
            calib_end = time.time() + self.calibration_sec
            logger.info(
                "[STT/Custom] Calibrating for %.2fs — stay quiet", self.calibration_sec
            )
        else:
            logger.info("[STT/Custom] Starting (background calibration)…")

        # ── Pre-roll ──────────────────────────────────────────────────────────
        preroll_max_samples = int(
            PREROLL_SEC * float(self.sample_rate or DEFAULT_SAMPLE_RATE)
        )
        preroll_buf: Deque[np.ndarray] = deque()
        preroll_samples = 0

        # ── Onset energy window (flatness check for hum rejection) ────────────
        onset_e_win: Deque[float] = deque(maxlen=ONSET_WIN_FRAMES)

        # ── Recording state ───────────────────────────────────────────────────
        recording = False
        speech_start: Optional[float] = None
        last_voiced: Optional[float] = None
        audio_buf: List[np.ndarray] = []
        above_for = 0.0
        below_for = 0.0
        cooldown_until = 0.0
        prev_e = 0.0
        total_frames = 0
        impulsive_frames = 0
        voiced_frames = 0
        dbg_last = 0.0

        while self._running and not self._mic_off:
            # ── 1. Fetch frame ────────────────────────────────────────────────
            try:
                data = self._audio_q.get(timeout=AUDIO_QUEUE_TIMEOUT)
            except queue.Empty:
                continue
            if not data or self._mic_off:
                break

            pcm = np.frombuffer(data, dtype=np.int16)
            if pcm.size == 0:
                continue

            now = time.time()
            frame_sec = max(0.001, min(self._frame_sec, 0.25))

            # ── 2. Energy ─────────────────────────────────────────────────────
            pcm64 = pcm.astype(np.int64)
            e = float(np.dot(pcm64, pcm64)) / pcm64.size / (32768.0**2)

            # ── 3. Peak / crest factor ────────────────────────────────────────
            mx = int(np.max(pcm))
            mn = int(np.min(pcm))
            peak = float(mx if mx >= -mn else -mn) / 32768.0
            rms = e**0.5
            crest = (peak / (rms + 1e-9)) if rms > 0.0 else 999.0

            # ── 4. Attack spike ───────────────────────────────────────────────
            attack = e / (prev_e + 1e-12) if prev_e > 0.0 else 1.0
            prev_e = e
            attacky = attack >= ATTACK_RATIO

            # ── 5. Impulsive frame detection ──────────────────────────────────
            crest_thr = IMPULSE_CREST * (IDLE_CREST_MULT if not recording else 1.0)
            impulsive = (crest >= crest_thr) or (
                attacky and crest >= crest_thr * ATTACK_CREST_MULT
            )

            # ── 6. Pre-roll maintenance (idle only) ───────────────────────────
            if not recording:
                preroll_buf.append(pcm.copy())
                preroll_samples += pcm.size
                while preroll_samples > preroll_max_samples and preroll_buf:
                    preroll_samples -= preroll_buf.popleft().size

            # ── 7. Calibration ────────────────────────────────────────────────
            if not calibrated:
                calib_energies.append(e)
                alpha = NOISE_ALPHA_FAST if e > baseline else NOISE_ALPHA_SLOW
                baseline = baseline * (1.0 - alpha) + e * alpha
                calib_frames += 1
                if blocking_calib:
                    if now >= calib_end:
                        baseline = self._finalize_baseline(baseline, calib_energies)
                        prev_e = baseline  # prevents first post-calib frame looking like an attack
                        calibrated = True
                        logger.info(
                            "[STT/Custom] Calibration done. baseline=%.6f", baseline
                        )
                    continue
                else:
                    if calib_frames >= CALIB_FRAMES:
                        baseline = self._finalize_baseline(baseline, calib_energies)
                        prev_e = baseline  # prevents first post-calib frame looking like an attack
                        calibrated = True
                        logger.info(
                            "[STT/Custom] Background calibration done. baseline=%.6f",
                            baseline,
                        )

            # ── 8. Baseline adaptation (idle + quiet; runs during cooldown too) ──
            if not recording and e < baseline * CEILING_MULT:
                alpha = NOISE_ALPHA_FAST if e > baseline else NOISE_ALPHA_SLOW
                baseline = baseline * (1.0 - alpha) + e * alpha

            # ── 9. Cooldown gate ──────────────────────────────────────────────
            if now < cooldown_until:
                continue

            adaptive_floor = max(ABS_FLOOR_ENERGY, baseline * FLOOR_MULT)
            start_thr = max(adaptive_floor, baseline * self.speech_trigger_mult)
            # keep_thr: level below which a frame counts as silence for hangover.
            # 0.35 × start_thr ≈ 1.05 × baseline — only true silence accumulates.
            # Inter-word pauses and stop-consonant closures (~1.5–3× baseline)
            # stay above this and keep last_voiced alive through the sentence.
            keep_thr = start_thr * 0.35

            # Early exit during initial calibration window — skip ZCR and
            # window updates so unstable early frames never bias onset_e_win.
            if calib_frames < CALIB_GATE and not recording:
                continue

            # ── 10. ZCR — debug diagnostics only (zero-cost when debug=False) ──
            # ZCR is NOT used in any trigger or keep decision.
            # Vowels (ZCR 0.010–0.025) would block vowel-initial words if gated.
            # Hum rejection is handled by onset flatness (energy envelope).
            zcr = 0.0
            if self.debug and e >= start_thr * 0.20:
                n = pcm.size
                zcr = (
                    float(np.count_nonzero(np.signbit(pcm[:-1]) ^ np.signbit(pcm[1:])))
                    / (n - 1)
                    if n > 1
                    else 0.0
                )

            if not recording:
                # onset_e_win: track energy ramp for flatness check
                if e >= start_thr * 0.20:
                    onset_e_win.append(e)

            # ── 11. Debug meter ───────────────────────────────────────────────
            if self.debug and not recording and (now - dbg_last) >= 1.0:
                dbg_last = now
                logger.info(
                    "[STT/Custom] e=%.6f baseline=%.6f thr=%.6f "
                    "crest=%.1f atk=%.1f zcr=%.3f above=%.3f",
                    e,
                    baseline,
                    start_thr,
                    crest,
                    attack,
                    zcr,
                    above_for,
                )

            # ── 12. IDLE → RECORDING ──────────────────────────────────────────
            if not recording:
                # Sustain condition: energy above threshold.
                # speech_like (ZCR) intentionally excluded — vowel-initial words
                # have ZCR 0.010-0.025 (below 0.036) and would reset
                # above_for to 0 on every frame, so they never trigger.
                # No per-frame ZCR gate — flat_ok handles hum rejection.
                # above_for decays (not hard-resets) on sub-threshold frames so
                # one quiet inter-word gap does not wipe accumulated evidence.
                if e >= start_thr:
                    above_for += frame_sec
                else:
                    # Hard reset only on true silence — decay on borderline frames
                    above_for = max(0.0, above_for - frame_sec * 2)
                    if e < start_thr * 0.20:
                        onset_e_win.clear()  # reset flatness window on true silence

                # ── Gate: Onset flatness (hum rejection) ───────────────────────
                # Humming has flat sustained energy (max/min ratio < 1.6).
                # Real speech has a dynamic envelope → ratio ≥ 1.6.
                # Applied only once we have a full window and the sustain timer
                # has elapsed so we are definitely looking at continuous sound.
                # NOTE: ZCR window gate (win_ok) was removed — it structurally
                # fails for vowel-initial words because peak energy always lands
                # on vowels (ZCR 0.010-0.025), causing win_ok
                # to read 0% at trigger time for any vowel-heavy onset.
                flat_ok = True
                oe_len = len(onset_e_win)
                if oe_len >= ONSET_WIN_FRAMES and above_for >= START_SUSTAIN_SEC:
                    e_max = max(onset_e_win)
                    e_min = min(onset_e_win)
                    ratio = e_max / (e_min + 1e-12)
                    # Fallback: allow high-energy monotone speech if significantly above baseline
                    if (
                        ratio < ONSET_FLATNESS_MAX
                        and e > baseline * self.speech_trigger_mult * 1.5
                    ):
                        flat_ok = True
                    else:
                        flat_ok = ratio >= ONSET_FLATNESS_MAX

                if above_for >= START_SUSTAIN_SEC and flat_ok:
                    recording = True
                    above_for = 0.0
                    below_for = 0.0
                    total_frames = 0
                    impulsive_frames = 0
                    voiced_frames = 0
                    audio_buf.clear()
                    while preroll_buf:
                        audio_buf.append(preroll_buf.popleft())
                    preroll_samples = 0
                    onset_e_win.clear()
                    speech_start = now
                    last_voiced = now
                    self._on_speech_detected()
                    if self.debug:
                        logger.info(
                            "[STT/Custom] RECORD start | e=%.6f thr=%.6f crest=%.1f"
                            " zcr=%.3f",
                            e,
                            start_thr,
                            crest,
                            zcr,
                        )
                elif above_for >= START_SUSTAIN_SEC and not flat_ok:
                    # Energy was high enough but onset is too flat — humming/drone.
                    if self.debug:
                        logger.info(
                            "[STT/Custom] Onset rejected (flat energy) | "
                            "e=%.6f zcr=%.3f flat_ok=%s",
                            e,
                            zcr,
                            flat_ok,
                        )
                    # Full reset — user must break and restart.
                    above_for = 0.0
                    onset_e_win.clear()
                continue

            # ── 13. RECORDING ─────────────────────────────────────────────────
            audio_buf.append(pcm.copy())
            total_frames += 1
            if impulsive:
                impulsive_frames += 1
            # speech_frames removed: ZCR misclassifies vowels as non-speech.
            # voiced_frames (energy-based) is used for debug instead.

            # During recording: voiced = energy above keep_thr and not impulsive.
            # speech_like (ZCR gate) is intentionally NOT checked here —
            # 0.036 ZCR threshold rejects pure vowels (ZCR 0.010–0.025) which are
            # obviously real speech. The ZCR gate is for onset detection only.
            if (e >= keep_thr) and not impulsive:
                last_voiced = now
                below_for = 0.0
                if self.debug:
                    voiced_frames += 1
            else:
                below_for += frame_sec

            time_rec = now - speech_start if speech_start else 0.0
            time_silent = now - last_voiced if last_voiced else 0.0

            if (
                below_for >= STOP_SUSTAIN_SEC and time_silent >= self.silence_timeout
            ) or time_rec >= MAX_RECORD_SEC:
                imp_r = impulsive_frames / total_frames if total_frames else 0.0

                # is_knock: reject clips that are dominated by impulsive frames
                # (knocks, taps, pops). Uses only the impulsive ratio — NOT
                # speech_r, because ZCR misclassifies vowels as non-speech
                # making legitimate short utterances look like knocks.
                is_knock = (
                    (time_rec < 0.40 and imp_r >= 0.60)
                    or (time_rec < 0.55 and imp_r >= 0.45)
                    or (time_rec < 0.80 and imp_r >= 0.35)
                )
                do_tx = time_rec >= MIN_SPEECH_SEC and not is_knock

                if do_tx:
                    clip = self._concat_i16(audio_buf)
                    do_tx = self._clip_passes_snr(clip, baseline)
                    if do_tx:
                        self._enqueue(clip, vad_sr)
                    else:
                        self._cbw.submit(self.on_segment_end)
                else:
                    self._cbw.submit(self.on_segment_end)

                if self.debug:
                    voice_r = voiced_frames / total_frames if total_frames else 0.0
                    logger.info(
                        "[STT/Custom] RECORD end | dur=%.2fs imp=%.2f voice=%.2f → %s",
                        time_rec,
                        imp_r,
                        voice_r,
                        "transcribe" if do_tx else "discard",
                    )

                audio_buf.clear()
                recording = False
                speech_start = None
                last_voiced = None
                above_for = 0.0
                below_for = 0.0
                cooldown_until = now + COOLDOWN_SEC
                # Clear windows so stale idle-silence values from this
                # gap does not inflate the onset window on the next utterance.
                onset_e_win.clear()

    # =========================================================================
    # Transcription worker  (stt-tx thread)
    # =========================================================================

    def _enqueue(self, clip: np.ndarray, sr: int) -> None:
        """Push a (clip, sample_rate) pair to the transcription queue, dropping oldest if full."""
        if clip.size == 0:
            return
        item: Tuple[np.ndarray, int] = (clip, sr)
        while True:
            try:
                self._tx_q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._tx_q.get_nowait()
                except queue.Empty:
                    pass

    def _transcribe_worker(self) -> None:
        """
        Serial STT transcription worker (Whisper or Parakeet, per active backend).

        Parks on _mic_on_event while mic is off → zero CPU / GPU usage.
        Processes one clip at a time; the queue depth (8) prevents accumulation.
        """
        while not self._tx_stop.is_set():
            # Park while muted — wait wakes instantly on unmute()
            if self._mic_off:
                self._mic_on_event.wait(timeout=1.0)
                continue

            try:
                item = self._tx_q.get(timeout=0.25)
            except queue.Empty:
                continue

            if item is None:
                break
            clip, clip_sr = item
            if clip.size == 0:
                self._cbw.submit(self.on_segment_end)
                continue

            text = self._transcribe(clip, clip_sr)
            if text:
                self._cbw.submit(self.on_text, text)
            # Always signal segment done so the mic UI returns to idle,
            # even when Whisper returns empty (noise, hallucination filter, etc.)
            self._cbw.submit(self.on_segment_end)

    def _transcribe(self, clip_i16: np.ndarray, sr: int) -> str:
        """Dispatch transcription to the active STT backend."""
        if self._stt_backend == "parakeet_onnx":
            return self._transcribe_parakeet(clip_i16, sr)
        return self._transcribe_whisper(clip_i16, sr)

    def _transcribe_whisper(self, clip_i16: np.ndarray, sr: int) -> str:
        """Convert int16 PCM to float32, resample to 16kHz if needed, run Whisper.
        sr must be the sample rate at which the clip was recorded (snapshotted at
        VAD loop start), NOT self.sample_rate which may have changed on reconnect."""
        model = self._whisper  # snapshot — mic_off() may set to None mid-inference
        if model is None:
            return ""

        audio = _resample_to_16k(clip_i16, sr)
        try:
            segs, _ = model.transcribe(
                audio,
                language=self._language_norm,
                beam_size=self.beam_size,
                vad_filter=self.whisper_vad_filter,
            )
            text = " ".join(s.text.strip() for s in segs if s.text.strip())
            if not text:
                return ""
            return _apply_hallucination_filter(text)
        except Exception:
            logger.exception("[STT] Whisper transcription failed")
            return ""

    def _transcribe_parakeet(self, clip_i16: np.ndarray, sr: int) -> str:
        """Resample int16 PCM to 16kHz float32, run Parakeet TDT ONNX inference."""
        model = self._parakeet  # snapshot — mic_off() may set to None mid-inference
        if model is None:
            return ""

        audio = _resample_to_16k(clip_i16, sr)
        try:
            text = model.recognize(audio).strip()
            if not text:
                return ""
            return _apply_hallucination_filter(text)
        except Exception:
            logger.exception("[STT] Parakeet transcription failed")
            return ""

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _clip_passes_snr(self, clip: np.ndarray, baseline: float) -> bool:
        """
        Return False if the clip's mean energy is too close to the noise floor.

        Rejects far-field / ambient speech that was not directed at the mic.
        The SNR ratio is energy-based (same metric as the VAD baseline), so a
        value of 5.0 means the clip must be 5× louder than ambient noise in
        mean-squared energy.  Close speech is typically 50–200×; far-field
        speech from the next table is typically 2–6×.

        Disabled when min_clip_snr ≤ 0.
        """
        if self.min_clip_snr <= 0.0 or clip.size == 0:
            return True
        clip_f = clip.astype(np.float32)
        clip_e = float(np.dot(clip_f, clip_f)) / clip.size / (32768.0**2)
        ratio = clip_e / (baseline + 1e-12)
        if ratio < self.min_clip_snr:
            logger.debug(
                "[STT] SNR gate rejected clip: ratio=%.1f < min=%.1f",
                ratio,
                self.min_clip_snr,
            )
            return False
        return True

    @staticmethod
    def _finalize_baseline(baseline: float, energies: List[float]) -> float:
        """Take the max of the EMA baseline and the median of collected samples."""
        if not energies:
            return baseline
        try:
            return max(
                baseline, float(np.median(np.asarray(energies, dtype=np.float32)))
            )
        except Exception:
            return baseline

    @staticmethod
    def _concat_i16(chunks: List[np.ndarray]) -> np.ndarray:
        """Concatenate a list of int16 arrays into one, with a single np.concatenate call."""
        if not chunks:
            return np.empty(0, dtype=np.int16)
        if len(chunks) == 1:
            return chunks[0]
        return np.concatenate(chunks)

    def _drain_queue(self, q: "queue.Queue") -> None:
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    # Virtual/aggregate device name fragments that cause AUHAL errors when
    # PortAudio tries to open them as raw input streams.
    _VIRTUAL_DEVICE_FRAGMENTS = (
        "teams",
        "zoom",
        "meet",
        "slack",
        "discord",
        "webex",
        "virtual",
        "aggregate",
        "multi-output",
        "blackhole",
        "loopback",
        "soundflower",
        "cables",
    )

    def _list_input_devices(self) -> List[Tuple[int, dict]]:
        """Return [(global_index, device_dict)] for real input-capable devices.

        Skips virtual / aggregate devices (Teams, Zoom, etc.) that have input
        channels on paper but fail with AUHAL errors when opened as raw streams.
        """
        out: List[Tuple[int, dict]] = []
        try:
            devs = sd.query_devices()

            for i, d in enumerate(devs):
                if not isinstance(d, dict):
                    continue
                if int(d.get("max_input_channels", 0)) <= 0:
                    continue
                # skip virtual/aggregate devices
                name_lower = str(d.get("name", "")).lower()
                if any(frag in name_lower for frag in self._VIRTUAL_DEVICE_FRAGMENTS):
                    logger.debug(
                        "[STT] Skipping virtual device idx=%d name=%r", i, d.get("name")
                    )
                    continue
                out.append((i, d))
        except Exception:
            logger.debug("[STT] Failed to query devices", exc_info=True)
        return out

    def _resolve_device(self) -> Optional[int]:
        """
        Resolve microphone_index robustly.

        Behavior:
        - None -> OS default
        - int  -> treated as "input-device list index" FIRST (0..N-1),
                if out of range then treated as global device index
        """
        if self.microphone_index is None:
            return None

        try:
            mi = int(self.microphone_index)
        except Exception:
            logger.warning(
                "[STT] microphone_index=%r invalid — using OS default.",
                self.microphone_index,
            )
            self.microphone_index = None
            return None

        # Build input-capable device list: [(global_idx, device_dict), ...]
        inputs: List[Tuple[int, dict]] = []
        try:
            devs = sd.query_devices()

            for gi, d in enumerate(devs):
                if isinstance(d, dict) and int(d.get("max_input_channels", 0)) > 0:
                    inputs.append((gi, d))
        except Exception:
            logger.debug("[STT] Failed to query devices", exc_info=True)

        # 1) Interpret as input-device list index (what users usually mean)
        if 0 <= mi < len(inputs):
            global_idx = inputs[mi][0]
            name = inputs[mi][1].get("name", "unknown")
            logger.info(
                "[STT] microphone_index=%d mapped to input device global=%d name=%r",
                mi,
                global_idx,
                name,
            )
            return global_idx

        # 2) Interpret as global device index
        try:
            dev = sd.query_devices(mi)
            if isinstance(dev, dict) and int(dev.get("max_input_channels", 0)) > 0:
                logger.info(
                    "[STT] microphone_index=%d using global input device name=%r",
                    mi,
                    dev.get("name", "unknown"),
                )
                return mi
            logger.warning(
                "[STT] microphone_index=%d exists but is not an input device"
                " (max_input_channels=%s) — using OS default.",
                mi,
                dev.get("max_input_channels") if isinstance(dev, dict) else None,
            )
        except Exception:
            pass

        logger.warning(
            "[STT] microphone_index=%r invalid — using OS default.",
            self.microphone_index,
        )
        self.microphone_index = None
        return None

    def _device_candidates(self) -> List[Optional[int]]:
        """
        Candidate order:
        1) requested device (resolved) if any
        2) OS default (None)
        3) all other input devices (global indices)
        """
        requested = self._resolve_device()
        candidates: List[Optional[int]] = []
        if requested is not None:
            candidates.append(requested)

        candidates.append(None)  # OS default as a fallback

        for idx, _d in self._list_input_devices():
            if idx != requested:
                candidates.append(idx)

        # de-dupe preserving order
        seen = set()
        out: List[Optional[int]] = []
        for c in candidates:
            key = "default" if c is None else c
            if key not in seen:
                seen.add(key)
                out.append(c)
        return out

    def _query_device_sr(self, device_id: Optional[int]) -> Tuple[Optional[int], int]:
        """Query the device's native sample rate; fall back to self.sample_rate."""
        # dict.fromkeys preserves insertion order and deduplicates — avoids querying
        # None twice when device_id is already None.
        for dev_arg in dict.fromkeys((device_id, None)):
            try:
                dev = sd.query_devices(dev_arg, "input")
                if isinstance(dev, dict):
                    sr = int(dev.get("default_samplerate", self.sample_rate))
                    name = dev.get("name", "unknown")
                    logger.info(
                        "[STT] Input device=%s name=%r sr=%s", dev_arg, name, sr
                    )
                    return dev_arg, sr
            except Exception:
                if dev_arg is not None:
                    logger.warning(
                        "[STT] Could not query device=%r — trying OS default.", dev_arg
                    )
        logger.warning(
            "[STT] Could not query any input device — using configured sr=%s.",
            self.sample_rate,
        )
        return None, self.sample_rate


# =============================================================================
# Live test
# =============================================================================


def live_test() -> None:
    """
    Interactive CLI test.  Reads settings from buddy.toml when available,
    then falls back to built-in defaults.

    Controls
    ─────────
      Enter      toggle mute / unmute
      Ctrl+C     stop

    Backend is printed at startup.  Override from buddy.toml:
      [voice]
      stt_backend = "faster_whisper"   # force Whisper
      stt_backend = "parakeet_onnx"    # force Parakeet (default)
    """
    import sys

    # ── Load config (best-effort) ─────────────────────────────────────────────
    _cfg: dict = {}
    try:
        if sys.version_info >= (3, 11):
            import tomllib as _toml
        else:
            import tomli as _toml  # type: ignore
        _cfg_path = Path(__file__).resolve().parent.parent / "config" / "buddy.toml"
        if _cfg_path.exists():
            with open(_cfg_path, "rb") as _f:
                _raw = _toml.load(_f)
            _buddy = _raw.get("buddy", _raw) if isinstance(_raw, dict) else {}
            _cfg = _buddy.get("voice", {}) if isinstance(_buddy, dict) else {}
    except Exception:
        pass

    # ── Settings (config → fallback) ─────────────────────────────────────────
    stt_backend = str(_cfg.get("stt_backend", "parakeet_onnx"))
    parakeet_model = str(_cfg.get("parakeet_model", "nemo-parakeet-tdt-0.6b-v2"))
    whisper_size = str(_cfg.get("whisper_model_size", "small"))
    beam_size = int(_cfg.get("beam_size", 4))
    silence_timeout = float(_cfg.get("silence_timeout", HANGOVER_SEC))
    trigger_mult = float(_cfg.get("speech_trigger_mult", 3.0))
    use_silero = bool(_cfg.get("use_silero_vad", True))
    enable_beep = bool(_cfg.get("enable_beep", True))
    calib_sec = float(_cfg.get("calibration_sec", 2.0))
    mic_index = int(_cfg.get("microphone_index", -1))
    min_clip_snr = float(_cfg.get("min_clip_snr", 4.0))
    silero_thresh_start = float(_cfg.get("silero_thresh_start", SILERO_THRESH_START))
    silero_onset_chunks = int(_cfg.get("silero_onset_chunks", SILERO_ONSET_CHUNKS))

    resolved = _resolve_backend(stt_backend)

    # ── Header ────────────────────────────────────────────────────────────────
    backend_label = (
        f"Parakeet TDT  [{parakeet_model}]"
        if resolved == "parakeet_onnx"
        else f"Whisper  [{whisper_size}]"
    )
    vad_label = "Silero" if (use_silero and _SILERO_AVAILABLE) else "Custom"
    snr_label = f"{min_clip_snr:.1f}×" if min_clip_snr > 0 else "off"

    print()
    print("━" * 56)
    print("  STT live test")
    print(f"  backend  : {backend_label}")
    print(
        f"  vad      : {vad_label}  (thresh={silero_thresh_start:.2f}"
        f" onset={silero_onset_chunks})"
    )
    print(f"  snr gate : {snr_label}")
    print(f"  silence  : {silence_timeout}s  |  trigger × {trigger_mult}")
    print("  controls : Enter = mute/unmute   Ctrl+C = stop")
    print("━" * 56)
    print()

    # ── State for display ─────────────────────────────────────────────────────
    _t0 = [0.0]  # mutable ref so inner callbacks can update it

    def on_speech_start() -> None:
        _t0[0] = time.perf_counter()
        print("● recording…", end="  ", flush=True)

    def on_text(text: str) -> None:
        elapsed = time.perf_counter() - _t0[0]
        print(f"\r▶  {text}  [{elapsed:.2f}s]\n", flush=True)

    def on_segment_end() -> None:
        # Only print idle if on_text didn't already print (i.e. empty result)
        pass

    def on_interrupt() -> None:
        pass  # speech_start already covers this

    # ── Build SpeechToText ────────────────────────────────────────────────────
    stt = SpeechToText(
        stt_backend=stt_backend,
        parakeet_model=parakeet_model,
        whisper_model_size=whisper_size,
        calibration_sec=calib_sec,
        beam_size=beam_size,
        whisper_vad_filter=bool(_cfg.get("whisper_vad_filter", True)),
        speech_trigger_mult=trigger_mult,
        silence_timeout=silence_timeout,
        microphone_index=mic_index if mic_index >= 0 else None,
        use_silero_vad=use_silero,
        enable_beep=enable_beep,
        min_clip_snr=min_clip_snr,
        silero_thresh_start=silero_thresh_start,
        silero_onset_chunks=silero_onset_chunks,
        on_text=on_text,
        on_interrupt=on_interrupt,
        on_speech_start=on_speech_start,
        on_segment_end=on_segment_end,
        debug=False,
    )

    print(f"  active backend: {stt._stt_backend}")
    print("  calibrating…\n")

    stt.start()

    # ── Enter-key mute toggle ─────────────────────────────────────────────────
    def _toggle() -> None:
        while stt._running:
            try:
                input()
            except EOFError:
                break
            if stt.is_mic_off:
                stt.mic_on()
                print("▶  mic on — recalibrating…\n")
            else:
                stt.mic_off()
                print("■  mic off — model offloaded.\n")

    threading.Thread(target=_toggle, daemon=True, name="live-mic").start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n■  stopping…")
        stt.stop()
        print("done.")


if __name__ == "__main__":
    live_test()
