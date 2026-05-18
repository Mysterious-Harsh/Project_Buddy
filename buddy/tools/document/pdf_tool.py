from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from buddy.prompts.pdf_prompts import PDF_TOOL_PROMPT
from buddy.tools.document.document_utils import (
    apply_edits,
    extract_pdf_to_html,
    html_source_path,
    load_html_source,
    save_html_source,
    search_html,
    stamp_ids,
)

_TOOL = "pdf"


# ── helpers ───────────────────────────────────────────────────────────────────

def _resolve(raw: str) -> str:
    import os
    p = os.path.expanduser(os.path.expandvars(raw.strip()))
    if not os.path.isabs(p):
        p = os.path.join(os.path.expanduser("~"), p)
    return p


def _ok(**kw: Any) -> Dict[str, Any]:
    return {"STATUS": "success", **kw}


def _err(msg: str, **kw: Any) -> Dict[str, Any]:
    return {"STATUS": "failed", "TOOL": _TOOL, "ERROR": msg, **kw}


def _needs_confirm(preview: str, **kw: Any) -> Dict[str, Any]:
    return {
        "STATUS": "failed", "TOOL": _TOOL,
        "NEEDS_CONFIRMATION": True,
        "PREVIEW": preview,
        "NOTE": "Call again with confirmed=true after user approves.",
        **kw,
    }


def _html_to_pdf(html: str, target: str) -> Optional[str]:
    """Render HTML string to PDF via weasyprint. Returns error string or None on success."""
    try:
        from weasyprint import HTML
    except ImportError:
        return "weasyprint not installed. Run: pip install weasyprint"
    try:
        HTML(string=html).write_pdf(target)
        return None
    except Exception as e:
        return f"weasyprint render failed: {e}"


def _page_count(path: str) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        return 0


# ── tool ──────────────────────────────────────────────────────────────────────

