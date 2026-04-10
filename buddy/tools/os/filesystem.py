from __future__ import annotations

# ==========================================================
# filesystem.py  —  v2.1.0
#
# Actions: search, read, read_lines, list, tree, open, info,
#          write, append, delete, copy, move, mkdir, grep, diff
#
# Design rules:
#   - Paths are auto-resolved: ~, $ENV_VAR, relative → absolute.
#   - Action names are case-insensitive.
#   - Read ops are always safe. No confirmation needed.
#   - write (overwrite), delete, move require confirmed=true to execute.
#     First call (confirmed=false) returns NEEDS_CONFIRMATION + PREVIEW.
#   - Results are per-action slim dicts — no null-dumping unused fields.
#   - grep: content_query = what to find. pattern = which files (glob).
#     If pattern has no glob chars and content_query is missing, pattern
#     is treated as the search query.
# ==========================================================

import difflib
import os
import platform
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from buddy.prompts.filesystem_prompts import (
    FILESYSTEM_ERROR_RECOVERY_PROMPT,
    FILESYSTEM_TOOL_PROMPT,
    tool_call_format,
)

# Per-action call examples — shown to the executor when the planner's intended
# action is known, so the LLM only sees the one relevant field set.
_ACTION_EXAMPLES: Dict[str, str] = {
    # Patch / modify
    "patch":      '{"action": "patch", "path": "/file.py", "old_str": "exact text to find", "new_str": "replacement text", "replace_all": false, "confirmed": false}',
    # Discovery
    "info":       '{"action": "info", "path": "~"}',
    "list":       '{"action": "list", "path": "~/Documents"}',
    "tree":       '{"action": "tree", "path": "~/project", "depth": 3}',
    "search":     '{"action": "search", "path": "~", "pattern": "*.py", "recursive": true, "max_results": 20}',
    "grep":       '{"action": "grep", "path": "/project", "content_query": "def login", "pattern": "*.py", "context_lines": 2}',
    # Read — format-specific examples (all use action="read", keys are example selectors only)
    "read":       '{"action": "read", "path": "/file.txt", "max_chars": 8000}',
    "read_table": '{"action": "read", "path": "/data.csv", "max_chars": 8000, "pandas_query": "col > value", "columns": ["col1", "col2"]}',
    "read_pdf":   '{"action": "read", "path": "/doc.pdf", "max_chars": 8000, "search_pattern": "keyword"}',
    "read_docx":  '{"action": "read", "path": "/doc.docx", "max_chars": 8000, "search_pattern": "keyword"}',
    "read_lines": '{"action": "read_lines", "path": "/file.txt", "start_line": 10, "end_line": 50}',
    # Write / modify
    "write":      '{"action": "write", "path": "/file.txt", "content": "full file text here", "confirmed": false}',
    "append":     '{"action": "append", "path": "/file.txt", "content": "text to add at end"}',
    # Manage
    "mkdir":      '{"action": "mkdir", "path": "~/new/folder"}',
    "delete":     '{"action": "delete", "path": "/file.txt", "confirmed": false}',
    "copy":       '{"action": "copy", "path": "/src.txt", "destination": "/dst/"}',
    "move":       '{"action": "move", "path": "/src.txt", "destination": "/dst/newname.txt", "confirmed": false}',
    "open":       '{"action": "open", "path": "/file.txt"}',
    "diff":       '{"action": "diff", "path": "/file1.txt", "destination": "/file2.txt"}',
}

