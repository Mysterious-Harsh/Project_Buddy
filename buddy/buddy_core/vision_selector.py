# buddy/buddy_core/vision_selector.py
# ═══════════════════════════════════════════════════════════
# VISION CAPABILITY SELECTOR
# ═══════════════════════════════════════════════════════════
#
# Asked once at first boot (or on force_vision_reselect=True).
# Saved to ~/.buddy/config/vision_choice.json.
#
# Shows a RAM comparison table: model_quant + mmproj = total
# for all quantizations (Q4_K_M, Q5_K_M, Q6_K, Q8_0) of the
# selected model, so the user can make an informed choice.
#
# Public API:
#   get_or_select_vision(model, os_profile, config_dir,
#                        show_ui, force_reselect) -> VisionChoice

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from buddy.logger.logger import get_logger

logger = get_logger("vision_selector")

_CHOICE_FILE = "vision_choice.json"
_MMPROJ_QUANT = "F16"          # only F16 available from unsloth
_MMPROJ_SIZE_GB = 0.67         # fallback only — actual size read from LLMOption.mmproj_size_gb


# ==========================================================
# Data types
# ==========================================================

@dataclass
class VisionChoice:
    enabled: bool
    # filled when enabled=True
    mmproj_quant: str = "F16"
    mmproj_hf_repo: str = ""
    mmproj_hf_filename: str = ""
    mmproj_size_gb: float = 0.0
    model_quant: str = ""        # quant chosen for the model (e.g. Q4_K_M)
    model_hf_filename: str = ""  # updated hf_filename if quant changed

    @property
    def mmproj_filename(self) -> str:
        return self.mmproj_hf_filename


# ==========================================================
# Persistence
# ==========================================================

def _load_saved_choice(config_dir: Path) -> Optional[VisionChoice]:
    p = config_dir / _CHOICE_FILE
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
        return VisionChoice(
            enabled=bool(d.get("enabled", False)),
            mmproj_quant=str(d.get("mmproj_quant", "F16")),
            mmproj_hf_repo=str(d.get("mmproj_hf_repo", "")),
            mmproj_hf_filename=str(d.get("mmproj_hf_filename", "")),
            mmproj_size_gb=float(d.get("mmproj_size_gb", 0.0)),
            model_quant=str(d.get("model_quant", "")),
            model_hf_filename=str(d.get("model_hf_filename", "")),
        )
    except Exception as ex:
        logger.warning("Could not load vision_choice.json: %r", ex)
        return None


def _save_choice(config_dir: Path, choice: VisionChoice) -> None:
    p = config_dir / _CHOICE_FILE
    data = {
        "enabled": choice.enabled,
        "mmproj_quant": choice.mmproj_quant,
        "mmproj_hf_repo": choice.mmproj_hf_repo,
        "mmproj_hf_filename": choice.mmproj_hf_filename,
        "mmproj_size_gb": choice.mmproj_size_gb,
        "model_quant": choice.model_quant,
        "model_hf_filename": choice.model_hf_filename,
        "saved_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved vision choice: enabled=%s quant=%s", choice.enabled, choice.model_quant)
    except Exception as ex:
        logger.warning("Could not save vision_choice.json: %r", ex)


# ==========================================================
# UI helpers
# ==========================================================

def _c(s: str, color: str) -> str:
    try:
        from buddy.ui.boot_ui import _c as _boot_c
        return _boot_c(s, color)
    except Exception:
        return s


_W = 72


def _box(text: str = "") -> str:
    return f"  ║  {text:<{_W}}║"


def _box_top() -> str:
    return f"  ╔{'═' * (_W + 2)}╗"


def _box_div() -> str:
    return f"  ╠{'═' * (_W + 2)}╣"


def _box_bot() -> str:
    return f"  ╚{'═' * (_W + 2)}╝"


def _box_section(name: str) -> str:
    label = f"  ─── {name} "
    pad = _W + 2 - len(label)
    return f"  ║{label}{'─' * max(1, pad)}║"


# ==========================================================
# Quant comparison table
# ==========================================================

def _model_key(model_filename: str) -> str:
    """Map a model filename to a QUANT_CATALOG key."""
    fn = model_filename.lower()
    if "35b-a3b" in fn or "35b_a3b" in fn:
        return "Qwen3.5-35B-A3B"
    if "27b" in fn:
        return "Qwen3.5-27B"
    if "9b" in fn:
        return "Qwen3.5-9B"
    if "4b" in fn:
        return "Qwen3.5-4B"
    return ""


