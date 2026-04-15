# buddy/buddy_core/model_selector.py
#
# Hardware-aware LLM model selection.
#
# First boot  → interactive prompt (hardware summary + all viable model+quant combos) → saved
# Subsequent  → loads saved choice silently, no prompt
# force_reselect=True → re-prompts even if a choice exists
#
# Model catalog: Qwen3.5 (newest, vision) > Qwen3 > Qwen2.5
# Each model size is shown with ALL quantizations that fit the hardware.
# Recommendation: largest model where Q5_K_M fits comfortably.
#
# Public API:
#   get_or_select_llm_model(os_profile, config_dir, show_ui, force_reselect) -> LLMOption

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from buddy.logger.logger import get_logger

logger = get_logger("model_selector")


# ==========================================================
# Data types
# ==========================================================


class HardwareTier(str, Enum):
    LOW   = "low"    # ≤ 8 GB effective memory
    MID   = "mid"    # 9–16 GB
    HIGH  = "high"   # 17–40 GB
    ULTRA = "ultra"  # 40 GB+


# Family priority — higher = preferred when hardware allows
_FAMILY_PRIORITY = {"Qwen3.5": 3, "Qwen3": 2, "Qwen2.5": 1}

# Quant preference order for recommendation (balance of quality vs speed)
# Q5_K_M = sweet spot, Q6_K = quality, Q4_K_M = minimum, Q8_0 = max quality
_QUANT_QUALITY: Dict[str, int] = {
    "Q8_0":   4,
    "Q6_K":   3,
    "Q5_K_M": 2,
    "Q4_K_M": 1,
    "IQ4_XS": 0,
}
_QUANT_DISPLAY_ORDER = ["Q4_K_M", "Q5_K_M", "Q6_K", "Q8_0"]


@dataclass
class LLMOption:
    tier: HardwareTier
    family: str           # "Qwen3.5" | "Qwen3" | "Qwen2.5"
    filename: str         # local GGUF filename
    hf_repo: str          # HuggingFace repo ID
    hf_filename: str      # filename inside the HF repo
    label: str            # shown in selection UI
    size_gb: float        # approximate disk size
    min_ram_gb: float     # minimum RAM to run comfortably
    description: str = ""
    vision_capable: bool = False
    mmproj_hf_repo: str = ""
    mmproj_hf_filename: str = ""
    mmproj_size_gb: float = 0.0
    # Internal: display_name groups quants together in the UI
    display_name: str = ""   # e.g. "Qwen3.5-9B"
    quant: str = ""          # e.g. "Q5_K_M"


# ==========================================================
# Compact base-model + quant-spec definitions
# ==========================================================


@dataclass
class _ModelBase:
    """Family/size metadata, quant-agnostic."""
    family: str
    display_name: str          # "Qwen3.5-9B"
    hf_repo: str
    description: str
    tier: HardwareTier
    vision_capable: bool = False
    mmproj_hf_repo: str = ""
    mmproj_hf_filename: str = ""
    mmproj_size_gb: float = 0.0


@dataclass
class _QuantSpec:
    quant: str           # "Q4_K_M" | "Q5_K_M" | "Q6_K" | "Q8_0"
    size_gb: float       # GGUF size on disk
    min_ram_gb: float    # min RAM to run comfortably (model + KV cache + OS)
    hf_filename: str     # exact filename in the HF repo


# ── Quant specs per model ──────────────────────────────────────────────────────
# min_ram_gb = size_gb + ~3 GB overhead (OS + KV cache at default ctx)