# Action trigger regex patterns — pre-compiled at module load.
# Ordered by specificity: most specific first so "read_lines" wins over "read",
# "read_table" wins over "read", etc.
# All patterns matched against lowercased hint text.
# Keys read_table / read_pdf / read_docx are example-selector keys only —
# they map to action="read" in _ACTION_EXAMPLES but show format-specific fields.
_ACTION_TRIGGERS: tuple = (
    ("read_lines", (
        r"\bread[_\s]lines?\b",         # read_lines, read line, read lines
        r"\bline[_\s]range\b",          # line range
        r"\bstart[_\s]line\b",          # start_line
        r"\bend[_\s]line\b",            # end_line
        r"\bspecific\s+lines?\b",       # specific lines
        r"\blines?\s+\d+",              # lines 10-50
    )),
    ("read_table", (
        r"\.(csv|tsv|xlsx?|parquet|feather|orc)\b",   # any tabular extension in path
        r"\bread\s+(csv|tsv|excel|spreadsheet|table|tabular|parquet|feather)\b",
        r"\bspreadsheet\b",
        r"\bpandas[_\s]query\b",
        r"\bdata\s*frame\b",
        r"\btabular\b",
    )),
    ("read_pdf", (
        r"\.pdf\b",                     # .pdf anywhere in hint
        r"\bread\s+pdf\b",
        r"\bpdf\s+(file|document|report)\b",
        r"\bopen\s+pdf\b",
    )),
    ("read_docx", (
        r"\.docx?\b",                   # .doc or .docx anywhere in hint
        r"\bread\s+(word|docx?)\b",
        r"\bword\s+(file|document|doc)\b",
        r"\bdocx?\s+file\b",
    )),
    ("search", (
        r"\bsearch\b",
        r"\bfind\s+(a\s+)?file\b",      # find file, find a file
        r"\blocate\s+(a\s+)?file\b",
        r"\blook\s+for\s+(a\s+)?file\b",
        r"\bfind\s+by\s+name\b",
        r"\bfile\s+named\b",
    )),
    ("grep", (
        r"\bgrep\b",
        r"\bfind\s+(lines?|text)\b",
        r"\bsearch\s+(lines?|text|inside|in\s+file)\b",
        r"\blines?\s+matching\b",
        r"\btext\s+in\s+(file|files)\b",
        r"\bcontent[_\s]search\b",
        r"\bsearch\s+inside\b",
        r"\bfind\s+in\s+file\b",
    )),
    ("read", (
        r"\bread\b",                    # generic text read — fallback after format-specific
        r"\bview\s+file\b",
        r"\bshow\s+file\b",
        r"\bdisplay\s+file\b",
        r"\bfile\s+content\b",
        r"\bcat\b",
    )),
    ("patch", (
        r"\bpatch\b",
        r"\bedit\s+(a\s+|the\s+)?file\b",
        r"\bmodify\s+(a\s+|the\s+)?file\b",
        r"\breplace\s+(text|string|line|word)\b",
        r"\bfind\s+and\s+replace\b",
        r"\bsearch\s+and\s+replace\b",
        r"\bchange\s+(a\s+|the\s+)?(line|text|string|word)\b",
        r"\bold[_\s]str\b",
        r"\bnew[_\s]str\b",
        r"\binline\s+edit\b",
    )),
    ("write", (
        r"\bwrite\b",
        r"\bcreate\s+(a\s+)?file\b",
        r"\boverwrite\b",
        r"\bsave\s+(to\s+)?(a\s+)?file\b",
        r"\bnew\s+file\b",
        r"\bwrite\s+(to|into|content)\b",
    )),
    ("append", (
        r"\bappend\b",
        r"\badd\s+to\s+(a\s+)?file\b",
        r"\badd\s+text\b",
        r"\binsert\s+at\s+end\b",
        r"\bwrite\s+at\s+end\b",
        r"\badd\s+line\b",
    )),
    ("delete", (
        r"\bdelete\b",
        r"\bremove\s+(a\s+)?(file|dir|directory|folder)\b",
        r"\berase\s+(a\s+)?file\b",
        r"\brm\b",
        r"\bunlink\b",
    )),
    ("copy", (
        r"\bcopy\b",
        r"\bcp\b",
        r"\bduplicate\s+(a\s+)?(file|dir|folder)\b",
    )),
    ("move", (
        r"\bmove\b",
        r"\brename\b",
        r"\bmv\b",
        r"\brelocate\b",
    )),
    ("mkdir", (
        r"\bmkdir\b",
        r"\b(make|create)\s+(a\s+)?(dir|directory|folder)\b",
        r"\bnew\s+(dir|directory|folder)\b",
    )),
    ("tree", (
        r"\btree\b",
        r"\b(dir|directory|folder)\s+structure\b",
        r"\bshow\s+(the\s+)?tree\b",
        r"\bdirectory\s+tree\b",
    )),
    ("list", (
        r"\blist\b",
        r"\bls\b",
        r"\bdirectory\s+listing\b",
        r"\blist\s+(files?|dirs?|folder|directory)\b",
        r"\bshow\s+files?\b",
    )),
    ("info", (
        r"\binfo\b",
        r"\bstat\b",
        r"\bfile\s+(info|details|metadata|size|type)\b",
        r"\bfile\s+exists?\b",
        r"\bpath\s+exists?\b",
        r"\bcheck\s+(if\s+)?(file|path)\b",
    )),
    ("open", (
        r"\bopen\s+with\b",
        r"\blaunch\s+(the\s+)?(app|application|file)\b",
        r"\bopen\s+in\b",
        r"\bdefault\s+app\b",
        r"\bopen\s+application\b",
    )),
    ("diff", (
        r"\bdiff\b",
        r"\bcompare\s+(two\s+|the\s+)?files?\b",
        r"\bdifferences?\s+between\b",
        r"\bchanges?\s+between\b",
        r"\bfile\s+comparison\b",
    )),
)

# Pre-compile all trigger patterns at module load — search at runtime is fast.
_ACTION_TRIGGERS_COMPILED: tuple = tuple(
    (action, tuple(re.compile(p) for p in patterns))
    for action, patterns in _ACTION_TRIGGERS
)

# ==========================================================
# Constants
# ==========================================================

_MAX_CHARS_HARD_LIMIT = 50_000
_MAX_RESULTS_HARD_LIMIT = 500
_DEFAULT_MAX_CHARS = 8_000
_DEFAULT_MAX_RESULTS = 20
_DEFAULT_DEPTH = 3
_MAX_DEPTH = 10
_GLOB_CHARS = set("*?[")
# Truly unreadable binary formats — no text extractor available.
# Redirect to open action. pdf/docx/doc/xlsx/xls removed — they have extractors.
_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
    ".mp3", ".mp4", ".wav", ".flac", ".aac", ".ogg",
    ".avi", ".mov", ".mkv", ".wmv",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".pptx",
    ".db", ".sqlite", ".sqlite3",
    ".pyc", ".pyo",
}

# Extensions handled by dedicated extractors in _read strategy chain.
_TABULAR_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".feather", ".orc"}
_PDF_EXTENSIONS     = {".pdf"}
_DOCX_EXTENSIONS    = {".docx", ".doc"}

_DESTRUCTIVE_ACTIONS = {"write", "patch", "delete", "move"}


# ==========================================================
# Path helpers
# ==========================================================

def _resolve_path(raw: str) -> str:
    """Expand ~ and $VAR, resolve to absolute path."""
    p = os.path.expanduser(os.path.expandvars(raw.strip()))
    if not os.path.isabs(p):
        # Try to make it absolute relative to home dir as a best-effort
        p = os.path.join(os.path.expanduser("~"), p)
    return p


def _human_size(n: Optional[int]) -> str:
    if n is None:
        return "unknown size"
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


# ==========================================================
# Input model
# ==========================================================

_ACTION_ALIASES: Dict[str, str] = {
    "readlines": "read_lines",
    "make_dir": "mkdir",
    "makedir": "mkdir",
    "create_dir": "mkdir",
    "remove": "delete",
    "rm": "delete",
    "rename": "move",
    "mv": "move",
    "cp": "copy",
    "ls": "list",
    "cat": "read",
    "find": "search",
    "grep_search": "grep",
    "content_search": "grep",
    "compare": "diff",
    "edit": "patch",
    "replace": "patch",
    "find_replace": "patch",
    "search_replace": "patch",
}


