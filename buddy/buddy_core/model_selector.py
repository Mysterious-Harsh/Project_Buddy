# buddy/buddy_core/model_selector.py
#
# Hardware-aware LLM model selection.
#
# First boot  → interactive prompt (hardware summary + curated options) → saved to model_choice.json
# Subsequent  → loads saved choice silently, no prompt
# force_reselect=True → re-prompts even if a choice exists
#
# Model catalog priority within same tier: Qwen3.5 > Qwen3 > Qwen2.5
#
# Public API:
#   get_or_select_llm_model(os_profile, config_dir, show_ui, force_reselect) -> LLMOption

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    vision_capable: bool = False  # True for Qwen3.5 (native multimodal, no VL suffix needed)
    # mmproj (multimodal projector) — required for vision with llama-server
    mmproj_hf_repo: str = ""       # HuggingFace repo for mmproj GGUF
    mmproj_hf_filename: str = ""   # mmproj filename inside HF repo
    mmproj_size_gb: float = 0.0    # approximate mmproj disk size (F16 ~0.67 GB)


# ==========================================================
# Quantization catalog  (Qwen3.5 — approximate sizes in GB)
# Used by vision selector to show RAM comparison per quant.
# ==========================================================

@dataclass
class QuantOption:
    quant: str          # "Q4_K_M" | "Q5_K_M" | "Q6_K" | "Q8_0"
    size_gb: float      # approximate GGUF size on disk
    min_ram_gb: float   # minimum RAM to run comfortably
    hf_filename: str    # filename inside the HF repo


# Model-size → quant options  (4B, 9B, 27B, 35B-A3B)
QUANT_CATALOG: Dict[str, List[QuantOption]] = {
    "Qwen3.5-4B": [
        QuantOption("Q4_K_M", 2.8,  5.0,  "Qwen3.5-4B-Q4_K_M.gguf"),
        QuantOption("Q5_K_M", 3.4,  6.0,  "Qwen3.5-4B-Q5_K_M.gguf"),
        QuantOption("Q6_K",   3.9,  6.5,  "Qwen3.5-4B-Q6_K.gguf"),
        QuantOption("Q8_0",   5.2,  8.0,  "Qwen3.5-4B-Q8_0.gguf"),
    ],
    "Qwen3.5-9B": [
        QuantOption("Q4_K_M", 5.5,  10.0, "Qwen3.5-9B-Q4_K_M.gguf"),
        QuantOption("Q5_K_M", 6.2,  11.0, "Qwen3.5-9B-Q5_K_M.gguf"),
        QuantOption("Q6_K",   7.4,  12.0, "Qwen3.5-9B-Q6_K.gguf"),
        QuantOption("Q8_0",   9.8,  14.0, "Qwen3.5-9B-Q8_0.gguf"),
    ],
    "Qwen3.5-27B": [
        QuantOption("Q4_K_M", 16.5, 20.0, "Qwen3.5-27B-Q4_K_M.gguf"),
        QuantOption("Q5_K_M", 19.8, 24.0, "Qwen3.5-27B-Q5_K_M.gguf"),
        QuantOption("Q6_K",   23.4, 28.0, "Qwen3.5-27B-Q6_K.gguf"),
        QuantOption("Q8_0",   29.0, 34.0, "Qwen3.5-27B-Q8_0.gguf"),
    ],
    "Qwen3.5-35B-A3B": [
        QuantOption("Q4_K_M", 22.0, 26.0, "Qwen3.5-35B-A3B-Q4_K_M.gguf"),
        QuantOption("Q5_K_M", 26.0, 30.0, "Qwen3.5-35B-A3B-Q5_K_M.gguf"),
        QuantOption("Q6_K",   30.0, 35.0, "Qwen3.5-35B-A3B-Q6_K.gguf"),
        QuantOption("Q8_0",   40.0, 46.0, "Qwen3.5-35B-A3B-Q8_0.gguf"),
    ],
}

# mmproj sizes per Qwen3.5 model (F16 variant, each model has its own mmproj):
#   4B  → 0.67 GB  (672 MB)
#   9B  → 0.90 GB  (918 MB)
#   27B → 0.93 GB  (928 MB)
#   35B-A3B → 0.88 GB  (899 MB)
# Each model's mmproj_size_gb is set in its LLMOption entry above.
# This default is used as a fallback only (e.g. unknown/custom model).
QWEN35_MMPROJ_SIZE_GB = 0.67        # fallback default — use LLMOption.mmproj_size_gb
QWEN35_MMPROJ_HF_FILENAME = "mmproj-F16.gguf"