_QUANT_SPECS: Dict[str, List[_QuantSpec]] = {

    # ── Qwen3.5 (unsloth repos, CamelCase filenames) ────────────────────────
    "Qwen3.5-4B": [
        _QuantSpec("Q4_K_M", 2.8,  5.5,  "Qwen3.5-4B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 3.4,  6.5,  "Qwen3.5-4B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   3.9,  7.0,  "Qwen3.5-4B-Q6_K.gguf"),
        _QuantSpec("Q8_0",   5.2,  8.5,  "Qwen3.5-4B-Q8_0.gguf"),
    ],
    "Qwen3.5-9B": [
        _QuantSpec("Q4_K_M", 5.5,  10.0, "Qwen3.5-9B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 6.2,  11.0, "Qwen3.5-9B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   7.4,  12.0, "Qwen3.5-9B-Q6_K.gguf"),
        _QuantSpec("Q8_0",   9.8,  14.0, "Qwen3.5-9B-Q8_0.gguf"),
    ],
    "Qwen3.5-27B": [
        _QuantSpec("Q4_K_M", 16.5, 20.0, "Qwen3.5-27B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 19.8, 24.0, "Qwen3.5-27B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   23.4, 28.0, "Qwen3.5-27B-Q6_K.gguf"),
        _QuantSpec("Q8_0",   29.0, 34.0, "Qwen3.5-27B-Q8_0.gguf"),
    ],
    "Qwen3.5-35B-A3B": [
        _QuantSpec("Q4_K_M", 22.0, 26.0, "Qwen3.5-35B-A3B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 26.0, 30.0, "Qwen3.5-35B-A3B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   30.0, 35.0, "Qwen3.5-35B-A3B-Q6_K.gguf"),
        _QuantSpec("Q8_0",   40.0, 46.0, "Qwen3.5-35B-A3B-Q8_0.gguf"),
    ],

    # ── Qwen3 (official Qwen repos, CamelCase filenames) ───────────────────
    "Qwen3-4B": [
        _QuantSpec("Q4_K_M", 2.5,  5.5,  "Qwen3-4B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 3.0,  6.5,  "Qwen3-4B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   3.6,  7.0,  "Qwen3-4B-Q6_K.gguf"),
        _QuantSpec("Q8_0",   4.7,  8.5,  "Qwen3-4B-Q8_0.gguf"),
    ],
    "Qwen3-8B": [
        _QuantSpec("Q4_K_M", 4.9,  8.5,  "Qwen3-8B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 5.6,  9.5,  "Qwen3-8B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   6.6,  10.5, "Qwen3-8B-Q6_K.gguf"),
        _QuantSpec("Q8_0",   8.8,  13.0, "Qwen3-8B-Q8_0.gguf"),
    ],
    "Qwen3-14B": [
        _QuantSpec("Q4_K_M", 8.6,  13.0, "Qwen3-14B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 10.2, 15.0, "Qwen3-14B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   12.0, 17.0, "Qwen3-14B-Q6_K.gguf"),
        _QuantSpec("Q8_0",   15.7, 20.0, "Qwen3-14B-Q8_0.gguf"),
    ],
    "Qwen3-30B-A3B": [
        _QuantSpec("Q4_K_M", 17.5, 22.0, "Qwen3-30B-A3B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 20.5, 26.0, "Qwen3-30B-A3B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   24.0, 30.0, "Qwen3-30B-A3B-Q6_K.gguf"),
    ],
    "Qwen3-32B": [
        _QuantSpec("Q4_K_M", 19.8, 24.0, "Qwen3-32B-Q4_K_M.gguf"),
        _QuantSpec("Q5_K_M", 23.5, 28.0, "Qwen3-32B-Q5_K_M.gguf"),
        _QuantSpec("Q6_K",   27.5, 33.0, "Qwen3-32B-Q6_K.gguf"),
        _QuantSpec("Q8_0",   34.0, 40.0, "Qwen3-32B-Q8_0.gguf"),
    ],

    # ── Qwen2.5 (official Qwen repos, lowercase filenames) ─────────────────
    "Qwen2.5-3B": [
        _QuantSpec("Q4_K_M", 2.0,  5.0,  "qwen2.5-3b-instruct-q4_k_m.gguf"),
        _QuantSpec("Q5_K_M", 2.4,  6.0,  "qwen2.5-3b-instruct-q5_k_m.gguf"),
        _QuantSpec("Q6_K",   2.8,  6.5,  "qwen2.5-3b-instruct-q6_k.gguf"),
        _QuantSpec("Q8_0",   3.7,  7.5,  "qwen2.5-3b-instruct-q8_0.gguf"),
    ],
    "Qwen2.5-7B": [
        _QuantSpec("Q4_K_M", 4.4,  8.0,  "qwen2.5-7b-instruct-q4_k_m.gguf"),
        _QuantSpec("Q5_K_M", 5.1,  9.0,  "qwen2.5-7b-instruct-q5_k_m.gguf"),
        _QuantSpec("Q6_K",   6.0,  10.0, "qwen2.5-7b-instruct-q6_k.gguf"),
        _QuantSpec("Q8_0",   7.7,  12.0, "qwen2.5-7b-instruct-q8_0.gguf"),
    ],
    "Qwen2.5-14B": [
        _QuantSpec("Q4_K_M", 8.6,  13.0, "qwen2.5-14b-instruct-q4_k_m.gguf"),
        _QuantSpec("Q5_K_M", 10.2, 15.0, "qwen2.5-14b-instruct-q5_k_m.gguf"),
        _QuantSpec("Q6_K",   12.0, 17.0, "qwen2.5-14b-instruct-q6_k.gguf"),
        _QuantSpec("Q8_0",   15.7, 20.0, "qwen2.5-14b-instruct-q8_0.gguf"),
    ],
    "Qwen2.5-32B": [
        _QuantSpec("Q4_K_M", 19.8, 24.0, "qwen2.5-32b-instruct-q4_k_m.gguf"),
        _QuantSpec("Q5_K_M", 23.5, 28.0, "qwen2.5-32b-instruct-q5_k_m.gguf"),
        _QuantSpec("Q6_K",   27.5, 33.0, "qwen2.5-32b-instruct-q6_k.gguf"),
    ],
    "Qwen2.5-72B": [
        _QuantSpec("Q4_K_M", 43.0, 48.0, "qwen2.5-72b-instruct-q4_k_m.gguf"),
    ],
}