def _print_vision_ui(
    *,
    model_filename: str,
    ram_gb: float,
    quant_opts: List[Any],   # List[QuantOption]
    mmproj_size_gb: float,
    recommended_quant: str,
) -> None:
    from buddy.buddy_core.model_selector import QuantOption

    print()
    print(_box_top())
    print(_box("  " + _c("BUDDY — VISION CAPABILITY SETUP", "accent")))
    print(_box_div())
    print(_box(f"  Model      {_c(model_filename, 'warn')}"))
    print(_box(f"  System RAM {_c(str(ram_gb) + ' GB', 'warn')}"))
    print(_box(f"  mmproj     {_c(f'F16  ({mmproj_size_gb:.2f} GB)', 'dim')}"))
    print(_box_div())
    print(_box("  " + _c("Quantization   Model     + mmproj  = Total     Viable?", "tagline")))
    print(_box("  " + "─" * 62))

    for opt in quant_opts:
        total = opt.size_gb + mmproj_size_gb
        viable = ram_gb >= total
        viable_str = _c("✓  fits", "ok") if viable else _c("✗  need more RAM", "dim")
        rec_tag = "  " + _c("★ RECOMMENDED", "ok") if opt.quant == recommended_quant else ""
        row = (
            f"  {_c(opt.quant, 'key'):<20}"
            f"  {opt.size_gb:>4.1f} GB"
            f"  + {mmproj_size_gb:.2f} GB"
            f"  = {total:>5.2f} GB"
            f"  {viable_str}"
            f"{rec_tag}"
        )
        print(_box(row))

    print(_box_div())
    print(_box("  " + _c("Does this model support vision (image understanding)?", "tagline")))
    print(_box())
    print(_box("  Note: vision requires downloading the mmproj file (~0.67 GB extra)."))
    print(_box("  Without vision, Buddy works text-only (less RAM, faster boot)."))
    print(_box_bot())
    print()


def _recommend_quant(quant_opts: List[Any], ram_gb: float, mmproj_size_gb: float) -> str:
    """
    Pick the highest quality quant that comfortably fits in system RAM.
    Falls back to Q4_K_M if nothing fits.
    """
    # Prefer quality descending: Q8_0 > Q6_K > Q5_K_M > Q4_K_M
    quality_order = ["Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M"]
    available = {opt.quant: opt for opt in quant_opts}

    for quant in quality_order:
        opt = available.get(quant)
        if opt and ram_gb >= (opt.size_gb + mmproj_size_gb):
            return quant

    # Nothing fits with vision — return lowest quant anyway (user can decline)
    return quant_opts[0].quant if quant_opts else "Q4_K_M"


# ==========================================================
# Non-vision model notice
# ==========================================================

def _print_no_vision_notice(model_filename: str) -> None:
    print()
    print(f"  {_c('ℹ', 'dim')}  Model {_c(model_filename, 'warn')} does not support vision.")
    print(f"  {_c('ℹ', 'dim')}  To enable vision, select a Qwen3.5 model (4B, 9B, 27B, or 35B-A3B).")
    print()


# ==========================================================
# Interactive prompt
# ==========================================================

