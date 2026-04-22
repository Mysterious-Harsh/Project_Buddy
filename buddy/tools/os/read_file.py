from __future__ import annotations

# ==========================================================
# read_file.py
#
# Reads any file — text, code, PDF, DOCX, CSV/Excel/Parquet.
# Also handles: directory listing, directory tree, file info.
# ==========================================================

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from buddy.prompts.read_file_prompts import (
    READ_FILE_ERROR_PROMPT,
    READ_FILE_PROMPT,
    tool_call_format,
)
from buddy.brain.text_reader import CHAR_THRESHOLD, maybe_read
from buddy.tools.os._fs_helpers import (
    BINARY_EXTENSIONS,
    DEFAULT_MAX_CHARS,
    DEFAULT_DEPTH,
    DOCX_EXTENSIONS,
    MAX_CHARS_HARD_LIMIT,
    MAX_DEPTH,
    PDF_EXTENSIONS,
    TABULAR_EXTENSIONS,
    compile_pattern,
    entry_dict,
    err,
    human_size,
    is_likely_binary,
    iso_time,
    needs_confirmation,
    ok,
    resolve_path,
    tree_entry_label,
)

# ==========================================================
# Input model
# ==========================================================


class ReadFileCall(BaseModel):
    path: str

    # line range — text files only
    start_line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)

    # content filtering
    search_pattern: Optional[str] = None

    # tabular files
    pandas_query: Optional[str] = None
    columns: Optional[List[str]] = None
    sheet_name: Optional[str] = None

    # limits
    max_chars: int = Field(default=DEFAULT_MAX_CHARS, ge=1, le=MAX_CHARS_HARD_LIMIT)
    encoding: str = "utf-8"

    # directory options
    depth: int = Field(default=DEFAULT_DEPTH, ge=1, le=MAX_DEPTH)
    show_hidden: bool = False

    # metadata only
    info: bool = False

    # binary gate
    confirmed: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, values: Any) -> Any:
        if isinstance(values, dict):
            p = values.get("path")
            if p:
                values["path"] = resolve_path(str(p))
        return values


# ==========================================================
# Tool
# ==========================================================