# ── Base model definitions (order = display preference within each family) ────

_MODEL_BASES: List[_ModelBase] = [

    # ── Qwen3.5 (newest, vision-capable) ───────────────────────────────────
    _ModelBase(
        family="Qwen3.5", display_name="Qwen3.5-4B",
        hf_repo="unsloth/Qwen3.5-4B-GGUF",
        description="Newest family, vision-capable. Ideal for 6–10 GB systems.",
        tier=HardwareTier.LOW,
        vision_capable=True,
        mmproj_hf_repo="unsloth/Qwen3.5-4B-GGUF",
        mmproj_hf_filename="mmproj-F16.gguf",
        mmproj_size_gb=0.67,
    ),
    _ModelBase(
        family="Qwen3.5", display_name="Qwen3.5-9B",
        hf_repo="unsloth/Qwen3.5-9B-GGUF",
        description="Best balance of quality and speed for 10–18 GB systems.",
        tier=HardwareTier.MID,
        vision_capable=True,
        mmproj_hf_repo="unsloth/Qwen3.5-9B-GGUF",
        mmproj_hf_filename="mmproj-F16.gguf",
        mmproj_size_gb=0.90,
    ),
    _ModelBase(
        family="Qwen3.5", display_name="Qwen3.5-27B",
        hf_repo="unsloth/Qwen3.5-27B-GGUF",
        description="Excellent reasoning and vision for 20–36 GB systems.",
        tier=HardwareTier.HIGH,
        vision_capable=True,
        mmproj_hf_repo="unsloth/Qwen3.5-27B-GGUF",
        mmproj_hf_filename="mmproj-F16.gguf",
        mmproj_size_gb=0.93,
    ),
    _ModelBase(
        family="Qwen3.5", display_name="Qwen3.5-35B-A3B",
        hf_repo="unsloth/Qwen3.5-35B-A3B-GGUF",
        description="MoE 35B (3B active). Fast inference, high capability. Needs 26 GB+.",
        tier=HardwareTier.ULTRA,
        vision_capable=True,
        mmproj_hf_repo="unsloth/Qwen3.5-35B-A3B-GGUF",
        mmproj_hf_filename="mmproj-F16.gguf",
        mmproj_size_gb=0.88,
    ),

    # ── Qwen3 ───────────────────────────────────────────────────────────────
    _ModelBase(
        family="Qwen3", display_name="Qwen3-4B",
        hf_repo="Qwen/Qwen3-4B-GGUF",
        description="Compact and capable. Good for 6–10 GB systems.",
        tier=HardwareTier.LOW,
    ),
    _ModelBase(
        family="Qwen3", display_name="Qwen3-8B",
        hf_repo="Qwen/Qwen3-8B-GGUF",
        description="Strong reasoning for 9–14 GB systems.",
        tier=HardwareTier.LOW,
    ),
    _ModelBase(
        family="Qwen3", display_name="Qwen3-14B",
        hf_repo="Qwen/Qwen3-14B-GGUF",
        description="Best quality/speed Qwen3 for 13–22 GB systems.",
        tier=HardwareTier.MID,
    ),
    _ModelBase(
        family="Qwen3", display_name="Qwen3-30B-A3B",
        hf_repo="Qwen/Qwen3-30B-A3B-GGUF",
        description="MoE 30B (3B active). Efficient for 22–32 GB systems.",
        tier=HardwareTier.HIGH,
    ),
    _ModelBase(
        family="Qwen3", display_name="Qwen3-32B",
        hf_repo="Qwen/Qwen3-32B-GGUF",
        description="Near-GPT-4 reasoning. For 24–42 GB systems.",
        tier=HardwareTier.HIGH,
    ),

    # ── Qwen2.5 (stable, strong baseline) ──────────────────────────────────
    _ModelBase(
        family="Qwen2.5", display_name="Qwen2.5-3B",
        hf_repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
        description="Minimal. For very constrained systems (4–6 GB).",
        tier=HardwareTier.LOW,
    ),
    _ModelBase(
        family="Qwen2.5", display_name="Qwen2.5-7B",
        hf_repo="Qwen/Qwen2.5-7B-Instruct-GGUF",
        description="Solid instruction following for 8–13 GB systems.",
        tier=HardwareTier.LOW,
    ),
    _ModelBase(
        family="Qwen2.5", display_name="Qwen2.5-14B",
        hf_repo="Qwen/Qwen2.5-14B-Instruct-GGUF",
        description="Strong Qwen2.5 baseline for 13–22 GB systems.",
        tier=HardwareTier.MID,
    ),
    _ModelBase(
        family="Qwen2.5", display_name="Qwen2.5-32B",
        hf_repo="Qwen/Qwen2.5-32B-Instruct-GGUF",
        description="Maximum Qwen2.5 quality for 24–36 GB systems.",
        tier=HardwareTier.HIGH,
    ),
    _ModelBase(
        family="Qwen2.5", display_name="Qwen2.5-72B",
        hf_repo="Qwen/Qwen2.5-72B-Instruct-GGUF",
        description="Largest model. Mac Pro / Studio Ultra only (48 GB+).",
        tier=HardwareTier.ULTRA,
    ),
]


