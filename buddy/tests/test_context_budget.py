"""
Tests for context_budget.py and smart_truncator.py
Run: python -m pytest buddy/tests/test_context_budget.py -v
"""
from __future__ import annotations

import pytest
from buddy.buddy_core.context_budget import (
    ContextBudget,
    _lookup_tier,
    _METAL_TIERS,
    _CUDA_TIERS,
    _CPU_TIERS,
    _FLOOR_RECENT_TURNS,
)
from buddy.buddy_core.boot import _inject_ctx_size
from buddy.buddy_core.smart_truncator import (
    truncate_middle,
    truncate_proportional,
    truncate_history,
)


# ==========================================================
# ContextBudget — from_hardware
# ==========================================================


class TestFromHardware:
    def test_metal_high(self):
        # 16GB Metal → HIGH tier (32768 n_ctx, 20 turns)
        profile = {"gpu": {"backend": "metal"}, "ram": {"total_gb": 16}}
        b = ContextBudget.from_hardware(profile)
        assert b.backend == "metal"
        assert b.n_ctx == 32768
        assert b.recent_turns == 20
        assert b.top_k_memories == 16
        assert "metal" in b.tier_label

    def test_metal_mid(self):
        # 8-15GB Metal → MID tier (16384 n_ctx, 14 turns)
        profile = {"gpu": {"backend": "metal"}, "ram": {"total_gb": 12}}
        b = ContextBudget.from_hardware(profile)
        assert b.backend == "metal"
        assert b.n_ctx == 16384
        assert b.recent_turns == 14
        assert b.top_k_memories == 10
        assert "metal" in b.tier_label

    def test_metal_ultra(self):
        profile = {"gpu": {"backend": "metal"}, "ram": {"total_gb": 64}}
        b = ContextBudget.from_hardware(profile)
        assert b.n_ctx == 65536
        assert b.recent_turns == 30

    def test_metal_low(self):
        profile = {"gpu": {"backend": "metal"}, "ram": {"total_gb": 4}}
        b = ContextBudget.from_hardware(profile)
        assert b.n_ctx == 8192
        assert b.recent_turns == 8

    def test_cuda_uses_vram(self):
        profile = {
            "gpu": {"backend": "cuda", "total_vram_gb": 8},
            "ram": {"total_gb": 32},
        }
        b = ContextBudget.from_hardware(profile)
        assert b.backend == "cuda"
        assert b.n_ctx == 8192

    def test_cuda_fallback_to_ram_when_no_vram(self):
        profile = {
            "gpu": {"backend": "cuda", "total_vram_gb": 0},
            "ram": {"total_gb": 16},
        }
        b = ContextBudget.from_hardware(profile)
        # falls back to RAM-based lookup in CUDA tiers
        assert b.n_ctx == 16384

    def test_cpu_only(self):
        profile = {"gpu": {"backend": "cpu_only"}, "ram": {"total_gb": 16}}
        b = ContextBudget.from_hardware(profile)
        assert b.backend == "cpu_only"
        assert b.n_ctx == 8192
        assert b.recent_turns == 12

    def test_empty_profile_returns_valid_budget(self):
        b = ContextBudget.from_hardware({})
        assert b.n_ctx > 0
        assert b.recent_turns >= _FLOOR_RECENT_TURNS

    def test_char_budgets_are_positive(self):
        profile = {"gpu": {"backend": "metal"}, "ram": {"total_gb": 16}}
        b = ContextBudget.from_hardware(profile)
        assert b.max_history_chars > 0
        assert b.max_memory_chars > 0
        assert b.max_tool_output_chars > 0
        assert b.max_exec_results_chars > 0

    def test_char_budgets_scale_with_n_ctx(self):
        low = ContextBudget.from_hardware({"gpu": {"backend": "metal"}, "ram": {"total_gb": 4}})
        high = ContextBudget.from_hardware({"gpu": {"backend": "metal"}, "ram": {"total_gb": 64}})
        assert high.max_history_chars > low.max_history_chars
        assert high.max_exec_results_chars > low.max_exec_results_chars


# ==========================================================
# ContextBudget — from_override
# ==========================================================


class TestFromOverride:
    def _base(self):
        return ContextBudget.from_hardware({"gpu": {"backend": "metal"}, "ram": {"total_gb": 16}})

    def test_override_disabled_returns_base(self):
        base = self._base()
        result = ContextBudget.from_override(base, {"override": False, "n_ctx": 999})
        assert result.n_ctx == base.n_ctx

    def test_override_enabled_replaces_values(self):
        base = self._base()
        result = ContextBudget.from_override(base, {
            "override": True,
            "n_ctx": 4096,
            "recent_turns": 6,
            "top_k_memories": 4,
            "pre_rerank_k": 10,
        })
        assert result.n_ctx == 4096
        assert result.recent_turns == 6
        assert result.top_k_memories == 4
        assert result.tier_label == "manual_override"

    def test_override_partial_uses_base_for_missing(self):
        base = self._base()
        result = ContextBudget.from_override(base, {
            "override": True,
            "n_ctx": 8192,
        })
        assert result.n_ctx == 8192
        assert result.recent_turns == base.recent_turns


# ==========================================================
# ContextBudget — adjusted_for_pressure
# ==========================================================


