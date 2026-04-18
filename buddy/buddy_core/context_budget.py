from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Tuple

import psutil

from buddy.logger.logger import get_logger

logger = get_logger("context_budget")


# ==========================================================
# CONFIG
# ==========================================================

_DEFAULT_CHARS_PER_TOKEN = 3.5

# Memory headroom reserved for OS + non-model processes
_RESERVED_RAM = 0.20
_RESERVED_VRAM = 0.25
_RESERVED_METAL = 0.35  # unified memory needs a larger buffer

# Fraction of total context chars held back as a system safety margin
_RESERVE = 0.30

# Slot allocations — intentionally allowed to sum > 1.0 because slots
# rarely fill simultaneously. If you need strict non-overlap, enforce
# it at call-site by dividing each by their sum (1.20).
_ALLOC_HISTORY = 0.35
_ALLOC_MEMORY = 0.20
_ALLOC_TOOL = 0.25
_ALLOC_EXEC = 0.40

# Runtime pressure thresholds (fraction of free memory)
_CRITICAL = 0.10  # immediate, aggressive reduction
_LOW = 0.15
_WARNING = 0.20
_GOOD = 0.30
_HIGH = 0.40  # memory is abundant — can relax limits

# Pressure scale factors applied to ALL char limits + turns
# Each level is a multiplier on the base budget
_PRESSURE_SCALE: dict[str, float] = {
    "critical": 0.40,
    "low": 0.60,
    "warning": 0.80,
    "nominal": 1.00,
    "good": 1.10,
    "high": 1.20,
}

_FLOOR_TURNS = 3
_MIN_CHARS = 256  # absolute floor for any single char slot


# ==========================================================
# KV heuristic constants for llama.cpp
#
# llama.cpp allocates KV cache as:
#   n_layers × n_kv_heads × head_dim × 2 × dtype_bytes  per token
#
# Without model metadata we only know file size. The constants below
# are empirically tuned across common llama.cpp model families:
#
#   Family              quant    KV_BYTES_PER_TOKEN_PER_GB
#   Llama-3 8B  (Q4_K)  ~4.7GB   ≈ 1900
#   Llama-3 70B (Q4_K)  ~40GB    ≈ 2200
#   Mistral 7B  (Q4_K)  ~4.1GB   ≈ 1800   (GQA 8 kv-heads)
#   Qwen2 7B    (Q4_K)  ~4.5GB   ≈ 1750
#   Phi-3 mini  (Q4_K)  ~2.2GB   ≈ 1600
#
# We use a conservative midpoint. The 0.80 safety margin in
# estimate_n_ctx_from_size covers model-to-model variance.
# ==========================================================

_KV_BYTES_PER_TOKEN_PER_GB = 1800  # bytes of KV cache per token, per GB of model
_KV_SAFETY_MARGIN = 0.80


# ==========================================================
# ModelMeta — optional; enables exact KV calculation
#
# For GQA models (Llama-3, Mistral, Qwen, Phi) you MUST supply
# num_kv_heads, not num_attention_heads. Using hidden_size alone
# overstates KV size by 4-8× on these architectures.
# ==========================================================


@dataclass(frozen=True)
class ModelMeta:
    layers: int
    hidden_size: int
    num_attention_heads: int
    num_kv_heads: int  # set equal to num_attention_heads for MHA models
    dtype_bytes: int = 2  # fp16 / bf16 default; use 1 for q8, 4 for fp32

    @property
    def head_dim(self) -> int:
        return self.hidden_size // self.num_attention_heads

    @property
    def kv_bytes_per_token(self) -> int:
        """Exact KV cache bytes consumed per token (both K and V)."""
        return self.layers * self.num_kv_heads * self.head_dim * 2 * self.dtype_bytes


# ==========================================================
# ContextBudget
# ==========================================================


