from __future__ import annotations

# ==========================================================
# context_budget.py  —  v1.0.0
#
# Hardware-aware context budgeting for Buddy.
#
# Boot-time:  ContextBudget.from_hardware(os_profile)
#             → n_ctx   injected into llama-server --ctx-size
#             → all soft limits stored in BootstrapState
#
# Per-turn:   ContextBudget.adjusted_for_pressure(base)
#             → checks free RAM / VRAM right now
#             → steps recent_turns ±1 (never half-cut)
#
# Platform:   Metal (Apple Silicon unified) · CUDA · ROCm · CPU-only
# ==========================================================

import subprocess
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

import psutil

from buddy.logger.logger import get_logger

logger = get_logger("context_budget")

# ----------------------------------------------------------
# Constants
# ----------------------------------------------------------

_CHARS_PER_TOKEN: float = 3.5      # conservative char/token approximation

# Fraction of total char budget reserved for system prompt + model output.
# The remaining (1 - _RESERVE) is split across history, memories, tool outputs.
_RESERVE: float = 0.30

# Allocation of the non-reserved budget
_ALLOC_HISTORY: float = 0.35
_ALLOC_MEMORY: float = 0.20
_ALLOC_TOOL_OUTPUT: float = 0.25   # per individual step output
_ALLOC_EXEC_RESULTS: float = 0.40  # total execution_results for responder

# Pressure thresholds for per-turn adjustment
_PRESSURE_DOWN_PCT: float = 0.15   # free < 15% → step down
_PRESSURE_UP_PCT: float = 0.30     # free > 30% → step up

# Absolute floor — never go below these regardless of pressure
_FLOOR_RECENT_TURNS: int = 3
_FLOOR_TOP_K: int = 3


# ==========================================================
# Tier tables — (n_ctx, recent_turns, top_k_memories, pre_rerank_k)
# ==========================================================

# Metal: Apple Silicon unified memory — model + KV cache share the same pool.
# effective_gb = total RAM (already handled by score_hardware in model_selector).
_METAL_TIERS: list[tuple[float, int, int, int, int]] = [
    # min_gb   n_ctx   turns  top_k  pre_rerank
    (32.0,    65536,   30,    24,    36),
    (16.0,    32768,   20,    16,    28),
    ( 8.0,    16384,   14,    10,    20),
    ( 0.0,     8192,    8,     6,    12),
]

# CUDA / ROCm: KV cache lives in VRAM — n_ctx is VRAM-limited.
_CUDA_TIERS: list[tuple[float, int, int, int, int]] = [
    (16.0,    16384,   20,    16,    28),
    ( 8.0,     8192,   14,    12,    20),
    ( 4.0,     4096,   10,     8,    15),
    ( 0.0,     2048,    6,     5,    10),
]

# CPU-only: everything in system RAM, most constrained.
_CPU_TIERS: list[tuple[float, int, int, int, int]] = [
    (32.0,    16384,   16,    14,    24),
    (16.0,     8192,   12,    10,    18),
    ( 8.0,     4096,    8,     6,    12),
    ( 0.0,     2048,    4,     4,     8),
]


# ==========================================================
# ContextBudget dataclass
# ==========================================================