def _make_option(base: _ModelBase, spec: _QuantSpec) -> LLMOption:
    label = (
        f"{base.display_name:<20s} {spec.quant:<8s}"
        f"  {spec.size_gb:>4.1f} GB disk  min {spec.min_ram_gb:.0f} GB RAM"
    )
    return LLMOption(
        tier=base.tier,
        family=base.family,
        filename=spec.hf_filename,
        hf_repo=base.hf_repo,
        hf_filename=spec.hf_filename,
        label=label,
        size_gb=spec.size_gb,
        min_ram_gb=spec.min_ram_gb,
        description=base.description,
        vision_capable=base.vision_capable,
        mmproj_hf_repo=base.mmproj_hf_repo,
        mmproj_hf_filename=base.mmproj_hf_filename,
        mmproj_size_gb=base.mmproj_size_gb,
        display_name=base.display_name,
        quant=spec.quant,
    )


# Full flat catalog — all models × all quants. Generated once at import.
QWEN_MODEL_CATALOG: List[LLMOption] = [
    _make_option(base, spec)
    for base in _MODEL_BASES
    for spec in _QUANT_SPECS.get(base.display_name, [])
]

# Backward-compat alias
QWEN3_OPTIONS = QWEN_MODEL_CATALOG

# QUANT_CATALOG: kept for vision_selector (Qwen3.5 quant sizes for RAM comparison).
# Maps "Qwen3.5-{size}" → list of QuantOption (same data as _QUANT_SPECS above).
@dataclass
class QuantOption:
    quant: str
    size_gb: float
    min_ram_gb: float
    hf_filename: str

