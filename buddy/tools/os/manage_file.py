from __future__ import annotations

# ==========================================================
# manage_file.py
#
# Actions: copy, move, delete, mkdir, open, diff
# ==========================================================

import asyncio
import difflib
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional

from pydantic import BaseModel, model_validator

from buddy.prompts.manage_file_prompts import (
    MANAGE_FILE_ERROR_PROMPT,
    MANAGE_FILE_PROMPT,
    tool_call_format,
)
from buddy.tools.os._fs_helpers import (
    DEFAULT_MAX_CHARS,
    err,
    human_size,
    is_likely_binary,
    needs_confirmation,
    ok,
    resolve_path,
)


# ==========================================================
# Input model
# ==========================================================

class ManageFileCall(BaseModel):
    action: Literal["copy", "move", "delete", "mkdir", "open", "diff"]
    path: str
    destination: Optional[str] = None
    confirmed: bool = False
    max_chars: int = DEFAULT_MAX_CHARS  # diff output limit

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        for field in ("path", "destination"):
            v = values.get(field)
            if v:
                values[field] = resolve_path(str(v))
        if values.get("action"):
            values["action"] = str(values["action"]).lower().strip()
        return values


# ==========================================================
# Tool
# ==========================================================

class ManageFile:
    tool_name = "manage_file"
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": "Copy, move, delete, create directories, open files, compare (diff) two files.",
            "prompt": MANAGE_FILE_PROMPT,
            "error_prompt": MANAGE_FILE_ERROR_PROMPT,
            "tool_call_format": tool_call_format,
        }

    def parse_call(self, payload: Dict[str, Any]) -> ManageFileCall:
        return ManageFileCall.model_validate(payload)

    async def execute(
        self,
        call: ManageFileCall,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        _verbs = {
            "copy": "Copying", "move": "Moving", "delete": "Deleting",
            "mkdir": "Creating folder", "open": "Opening", "diff": "Comparing",
        }
        if on_progress:
            on_progress(f"{_verbs.get(call.action, call.action.capitalize())} · {call.path}", False)
        dispatch = {
            "copy": self._copy,
            "move": self._move,
            "delete": self._delete,
            "mkdir": self._mkdir,
            "open": self._open,
            "diff": self._diff,
        }
        try:
            return await asyncio.to_thread(dispatch[call.action], call)
        except Exception as exc:
            return err(self.tool_name, call.path, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------
    # copy
    # ----------------------------------------------------------

    def _copy(self, call: ManageFileCall) -> Dict[str, Any]:
        if not call.destination:
            return err(self.tool_name, call.path, "destination is required for copy.")
        src, dst = Path(call.path), Path(call.destination)
        if not src.exists():
            return err(self.tool_name, call.path, f"Source does not exist: {call.path}")

        if dst.is_dir():
            dst = dst / src.name

        if src.resolve() == dst.resolve():
            return err(self.tool_name, call.path, "Source and destination are the same path.")

        # Overwrite guard
        if dst.exists() and not call.confirmed:
            return needs_confirmation(
                self.tool_name, call.path,
                f"Destination {dst} already exists. Will OVERWRITE it with copy of {call.path}. Cannot be undone.",
            )

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))
        return ok(self.tool_name, call.path, ACTION="copy", DESTINATION=str(dst))

    # ----------------------------------------------------------
    # move
    # ----------------------------------------------------------

    def _move(self, call: ManageFileCall) -> Dict[str, Any]:
        if not call.destination:
            return err(self.tool_name, call.path, "destination is required for move.")
        src, dst = Path(call.path), Path(call.destination)
        if not src.exists():
            return err(self.tool_name, call.path, f"Source does not exist: {call.path}")
        if dst.is_dir():
            dst = dst / src.name
        if src.resolve() == dst.resolve():
            return err(self.tool_name, call.path, "Source and destination are the same path.")

        if not call.confirmed:
            exists_note = f"Destination {dst} {'already exists and will be replaced' if dst.exists() else 'does not exist'}."
            return needs_confirmation(self.tool_name, call.path, f"Will MOVE {call.path} → {dst}. {exists_note} Cannot be undone.")

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))
        return ok(self.tool_name, call.path, ACTION="move", DESTINATION=str(dst))

    # ----------------------------------------------------------
    # delete
    # ----------------------------------------------------------

    def _delete(self, call: ManageFileCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return err(self.tool_name, call.path, f"Path does not exist: {call.path}")

        if not call.confirmed:
            if p.is_file():
                try:
                    size = human_size(p.stat().st_size)
                except OSError:
                    size = "unknown size"
                preview = f"Will permanently DELETE file {call.path} ({size}). Cannot be undone."
            else:
                try:
                    count = sum(1 for _ in p.rglob("*"))
                    preview = f"Will permanently DELETE directory {call.path} and all {count} items inside. Cannot be undone."
                except Exception:
                    preview = f"Will permanently DELETE directory {call.path} and all its contents. Cannot be undone."
            return needs_confirmation(self.tool_name, call.path, preview)

        try:
            if p.is_dir():
                shutil.rmtree(str(p))
            else:
                p.unlink()
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))
        return ok(self.tool_name, call.path, ACTION="delete")

    # ----------------------------------------------------------
    # mkdir
    # ----------------------------------------------------------

    def _mkdir(self, call: ManageFileCall) -> Dict[str, Any]:
        p = Path(call.path)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))
        return ok(self.tool_name, call.path, ACTION="mkdir")

    # ----------------------------------------------------------
    # open
    # ----------------------------------------------------------

    def _open(self, call: ManageFileCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return err(self.tool_name, call.path, f"Path does not exist: {call.path}")
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.Popen(["open", str(p)])
            elif system == "Linux":
                subprocess.Popen(["xdg-open", str(p)])
            elif system == "Windows":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                return err(self.tool_name, call.path, f"Unsupported platform: {system}")
        except Exception as exc:
            return err(self.tool_name, call.path, f"Failed to open: {exc}")
        return ok(self.tool_name, call.path, ACTION="open", OPENED=True)

    # ----------------------------------------------------------
    # diff
    # ----------------------------------------------------------

    def _diff(self, call: ManageFileCall) -> Dict[str, Any]:
        if not call.destination:
            return err(self.tool_name, call.path, "destination (second file path) is required for diff.")
        a, b = Path(call.path), Path(call.destination)
        for p, label in [(a, "path"), (b, "destination")]:
            if not p.exists():
                return err(self.tool_name, call.path, f"{label} does not exist: {p}")
            if p.is_dir():
                return err(self.tool_name, call.path, f"{label} is a directory — diff requires two files.")
            if is_likely_binary(p):
                return err(self.tool_name, call.path, f"{label} is binary ({p.suffix}) — cannot diff.")

        try:
            a_lines = a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            b_lines = b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))

        diff = list(difflib.unified_diff(a_lines, b_lines, fromfile=str(a), tofile=str(b), lineterm=""))

        if not diff:
            return ok(self.tool_name, call.path, ACTION="diff", IDENTICAL=True)

        diff_text = "\n".join(diff)
        truncated = len(diff_text) > call.max_chars
        result = ok(
            self.tool_name, call.path,
            ACTION="diff",
            IDENTICAL=False,
            DIFF_TEXT=diff_text[: call.max_chars] if truncated else diff_text,
        )
        if truncated:
            result["TRUNCATED"] = True
            result["NOTE"] = f"Diff truncated at {call.max_chars} chars."
        return result


# ==========================================================
# Registry hooks
# ==========================================================

TOOL_NAME = "manage_file"
TOOL_CLASS = ManageFile


def get_tool() -> ManageFile:
    return ManageFile()