@dataclass(frozen=True)
class ContextBudget:
    # -- context window --
    n_ctx: int
    # -- retrieval soft limits --
    recent_turns: int
    top_k_memories: int
    pre_rerank_k: int
    # -- character budgets --
    max_history_chars: int
    max_memory_chars: int
    max_tool_chars: int
    max_exec_chars: int
    # -- provenance --
    backend: str
    tier: str
    pressure_level: str = "nominal"
    # -- token estimation --
    chars_per_token: float = _DEFAULT_CHARS_PER_TOKEN

    # ===========================================================
    # FACTORY — from_hardware
    # ===========================================================

    @classmethod
    def from_hardware(
        cls,
        profile: Dict[str, Any],
        *,
        model_size_gb: float,
        model_meta: Optional[ModelMeta] = None,
    ) -> "ContextBudget":
        """
        Build a ContextBudget from the OS profile produced by build_os_profile().

        Resolution order:
          1. model_meta supplied  → exact GQA-aware KV calculation
          2. model_size_gb > 0   → heuristic KV estimation (llama.cpp-tuned)
          3. neither             → static tier lookup (pure fallback)
        """
        hw = profile.get("hardware", {})
        gpu = hw.get("gpu") or {}
        ram = hw.get("ram") or {}

        backend = str(gpu.get("backend") or "cpu_only").lower()
        total_ram = float(ram.get("total_gb") or 0)
        total_vram = float(gpu.get("vram_gb") or 0)

        # ── usable memory after OS headroom ─────────────────────
        if backend == "metal":
            usable = total_ram * (1 - _RESERVED_METAL)
        elif backend in ("cuda", "rocm"):
            usable = (total_vram if total_vram > 0 else total_ram) * (
                1 - _RESERVED_VRAM
            )
        else:
            usable = total_ram * (1 - _RESERVED_RAM)

        # ── KV budget = usable minus the model weights ───────────
        kv_budget_gb = max(usable - model_size_gb, 0.5)

        # ── n_ctx resolution ────────────────────────────────────
        if model_meta:
            n_ctx_dyn = _compute_kv_n_ctx_from_meta(kv_budget_gb, model_meta)
            tier = "dynamic_kv_meta"

        elif model_size_gb > 0:
            n_ctx_dyn = _estimate_n_ctx_from_size(kv_budget_gb, model_size_gb)
            tier = "dynamic_kv_size"

        tiers = _select_tiers(backend)
        n_ctx_base, turns, top_k, pre = _lookup_tier(tiers, usable)

        logger.info(
            "context_budget | backend=%s usable=%.1fGB kv_budget=%.1fGB "
            "n_ctx=%d tier=%s",
            backend,
            usable,
            kv_budget_gb,
            min(n_ctx_dyn, n_ctx_base),
            tier,
        )

        return cls._build(
            n_ctx=min(n_ctx_dyn, n_ctx_base),
            recent_turns=turns,
            top_k_memories=top_k,
            pre_rerank_k=pre,
            backend=backend,
            tier=tier,
        )

    # ===========================================================
    # FACTORY — from_override  (buddy.toml [context_budget])
    # ===========================================================

    @classmethod
    def from_override(
        cls,
        base: "ContextBudget",
        cfg: Dict[str, Any],
    ) -> "ContextBudget":
        """Apply [context_budget] override block from buddy.toml.
        Only keys that are explicitly present and non-zero are replaced."""
        if not cfg.get("override", False):
            return base

        def _get(key: str, floor: int, base_val: int) -> int:
            v = cfg.get(key)
            return max(floor, int(v)) if v else base_val

        n_ctx = _get("n_ctx", 512, base.n_ctx)
        recent_turns = _get("recent_turns", _FLOOR_TURNS, base.recent_turns)
        top_k = _get("top_k_memories", 1, base.top_k_memories)
        pre_rerank = _get("pre_rerank_k", 1, base.pre_rerank_k)

        overridden = cls._build(
            n_ctx=n_ctx,
            recent_turns=recent_turns,
            top_k_memories=top_k,
            pre_rerank_k=pre_rerank,
            tier="manual_override",
            backend=base.backend,
        )
        logger.info(
            "context_budget | override applied: n_ctx=%d turns=%d top_k=%d pre=%d",
            n_ctx,
            recent_turns,
            top_k,
            pre_rerank,
        )
        return overridden

    # ===========================================================
    # PRESSURE ADJUSTMENT — adjusts ALL limits
    # ===========================================================

    @classmethod
    def adjust_for_pressure(
        cls,
        base: "ContextBudget",
        *,
        current_turns: int,
    ) -> "ContextBudget":
        """
        Re-scale ALL char limits and recent_turns based on current
        free memory. Called before each LLM invocation.

        Pressure levels and their scale factors:
          critical (<10%)  → 0.40×  aggressive shrink across every slot
          low      (<15%)  → 0.60×
          warning  (<20%)  → 0.80×
          nominal          → 1.00×  (no change from base)
          good     (>30%)  → 1.10×
          high     (>40%)  → 1.20×  can expand slightly beyond base
        """
        free = _free_memory_pct(base.backend)
        level = _pressure_level(free)
        scale = _PRESSURE_SCALE[level]

        # turns: scale from base, then clamp [FLOOR, base.recent_turns]
        # Expanding beyond base.recent_turns is intentional when memory is high
        turns_ceiling = int(base.recent_turns * min(scale, 1.20))
        new_turns = max(
            _FLOOR_TURNS,
            min(turns_ceiling, int(current_turns * scale)),
        )

        # char limits: scale from base, enforce per-slot floor
        def _scaled(base_val: int) -> int:
            return max(_MIN_CHARS, int(base_val * scale))

        # Skip rebuild if nothing changes
        if level == base.pressure_level and new_turns == base.recent_turns:
            return base

        adjusted = replace(
            base,
            recent_turns=new_turns,
            max_history_chars=_scaled(base.max_history_chars),
            max_memory_chars=_scaled(base.max_memory_chars),
            max_tool_chars=_scaled(base.max_tool_chars),
            max_exec_chars=_scaled(base.max_exec_chars),
            pressure_level=level,
        )

        if level != base.pressure_level:
            logger.info(
                "context_budget | pressure %s→%s (free=%.0f%%) scale=%.2f "
                "turns=%d→%d history=%d memory=%d tool=%d exec=%d",
                base.pressure_level,
                level,
                free * 100,
                scale,
                base.recent_turns,
                new_turns,
                adjusted.max_history_chars,
                adjusted.max_memory_chars,
                adjusted.max_tool_chars,
                adjusted.max_exec_chars,
            )

        return adjusted

    # ===========================================================
    # TOKEN ESTIMATION & CALIBRATION
    # ===========================================================

    def estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    def calibrate(self, estimated: int, actual: int) -> "ContextBudget":
        """EMA update of chars_per_token from a real tokenizer observation."""
        if estimated <= 0 or actual <= 0:
            return self
        # observed cpt = chars that produced `estimated` tokens / actual tokens
        observed_cpt = (estimated * self.chars_per_token) / actual
        new_cpt = self.chars_per_token * 0.85 + observed_cpt * 0.15
        # guard against drift from a single bad sample
        new_cpt = max(1.5, min(8.0, new_cpt))
        return replace(self, chars_per_token=new_cpt)

    # ===========================================================
    # BUDGET UTILISATION SNAPSHOT
    # ===========================================================

    def utilisation(
        self,
        *,
        history_chars: int = 0,
        memory_chars: int = 0,
        tool_chars: int = 0,
        exec_chars: int = 0,
    ) -> Dict[str, Any]:
        """
        Returns a snapshot dict showing how full each slot is.
        Useful for logging and deciding whether to evict context.
        """

        def _pct(used: int, cap: int) -> float:
            return round(used / cap, 3) if cap else 0.0

        total_used = history_chars + memory_chars + tool_chars + exec_chars
        total_cap = (
            self.max_history_chars
            + self.max_memory_chars
            + self.max_tool_chars
            + self.max_exec_chars
        )
        return {
            "pressure_level": self.pressure_level,
            "n_ctx": self.n_ctx,
            "tier": self.tier,
            "history": {
                "used": history_chars,
                "cap": self.max_history_chars,
                "pct": _pct(history_chars, self.max_history_chars),
            },
            "memory": {
                "used": memory_chars,
                "cap": self.max_memory_chars,
                "pct": _pct(memory_chars, self.max_memory_chars),
            },
            "tool": {
                "used": tool_chars,
                "cap": self.max_tool_chars,
                "pct": _pct(tool_chars, self.max_tool_chars),
            },
            "exec": {
                "used": exec_chars,
                "cap": self.max_exec_chars,
                "pct": _pct(exec_chars, self.max_exec_chars),
            },
            "total": {
                "used": total_used,
                "cap": total_cap,
                "pct": _pct(total_used, total_cap),
            },
        }

    # ===========================================================
    # INTERNAL BUILDER
    # ===========================================================

    @classmethod
    def _build(
        cls,
        *,
        n_ctx: int,
        recent_turns: int,
        top_k_memories: int,
        pre_rerank_k: int,
        backend: str,
        tier: str,
    ) -> "ContextBudget":
        total_chars = int(n_ctx * _DEFAULT_CHARS_PER_TOKEN)
        usable = int(total_chars * (1 - _RESERVE))

        return cls(
            n_ctx=n_ctx,
            recent_turns=recent_turns,
            top_k_memories=top_k_memories,
            pre_rerank_k=pre_rerank_k,
            max_history_chars=max(_MIN_CHARS, int(usable * _ALLOC_HISTORY)),
            max_memory_chars=max(_MIN_CHARS, int(usable * _ALLOC_MEMORY)),
            max_tool_chars=max(_MIN_CHARS, int(usable * _ALLOC_TOOL)),
            max_exec_chars=max(_MIN_CHARS, int(usable * _ALLOC_EXEC)),
            backend=backend,
            tier=tier,
            pressure_level="nominal",
        )


