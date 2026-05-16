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
    _FLOOR_TURNS,
    _RESPONSE_PCT,
    _ALLOC_HISTORY,
    _ALLOC_MEMORY,
    _ALLOC_EXEC,
)
from buddy.buddy_core.boot import _inject_ctx_size
from buddy.buddy_core.smart_truncator import (
    truncate_middle,
    truncate_proportional,
)


# ==========================================================
# ContextBudget — _build() (internal constructor)
# ==========================================================


class TestBuildDirect:
    def _b(self, n_ctx=8192):
        return ContextBudget._build(
            n_ctx=n_ctx, recent_turns=14, top_k_memories=14,
            pre_rerank_k=20, backend="cpu_only", tier="test",
        )

    def test_response_tokens_is_20pct(self):
        b = self._b(16384)
        assert b.response_tokens == max(64, int(16384 * _RESPONSE_PCT))
        assert b.response_tokens + b.max_prompt_tokens == b.n_ctx

    def test_token_slots_are_percentages_of_n_ctx(self):
        b = self._b(8192)
        assert b.max_history_tokens == max(64, int(8192 * _ALLOC_HISTORY))
        assert b.max_memory_tokens  == max(64, int(8192 * _ALLOC_MEMORY))
        assert b.max_exec_tokens    == max(64, int(8192 * _ALLOC_EXEC))

    def test_char_ceilings_derived_from_token_fields(self):
        b = self._b(8192)
        cpt = b.chars_per_token
        assert b.max_history_chars == int(b.max_history_tokens * cpt)
        assert b.max_memory_chars  == int(b.max_memory_tokens  * cpt)
        assert b.max_exec_chars    == int(b.max_exec_tokens    * cpt)

    def test_no_tool_token_field(self):
        b = self._b()
        assert not hasattr(b, "max_tool_tokens")
        assert not hasattr(b, "max_tool_chars")

    def test_all_fields_positive(self):
        b = self._b(2048)
        assert b.max_history_chars > 0
        assert b.max_memory_chars  > 0
        assert b.max_exec_chars    > 0
        assert b.max_prompt_tokens > 0
        assert b.response_tokens   > 0

    def test_larger_n_ctx_gives_larger_budgets(self):
        small = self._b(4096)
        large = ContextBudget._build(
            n_ctx=32768, recent_turns=30, top_k_memories=24,
            pre_rerank_k=36, backend="metal", tier="test",
        )
        assert large.max_history_tokens > small.max_history_tokens
        assert large.max_prompt_tokens  > small.max_prompt_tokens
        assert large.max_exec_chars     > small.max_exec_chars


# ==========================================================
# ContextBudget — from_hardware()  (integration smoke tests)
# ==========================================================


class TestFromHardware:
    def _profile(self, backend, ram_gb, vram_gb=0):
        return {
            "hardware": {
                "gpu": {"backend": backend, "vram_gb": vram_gb},
                "ram": {"total_gb": ram_gb},
            }
        }

    def test_metal_returns_valid_budget(self):
        b = ContextBudget.from_hardware(self._profile("metal", 16), model_size_gb=7.0)
        assert b.backend == "metal"
        assert b.n_ctx > 0
        assert b.max_prompt_tokens > 0
        assert b.recent_turns >= _FLOOR_TURNS

    def test_cpu_only_returns_valid_budget(self):
        b = ContextBudget.from_hardware(self._profile("cpu_only", 16), model_size_gb=7.0)
        assert b.backend == "cpu_only"
        assert b.n_ctx > 0

    def test_empty_profile_returns_valid_budget(self):
        b = ContextBudget.from_hardware({}, model_size_gb=7.0)
        assert b.n_ctx > 0
        assert b.recent_turns >= _FLOOR_TURNS

    def test_larger_ram_gives_larger_or_equal_n_ctx(self):
        low  = ContextBudget.from_hardware(self._profile("metal", 8),  model_size_gb=4.0)
        high = ContextBudget.from_hardware(self._profile("metal", 64), model_size_gb=4.0)
        assert high.n_ctx >= low.n_ctx

    def test_char_budgets_are_positive(self):
        b = ContextBudget.from_hardware(self._profile("metal", 16), model_size_gb=7.0)
        assert b.max_history_chars > 0
        assert b.max_memory_chars  > 0
        assert b.max_exec_chars    > 0


# ==========================================================
# ContextBudget — from_override()
# ==========================================================


class TestFromOverride:
    def _base(self):
        return ContextBudget._build(
            n_ctx=16384, recent_turns=20, top_k_memories=18,
            pre_rerank_k=28, backend="metal", tier="test",
        )

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
        assert result.tier == "manual_override"

    def test_override_partial_uses_base_for_missing(self):
        base = self._base()
        result = ContextBudget.from_override(base, {"override": True, "n_ctx": 8192})
        assert result.n_ctx == 8192
        assert result.recent_turns == base.recent_turns

    def test_override_recomputes_token_fields(self):
        base = self._base()
        result = ContextBudget.from_override(base, {"override": True, "n_ctx": 4096})
        assert result.max_prompt_tokens == 4096 - result.response_tokens
        assert result.response_tokens == max(64, int(4096 * _RESPONSE_PCT))


# ==========================================================
# ContextBudget — adjusted_for_pressure()
# ==========================================================