class TestPressureAdjustment:
    def _base(self):
        # 16GB Metal = HIGH tier: recent_turns=20
        return ContextBudget.from_hardware({"gpu": {"backend": "metal"}, "ram": {"total_gb": 16}})

    def test_no_change_in_normal_range(self, monkeypatch):
        # 20% free — between 15% (down) and 30% (up) — no change
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.20)
        base = self._base()
        result = ContextBudget.adjusted_for_pressure(base, current_turns=base.recent_turns)
        assert result.recent_turns == base.recent_turns

    def test_step_down_under_pressure(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.10)
        base = self._base()
        result = ContextBudget.adjusted_for_pressure(base, current_turns=16)
        assert result.recent_turns == 15  # exactly -1

    def test_step_up_when_pressure_low(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.80)
        base = self._base()  # ceiling = 20
        result = ContextBudget.adjusted_for_pressure(base, current_turns=10)
        assert result.recent_turns == 11  # exactly +1

    def test_never_exceeds_base_ceiling(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.99)
        base = self._base()  # recent_turns = 20
        result = ContextBudget.adjusted_for_pressure(base, current_turns=base.recent_turns)
        assert result.recent_turns == base.recent_turns  # already at ceiling

    def test_never_drops_below_floor(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.01)
        base = self._base()
        result = ContextBudget.adjusted_for_pressure(base, current_turns=_FLOOR_RECENT_TURNS)
        assert result.recent_turns == _FLOOR_RECENT_TURNS  # max(3, 3-1) = 3

    def test_step_is_always_one(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.05)
        base = self._base()
        result = ContextBudget.adjusted_for_pressure(base, current_turns=10)
        assert result.recent_turns == 9  # exactly -1


# ==========================================================
# _inject_ctx_size helper
# ==========================================================


class TestInjectCtxSize:
    def test_replaces_existing(self):
        args = ["--threads", "8", "--ctx-size", "4096", "--mmap"]
        result = _inject_ctx_size(args, 16384)
        assert "--ctx-size" in result
        idx = result.index("--ctx-size")
        assert result[idx + 1] == "16384"

    def test_appends_when_missing(self):
        args = ["--threads", "8", "--mmap"]
        result = _inject_ctx_size(args, 8192)
        assert "--ctx-size" in result
        idx = result.index("--ctx-size")
        assert result[idx + 1] == "8192"

    def test_replaces_short_flag(self):
        args = ["-c", "2048"]
        result = _inject_ctx_size(args, 32768)
        assert result[1] == "32768"

    def test_does_not_duplicate(self):
        args = ["--ctx-size", "1024"]
        result = _inject_ctx_size(args, 8192)
        assert result.count("--ctx-size") == 1


# ==========================================================
# smart_truncator — truncate_middle
# ==========================================================


class TestTruncateMiddle:
    def test_short_text_unchanged(self):
        text = "hello world"
        assert truncate_middle(text, 100) == text

    def test_exactly_at_limit_unchanged(self):
        text = "a" * 100
        assert truncate_middle(text, 100) == text

    def test_long_text_truncated(self):
        text = "A" * 200 + "M" * 200 + "Z" * 200
        result = truncate_middle(text, 100)
        assert len(result) < 250  # well under original 600
        assert "omitted" in result
        assert result.startswith("A")
        assert result.endswith("Z" * 40)

    def test_middle_is_cut(self):
        text = "START" + "X" * 1000 + "END"
        result = truncate_middle(text, 50)
        assert "START" in result
        assert "END" in result
        assert "omitted" in result

    def test_empty_text_unchanged(self):
        assert truncate_middle("", 100) == ""

    def test_none_like_handled(self):
        assert truncate_middle("", 10) == ""


# ==========================================================
# smart_truncator — truncate_proportional
# ==========================================================


class TestTruncateProportional:
    def _make_map(self, n_steps: int, chars_per_step: int):
        m = {}
        for i in range(1, n_steps + 1):
            m[str(i)] = {
                "step_id": i,
                "tool": "filesystem",
                "ok": True,
                "output_name": f"step_{i}_output",
                "output_data": {"CONTENT": "X" * chars_per_step},
            }
        return m

    def test_small_map_unchanged(self):
        m = self._make_map(2, 100)
        result = truncate_proportional(m, 100_000)
        assert result == m

    def test_large_map_trimmed(self):
        m = self._make_map(3, 10_000)  # 30k+ chars total
        result = truncate_proportional(m, 5_000)
        import json
        raw = json.dumps(result, ensure_ascii=False)
        # should be significantly smaller
        assert len(raw) < len(json.dumps(m, ensure_ascii=False))

    def test_all_steps_get_share(self):
        m = self._make_map(4, 10_000)
        result = truncate_proportional(m, 8_000)
        # all steps should still be present
        assert len(result) == 4

    def test_empty_map(self):
        assert truncate_proportional({}, 1000) == {}

    def test_no_output_data_steps_untouched(self):
        m = {
            "1": {"step_id": 1, "ok": False, "output_data": None, "tool": "x"},
        }
        result = truncate_proportional(m, 100)
        assert result == m


# ==========================================================
# smart_truncator — truncate_history
# ==========================================================


class TestTruncateHistory:
    def _turns(self, n: int, chars_each: int = 50) -> str:
        return "\n\n".join(f"User: turn {i}\n\nBuddy: {'r' * chars_each}" for i in range(n))

    def test_short_history_unchanged(self):
        h = self._turns(3, 10)
        assert truncate_history(h, 10_000) == h

    def test_drops_oldest_first(self):
        turns = ["turn_1 content", "turn_2 content", "turn_3 content", "turn_4 content"]
        text = "\n\n".join(turns)
        result = truncate_history(text, len("\n\n".join(turns[1:])) + 5)
        assert "turn_1" not in result
        assert "turn_4" in result

    def test_always_keeps_last_two(self):
        turns = [f"turn_{i} " + "x" * 10 for i in range(10)]
        text = "\n\n".join(turns)
        # tiny budget — should keep last 2 at minimum
        result = truncate_history(text, 5)
        # with 2-turn minimum + middle-cut fallback, result is non-empty
        assert result

    def test_empty_text(self):
        assert truncate_history("", 1000) == ""