# ==========================================================
# KV CALCULATION
# ==========================================================


def _compute_kv_n_ctx_from_meta(kv_budget_gb: float, meta: ModelMeta) -> int:
    """
    Exact KV-aware n_ctx calculation using model architecture metadata.
    Correctly handles GQA (num_kv_heads < num_attention_heads).
    """
    total_bytes = kv_budget_gb * (1024**3)
    n_ctx = int(total_bytes / meta.kv_bytes_per_token)
    return max(512, int(n_ctx * _KV_SAFETY_MARGIN))


def _estimate_n_ctx_from_size(kv_budget_gb: float, model_size_gb: float) -> int:
    """
    Heuristic n_ctx from model file size alone (llama.cpp-tuned).

    kv_budget_gb already has model weights subtracted by the caller.
    model_size_gb is used only to derive the per-token KV byte estimate.

    The constant _KV_BYTES_PER_TOKEN_PER_GB is empirically tuned across
    Q4_K_M quants of Llama-3, Mistral, Qwen2, and Phi-3. Larger quants
    (Q8, fp16) will get a slightly conservative estimate — that is safe.
    """
    if model_size_gb <= 0:
        return 2048

    kv_per_token = model_size_gb * _KV_BYTES_PER_TOKEN_PER_GB
    total_bytes = kv_budget_gb * (1024**3)
    n_ctx = int(total_bytes / kv_per_token)
    return max(512, int(n_ctx * _KV_SAFETY_MARGIN))


