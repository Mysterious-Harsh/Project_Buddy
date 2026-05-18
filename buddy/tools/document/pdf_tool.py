from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from buddy.prompts.pdf_prompts import PDF_TOOL_PROMPT
from buddy.tools.document.document_utils import (
    apply_edits,
    extract_pdf_to_html,
    search_html,
    stamp_ids,
)

_TOOL = "pdf"

# Design note — HTML sidecar:
# PDFs are not natively editable. This tool keeps a  <name>.html  sidecar
# next to every PDF it creates. The sidecar is the source of truth for
# read/edit; extracting from the PDF is only a lossy fallback for PDFs that
# were not created by this tool.


# ── private utilities ─────────────────────────────────────────────────────────


def _resolve(raw: str) -> str:
    p = os.path.expanduser(os.path.expandvars(raw.strip()))
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser("~"), p)
    return p


def _ok(**kw: Any) -> Dict[str, Any]:
    return {"STATUS": "success", "TOOL": _TOOL, **kw}


def _err(msg: str, **kw: Any) -> Dict[str, Any]:
    return {"STATUS": "failed", "TOOL": _TOOL, "ERROR": msg, **kw}


def _needs_confirm(preview: str, **kw: Any) -> Dict[str, Any]:
    # NOT "failed" — the AI must be able to distinguish a confirmation prompt
    # from a real error so it knows to retry with confirmed=true.
    return {
        "STATUS": "needs_confirmation",
        "TOOL": _TOOL,
        "NEEDS_CONFIRMATION": True,
        "PREVIEW": preview,
        "NOTE": "Call again with confirmed=true after user approves.",
        **kw,
    }


def _sidecar(pdf_path: str) -> Path:
    """Return the .html sidecar path that lives alongside a PDF."""
    return Path(pdf_path).with_suffix(".html")


def _html_to_pdf(html: str, target: str) -> Optional[str]:
    """
    Render an HTML string to a PDF file via weasyprint.
    Returns an error message string on failure, or None on success.
    """
    try:
        from weasyprint import HTML
    except ImportError:
        return "weasyprint not installed. Run: pip install weasyprint"
    try:
        HTML(string=html).write_pdf(target)
        return None
    except Exception as exc:
        return f"weasyprint render failed: {exc}"


def _page_count(path: str) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(path).pages)
    except Exception:
        return 0


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except Exception:
        pass


def _restore_backup(backup: str, original: str) -> None:
    try:
        shutil.copy2(backup, original)
    except Exception:
        pass
    _safe_remove(backup)


def _get_html_source(path: str) -> Tuple[str, bool]:
    """
    Retrieve the HTML source for a PDF.
    Returns (html_string, from_sidecar).

    Prefers the .html sidecar (lossless, the original authored source).
    Falls back to extracting from the PDF itself (lossy — use only when no
    sidecar is present, e.g. for PDFs not created by this tool).
    """
    sc = _sidecar(path)
    if sc.exists():
        return sc.read_text(encoding="utf-8"), True
    return extract_pdf_to_html(path), False


def _decrypt_reader(reader: Any, password: str = "") -> bool:
    """Try to decrypt an encrypted PdfReader. Returns True if successful."""
    try:
        return bool(reader.decrypt(password))
    except Exception:
        return False


# ── tool ──────────────────────────────────────────────────────────────────────