# ==========================================================
# Curated model catalog
# Order within each tier: Qwen3.5 first, then Qwen3, then Qwen2.5
# ==========================================================

QWEN_MODEL_CATALOG: List[LLMOption] = [

    # ── LOW tier  (≤ 8 GB effective RAM) ──────────────────────────────────

    LLMOption(
        tier=HardwareTier.LOW,
        family="Qwen3.5",
        filename="Qwen3.5-4B-Q4_K_M.gguf",
        hf_repo="unsloth/Qwen3.5-4B-GGUF",
        hf_filename="Qwen3.5-4B-Q4_K_M.gguf",
        label="Qwen3.5-4B   Q4_K_M    latest·vision  (5 GB RAM min)",
        size_gb=2.8,
        min_ram_gb=5.0,
        description="Newest family. Vision-capable. Good for 6–8 GB systems.",
        vision_capable=True,
        mmproj_hf_repo="unsloth/Qwen3.5-4B-GGUF",
        mmproj_hf_filename="mmproj-F16.gguf",
        mmproj_size_gb=0.67,
    ),
    LLMOption(
        tier=HardwareTier.LOW,
        family="Qwen3",
        filename="Qwen3-4B-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen3-4B-GGUF",
        hf_filename="Qwen3-4B-Q4_K_M.gguf",
        label="Qwen3-4B   Q4_K_M    lightweight    (6 GB RAM min)",
        size_gb=2.5,
        min_ram_gb=6.0,
        description="Compact. Good for 6–8 GB systems. Reasonable quality.",
    ),
    LLMOption(
        tier=HardwareTier.LOW,
        family="Qwen3",
        filename="Qwen3-8B-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen3-8B-GGUF",
        hf_filename="Qwen3-8B-Q4_K_M.gguf",
        label="Qwen3-8B   Q4_K_M    fast response  (8 GB RAM min)",
        size_gb=4.9,
        min_ram_gb=8.0,
        description="Best Qwen3 for 8 GB systems. Snappy, solid quality.",
    ),
    LLMOption(
        tier=HardwareTier.LOW,
        family="Qwen2.5",
        filename="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen2.5-7B-Instruct-GGUF",
        hf_filename="qwen2.5-7b-instruct-q4_k_m.gguf",
        label="Qwen2.5-7B Q4_K_M    fallback       (8 GB RAM min)",
        size_gb=4.4,
        min_ram_gb=8.0,
        description="Qwen2.5 fallback for 8 GB. Solid instruction following.",
    ),
    LLMOption(
        tier=HardwareTier.LOW,
        family="Qwen2.5",
        filename="Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
        hf_filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        label="Qwen2.5-3B Q4_K_M    minimal        (4 GB RAM min)",
        size_gb=2.0,
        min_ram_gb=4.0,
        description="Absolute minimum. 4 GB RAM. Limited capability.",
    ),

    # ── MID tier  (9–16 GB effective RAM) ─────────────────────────────────

    LLMOption(
        tier=HardwareTier.MID,
        family="Qwen3.5",
        filename="Qwen3.5-9B-Q4_K_M.gguf",
        hf_repo="unsloth/Qwen3.5-9B-GGUF",
        hf_filename="Qwen3.5-9B-Q4_K_M.gguf",
        label="Qwen3.5-9B  Q4_K_M   latest·fast    (10 GB RAM min)",
        size_gb=5.5,
        min_ram_gb=10.0,
        description="Newest family. Great for 10–16 GB. Fast and capable.",
        vision_capable=True,
        mmproj_hf_repo="unsloth/Qwen3.5-9B-GGUF",
        mmproj_hf_filename="mmproj-F16.gguf",
        mmproj_size_gb=0.90,
    ),
    LLMOption(
        tier=HardwareTier.MID,
        family="Qwen3",
        filename="Qwen3-14B-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen3-14B-GGUF",
        hf_filename="Qwen3-14B-Q4_K_M.gguf",
        label="Qwen3-14B  Q4_K_M    balanced       (16 GB RAM min)",
        size_gb=8.6,
        min_ram_gb=16.0,
        description="Best quality/speed for 16 GB unified memory.",
    ),
    LLMOption(
        tier=HardwareTier.MID,
        family="Qwen2.5",
        filename="Qwen2.5-14B-Instruct-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen2.5-14B-Instruct-GGUF",
        hf_filename="qwen2.5-14b-instruct-q4_k_m.gguf",
        label="Qwen2.5-14B Q4_K_M   fallback       (16 GB RAM min)",
        size_gb=8.6,
        min_ram_gb=16.0,
        description="Qwen2.5 fallback for 16 GB. Strong instruction quality.",
    ),

    # ── HIGH tier  (17–40 GB effective RAM) ───────────────────────────────

    LLMOption(
        tier=HardwareTier.HIGH,
        family="Qwen3.5",
        filename="Qwen3.5-27B-Q4_K_M.gguf",
        hf_repo="unsloth/Qwen3.5-27B-GGUF",
        hf_filename="Qwen3.5-27B-Q4_K_M.gguf",
        label="Qwen3.5-27B Q4_K_M   latest·capable (32 GB RAM min)",
        size_gb=16.5,
        min_ram_gb=32.0,
        description="Newest family at 27B. Excellent reasoning for 32 GB systems.",
        vision_capable=True,
        mmproj_hf_repo="unsloth/Qwen3.5-27B-GGUF",
        mmproj_hf_filename="mmproj-F16.gguf",
        mmproj_size_gb=0.93,
    ),
    LLMOption(
        tier=HardwareTier.HIGH,
        family="Qwen3",
        filename="Qwen3-32B-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen3-32B-GGUF",
        hf_filename="Qwen3-32B-Q4_K_M.gguf",
        label="Qwen3-32B  Q4_K_M    powerful       (32 GB RAM min)",
        size_gb=19.8,
        min_ram_gb=32.0,
        description="32B Qwen3. Near-GPT4 reasoning for high-memory Macs.",
    ),
    LLMOption(
        tier=HardwareTier.HIGH,
        family="Qwen3",
        filename="Qwen3-14B-Q8_0.gguf",
        hf_repo="Qwen/Qwen3-14B-GGUF",
        hf_filename="Qwen3-14B-Q8_0.gguf",
        label="Qwen3-14B  Q8_0      high precision (32 GB RAM min)",
        size_gb=15.7,
        min_ram_gb=32.0,
        description="Higher precision 14B. Sharper reasoning on 32 GB systems.",
    ),
    LLMOption(
        tier=HardwareTier.HIGH,
        family="Qwen2.5",
        filename="Qwen2.5-32B-Instruct-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen2.5-32B-Instruct-GGUF",
        hf_filename="qwen2.5-32b-instruct-q4_k_m.gguf",
        label="Qwen2.5-32B Q4_K_M   fallback       (32 GB RAM min)",
        size_gb=19.8,
        min_ram_gb=32.0,
        description="Qwen2.5 32B fallback. Strong baseline for 32 GB.",
    ),

    # ── ULTRA tier  (40 GB+ effective RAM) ────────────────────────────────

    LLMOption(
        tier=HardwareTier.ULTRA,
        family="Qwen3.5",
        filename="Qwen3.5-35B-A3B-Q4_K_M.gguf",
        hf_repo="unsloth/Qwen3.5-35B-A3B-GGUF",
        hf_filename="Qwen3.5-35B-A3B-Q4_K_M.gguf",
        label="Qwen3.5-35B-A3B Q4   latest·MoE     (24 GB RAM min)",
        size_gb=22.0,
        min_ram_gb=24.0,
        description="MoE 35B — active params 3B. Fast inference, high capability.",
        vision_capable=True,
        mmproj_hf_repo="unsloth/Qwen3.5-35B-A3B-GGUF",
        mmproj_hf_filename="mmproj-F16.gguf",
        mmproj_size_gb=0.88,
    ),
    LLMOption(
        tier=HardwareTier.ULTRA,
        family="Qwen3",
        filename="Qwen3-30B-A3B-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen3-30B-A3B-GGUF",
        hf_filename="Qwen3-30B-A3B-Q4_K_M.gguf",
        label="Qwen3-30B-A3B Q4    MoE·efficient  (32 GB RAM min)",
        size_gb=17.5,
        min_ram_gb=32.0,
        description="MoE 30B — active params 3B. Fast, smart, memory efficient.",
    ),
    LLMOption(
        tier=HardwareTier.ULTRA,
        family="Qwen2.5",
        filename="Qwen2.5-72B-Instruct-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen2.5-72B-Instruct-GGUF",
        hf_filename="qwen2.5-72b-instruct-q4_k_m.gguf",
        label="Qwen2.5-72B Q4_K_M  maximum        (48 GB RAM min)",
        size_gb=43.0,
        min_ram_gb=48.0,
        description="Largest model. Mac Pro/Studio Ultra only. Maximum capability.",
    ),
]

