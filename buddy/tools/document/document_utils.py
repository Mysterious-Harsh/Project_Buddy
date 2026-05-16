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


# ── HTML source path ──────────────────────────────────────────────────────────

def html_source_path(doc_path: str) -> Path:
    """Return the .html source path alongside a document file."""
    return Path(doc_path).with_suffix(".html")


def load_html_source(doc_path: str) -> Optional[str]:
    """Return HTML source content if it exists alongside the document."""
    src = html_source_path(doc_path)
    if src.exists():
        return src.read_text(encoding="utf-8")
    return None


def save_html_source(doc_path: str, html: str) -> None:
    """Save HTML source alongside the document."""
    html_source_path(doc_path).write_text(html, encoding="utf-8")


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
    """Extract content from an existing .docx and reconstruct as HTML."""
    try:
        from docx import Document
        from docx.oxml.ns import qn
    except ImportError:
        raise ImportError("python-docx not installed. Run: pip install python-docx")

    doc = Document(path)
    parts: List[str] = ["<html><body>"]

    for block in doc.element.body:
        tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag

        if tag == "p":
            from docx.text.paragraph import Paragraph
            para = Paragraph(block, doc)
            style_name = (para.style.name or "").lower()
            text = _runs_to_html(para.runs)
            if not text.strip():
                continue
            if style_name.startswith("heading"):
                try:
                    level = int(style_name.replace("heading", "").strip())
                    level = max(1, min(6, level))
                except ValueError:
                    level = 2
                parts.append(f"<h{level}>{text}</h{level}>")
            else:
                parts.append(f"<p>{text}</p>")

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
            parts.append(f"<table>{''.join(rows_html)}</table>")

    parts.append("</body></html>")
    return stamp_ids("\n".join(parts))


def _runs_to_html(runs: Any) -> str:
    out = []
    for run in runs:
        text = run.text or ""
        if not text:
            continue
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
    """Extract content from an existing .pdf and reconstruct as HTML (best-effort)."""
    try:
        import pdfplumber
    except ImportError:
        raise ImportError("pdfplumber not installed. Run: pip install pdfplumber")

    parts: List[str] = ["<html><body>"]

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(extra_attrs=["size"]) or []
            if not words:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        parts.append(f"<p>{line}</p>")
                continue

            # guess headings by font size relative to median
            sizes = [float(w.get("size", 12) or 12) for w in words]
            if sizes:
                median_size = sorted(sizes)[len(sizes) // 2]
            else:
                median_size = 12.0

            # group words into lines by top position
            lines: Dict[float, List[Any]] = {}
            for w in words:
                top = round(float(w.get("top", 0)), 1)
                lines.setdefault(top, []).append(w)

            for top in sorted(lines):
                line_words = sorted(lines[top], key=lambda w: float(w.get("x0", 0)))
                text = " ".join(w["text"] for w in line_words).strip()
                if not text:
                    continue
                avg_size = sum(float(w.get("size", 12) or 12) for w in line_words) / len(line_words)
                if avg_size >= median_size * 1.3:
                    parts.append(f"<h2>{text}</h2>")
                else:
                    parts.append(f"<p>{text}</p>")

    parts.append("</body></html>")
    return stamp_ids("\n".join(parts))