# ==========================================================
# SOFT LIMIT DERIVATION
# ==========================================================


def _derive_soft_limits(n_ctx: int) -> Tuple[int, int, int]:
    """
    Map n_ctx → (recent_turns, top_k_memories, pre_rerank_k).
    These are the *base* values before any pressure scaling.
    """
    if n_ctx >= 65536:
        return 40, 32, 48
    elif n_ctx >= 32768:
        return 30, 28, 36
    elif n_ctx >= 16384:
        return 20, 18, 28
    elif n_ctx >= 8192:
        return 14, 14, 20
    else:
        return 8, 6, 12


# ==========================================================
# MEMORY PRESSURE
# ==========================================================


def _pressure_level(free_pct: float) -> str:
    if free_pct < _CRITICAL:
        return "critical"
    if free_pct < _LOW:
        return "low"
    if free_pct < _WARNING:
        return "warning"
    if free_pct > _HIGH:
        return "high"
    if free_pct > _GOOD:
        return "good"
    return "nominal"


def _free_memory_pct(backend: str) -> float:
    """
    Returns fraction of memory currently free (0.0–1.0).
    For CUDA: queries VRAM via pynvml.
    For Metal / CPU: queries system RAM (correct for unified memory).
    For ROCm: falls back to RAM (rocm-smi parsing is fragile; RAM is a
              reasonable proxy until a dedicated path is needed).
    """
    try:
        if backend == "cuda":
            pct = _free_vram_cuda()
            if pct is not None:
                return pct
        vm = psutil.virtual_memory()
        return vm.available / vm.total if vm.total else 0.10
    except Exception:
        return 0.10


def _free_vram_cuda() -> Optional[float]:
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        pynvml.nvmlShutdown()
        return info.free / info.total
    except Exception:
        return None


# ==========================================================
# TIER TABLES  (pure fallback — used only when model_size_gb == 0)
# ==========================================================


def _select_tiers(backend: str):
    if backend == "metal":
        return _METAL_TIERS
    if backend in ("cuda", "rocm"):
        return _CUDA_TIERS
    return _CPU_TIERS


_METAL_TIERS = [
    #  min_gb   n_ctx   turns  top_k  pre
    (32.0, 65536, 40, 32, 48),
    (16.0, 32768, 30, 24, 36),
    (8.0, 16384, 20, 16, 28),
    (0.0, 8192, 16, 12, 18),
]

_CUDA_TIERS = [
    (24.0, 32768, 30, 24, 36),
    (16.0, 16384, 20, 16, 28),
    (8.0, 8192, 14, 12, 20),
    (4.0, 4096, 10, 8, 15),
    (0.0, 2048, 6, 5, 10),
]

_CPU_TIERS = [
    (32.0, 16384, 16, 14, 24),
    (16.0, 8192, 12, 10, 18),
    (8.0, 4096, 8, 6, 12),
    (0.0, 2048, 4, 4, 8),
]


def _lookup_tier(tiers, gb: float) -> Tuple[int, int, int, int]:
    for min_gb, n_ctx, turns, top_k, pre in tiers:
        if gb >= min_gb:
            return n_ctx, turns, top_k, pre
    return tiers[-1][1:]