QUANT_CATALOG: Dict[str, List[QuantOption]] = {
    key: [QuantOption(s.quant, s.size_gb, s.min_ram_gb, s.hf_filename) for s in specs]
    for key, specs in _QUANT_SPECS.items()
    if key.startswith("Qwen3.5")
}

QWEN35_MMPROJ_SIZE_GB = 0.67       # fallback default
QWEN35_MMPROJ_HF_FILENAME = "mmproj-F16.gguf"


# ==========================================================
# Hardware scoring
# ==========================================================


def _effective_ram(os_profile: Dict[str, Any]) -> float:
    """Return effective RAM in GB for model sizing decisions."""
    gpu  = os_profile.get("gpu",  {}) or {}
    ram  = os_profile.get("ram",  {}) or {}
    total_ram_gb: float = float(ram.get("total_gb") or 0)
    backend: str = str(gpu.get("backend") or "cpu_only")
    if backend == "metal":
        return total_ram_gb                       # Apple Silicon — unified memory
    if backend in ("cuda", "rocm"):
        vram_gb = float(gpu.get("total_vram_gb") or 0)
        return vram_gb if vram_gb > 0 else total_ram_gb
    return total_ram_gb                           # CPU-only


def score_hardware(os_profile: Dict[str, Any]) -> HardwareTier:
    effective_gb = _effective_ram(os_profile)
    if effective_gb >= 40:
        return HardwareTier.ULTRA
    if effective_gb >= 17:
        return HardwareTier.HIGH
    if effective_gb >= 9:
        return HardwareTier.MID
    return HardwareTier.LOW


def recommend_llm(os_profile: Dict[str, Any]) -> LLMOption:
    """
    Return the best-balanced LLMOption for this hardware.

    Strategy:
      1. Compute usable RAM = effective_ram - overhead (3 GB min, or 12%).
      2. Among options that fit (min_ram_gb ≤ effective_ram), prefer:
           - Newest family (Qwen3.5 > Qwen3 > Qwen2.5)
           - Largest model that fits with headroom
           - Q5_K_M if it fits comfortably, otherwise Q4_K_M
    """
    effective_gb = _effective_ram(os_profile)
    overhead_gb  = max(3.0, effective_gb * 0.12)
    usable_gb    = effective_gb - overhead_gb

    # All options that will actually run (may be tight)
    viable = [o for o in QWEN_MODEL_CATALOG if o.min_ram_gb <= effective_gb]
    if not viable:
        return QWEN_MODEL_CATALOG[0]

    # Preferred quants in order: Q5_K_M (balanced), Q6_K (quality), Q4_K_M (fast)
    preferred_quants = ["Q5_K_M", "Q6_K", "Q4_K_M", "Q8_0"]

    # Group by (family_priority, model_size_score) descending, pick best quant per model
    best: Optional[LLMOption] = None
    best_score: Tuple = (-1, -1, -1)

    for opt in viable:
        family_score = _FAMILY_PRIORITY.get(opt.family, 0)
        # Use size_gb as a proxy for model quality (bigger = better capability)
        # Use Q4_K_M size as the canonical model size (strip quant variation)
        q4_size = next(
            (o.size_gb for o in QWEN_MODEL_CATALOG
             if o.display_name == opt.display_name and o.quant == "Q4_K_M"),
            opt.size_gb,
        )
        quant_quality = _QUANT_QUALITY.get(opt.quant, 0)
        # Prefer options that fit comfortably (within usable_gb)
        comfortable = 1 if opt.min_ram_gb <= usable_gb else 0
        # Prefer specific quants in our preferred order
        quant_pref = len(preferred_quants) - preferred_quants.index(opt.quant) \
            if opt.quant in preferred_quants else 0

        score = (comfortable, family_score, q4_size, quant_pref, quant_quality)
        if score > best_score:
            best_score = score
            best = opt

    return best or viable[0]


