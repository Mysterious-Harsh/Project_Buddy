from __future__ import annotations

# ==========================================================
# search_file.py
#
# Actions:
#   search — find FILES by name or content (file-level results)
#   grep   — find LINES matching text (line-level results + line numbers)
# ==========================================================

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from buddy.prompts.search_file_prompts import (
    SEARCH_FILE_ERROR_PROMPT,
    SEARCH_FILE_PROMPT,
    tool_call_format,
)
from buddy.brain.text_reader import CHAR_THRESHOLD, maybe_read
from buddy.tools.os._fs_helpers import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_HARD_LIMIT,
    compile_pattern,
    entry_dict,
    err,
    is_likely_binary,
    matches_file_types,
    ok,
    resolve_path,
)


# ==========================================================
# Input model
# ==========================================================

_GLOB_CHARS = set("*?[")


class SearchFileCall(BaseModel):
    action: Literal["search", "grep"] = "search"
    path: str
    pattern: Optional[str] = None
    content_query: Optional[str] = None
    recursive: bool = True
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=MAX_RESULTS_HARD_LIMIT)
    context_lines: int = Field(default=2, ge=0, le=10)
    file_types: Optional[List[str]] = None
    case_sensitive: bool = False
    regex: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        p = values.get("path")
        if p:
            values["path"] = resolve_path(str(p))

        if values.get("action"):
            values["action"] = str(values["action"]).lower().strip()

        # smart-fix: if grep has pattern with no glob chars and no content_query,
        # the LLM probably meant content_query
        action = values.get("action", "search")
        if action == "grep" and not values.get("content_query") and values.get("pattern"):
            pat = str(values["pattern"])
            if not any(c in pat for c in _GLOB_CHARS):
                values["content_query"] = pat
                values.pop("pattern", None)

        # normalize file_types: strip dots, lowercase
        if values.get("file_types"):
            values["file_types"] = [t.lstrip(".").strip().lower() for t in values["file_types"] if str(t).strip()]

        return values


# ==========================================================
# Tool
# ==========================================================