# Backward-compat alias (boot.py uses QWEN3_OPTIONS in _load_saved_choice)
QWEN3_OPTIONS = QWEN_MODEL_CATALOG


# ==========================================================
# Hardware scoring
# ==========================================================


def score_hardware(os_profile: Dict[str, Any]) -> HardwareTier:
    """
    Derive a HardwareTier from the OS profile.

    Apple Silicon  → unified memory = both RAM and VRAM → use total RAM.
    Discrete GPU   → use VRAM; fall back to system RAM if not reported.
    CPU-only       → use system RAM.
    """
    gpu = os_profile.get("gpu", {}) or {}
    ram = os_profile.get("ram", {}) or {}

    total_ram_gb: float = float(ram.get("total_gb") or 0)
    backend: str = str(gpu.get("backend") or "cpu_only")

    if backend == "metal":
        effective_gb = total_ram_gb
    elif backend in ("cuda", "rocm"):
        vram_gb = float(gpu.get("total_vram_gb") or 0)
        effective_gb = vram_gb if vram_gb > 0 else total_ram_gb
    else:
        effective_gb = total_ram_gb

    if effective_gb >= 40:
        return HardwareTier.ULTRA
    if effective_gb >= 17:
        return HardwareTier.HIGH
    if effective_gb >= 9:
        return HardwareTier.MID
    return HardwareTier.LOW


