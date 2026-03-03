# buddy/buddy_core/model_selector.py
#
# Hardware-aware LLM model selection.
#
# First boot  → interactive prompt (hardware summary + curated options) → saved to model_choice.json
# Subsequent  → loads saved choice silently, no prompt
# force_reselect=True → re-prompts even if a choice exists
#
# Public API:
#   get_or_select_llm_model(os_profile, config_dir, show_ui, force_reselect) -> LLMOption

from __future__ import annotations

import json
from dataclasses import dataclass
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
    LOW = "low"  # ≤ 8 GB effective memory
    MID = "mid"  # 16 GB
    HIGH = "high"  # 32 GB
    ULTRA = "ultra"  # 64 GB+


@dataclass
class LLMOption:
    tier: HardwareTier
    filename: str  # local GGUF filename
    hf_repo: str  # HuggingFace repo ID
    hf_filename: str  # filename inside the HF repo
    label: str  # shown in selection UI
    size_gb: float  # approximate disk size
    min_ram_gb: float  # minimum RAM to run comfortably
    description: str = ""


# ==========================================================
# Curated model list
# ==========================================================

QWEN3_OPTIONS: List[LLMOption] = [
    LLMOption(
        tier=HardwareTier.LOW,
        filename="Qwen3-8B-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen3-8B-GGUF",
        hf_filename="Qwen3-8B-Q4_K_M.gguf",
        label="Qwen3-8B  Q4_K_M    fast response  (8 GB RAM min)",
        size_gb=4.9,
        min_ram_gb=8.0,
        description="Best for 8 GB systems. Snappy responses, solid quality.",
    ),
    LLMOption(
        tier=HardwareTier.MID,
        filename="Qwen3-14B-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen3-14B-GGUF",
        hf_filename="Qwen3-14B-Q4_K_M.gguf",
        label="Qwen3-14B Q4_K_M    balanced       (16 GB RAM min)",
        size_gb=8.6,
        min_ram_gb=16.0,
        description="Recommended for 16 GB unified memory. Best quality/speed ratio.",
    ),
    LLMOption(
        tier=HardwareTier.HIGH,
        filename="Qwen3-14B-Q8_0.gguf",
        hf_repo="Qwen/Qwen3-14B-GGUF",
        hf_filename="Qwen3-14B-Q8_0.gguf",
        label="Qwen3-14B Q8_0      high quality   (32 GB RAM min)",
        size_gb=15.7,
        min_ram_gb=32.0,
        description="Higher precision. Noticeably sharper reasoning.",
    ),
    LLMOption(
        tier=HardwareTier.ULTRA,
        filename="Qwen3-32B-Q4_K_M.gguf",
        hf_repo="Qwen/Qwen3-32B-GGUF",
        hf_filename="Qwen3-32B-Q4_K_M.gguf",
        label="Qwen3-32B Q4_K_M    powerful       (64 GB RAM min)",
        size_gb=19.8,
        min_ram_gb=64.0,
        description="Maximum capability for high-memory Mac Pro / Studio Ultra.",
    ),
]


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

    if effective_gb >= 64:
        return HardwareTier.ULTRA
    if effective_gb >= 32:
        return HardwareTier.HIGH
    if effective_gb >= 16:
        return HardwareTier.MID
    return HardwareTier.LOW