def viable_options(os_profile: Dict[str, Any]) -> List[LLMOption]:
    """
    Return all model+quant combinations the hardware can run, sorted for display.

    Sort order: family priority (Qwen3.5→Qwen3→Qwen2.5),
                then model size descending within family,
                then quant quality ascending within model (Q4→Q5→Q6→Q8).
    """
    effective_gb = _effective_ram(os_profile)
    opts = [o for o in QWEN_MODEL_CATALOG if o.min_ram_gb <= effective_gb]
    if not opts:
        # Absolute fallback: show the smallest model at Q4_K_M
        fallback = next(
            (o for o in QWEN_MODEL_CATALOG if o.quant == "Q4_K_M"),
            QWEN_MODEL_CATALOG[0],
        )
        return [fallback]

    def _sort_key(o: LLMOption):
        family_score = _FAMILY_PRIORITY.get(o.family, 0)
        q4_size = next(
            (x.size_gb for x in QWEN_MODEL_CATALOG
             if x.display_name == o.display_name and x.quant == "Q4_K_M"),
            o.size_gb,
        )
        quant_order = _QUANT_DISPLAY_ORDER.index(o.quant) \
            if o.quant in _QUANT_DISPLAY_ORDER else len(_QUANT_DISPLAY_ORDER)
        return (family_score, q4_size, -quant_order)  # quants ascending within model

    opts.sort(key=_sort_key, reverse=True)
    return opts


# ==========================================================
# Persistence
# ==========================================================


def _toml_val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_toml_section(toml_path: Path, section: str, data: Dict[str, Any]) -> None:
    """Upsert a flat [section] block in a TOML file."""
    import re as _re
    try:
        text = toml_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    text = _re.sub(rf"\n*(# auto-managed\n)?\[{_re.escape(section)}\][^\[]*", "", text)
    text = text.rstrip() + "\n"
    lines = [f"\n# auto-managed\n[{section}]"]
    lines += [f"{k} = {_toml_val(v)}" for k, v in data.items()]
    toml_path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")


def _load_saved_choice(config_dir: Path) -> Optional[LLMOption]:
    p = config_dir / "buddy.toml"
    if not p.exists():
        return None
    try:
        if sys.version_info >= (3, 11):
            import tomllib as _t  # type: ignore
        else:
            import tomli as _t  # type: ignore  # noqa: PLC0415
        with p.open("rb") as f:
            raw = _t.load(f)
        saved = raw.get("model_choice", {})
        if not isinstance(saved, dict) or not saved.get("filename"):
            return None
        # Match by filename against full catalog
        for opt in QWEN_MODEL_CATALOG:
            if opt.filename == saved["filename"]:
                logger.info("Loaded saved model choice: %s  %s", opt.display_name, opt.quant)
                return opt
        # Unknown / custom GGUF — reconstruct synthetic option
        return LLMOption(
            tier=HardwareTier.MID,
            family="custom",
            filename=saved["filename"],
            hf_repo=saved.get("hf_repo", ""),
            hf_filename=saved.get("hf_filename", saved["filename"]),
            label=saved.get("label", saved["filename"]),
            size_gb=0.0,
            min_ram_gb=0.0,
            description="Custom model (user-defined)",
            display_name=saved.get("label", saved["filename"]),
            quant="",
        )
    except Exception as ex:
        logger.warning("Could not load model_choice from buddy.toml: %r", ex)
        return None