class TestPressureAdjustment:
    def _base(self):
        return ContextBudget._build(
            n_ctx=16384, recent_turns=20, top_k_memories=18,
            pre_rerank_k=28, backend="metal", tier="test",
        )

    def test_nominal_no_change(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.25)
        base = self._base()
        result = base.adjusted_for_pressure(current_turns=base.recent_turns)
        assert result.pressure_level == "nominal"
        assert result.recent_turns == base.recent_turns

    def test_low_pressure_scales_down(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.12)  # low: < 0.15
        base = self._base()
        result = base.adjusted_for_pressure(current_turns=20)
        assert result.pressure_level == "low"
        assert result.recent_turns < base.recent_turns
        assert result.max_history_tokens < base.max_history_tokens
        assert result.max_memory_tokens  < base.max_memory_tokens
        assert result.max_exec_tokens    < base.max_exec_tokens

    def test_high_memory_scales_up(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.50)  # high: > 0.40
        base = self._base()
        result = base.adjusted_for_pressure(current_turns=base.recent_turns)
        assert result.pressure_level == "high"
        assert result.max_history_tokens > base.max_history_tokens
        assert result.max_memory_tokens  > base.max_memory_tokens

    def test_never_drops_below_floor(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.01)  # critical
        base = self._base()
        result = base.adjusted_for_pressure(current_turns=_FLOOR_TURNS)
        assert result.recent_turns >= _FLOOR_TURNS

    def test_critical_applies_040_scale(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.05)  # critical
        base = self._base()
        result = base.adjusted_for_pressure(current_turns=20)
        assert result.pressure_level == "critical"
        assert result.max_history_tokens == max(64, int(base.max_history_tokens * 0.40))
        assert result.max_exec_tokens    == max(64, int(base.max_exec_tokens    * 0.40))

    def test_char_ceilings_stay_in_sync_after_pressure(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.12)
        base = self._base()
        result = base.adjusted_for_pressure(current_turns=15)
        cpt = result.chars_per_token
        assert result.max_history_chars == int(result.max_history_tokens * cpt)
        assert result.max_memory_chars  == int(result.max_memory_tokens  * cpt)
        assert result.max_exec_chars    == int(result.max_exec_tokens    * cpt)

    def test_instance_method_matches_classmethod(self, monkeypatch):
        import buddy.buddy_core.context_budget as mod
        monkeypatch.setattr(mod, "_free_memory_pct", lambda b: 0.12)
        base = self._base()
        via_instance = base.adjusted_for_pressure(current_turns=15)
        via_class    = ContextBudget.adjust_for_pressure(base, current_turns=15)
        assert via_instance == via_class


# ==========================================================
# ContextBudget — calibrate()
# ==========================================================


class TestCalibrate:
    def _base(self):
        return ContextBudget._build(
            n_ctx=16384, recent_turns=20, top_k_memories=18,
            pre_rerank_k=28, backend="metal", tier="test",
        )

    def test_calibrate_updates_chars_per_token(self):
        base = self._base()
        b2 = base.calibrate(38000, 12000)  # 3.167 cpt — different from default 3.5
        assert b2.chars_per_token != base.chars_per_token

    def test_calibrate_refreshes_char_ceilings(self):
        base = self._base()
        b2 = base.calibrate(42000, 12000)
        cpt = b2.chars_per_token
        assert b2.max_history_chars == int(b2.max_history_tokens * cpt)
        assert b2.max_memory_chars  == int(b2.max_memory_tokens  * cpt)
        assert b2.max_exec_chars    == int(b2.max_exec_tokens    * cpt)

    def test_calibrate_token_fields_unchanged(self):
        base = self._base()
        b2 = base.calibrate(42000, 12000)
        assert b2.max_history_tokens == base.max_history_tokens
        assert b2.max_memory_tokens  == base.max_memory_tokens
        assert b2.max_exec_tokens    == base.max_exec_tokens
        assert b2.max_prompt_tokens  == base.max_prompt_tokens

    def test_calibrate_cpt_clamped_low(self):
        base = self._base()
        b2 = base.calibrate(100, 10000)   # 0.01 cpt — clamps to 1.5
        assert b2.chars_per_token >= 1.5

    def test_calibrate_cpt_clamped_high(self):
        base = self._base()
        b2 = base.calibrate(100000, 100)  # 1000 cpt — clamps to 8.0
        assert b2.chars_per_token <= 8.0

    def test_calibrate_zero_inputs_noop(self):
        base = self._base()
        assert base.calibrate(0, 100) is base
        assert base.calibrate(100, 0) is base


# ==========================================================
# _inject_ctx_size helper
# ==========================================================


class TestInjectCtxSize:
    def test_replaces_existing(self):
        args = ["--threads", "8", "--ctx-size", "4096", "--mmap"]
        result = _inject_ctx_size(args, 16384)
        assert "--ctx-size" in result
        assert result[result.index("--ctx-size") + 1] == "16384"

    def test_appends_when_missing(self):
        args = ["--threads", "8", "--mmap"]
        result = _inject_ctx_size(args, 8192)
        assert "--ctx-size" in result
        assert result[result.index("--ctx-size") + 1] == "8192"

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
        assert len(result) < 250
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
        return {
            str(i): {
                "step_id": i,
                "tool": "filesystem",
                "ok": True,
                "output_name": f"step_{i}_output",
                "output_data": {"CONTENT": "X" * chars_per_step},
            }
            for i in range(1, n_steps + 1)
        }

    def test_small_map_unchanged(self):
        m = self._make_map(2, 100)
        assert truncate_proportional(m, 100_000) == m

    def test_large_map_trimmed(self):
        import json
        m = self._make_map(3, 10_000)
        result = truncate_proportional(m, 5_000)
        assert len(json.dumps(result)) < len(json.dumps(m))

    def test_all_steps_present_after_trim(self):
        m = self._make_map(4, 10_000)
        result = truncate_proportional(m, 8_000)
        assert len(result) == 4

    def test_empty_map(self):
        assert truncate_proportional({}, 1000) == {}

    def test_no_output_data_steps_untouched(self):
        m = {"1": {"step_id": 1, "ok": False, "output_data": None, "tool": "x"}}
        assert truncate_proportional(m, 100) == m
