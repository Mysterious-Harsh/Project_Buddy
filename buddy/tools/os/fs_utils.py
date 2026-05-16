"""Shared constants and helpers for all fs_* tools."""
from __future__ import annotations

import mimetypes
import os
import re
import stat as _stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── limits ───────────────────────────────────────────────────────────────────
MAX_CHARS = 8_000
MAX_CHARS_HARD = 50_000
MAX_DIR_ENTRIES = 200
MAX_DEPTH = 10

# ── extension sets ────────────────────────────────────────────────────────────
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


# ── path ─────────────────────────────────────────────────────────────────────

def resolve_path(raw: str) -> str:
    p = os.path.expanduser(os.path.expandvars(raw.strip()))
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser("~"), p)
    return p


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


def iso_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_binary(p: Path) -> bool:
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


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[{len(text) - limit} chars truncated]", True


# ── result helpers ────────────────────────────────────────────────────────────

def ok(tool: str, path: str = "", **extra: Any) -> Dict[str, Any]:
    r: Dict[str, Any] = {"STATUS": "success"}
    if path:
        r["PATH"] = path
    r.update(extra)
    return r


def err(tool: str, path: str = "", action: str = "", msg: str = "") -> Dict[str, Any]:
    r: Dict[str, Any] = {"STATUS": "failed", "TOOL": tool}
    if action:
        r["ACTION"] = action
    if path:
        r["PATH"] = path
    r["ERROR"] = msg
    return r


def needs_confirm(tool: str, path: str, preview: str) -> Dict[str, Any]:
    return {
        "STATUS": "failed",
        "TOOL": tool,
        "PATH": path,
        "NEEDS_CONFIRMATION": True,
        "PREVIEW": preview,
        "NOTE": "Call again with confirmed=true after user approves.",
    }


# ── directory helpers ─────────────────────────────────────────────────────────

def dir_entry(p: Path) -> Dict[str, Any]:
    try:
        s = p.stat()
        is_dir = p.is_dir()
        e: Dict[str, Any] = {
            "name": p.name,
            "path": str(p),
            "type": "dir" if is_dir else "file",
            "permissions": _stat.filemode(s.st_mode),
            "modified": iso_time(s.st_mtime),
            "created": iso_time(s.st_ctime),
        }
        if is_dir:
            try:
                e["item_count"] = sum(1 for _ in p.iterdir())
            except PermissionError:
                e["item_count"] = None
        else:
            e["size_bytes"] = s.st_size
            e["size"] = human_size(s.st_size)
            if p.suffix:
                e["extension"] = p.suffix.lower()
        return e
    except OSError:
        return {"name": p.name, "path": str(p), "type": "unknown"}


def tree_label(p: Path) -> str:
    try:
        s = p.stat()
        date = datetime.fromtimestamp(s.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        if p.is_dir():
            try:
                count = sum(1 for _ in p.iterdir())
                return f"{p.name}/ ({count} items, {date})"
            except PermissionError:
                return f"{p.name}/ ({date})"
        return f"{p.name} ({human_size(s.st_size)}, {date})"
    except OSError:
        return p.name + ("/" if p.is_dir() else "")


def make_matcher(pattern: str, *, case_sensitive: bool = False, use_regex: bool = False):
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        try:
            return re.compile(pattern, flags).search
        except re.error:
            return re.compile(re.escape(pattern), flags).search
    needle = pattern if case_sensitive else pattern.lower()
    return (lambda t: needle in t) if case_sensitive else (lambda t: needle in t.lower())