def _prompt_vision_yn(default_yes: bool = True) -> bool:
    default_str = "[Y/n]" if default_yes else "[y/N]"
    while True:
        try:
            raw = input(
                f"  {_c('▸', 'accent')} Enable vision capability? {default_str}: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default_yes
        if raw in ("", "y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please enter y or n.")


def _prompt_quant_choice(quant_opts: List[Any], recommended: str) -> str:
    """Let the user pick a quant, defaulting to the recommended one."""
    opts_map = {str(i + 1): opt.quant for i, opt in enumerate(quant_opts)}
    rec_idx = next(
        (str(i + 1) for i, opt in enumerate(quant_opts) if opt.quant == recommended),
        "1",
    )

    quant_list = "  ".join(
        f"[{i + 1}] {opt.quant}" for i, opt in enumerate(quant_opts)
    )
    print(f"\n  Quantization options: {quant_list}")

    while True:
        try:
            raw = input(
                f"  {_c('▸', 'accent')} Choose quantization "
                f"or press Enter for {_c(recommended, 'ok')} [{rec_idx}]: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return recommended
        if raw == "":
            return recommended
        if raw in opts_map:
            return opts_map[raw]
        print(f"  Please enter a number between 1 and {len(quant_opts)}.")


# ==========================================================
# Public API
# ==========================================================

def get_or_select_vision(
    model: Any,       # LLMOption
    os_profile: Dict[str, Any],
    *,
    config_dir: Path,
    show_ui: bool = True,
    force_reselect: bool = False,
) -> VisionChoice:
    """
    Return the VisionChoice for this session.

    - First boot (no saved choice) → interactive selection → saved
    - Subsequent boots → loads saved choice silently
    - force_reselect=True → always prompts even if a choice exists

    Args:
        model:          The selected LLMOption (from model_selector).
        os_profile:     Full OS profile dict (for RAM info).
        config_dir:     ~/.buddy/config  (where vision_choice.json lives)
        show_ui:        Whether to print the selection UI
        force_reselect: Force the interactive prompt
    """
    # ── Not a vision-capable model → always disabled ──────
    if not getattr(model, "vision_capable", False):
        if show_ui:
            _print_no_vision_notice(model.filename)
        choice = VisionChoice(enabled=False)
        _save_choice(config_dir, choice)
        return choice

    # ── Load saved choice ──────────────────────────────────
    if not force_reselect:
        saved = _load_saved_choice(config_dir)
        if saved is not None:
            logger.info(
                "Loaded saved vision choice: enabled=%s quant=%s",
                saved.enabled,
                saved.model_quant,
            )
            return saved

    # ── Interactive selection ──────────────────────────────
    from buddy.buddy_core.model_selector import QUANT_CATALOG

    ram_gb: float = float((os_profile.get("ram") or {}).get("total_gb") or 0)
    mmproj_size_gb: float = float(getattr(model, "mmproj_size_gb", 0.67))
    mmproj_hf_repo: str = str(getattr(model, "mmproj_hf_repo", ""))
    mmproj_hf_filename: str = str(getattr(model, "mmproj_hf_filename", "mmproj-F16.gguf"))

    model_key = _model_key(model.filename)
    quant_opts = QUANT_CATALOG.get(model_key, [])

    if not quant_opts:
        # Unknown model size — fallback to current quant, no table
        if show_ui:
            print(f"\n  {_c('ℹ', 'dim')}  Vision is available for {model.filename}.")
        want = _prompt_vision_yn(default_yes=True) if show_ui else False
        choice = VisionChoice(
            enabled=want,
            mmproj_quant=_MMPROJ_QUANT,
            mmproj_hf_repo=mmproj_hf_repo,
            mmproj_hf_filename=mmproj_hf_filename,
            mmproj_size_gb=mmproj_size_gb,
            model_quant=model.filename.split("-")[-1].replace(".gguf", ""),
            model_hf_filename=model.hf_filename,
        )
        _save_choice(config_dir, choice)
        return choice

    recommended_quant = _recommend_quant(quant_opts, ram_gb, mmproj_size_gb)

    if show_ui:
        _print_vision_ui(
            model_filename=model.filename,
            ram_gb=ram_gb,
            quant_opts=quant_opts,
            mmproj_size_gb=mmproj_size_gb,
            recommended_quant=recommended_quant,
        )
        want = _prompt_vision_yn(default_yes=True)
    else:
        # Headless — auto-enable if RAM allows
        min_opt = quant_opts[0]
        want = ram_gb >= (min_opt.size_gb + mmproj_size_gb)

    if not want:
        print(
            f"\n  {_c('✓', 'ok')}  Vision disabled. Buddy will run text-only.\n"
        ) if show_ui else None
        choice = VisionChoice(enabled=False)
        _save_choice(config_dir, choice)
        return choice

    # Pick quant
    chosen_quant = (
        _prompt_quant_choice(quant_opts, recommended_quant)
        if show_ui
        else recommended_quant
    )

    chosen_opt = next((o for o in quant_opts if o.quant == chosen_quant), quant_opts[0])

    if show_ui:
        print(
            f"\n  {_c('✓', 'ok')}  Vision enabled"
            f"  ·  model {_c(chosen_quant, 'key')}"
            f"  ·  mmproj {_c('F16', 'key')}"
            f"  ·  total ~{chosen_opt.size_gb + mmproj_size_gb:.1f} GB\n"
        )

    choice = VisionChoice(
        enabled=True,
        mmproj_quant=_MMPROJ_QUANT,
        mmproj_hf_repo=mmproj_hf_repo,
        mmproj_hf_filename=mmproj_hf_filename,
        mmproj_size_gb=mmproj_size_gb,
        model_quant=chosen_quant,
        model_hf_filename=chosen_opt.hf_filename,
    )
    _save_choice(config_dir, choice)
    return choice
