"""
Shared helpers for read_file, edit_file, search_file, manage_file tools.
"""
from __future__ import annotations

import os
import re
import stat as _stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ==========================================================
# Constants
# ==========================================================

MAX_CHARS_HARD_LIMIT = 50_000
MAX_RESULTS_HARD_LIMIT = 500
DEFAULT_MAX_CHARS = 8_000
DEFAULT_MAX_RESULTS = 20
DEFAULT_DEPTH = 3
MAX_DEPTH = 10

# Truly unreadable binary formats — redirect to open.
# pdf/docx/xlsx removed — they have text extractors.
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

# ==========================================================
# Path helpers
# ==========================================================

def resolve_path(raw: str) -> str:
    """Expand ~, $VAR, resolve to absolute path."""
    p = os.path.expanduser(os.path.expandvars(raw.strip()))
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser("~"), p)
    return p


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "unknown size"
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_likely_binary(p: Path) -> bool:
    return p.suffix.lower() in BINARY_EXTENSIONS


def matches_file_types(p: Path, file_types: Optional[List[str]]) -> bool:
    if not file_types:
        return True
    return p.suffix.lstrip(".").lower() in file_types


# ==========================================================
# Pattern compiler
# ==========================================================

def compile_pattern(pattern: str, *, case_sensitive: bool, use_regex: bool):
    """Return a callable(text) -> bool for matching lines."""
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


# ==========================================================
# Result builders
# ==========================================================

def ok(tool: str, path: str, **extra) -> Dict[str, Any]:
    return {"OK": True, "TOOL": tool, "PATH": path, **extra}


def err(tool: str, path: str, error: str) -> Dict[str, Any]:
    return {"OK": False, "TOOL": tool, "PATH": path, "ERROR": error}


def needs_confirmation(tool: str, path: str, preview: str) -> Dict[str, Any]:
    return {
        "OK": False,
        "TOOL": tool,
        "PATH": path,
        "NEEDS_CONFIRMATION": True,
        "PREVIEW": preview,
        "NOTE": "Call again with confirmed=true after user approves.",
    }


# ==========================================================
# Directory entry dict + tree label
# ==========================================================

def entry_dict(p: Path) -> Dict[str, Any]:
    try:
        s = p.stat()
        is_dir = p.is_dir()
        result: Dict[str, Any] = {
            "name": p.name,
            "path": str(p),
            "type": "dir" if is_dir else "file",
            "permissions": _stat.filemode(s.st_mode),
            "modified": iso_time(s.st_mtime),
            "created": iso_time(s.st_ctime),
        }
        if is_dir:
            try:
                result["item_count"] = sum(1 for _ in p.iterdir())
            except PermissionError:
                result["item_count"] = None
        else:
            result["size_bytes"] = s.st_size
            result["size"] = human_size(s.st_size)
            if p.suffix:
                result["extension"] = p.suffix.lower()
        return result
    except OSError:
        return {"name": p.name, "path": str(p), "type": "unknown"}


def tree_entry_label(p: Path) -> str:
    """Return a short inline label for tree view: 'name (size, date)' or 'name/ (N items, date)'."""
    try:
        s = p.stat()
        date = datetime.fromtimestamp(s.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        if p.is_dir():
            try:
                count = sum(1 for _ in p.iterdir())
                return f"{p.name}/ ({count} items, {date})"
            except PermissionError:
                return f"{p.name}/ ({date})"
        else:
            return f"{p.name} ({human_size(s.st_size)}, {date})"
    except OSError:
        return p.name + ("/" if p.is_dir() else "")