class ReadFile:
    tool_name = "read_file"
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "Read any file (text, code, PDF, DOCX, CSV/Excel/Parquet). Also lists"
                " directory contents, shows directory tree, returns file metadata."
            ),
            "prompt": READ_FILE_PROMPT,
            "error_prompt": READ_FILE_ERROR_PROMPT,
            "tool_call_format": tool_call_format,
        }

    def parse_call(self, payload: Dict[str, Any]) -> ReadFileCall:
        return ReadFileCall.model_validate(payload)

    async def execute(
        self,
        call: ReadFileCall,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        brain = _kwargs.get("brain")
        goal = str(_kwargs.get("goal") or "")
        if on_progress:
            on_progress(f"Reading · {call.path}", False)
        return await asyncio.to_thread(self._execute_sync, call, brain, goal, on_progress)

    def _execute_sync(
        self,
        call: ReadFileCall,
        brain: Any = None,
        goal: str = "",
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> Dict[str, Any]:
        try:
            p = Path(call.path)

            # info only
            if call.info:
                return self._info(call, p)

            # directory
            if p.is_dir():
                return self._directory(call, p)

            # file does not exist
            if not p.exists():
                return err(self.tool_name, call.path, f"File not found: {call.path}")

            # line range read
            if call.start_line or call.end_line:
                return self._read_lines(call, p)

            # content read (format auto-detected)
            return self._read(call, p, brain=brain, goal=goal, on_progress=on_progress)

        except Exception as exc:
            return err(self.tool_name, call.path, f"{type(exc).__name__}: {exc}")

    # ----------------------------------------------------------
    # info
    # ----------------------------------------------------------

    def _info(self, call: ReadFileCall, p: Path) -> Dict[str, Any]:
        if not p.exists():
            return ok(self.tool_name, call.path, EXISTS=False)
        try:
            stat = p.stat()
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))
        result = ok(
            self.tool_name,
            call.path,
            EXISTS=True,
            IS_FILE=p.is_file(),
            IS_DIR=p.is_dir(),
            MODIFIED=iso_time(stat.st_mtime),
            CREATED=iso_time(stat.st_ctime),
        )
        if p.is_file():
            result["SIZE_BYTES"] = stat.st_size
        return result

    # ----------------------------------------------------------
    # directory listing / tree
    # ----------------------------------------------------------

    def _directory(self, call: ReadFileCall, p: Path) -> Dict[str, Any]:
        # tree if depth requested explicitly or default — use tree for dirs
        if call.depth and call.depth > 1:
            return self._tree(call, p)
        return self._list(call, p)

    def _list(self, call: ReadFileCall, p: Path) -> Dict[str, Any]:
        try:
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return err(self.tool_name, call.path, f"Permission denied: {call.path}")
        results = [
            entry_dict(child)
            for child in entries
            if call.show_hidden or not child.name.startswith(".")
        ]
        return ok(self.tool_name, call.path, RESULTS=results, TOTAL_FOUND=len(results))

    def _tree(self, call: ReadFileCall, p: Path) -> Dict[str, Any]:
        lines: List[str] = [str(p)]
        count = [0]

        def _walk(directory: Path, prefix: str, current_depth: int) -> None:
            if current_depth > call.depth:
                return
            try:
                children = sorted(
                    directory.iterdir(), key=lambda x: (x.is_file(), x.name.lower())
                )
            except PermissionError:
                lines.append(f"{prefix}└── [permission denied]")
                return
            visible = [
                c for c in children if call.show_hidden or not c.name.startswith(".")
            ]
            for i, child in enumerate(visible):
                if count[0] >= 200:
                    lines.append(f"{prefix}└── ... ({len(visible) - i} more)")
                    return
                is_last = i == len(visible) - 1
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{tree_entry_label(child)}")
                count[0] += 1
                if child.is_dir():
                    _walk(
                        child,
                        prefix + ("    " if is_last else "│   "),
                        current_depth + 1,
                    )

        _walk(p, "", 1)
        return ok(
            self.tool_name, call.path, TREE_TEXT="\n".join(lines), TOTAL_FOUND=count[0]
        )

    # ----------------------------------------------------------
    # read lines
    # ----------------------------------------------------------

    def _read_lines(self, call: ReadFileCall, p: Path) -> Dict[str, Any]:
        if is_likely_binary(p):
            return err(
                self.tool_name,
                call.path,
                f"Binary file ({p.suffix}) cannot be read as text.",
            )
        try:
            lines = p.read_text(encoding=call.encoding, errors="replace").splitlines(
                keepends=True
            )
        except OSError as exc:
            return err(self.tool_name, call.path, str(exc))

        total = len(lines)
        start = max(1, call.start_line or 1)
        end = min(total, call.end_line or total)

        if start > total:
            return err(
                self.tool_name,
                call.path,
                f"start_line={start} exceeds file length ({total} lines).",
            )

        content = "".join(lines[start - 1 : end])
        truncated = len(content) > call.max_chars
        result = ok(
            self.tool_name,
            call.path,
            CONTENT=content[: call.max_chars] if truncated else content,
            LINE_COUNT=total,
            START_LINE=start,
            END_LINE=min(end, total),
        )
        if truncated:
            result["TRUNCATED"] = True
            result["NOTE"] = (
                f"Content truncated at {call.max_chars} chars. Use a smaller line"
                " range."
            )
        return result

    # ----------------------------------------------------------
    # read content (multi-format)
    # ----------------------------------------------------------

    def _read(
        self,
        call: ReadFileCall,
        p: Path,
        brain: Any = None,
        goal: str = "",
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> Dict[str, Any]:
        ext = p.suffix.lower()

        if ext in BINARY_EXTENSIONS:
            return self._binary_gate(call, p)

        strategies = []
        if ext in TABULAR_EXTENSIONS:
            strategies.append(lambda _p, _c: self._try_tabular(_p, _c))
        elif ext in PDF_EXTENSIONS:
            strategies.append(
                lambda _p, _c: self._try_pdf(_p, _c, brain=brain, goal=goal, on_progress=on_progress)
            )
        elif ext in DOCX_EXTENSIONS:
            strategies.append(
                lambda _p, _c: self._try_docx(_p, _c, brain=brain, goal=goal, on_progress=on_progress)
            )
        strategies.append(
            lambda _p, _c: self._try_text(_p, _c, brain=brain, goal=goal, on_progress=on_progress)
        )

        for strategy in strategies:
            try:
                result = strategy(p, call)
                if result is not None:
                    return result
            except Exception:
                continue

        return self._binary_gate(call, p)

    # ----------------------------------------------------------
    # format strategies
    # ----------------------------------------------------------

    def _try_text(
        self,
        p: Path,
        call: ReadFileCall,
        brain: Any = None,
        goal: str = "",
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        raw = None
        for enc in [call.encoding, "latin-1", "cp1252"]:
            try:
                raw = p.read_text(encoding=enc, errors="strict")
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if raw is None:
            return None

        try:
            stat = p.stat()
        except OSError:
            stat = None

        if call.search_pattern:
            try:
                matched = self._filter_lines(raw, call)
            except ValueError as exc:
                return err(self.tool_name, call.path, f"search_pattern error: {exc}")
            content = "\n".join(matched)
            result = ok(
                self.tool_name,
                call.path,
                FORMAT="text",
                CONTENT=content[: call.max_chars],
                TOTAL_FOUND=len(matched),
            )
            if not matched:
                result["NOTE"] = "No lines matched search_pattern."
            elif len(content) > call.max_chars:
                result["TRUNCATED"] = True
            if stat:
                result["SIZE_BYTES"] = stat.st_size
                result["MODIFIED"] = iso_time(stat.st_mtime)
            return result

        # Large text: use TextReader for intelligent extraction if brain is available
        if brain and goal and len(raw) > CHAR_THRESHOLD:
            content = maybe_read(raw, goal, brain, on_progress)
            result = ok(self.tool_name, call.path, FORMAT="text", CONTENT=content[: call.max_chars])
            if stat:
                result["SIZE_BYTES"] = stat.st_size
                result["MODIFIED"] = iso_time(stat.st_mtime)
            return result

        truncated = len(raw) > call.max_chars
        result = ok(
            self.tool_name,
            call.path,
            FORMAT="text",
            CONTENT=raw[: call.max_chars] if truncated else raw,
        )
        if stat:
            result["SIZE_BYTES"] = stat.st_size
            result["MODIFIED"] = iso_time(stat.st_mtime)
        if truncated:
            result["TRUNCATED"] = True
            result["NOTE"] = (
                f"Showing first {call.max_chars} chars of"
                f" {human_size(stat.st_size if stat else None)}. Use"
                " start_line/end_line to read other sections, or search_pattern to"
                " filter."
            )
        return result

    def _try_tabular(self, p: Path, call: ReadFileCall) -> Optional[Dict[str, Any]]:
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
                    call.sheet_name
                    if call.sheet_name in xl.sheet_names
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
        all_columns = list(df.columns)

        if call.columns:
            valid = [c for c in call.columns if c in df.columns]
            if not valid:
                return err(
                    self.tool_name,
                    call.path,
                    f"None of columns {call.columns} found. Available: {all_columns}",
                )
            df = df[valid]

        if call.pandas_query:
            try:
                df = df.query(call.pandas_query)
            except Exception as exc:
                return err(
                    self.tool_name,
                    call.path,
                    f"pandas_query error: {exc}. Available columns: {all_columns}",
                )

        if call.search_pattern:
            try:
                import re as _re

                flags = 0 if False else _re.IGNORECASE
                mask = df.apply(
                    lambda row: row.astype(str)
                    .str.contains(
                        call.search_pattern, flags=flags, regex=False, na=False
                    )
                    .any(),
                    axis=1,
                )
                df = df[mask]
            except Exception as exc:
                return err(self.tool_name, call.path, f"search_pattern error: {exc}")

        rows_after = len(df)
        rendered = self._render_df(df)

        if len(rendered) > call.max_chars:
            preview = self._render_df(df.head(2))
            result = {
                "OK": False,
                "TOOL": self.tool_name,
                "PATH": call.path,
                "NEEDS_CONFIRMATION": True,
                "FORMAT": "table",
                "ROWS_TOTAL": rows_total,
                "ROWS_AFTER_FILTER": rows_after,
                "COLUMNS": all_columns,
                "PREVIEW": (
                    f"Table has {rows_after} rows × {len(df.columns)} cols"
                    f" ({rows_total} total). Output ({len(rendered):,} chars) exceeds"
                    f" max_chars ({call.max_chars:,}). Columns: {all_columns}. First 2"
                    f" rows:\n{preview}\nFilter with pandas_query='col > value' or"
                    " columns=['col1','col2']."
                ),
                "NOTE": "Call again with a filter to get results.",
            }
            if sheet_used:
                result["SHEET"] = sheet_used
            return result

        result = ok(
            self.tool_name,
            call.path,
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

    def _try_pdf(
        self,
        p: Path,
        call: ReadFileCall,
        brain: Any = None,
        goal: str = "",
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        text: Optional[str] = None
        try:
            import pdfplumber

            with pdfplumber.open(str(p)) as pdf:
                text = "\n\n".join(pg.extract_text() or "" for pg in pdf.pages).strip()
        except ImportError:
            pass
        except Exception:
            text = None

        if text is None:
            try:
                import PyPDF2

                with open(str(p), "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    text = "\n\n".join(
                        reader.pages[i].extract_text() or ""
                        for i in range(len(reader.pages))
                    ).strip()
            except ImportError:
                return None
            except Exception:
                return None

        if not text:
            result = ok(self.tool_name, call.path, FORMAT="pdf", CONTENT="")
            result["NOTE"] = (
                "PDF contains no extractable text — may be scanned image. Use"
                " confirmed=true to open."
            )
            return result

        return self._finalize_text(call, text, fmt="pdf", brain=brain, goal=goal, on_progress=on_progress)

    def _try_docx(
        self,
        p: Path,
        call: ReadFileCall,
        brain: Any = None,
        goal: str = "",
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            from docx import Document

            doc = Document(str(p))
            text = "\n".join(para.text for para in doc.paragraphs if para.text.strip())
        except ImportError:
            return None
        except Exception:
            return None

        if not text:
            result = ok(self.tool_name, call.path, FORMAT="docx", CONTENT="")
            result["NOTE"] = "DOCX extracted but no text found."
            return result

        return self._finalize_text(call, text, fmt="docx", brain=brain, goal=goal, on_progress=on_progress)

    def _finalize_text(
        self,
        call: ReadFileCall,
        text: str,
        fmt: str,
        brain: Any = None,
        goal: str = "",
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> Dict[str, Any]:
        if call.search_pattern:
            try:
                matched = self._filter_lines(text, call)
            except ValueError as exc:
                return err(self.tool_name, call.path, f"search_pattern error: {exc}")
            content = "\n".join(matched)
            result = ok(
                self.tool_name,
                call.path,
                FORMAT=fmt,
                CONTENT=content[: call.max_chars],
                TOTAL_FOUND=len(matched),
            )
            if not matched:
                result["NOTE"] = f"No lines matched search_pattern in {fmt.upper()}."
            elif len(content) > call.max_chars:
                result["TRUNCATED"] = True
            return result

        # Large document: use TextReader for intelligent extraction if brain is available
        if brain and goal and len(text) > CHAR_THRESHOLD:
            content = maybe_read(text, goal, brain, on_progress)
            result = ok(self.tool_name, call.path, FORMAT=fmt, CONTENT=content[: call.max_chars])
            return result

        truncated = len(text) > call.max_chars
        result = ok(
            self.tool_name,
            call.path,
            FORMAT=fmt,
            CONTENT=text[: call.max_chars] if truncated else text,
        )
        if truncated:
            result["TRUNCATED"] = True
            result["NOTE"] = (
                f"{fmt.upper()} truncated at {call.max_chars} chars. Use search_pattern"
                " to find specific content."
            )
        return result

    def _filter_lines(self, text: str, call: ReadFileCall) -> List[str]:
        assert call.search_pattern is not None
        matcher = compile_pattern(
            call.search_pattern, case_sensitive=False, use_regex=False
        )
        lines = text.splitlines()
        seen: set = set()
        result: List[str] = []
        for idx, line in enumerate(lines):
            if not matcher(line):
                continue
            start = max(0, idx - 2)
            end = min(len(lines), idx + 3)
            for i in range(start, end):
                if i not in seen:
                    seen.add(i)
                    result.append(lines[i])
        return result

    def _render_df(self, df: Any) -> str:
        try:
            cols = list(df.columns)
            col_w = {
                c: max(len(str(c)), int(df[c].astype(str).str.len().max() or 0))
                for c in cols
            }
            header = " | ".join(str(c).ljust(col_w[c]) for c in cols)
            sep = "-+-".join("-" * col_w[c] for c in cols)
            rows = [
                " | ".join(str(v).ljust(col_w[c]) for c, v in zip(cols, row))
                for row in df.itertuples(index=False, name=None)
            ]
            return "\n".join([header, sep] + rows)
        except Exception:
            return str(df)

    def _binary_gate(self, call: ReadFileCall, p: Path) -> Dict[str, Any]:
        if not call.confirmed:
            return {
                "OK": False,
                "TOOL": self.tool_name,
                "PATH": call.path,
                "NEEDS_CONFIRMATION": True,
                "FORMAT": "binary",
                "PREVIEW": (
                    f"Cannot read {call.path} as text — binary or unsupported format."
                    " Call again with confirmed=true to open with default app."
                ),
                "NOTE": "Call again with confirmed=true to open.",
            }
        import platform, subprocess

        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.Popen(["open", str(p)])
            elif system == "Linux":
                subprocess.Popen(["xdg-open", str(p)])
            elif system == "Windows":
                import os as _os

                _os.startfile(str(p))
            else:
                return err(self.tool_name, call.path, f"Unsupported platform: {system}")
        except Exception as exc:
            return err(self.tool_name, call.path, f"Failed to open: {exc}")
        return ok(self.tool_name, call.path, OPENED=True)


# ==========================================================
# Registry hooks
# ==========================================================

TOOL_NAME = "read_file"
TOOL_CLASS = ReadFile


def get_tool() -> ReadFile:
    return ReadFile()