@dataclass(frozen=True)
class ContextBudget:
    # ---- boot-time (llama-server --ctx-size) ----
    n_ctx: int

    # ---- per-turn soft limits ----
    recent_turns: int
    top_k_memories: int
    pre_rerank_k: int

    # ---- per-call char budgets ----
    max_history_chars: int       # cap on recent_conversations string
    max_memory_chars: int        # cap on mem_text string
    max_tool_output_chars: int   # cap on each individual step output
    max_exec_results_chars: int  # cap on total execution_results for responder

    # ---- metadata ----
    tier_label: str   # e.g. "metal_mid"
    backend: str      # "metal" | "cuda" | "rocm" | "cpu_only"

    # ----------------------------------------------------------
    # Factory: compute from hardware profile (use at boot)
    # ----------------------------------------------------------

    @classmethod
    def from_hardware(cls, os_profile: Dict[str, Any]) -> "ContextBudget":
        """
        Derive a ContextBudget from the OS profile produced by bootstrap.
        Reads backend (metal/cuda/rocm/cpu_only) and effective memory.
        """
        gpu = (os_profile.get("gpu") or {})
        backend = str(gpu.get("backend") or "cpu_only").lower().strip()

        if backend == "metal":
            effective_gb = float((os_profile.get("ram") or {}).get("total_gb") or 0)
            tiers = _METAL_TIERS
            tier_prefix = "metal"
        elif backend in ("cuda", "rocm"):
            effective_gb = float(gpu.get("total_vram_gb") or 0)
            if effective_gb <= 0:
                # fallback to RAM if VRAM not reported
                effective_gb = float((os_profile.get("ram") or {}).get("total_gb") or 0)
            tiers = _CUDA_TIERS
            tier_prefix = backend
        else:
            effective_gb = float((os_profile.get("ram") or {}).get("total_gb") or 0)
            tiers = _CPU_TIERS
            tier_prefix = "cpu"

        n_ctx, recent_turns, top_k, pre_rerank = _lookup_tier(tiers, effective_gb)
        tier_label = f"{tier_prefix}_{_tier_name(tiers, effective_gb)}"

        budget = cls._build(
            n_ctx=n_ctx,
            recent_turns=recent_turns,
            top_k_memories=top_k,
            pre_rerank_k=pre_rerank,
            tier_label=tier_label,
            backend=backend,
        )
        logger.info(
            "context_budget | backend=%s effective_gb=%.1f tier=%s "
            "n_ctx=%d turns=%d top_k=%d pre_rerank=%d",
            backend, effective_gb, tier_label,
            n_ctx, recent_turns, top_k, pre_rerank,
        )
        return budget

    # ----------------------------------------------------------
    # Factory: apply toml user override
    # ----------------------------------------------------------

    @classmethod
    def from_override(
        cls,
        base: "ContextBudget",
        cfg: Dict[str, Any],
    ) -> "ContextBudget":
        """
        Apply [context_budget] override block from buddy.toml.
        Only replaces values that are explicitly set.
        """
        if not cfg.get("override", False):
            return base

        n_ctx = int(cfg.get("n_ctx") or base.n_ctx)
        recent_turns = int(cfg.get("recent_turns") or base.recent_turns)
        top_k = int(cfg.get("top_k_memories") or base.top_k_memories)
        pre_rerank = int(cfg.get("pre_rerank_k") or base.pre_rerank_k)

        overridden = cls._build(
            n_ctx=n_ctx,
            recent_turns=recent_turns,
            top_k_memories=top_k,
            pre_rerank_k=pre_rerank,
            tier_label="manual_override",
            backend=base.backend,
        )
        logger.info(
            "context_budget | manual override applied: "
            "n_ctx=%d turns=%d top_k=%d pre_rerank=%d",
            n_ctx, recent_turns, top_k, pre_rerank,
        )
        return overridden

    # ----------------------------------------------------------
    # Per-turn: adjust for live memory pressure
    # ----------------------------------------------------------

    @classmethod
    def adjusted_for_pressure(
        cls,
        base: "ContextBudget",
        *,
        current_turns: int,
    ) -> "ContextBudget":
        """
        Check free RAM / VRAM right now.
        Step recent_turns ±1 based on pressure.
        Never cuts in half — always ±1.

        current_turns: the value in use this turn (may already be adjusted
                       from a previous pressure check — passed in for hysteresis).
        """
        free_pct = _free_memory_pct(base.backend)

        new_turns = current_turns

        if free_pct < _PRESSURE_DOWN_PCT:
            new_turns = max(_FLOOR_RECENT_TURNS, current_turns - 1)
        elif free_pct > _PRESSURE_UP_PCT:
            new_turns = min(base.recent_turns, current_turns + 1)

        if new_turns != current_turns:
            logger.info(
                "context_budget | pressure free=%.1f%% turns %d→%d",
                free_pct * 100, current_turns, new_turns,
            )

        # Return base only when new_turns matches the tier default exactly
        if new_turns == base.recent_turns:
            return base
        return replace(base, recent_turns=new_turns)

    # ----------------------------------------------------------
    # Internal builder — derives char budgets from n_ctx
    # ----------------------------------------------------------

    @classmethod
    def _build(
        cls,
        *,
        n_ctx: int,
        recent_turns: int,
        top_k_memories: int,
        pre_rerank_k: int,
        tier_label: str,
        backend: str,
    ) -> "ContextBudget":
        total_chars = int(n_ctx * _CHARS_PER_TOKEN)
        usable = int(total_chars * (1.0 - _RESERVE))

        return cls(
            n_ctx=n_ctx,
            recent_turns=recent_turns,
            top_k_memories=top_k_memories,
            pre_rerank_k=pre_rerank_k,
            max_history_chars=int(usable * _ALLOC_HISTORY),
            max_memory_chars=int(usable * _ALLOC_MEMORY),
            max_tool_output_chars=int(usable * _ALLOC_TOOL_OUTPUT),
            max_exec_results_chars=int(usable * _ALLOC_EXEC_RESULTS),
            tier_label=tier_label,
            backend=backend,
        )