class PdfTool:
    tool_name = _TOOL
    version = "1.1.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "WHEN: creating, editing, or merging .pdf files.\n\nFUNCTIONS:\n "
                " create(path, content)        — HTML+CSS string → .pdf; saves .html"
                " source alongside for future edits\n  read(path, search?)          —"
                " returns full HTML source with section IDs stamped (id='s1','s2',...);"
                " search= returns only matching sections\n  edit(path, edits[])        "
                "  — patch HTML and re-render: {section_id+new} replace, {old+new} text"
                " patch, {op:add_after/add_before/add_end+new} insert,"
                " {op:remove+section_id} delete\n  merge(sources[], target)     — merge"
                " multiple .pdf files into one in order\n\nCHAIN: always call read"
                " before edit to get current section IDs — IDs change after every edit."
                " merge requires all source paths confirmed to exist first (use"
                " fs_browse.find if unsure).\nNOT: .xlsx → excel | .docx → word | plain"
                " file reads → fs_read | plain file writes → fs_write | convert →"
                " converter"
            ),
            "prompt": PDF_TOOL_PROMPT,
        }

    # ── dispatch ──────────────────────────────────────────────────────────────

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
        handlers: Dict[str, Any] = {
            "create": self._create,
            "read": self._read,
            "edit": self._edit,
            "merge": self._merge,
            "split": self._split,
            "extract_pages": self._extract_pages,
            "remove_page": self._remove_page,
            "rotate": self._rotate,
            "watermark": self._watermark,
            "protect": self._protect,
            "metadata": self._metadata,
            "compress": self._compress,
        }
        if fn not in handlers:
            return _err(
                f"Unknown function: {function!r}. Must be one of: {', '.join(handlers)}"
            )
        # All handlers do blocking I/O — run in a thread to avoid stalling
        # the event loop.
        return await asyncio.to_thread(handlers[fn], arguments)

    # ── create ────────────────────────────────────────────────────────────────

    def _create(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err(
                "pdf.create: 'path' is required — provide an absolute .pdf path"
            )
        if not raw.lower().endswith(".pdf"):
            return _err("pdf.create: path must end in .pdf")

        content = str(args.get("content") or "").strip()
        if not content:
            return _err(
                "pdf.create: 'content' is required — provide an HTML+CSS string"
            )

        path = _resolve(raw)
        p = Path(path)

        if p.exists() and not args.get("confirmed"):
            return _needs_confirm(
                f"File already exists: {path}\nPass confirmed=true to overwrite it."
            )

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            stamped = stamp_ids(content)

            # Write sidecar first — it is the source of truth for future edits.
            # If PDF rendering later fails, the sidecar is cleaned up.
            sc = _sidecar(path)
            sc.write_text(stamped, encoding="utf-8")

            err = _html_to_pdf(stamped, path)
            if err:
                _safe_remove(str(sc))
                return _err(err)

            return _ok(
                PATH=str(p),
                SIDECAR=str(sc),
                PAGES=_page_count(path),
                SIZE_BYTES=p.stat().st_size,
            )
        except PermissionError:
            return _err(f"Permission denied: {path}")
        except Exception as exc:
            return _err(f"Failed to create PDF: {exc}")

    # ── read ──────────────────────────────────────────────────────────────────

    def _read(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("pdf.read: 'path' is required — provide an absolute .pdf path")

        path = _resolve(raw)
        p = Path(path)
        if not p.exists():
            return _err(f"pdf.read: file not found: {path}")

        search = str(args.get("search") or "").strip() or None

        try:
            html, from_sidecar = _get_html_source(path)
        except ImportError as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(f"Failed to read PDF content: {exc}")

        if search:
            matched = search_html(html, search)
            if not matched:
                return _ok(
                    PATH=str(p),
                    SEARCH=search,
                    HTML="",
                    NOTE="No sections matched the search query.",
                )
            html = matched

        out = _ok(PATH=str(p), HTML=html, PAGES=_page_count(path))
        if not from_sidecar:
            out["NOTE"] = (
                "No .html sidecar found — HTML was extracted from the PDF directly. "
                "Formatting may be approximate. "
                "For faithful edits, recreate this PDF using pdf.create."
            )
        return out

    # ── edit ──────────────────────────────────────────────────────────────────

    def _edit(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("pdf.edit: 'path' is required — provide an absolute .pdf path")

        edits = args.get("edits")
        if not edits or not isinstance(edits, list):
            return _err("pdf.edit: 'edits' must be a non-empty list")

        path = _resolve(raw)
        p = Path(path)
        if not p.exists():
            return _err(f"pdf.edit: file not found: {path}")

        # Backup the PDF before any changes — restored on any failure.
        backup = path + ".bak"
        try:
            shutil.copy2(path, backup)
        except Exception as exc:
            return _err(f"Could not create backup before editing: {exc}")

        try:
            html, from_sidecar = _get_html_source(path)
        except ImportError as exc:
            _safe_remove(backup)
            return _err(str(exc))
        except Exception as exc:
            _safe_remove(backup)
            return _err(f"Failed to read PDF content: {exc}")

        warnings: List[str] = []
        if not from_sidecar:
            warnings.append(
                "No .html sidecar — HTML extracted from PDF. "
                "Formatting fidelity may be reduced. "
                "Recreate with pdf.create for best results."
            )

        new_html, applied, failed = apply_edits(html, edits)

        # Atomic: if any edit failed, restore and report — nothing is written.
        if failed:
            _safe_remove(backup)
            out: Dict[str, Any] = {
                "STATUS": "failed",
                "TOOL": _TOOL,
                "PATH": str(p),
                "EDITS_APPLIED": 0,
                "EDITS_FAILED": len(failed),
                "FAILED": failed,
                "ERROR": (
                    f"{len(failed)} edit(s) failed — no changes were saved. "
                    "Fix the errors and retry."
                ),
            }
            if warnings:
                out["WARNINGS"] = warnings
            return out

        # Re-render PDF
        err = _html_to_pdf(new_html, path)
        if err:
            _restore_backup(backup, path)
            return _err(f"Re-render failed (original restored): {err}")

        # Update the sidecar to match the new HTML
        try:
            _sidecar(path).write_text(new_html, encoding="utf-8")
        except Exception as exc:
            warnings.append(f"PDF updated but sidecar could not be saved: {exc}")

        _safe_remove(backup)

        out = _ok(
            PATH=str(p),
            EDITS_APPLIED=len(applied),
            APPLIED=applied,
            PAGES=_page_count(path),
            # NOTE: HTML is intentionally NOT returned here — it would waste
            # the AI's context window. Call read() again if you need the updated HTML.
        )
        if warnings:
            out["WARNINGS"] = warnings
        return out

    # ── merge ─────────────────────────────────────────────────────────────────

    def _merge(self, args: Dict[str, Any]) -> Dict[str, Any]:
        sources = args.get("sources")
        if not sources or not isinstance(sources, list):
            return _err("pdf.merge: 'sources' must be a non-empty list of .pdf paths")

        raw_tgt = str(args.get("target") or "").strip()
        if not raw_tgt:
            return _err(
                "pdf.merge: 'target' is required — provide an absolute .pdf path"
            )
        if not raw_tgt.lower().endswith(".pdf"):
            return _err("pdf.merge: 'target' must end in .pdf")

        target = _resolve(raw_tgt)
        tgt_p = Path(target)

        if tgt_p.exists() and not args.get("confirmed"):
            return _needs_confirm(
                f"Target already exists: {target}\nPass confirmed=true to overwrite it."
            )

        resolved = [_resolve(str(s)) for s in sources]
        missing = [s for s in resolved if not Path(s).exists()]
        if missing:
            return _err("Source file(s) not found.", MISSING_FILES=missing)

        # passwords={"/abs/path/to/file.pdf": "secret"} for encrypted sources
        passwords: Dict[str, str] = args.get("passwords") or {}

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        try:
            writer = PdfWriter()
            total_pages = 0
            source_errors: List[Dict] = []

            for src in resolved:
                try:
                    reader = PdfReader(src)
                    if reader.is_encrypted:
                        pw = passwords.get(src, "")
                        if not _decrypt_reader(reader, pw):
                            source_errors.append({
                                "file": src,
                                "error": (
                                    "Encrypted — provide its password via "
                                    f'passwords={{"{src}": "<password>"}}'
                                ),
                            })
                            continue
                    for page in reader.pages:
                        writer.add_page(page)
                    total_pages += len(reader.pages)
                except Exception as exc:
                    source_errors.append({"file": src, "error": str(exc)})

            if not total_pages:
                return _err(
                    "No pages could be read from any source file.",
                    ERRORS=source_errors,
                )

            tgt_p.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as f:
                writer.write(f)

            out = _ok(
                TARGET=target,
                SOURCES_MERGED=len(resolved) - len(source_errors),
                SOURCES_SKIPPED=len(source_errors),
                PAGES_TOTAL=total_pages,
                SIZE_BYTES=tgt_p.stat().st_size,
            )
            if source_errors:
                out["WARNINGS"] = [f"{e['file']}: {e['error']}" for e in source_errors]
            return out

        except PermissionError:
            return _err(f"Permission denied: {target}")
        except Exception as exc:
            return _err(f"Merge failed: {exc}")

    # ── split ─────────────────────────────────────────────────────────────────

    def _split(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Split a PDF into separate files.
        Without  ranges: one file per page → target_dir/page_001.pdf …
        With     ranges: [[1,3],[4,6]] → target_dir/part_1.pdf, part_2.pdf …
        Page numbers are 1-based inclusive.
        """
        raw = str(args.get("path") or "").strip()
        raw_dir = str(args.get("target_dir") or "").strip()
        if not raw:
            return _err("pdf.split: 'path' is required")
        if not raw_dir:
            return _err("pdf.split: 'target_dir' is required — absolute directory path")

        path = _resolve(raw)
        target_dir = Path(_resolve(raw_dir))
        if not Path(path).exists():
            return _err(f"pdf.split: file not found: {path}")

        ranges: Optional[List[List[int]]] = args.get("ranges")

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                return _err(
                    "pdf.split: file is encrypted — decrypt it first with"
                    " pdf.protect(remove=true)"
                )

            n = len(reader.pages)
            target_dir.mkdir(parents=True, exist_ok=True)
            created: List[str] = []

            if ranges:
                for i, pair in enumerate(ranges, 1):
                    if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                        return _err(
                            f"pdf.split: ranges[{i-1}] must be [start, end] (1-based),"
                            f" got {pair!r}"
                        )
                    start, end = int(pair[0]), int(pair[1])
                    if start < 1 or end > n or start > end:
                        return _err(
                            f"pdf.split: invalid range [{start},{end}] — "
                            f"document has {n} pages (1-based)"
                        )
                    w = PdfWriter()
                    for pg in range(start - 1, end):
                        w.add_page(reader.pages[pg])
                    out_path = target_dir / f"part_{i}.pdf"
                    with open(out_path, "wb") as f:
                        w.write(f)
                    created.append(str(out_path))
            else:
                for i, page in enumerate(reader.pages, 1):
                    w = PdfWriter()
                    w.add_page(page)
                    out_path = target_dir / f"page_{i:03d}.pdf"
                    with open(out_path, "wb") as f:
                        w.write(f)
                    created.append(str(out_path))

            return _ok(
                SOURCE=path,
                TARGET_DIR=str(target_dir),
                FILES_CREATED=len(created),
                PAGES_TOTAL=n,
                FILES=created,
            )
        except PermissionError:
            return _err(f"Permission denied: {target_dir}")
        except Exception as exc:
            return _err(f"Split failed: {exc}")

    # ── extract_pages ─────────────────────────────────────────────────────────

    def _extract_pages(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Pull specific pages (1-based list) into a new PDF."""
        raw = str(args.get("path") or "").strip()
        target_raw = str(args.get("target") or "").strip()
        pages = args.get("pages")

        if not raw:
            return _err("pdf.extract_pages: 'path' is required")
        if not target_raw:
            return _err("pdf.extract_pages: 'target' is required")
        if not pages or not isinstance(pages, list):
            return _err(
                "pdf.extract_pages: 'pages' must be a non-empty list of "
                "page numbers (1-based)"
            )

        path = _resolve(raw)
        target_path = _resolve(target_raw)

        if not Path(path).exists():
            return _err(f"pdf.extract_pages: file not found: {path}")

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        try:
            reader = PdfReader(path)
            n = len(reader.pages)
            invalid = [
                pg for pg in pages if not isinstance(pg, int) or not (1 <= pg <= n)
            ]
            if invalid:
                return _err(
                    f"pdf.extract_pages: invalid page number(s): {invalid}. "
                    f"Document has {n} pages (1-based)."
                )

            writer = PdfWriter()
            for pg in pages:
                writer.add_page(reader.pages[pg - 1])

            Path(target_path).parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "wb") as f:
                writer.write(f)

            return _ok(
                SOURCE=path,
                TARGET=target_path,
                PAGES_EXTRACTED=len(pages),
                SIZE_BYTES=Path(target_path).stat().st_size,
            )
        except PermissionError:
            return _err(f"Permission denied: {target_path}")
        except Exception as exc:
            return _err(f"Extract pages failed: {exc}")

    # ── remove_page ───────────────────────────────────────────────────────────

    def _remove_page(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Delete one or more pages from a PDF in place (1-based page numbers)."""
        raw = str(args.get("path") or "").strip()
        pages = args.get("pages")

        if not raw:
            return _err("pdf.remove_page: 'path' is required")
        if not pages or not isinstance(pages, list):
            return _err(
                "pdf.remove_page: 'pages' must be a non-empty list of page numbers"
                " (1-based)"
            )

        path = _resolve(raw)
        p = Path(path)
        if not p.exists():
            return _err(f"pdf.remove_page: file not found: {path}")

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        backup = path + ".bak"
        try:
            shutil.copy2(path, backup)
        except Exception as exc:
            return _err(f"Could not create backup: {exc}")

        try:
            reader = PdfReader(path)
            n = len(reader.pages)

            invalid = [
                pg for pg in pages if not isinstance(pg, int) or not (1 <= pg <= n)
            ]
            if invalid:
                _safe_remove(backup)
                return _err(
                    f"pdf.remove_page: invalid page number(s): {invalid}. "
                    f"Document has {n} pages (1-based)."
                )
            if len(pages) >= n:
                _safe_remove(backup)
                return _err("pdf.remove_page: cannot remove all pages from a PDF.")

            to_remove = set(pages)
            writer = PdfWriter()
            for i, page in enumerate(reader.pages, 1):
                if i not in to_remove:
                    writer.add_page(page)

            with open(path, "wb") as f:
                writer.write(f)

            _safe_remove(backup)
            return _ok(
                PATH=path,
                PAGES_REMOVED=len(pages),
                PAGES_REMAINING=n - len(pages),
            )
        except Exception as exc:
            _restore_backup(backup, path)
            return _err(f"Remove page failed (original restored): {exc}")

    # ── rotate ────────────────────────────────────────────────────────────────

    def _rotate(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Rotate all or specific pages.
        degrees: 90 / 180 / 270 (positive = clockwise).
        pages: optional list of 1-based page numbers; omit to rotate all.
        target: optional output path; omit to modify in place.
        """
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("pdf.rotate: 'path' is required")

        degrees = args.get("degrees")
        if degrees not in (90, 180, 270, -90, -180, -270):
            return _err(
                "pdf.rotate: 'degrees' must be 90, 180, or 270 "
                "(positive = clockwise, negative = counter-clockwise)"
            )

        pages_arg: Optional[List[int]] = args.get("pages")
        raw_target = str(args.get("target") or raw).strip()

        path = _resolve(raw)
        target_path = _resolve(raw_target)

        if not Path(path).exists():
            return _err(f"pdf.rotate: file not found: {path}")

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        backup = path + ".bak"
        try:
            shutil.copy2(path, backup)
        except Exception as exc:
            return _err(f"Could not create backup: {exc}")

        try:
            reader = PdfReader(path)
            n = len(reader.pages)

            if pages_arg:
                invalid = [pg for pg in pages_arg if not (1 <= pg <= n)]
                if invalid:
                    _safe_remove(backup)
                    return _err(
                        f"pdf.rotate: invalid page number(s): {invalid}. "
                        f"Document has {n} pages."
                    )

            writer = PdfWriter()
            pages_rotated = 0
            for i, page in enumerate(reader.pages, 1):
                if pages_arg is None or i in pages_arg:
                    page.rotate(degrees)
                    pages_rotated += 1
                writer.add_page(page)

            Path(target_path).parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "wb") as f:
                writer.write(f)

            _safe_remove(backup)
            return _ok(
                PATH=target_path,
                PAGES_ROTATED=pages_rotated,
                DEGREES=degrees,
            )
        except Exception as exc:
            _restore_backup(backup, path)
            return _err(f"Rotate failed (original restored): {exc}")

    # ── watermark ─────────────────────────────────────────────────────────────

    def _watermark(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Overlay a diagonal text watermark on every page.
        Uses weasyprint to render the watermark to a single-page PDF,
        then merges it onto each page of the source via pypdf.

        text        — watermark label (required)
        target      — output path; defaults to in-place overwrite
        opacity     — 0.0–1.0, default 0.25
        color       — CSS hex colour, default "#aaaaaa"
        font_size   — pt, default 48
        """
        raw = str(args.get("path") or "").strip()
        text = str(args.get("text") or "").strip()

        if not raw:
            return _err("pdf.watermark: 'path' is required")
        if not text:
            return _err("pdf.watermark: 'text' is required")

        raw_target = str(args.get("target") or raw).strip()
        opacity = max(0.0, min(1.0, float(args.get("opacity") or 0.25)))
        color = str(args.get("color") or "#aaaaaa")
        font_size = int(args.get("font_size") or 48)

        path = _resolve(raw)
        target_path = _resolve(raw_target)

        if not Path(path).exists():
            return _err(f"pdf.watermark: file not found: {path}")

        # Render a single-page watermark PDF using weasyprint
        wm_html = f"""<!DOCTYPE html>
<html>
<head>
<style>
  @page {{ margin: 0; }}
  body  {{ margin: 0; padding: 0; width: 210mm; height: 297mm;
           position: relative; overflow: hidden; }}
  .wm  {{
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%) rotate(-45deg);
    font-size: {font_size}pt;
    color: {color};
    opacity: {opacity};
    white-space: nowrap;
    user-select: none;
  }}
</style>
</head>
<body><div class="wm">{text}</div></body>
</html>"""

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        tmp_wm = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp_wm.close()
        try:
            err = _html_to_pdf(wm_html, tmp_wm.name)
            if err:
                return _err(f"Could not render watermark page: {err}")

            reader = PdfReader(path)
            wm_reader = PdfReader(tmp_wm.name)
            wm_page = wm_reader.pages[0]
            writer = PdfWriter()

            for page in reader.pages:
                page.merge_page(wm_page)
                writer.add_page(page)

            Path(target_path).parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "wb") as f:
                writer.write(f)

            return _ok(
                PATH=target_path,
                WATERMARK_TEXT=text,
                PAGES=len(reader.pages),
                SIZE_BYTES=Path(target_path).stat().st_size,
            )
        except PermissionError:
            return _err(f"Permission denied: {target_path}")
        except Exception as exc:
            return _err(f"Watermark failed: {exc}")
        finally:
            _safe_remove(tmp_wm.name)

    # ── protect ───────────────────────────────────────────────────────────────

    def _protect(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Add or remove password protection.

        To protect:    password="secret"
        To decrypt:    remove=true, current_password="secret"
        owner_password sets the owner (permissions) password; defaults to password.
        target: optional output path; defaults to in-place overwrite.
        """
        raw = str(args.get("path") or "").strip()
        password = str(args.get("password") or "").strip()
        remove = bool(args.get("remove") or False)
        owner_pw = str(args.get("owner_password") or password).strip()
        curr_pw = str(args.get("current_password") or "").strip()
        raw_tgt = str(args.get("target") or raw).strip()

        if not raw:
            return _err("pdf.protect: 'path' is required")
        if not remove and not password:
            return _err(
                "pdf.protect: 'password' is required (or set remove=true to decrypt)"
            )

        path = _resolve(raw)
        target_path = _resolve(raw_tgt)

        if not Path(path).exists():
            return _err(f"pdf.protect: file not found: {path}")

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                if not _decrypt_reader(reader, curr_pw):
                    return _err(
                        "pdf.protect: file is encrypted — provide 'current_password' to"
                        " unlock it"
                    )

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            if reader.metadata:
                writer.add_metadata(dict(reader.metadata))

            if not remove:
                writer.encrypt(
                    user_password=password,
                    owner_password=owner_pw,
                    use_128bit=True,
                )

            Path(target_path).parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "wb") as f:
                writer.write(f)

            return _ok(
                PATH=target_path,
                PROTECTED=not remove,
                SIZE_BYTES=Path(target_path).stat().st_size,
            )
        except PermissionError:
            return _err(f"Permission denied: {target_path}")
        except Exception as exc:
            return _err(f"Protect failed: {exc}")

    # ── metadata ──────────────────────────────────────────────────────────────

    def _metadata(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Read or update PDF metadata.

        To read:   metadata(path)
        To update: metadata(path, set={"Title":"…","Author":"…","Subject":"…","Keywords":"…"})
        Keys are case-insensitive; leading "/" is optional (added automatically).
        """
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("pdf.metadata: 'path' is required")

        path = _resolve(raw)
        if not Path(path).exists():
            return _err(f"pdf.metadata: file not found: {path}")

        set_meta: Optional[Dict[str, str]] = args.get("set")

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        try:
            reader = PdfReader(path)
            existing: Dict[str, str] = dict(reader.metadata or {})

            if not set_meta:
                return _ok(
                    PATH=path,
                    METADATA=existing,
                    PAGES=len(reader.pages),
                )

            # Normalise keys: "Title" → "/Title"
            normalised = {
                (k if k.startswith("/") else f"/{k.strip()}"): v
                for k, v in set_meta.items()
            }

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            writer.add_metadata({**existing, **normalised})

            with open(path, "wb") as f:
                writer.write(f)

            return _ok(PATH=path, METADATA_UPDATED=normalised)
        except PermissionError:
            return _err(f"Permission denied: {path}")
        except Exception as exc:
            return _err(f"Metadata operation failed: {exc}")

    # ── compress ──────────────────────────────────────────────────────────────

    def _compress(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Reduce PDF file size by compressing content streams.
        target: optional output path; defaults to in-place overwrite.
        Note: results vary — content-heavy PDFs compress well; already-compressed
        ones may see little or no reduction.
        """
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("pdf.compress: 'path' is required")

        raw_target = str(args.get("target") or raw).strip()
        path = _resolve(raw)
        target_path = _resolve(raw_target)

        if not Path(path).exists():
            return _err(f"pdf.compress: file not found: {path}")

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        original_size = Path(path).stat().st_size

        try:
            reader = PdfReader(path)
            writer = PdfWriter()
            for page in reader.pages:
                page.compress_content_streams()
                writer.add_page(page)
            if reader.metadata:
                writer.add_metadata(dict(reader.metadata))

            Path(target_path).parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "wb") as f:
                writer.write(f)

            new_size = Path(target_path).stat().st_size
            saved = original_size - new_size
            saved_pct = round(saved / original_size * 100, 1) if original_size else 0.0

            return _ok(
                PATH=target_path,
                ORIGINAL_SIZE_BYTES=original_size,
                COMPRESSED_SIZE_BYTES=new_size,
                SAVED_BYTES=saved,
                SAVED_PERCENT=saved_pct,
                NOTE=(
                    "File size increased slightly — PDF was already well-compressed."
                    if saved < 0
                    else None
                ),
            )
        except PermissionError:
            return _err(f"Permission denied: {target_path}")
        except Exception as exc:
            return _err(f"Compress failed: {exc}")


# ── registry ──────────────────────────────────────────────────────────────────

TOOL_NAME = "pdf"
TOOL_CLASS = PdfTool


def get_tool() -> PdfTool:
    return PdfTool()
