from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from buddy.prompts.word_prompts import WORD_TOOL_PROMPT
from buddy.tools.document.document_utils import (
    extract_docx_to_html,
    search_html,
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
            converter = HtmlToDocx()
            converter.parse_html_file_and_save(None, str(p), html_content=content)
            return _ok(
                PATH=str(p),
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

        try:
            html = extract_docx_to_html(path)
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

        try:
            from docx import Document
            from docx.shared import Inches
            from docx.enum.text import WD_BREAK
            from docx.enum.section import WD_ORIENT
            from lxml.etree import Element
            import html.parser
            import base64
            import io
            import os
        except ImportError:
            return _err("python-docx not installed. Run: pip install python-docx")

        try:
            doc = Document(path)
        except Exception as e:
            return _err(f"Failed to open document: {e}")

        applied = []
        failed = []

        class HTMLInterceptor(html.parser.HTMLParser):
            def __init__(self, target_para):
                super().__init__()
                self.target_para = target_para
                self.bold = False
                self.italic = False
                self.underline = False
                
            def handle_starttag(self, tag, attrs):
                if tag in ('b', 'strong'): self.bold = True
                elif tag in ('i', 'em'): self.italic = True
                elif tag == 'u': self.underline = True
                elif tag == 'img':
                    src = dict(attrs).get('src', '')
                    if src:
                        try:
                            run = self.target_para.add_run()
                            if src.startswith('data:image'):
                                header, b64 = src.split(',', 1)
                                run.add_picture(io.BytesIO(base64.b64decode(b64)))
                            else:
                                if src.startswith('~'): src = os.path.expanduser(src)
                                run.add_picture(src)
                        except Exception as e:
                            pass
                            
            def handle_endtag(self, tag):
                if tag in ('b', 'strong'): self.bold = False
                elif tag in ('i', 'em'): self.italic = False
                elif tag == 'u': self.underline = False
                
            def handle_data(self, data):
                if not data: return
                run = self.target_para.add_run(data)
                run.bold = self.bold
                run.italic = self.italic
                run.underline = self.underline

        def parse_and_apply(html_str: str, para: Any) -> None:
            para.clear()
            html_str = html_str.strip()
            import re
            if html_str.startswith('<p') and html_str.endswith('</p>'):
                html_str = re.sub(r'^<p[^>]*>|</p>$', '', html_str)
            HTMLInterceptor(para).feed(html_str)

        for edit in edits:
            op = edit.get("op")
            section_id = str(edit.get("section_id") or "")
            
            try:
                if op == "set_page_setup":
                    margin = edit.get("margin")
                    orient = edit.get("orientation")
                    for section in doc.sections:
                        if margin == "narrow":
                            section.top_margin = Inches(0.5)
                            section.bottom_margin = Inches(0.5)
                            section.left_margin = Inches(0.5)
                            section.right_margin = Inches(0.5)
                        elif margin == "wide":
                            section.top_margin = Inches(1)
                            section.bottom_margin = Inches(1)
                            section.left_margin = Inches(2)
                            section.right_margin = Inches(2)
                        elif margin == "normal":
                            section.top_margin = Inches(1)
                            section.bottom_margin = Inches(1)
                            section.left_margin = Inches(1)
                            section.right_margin = Inches(1)
                            
                        if orient == "landscape":
                            section.orientation = WD_ORIENT.LANDSCAPE
                            section.page_width, section.page_height = section.page_height, section.page_width
                        elif orient == "portrait":
                            section.orientation = WD_ORIENT.PORTRAIT
                            section.page_width, section.page_height = min(section.page_width, section.page_height), max(section.page_width, section.page_height)
                    applied.append({"op": op})
                    continue

                if op == "add_end":
                    new_p = doc.add_paragraph()
                    parse_and_apply(edit.get("new", ""), new_p)
                    applied.append({"op": op})
                    continue

                if not section_id:
                    failed.append({"edit": op, "error": "section_id is required"})
                    continue

                t_type = section_id[0]
                try:
                    idx = int(section_id[1:])
                except ValueError:
                    failed.append({"section_id": section_id, "error": "Invalid section_id format"})
                    continue
                
                target = None
                if t_type == "p" and 0 <= idx < len(doc.paragraphs):
                    target = doc.paragraphs[idx]
                elif t_type == "t" and 0 <= idx < len(doc.tables):
                    target = doc.tables[idx]
                
                if not target:
                    failed.append({"section_id": section_id, "error": "Index out of bounds"})
                    continue

                if op == "replace":
                    if t_type == "p":
                        parse_and_apply(edit.get("new", ""), target)
                        applied.append({"op": op, "section_id": section_id})
                    else:
                        failed.append({"section_id": section_id, "error": "Replacing entire tables is not yet supported."})

                elif op == "remove":
                    element = target._element
                    element.getparent().remove(element)
                    target._element = None
                    applied.append({"op": op, "section_id": section_id})

                elif op in ("add_after", "add_before"):
                    style = edit.get("style")
                    element = target._element
                    new_element = Element(element.tag)
                    if op == "add_after":
                        element.addnext(new_element)
                    else:
                        element.addprevious(new_element)
                    
                    if t_type == "p":
                        from docx.text.paragraph import Paragraph
                        new_p = Paragraph(new_element, target._parent)
                        if style: new_p.style = style
                        parse_and_apply(edit.get("new", ""), new_p)
                    applied.append({"op": op, "section_id": section_id})

                elif op == "add_page_break":
                    if t_type == "p":
                        target.add_run().add_break(WD_BREAK.PAGE)
                        applied.append({"op": op, "section_id": section_id})
                    else:
                        failed.append({"section_id": section_id, "error": "Can only add page break to paragraphs"})
                else:
                    failed.append({"edit": op, "error": "Unknown op"})
            except Exception as e:
                failed.append({"edit": edit, "error": str(e)})

        try:
            doc.save(path)
        except PermissionError:
            return _err(f"Permission denied when saving: {path}")
        except Exception as e:
            return _err(f"Edits applied but save failed: {e}")

        out: Dict[str, Any] = {
            "STATUS": "success" if not failed else "failed",
            "PATH": str(p),
            "EDITS_APPLIED": len(applied),
            "EDITS_FAILED": len(failed),
            "APPLIED": applied,
            "FAILED": failed,
        }
        if failed:
            out["ERROR"] = f"{len(failed)} edit(s) could not be applied — check FAILED for details."
        return out



# ── registry ──────────────────────────────────────────────────────────────────

TOOL_NAME = "word"
TOOL_CLASS = WordTool


def get_tool() -> WordTool:
    return WordTool()