class SearchFile:
    tool_name = "search_file"
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "Find files by name or content (search), or find lines matching text inside files (grep). "
                "Use grep when you need line numbers for a follow-up read or edit."
            ),
            "prompt": SEARCH_FILE_PROMPT,
            "error_prompt": SEARCH_FILE_ERROR_PROMPT,
            "tool_call_format": tool_call_format,
        }

    def parse_call(self, payload: Dict[str, Any]) -> SearchFileCall:
        return SearchFileCall.model_validate(payload)

    async def execute(
        self,
        call: SearchFileCall,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        brain = _kwargs.get("brain")
        goal = str(_kwargs.get("goal") or "")
        if on_progress:
            on_progress(f"{'Searching' if call.action == 'search' else 'Grepping'} · {call.path}", False)
        try:
            if call.action == "grep":
                return await asyncio.to_thread(self._grep, call)
            return await asyncio.to_thread(self._search, call, brain=brain, goal=goal, on_progress=on_progress)
        except Exception as exc:
            return err(self.tool_name, call.path, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------
    # search
    # ----------------------------------------------------------

    def _search(
        self,
        call: SearchFileCall,
        brain: Any = None,
        goal: str = "",
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> Dict[str, Any]:
        root = Path(call.path)
        if not root.exists():
            return err(self.tool_name, call.path, f"Path does not exist: {call.path}")
        if not root.is_dir():
            return err(self.tool_name, call.path, "Path must be a directory for search.")

        pattern = call.pattern or "*"
        try:
            candidates = root.rglob(pattern) if call.recursive else root.glob(pattern)
        except Exception as exc:
            return err(self.tool_name, call.path, f"Glob pattern error: {exc}")

        matcher = None
        if call.content_query:
            try:
                matcher = compile_pattern(call.content_query, case_sensitive=call.case_sensitive, use_regex=call.regex)
            except ValueError as exc:
                return err(self.tool_name, call.path, str(exc))

        matches: List[Dict[str, Any]] = []
        total = 0

        for p in candidates:
            if any(part.startswith(".") for part in p.parts[len(root.parts):]):
                continue
            if p.is_file() and not matches_file_types(p, call.file_types):
                continue

            if matcher:
                if not p.is_file() or is_likely_binary(p):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                match_line = None
                for i, line in enumerate(text.splitlines(), 1):
                    if matcher(line):
                        match_line = {"line_number": i, "line": line.rstrip()}
                        break
                if match_line is None and not matcher(text):
                    continue
                entry = entry_dict(p)
                if match_line:
                    entry["match"] = match_line
                # For large files with a content_query, include a TextReader excerpt
                if brain and goal and len(text) > CHAR_THRESHOLD:
                    excerpt = maybe_read(text, goal, brain, on_progress)
                    if excerpt:
                        entry["excerpt"] = excerpt
                total += 1
                if len(matches) < call.max_results:
                    matches.append(entry)
            else:
                total += 1
                if len(matches) < call.max_results:
                    matches.append(entry_dict(p))

        result = ok(self.tool_name, call.path, ACTION="search", RESULTS=matches, TOTAL_FOUND=total)
        if total == 0:
            result["NOTE"] = "0 results. Try broader pattern, remove file_types filter, or try parent directory."
        elif total > call.max_results:
            result["NOTE"] = f"Showing {call.max_results} of {total}. Increase max_results or narrow pattern."
        return result

    # ----------------------------------------------------------
    # grep
    # ----------------------------------------------------------

    def _grep(self, call: SearchFileCall) -> Dict[str, Any]:
        root = Path(call.path)
        if not root.exists():
            return err(self.tool_name, call.path, f"Path does not exist: {call.path}")

        query = call.content_query or ""
        if not query:
            return err(self.tool_name, call.path, "grep requires content_query — the text or regex to find.")

        try:
            matcher = compile_pattern(query, case_sensitive=call.case_sensitive, use_regex=call.regex)
        except ValueError as exc:
            return err(self.tool_name, call.path, str(exc))

        if root.is_file():
            if is_likely_binary(root):
                return err(self.tool_name, call.path, f"Binary file ({root.suffix}) cannot be grepped.")
            files_to_search = [root]
        else:
            file_pattern = call.pattern or "*"
            try:
                candidates = root.rglob(file_pattern) if call.recursive else root.glob(file_pattern)
            except Exception as exc:
                return err(self.tool_name, call.path, f"Glob pattern error: {exc}")
            files_to_search = [
                p for p in candidates
                if p.is_file()
                and not is_likely_binary(p)
                and matches_file_types(p, call.file_types)
                and not any(part.startswith(".") for part in p.parts[len(root.parts):])
            ]

        results: List[Dict[str, Any]] = []
        total = 0

        for file_path in files_to_search:
            try:
                lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if not matcher(line):
                    continue
                total += 1
                if len(results) < call.max_results:
                    entry: Dict[str, Any] = {"path": str(file_path), "line_number": i + 1, "line": line.rstrip()}
                    ctx_before = [l.rstrip() for l in lines[max(0, i - call.context_lines): i]]
                    ctx_after = [l.rstrip() for l in lines[i + 1: i + 1 + call.context_lines]]
                    if ctx_before:
                        entry["context_before"] = ctx_before
                    if ctx_after:
                        entry["context_after"] = ctx_after
                    results.append(entry)

        result = ok(self.tool_name, call.path, ACTION="grep", RESULTS=results, TOTAL_FOUND=total)
        if total == 0:
            result["NOTE"] = "0 matches. Try case_sensitive=false, simplify content_query, or broaden pattern."
        elif total > call.max_results:
            result["NOTE"] = f"Showing {call.max_results} of {total} matches. Increase max_results or narrow query."
        return result


# ==========================================================
# Registry hooks
# ==========================================================

TOOL_NAME = "search_file"
TOOL_CLASS = SearchFile


def get_tool() -> SearchFile:
    return SearchFile()
