from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from buddy.prompts.word_prompts import WORD_TOOL_PROMPT
from buddy.tools.document.document_utils import (
    apply_edits,
    extract_docx_to_html,
    html_source_path,
    load_html_source,
    save_html_source,
    search_html,
    stamp_ids,
)

_TOOL = "word"


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


# ── tool ──────────────────────────────────────────────────────────────────────

class WordTool:
    tool_name = _TOOL
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "WHEN: creating or editing .docx documents, or converting other file formats into .docx.\n\n"
                "FUNCTIONS:\n"
                "  create(path, content)       — HTML+CSS string → .docx; saves .html source alongside for future edits\n"
                "  read(path, search?)         — returns full HTML source with section IDs stamped (id='s1','s2',...); search= returns only matching sections\n"
                "  edit(path, edits[])         — patch HTML and re-render: {section_id+new} replace, {old+new} text patch, {op:add_after/add_before/add_end+new} insert, {op:remove+section_id} delete\n"
                "  convert(source, target)     — convert .pdf/.html/.md/.txt → .docx\n"
                "  export(path, target)        — .docx → .pdf via LibreOffice\n\n"
                "CHAIN: always call read before edit to get current section IDs — IDs change after every edit. "
                "convert output path feeds read/edit for further changes.\n"
                "NOT: .xlsx → excel | .pdf creation → pdf | plain file reads → fs_read | plain file writes → fs_write"
            ),
            "prompt": WORD_TOOL_PROMPT,
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
        return _err(f"Unknown function: {function!r}. Must be: create, read, edit")

    # ── create ────────────────────────────────────────────────────────────────

    def _create(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("word.create: 'path' is required — provide an absolute .docx path")
        if not raw.lower().endswith(".docx"):
            return _err("word.create: path must end in .docx")

        content = str(args.get("content") or "").strip()
        if not content:
            return _err("content is required — provide an HTML+CSS string")

        path = _resolve(raw)
        p = Path(path)

        if p.exists() and not args.get("confirmed"):
            return _needs_confirm(f"File already exists: {path}\nSetting confirmed=true will overwrite it.")

        try:
            from htmldocx import HtmlToDocx
        except ImportError:
            return _err("htmldocx not installed. Run: pip install htmldocx")

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            stamped = stamp_ids(content)
            converter = HtmlToDocx()
            converter.parse_html_file_and_save(None, str(p), html_content=stamped)
            save_html_source(path, stamped)
            return _ok(
                PATH=str(p),
                HTML_SOURCE=str(html_source_path(path)),
                SIZE_BYTES=p.stat().st_size,
            )
        except PermissionError:
            return _err(f"Permission denied: {path}")
        except Exception as e:
            return _err(f"Failed to create document: {e}")

    # ── read ──────────────────────────────────────────────────────────────────

    def _read(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("word.read: 'path' is required — provide an absolute .docx path")

        path = _resolve(raw)
        p = Path(path)
        if not p.exists():
            return _err(f"word.read: file not found: {path} — use fs_browse.find to locate it")

        search = str(args.get("search") or "").strip() or None

        html = load_html_source(path)
        if html is None:
            try:
                html = extract_docx_to_html(path)
                save_html_source(path, html)
            except ImportError as e:
                return _err(str(e))
            except Exception as e:
                return _err(f"Failed to extract document content: {e}")

        if search:
            html = search_html(html, search)
            if not html:
                return _ok(PATH=str(p), SEARCH=search, HTML="", NOTE="No sections matched the search query.")

        return _ok(PATH=str(p), HTML=html)

    # ── edit ──────────────────────────────────────────────────────────────────

    def _edit(self, args: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return _err("word.edit: 'path' is required — provide an absolute .docx path")

        edits = args.get("edits")
        if not edits or not isinstance(edits, list):
            return _err("word.edit: 'edits' is required and must be a non-empty list")

        path = _resolve(raw)
        p = Path(path)
        if not p.exists():
            return _err(f"word.edit: file not found: {path} — use fs_browse.find to locate it")

        html = load_html_source(path)
        if html is None:
            try:
                html = extract_docx_to_html(path)
            except ImportError as e:
                return _err(str(e))
            except Exception as e:
                return _err(f"Failed to extract document content: {e}")

        html, applied, failed = apply_edits(html, edits)

        try:
            from htmldocx import HtmlToDocx
        except ImportError:
            return _err("htmldocx not installed. Run: pip install htmldocx")

        try:
            save_html_source(path, html)
            converter = HtmlToDocx()
            converter.parse_html_file_and_save(None, str(p), html_content=html)
        except PermissionError:
            return _err(f"Permission denied when saving: {path}")
        except Exception as e:
            return _err(f"Edits applied but re-render failed: {e}", EDITS_APPLIED=applied, EDITS_FAILED=failed)

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



# ── registry ──────────────────────────────────────────────────────────────────

TOOL_NAME = "word"
TOOL_CLASS = WordTool


def get_tool() -> WordTool:
    return WordTool()
