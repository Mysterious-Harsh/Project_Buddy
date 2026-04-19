from __future__ import annotations

# ==========================================================
# edit_file.py
#
# Actions: write (create/overwrite), append, patch (find+replace)
# ==========================================================

import asyncio
import difflib
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional

from pydantic import BaseModel, model_validator

from buddy.prompts.edit_file_prompts import (
    EDIT_FILE_ERROR_PROMPT,
    EDIT_FILE_PROMPT,
    tool_call_format,
)
from buddy.tools.os._fs_helpers import (
    err,
    human_size,
    is_likely_binary,
    iso_time,
    needs_confirmation,
    ok,
    resolve_path,
)

# ==========================================================
# Input model
# ==========================================================


class EditFileCall(BaseModel):
    action: Literal["write", "append", "patch"]
    path: str
    content: Optional[str] = None  # write / append
    old_str: Optional[str] = None  # patch
    new_str: Optional[str] = None  # patch
    replace_all: bool = False  # patch
    confirmed: bool = False  # write / patch

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, values: Any) -> Any:
        if isinstance(values, dict):
            p = values.get("path")
            if p:
                values["path"] = resolve_path(str(p))
            # normalize action
            if values.get("action"):
                values["action"] = str(values["action"]).lower().strip()
        return values


# ==========================================================
# Tool
# ==========================================================


class EditFile:
    tool_name = "edit_file"
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "Create, write, overwrite, append to, or patch (find+replace) a file."
            ),
            "prompt": EDIT_FILE_PROMPT,
            "error_prompt": EDIT_FILE_ERROR_PROMPT,
            "tool_call_format": tool_call_format,
        }

    def parse_call(self, payload: Dict[str, Any]) -> EditFileCall:
        return EditFileCall.model_validate(payload)

    async def execute(
        self,
        call: EditFileCall,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        _verbs = {"write": "Writing", "append": "Appending", "patch": "Patching"}
        if on_progress:
            on_progress(f"{_verbs.get(call.action, 'Editing')} · {call.path}", False)
        dispatch = {
            "write": self._write,
            "append": self._append,
            "patch": self._patch,
        }
        try:
            return await asyncio.to_thread(dispatch[call.action], call)
        except Exception as exc:
            return err(self.tool_name, call.path, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------
    # write
    # ----------------------------------------------------------

    def _write(self, call: EditFileCall) -> Dict[str, Any]:
        p = Path(call.path)
        content = call.content or ""

        if not call.confirmed:
            if p.exists() and p.is_file():
                try:
                    old_size = p.stat().st_size
                    old_mod = iso_time(p.stat().st_mtime)
                except OSError:
                    old_size, old_mod = None, None
                preview = (
                    f"Will OVERWRITE {call.path} "
                    f"(currently {human_size(old_size)}, modified {old_mod}) "
                    f"with {len(content)} chars of new content. Cannot be undone."
                )
            else:
                preview = (
                    f"Will CREATE {call.path} with {len(content)} chars of content."
                )
            if not content:
                preview += " NOTE: content is empty — is this intentional?"
            return needs_confirmation(self.tool_name, call.path, preview)

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))
        return ok(
            self.tool_name,
            call.path,
            ACTION="write",
            SIZE_BYTES=len(content.encode("utf-8")),
        )

    # ----------------------------------------------------------
    # append
    # ----------------------------------------------------------

    def _append(self, call: EditFileCall) -> Dict[str, Any]:
        p = Path(call.path)
        content = call.content or ""
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
            size = p.stat().st_size
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))
        return ok(self.tool_name, call.path, ACTION="append", SIZE_BYTES=size)

    # ----------------------------------------------------------
    # patch
    # ----------------------------------------------------------

    def _patch(self, call: EditFileCall) -> Dict[str, Any]:
        p = Path(call.path)

        if not p.exists():
            return err(self.tool_name, call.path, f"File not found: {call.path}")
        if p.is_dir():
            return err(
                self.tool_name,
                call.path,
                "Path is a directory — patch requires a file.",
            )
        if is_likely_binary(p):
            return err(
                self.tool_name,
                call.path,
                f"Binary file ({p.suffix}) — patch works on text files only.",
            )
        if not call.old_str:
            return err(
                self.tool_name,
                call.path,
                "old_str is required — the exact text to find.",
            )

        try:
            original = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))

        count = original.count(call.old_str)
        new_str = call.new_str if call.new_str is not None else ""

        if count == 0:
            return err(
                self.tool_name,
                call.path,
                "old_str not found. Check exact whitespace, indentation, and line"
                " endings.",
            )

        if count > 1 and not call.replace_all:
            return err(
                self.tool_name,
                call.path,
                f"Found {count} occurrences of old_str. Set replace_all=true or add"
                " more context to old_str to make it unique.",
            )

        patched = (
            original.replace(call.old_str, new_str)
            if call.replace_all
            else original.replace(call.old_str, new_str, 1)
        )

        diff_lines = list(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                patched.splitlines(keepends=True),
                fromfile=call.path,
                tofile=call.path,
                lineterm="",
                n=2,
            )
        )
        diff_text = "\n".join(diff_lines[:60])

        if not call.confirmed:
            action_desc = (
                f"replace all {count} occurrence{'s' if count != 1 else ''}"
                if call.replace_all
                else "replace 1 occurrence"
            )
            return needs_confirmation(
                self.tool_name,
                call.path,
                f"Will PATCH {call.path} — {action_desc}:\n{diff_text}",
            )

        try:
            p.write_text(patched, encoding="utf-8")
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))

        return ok(
            self.tool_name,
            call.path,
            ACTION="patch",
            OCCURRENCES=count,
            SIZE_BYTES=len(patched.encode("utf-8")),
        )


# ==========================================================
# Registry hooks
# ==========================================================

TOOL_NAME = "edit_file"
TOOL_CLASS = EditFile


def get_tool() -> EditFile:
    return EditFile()
