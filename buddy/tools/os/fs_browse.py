from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from buddy.prompts.fs_browse_prompts import FS_BROWSE_TOOL_PROMPT
from buddy.tools.os.fs_utils import (
    MAX_DEPTH, MAX_DIR_ENTRIES,
    dir_entry, err, is_binary, make_matcher, ok, resolve_path, tree_label,
)

TOOL_NAME = "fs_browse"
_TOOL = TOOL_NAME


class FsBrowse:
    tool_name = _TOOL
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "WHEN: discovering what files and directories exist — list a directory or find files by name/content.\n\n"
                "FUNCTIONS:\n"
                "  ls(path, depth?)                                      — list directory flat or as a tree\n"
                "  find(path, pattern, type?, file_types?, max_results?) — find by name glob OR search text inside files\n\n"
                "CHAIN: ls/find output (paths) feeds fs_read.read or fs_manage as source paths.\n"
                "NOT: reading file contents → fs_read | writing/editing → fs_write | moving/deleting → fs_manage"
            ),
            "prompt": FS_BROWSE_TOOL_PROMPT,
        }

    async def execute(
        self,
        function: str,
        arguments: Dict[str, Any],
        on_progress: Optional[Callable] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        fn = str(function or "").strip().lower()
        if fn == "ls":
            return self._ls(arguments)
        if fn == "find":
            return self._find(arguments)
        return err(_TOOL, msg=f"Unknown function '{function}'. fs_browse supports: ls, find.")

    # ── ls ───────────────────────────────────────────────────────────────────

    def _ls(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return err(_TOOL, msg="ls requires 'path' — absolute path to a directory.")

        path = resolve_path(raw)
        p = Path(path)

        try:
            if not p.exists():
                return err(_TOOL, path=path, msg=(
                    f"ls failed — '{path}' does not exist. "
                    "Verify the path or use find to locate the directory."
                ))
            if not p.is_dir():
                try:
                    size_info = f" ({p.stat().st_size} bytes)"
                except OSError:
                    size_info = ""
                return err(_TOOL, path=path, msg=(
                    f"ls failed — '{path}'{size_info} is a file, not a directory. "
                    "Use fs_read.read to read file contents."
                ))

            depth = min(int(args.get("depth") or 1), MAX_DEPTH)
            show_hidden = bool(args.get("show_hidden", False))

            if depth > 1:
                return self._tree(p, depth=depth, show_hidden=show_hidden)
            return self._list(p, show_hidden=show_hidden)

        except PermissionError:
            return err(_TOOL, path=path, msg=(
                f"ls failed — permission denied reading directory '{path}'. "
                "Check directory permissions before retrying."
            ))
        except Exception as e:
            return err(_TOOL, path=path, msg=f"ls failed — {type(e).__name__}: {e}")

    def _list(self, p: Path, show_hidden: bool) -> Dict[str, Any]:
        try:
            children = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return err(_TOOL, path=str(p), msg=(
                f"ls failed — permission denied listing contents of '{p}'. "
                "The directory exists but its contents cannot be read."
            ))
        entries = [dir_entry(c) for c in children if show_hidden or not c.name.startswith(".")]
        return ok(_TOOL, path=str(p), ENTRIES=entries, TOTAL=len(entries))

    def _tree(self, p: Path, depth: int, show_hidden: bool) -> Dict[str, Any]:
        lines: List[str] = [str(p)]
        count = [0]

        def _walk(d: Path, prefix: str, cur: int) -> None:
            if cur > depth:
                return
            try:
                children = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except PermissionError:
                lines.append(f"{prefix}└── [permission denied]")
                return
            visible = [c for c in children if show_hidden or not c.name.startswith(".")]
            for i, child in enumerate(visible):
                if count[0] >= MAX_DIR_ENTRIES:
                    lines.append(f"{prefix}└── ... ({len(visible) - i} more)")
                    return
                is_last = i == len(visible) - 1
                lines.append(f"{prefix}{'└── ' if is_last else '├── '}{tree_label(child)}")
                count[0] += 1
                if child.is_dir():
                    _walk(child, prefix + ("    " if is_last else "│   "), cur + 1)

        _walk(p, "", 1)
        return ok(_TOOL, path=str(p), TREE_TEXT="\n".join(lines), TOTAL=count[0])

    # ── find ─────────────────────────────────────────────────────────────────

    def _find(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        pattern = str(args.get("pattern") or "").strip()
        find_type = str(args.get("type") or "name").strip().lower()
        recursive = bool(args.get("recursive", True))
        max_results = min(int(args.get("max_results") or 50), 500)
        context_lines = int(args.get("context_lines") or 0)
        file_types: Optional[List[str]] = args.get("file_types")

        if not raw:
            return err(_TOOL, msg="find requires 'path' — absolute directory to search in.")
        if not pattern:
            return err(_TOOL, msg="find requires 'pattern' — glob pattern (type=name) or text (type=content).")
        if find_type not in ("name", "content"):
            return err(_TOOL, msg=(
                f"find 'type' must be 'name' or 'content' — got '{find_type}'. "
                "Use 'name' for filename glob patterns (e.g. '*.py'), 'content' to search text inside files."
            ))

        path = resolve_path(raw)
        root = Path(path)

        if not root.exists():
            return err(_TOOL, path=path, msg=(
                f"find failed — root directory '{path}' does not exist. "
                "Verify the directory path before searching."
            ))
        if not root.is_dir():
            return err(_TOOL, path=path, msg=(
                f"find failed — '{path}' is a file, not a directory. "
                "Provide the parent directory that contains the files to search."
            ))

        try:
            if find_type == "name":
                return self._find_name(path, pattern, recursive, max_results)
            return self._find_content(path, pattern, recursive, max_results, context_lines, file_types)
        except Exception as e:
            return err(_TOOL, path=path, msg=f"find failed — {type(e).__name__}: {e}")

    def _find_name(self, path: str, pattern: str, recursive: bool, max_results: int) -> Dict[str, Any]:
        root = Path(path)
        results: List[Dict[str, Any]] = []
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)

        try:
            for match in iterator:
                e: Dict[str, Any] = {
                    "path": str(match),
                    "name": match.name,
                    "type": "dir" if match.is_dir() else "file",
                }
                if match.is_file():
                    try:
                        e["size_bytes"] = match.stat().st_size
                    except OSError:
                        pass
                results.append(e)
                if len(results) >= max_results:
                    break
        except PermissionError as pe:
            if not results:
                return err(_TOOL, path=path, msg=(
                    f"find name failed — permission denied searching '{path}': {pe}"
                ))

        return ok(_TOOL, path=path, TYPE="name", PATTERN=pattern, RESULTS=results, TOTAL_FOUND=len(results))

    def _find_content(
        self, path: str, pattern: str, recursive: bool,
        max_results: int, context_lines: int, file_types: Optional[List[str]],
    ) -> Dict[str, Any]:
        root = Path(path)
        matcher = make_matcher(pattern, case_sensitive=False, use_regex=True)
        results: List[Dict[str, Any]] = []
        skipped_binary = 0
        walker = root.rglob("*") if recursive else root.glob("*")

        for fp in walker:
            if len(results) >= max_results:
                break
            if not fp.is_file():
                continue
            if file_types and fp.suffix.lstrip(".").lower() not in file_types:
                continue
            if is_binary(fp):
                skipped_binary += 1
                continue
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                for i, line in enumerate(lines):
                    if not matcher(line):
                        continue
                    entry: Dict[str, Any] = {"file": str(fp), "line": i + 1, "text": line.rstrip()}
                    if context_lines > 0:
                        before = [ln.rstrip() for ln in lines[max(0, i - context_lines):i]]
                        after = [ln.rstrip() for ln in lines[i + 1:min(len(lines), i + 1 + context_lines)]]
                        if before:
                            entry["before"] = before
                        if after:
                            entry["after"] = after
                    results.append(entry)
                    if len(results) >= max_results:
                        break
            except Exception:
                continue

        r = ok(_TOOL, path=path, TYPE="content", PATTERN=pattern, RESULTS=results, TOTAL_FOUND=len(results))
        if skipped_binary:
            r["SKIPPED_BINARY"] = skipped_binary
        return r


TOOL_CLASS = FsBrowse


def get_tool() -> FsBrowse:
    return FsBrowse()