class PdfTool:
    tool_name = _TOOL
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "WHEN: creating, editing, or merging .pdf files.\n\n"
                "FUNCTIONS:\n"
                "  create(path, content)        — HTML+CSS string → .pdf; saves .html source alongside for future edits\n"
                "  read(path, search?)          — returns full HTML source with section IDs stamped (id='s1','s2',...); search= returns only matching sections\n"
                "  edit(path, edits[])          — patch HTML and re-render: {section_id+new} replace, {old+new} text patch, {op:add_after/add_before/add_end+new} insert, {op:remove+section_id} delete\n"
                "  merge(sources[], target)     — merge multiple .pdf files into one in order\n\n"
                "CHAIN: always call read before edit to get current section IDs — IDs change after every edit. "
                "merge requires all source paths confirmed to exist first (use fs_browse.find if unsure).\n"
                "NOT: .xlsx → excel | .docx → word | plain file reads → fs_read | plain file writes → fs_write | convert → converter"
            ),
            "prompt": PDF_TOOL_PROMPT,
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
        if fn == "create":
            return self._create(arguments)
        if fn == "read":
            return self._read(arguments)
        if fn == "edit":
            return self._edit(arguments)
        if fn == "merge":
            return self._merge(arguments)
        return _err(f"Unknown function: {function!r}. Must be: create, read, edit, merge")

    # ── create ────────────────────────────────────────────────────────────────

    def _create(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("pdf.create: 'path' is required — provide an absolute .pdf path")
        if not raw.lower().endswith(".pdf"):
            return _err("pdf.create: path must end in .pdf")

        content = str(args.get("content") or "").strip()
        if not content:
            return _err("content is required — provide an HTML+CSS string")

        path = _resolve(raw)
        p = Path(path)

        if p.exists() and not args.get("confirmed"):
            return _needs_confirm(f"File already exists: {path}\nSetting confirmed=true will overwrite it.")

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            stamped = stamp_ids(content)
            err = _html_to_pdf(stamped, path)
            if err:
                return _err(err)
            return _ok(
                PATH=str(p),
                PAGES=_page_count(path),
                SIZE_BYTES=p.stat().st_size,
            )
        except PermissionError:
            return _err(f"Permission denied: {path}")
        except Exception as e:
            return _err(f"Failed to create PDF: {e}")

    # ── read ──────────────────────────────────────────────────────────────────

    def _read(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("pdf.read: 'path' is required — provide an absolute .pdf path")

        path = _resolve(raw)
        p = Path(path)
        if not p.exists():
            return _err(f"pdf.read: file not found: {path} — use fs_browse.find to locate it")

        search = str(args.get("search") or "").strip() or None

        try:
            html = extract_pdf_to_html(path)
        except ImportError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Failed to extract PDF content: {e}")

        if search:
            html = search_html(html, search)
            if not html:
                return _ok(PATH=str(p), SEARCH=search, HTML="", NOTE="No sections matched the search query.")

        return _ok(PATH=str(p), HTML=html)

    # ── edit ──────────────────────────────────────────────────────────────────

    def _edit(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("pdf.edit: 'path' is required — provide an absolute .pdf path")

        edits = args.get("edits")
        if not edits or not isinstance(edits, list):
            return _err("pdf.edit: 'edits' is required and must be a non-empty list")

        path = _resolve(raw)
        p = Path(path)
        if not p.exists():
            return _err(f"pdf.edit: file not found: {path} — use fs_browse.find to locate it")

        try:
            html = extract_pdf_to_html(path)
        except ImportError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"Failed to extract PDF content: {e}")

        html, applied, failed = apply_edits(html, edits)

        err = _html_to_pdf(html, path)
        if err:
            return _err(f"Edits applied but re-render failed: {err}", EDITS_APPLIED=applied, EDITS_FAILED=failed)

        out: Dict[str, Any] = {
            "STATUS": "success" if not failed else "failed",
            "PATH": str(p),
            "EDITS_APPLIED": len(applied),
            "EDITS_FAILED": len(failed),
            "APPLIED": applied,
            "FAILED": failed,
            "HTML": html,
        }
        if failed:
            out["ERROR"] = f"{len(failed)} edit(s) could not be applied — check FAILED for details."
        return out

    # ── merge ─────────────────────────────────────────────────────────────────

    def _merge(self, args: Dict[str, Any]) -> Dict[str, Any]:
        sources = args.get("sources")
        if not sources or not isinstance(sources, list):
            return _err("pdf.merge: 'sources' is required and must be a non-empty list of .pdf paths")

        raw_tgt = str(args.get("target") or "").strip()
        if not raw_tgt:
            return _err("pdf.merge: 'target' is required — provide an absolute .pdf output path")
        if not raw_tgt.lower().endswith(".pdf"):
            return _err("pdf.merge: target must end in .pdf")

        target = _resolve(raw_tgt)
        tgt_p = Path(target)

        if tgt_p.exists() and not args.get("confirmed"):
            return _needs_confirm(f"Target already exists: {target}\nSetting confirmed=true will overwrite it.")

        resolved_sources = [_resolve(str(s)) for s in sources]
        missing = [s for s in resolved_sources if not Path(s).exists()]
        if missing:
            return _err(f"Source file(s) not found.", MISSING_FILES=missing)

        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            return _err("pypdf not installed. Run: pip install pypdf")

        try:
            writer = PdfWriter()
            total_pages = 0
            for src in resolved_sources:
                reader = PdfReader(src)
                for page in reader.pages:
                    writer.add_page(page)
                total_pages += len(reader.pages)

            tgt_p.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as f:
                writer.write(f)

            return _ok(
                TARGET=target,
                SOURCES_MERGED=len(resolved_sources),
                PAGES_TOTAL=total_pages,
                SIZE_BYTES=tgt_p.stat().st_size,
            )
        except PermissionError:
            return _err(f"Permission denied: {target}")
        except Exception as e:
            return _err(f"Merge failed: {e}")


# ── registry ──────────────────────────────────────────────────────────────────

TOOL_NAME = "pdf"
TOOL_CLASS = PdfTool


def get_tool() -> PdfTool:
    return PdfTool()
