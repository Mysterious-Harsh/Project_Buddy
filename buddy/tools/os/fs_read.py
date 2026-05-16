from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from buddy.prompts.fs_read_prompts import FS_READ_TOOL_PROMPT
from buddy.tools.os.fs_utils import (
    BINARY_EXTENSIONS, DOCX_EXTENSIONS, MAX_CHARS, MAX_CHARS_HARD, PDF_EXTENSIONS,
    TABULAR_EXTENSIONS, err, human_size, is_binary, iso_time, make_matcher,
    needs_confirm, ok, resolve_path, truncate,
)

TOOL_NAME = "fs_read"
_TOOL = TOOL_NAME


class FsRead:
    tool_name = _TOOL
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "WHEN: reading what is inside a file, or comparing two text files.\n\n"
                "FORMAT AUTO-DETECTION:\n"
                "  text/code  — .txt .md .json .yaml .toml .html .py .js .sh .log etc. → plain text\n"
                "  tabular    — .csv .tsv .xlsx .xls .parquet → rendered table; filter with pandas_query or columns\n"
                "  documents  — .pdf .docx/.doc → text extracted\n"
                "  binary     — .png .mp3 .zip etc. → returns NEEDS_CONFIRMATION to open with system app\n\n"
                "FUNCTIONS:\n"
                "  read(path, search_pattern?, start_line?, end_line?, info?)  — read any file; auto-detects format\n"
                "  diff(path_a, path_b)                                        — unified diff between two text files\n\n"
                "CHAIN: fs_browse find paths → fs_read. CONTENT output feeds word/pdf/excel/analyzer.\n"
                "NOT: listing dirs → fs_browse | writing/editing → fs_write | moving/deleting → fs_manage | structured Excel → excel | .docx create/edit → word | .pdf create/edit → pdf"
            ),
            "prompt": FS_READ_TOOL_PROMPT,
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
        if fn == "read":
            return self._read(arguments, brain=brain, goal=goal, on_progress=on_progress)
        if fn == "diff":
            return self._diff(arguments)
        return err(_TOOL, msg=f"Unknown function '{function}'. fs_read supports: read, diff.")

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
            return err(_TOOL, msg="read requires 'path' — absolute path to a file.")

        path = resolve_path(raw)
        p = Path(path)

        try:
            if args.get("info"):
                return self._info(p)

            if not p.exists():
                return err(_TOOL, path=path, msg=(
                    f"read failed — '{path}' does not exist. "
                    "Verify the path from prior step output or use fs_browse.find to locate the file."
                ))
            if p.is_dir():
                try:
                    count = sum(1 for _ in p.iterdir())
                    count_info = f" ({count} items)"
                except OSError:
                    count_info = ""
                return err(_TOOL, path=path, msg=(
                    f"read failed — '{path}'{count_info} is a directory, not a file. "
                    "Use fs_browse.ls to list directory contents."
                ))

            if args.get("start_line") or args.get("end_line"):
                return self._read_lines(p, args)
            return self._read_content(p, args, brain=brain, goal=goal, on_progress=on_progress)

        except PermissionError:
            return err(_TOOL, path=path, msg=(
                f"read failed — permission denied reading '{path}'. "
                "Check file permissions before retrying."
            ))
        except Exception as e:
            return err(_TOOL, path=path, msg=f"read failed — {type(e).__name__}: {e}")

    def _info(self, p: Path) -> Dict[str, Any]:
        if not p.exists():
            return ok(_TOOL, path=str(p), EXISTS=False)
        try:
            s = p.stat()
            r = ok(_TOOL, path=str(p), EXISTS=True, IS_FILE=p.is_file(), IS_DIR=p.is_dir(),
                   MODIFIED=iso_time(s.st_mtime), CREATED=iso_time(s.st_ctime))
            if p.is_file():
                r["SIZE_BYTES"] = s.st_size
                r["SIZE"] = human_size(s.st_size)
            return r
        except OSError as e:
            return err(_TOOL, path=str(p), msg=f"read info failed — {type(e).__name__}: {e}")

    def _read_lines(self, p: Path, args: Dict[str, Any]) -> Dict[str, Any]:
        if is_binary(p):
            return err(_TOOL, path=str(p), msg=(
                f"read lines failed — '{p.name}' is a binary file ({p.suffix or 'no extension'}). "
                "Line-range reading requires a plain text file."
            ))

        encoding = str(args.get("encoding") or "utf-8")
        max_chars = min(int(args.get("max_chars") or MAX_CHARS), MAX_CHARS_HARD)

        try:
            lines = p.read_text(encoding=encoding, errors="replace").splitlines(keepends=True)
        except (UnicodeDecodeError, LookupError):
            try:
                lines = p.read_text(encoding="latin-1", errors="replace").splitlines(keepends=True)
            except OSError as e:
                return err(_TOOL, path=str(p), msg=f"read lines failed — {type(e).__name__}: {e}")
        except OSError as e:
            return err(_TOOL, path=str(p), msg=f"read lines failed — {type(e).__name__}: {e}")

        total = len(lines)
        start = max(1, int(args.get("start_line") or 1))
        end = min(total, int(args.get("end_line") or total))

        if start > total:
            return err(_TOOL, path=str(p), msg=(
                f"read lines failed — start_line={start} exceeds file length ({total} lines). "
                f"Valid range: 1–{total}."
            ))

        content = "".join(lines[start - 1:end])
        content, truncated = truncate(content, max_chars)
        r = ok(_TOOL, path=str(p), FORMAT="text", CONTENT=content,
               LINE_COUNT=total, START_LINE=start, END_LINE=min(end, total))
        if truncated:
            r["TRUNCATED"] = True
            r["NOTE"] = (
                f"Content truncated at {max_chars} chars. "
                f"Read the next section with start_line={min(end, total) + 1}."
            )
        return r

    def _read_content(
        self, p: Path, args: Dict[str, Any],
        brain: Any = None, goal: str = "", on_progress: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        ext = p.suffix.lower()
        max_chars = min(int(args.get("max_chars") or MAX_CHARS), MAX_CHARS_HARD)

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
            try:
                size_info = f" ({human_size(p.stat().st_size)})"
            except OSError:
                size_info = ""
            return needs_confirm(
                _TOOL, str(p),
                f"'{p.name}'{size_info} is a binary file ({p.suffix or 'no extension'}) — cannot read as text. "
                "Call again with confirmed=true to open with the system application."
            )
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            elif sys.platform == "win32":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                import shutil as _shutil
                if not _shutil.which("xdg-open"):
                    return err(_TOOL, path=str(p), msg=(
                        "binary open failed — 'xdg-open' not found on this system. "
                        "Install it (e.g. 'sudo apt install xdg-utils') or open the file manually."
                    ))
                subprocess.Popen(["xdg-open", str(p)])
            return ok(_TOOL, path=str(p), FORMAT="binary", OPENED=True)
        except Exception as e:
            return err(_TOOL, path=str(p), msg=f"binary open failed — {type(e).__name__}: {e}")

    def _read_text(
        self, p: Path, args: Dict[str, Any], max_chars: int,
        brain: Any = None, goal: str = "", on_progress: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        encoding = str(args.get("encoding") or "utf-8")
        raw: Optional[str] = None

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
            size, modified = s.st_size, iso_time(s.st_mtime)
        except OSError:
            size, modified = None, None

        search = args.get("search_pattern")
        if search:
            return self._apply_search(str(p), raw, search, "text", max_chars, size, modified)

        try:
            from buddy.brain.text_reader import CHAR_THRESHOLD, maybe_read
            _threshold = getattr(brain, "char_threshold", CHAR_THRESHOLD)
            if brain and goal and len(raw) > _threshold:
                content = maybe_read(raw, goal, brain, on_progress)
                r = ok(_TOOL, path=str(p), FORMAT="text", CONTENT=content[:max_chars])
                if size is not None:
                    r["SIZE_BYTES"] = size
                return r
        except ImportError:
            pass

        content, truncated = truncate(raw, max_chars)
        r = ok(_TOOL, path=str(p), FORMAT="text", CONTENT=content)
        if size is not None:
            r["SIZE_BYTES"] = size
        if modified:
            r["MODIFIED"] = modified
        if truncated:
            r["TRUNCATED"] = True
            r["NOTE"] = (
                f"File is {human_size(size)} — showing first {max_chars} chars. "
                "Use start_line/end_line to read a specific section, or search_pattern to find specific text."
            )
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
                return err(_TOOL, path=str(p), msg=(
                    f"read tabular failed — none of columns {cols} found in '{p.name}'. "
                    f"Available columns: {all_cols}"
                ))
            df = df[valid]

        pq = args.get("pandas_query")
        if pq:
            try:
                df = df.query(pq)
            except Exception as e:
                return err(_TOOL, path=str(p), msg=(
                    f"read tabular failed — pandas_query '{pq}' raised: {e}. "
                    f"Available columns: {all_cols}"
                ))

        sp = args.get("search_pattern")
        if sp:
            try:
                mask = df.apply(
                    lambda row: row.astype(str).str.contains(sp, flags=re.IGNORECASE, regex=False, na=False).any(),
                    axis=1,
                )
                df = df[mask]
            except Exception as e:
                return err(_TOOL, path=str(p), msg=f"read tabular search_pattern '{sp}' failed: {e}")

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
                    f"'{p.name}' has {rows_after} rows × {len(df.columns)} cols. "
                    f"Rendered output ({len(rendered):,} chars) exceeds max_chars ({max_chars:,}). "
                    f"First 2 rows:\n{preview}\n"
                    "Add pandas_query to filter rows (e.g. pandas_query=\"score > 90\") or "
                    "columns=[...] to select specific columns, then call again."
                ),
                "NOTE": "Add a filter (pandas_query or columns) to reduce output, then call again.",
            }
            if sheet_used:
                r["SHEET"] = sheet_used
            return r

        r2 = ok(_TOOL, path=str(p), FORMAT="table", CONTENT=rendered,
                ROWS_TOTAL=rows_total, COLUMNS=all_cols)
        if rows_after < rows_total:
            r2["ROWS_AFTER_FILTER"] = rows_after
        if sheet_used:
            r2["SHEET"] = sheet_used
        return r2

    def _render_df(self, df: Any) -> str:
        try:
            cols = list(df.columns)
            col_w = {c: max(len(str(c)), int(df[c].astype(str).str.len().max() or 0)) for c in cols}
            header = " | ".join(str(c).ljust(col_w[c]) for c in cols)
            sep = "-+-".join("-" * col_w[c] for c in cols)
            rows = [
                " | ".join(str(v).ljust(col_w[c]) for c, v in zip(cols, row))
                for row in df.itertuples(index=False, name=None)
            ]
            return "\n".join([header, sep] + rows)
        except Exception:
            return str(df)

    def _read_pdf(
        self, p: Path, args: Dict[str, Any], max_chars: int,
        brain: Any = None, goal: str = "", on_progress: Optional[Callable] = None,
    ) -> Optional[Dict[str, Any]]:
        text: Optional[str] = None
        extract_error: Optional[str] = None

        for extractor in [self._pdf_pdfplumber, self._pdf_pypdf2]:
            try:
                text = extractor(p)
                if text is not None:
                    break
            except Exception as e:
                extract_error = f"{type(e).__name__}: {e}"

        if text is None:
            if extract_error:
                return err(_TOOL, path=str(p), msg=(
                    f"read pdf failed — could not extract text from '{p.name}': {extract_error}. "
                    "The file may be corrupted, encrypted, or require a different PDF library."
                ))
            return None

        if not text:
            r = ok(_TOOL, path=str(p), FORMAT="pdf", CONTENT="")
            r["NOTE"] = (
                f"'{p.name}' has no extractable text — likely a scanned image-only PDF. "
                "Use the vision tool to analyze scanned documents."
            )
            return r

        return self._finalize_doc(str(p), text, "pdf", args, max_chars, brain=brain, goal=goal, on_progress=on_progress)

    def _pdf_pdfplumber(self, p: Path) -> Optional[str]:
        try:
            import pdfplumber
            with pdfplumber.open(str(p)) as pdf:
                return "\n\n".join(pg.extract_text() or "" for pg in pdf.pages).strip()
        except ImportError:
            return None
        except Exception:
            return None

    def _pdf_pypdf2(self, p: Path) -> Optional[str]:
        try:
            import PyPDF2
            with open(p, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                return "\n\n".join(reader.pages[i].extract_text() or "" for i in range(len(reader.pages))).strip()
        except ImportError:
            return None
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
        except ImportError:
            return None
        except Exception as e:
            return err(_TOOL, path=str(p), msg=(
                f"read docx failed — could not open '{p.name}': {type(e).__name__}: {e}. "
                "The file may be corrupted or password-protected."
            ))

        if not text:
            r = ok(_TOOL, path=str(p), FORMAT="docx", CONTENT="")
            r["NOTE"] = f"'{p.name}' opened successfully but contains no text paragraphs."
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
            _threshold = getattr(brain, "char_threshold", CHAR_THRESHOLD)
            if brain and goal and len(text) > _threshold:
                content = maybe_read(text, goal, brain, on_progress)
                return ok(_TOOL, path=path, FORMAT=fmt, CONTENT=content[:max_chars])
        except ImportError:
            pass

        content, truncated = truncate(text, max_chars)
        r = ok(_TOOL, path=path, FORMAT=fmt, CONTENT=content)
        if truncated:
            r["TRUNCATED"] = True
            r["NOTE"] = (
                f"{fmt.upper()} content truncated at {max_chars} chars. "
                "Use search_pattern to find specific content instead of reading the whole document."
            )
        return r

    def _apply_search(
        self, path: str, text: str, pattern: str, fmt: str,
        max_chars: int, size: Optional[int] = None, modified: Optional[str] = None,
    ) -> Dict[str, Any]:
        matcher = make_matcher(pattern, case_sensitive=False, use_regex=False)
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
        content, truncated = truncate(content, max_chars)
        r = ok(_TOOL, path=path, FORMAT=fmt, CONTENT=content, TOTAL_FOUND=len(matched))
        if size is not None:
            r["SIZE_BYTES"] = size
        if modified:
            r["MODIFIED"] = modified
        if not matched:
            r["NOTE"] = f"search_pattern '{pattern}' matched 0 lines in this file."
        elif truncated:
            r["TRUNCATED"] = True
        return r

    # ── diff ────────────────────────────────────────────────────────────────

    def _diff(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw_a = str(args.get("path_a") or "").strip()
        raw_b = str(args.get("path_b") or "").strip()

        if not raw_a:
            return err(_TOOL, msg="diff requires 'path_a' — absolute path to the first file.")
        if not raw_b:
            return err(_TOOL, msg="diff requires 'path_b' — absolute path to the second file.")

        path_a, path_b = resolve_path(raw_a), resolve_path(raw_b)
        pa, pb = Path(path_a), Path(path_b)

        if not pa.exists():
            return err(_TOOL, path=path_a, msg=f"diff failed — path_a '{path_a}' does not exist.")
        if not pb.exists():
            return err(_TOOL, path=path_b, msg=f"diff failed — path_b '{path_b}' does not exist.")
        if not pa.is_file():
            return err(_TOOL, path=path_a, msg=f"diff failed — path_a '{path_a}' is a directory, not a file.")
        if not pb.is_file():
            return err(_TOOL, path=path_b, msg=f"diff failed — path_b '{path_b}' is a directory, not a file.")
        if is_binary(pa):
            return err(_TOOL, path=path_a, msg=(
                f"diff failed — '{pa.name}' is a binary file ({pa.suffix}). "
                "diff only works on plain text files."
            ))
        if is_binary(pb):
            return err(_TOOL, path=path_b, msg=(
                f"diff failed — '{pb.name}' is a binary file ({pb.suffix}). "
                "diff only works on plain text files."
            ))

        try:
            import difflib
            a = pa.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            b = pb.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            diff = "".join(difflib.unified_diff(a, b, fromfile=path_a, tofile=path_b))
            diff_text, truncated = truncate(diff or "(files are identical)", MAX_CHARS)
            r = ok(_TOOL, PATH_A=path_a, PATH_B=path_b, DIFF=diff_text)
            if truncated:
                r["TRUNCATED"] = True
                r["NOTE"] = f"Diff output truncated at {MAX_CHARS} chars — files differ in many places."
            return r
        except Exception as e:
            return err(_TOOL, msg=f"diff failed — {type(e).__name__}: {e}")


TOOL_CLASS = FsRead


def get_tool() -> FsRead:
    return FsRead()
