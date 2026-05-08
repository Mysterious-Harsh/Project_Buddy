from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from buddy.prompts.analyzer_prompts import ANALYZER_TOOL_PROMPT

TOOL_NAME = "analyzer"

_FUNCTIONS = {"analyze", "summarize"}


class AnalyzerTool:
    def get_info(self) -> Dict[str, Any]:
        return {
            "name": TOOL_NAME,
            "version": "1.1.0",
            "description": "Pure reasoning — use when execution is not needed and thinking over prior step outputs is enough. Categorize files, make decisions, extract key findings, or condense long results into structured text a downstream step can act on. No filesystem, terminal, or network calls are made.",
            "prompt": ANALYZER_TOOL_PROMPT,
        }

    async def execute(
        self,
        function: str,
        arguments: Dict[str, Any],
        on_progress: Optional[Callable] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        fn = str(function or "").strip().lower()

        if fn not in _FUNCTIONS:
            return {
                "STATUS": "failed",
                "TOOL": TOOL_NAME,
                "ERROR": f"Unknown function: '{function}'. Available: {', '.join(sorted(_FUNCTIONS))}",
            }

        result = str(arguments.get("result") or "").strip()
        if not result:
            return {
                "STATUS": "failed",
                "TOOL": TOOL_NAME,
                "ERROR": f"{fn}() requires a non-empty 'result' argument.",
            }

        tag = "ANALYSIS" if fn == "analyze" else "SUMMARY"
        return {
            "STATUS": "success",
            "TOOL": TOOL_NAME,
            tag: result,
        }


def get_tool() -> AnalyzerTool:
    return AnalyzerTool()