def recommend_llm(os_profile: Dict[str, Any]) -> LLMOption:
    """
    Return the best LLMOption the hardware can run.

    Within each tier, prefers newest family: Qwen3.5 > Qwen3 > Qwen2.5.
    Walks from detected tier downward until a fitting option is found.
    """
    ram_gb = float(os_profile.get("ram", {}).get("total_gb") or 0)
    tier = score_hardware(os_profile)
    tier_order = [
        HardwareTier.ULTRA,
        HardwareTier.HIGH,
        HardwareTier.MID,
        HardwareTier.LOW,
    ]
    start = tier_order.index(tier)

    for t in tier_order[start:]:
        candidates = [
            opt for opt in QWEN_MODEL_CATALOG
            if opt.tier == t and ram_gb >= opt.min_ram_gb
        ]
        if candidates:
            # Sort by family priority descending — Qwen3.5 first
            candidates.sort(
                key=lambda o: _FAMILY_PRIORITY.get(o.family, 0),
                reverse=True,
            )
            return candidates[0]

    return QWEN_MODEL_CATALOG[0]  # absolute fallback


def viable_options(os_profile: Dict[str, Any]) -> List[LLMOption]:
    """
    Return all models the hardware can run, sorted for display.
    Order: tier (HIGH→LOW), then family priority (Qwen3.5→Qwen3→Qwen2.5).
    """
    ram_gb = float(os_profile.get("ram", {}).get("total_gb") or 0)
    tier_rank = {
        HardwareTier.ULTRA: 3,
        HardwareTier.HIGH: 2,
        HardwareTier.MID: 1,
        HardwareTier.LOW: 0,
    }
    opts = [o for o in QWEN_MODEL_CATALOG if ram_gb >= o.min_ram_gb]
    if not opts:
        opts = [QWEN_MODEL_CATALOG[0]]
    opts.sort(
        key=lambda o: (
            tier_rank.get(o.tier, 0),
            _FAMILY_PRIORITY.get(o.family, 0),
        ),
        reverse=True,
    )
    return opts


# ==========================================================
# Persistence
# ==========================================================

_CHOICE_FILE = "model_choice.json"


def _load_saved_choice(config_dir: Path) -> Optional[LLMOption]:
    p = config_dir / _CHOICE_FILE
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict) or not saved.get("filename"):
            return None
        # Match against full catalog
        for opt in QWEN_MODEL_CATALOG:
            if opt.filename == saved["filename"]:
                logger.info("Loaded saved model choice: %s", opt.label)
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
        )
    except Exception as ex:
        logger.warning("Could not load model_choice.json: %r", ex)
        return None


