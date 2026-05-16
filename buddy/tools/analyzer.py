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
            "description": (
                "WHEN: reasoning over prior step outputs without touching files, web, or system — pure thinking, no execution.\n\n"
                "FUNCTIONS:\n"
                "  analyze(data, task)        — reason over provided content: categorize, decide, or answer a question\n"
                "  extract(data, schema)      — pull specific structured fields out of unstructured text\n"
                "  summarize(data, focus)     — condense a long result down to what matters for the current task\n\n"
                "INPUT: content must come from a prior step output — pass it directly in the instruction. "
                "Analyzer cannot fetch or read anything itself.\n\n"
                "CHAIN: insert between a data-producing step (web_fetch, terminal, fs_read.read, vision) and a downstream step that needs structured data. "
                "Analyzer output feeds word/pdf/excel create, or feeds the responder directly.\n"
                "NOT: anything requiring file access, network, or system calls — use the appropriate tool instead"
            ),
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