def recommend_llm(os_profile: Dict[str, Any]) -> LLMOption:
    """
    Return the best LLMOption the hardware can comfortably run.
    Walks from detected tier downward; picks first option that fits RAM.
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
        for opt in QWEN3_OPTIONS:
            if opt.tier == t and ram_gb >= opt.min_ram_gb:
                return opt

    return QWEN3_OPTIONS[0]  # absolute fallback


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
        # Match against known options
        for opt in QWEN3_OPTIONS:
            if opt.filename == saved["filename"]:
                logger.info("Loaded saved model choice: %s", opt.label)
                return opt
        # Unknown / custom GGUF — reconstruct synthetic option
        return LLMOption(
            tier=HardwareTier.MID,
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


# Import UI helpers lazily to avoid circular import at module level
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


_W = 68  # box inner width


def _box(text: str = "") -> str:
    return f"  ║  {text:<{_W}}║"


def _box_top() -> str:
    return f"  ╔{'═' * (_W + 2)}╗"


def _box_div() -> str:
    return f"  ╠{'═' * (_W + 2)}╣"


def _box_bot() -> str:
    return f"  ╚{'═' * (_W + 2)}╝"


def _print_selection_ui(
    os_profile: Dict[str, Any],
    viable: List[LLMOption],
    recommended: LLMOption,
) -> None:
    gpu = os_profile.get("gpu", {}) or {}
    ram = os_profile.get("ram", {}) or {}
    cpu = os_profile.get("cpu", {}) or {}
    macos = os_profile.get("macos", {}) or {}

    ram_gb = ram.get("total_gb", "?")
    cores = cpu.get("logical_cores", "?")
    gpu_name = gpu.get("name", "unknown")
    gpu_back = gpu.get("backend", "cpu_only")
    vram_gb = gpu.get("total_vram_gb", None)
    mac_ver = macos.get("product_version", "")
    vram_str = f"{vram_gb} GB VRAM" if vram_gb else "unified memory"

    print()
    print(_box_top())
    print(_box("  " + _c_local("BUDDY — MODEL SELECTION", "bright_cyan")))
    print(_box_div())
    print(_box(f"  GPU      {_c_local(str(gpu_name), 'bright_yellow')}"))
    print(_box(f"  Backend  {_c_local(gpu_back, 'dim')}  ({vram_str})"))
    print(_box(f"  RAM      {_c_local(str(ram_gb) + ' GB', 'bright_yellow')}"))
    print(_box(f"  Cores    {cores}"))
    if mac_ver:
        print(_box(f"  macOS    {mac_ver}"))
    print(_box_div())
    print(_box("  " + _c_local("Choose your LLM model:", "magenta")))
    print(_box_div())

    for i, opt in enumerate(viable, start=1):
        is_rec = opt.filename == recommended.filename
        tag = "  " + _c_local("★ RECOMMENDED", "bright_green") if is_rec else ""
        print(_box(f"  [{i}]  {_c_local(opt.label, 'bright_cyan')}{tag}"))
        print(_box(f"       {_c_local(opt.description, 'dim')}"))
        print(_box(f"       ~{opt.size_gb} GB on disk  ·  min {opt.min_ram_gb} GB RAM"))
        if i < len(viable):
            print(_box())

    print(_box_bot())
    print()


def _prompt_user_choice(viable: List[LLMOption], recommended: LLMOption) -> LLMOption:
    rec_idx = next(
        (i for i, o in enumerate(viable, 1) if o.filename == recommended.filename), 1
    )
    while True:
        try:
            raw = input(
                _c_local(f"  ▸ ", "bright_cyan")
                + f"Enter [1–{len(viable)}] or press Enter to accept "
                + _c_local(f"[{rec_idx}]", "bright_green")
                + ": "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return recommended

        if raw == "":
            return recommended
        if raw.isdigit() and 1 <= int(raw) <= len(viable):
            return viable[int(raw) - 1]
        print(f"  Please enter a number between 1 and {len(viable)}.")


def _run_interactive_selection(
    os_profile: Dict[str, Any],
    config_dir: Path,
) -> LLMOption:
    recommended = recommend_llm(os_profile)
    ram_gb = float(os_profile.get("ram", {}).get("total_gb") or 0)
    viable = [opt for opt in QWEN3_OPTIONS if ram_gb >= opt.min_ram_gb]
    if not viable:
        viable = [QWEN3_OPTIONS[0]]

    _print_selection_ui(os_profile, viable, recommended)
    chosen = _prompt_user_choice(viable, recommended)

    print()
    print(
        f"  {_c_local('✓', 'bright_green')}  Selected:"
        f" {_c_local(chosen.label, 'bright_cyan')}"
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