def _save_choice(config_dir: Path, option: LLMOption) -> None:
    p = config_dir / _CHOICE_FILE
    data = {
        "filename": option.filename,
        "hf_repo": option.hf_repo,
        "hf_filename": option.hf_filename,
        "label": option.label,
        "family": option.family,
        "tier": option.tier.value,
        "saved_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved model choice: %s", option.filename)
    except Exception as ex:
        logger.warning("Could not save model_choice.json: %r", ex)


# ==========================================================
# Interactive selection UI
# ==========================================================


def _c_local(s: str, color: str) -> str:
    try:
        from buddy.ui.boot_ui import _c
        return _c(s, color)
    except Exception:
        return s


def _center_local(s: str, w: int) -> str:
    try:
        from buddy.ui.boot_ui import _center_visible
        return _center_visible(s, w)
    except Exception:
        return s


_W = 72  # box inner width


def _box(text: str = "") -> str:
    return f"  ║  {text:<{_W}}║"


def _box_top() -> str:
    return f"  ╔{'═' * (_W + 2)}╗"


def _box_div() -> str:
    return f"  ╠{'═' * (_W + 2)}╣"


def _box_bot() -> str:
    return f"  ╚{'═' * (_W + 2)}╝"


def _box_family(name: str) -> str:
    label = f"  ─── {name} "
    pad = _W + 2 - len(label)
    return f"  ║{label}{'─' * max(1, pad)}║"


def _print_selection_ui(
    os_profile: Dict[str, Any],
    options: List[LLMOption],
    recommended: LLMOption,
) -> None:
    gpu  = os_profile.get("gpu",   {}) or {}
    ram  = os_profile.get("ram",   {}) or {}
    cpu  = os_profile.get("cpu",   {}) or {}
    macos = os_profile.get("macos", {}) or {}

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
    print(_box(f"  RAM      {_c_local(str(ram_gb) + ' GB', 'warn')}"))
    print(_box(f"  Cores    {cores}"))
    if mac_ver:
        print(_box(f"  macOS    {mac_ver}"))
    print(_box_div())
    print(_box("  " + _c_local("Choose your LLM model:", "tagline")))
    print(_box())

    # Group by family for display
    seen_families: List[str] = []
    for i, opt in enumerate(options, start=1):
        if opt.family not in seen_families:
            seen_families.append(opt.family)
            print(_box_family(_c_local(opt.family, "accent")))

        is_rec = opt.filename == recommended.filename
        tag = "  " + _c_local("★ RECOMMENDED", "ok") if is_rec else ""
        print(_box(f"  [{i}]  {_c_local(opt.label, 'key')}{tag}"))
        print(_box(f"       {_c_local(opt.description, 'dim')}"))
        print(_box(
            f"       ~{opt.size_gb} GB on disk  ·  min {opt.min_ram_gb} GB RAM"
        ))
        if i < len(options):
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
                + f"Enter [1–{len(options)}] or press Enter to accept "
                + _c_local(f"[{rec_idx}]", "ok")
                + ": "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return recommended

        if raw == "":
            return recommended
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print(f"  Please enter a number between 1 and {len(options)}.")


def _run_interactive_selection(
    os_profile: Dict[str, Any],
    config_dir: Path,
) -> LLMOption:
    recommended = recommend_llm(os_profile)
    options = viable_options(os_profile)

    _print_selection_ui(os_profile, options, recommended)
    chosen = _prompt_user_choice(options, recommended)

    print()
    print(
        f"  {_c_local('✓', 'ok')}  Selected:"
        f" {_c_local(chosen.label, 'key')}"
    )
    print()

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

    - First boot (no saved choice) → interactive selection → saved to model_choice.json
    - Subsequent boots → loads saved choice silently
    - force_reselect=True → always prompts even if a choice exists

    Args:
        os_profile:     Full OS profile dict from bootstrap.
        config_dir:     ~/.buddy/config  (where model_choice.json lives)
        show_ui:        Whether to print the selection UI
        force_reselect: Force the interactive prompt
    """
    if not force_reselect:
        saved = _load_saved_choice(config_dir)
        if saved is not None:
            return saved

    if show_ui:
        return _run_interactive_selection(os_profile, config_dir)

    # Headless / no UI — use hardware recommendation directly
    chosen = recommend_llm(os_profile)
    _save_choice(config_dir, chosen)
    return chosen