def _save_choice(config_dir: Path, option: LLMOption) -> None:
    toml_path = config_dir / "buddy.toml"
    data = {
        "filename":  option.filename,
        "hf_repo":   option.hf_repo,
        "hf_filename": option.hf_filename,
        "label":     option.label,
        "family":    option.family,
        "display_name": option.display_name,
        "quant":     option.quant,
        "tier":      option.tier.value,
        "saved_at":  datetime.utcnow().isoformat() + "Z",
    }
    try:
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        _write_toml_section(toml_path, "model_choice", data)
        logger.info("Saved model choice: %s %s", option.display_name, option.quant)
    except Exception as ex:
        logger.warning("Could not save model_choice to buddy.toml: %r", ex)


# ==========================================================
# Interactive selection UI
# ==========================================================


def _c_local(s: str, color: str) -> str:
    try:
        from buddy.ui.boot_ui import _c
        return _c(s, color)
    except Exception:
        return s


_W = 76  # box inner width


def _box(text: str = "") -> str:
    visible = len(text.encode("ascii", errors="ignore"))
    pad = max(0, _W - visible)
    return f"  ║  {text}{' ' * pad}║"


def _box_top() -> str:
    return f"  ╔{'═' * (_W + 2)}╗"


def _box_div() -> str:
    return f"  ╠{'═' * (_W + 2)}╣"


def _box_bot() -> str:
    return f"  ╚{'═' * (_W + 2)}╝"


def _box_model(name: str, description: str) -> str:
    label = f"  ─── {name}  "
    desc  = _c_local(description, "dim")
    pad   = _W + 2 - len(label) - len(description)
    return f"  ║{label}{desc}{' ' * max(0, pad)}║"


def _print_selection_ui(
    os_profile: Dict[str, Any],
    options: List[LLMOption],
    recommended: LLMOption,
) -> None:
    gpu   = os_profile.get("gpu",   {}) or {}
    ram   = os_profile.get("ram",   {}) or {}
    cpu   = os_profile.get("cpu",   {}) or {}
    macos = os_profile.get("macos", {}) or {}

    effective_gb = _effective_ram(os_profile)
    ram_gb   = ram.get("total_gb", "?")
    cores    = cpu.get("logical_cores", "?")
    gpu_name = gpu.get("name", "unknown")
    gpu_back = gpu.get("backend", "cpu_only")
    vram_gb  = gpu.get("total_vram_gb", None)
    mac_ver  = macos.get("product_version", "")
    vram_str = f"{vram_gb} GB VRAM" if vram_gb else "unified memory"

    print()
    print(_box_top())
    print(_box("  " + _c_local("BUDDY — MODEL SELECTION", "accent")))
    print(_box_div())
    print(_box(f"  GPU      {_c_local(str(gpu_name), 'warn')}"))
    print(_box(f"  Backend  {_c_local(gpu_back, 'dim')}  ({vram_str})"))
    print(_box(f"  RAM      {_c_local(str(ram_gb) + ' GB total', 'warn')}"
               f"  ·  {_c_local(str(round(effective_gb, 1)) + ' GB effective', 'ok')}"))
    print(_box(f"  Cores    {cores}"))
    if mac_ver:
        print(_box(f"  macOS    {mac_ver}"))
    print(_box_div())
    print(_box("  " + _c_local("Choose your LLM:", "tagline")
               + "  " + _c_local("(↑ better quality / ↑ higher quant = more RAM used)", "dim")))
    print(_box())

    # Group by model display_name — show quants as sub-entries
    seen_models: List[str] = []
    seen_families: List[str] = []

    for i, opt in enumerate(options, start=1):
        # Family divider
        if opt.family not in seen_families:
            seen_families.append(opt.family)
            fam_label = opt.family
            if opt.family == "Qwen3.5":
                fam_label += "  " + _c_local("(newest · vision-capable)", "dim")
            elif opt.family == "Qwen3":
                fam_label += "  " + _c_local("(strong reasoning)", "dim")
            else:
                fam_label += "  " + _c_local("(stable baseline)", "dim")
            pad = _W + 2 - 4 - len(opt.family) - 2
            print(f"  ║  {_c_local('─── ' + fam_label, 'accent')}"
                  f"{'─' * max(1, _W - 5 - len(opt.family) - 2 - (20 if opt.family == 'Qwen3.5' else 18 if opt.family == 'Qwen3' else 16))}║")

        # Model-size separator (first quant of a new model size)
        if opt.display_name not in seen_models:
            seen_models.append(opt.display_name)
            print(_box())
            print(_box(f"     {_c_local(opt.display_name, 'key')}"
                       f"  {_c_local(opt.description, 'dim')}"))

        is_rec = opt.filename == recommended.filename
        rec_tag = "  " + _c_local("★ RECOMMENDED", "ok") if is_rec else ""
        quant_color = (
            "ok" if opt.quant in ("Q6_K", "Q8_0") else
            "warn" if opt.quant == "Q5_K_M" else "dim"
        )
        line = (
            f"    [{i:>2}]  {_c_local(opt.quant, quant_color):<8}"
            f"  {opt.size_gb:>4.1f} GB disk  min {opt.min_ram_gb:.0f} GB RAM"
            f"{rec_tag}"
        )
        print(_box(line))

    print(_box())
    print(_box_bot())
    print()