class FilesystemCall(BaseModel):
    action: str   # validated + normalized in pre-validator
    path: str

    # search / grep / list
    pattern: Optional[str] = None
    content_query: Optional[str] = None
    recursive: bool = True
    max_results: int = Field(default=_DEFAULT_MAX_RESULTS, ge=1, le=_MAX_RESULTS_HARD_LIMIT)
    file_types: Optional[List[str]] = None
    case_sensitive: bool = False
    regex: bool = False

    # read
    max_chars: int = Field(default=_DEFAULT_MAX_CHARS, ge=1, le=_MAX_CHARS_HARD_LIMIT)
    encoding: str = "utf-8"

    # read_lines
    start_line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)

    # grep
    context_lines: int = Field(default=2, ge=0, le=10)

    # list / tree / search
    show_hidden: bool = False

    # tree
    depth: int = Field(default=_DEFAULT_DEPTH, ge=1, le=_MAX_DEPTH)

    # write / append
    content: Optional[str] = None
    overwrite: bool = False

    # copy / move / diff
    destination: Optional[str] = None

    # multi-format read fields
    search_pattern: Optional[str] = None   # text/regex: filters lines (text/pdf/docx) or rows (table)
    pandas_query: Optional[str] = None     # pandas query expression, tabular only
    columns: Optional[List[str]] = None    # select columns, tabular only
    sheet_name: Optional[str] = None       # Excel sheet name, default: first sheet

    # patch fields
    old_str: Optional[str] = None          # exact text to find (required for patch)
    new_str: Optional[str] = None          # replacement text (empty string = deletion)
    replace_all: bool = False              # replace all occurrences vs only first unique match

    # confirmation gate for destructive actions
    confirmed: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        # normalize action: lowercase + alias resolution
        raw_action = str(values.get("action", "")).lower().strip()
        values["action"] = _ACTION_ALIASES.get(raw_action, raw_action)

        # resolve paths
        for field in ("path", "destination"):
            v = values.get(field)
            if v:
                values[field] = _resolve_path(str(v))

        # grep field smart-fix: if grep/search has pattern with no glob chars
        # and no content_query, the LLM probably put the search query in pattern
        action = values.get("action", "")
        if action == "grep" and not values.get("content_query") and values.get("pattern"):
            pat = str(values["pattern"])
            if not any(c in pat for c in _GLOB_CHARS):
                values["content_query"] = pat
                values.pop("pattern", None)

        # normalise file_types: strip dots, lowercase
        if values.get("file_types"):
            values["file_types"] = [
                t.lstrip(".").strip().lower()
                for t in values["file_types"] if str(t).strip()
            ]

        return values

    @model_validator(mode="after")
    def _validate_action_and_paths(self) -> "FilesystemCall":
        _valid_actions = {
            "search", "read", "read_lines", "list", "tree", "open", "info",
            "write", "append", "patch", "delete", "copy", "move", "mkdir", "grep", "diff",
        }
        if self.action not in _valid_actions:
            raise ValueError(
                f"Unknown action: {self.action!r}. "
                f"Valid actions: {', '.join(sorted(_valid_actions))}"
            )
        if not self.path:
            raise ValueError("path must not be empty")
        return self


# ==========================================================
# Result helpers
# ==========================================================

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_dict(p: Path) -> Dict[str, Any]:
    try:
        stat = p.stat()
        return {
            "name": p.name,
            "path": str(p),
            "type": "dir" if p.is_dir() else "file",
            "size_bytes": stat.st_size if p.is_file() else None,
            "modified": _iso(stat.st_mtime),
        }
    except OSError:
        return {"name": p.name, "path": str(p), "type": "unknown",
                "size_bytes": None, "modified": None}


def _matches_file_types(p: Path, file_types: Optional[List[str]]) -> bool:
    if not file_types:
        return True
    return p.suffix.lstrip(".").lower() in file_types


def _is_likely_binary(p: Path) -> bool:
    return p.suffix.lower() in _BINARY_EXTENSIONS


def _compile_pattern(pattern: str, *, case_sensitive: bool, use_regex: bool):
    """Return a callable(text) -> bool."""
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex: {exc}") from exc
        return compiled.search
    else:
        if case_sensitive:
            needle = pattern
            return lambda text: needle in text
        else:
            needle = pattern.lower()
            return lambda text: needle in text.lower()


def _ok(action: str, path: str, **extra) -> Dict[str, Any]:
    return {"OK": True, "ACTION": action, "PATH": path, **extra}


def _err(action: str, path: str, error: str) -> Dict[str, Any]:
    return {"OK": False, "ACTION": action, "PATH": path, "ERROR": error}


def _needs_confirmation(action: str, path: str, preview: str) -> Dict[str, Any]:
    return {
        "OK": False,
        "ACTION": action,
        "PATH": path,
        "NEEDS_CONFIRMATION": True,
        "PREVIEW": preview,
        "NOTE": "Call again with confirmed=true after user approves.",
    }


# ==========================================================
# Filesystem tool
# ==========================================================


