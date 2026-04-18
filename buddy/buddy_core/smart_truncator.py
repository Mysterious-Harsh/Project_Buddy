from __future__ import annotations

# ==========================================================
# smart_truncator.py  —  v1.0.0
#
# Three truncation strategies for Buddy's context inputs.
# No LLM calls — pure string manipulation, zero latency.
#
# truncate_middle()       — file contents: keep head + tail, cut middle
# truncate_proportional() — execution results: each step gets equal share
# truncate_history()      — conversation history: drop oldest turn-by-turn
# ==========================================================

import json
from typing import Any, Dict

_TRUNCATION_MARKER = "\n[... {n} chars omitted ...]\n"


# ==========================================================
# 1. Middle-cut — for file / tool output contents
# ==========================================================


def truncate_middle(text: str, max_chars: int) -> str:
    """
    Keep the first 40% and last 40% of text, cut the middle.
    Files typically have the most signal in headers and footers.

    Adds a clear marker showing how many chars were dropped.
    Returns text unchanged if it already fits.
    """
    if not text or len(text) <= max_chars:
        return text

    head = int(max_chars * 0.40)
    tail = int(max_chars * 0.40)
    omitted = len(text) - head - tail
    marker = _TRUNCATION_MARKER.format(n=omitted)

    return text[:head] + marker + text[len(text) - tail :]


# ==========================================================
# 2. Proportional trim — for execution_results (responder input)
# ==========================================================


def truncate_proportional(
    step_execution_map: Dict[str, Any],
    max_total_chars: int,
    *,
    max_per_step_chars: int = 0,
) -> Dict[str, Any]:
    """
    Given the step_execution_map from ActionRouter, trim each step's
    output_data so the total JSON stays within max_total_chars.

    Strategy:
      - Count steps that have output_data.
      - Give each step an equal char budget: max_total_chars / n_steps.
      - If max_per_step_chars > 0, cap each step at that value too.
      - For each step, truncate the JSON of output_data using middle-cut.
      - Steps without output_data are left untouched.

    Returns a new dict — does not mutate the original.
    """
    if not step_execution_map:
        return step_execution_map

    # quick check — does it already fit?
    try:
        raw = json.dumps(step_execution_map, ensure_ascii=False)
    except Exception:
        return step_execution_map

    if len(raw) <= max_total_chars:
        return step_execution_map

    # steps that carry output data
    steps_with_data = [
        k
        for k, v in step_execution_map.items()
        if isinstance(v, dict) and v.get("output_data") is not None
    ]

    if not steps_with_data:
        return step_execution_map

    per_step_budget = max(512, max_total_chars // len(steps_with_data))
    if max_per_step_chars > 0:
        per_step_budget = min(per_step_budget, max_per_step_chars)

    result: Dict[str, Any] = {}
    for key, step in step_execution_map.items():
        if not isinstance(step, dict) or key not in steps_with_data:
            result[key] = step
            continue

        output_data = step.get("output_data")
        try:
            output_str = json.dumps(output_data, ensure_ascii=False)
        except Exception:
            output_str = str(output_data)

        if len(output_str) > per_step_budget:
            trimmed_str = truncate_middle(output_str, per_step_budget)
            # Store trimmed output as a plain string so JSON remains valid
            trimmed_step = dict(step)
            trimmed_step["output_data"] = trimmed_str
            trimmed_step["_output_truncated"] = True
            result[key] = trimmed_step
        else:
            result[key] = step

    return result


# ==========================================================
# 3. History trim — for conversation turns
# ==========================================================


def truncate_history(text: str, max_chars: int) -> str:
    """
    Trim conversation history to max_chars by dropping the oldest turns first.

    Assumptions about format produced by conversations.py:
      - Each turn is separated by a blank line OR starts with a role prefix
        like "User:" / "Buddy:" / "[timestamp]".
      - We split on double-newline as the turn boundary.

    Rules:
      - Always keep at least the last 2 turns (floor).
      - Drop oldest turns one at a time — never cut in half.
      - If even the last 2 turns exceed max_chars, middle-cut the whole string.
    """
    if not text or len(text) <= max_chars:
        return text

    # Split into turns on blank lines
    turns = [t.strip() for t in text.split("\n") if t.strip()]

    if len(turns) <= 2:
        # Can't drop any turns — fall back to middle-cut
        return truncate_middle(text, max_chars)

    # Drop oldest turns until it fits, keeping at least 2
    while len(turns) > 2:
        candidate = "\n".join(turns)
        if len(candidate) <= max_chars:
            return candidate
        turns.pop(0)  # drop oldest

    # Still too long with only 2 turns — middle-cut as last resort
    return truncate_middle("\n".join(turns), max_chars)


def truncate_memory(text: str, max_chars: int) -> str:
    """
    Alias for truncate_history — used for memory trimming in responder input.
    """
    mem = text.split("\n")
    while len(text) > max_chars:

        mem.pop(0)  # drop oldest line
        text = "\n".join(mem)

    return text
