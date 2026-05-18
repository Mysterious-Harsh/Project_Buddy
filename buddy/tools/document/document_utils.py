from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── section ID stamping ───────────────────────────────────────────────────────

_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "table", "ul", "ol", "div", "section", "article", "blockquote", "pre", "figure"}
_BLOCK_RE = re.compile(r"<(h[1-6]|p|table|ul|ol|div|section|article|blockquote|pre|figure)(\s[^>]*)?>", re.IGNORECASE)
_ID_ATTR_RE = re.compile(r'\bid\s*=\s*["\'][^"\']*["\']', re.IGNORECASE)


def stamp_ids(html: str) -> str:
    """Add id='s{n}' to every block-level element. Replaces any existing id attributes."""
    counter = 0

    def _replace(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        tag = m.group(1)
        attrs = m.group(2) or ""
        attrs = _ID_ATTR_RE.sub("", attrs).strip()
        return f"<{tag} id=\"s{counter}\"" + (f" {attrs}" if attrs else "") + ">"

    return _BLOCK_RE.sub(_replace, html)


# ── HTML source path (REMOVED) ────────────────────────────────────────────────


# ── edit ops ──────────────────────────────────────────────────────────────────

def apply_edits(html: str, edits: List[Dict[str, Any]]) -> Tuple[str, List[Dict], List[Dict]]:
    """
    Apply a list of edit ops to HTML. Returns (new_html, applied[], failed[]).
    After all ops, section IDs are re-stamped sequentially.
    """
    applied: List[Dict] = []
    failed: List[Dict] = []

    for edit in edits:
        if not isinstance(edit, dict):
            failed.append({"edit": str(edit), "error": "Each edit must be an object."})
            continue

        op = edit.get("op")
        section_id = edit.get("section_id")

        # replace by section_id
        if section_id and not op:
            new_content = edit.get("new")
            if not new_content:
                failed.append({"section_id": section_id, "error": "new is required for replace."})
                continue
            pattern = re.compile(
                rf"<[^>]+\bid\s*=\s*[\"']{re.escape(section_id)}[\"'][^>]*>.*?</[^>]+>",
                re.IGNORECASE | re.DOTALL,
            )
            if not pattern.search(html):
                failed.append({"section_id": section_id, "error": f"Section '{section_id}' not found."})
                continue
            html = pattern.sub(new_content, html, count=1)
            applied.append({"section_id": section_id, "op": "replace"})

        # old/new text patch
        elif "old" in edit and not op:
            old = edit.get("old", "")
            new = edit.get("new", "")
            if old not in html:
                failed.append({"old": old[:80], "error": "Text not found in HTML."})
                continue
            html = html.replace(old, new, 1)
            applied.append({"op": "text_patch", "old_preview": old[:40]})

        # add_after
        elif op == "add_after":
            if not section_id:
                failed.append({"op": op, "error": "section_id is required."})
                continue
            new_content = edit.get("new", "")
            pattern = re.compile(
                rf"(<[^>]+\bid\s*=\s*[\"']{re.escape(section_id)}[\"'][^>]*>.*?</[^>]+>)",
                re.IGNORECASE | re.DOTALL,
            )
            if not pattern.search(html):
                failed.append({"op": op, "section_id": section_id, "error": f"Section '{section_id}' not found."})
                continue
            html = pattern.sub(rf"\1\n{new_content}", html, count=1)
            applied.append({"op": op, "section_id": section_id})

        # add_before
        elif op == "add_before":
            if not section_id:
                failed.append({"op": op, "error": "section_id is required."})
                continue
            new_content = edit.get("new", "")
            pattern = re.compile(
                rf"(<[^>]+\bid\s*=\s*[\"']{re.escape(section_id)}[\"'][^>]*>.*?</[^>]+>)",
                re.IGNORECASE | re.DOTALL,
            )
            if not pattern.search(html):
                failed.append({"op": op, "section_id": section_id, "error": f"Section '{section_id}' not found."})
                continue
            html = pattern.sub(rf"{new_content}\n\1", html, count=1)
            applied.append({"op": op, "section_id": section_id})

        # add_end
        elif op == "add_end":
            new_content = edit.get("new", "")
            # insert before </body> if present, else append
            if re.search(r"</body>", html, re.IGNORECASE):
                html = re.sub(r"(</body>)", rf"\n{new_content}\n\1", html, count=1, flags=re.IGNORECASE)
            else:
                html = html + "\n" + new_content
            applied.append({"op": op})

        # remove
        elif op == "remove":
            if not section_id:
                failed.append({"op": op, "error": "section_id is required."})
                continue
            pattern = re.compile(
                rf"<[^>]+\bid\s*=\s*[\"']{re.escape(section_id)}[\"'][^>]*>.*?</[^>]+>\n?",
                re.IGNORECASE | re.DOTALL,
            )
            if not pattern.search(html):
                failed.append({"op": op, "section_id": section_id, "error": f"Section '{section_id}' not found."})
                continue
            html = pattern.sub("", html, count=1)
            applied.append({"op": op, "section_id": section_id})

        else:
            failed.append({"edit": str(edit)[:80], "error": f"Unknown op: '{op}'. Valid: add_after, add_before, add_end, remove, or provide section_id/old+new."})

    html = stamp_ids(html)
    return html, applied, failed


# ── search HTML ───────────────────────────────────────────────────────────────

def search_html(html: str, query: str) -> str:
    """Return only block-level HTML elements whose text content contains query (case-insensitive)."""
    pattern = re.compile(
        r"<([a-z][a-z0-9]*)\s[^>]*\bid\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</\1>",
        re.IGNORECASE | re.DOTALL,
    )
    q = query.lower()
    matches = []
    for m in pattern.finditer(html):
        inner_text = re.sub(r"<[^>]+>", "", m.group(3))
        if q in inner_text.lower():
            matches.append(m.group(0))
    return "\n".join(matches)


# ── extraction: .docx → HTML ──────────────────────────────────────────────────

def extract_docx_to_html(path: str) -> str:
    """Extract content from an existing .docx and reconstruct as indexed HTML for editing."""
    import hashlib
    import os
    try:
        from docx import Document
        from docx.oxml.ns import qn
    except ImportError:
        raise ImportError("python-docx not installed. Run: pip install python-docx")

    doc = Document(path)
    parts: List[str] = ["<html><body>"]

    media_dir = Path("~/.buddy/word_media").expanduser()
    media_dir.mkdir(parents=True, exist_ok=True)

    p_idx = 0
    t_idx = 0

    for block in doc.element.body:
        tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag

        if tag == "p":
            from docx.text.paragraph import Paragraph
            para = Paragraph(block, doc)
            
            # Extract images in this paragraph
            images_html = ""
            for run in para.runs:
                for drawing in run.element.findall('.//w:drawing', namespaces=run.element.nsmap):
                    for blip in drawing.findall('.//a:blip', namespaces=run.element.nsmap):
                        embed = blip.get(f'{{{run.element.nsmap["r"]}}}embed')
                        if embed and embed in doc.part.related_parts:
                            image_part = doc.part.related_parts[embed]
                            blob = image_part.blob
                            ext = image_part.content_type.split("/")[-1]
                            if ext == "jpeg": ext = "jpg"
                            
                            file_hash = hashlib.md5(blob).hexdigest()[:10]
                            img_path = media_dir / f"img_{file_hash}.{ext}"
                            if not img_path.exists():
                                with open(img_path, "wb") as f:
                                    f.write(blob)
                            images_html += f'<img src="{img_path}">'
            
            style_name = (para.style.name or "").lower()
            text = _runs_to_html(para.runs)
            
            # Skip if truly empty
            if not text.strip() and not images_html:
                p_idx += 1
                continue
                
            content = text + images_html

            if style_name.startswith("heading"):
                try:
                    level = int(style_name.replace("heading", "").strip())
                    level = max(1, min(6, level))
                except ValueError:
                    level = 2
                parts.append(f"<h{level} id=\"p{p_idx}\">{content}</h{level}>")
            else:
                parts.append(f"<p id=\"p{p_idx}\">{content}</p>")
            
            p_idx += 1

        elif tag == "tbl":
            from docx.table import Table
            tbl = Table(block, doc)
            rows_html = []
            for i, row in enumerate(tbl.rows):
                cells = "".join(
                    f"<{'th' if i == 0 else 'td'}>{c.text}</{'th' if i == 0 else 'td'}>"
                    for c in row.cells
                )
                rows_html.append(f"<tr>{cells}</tr>")
            parts.append(f"<table id=\"t{t_idx}\">{''.join(rows_html)}</table>")
            t_idx += 1

    parts.append("</body></html>")
    return "\n".join(parts)


def _runs_to_html(runs: Any) -> str:
    out = []
    for run in runs:
        text = run.text or ""
        if not text:
            continue
        # simple html escape
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if run.bold and run.italic:
            text = f"<strong><em>{text}</em></strong>"
        elif run.bold:
            text = f"<strong>{text}</strong>"
        elif run.italic:
            text = f"<em>{text}</em>"
        elif run.underline:
            text = f"<u>{text}</u>"
        out.append(text)
    return "".join(out)


# ── extraction: .pdf → HTML ───────────────────────────────────────────────────

def extract_pdf_to_html(path: str) -> str:
    """Extract content from an existing .pdf and reconstruct as structured HTML using pymupdf4llm."""
    try:
        import pymupdf4llm
        import markdown
    except ImportError:
        raise ImportError("pymupdf4llm or Markdown not installed. Run: pip install pymupdf4llm Markdown")

    # Extract PDF to Markdown (preserves tables, lists, and reading order perfectly)
    md_text = pymupdf4llm.to_markdown(path)
    
    # Convert the structured Markdown back to semantic HTML for the LLM to read and patch
    html_content = markdown.markdown(md_text, extensions=['tables', 'fenced_code'])
    
    full_html = f"<html><body>\n{html_content}\n</body></html>"
    
    # Stamp standard s1, s2 section IDs for the LLM to target
    return stamp_ids(full_html)