# ==========================================================
# Free memory detection — cross-platform
# ==========================================================


def _free_memory_pct(backend: str) -> float:
    """
    Return fraction of relevant memory that is currently free.
    Metal / CPU → system RAM (psutil).
    CUDA        → GPU VRAM (pynvml → nvidia-smi fallback).
    ROCm        → GPU VRAM (rocm-smi fallback).
    """
    try:
        if backend == "cuda":
            pct = _free_vram_cuda()
            if pct is not None:
                return pct
        elif backend == "rocm":
            pct = _free_vram_rocm()
            if pct is not None:
                return pct
        # metal / cpu_only / fallback
        vm = psutil.virtual_memory()
        return vm.available / vm.total if vm.total > 0 else 1.0
    except Exception as e:
        logger.debug("_free_memory_pct failed: %r", e)
        return 1.0  # assume no pressure on error


def _free_vram_cuda() -> Optional[float]:
    """Try pynvml first, then nvidia-smi subprocess."""
    # --- pynvml ---
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        pynvml.nvmlShutdown()
        return info.free / info.total if info.total > 0 else 1.0
    except Exception:
        pass

    # --- nvidia-smi subprocess ---
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).decode().strip().split("\n")[0]
        free_mb, total_mb = (float(x.strip()) for x in out.split(","))
        return free_mb / total_mb if total_mb > 0 else 1.0
    except Exception:
        pass

    return None


def _free_vram_rocm() -> Optional[float]:
    """Parse rocm-smi --showmeminfo vram (Linux/ROCm only)."""
    try:
        out = subprocess.check_output(
            ["rocm-smi", "--showmeminfo", "vram"],
            timeout=3,
            stderr=subprocess.DEVNULL,
        ).decode()
        # Output format: "GPU[0]  : VRAM Total Memory (B): 12884901888"
        #                "GPU[0]  : VRAM Total Used Memory (B): 2147483648"
        total = used = 0
        for line in out.splitlines():
            if "VRAM Total Memory" in line and "Used" not in line:
                total = int(line.split(":")[-1].strip())
            elif "VRAM Total Used Memory" in line:
                used = int(line.split(":")[-1].strip())
        if total > 0:
            return (total - used) / total
    except Exception:
        pass
    return None


# ==========================================================
# Tier lookup helpers
# ==========================================================


def _lookup_tier(
    tiers: list[tuple[float, int, int, int, int]],
    effective_gb: float,
) -> Tuple[int, int, int, int]:
    """Return (n_ctx, recent_turns, top_k, pre_rerank) for given effective_gb."""
    for min_gb, n_ctx, turns, top_k, pre_rerank in tiers:
        if effective_gb >= min_gb:
            return n_ctx, turns, top_k, pre_rerank
    # absolute fallback — lowest tier
    _, n_ctx, turns, top_k, pre_rerank = tiers[-1]
    return n_ctx, turns, top_k, pre_rerank


def _tier_name(
    tiers: list[tuple[float, int, int, int, int]],
    effective_gb: float,
) -> str:
    labels = ["ultra", "high", "mid", "low"]
    for i, (min_gb, *_) in enumerate(tiers):
        if effective_gb >= min_gb:
            return labels[i] if i < len(labels) else "low"
    return "low"
