from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from buddy.prompts.fs_write_prompts import FS_WRITE_TOOL_PROMPT
from buddy.tools.os.fs_utils import err, human_size, iso_time, needs_confirm, ok, resolve_path

TOOL_NAME = "fs_write"
_TOOL = TOOL_NAME


class FsWrite:
    tool_name = _TOOL
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "WHEN: creating, appending to, or editing a plain text file.\n\n"
                "TEXT FILES — use fs_write for ANY plain text format:\n"
                "  .txt .md .csv .json .yaml .yml .toml .xml .html .htm .css .js .ts .py .sh .bat\n"
                "  .log .env .ini .cfg .conf .rst .tex and any other text-based file.\n\n"
                "FUNCTIONS:\n"
                "  write(path, action, content?)  — action: 'create' | 'append' | 'patch'\n"
                "    create: new file or overwrite existing; requires confirmed=true if file already exists\n"
                "    append: add to end; creates file if missing\n"
                "    patch(old_str, new_str): replace exact text in-place; read first to get exact strings\n\n"
                "CHAIN: fs_read.read content → fs_write.patch when editing. fs_write is typically a final step.\n"
                "NOT: listing/finding → fs_browse | reading → fs_read | moving/deleting → fs_manage | Excel structured edits → excel | .docx create/edit → word | .pdf create/edit → pdf"
            ),
            "prompt": FS_WRITE_TOOL_PROMPT,
        }

    async def execute(
        self,
        function: str,
        arguments: Dict[str, Any],
        on_progress: Optional[Callable] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        fn = str(function or "").strip().lower()
        if fn == "write":
            return self._write(arguments)
        return err(_TOOL, msg=f"Unknown function '{function}'. fs_write supports: write.")

    def _write(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        action = str(args.get("action") or "").strip().lower()

        if not raw:
            return err(_TOOL, msg="write requires 'path' — absolute path to the file.")
        if action not in ("create", "append", "patch"):
            return err(_TOOL, action=action, msg=(
                f"write 'action' must be 'create', 'append', or 'patch' — got '{action}'."
            ))

        path = resolve_path(raw)
        p = Path(path)

        try:
            if action == "create":
                return self._create(p, path, args)
            if action == "append":
                return self._append(p, path, args)
            return self._patch(p, path, args)

        except PermissionError:
            return err(_TOOL, path=path, action=action, msg=(
                f"write {action} failed — permission denied on '{path}'. "
                "Check file and directory permissions before retrying."
            ))
        except OSError as e:
            return err(_TOOL, path=path, action=action, msg=(
                f"write {action} failed — {type(e).__name__}: {e}"
            ))
        except Exception as e:
            return err(_TOOL, path=path, action=action, msg=(
                f"write {action} failed — {type(e).__name__}: {e}"
            ))

    def _create(self, p: Path, path: str, args: Dict[str, Any]) -> Dict[str, Any]:
        content = str(args.get("content") or "")

        if p.exists() and not args.get("confirmed"):
            try:
                s = p.stat()
                size_info = f" ({human_size(s.st_size)}, last modified {iso_time(s.st_mtime)})"
            except OSError:
                size_info = ""
            return needs_confirm(
                _TOOL, path,
                f"write create aborted — '{path}'{size_info} already exists. "
                "Set confirmed=true after user confirms overwrite."
            )

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ok(_TOOL, path=path, ACTION="create", SIZE_BYTES=p.stat().st_size)

    def _append(self, p: Path, path: str, args: Dict[str, Any]) -> Dict[str, Any]:
        content = str(args.get("content") or "")
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
        return ok(_TOOL, path=path, ACTION="append", SIZE_BYTES=p.stat().st_size)

    def _patch(self, p: Path, path: str, args: Dict[str, Any]) -> Dict[str, Any]:
        old_str = args.get("old_str")
        new_str = args.get("new_str")

        if old_str is None:
            return err(_TOOL, path=path, action="patch", msg=(
                "write patch requires 'old_str' — the exact text to replace. "
                "Read the file first with fs_read.read to get the exact current content."
            ))
        if new_str is None:
            return err(_TOOL, path=path, action="patch", msg=(
                "write patch requires 'new_str' — the replacement text."
            ))
        if not p.exists():
            return err(_TOOL, path=path, action="patch", msg=(
                f"write patch failed — '{path}' does not exist. "
                "Verify the path or create the file with action='create'."
            ))

        try:
            original = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                original = p.read_text(encoding="latin-1")
            except OSError as e:
                return err(_TOOL, path=path, action="patch", msg=(
                    f"write patch failed — could not read '{p.name}' for patching: {type(e).__name__}: {e}"
                ))

        count = original.count(old_str)

        if count == 0:
            excerpt = original[:300].replace("\n", "↵ ")
            return err(_TOOL, path=path, action="patch", msg=(
                f"write patch failed — old_str not found in '{p.name}' ({len(original)} chars). "
                "Read the file with fs_read.read to get exact current content, then retry with the correct text. "
                f"File starts with: {excerpt!r}"
            ))

        if count > 1:
            return err(_TOOL, path=path, action="patch", msg=(
                f"write patch failed — old_str matched {count} times in '{p.name}'. "
                "Expand old_str to include more surrounding lines until it matches exactly once."
            ))

        p.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")
        return ok(_TOOL, path=path, ACTION="patch", SIZE_BYTES=p.stat().st_size)


TOOL_CLASS = FsWrite


def get_tool() -> FsWrite:
    return FsWrite()