def _prompt_user_choice(
    options: List[LLMOption],
    recommended: LLMOption,
) -> LLMOption:
    rec_idx = next(
        (i for i, o in enumerate(options, 1) if o.filename == recommended.filename),
        1,
    )
    while True:
        try:
            raw = input(
                _c_local("  ▸ ", "accent")
                + f"Enter [1–{len(options)}] or press Enter for recommended "
                + _c_local(f"[{rec_idx}]", "ok")
                + ": "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return recommended

        if raw == "":
            return recommended
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            chosen = options[int(raw) - 1]
            print()
            print(
                f"  {_c_local('✓', 'ok')}  Selected: "
                f"{_c_local(chosen.display_name, 'key')}  "
                f"{_c_local(chosen.quant, 'warn')}"
                f"  ({chosen.size_gb:.1f} GB)"
            )
            print()
            return chosen
        print(f"  Please enter a number between 1 and {len(options)}.")


def _run_interactive_selection(
    os_profile: Dict[str, Any],
    config_dir: Path,
) -> LLMOption:
    recommended = recommend_llm(os_profile)
    options = viable_options(os_profile)

    _print_selection_ui(os_profile, options, recommended)
    chosen = _prompt_user_choice(options, recommended)
    _save_choice(config_dir, chosen)
    return chosen


# ==========================================================
# Public API
# ==========================================================


def get_or_select_llm_model(
    os_profile: Dict[str, Any],
    *,
    config_dir: Path,
    show_ui: bool = True,
    force_reselect: bool = False,
) -> LLMOption:
    """
    Return the LLMOption to use this session.

    - First boot (no saved choice) → interactive selection → saved to buddy.toml [model_choice]
    - Subsequent boots → loads saved choice silently
    - force_reselect=True → always prompts even if a choice exists

    Args:
        os_profile:     Full OS profile dict from bootstrap.
        config_dir:     ~/.buddy/config  (where buddy.toml lives)
        show_ui:        Whether to print the selection UI
        force_reselect: Force the interactive prompt
    """
    if not force_reselect:
        saved = _load_saved_choice(config_dir)
        if saved is not None:
            return saved

    if show_ui:
        return _run_interactive_selection(os_profile, config_dir)

    # Headless / no UI — use hardware recommendation directly and save it
    chosen = recommend_llm(os_profile)
    _save_choice(config_dir, chosen)
    return chosen