class Filesystem:
    """
    Filesystem tool v2.1.0.
    Actions: search, read, read_lines, list, tree, open, info,
             write, append, delete, copy, move, mkdir, grep, diff
    """

    tool_name = "filesystem"
    version = "2.1.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "Use for ALL file and directory operations: "
                "find files (search), find text in files (grep), "
                "read ANY file — text, CSV/Excel/Parquet (tabular), PDF, DOCX (read), "
                "read a line range (read_lines), "
                "write or create files (write / append), "
                "browse directories (list / tree), "
                "delete / copy / move / rename files, "
                "check file info (info), open with default app (open), compare files (diff). "
                "For tabular files use pandas_query='col > val' or search_pattern='text' to filter. "
                "ALWAYS name the intended action + file type in hints — "
                "e.g. 'use read action on csv file' or 'use write action'. "
                "Do NOT use terminal for file operations."
            ),
            "prompt": FILESYSTEM_TOOL_PROMPT,
            "error_prompt": FILESYSTEM_ERROR_RECOVERY_PROMPT,
            "tool_call_format": tool_call_format,
        }

    def detect_action(self, hint_text: str) -> Optional[str]:
        """Detect intended filesystem action from planner goal+hints text using compiled regex.
        Returns the example-selector key (may be read_table/read_pdf/read_docx for focused
        examples), or None if no pattern matches."""
        text = hint_text.lower()
        for action, compiled_patterns in _ACTION_TRIGGERS_COMPILED:
            if any(p.search(text) for p in compiled_patterns):
                return action
        return None

    def get_call_example(self, action: str) -> str:
        """Return the focused JSON example for a specific action.
        Falls back to the generic tool_call_format if action is unknown."""
        return _ACTION_EXAMPLES.get(action.lower().strip(), tool_call_format)

    def parse_call(self, payload: Dict[str, Any]) -> FilesystemCall:
        return FilesystemCall.model_validate(payload)

    def execute(
        self,
        call: FilesystemCall,
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> Dict[str, Any]:
        if on_progress:
            on_progress(f"{call.action}: {call.path}", False)

        try:
            dispatch = {
                "search":     self._search,
                "read":       self._read,
                "read_lines": self._read_lines,
                "list":       self._list,
                "tree":       self._tree,
                "open":       self._open,
                "info":       self._info,
                "write":      self._write,
                "append":     self._append,
                "patch":      self._patch,
                "delete":     self._delete,
                "copy":       self._copy,
                "move":       self._move,
                "mkdir":      self._mkdir,
                "grep":       self._grep,
                "diff":       self._diff,
            }
            return dispatch[call.action](call)
        except Exception as exc:
            return _err(call.action, call.path, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------
    # search
    # ----------------------------------------------------------

    def _search(self, call: FilesystemCall) -> Dict[str, Any]:
        root = Path(call.path)
        if not root.exists():
            return _err("search", call.path, f"Path does not exist: {call.path}")
        if not root.is_dir():
            return _err("search", call.path, f"Not a directory: {call.path}")

        pattern = call.pattern or "*"
        try:
            candidates = root.rglob(pattern) if call.recursive else root.glob(pattern)
        except Exception as exc:
            return _err("search", call.path, f"Glob pattern error: {exc}")

        matcher = None
        if call.content_query:
            try:
                matcher = _compile_pattern(
                    call.content_query,
                    case_sensitive=call.case_sensitive,
                    use_regex=call.regex,
                )
            except ValueError as exc:
                return _err("search", call.path, str(exc))

        matches: List[Dict[str, Any]] = []
        total = 0

        for p in candidates:
            if not call.show_hidden and any(
                part.startswith(".") for part in p.parts[len(root.parts):]
            ):
                continue
            if p.is_file() and not _matches_file_types(p, call.file_types):
                continue

            if matcher:
                if not p.is_file() or _is_likely_binary(p):
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
                entry = _entry_dict(p)
                if match_line:
                    entry["match"] = match_line
                total += 1
                if len(matches) < call.max_results:
                    matches.append(entry)
            else:
                total += 1
                if len(matches) < call.max_results:
                    matches.append(_entry_dict(p))

        result = _ok("search", call.path, RESULTS=matches, TOTAL_FOUND=total)
        if total > call.max_results:
            result["NOTE"] = f"Showing {call.max_results} of {total}. Increase max_results or narrow pattern."
        return result

    # ----------------------------------------------------------
    # read  (multi-format strategy chain)
    # ----------------------------------------------------------

    def _read(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return _err("read", call.path, f"File not found: {call.path}")
        if p.is_dir():
            return _err("read", call.path, "Path is a directory — use list or tree instead.")

        ext = p.suffix.lower()

        # Truly unreadable — skip straight to binary gate
        if ext in _BINARY_EXTENSIONS:
            return self._binary_open_gate(call)

        # Build strategy list based on extension and requested fields
        strategies = []

        if ext in _TABULAR_EXTENSIONS:
            strategies.append(self._try_read_table)
        elif ext in _PDF_EXTENSIONS:
            strategies.append(self._try_read_pdf)
        elif ext in _DOCX_EXTENSIONS:
            strategies.append(self._try_read_docx)

        # If user explicitly requests tabular ops on a non-tabular extension, try table first
        if (call.pandas_query or call.columns) and ext not in _TABULAR_EXTENSIONS:
            strategies.insert(0, self._try_read_table)

        # Text is always the last resort before binary gate
        strategies.append(self._try_read_text)

        for strategy in strategies:
            try:
                result = strategy(p, call)
                if result is not None:
                    return result
            except Exception:
                continue

        # Every strategy returned None — treat as binary
        return self._binary_open_gate(call)

    # ----------------------------------------------------------
    # read helpers
    # ----------------------------------------------------------

    def _try_read_text(self, p: Path, call: FilesystemCall) -> Optional[Dict[str, Any]]:
        """Try UTF-8 → latin-1 → cp1252. Returns None if file is not text-decodable."""
        raw = None
        for enc in [call.encoding, "latin-1", "cp1252"]:
            try:
                raw = p.read_text(encoding=enc, errors="strict")
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if raw is None:
            return None  # not a text file

        try:
            stat = p.stat()
        except OSError:
            stat = None

        # Apply search_pattern — filter lines
        if call.search_pattern:
            try:
                matched = self._apply_search_to_text(
                    raw, call.search_pattern,
                    context_lines=call.context_lines,
                    case_sensitive=call.case_sensitive,
                    use_regex=call.regex,
                )
            except ValueError as exc:
                return _err("read", call.path, f"search_pattern error: {exc}")
            content = "\n".join(matched)
            result = _ok("read", call.path,
                FORMAT="text",
                CONTENT=content[: call.max_chars],
                TOTAL_FOUND=len(matched),
            )
            if stat:
                result["SIZE_BYTES"] = stat.st_size
                result["MODIFIED"] = _iso(stat.st_mtime)
            if not matched:
                result["NOTE"] = "No lines matched search_pattern."
            elif len(content) > call.max_chars:
                result["TRUNCATED"] = True
            return result

        # No filter — return full content
        truncated = len(raw) > call.max_chars
        result = _ok("read", call.path,
            FORMAT="text",
            CONTENT=raw[: call.max_chars] if truncated else raw,
        )
        if stat:
            result["SIZE_BYTES"] = stat.st_size
            result["MODIFIED"] = _iso(stat.st_mtime)
        if truncated:
            result["TRUNCATED"] = True
            result["NOTE"] = (
                f"Showing first {call.max_chars} chars of {stat.st_size if stat else '?'} bytes. "
                "Use read_lines with start_line/end_line to read other sections, "
                "or search_pattern to filter content."
            )
        return result

    def _try_read_table(self, p: Path, call: FilesystemCall) -> Optional[Dict[str, Any]]:
        """Read tabular file via pandas. Returns None if pandas not installed or file unreadable."""
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
                sheet_used = call.sheet_name if call.sheet_name in xl.sheet_names else xl.sheet_names[0]
                df = pd.read_excel(str(p), sheet_name=sheet_used)
            elif ext == ".parquet":
                df = pd.read_parquet(str(p))
            elif ext == ".feather":
                df = pd.read_feather(str(p))
            elif ext == ".orc":
                df = pd.read_orc(str(p))
            else:
                # Non-tabular extension with pandas_query requested — try CSV parse
                df = pd.read_csv(str(p))
        except Exception:
            return None

        rows_total = len(df)
        all_columns = list(df.columns)

        # Column selection
        if call.columns:
            valid = [c for c in call.columns if c in df.columns]
            if valid:
                df = df[valid]
            else:
                return _err("read", call.path,
                    f"None of columns {call.columns} found. Available: {all_columns}")

        # pandas_query filter
        if call.pandas_query:
            try:
                df = df.query(call.pandas_query)
            except Exception as exc:
                return _err("read", call.path,
                    f"pandas_query error: {exc}. "
                    f"Available columns: {all_columns}. "
                    "Check column names and query syntax.")

        # search_pattern row filter
        if call.search_pattern:
            try:
                flags = 0 if call.case_sensitive else re.IGNORECASE
                mask = df.apply(
                    lambda row: row.astype(str).str.contains(
                        call.search_pattern, flags=flags,
                        regex=bool(call.regex), na=False,
                    ).any(),
                    axis=1,
                )
                df = df[mask]
            except Exception as exc:
                return _err("read", call.path, f"search_pattern row filter error: {exc}")

        rows_after = len(df)
        rendered = self._render_dataframe(df)

        # Context budget check — too large → ask user to filter
        if len(rendered) > call.max_chars:
            preview = self._render_dataframe(df.head(2))
            extra = {}
            if sheet_used:
                extra["SHEET"] = sheet_used
            return {
                "OK": False,
                "ACTION": "read",
                "PATH": call.path,
                "NEEDS_CONFIRMATION": True,
                "FORMAT": "table",
                "ROWS_TOTAL": rows_total,
                "ROWS_AFTER_FILTER": rows_after,
                "COLUMNS": all_columns,
                **extra,
                "PREVIEW": (
                    f"Table has {rows_after} rows × {len(df.columns)} cols "
                    f"({rows_total} total rows). "
                    f"Output ({len(rendered):,} chars) exceeds max_chars ({call.max_chars:,}). "
                    f"Columns: {all_columns}. First 2 rows:\n{preview}\n"
                    f"Filter with pandas_query='col > value' or search_pattern='text' "
                    f"or columns=['col1','col2']."
                ),
                "NOTE": "Call again with a filter to get results.",
            }

        result = _ok("read", call.path,
            FORMAT="table",
            CONTENT=rendered,
            ROWS_TOTAL=rows_total,
            COLUMNS=all_columns,
        )
        if rows_after < rows_total:
            result["ROWS_AFTER_FILTER"] = rows_after
        if sheet_used:
            result["SHEET"] = sheet_used
        return result

    def _try_read_pdf(self, p: Path, call: FilesystemCall) -> Optional[Dict[str, Any]]:
        """Extract text from PDF via pdfplumber → PyPDF2 fallback. Returns None if both unavailable."""
        text: Optional[str] = None

        try:
            import pdfplumber
            with pdfplumber.open(str(p)) as pdf:
                pages = [pg.extract_text() or "" for pg in pdf.pages]
            text = "\n\n".join(pages).strip()
        except ImportError:
            pass
        except Exception:
            text = None

        if text is None:
            try:
                import PyPDF2
                with open(str(p), "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    pages = [reader.pages[i].extract_text() or "" for i in range(len(reader.pages))]
                text = "\n\n".join(pages).strip()
            except ImportError:
                return None  # neither library available
            except Exception:
                return None

        if not text:
            result = _ok("read", call.path, FORMAT="pdf", CONTENT="")
            result["NOTE"] = "PDF contains no extractable text — may be a scanned image. Use open to view."
            return result

        return self._finalize_text_result(call, text, fmt="pdf")

    def _try_read_docx(self, p: Path, call: FilesystemCall) -> Optional[Dict[str, Any]]:
        """Extract text from DOCX via python-docx. Returns None if library unavailable."""
        try:
            from docx import Document
            doc = Document(str(p))
            text = "\n".join(para.text for para in doc.paragraphs if para.text.strip())
        except ImportError:
            return None
        except Exception:
            return None

        if not text:
            result = _ok("read", call.path, FORMAT="docx", CONTENT="")
            result["NOTE"] = "DOCX extracted but no text found."
            return result

        return self._finalize_text_result(call, text, fmt="docx")

    def _finalize_text_result(
        self, call: FilesystemCall, text: str, fmt: str
    ) -> Dict[str, Any]:
        """Apply search_pattern and max_chars to extracted text (pdf/docx shared logic)."""
        if call.search_pattern:
            try:
                matched = self._apply_search_to_text(
                    text, call.search_pattern,
                    context_lines=call.context_lines,
                    case_sensitive=call.case_sensitive,
                    use_regex=call.regex,
                )
            except ValueError as exc:
                return _err("read", call.path, f"search_pattern error: {exc}")
            content = "\n".join(matched)
            result = _ok("read", call.path,
                FORMAT=fmt,
                CONTENT=content[: call.max_chars],
                TOTAL_FOUND=len(matched),
            )
            if not matched:
                result["NOTE"] = f"No lines matched search_pattern in {fmt.upper()} text."
            elif len(content) > call.max_chars:
                result["TRUNCATED"] = True
            return result

        truncated = len(text) > call.max_chars
        result = _ok("read", call.path,
            FORMAT=fmt,
            CONTENT=text[: call.max_chars] if truncated else text,
        )
        if truncated:
            result["TRUNCATED"] = True
            result["NOTE"] = (
                f"{fmt.upper()} text truncated at {call.max_chars} chars. "
                "Use search_pattern to find specific content."
            )
        return result

    def _apply_search_to_text(
        self, text: str, pattern: str, *,
        context_lines: int, case_sensitive: bool, use_regex: bool,
    ) -> List[str]:
        """Filter text lines by pattern. Returns matching lines with context."""
        matcher = _compile_pattern(pattern, case_sensitive=case_sensitive, use_regex=use_regex)
        lines = text.splitlines()
        seen: set = set()
        result: List[str] = []
        for idx, line in enumerate(lines):
            if not matcher(line):
                continue
            start = max(0, idx - context_lines)
            end = min(len(lines), idx + context_lines + 1)
            for i in range(start, end):
                if i not in seen:
                    seen.add(i)
                    result.append(lines[i])
        return result

    def _render_dataframe(self, df: Any) -> str:
        """Render DataFrame as pipe-separated table string readable by the LLM."""
        try:
            cols = list(df.columns)
            col_w = {
                c: max(len(str(c)), int(df[c].astype(str).str.len().max() or 0))
                for c in cols
            }
            header = " | ".join(str(c).ljust(col_w[c]) for c in cols)
            sep    = "-+-".join("-" * col_w[c] for c in cols)
            rows   = [
                " | ".join(str(v).ljust(col_w[c]) for c, v in zip(cols, row))
                for row in df.itertuples(index=False, name=None)
            ]
            return "\n".join([header, sep] + rows)
        except Exception:
            return str(df)

    def _binary_open_gate(self, call: FilesystemCall) -> Dict[str, Any]:
        """Confirm with user before opening a binary file; if confirmed, open it."""
        if not call.confirmed:
            return {
                "OK": False,
                "ACTION": "read",
                "PATH": call.path,
                "NEEDS_CONFIRMATION": True,
                "FORMAT": "binary",
                "PREVIEW": (
                    f"Cannot read {call.path} as text — binary or unsupported format. "
                    "I can open it with the default application instead. Shall I?"
                ),
                "NOTE": "Call again with confirmed=true to open, or check the file path.",
            }
        return self._open(call)

    # ----------------------------------------------------------
    # read_lines
    # ----------------------------------------------------------

    def _read_lines(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return _err("read_lines", call.path, f"File not found: {call.path}")
        if p.is_dir():
            return _err("read_lines", call.path, "Path is a directory.")
        if _is_likely_binary(p):
            return _err("read_lines", call.path, f"Binary file ({p.suffix}) cannot be read as text.")

        try:
            lines = p.read_text(encoding=call.encoding, errors="replace").splitlines(keepends=True)
        except OSError as exc:
            return _err("read_lines", call.path, str(exc))

        total = len(lines)
        start = max(1, call.start_line or 1)
        end = min(total, call.end_line or total)

        if start > total:
            return _err("read_lines", call.path,
                f"start_line={start} exceeds file length ({total} lines).")

        content = "".join(lines[start - 1 : end])
        truncated = len(content) > call.max_chars
        result = _ok("read_lines", call.path,
            CONTENT=content[: call.max_chars] if truncated else content,
            LINE_COUNT=total,
            START_LINE=start,
            END_LINE=min(end, total),
        )
        if truncated:
            result["TRUNCATED"] = True
            result["NOTE"] = f"Content truncated at {call.max_chars} chars. Use a smaller line range."
        return result

    # ----------------------------------------------------------
    # list
    # ----------------------------------------------------------

    def _list(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return _err("list", call.path, f"Path does not exist: {call.path}")
        if not p.is_dir():
            return _err("list", call.path, "Not a directory — use read instead.")

        try:
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return _err("list", call.path, f"Permission denied: {call.path}")

        results = [
            _entry_dict(child) for child in entries
            if call.show_hidden or not child.name.startswith(".")
        ]
        return _ok("list", call.path, RESULTS=results, TOTAL_FOUND=len(results))

    # ----------------------------------------------------------
    # tree
    # ----------------------------------------------------------

    def _tree(self, call: FilesystemCall) -> Dict[str, Any]:
        root = Path(call.path)
        if not root.exists():
            return _err("tree", call.path, f"Path does not exist: {call.path}")
        if not root.is_dir():
            return _err("tree", call.path, "Not a directory.")

        lines: List[str] = [str(root)]
        entry_count = [0]

        def _walk(directory: Path, prefix: str, current_depth: int) -> None:
            if current_depth > call.depth:
                return
            try:
                children = sorted(directory.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except PermissionError:
                lines.append(f"{prefix}└── [permission denied]")
                return
            visible = [c for c in children if call.show_hidden or not c.name.startswith(".")]
            for i, child in enumerate(visible):
                if entry_count[0] >= call.max_results:
                    lines.append(f"{prefix}└── ... ({len(visible) - i} more entries)")
                    return
                is_last = i == len(visible) - 1
                connector = "└── " if is_last else "├── "
                suffix = "/" if child.is_dir() else ""
                lines.append(f"{prefix}{connector}{child.name}{suffix}")
                entry_count[0] += 1
                if child.is_dir():
                    extension = "    " if is_last else "│   "
                    _walk(child, prefix + extension, current_depth + 1)

        _walk(root, "", 1)
        return _ok("tree", call.path,
            TREE_TEXT="\n".join(lines),
            TOTAL_FOUND=entry_count[0],
        )

    # ----------------------------------------------------------
    # open
    # ----------------------------------------------------------

    def _open(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return _err("open", call.path, f"Path does not exist: {call.path}")
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.Popen(["open", str(p)])
            elif system == "Linux":
                subprocess.Popen(["xdg-open", str(p)])
            elif system == "Windows":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                return _err("open", call.path, f"Unsupported platform: {system}")
        except Exception as exc:
            return _err("open", call.path, f"Failed to open: {exc}")
        return _ok("open", call.path, OPENED=True)

    # ----------------------------------------------------------
    # info
    # ----------------------------------------------------------

    def _info(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return _ok("info", call.path, EXISTS=False)
        try:
            stat = p.stat()
        except OSError as exc:
            return _err("info", call.path, str(exc))
        result = _ok("info", call.path,
            EXISTS=True,
            IS_FILE=p.is_file(),
            IS_DIR=p.is_dir(),
            MODIFIED=_iso(stat.st_mtime),
            CREATED=_iso(stat.st_ctime),
        )
        if p.is_file():
            result["SIZE_BYTES"] = stat.st_size
        return result

    # ----------------------------------------------------------
    # write  (confirmed gate)
    # ----------------------------------------------------------

    def _write(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        content = call.content or ""

        if not call.confirmed:
            if p.exists() and p.is_file():
                try:
                    old_size = p.stat().st_size
                    old_mod = _iso(p.stat().st_mtime)
                except OSError:
                    old_size, old_mod = None, None
                preview = (
                    f"Will OVERWRITE {call.path} "
                    f"(currently {_human_size(old_size)}, modified {old_mod}) "
                    f"with {len(content)} chars of new content. "
                    "This cannot be undone."
                )
            else:
                preview = (
                    f"Will CREATE {call.path} "
                    f"with {len(content)} chars of content."
                )
            if not content:
                preview += " NOTE: content is empty — is this intentional?"
            return _needs_confirmation("write", call.path, preview)

        # confirmed=true: user has seen the PREVIEW and approved — proceed regardless of overwrite flag.

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return _err("write", call.path, str(exc))

        return _ok("write", call.path, SIZE_BYTES=len(content.encode("utf-8")))

    # ----------------------------------------------------------
    # append  (no confirmation — additive, not destructive)
    # ----------------------------------------------------------

    def _append(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        content = call.content or ""
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
            size = p.stat().st_size
        except OSError as exc:
            return _err("append", call.path, str(exc))
        return _ok("append", call.path, SIZE_BYTES=size)

    # ----------------------------------------------------------
    # patch  (confirmed gate — find + replace text in file)
    # ----------------------------------------------------------

    def _patch(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return _err("patch", call.path, f"File not found: {call.path}")
        if p.is_dir():
            return _err("patch", call.path, "Path is a directory — patch requires a file.")
        if _is_likely_binary(p):
            return _err("patch", call.path, f"Binary file ({p.suffix}) — patch works on text files only.")
        if not call.old_str:
            return _err("patch", call.path, "old_str is required — the exact text to find.")

        try:
            original = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _err("patch", call.path, str(exc))

        count = original.count(call.old_str)
        new_str = call.new_str if call.new_str is not None else ""

        if count == 0:
            return _err("patch", call.path,
                "old_str not found in file. "
                "Check exact whitespace, indentation, and line endings.")

        if count > 1 and not call.replace_all:
            return _err("patch", call.path,
                f"Found {count} occurrences of old_str. "
                "Set replace_all=true to replace all occurrences, "
                "or add more surrounding context to old_str to make it unique.")

        patched = (
            original.replace(call.old_str, new_str)
            if call.replace_all
            else original.replace(call.old_str, new_str, 1)
        )

        # Build unified diff for the preview
        diff_lines = list(difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=call.path,
            tofile=call.path,
            lineterm="",
            n=2,
        ))
        diff_text = "\n".join(diff_lines[:60])  # cap preview at 60 lines

        if not call.confirmed:
            action_desc = (
                f"replace all {count} occurrence{'s' if count != 1 else ''}"
                if call.replace_all
                else "replace 1 occurrence"
            )
            return _needs_confirmation(
                "patch", call.path,
                f"Will PATCH {call.path} — {action_desc}:\n{diff_text}"
            )

        try:
            p.write_text(patched, encoding="utf-8")
        except OSError as exc:
            return _err("patch", call.path, str(exc))

        return _ok("patch", call.path,
            OCCURRENCES=count,
            SIZE_BYTES=len(patched.encode("utf-8")),
        )

    # ----------------------------------------------------------
    # delete  (confirmed gate)
    # ----------------------------------------------------------

    def _delete(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        if not p.exists():
            return _err("delete", call.path, f"Path does not exist: {call.path}")

        if not call.confirmed:
            if p.is_file():
                try:
                    size = _human_size(p.stat().st_size)
                except OSError:
                    size = "unknown size"
                preview = f"Will permanently DELETE file {call.path} ({size}). Cannot be undone."
            else:
                try:
                    count = sum(1 for _ in p.rglob("*"))
                    preview = f"Will permanently DELETE directory {call.path} and all {count} items inside it. Cannot be undone."
                except Exception:
                    preview = f"Will permanently DELETE directory {call.path} and all its contents. Cannot be undone."
            return _needs_confirmation("delete", call.path, preview)

        try:
            if p.is_dir():
                shutil.rmtree(str(p))
            else:
                p.unlink()
        except OSError as exc:
            return _err("delete", call.path, str(exc))
        return _ok("delete", call.path)

    # ----------------------------------------------------------
    # copy  (no confirmation — non-destructive to source)
    # ----------------------------------------------------------

    def _copy(self, call: FilesystemCall) -> Dict[str, Any]:
        if not call.destination:
            return _err("copy", call.path, "destination is required for copy.")
        src, dst = Path(call.path), Path(call.destination)
        if not src.exists():
            return _err("copy", call.path, f"Source does not exist: {call.path}")
        # If destination is a directory, copy into it
        if dst.is_dir():
            dst = dst / src.name
        if src.resolve() == dst.resolve():
            return _err("copy", call.path, "Source and destination are the same path.")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
        except OSError as exc:
            return _err("copy", call.path, str(exc))
        return _ok("copy", call.path, DESTINATION=str(dst))

    # ----------------------------------------------------------
    # move  (confirmed gate)
    # ----------------------------------------------------------

    def _move(self, call: FilesystemCall) -> Dict[str, Any]:
        if not call.destination:
            return _err("move", call.path, "destination is required for move.")
        src, dst = Path(call.path), Path(call.destination)
        if not src.exists():
            return _err("move", call.path, f"Source does not exist: {call.path}")
        # If destination is a directory, move into it
        if dst.is_dir():
            dst = dst / src.name

        if src.resolve() == dst.resolve():
            return _err("move", call.path, "Source and destination are the same path.")

        if not call.confirmed:
            exists_note = f"Destination {dst} {'already exists and will be replaced' if dst.exists() else 'does not exist'}."
            preview = f"Will MOVE {call.path} → {dst}. {exists_note} Cannot be undone."
            return _needs_confirmation("move", call.path, preview)

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as exc:
            return _err("move", call.path, str(exc))
        return _ok("move", call.path, DESTINATION=str(dst))

    # ----------------------------------------------------------
    # mkdir  (no confirmation — idempotent)
    # ----------------------------------------------------------

    def _mkdir(self, call: FilesystemCall) -> Dict[str, Any]:
        p = Path(call.path)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _err("mkdir", call.path, str(exc))
        return _ok("mkdir", call.path)

    # ----------------------------------------------------------
    # grep
    # ----------------------------------------------------------

    def _grep(self, call: FilesystemCall) -> Dict[str, Any]:
        root = Path(call.path)
        if not root.exists():
            return _err("grep", call.path, f"Path does not exist: {call.path}")

        query = call.content_query or ""
        if not query:
            return _err("grep", call.path,
                "grep requires content_query (the text/regex to find). "
                "Use pattern to filter which files to search.")

        try:
            matcher = _compile_pattern(query, case_sensitive=call.case_sensitive, use_regex=call.regex)
        except ValueError as exc:
            return _err("grep", call.path, str(exc))

        if root.is_file():
            if _is_likely_binary(root):
                return _err("grep", call.path, f"Binary file ({root.suffix}) cannot be grepped.")
            files_to_search = [root]
        else:
            file_pattern = call.pattern or "*"
            try:
                candidates = root.rglob(file_pattern) if call.recursive else root.glob(file_pattern)
            except Exception as exc:
                return _err("grep", call.path, f"Glob pattern error: {exc}")
            files_to_search = [
                p for p in candidates
                if p.is_file()
                and not _is_likely_binary(p)
                and _matches_file_types(p, call.file_types)
                and (call.show_hidden or not any(
                    part.startswith(".") for part in p.parts[len(root.parts):]
                ))
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
                    ctx_before = [l.rstrip() for l in lines[max(0, i - call.context_lines): i]]
                    ctx_after = [l.rstrip() for l in lines[i + 1: i + 1 + call.context_lines]]
                    entry: Dict[str, Any] = {
                        "path": str(file_path),
                        "line_number": i + 1,
                        "line": line.rstrip(),
                    }
                    if ctx_before:
                        entry["context_before"] = ctx_before
                    if ctx_after:
                        entry["context_after"] = ctx_after
                    results.append(entry)

        result = _ok("grep", call.path, RESULTS=results, TOTAL_FOUND=total)
        if total > call.max_results:
            result["NOTE"] = f"Showing {call.max_results} of {total} matches. Increase max_results or narrow query."
        elif total == 0:
            result["NOTE"] = "No matches. Try case_sensitive=false, regex=false, or a broader query."
        return result

    # ----------------------------------------------------------
    # diff
    # ----------------------------------------------------------

    def _diff(self, call: FilesystemCall) -> Dict[str, Any]:
        if not call.destination:
            return _err("diff", call.path, "destination (second file path) is required for diff.")
        a, b = Path(call.path), Path(call.destination)
        for p, label in [(a, "path"), (b, "destination")]:
            if not p.exists():
                return _err("diff", call.path, f"{label} does not exist: {p}")
            if p.is_dir():
                return _err("diff", call.path, f"{label} is a directory — diff requires two files.")
            if _is_likely_binary(p):
                return _err("diff", call.path, f"{label} is binary ({p.suffix}) — cannot diff.")
        try:
            a_lines = a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            b_lines = b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError as exc:
            return _err("diff", call.path, str(exc))

        diff = list(difflib.unified_diff(
            a_lines, b_lines,
            fromfile=str(a), tofile=str(b), lineterm="",
        ))

        if not diff:
            return _ok("diff", call.path, IDENTICAL=True)

        diff_text = "\n".join(diff)
        truncated = len(diff_text) > call.max_chars
        result = _ok("diff", call.path,
            IDENTICAL=False,
            DIFF_TEXT=diff_text[: call.max_chars] if truncated else diff_text,
        )
        if truncated:
            result["TRUNCATED"] = True
            result["NOTE"] = f"Diff truncated at {call.max_chars} chars."
        return result


# ==========================================================
# Dynamic registry hooks
# ==========================================================

TOOL_NAME = "filesystem"
TOOL_CLASS = Filesystem


def get_tool() -> Filesystem:
    return Filesystem()
