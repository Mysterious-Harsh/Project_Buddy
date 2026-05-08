from __future__ import annotations

import mimetypes
import os
import re
import shutil
import stat as _stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from buddy.prompts.filesystem_prompts import FILESYSTEM_TOOL_PROMPT

# ── constants ───────────────────────────────────────────────────────────────

_MAX_CHARS = 8_000
_MAX_CHARS_HARD = 50_000
_MAX_DIR_ENTRIES = 200
_DEFAULT_DEPTH = 3
_MAX_DEPTH = 10
_TOOL = "filesystem"

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
    ".mp3", ".mp4", ".wav", ".flac", ".aac", ".ogg",
    ".avi", ".mov", ".mkv", ".wmv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".pptx", ".db", ".sqlite", ".sqlite3",
    ".pyc", ".pyo",
}
TABULAR_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".feather", ".orc"}
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx", ".doc"}

# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve(raw: str) -> str:
    p = os.path.expanduser(os.path.expandvars(raw.strip()))
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser("~"), p)
    return p


def _human(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_binary(p: Path) -> bool:
    if p.suffix.lower() in BINARY_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(str(p))
    if mime and not mime.startswith("text/"):
        return True
    try:
        with open(p, "rb") as f:
            return b"\x00" in f.read(1024)
    except Exception:
        return False


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[{len(text) - limit} chars truncated]", True


def _ok(path: str = "", **extra: Any) -> Dict[str, Any]:
    r: Dict[str, Any] = {"STATUS": "success", "TOOL": _TOOL}
    if path:
        r["PATH"] = path
    r.update(extra)
    return r


def _err(path: str = "", action: str = "", msg: str = "") -> Dict[str, Any]:
    r: Dict[str, Any] = {"STATUS": "failed", "TOOL": _TOOL}
    if action:
        r["ACTION"] = action
    if path:
        r["PATH"] = path
    r["ERROR"] = msg
    return r


def _needs_confirm(path: str, preview: str) -> Dict[str, Any]:
    return {
        "STATUS": "failed", "TOOL": _TOOL, "PATH": path,
        "NEEDS_CONFIRMATION": True,
        "PREVIEW": preview,
        "NOTE": "Call again with confirmed=true after user approves.",
    }


def _entry(p: Path) -> Dict[str, Any]:
    try:
        s = p.stat()
        is_dir = p.is_dir()
        e: Dict[str, Any] = {
            "name": p.name, "path": str(p),
            "type": "dir" if is_dir else "file",
            "permissions": _stat.filemode(s.st_mode),
            "modified": _iso(s.st_mtime),
            "created": _iso(s.st_ctime),
        }
        if is_dir:
            try:
                e["item_count"] = sum(1 for _ in p.iterdir())
            except PermissionError:
                e["item_count"] = None
        else:
            e["size_bytes"] = s.st_size
            e["size"] = _human(s.st_size)
            if p.suffix:
                e["extension"] = p.suffix.lower()
        return e
    except OSError:
        return {"name": p.name, "path": str(p), "type": "unknown"}


def _tree_label(p: Path) -> str:
    try:
        s = p.stat()
        date = datetime.fromtimestamp(s.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        if p.is_dir():
            try:
                count = sum(1 for _ in p.iterdir())
                return f"{p.name}/ ({count} items, {date})"
            except PermissionError:
                return f"{p.name}/ ({date})"
        return f"{p.name} ({_human(s.st_size)}, {date})"
    except OSError:
        return p.name + ("/" if p.is_dir() else "")


def _compile(pattern: str, *, case_sensitive: bool = False, use_regex: bool = False):
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        try:
            return re.compile(pattern, flags).search
        except re.error:
            return re.compile(re.escape(pattern), flags).search
    needle = pattern if case_sensitive else pattern.lower()
    return (lambda t: needle in t) if case_sensitive else (lambda t: needle in t.lower())


# ── tool ────────────────────────────────────────────────────────────────────


class Filesystem:
    tool_name = _TOOL
    version = "2.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "Read, write, search, and manage files and directories.\n"
                "ls     — directory listing or tree\n"
                "read   — file content (text/PDF/DOCX/tabular/binary) or metadata\n"
                "write  — create / append / patch files\n"
                "find   — files by name glob or content search\n"
                "manage — copy / move / delete / mkdir (batch, multiple paths; transfers for multi-destination)\n"
                "rename — rename files or directories in place (batch)\n"
                "diff   — unified diff between two files"
            ),
            "prompt": FILESYSTEM_TOOL_PROMPT,
        }

    async def execute(
        self,
        function: str,
        arguments: Dict[str, Any],
        on_progress: Optional[Callable] = None,
        goal: str = "",
        brain: Any = None,
        **_: Any,
    ) -> Dict[str, Any]:
        fn = str(function or "").strip().lower()
        if fn == "ls":
            return self._ls(arguments)
        if fn == "read":
            return self._read(arguments, brain=brain, goal=goal, on_progress=on_progress)
        if fn == "write":
            return self._write(arguments)
        if fn == "find":
            return self._find(arguments)
        if fn == "manage":
            return self._manage(arguments)
        if fn == "rename":
            return self._rename(arguments)
        if fn == "diff":
            return self._diff(arguments)
        return _err(msg=f"Unknown function: {function!r}. Must be ls, read, write, find, manage, rename, or diff.")

    # ── ls ───────────────────────────────────────────────────────────────────

    def _ls(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err(msg="path is required")
        path = _resolve(raw)
        p = Path(path)
        try:
            if not p.exists():
                return _err(path=path, msg="Path not found")
            if not p.is_dir():
                return _err(path=path, msg="Path is not a directory. Use read for files.")
            depth = min(int(args.get("depth") or 1), _MAX_DEPTH)
            show_hidden = bool(args.get("show_hidden", False))
            if depth > 1:
                return self._tree(p, depth=depth, show_hidden=show_hidden)
            return self._list(p, show_hidden=show_hidden)
        except PermissionError:
            return _err(path=path, msg="Permission denied")
        except Exception as e:
            return _err(path=path, msg=f"{type(e).__name__}: {e}")

    def _list(self, p: Path, show_hidden: bool) -> Dict[str, Any]:
        try:
            children = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return _err(path=str(p), msg="Permission denied")
        entries = [_entry(c) for c in children if show_hidden or not c.name.startswith(".")]
        return _ok(path=str(p), ENTRIES=entries, TOTAL=len(entries))

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
                if count[0] >= _MAX_DIR_ENTRIES:
                    lines.append(f"{prefix}└── ... ({len(visible) - i} more)")
                    return
                is_last = i == len(visible) - 1
                lines.append(f"{prefix}{'└── ' if is_last else '├── '}{_tree_label(child)}")
                count[0] += 1
                if child.is_dir():
                    _walk(child, prefix + ("    " if is_last else "│   "), cur + 1)

        _walk(p, "", 1)
        return _ok(path=str(p), TREE_TEXT="\n".join(lines), TOTAL=count[0])

    # ── read ────────────────────────────────────────────────────────────────

    def _read(
        self,
        args: Dict[str, Any],
        brain: Any = None,
        goal: str = "",
        on_progress: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err(msg="path is required")
        path = _resolve(raw)
        p = Path(path)
        try:
            if args.get("info"):
                return self._info(p)
            if not p.exists():
                return _err(path=path, msg="Path not found")
            if p.is_dir():
                return _err(path=path, msg="Path is a directory. Use ls to list directories.")
            if args.get("start_line") or args.get("end_line"):
                return self._read_lines(p, args)
            return self._read_content(p, args, brain=brain, goal=goal, on_progress=on_progress)
        except PermissionError:
            return _err(path=path, msg="Permission denied")
        except Exception as e:
            return _err(path=path, msg=f"{type(e).__name__}: {e}")

    def _info(self, p: Path) -> Dict[str, Any]:
        if not p.exists():
            return _ok(path=str(p), EXISTS=False)
        try:
            s = p.stat()
            r = _ok(path=str(p), EXISTS=True, IS_FILE=p.is_file(), IS_DIR=p.is_dir(),
                    MODIFIED=_iso(s.st_mtime), CREATED=_iso(s.st_ctime))
            if p.is_file():
                r["SIZE_BYTES"] = s.st_size
            return r
        except OSError as e:
            return _err(path=str(p), msg=str(e))

    def _read_lines(self, p: Path, args: Dict[str, Any]) -> Dict[str, Any]:
        if _is_binary(p):
            return _err(path=str(p), msg=f"Binary file ({p.suffix}) cannot be read as text.")
        encoding = str(args.get("encoding") or "utf-8")
        max_chars = min(int(args.get("max_chars") or _MAX_CHARS), _MAX_CHARS_HARD)
        try:
            lines = p.read_text(encoding=encoding, errors="replace").splitlines(keepends=True)
        except OSError as e:
            return _err(path=str(p), msg=str(e))

        total = len(lines)
        start = max(1, int(args.get("start_line") or 1))
        end = min(total, int(args.get("end_line") or total))

        if start > total:
            return _err(path=str(p), msg=f"start_line={start} exceeds file length ({total} lines)")

        content = "".join(lines[start - 1:end])
        content, truncated = _truncate(content, max_chars)
        r = _ok(path=str(p), FORMAT="text", CONTENT=content,
                LINE_COUNT=total, START_LINE=start, END_LINE=min(end, total))
        if truncated:
            r["TRUNCATED"] = True
            r["NOTE"] = "Content truncated. Use a smaller line range."
        return r

    def _read_content(
        self, p: Path, args: Dict[str, Any],
        brain: Any = None, goal: str = "", on_progress: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        ext = p.suffix.lower()
        max_chars = min(int(args.get("max_chars") or _MAX_CHARS), _MAX_CHARS_HARD)

        if ext in BINARY_EXTENSIONS:
            return self._binary_gate(p, args)
        if ext in TABULAR_EXTENSIONS:
            result = self._read_tabular(p, args, max_chars)
            if result is not None:
                return result
        if ext in PDF_EXTENSIONS:
            result = self._read_pdf(p, args, max_chars, brain=brain, goal=goal, on_progress=on_progress)
            if result is not None:
                return result
        if ext in DOCX_EXTENSIONS:
            result = self._read_docx(p, args, max_chars, brain=brain, goal=goal, on_progress=on_progress)
            if result is not None:
                return result
        return self._read_text(p, args, max_chars, brain=brain, goal=goal, on_progress=on_progress)

    def _binary_gate(self, p: Path, args: Dict[str, Any]) -> Dict[str, Any]:
        if not args.get("confirmed"):
            return _needs_confirm(
                str(p),
                f"Cannot read {p.name} as text — binary format ({p.suffix or 'no extension'})."
                " Call again with confirmed=true to open with system app.",
            )
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            elif sys.platform == "win32":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(p)])
            return _ok(path=str(p), FORMAT="binary", OPENED=True)
        except Exception as e:
            return _err(path=str(p), msg=f"Failed to open: {e}")

    def _read_text(
        self, p: Path, args: Dict[str, Any], max_chars: int,
        brain: Any = None, goal: str = "", on_progress: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        raw: Optional[str] = None
        encoding = str(args.get("encoding") or "utf-8")
        for enc in [encoding, "latin-1", "cp1252"]:
            try:
                raw = p.read_text(encoding=enc, errors="strict")
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if raw is None:
            return self._binary_gate(p, args)

        try:
            s = p.stat()
            size = s.st_size
            modified = _iso(s.st_mtime)
        except OSError:
            size, modified = None, None

        search = args.get("search_pattern")
        if search:
            return self._apply_search(str(p), raw, search, "text", max_chars, size, modified)

        # large file — use TextReader if brain available
        try:
            from buddy.brain.text_reader import CHAR_THRESHOLD, maybe_read
            if brain and goal and len(raw) > CHAR_THRESHOLD:
                content = maybe_read(raw, goal, brain, on_progress)
                r = _ok(path=str(p), FORMAT="text", CONTENT=content[:max_chars])
                if size is not None:
                    r["SIZE_BYTES"] = size
                return r
        except ImportError:
            pass

        content, truncated = _truncate(raw, max_chars)
        r = _ok(path=str(p), FORMAT="text", CONTENT=content)
        if size is not None:
            r["SIZE_BYTES"] = size
        if modified:
            r["MODIFIED"] = modified
        if truncated:
            r["TRUNCATED"] = True
            r["NOTE"] = f"Showing first {max_chars} chars of {_human(size)}. Use start_line/end_line or search_pattern."
        return r

    def _read_tabular(self, p: Path, args: Dict[str, Any], max_chars: int) -> Optional[Dict[str, Any]]:
        try:
            import pandas as pd
        except ImportError:
            return None

        ext = p.suffix.lower()
        sheet_used: Optional[str] = None
        try:
            if ext in {".csv", ".tsv"}:
                df = pd.read_csv(str(p), sep="\t" if ext == ".tsv" else ",")
            elif ext in {".xlsx", ".xls"}:
                xl = pd.ExcelFile(str(p))
                sheet_used = (
                    args.get("sheet_name") if args.get("sheet_name") in xl.sheet_names
                    else str(xl.sheet_names[0])
                )
                df = pd.read_excel(str(p), sheet_name=sheet_used)
            elif ext == ".parquet":
                df = pd.read_parquet(str(p))
            elif ext == ".feather":
                df = pd.read_feather(str(p))
            elif ext == ".orc":
                df = pd.read_orc(str(p))
            else:
                df = pd.read_csv(str(p))
        except Exception:
            return None

        rows_total = len(df)
        all_cols = list(df.columns)

        cols = args.get("columns")
        if cols:
            valid = [c for c in cols if c in df.columns]
            if not valid:
                return _err(path=str(p), msg=f"None of {cols} found. Available: {all_cols}")
            df = df[valid]

        pq = args.get("pandas_query")
        if pq:
            try:
                df = df.query(pq)
            except Exception as e:
                return _err(path=str(p), msg=f"pandas_query error: {e}. Columns: {all_cols}")

        sp = args.get("search_pattern")
        if sp:
            try:
                mask = df.apply(
                    lambda row: row.astype(str).str.contains(sp, flags=re.IGNORECASE, regex=False, na=False).any(),
                    axis=1,
                )
                df = df[mask]
            except Exception as e:
                return _err(path=str(p), msg=f"search_pattern error: {e}")

        rendered = self._render_df(df)
        rows_after = len(df)

        if len(rendered) > max_chars:
            preview = self._render_df(df.head(2))
            r: Dict[str, Any] = {
                "STATUS": "failed", "TOOL": _TOOL, "PATH": str(p),
                "NEEDS_CONFIRMATION": True,
                "FORMAT": "table",
                "ROWS_TOTAL": rows_total,
                "ROWS_AFTER_FILTER": rows_after,
                "COLUMNS": all_cols,
                "PREVIEW": (
                    f"{rows_after} rows × {len(df.columns)} cols. Output ({len(rendered):,} chars) "
                    f"exceeds max_chars ({max_chars:,}). First 2 rows:\n{preview}\n"
                    "Add pandas_query or columns to reduce output."
                ),
                "NOTE": "Call again with a filter to get results.",
            }
            if sheet_used:
                r["SHEET"] = sheet_used
            return r

        r2 = _ok(path=str(p), FORMAT="table", CONTENT=rendered,
                 ROWS_TOTAL=rows_total, COLUMNS=all_cols)
        if rows_after < rows_total:
            r2["ROWS_AFTER_FILTER"] = rows_after
        if sheet_used:
            r2["SHEET"] = sheet_used
        return r2

    def _read_pdf(
        self, p: Path, args: Dict[str, Any], max_chars: int,
        brain: Any = None, goal: str = "", on_progress: Optional[Callable] = None,
    ) -> Optional[Dict[str, Any]]:
        text: Optional[str] = None
        for extractor in [self._pdf_pdfplumber, self._pdf_pypdf2]:
            text = extractor(p)
            if text is not None:
                break
        if text is None:
            return None
        if not text:
            r = _ok(path=str(p), FORMAT="pdf", CONTENT="")
            r["NOTE"] = "PDF has no extractable text — likely a scanned image. Use confirmed=true to open."
            return r
        return self._finalize_doc(str(p), text, "pdf", args, max_chars, brain=brain, goal=goal, on_progress=on_progress)

    def _pdf_pdfplumber(self, p: Path) -> Optional[str]:
        try:
            import pdfplumber
            with pdfplumber.open(str(p)) as pdf:
                return "\n\n".join(pg.extract_text() or "" for pg in pdf.pages).strip()
        except Exception:
            return None

    def _pdf_pypdf2(self, p: Path) -> Optional[str]:
        try:
            import PyPDF2
            with open(p, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                return "\n\n".join(reader.pages[i].extract_text() or "" for i in range(len(reader.pages))).strip()
        except Exception:
            return None

    def _read_docx(
        self, p: Path, args: Dict[str, Any], max_chars: int,
        brain: Any = None, goal: str = "", on_progress: Optional[Callable] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            from docx import Document
            doc = Document(str(p))
            text = "\n".join(para.text for para in doc.paragraphs if para.text.strip())
        except Exception:
            return None
        if not text:
            r = _ok(path=str(p), FORMAT="docx", CONTENT="")
            r["NOTE"] = "DOCX extracted but no text found."
            return r
        return self._finalize_doc(str(p), text, "docx", args, max_chars, brain=brain, goal=goal, on_progress=on_progress)

    def _finalize_doc(
        self, path: str, text: str, fmt: str, args: Dict[str, Any], max_chars: int,
        brain: Any = None, goal: str = "", on_progress: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        sp = args.get("search_pattern")
        if sp:
            return self._apply_search(path, text, sp, fmt, max_chars)

        try:
            from buddy.brain.text_reader import CHAR_THRESHOLD, maybe_read
            if brain and goal and len(text) > CHAR_THRESHOLD:
                content = maybe_read(text, goal, brain, on_progress)
                return _ok(path=path, FORMAT=fmt, CONTENT=content[:max_chars])
        except ImportError:
            pass

        content, truncated = _truncate(text, max_chars)
        r = _ok(path=path, FORMAT=fmt, CONTENT=content)
        if truncated:
            r["TRUNCATED"] = True
            r["NOTE"] = f"{fmt.upper()} truncated. Use search_pattern to find specific content."
        return r

    def _apply_search(
        self, path: str, text: str, pattern: str, fmt: str,
        max_chars: int, size: Optional[int] = None, modified: Optional[str] = None,
    ) -> Dict[str, Any]:
        matcher = _compile(pattern, case_sensitive=False, use_regex=False)
        lines = text.splitlines()
        seen: set = set()
        matched: List[str] = []
        for idx, line in enumerate(lines):
            if not matcher(line):
                continue
            for i in range(max(0, idx - 2), min(len(lines), idx + 3)):
                if i not in seen:
                    seen.add(i)
                    matched.append(lines[i])

        content = "\n".join(matched)
        content, truncated = _truncate(content, max_chars)
        r = _ok(path=path, FORMAT=fmt, CONTENT=content, TOTAL_FOUND=len(matched))
        if size is not None:
            r["SIZE_BYTES"] = size
        if modified:
            r["MODIFIED"] = modified
        if not matched:
            r["NOTE"] = "No lines matched search_pattern."
        elif truncated:
            r["TRUNCATED"] = True
        return r

    def _render_df(self, df: Any) -> str:
        try:
            cols = list(df.columns)
            col_w = {c: max(len(str(c)), int(df[c].astype(str).str.len().max() or 0)) for c in cols}
            header = " | ".join(str(c).ljust(col_w[c]) for c in cols)
            sep = "-+-".join("-" * col_w[c] for c in cols)
            rows = [" | ".join(str(v).ljust(col_w[c]) for c, v in zip(cols, row))
                    for row in df.itertuples(index=False, name=None)]
            return "\n".join([header, sep] + rows)
        except Exception:
            return str(df)

    # ── write ────────────────────────────────────────────────────────────────

    def _write(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        action = str(args.get("action") or "").strip().lower()
        if not raw:
            return _err(msg="path is required")
        if action not in ("create", "append", "patch"):
            return _err(action=action, msg=f"Invalid action {action!r}. Must be: create, append, patch")
        path = _resolve(raw)
        p = Path(path)
        try:
            if action == "create":
                content = str(args.get("content") or "")
                if p.exists() and not args.get("confirmed"):
                    return _err(path=path, action=action,
                                msg="File already exists. Set confirmed=true after user confirms overwrite.")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")

            elif action == "append":
                content = str(args.get("content") or "")
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(content)

            elif action == "patch":
                old_str = args.get("old_str")
                new_str = args.get("new_str")
                if old_str is None or new_str is None:
                    return _err(path=path, action=action, msg="patch requires old_str and new_str")
                if not p.exists():
                    return _err(path=path, action=action, msg="File not found")
                original = p.read_text(encoding="utf-8")
                count = original.count(old_str)
                if count == 0:
                    return _err(path=path, action=action,
                                msg="old_str not found. Read the file first to get the exact current content.")
                if count > 1:
                    return _err(path=path, action=action,
                                msg=f"old_str matched {count} times. Add surrounding lines to make it unique.")
                p.write_text(original.replace(old_str, new_str, 1), encoding="utf-8")

            return _ok(path=path, ACTION=action, SIZE_BYTES=p.stat().st_size)

        except PermissionError:
            return _err(path=path, action=action, msg="Permission denied")
        except Exception as e:
            return _err(path=path, action=action, msg=str(e))

    # ── find ─────────────────────────────────────────────────────────────────

    def _find(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        pattern = str(args.get("pattern") or "").strip()
        find_type = str(args.get("type") or "name").strip().lower()
        recursive = bool(args.get("recursive", True))
        max_results = int(args.get("max_results") or 50)
        context_lines = int(args.get("context_lines") or 0)
        file_types: Optional[List[str]] = args.get("file_types")

        if not raw:
            return _err(msg="path is required")
        if not pattern:
            return _err(msg="pattern is required")
        if find_type not in ("name", "content"):
            return _err(msg=f"Invalid type {find_type!r}. Must be: name, content")

        path = _resolve(raw)
        if not Path(path).exists():
            return _err(path=path, msg="Path not found")

        try:
            if find_type == "name":
                return self._find_name(path, pattern, recursive, max_results)
            return self._find_content(path, pattern, recursive, max_results, context_lines, file_types)
        except Exception as e:
            return _err(path=path, msg=str(e))

    def _find_name(self, path: str, pattern: str, recursive: bool, max_results: int) -> Dict[str, Any]:
        root = Path(path)
        results: List[Dict[str, Any]] = []
        iterator = root.rglob(pattern) if recursive else root.glob(pattern)
        for match in iterator:
            e: Dict[str, Any] = {"path": str(match), "type": "dir" if match.is_dir() else "file"}
            if match.is_file():
                try:
                    e["size_bytes"] = match.stat().st_size
                except Exception:
                    pass
            results.append(e)
            if len(results) >= max_results:
                break
        return _ok(path=path, TYPE="name", PATTERN=pattern, RESULTS=results, TOTAL_FOUND=len(results))

    def _find_content(
        self, path: str, pattern: str, recursive: bool,
        max_results: int, context_lines: int, file_types: Optional[List[str]],
    ) -> Dict[str, Any]:
        root = Path(path)
        matcher = _compile(pattern, case_sensitive=False, use_regex=True)
        results: List[Dict[str, Any]] = []
        walker = root.rglob("*") if recursive else root.glob("*")

        for fp in walker:
            if len(results) >= max_results:
                break
            if not fp.is_file():
                continue
            if file_types and fp.suffix.lstrip(".").lower() not in file_types:
                continue
            if _is_binary(fp):
                continue
            try:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                for i, line in enumerate(lines):
                    if not matcher(line):
                        continue
                    entry: Dict[str, Any] = {"file": str(fp), "line": i + 1, "text": line.rstrip()}
                    if context_lines > 0:
                        before = [l.rstrip() for l in lines[max(0, i - context_lines):i]]
                        after = [l.rstrip() for l in lines[i + 1:min(len(lines), i + 1 + context_lines)]]
                        if before:
                            entry["before"] = before
                        if after:
                            entry["after"] = after
                    results.append(entry)
                    if len(results) >= max_results:
                        break
            except Exception:
                continue

        return _ok(path=path, TYPE="content", PATTERN=pattern, RESULTS=results, TOTAL_FOUND=len(results))

    # ── diff ─────────────────────────────────────────────────────────────────

    def _diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw_a = str(args.get("path_a") or "").strip()
        raw_b = str(args.get("path_b") or "").strip()
        if not raw_a:
            return _err(msg="path_a is required")
        if not raw_b:
            return _err(msg="path_b is required")
        path_a = _resolve(raw_a)
        path_b = _resolve(raw_b)
        pa, pb = Path(path_a), Path(path_b)
        if not pa.is_file():
            return _err(path=path_a, msg="path_a must be an existing file")
        if not pb.is_file():
            return _err(path=path_b, msg="path_b must be an existing file")
        import difflib
        a = pa.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        b = pb.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(a, b, fromfile=path_a, tofile=path_b))
        diff, truncated = _truncate(diff or "(files are identical)", _MAX_CHARS)
        r = _ok(PATH_A=path_a, PATH_B=path_b, DIFF=diff)
        if truncated:
            r["TRUNCATED"] = True
        return r

    # ── manage ───────────────────────────────────────────────────────────────

    def _expand_globs(self, path_list: List[str], warnings: List[str]) -> List[str]:
        if not any("*" in p or "?" in p or "[" in p for p in path_list):
            return path_list
        expanded: List[str] = []
        for p_str in path_list:
            if "*" in p_str or "?" in p_str or "[" in p_str:
                matches = sorted(str(m) for m in Path(p_str).parent.glob(Path(p_str).name))
                if not matches:
                    warnings.append(f"Pattern '{p_str}' matched no files")
                else:
                    expanded.extend(matches)
            else:
                expanded.append(p_str)
        return expanded

    def _manage(self, args: Dict[str, Any]) -> Dict[str, Any]:
        action = str(args.get("action") or "").strip().lower()
        valid = ("copy", "move", "delete", "mkdir")
        if action not in valid:
            return _err(action=action, msg=f"Invalid action {action!r}. Must be: {', '.join(valid)}")

        confirmed = bool(args.get("confirmed", False))
        permanent = bool(args.get("permanent", False))

        # ── batch transfers (copy/move only) ──────────────────────────────────
        transfers_raw = args.get("transfers")
        if transfers_raw and action in ("copy", "move"):
            if not isinstance(transfers_raw, list):
                return _err(action=action, msg="transfers must be a list of {paths, destination_dir} objects")

            # Resolve and expand all transfers first
            transfers: List[Dict[str, Any]] = []
            for t in transfers_raw:
                paths_raw = t.get("paths") or []
                dest_raw = str(t.get("destination_dir") or "").strip()
                if not paths_raw or not dest_raw:
                    return _err(action=action, msg="Each transfer must have paths and destination_dir")
                path_list = [_resolve(str(r).strip()) for r in paths_raw if str(r).strip()]
                glob_warnings: List[str] = []
                path_list = self._expand_globs(path_list, glob_warnings)
                transfers.append({
                    "paths": path_list,
                    "destination_dir": _resolve(dest_raw),
                    "glob_warnings": glob_warnings,
                })

            # Upfront destructive gate across all transfers
            if not confirmed:
                all_conflicts: List[str] = []
                for t in transfers:
                    dest_p = Path(t["destination_dir"])
                    if dest_p.is_dir():
                        all_conflicts.extend(
                            str(dest_p / Path(p).name)
                            for p in t["paths"]
                            if (dest_p / Path(p).name).exists()
                        )
                if all_conflicts:
                    preview = (
                        f"Will {action} files to multiple destinations.\n"
                        f"{len(all_conflicts)} destination(s) already exist and will be overwritten:\n"
                        + "\n".join(f"  {c}" for c in all_conflicts)
                    )
                    return _needs_confirm("(batch transfer)", preview)

            results: List[Dict[str, Any]] = []
            all_warnings: List[str] = []
            for t in transfers:
                all_warnings.extend(t.get("glob_warnings", []))
                for p_str in t["paths"]:
                    results.append(self._manage_one(p_str, action, destination_dir=t["destination_dir"], permanent=permanent))

            succeeded = sum(1 for r in results if r.get("STATUS") == "success")
            out: Dict[str, Any] = {
                "STATUS": "success" if succeeded == len(results) else "failed",
                "TOOL": _TOOL,
                "ACTION": action,
                "TOTAL": len(results),
                "SUCCEEDED": succeeded,
                "FAILED": len(results) - succeeded,
                "RESULTS": results,
            }
            if all_warnings:
                out["GLOB_WARNINGS"] = all_warnings
            return out

        # ── single destination (original behaviour) ───────────────────────────
        paths_raw: Optional[List[Any]] = args.get("paths")
        if not paths_raw or not isinstance(paths_raw, list):
            return _err(msg="paths is required and must be a non-empty list of strings")

        path_list = [_resolve(str(r).strip()) for r in paths_raw if str(r).strip()]
        if not path_list:
            return _err(msg="paths resolved to an empty list")

        glob_warnings = []
        if action != "mkdir":
            path_list = self._expand_globs(path_list, glob_warnings)
            if not path_list:
                msg = "No files matched the given pattern(s)"
                if glob_warnings:
                    msg += ": " + "; ".join(glob_warnings)
                return _err(msg=msg)

        dest_raw = str(args.get("destination_dir") or "").strip()
        destination_dir = _resolve(dest_raw) if dest_raw else None

        if action in ("copy", "move") and not destination_dir:
            return _err(action=action, msg="destination_dir is required for copy/move")

        if not confirmed and action != "mkdir":
            if action == "delete":
                targets = [p for p in path_list if Path(p).exists()]
                if targets:
                    dest = "permanently delete" if permanent else "move to trash"
                    preview = (
                        f"Will {dest} {len(targets)} item(s):\n"
                        + "\n".join(f"  {p}" for p in targets)
                    )
                    return _needs_confirm("(multiple paths)", preview)
            elif action in ("copy", "move") and destination_dir:
                dest_p = Path(destination_dir)
                if dest_p.is_dir():
                    conflicts = [
                        str(dest_p / Path(p).name)
                        for p in path_list
                        if (dest_p / Path(p).name).exists()
                    ]
                else:
                    conflicts = [destination_dir] if dest_p.exists() else []
                if conflicts:
                    preview = (
                        f"Will {action} {len(path_list)} item(s) → {destination_dir}\n"
                        f"{len(conflicts)} destination(s) already exist and will be overwritten:\n"
                        + "\n".join(f"  {c}" for c in conflicts)
                    )
                    return _needs_confirm("(multiple paths)", preview)

        results = []
        for p_str in path_list:
            results.append(self._manage_one(p_str, action, destination_dir=destination_dir, permanent=permanent))

        succeeded = sum(1 for r in results if r.get("STATUS") == "success")
        out = {
            "STATUS": "success" if succeeded == len(results) else "failed",
            "TOOL": _TOOL,
            "ACTION": action,
            "TOTAL": len(results),
            "SUCCEEDED": succeeded,
            "FAILED": len(results) - succeeded,
            "RESULTS": results,
        }
        if glob_warnings:
            out["GLOB_WARNINGS"] = glob_warnings
        return out

    def _manage_one(self, path: str, action: str, destination_dir: Optional[str], permanent: bool = False) -> Dict[str, Any]:
        p = Path(path)
        try:
            if action == "mkdir":
                p.mkdir(parents=True, exist_ok=True)
                return _ok(path=path, ACTION=action)

            if action == "delete":
                if not p.exists():
                    return _ok(path=path, ACTION=action, NOTE="Already absent — nothing to delete.")
                if permanent:
                    shutil.rmtree(path) if p.is_dir() else p.unlink()
                else:
                    try:
                        import send2trash
                        send2trash.send2trash(path)
                    except ImportError:
                        shutil.rmtree(path) if p.is_dir() else p.unlink()
                return _ok(path=path, ACTION=action, PERMANENT=permanent)

            if action in ("copy", "move"):
                if not destination_dir:
                    return _err(path=path, action=action, msg="destination_dir is required")
                dest = Path(destination_dir)
                actual_dest = dest / p.name if dest.is_dir() else dest
                actual_dest.parent.mkdir(parents=True, exist_ok=True)
                if action == "copy":
                    shutil.copytree(path, str(actual_dest), dirs_exist_ok=True) if p.is_dir() else shutil.copy2(path, str(actual_dest))
                else:
                    shutil.move(path, str(actual_dest))
                return _ok(path=path, ACTION=action, DESTINATION=str(actual_dest))

        except PermissionError:
            return _err(path=path, action=action, msg="Permission denied")
        except Exception as e:
            return _err(path=path, action=action, msg=str(e))

        return _err(path=path, action=action, msg="Unreachable")

    # ── rename ────────────────────────────────────────────────────────────────

    def _rename(self, args: Dict[str, Any]) -> Dict[str, Any]:
        renames_raw = args.get("renames")
        if not renames_raw or not isinstance(renames_raw, list):
            return _err(action="rename", msg="renames is required and must be a non-empty list")

        confirmed = bool(args.get("confirmed", False))

        items: List[Dict[str, Any]] = []
        for r in renames_raw:
            path_raw = str(r.get("path") or "").strip()
            new_name = str(r.get("new_name") or "").strip()
            if not path_raw or not new_name:
                return _err(action="rename", msg="Each rename entry must have path and new_name")
            if "/" in new_name or "\\" in new_name:
                return _err(action="rename", msg=f"new_name must be a filename only, not a path: {new_name!r}")
            items.append({"path": _resolve(path_raw), "new_name": new_name})

        # Upfront destructive gate
        if not confirmed:
            conflicts = [
                str(Path(item["path"]).parent / item["new_name"])
                for item in items
                if (Path(item["path"]).parent / item["new_name"]).exists()
            ]
            if conflicts:
                preview = (
                    f"Will rename {len(items)} item(s). "
                    f"{len(conflicts)} target(s) already exist and will be overwritten:\n"
                    + "\n".join(f"  {c}" for c in conflicts)
                )
                return _needs_confirm("(multiple renames)", preview)

        results: List[Dict[str, Any]] = []
        for item in items:
            p = Path(item["path"])
            target = p.parent / item["new_name"]
            try:
                if not p.exists():
                    results.append(_err(path=str(p), action="rename", msg="Path not found"))
                    continue
                p.rename(target)
                results.append(_ok(path=str(p), ACTION="rename", DESTINATION=str(target)))
            except PermissionError:
                results.append(_err(path=str(p), action="rename", msg="Permission denied"))
            except Exception as e:
                results.append(_err(path=str(p), action="rename", msg=str(e)))

        succeeded = sum(1 for r in results if r.get("STATUS") == "success")
        return {
            "STATUS": "success" if succeeded == len(results) else "failed",
            "TOOL": _TOOL,
            "ACTION": "rename",
            "TOTAL": len(results),
            "SUCCEEDED": succeeded,
            "FAILED": len(results) - succeeded,
            "RESULTS": results,
        }


# ── registry ─────────────────────────────────────────────────────────────────

TOOL_NAME = "filesystem"
TOOL_CLASS = Filesystem


def get_tool() -> Filesystem:
    return Filesystem()
