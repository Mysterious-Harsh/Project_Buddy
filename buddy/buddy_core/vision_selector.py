# buddy/buddy_core/vision_selector.py
# ═══════════════════════════════════════════════════════════
# VISION CAPABILITY SELECTOR
# ═══════════════════════════════════════════════════════════
#
# Asked once at first boot (or on force_vision_reselect=True).
# Saved to ~/.buddy/config/buddy.toml [vision_choice].
#
# Only Qwen3.5 models support vision. If the selected LLM is
# Qwen3.5, the user is asked: enable vision? (Y/n).
# The mmproj quant is always F16; the model quant reuses
# whatever quant was already chosen for the LLM.
#
# Public API:
#   get_or_select_vision(model, os_profile, config_dir,
#                        show_ui, force_reselect) -> VisionChoice

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from buddy.logger.logger import get_logger

logger = get_logger("vision_selector")
_MMPROJ_QUANT = "F16"


# ==========================================================
# Data types
# ==========================================================

@dataclass
class VisionChoice:
    enabled: bool
    mmproj_quant: str = "F16"
    mmproj_hf_repo: str = ""
    mmproj_hf_filename: str = ""
    mmproj_size_gb: float = 0.0
    model_quant: str = ""
    model_hf_filename: str = ""

    @property
    def mmproj_filename(self) -> str:
        return self.mmproj_hf_filename


# ==========================================================
# TOML helpers
# ==========================================================

def _toml_val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _write_toml_section(toml_path: Path, section: str, data: Dict[str, Any]) -> None:
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


# ==========================================================
# Persistence
# ==========================================================

def _load_saved_choice(config_dir: Path) -> Optional[VisionChoice]:
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
        d = raw.get("vision_choice", {})
        # "enabled" key is always written by _save_choice.
        # If it's absent the section was never saved — treat as unconfigured.
        if not isinstance(d, dict) or "enabled" not in d:
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
        logger.warning("Could not load vision_choice from buddy.toml: %r", ex)
        return None


def _save_choice(config_dir: Path, choice: VisionChoice) -> None:
    toml_path = config_dir / "buddy.toml"
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
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        _write_toml_section(toml_path, "vision_choice", data)
        logger.info("Saved vision choice: enabled=%s quant=%s", choice.enabled, choice.model_quant)
    except Exception as ex:
        logger.warning("Could not save vision_choice to buddy.toml: %r", ex)


# ==========================================================
# UI helpers
# ==========================================================

def _c(s: str, color: str) -> str:
    try:
        from buddy.ui.boot_ui import _c as _boot_c
        return _boot_c(s, color)
    except Exception:
        return s


def _prompt_vision_yn(default_yes: bool = True) -> bool:
    default_str = "[Y/n]" if default_yes else "[y/N]"
    while True:
        try:
            raw = input(
                f"  {_c('▸', 'accent')} Enable vision (image understanding)? {default_str}: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default_yes
        if raw in ("", "y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  Please enter y or n.")


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

    Vision is only available for Qwen3.5 models. If the selected
    LLM is vision-capable, the user is asked once whether to enable
    it. The model quant and hf_filename are inherited from the
    already-chosen LLMOption — no separate quant selection needed.

    - First boot (no saved choice) → interactive Y/n → saved
    - Subsequent boots → loads saved choice silently
    - force_reselect=True → always prompts even if a choice exists
    """
    # ── Not a vision-capable model → always disabled ──────────────
    if not getattr(model, "vision_capable", False):
        if show_ui:
            print(
                f"\n  {_c('ℹ', 'dim')}  {_c(model.filename, 'warn')} does not support vision."
                f"\n  {_c('ℹ', 'dim')}  Select a Qwen3.5 model to enable image understanding.\n"
            )
        choice = VisionChoice(enabled=False)
        _save_choice(config_dir, choice)
        return choice

    # ── Load saved choice ──────────────────────────────────────────
    if not force_reselect:
        saved = _load_saved_choice(config_dir)
        if saved is not None:
            logger.info(
                "Loaded saved vision choice: enabled=%s quant=%s",
                saved.enabled,
                saved.model_quant,
            )
            return saved

    # ── Interactive Y/n ────────────────────────────────────────────
    mmproj_size_gb: float = float(getattr(model, "mmproj_size_gb", 0.67))
    mmproj_hf_repo: str = str(getattr(model, "mmproj_hf_repo", ""))
    mmproj_hf_filename: str = str(getattr(model, "mmproj_hf_filename", "mmproj-F16.gguf"))
    model_quant: str = str(getattr(model, "quant", ""))
    model_hf_filename: str = str(getattr(model, "hf_filename", ""))

    if show_ui:
        ram_gb: float = float(((os_profile.get("hardware") or {}).get("ram") or {}).get("total_gb") or 0)
        total_gb = float(getattr(model, "size_gb", 0)) + mmproj_size_gb
        print(
            f"\n  {_c('VISION SETUP', 'accent')}"
            f"  ·  {_c(model.filename, 'warn')}"
            f"  ·  mmproj F16 +{mmproj_size_gb:.2f} GB"
            f"  ·  total ~{total_gb:.1f} GB  (system RAM {ram_gb:.0f} GB)"
        )
        print(
            f"  {_c('ℹ', 'dim')}  Adds image understanding. Requires ~{mmproj_size_gb:.2f} GB extra RAM.\n"
        )
        want = _prompt_vision_yn(default_yes=ram_gb >= total_gb)
    else:
        # Headless — auto-enable if RAM allows
        ram_gb = float(((os_profile.get("hardware") or {}).get("ram") or {}).get("total_gb") or 0)
        total_gb = float(getattr(model, "size_gb", 0)) + mmproj_size_gb
        want = ram_gb >= total_gb

    if not want:
        if show_ui:
            print(f"\n  {_c('✓', 'ok')}  Vision disabled. Buddy will run text-only.\n")
        choice = VisionChoice(enabled=False)
        _save_choice(config_dir, choice)
        return choice

    if show_ui:
        print(f"\n  {_c('✓', 'ok')}  Vision enabled  ·  model {_c(model_quant, 'key')}  ·  mmproj {_c('F16', 'key')}\n")

    choice = VisionChoice(
        enabled=True,
        mmproj_quant=_MMPROJ_QUANT,
        mmproj_hf_repo=mmproj_hf_repo,
        mmproj_hf_filename=mmproj_hf_filename,
        mmproj_size_gb=mmproj_size_gb,
        model_quant=model_quant,
        model_hf_filename=model_hf_filename,
    )
    _save_choice(config_dir, choice)
    return choice
