# buddy/brain/text_reader.py
# ═══════════════════════════════════════════════════════════
# LARGE TEXT READER
# ═══════════════════════════════════════════════════════════
#
# Reads large text paragraph by paragraph using the LLM.
# Each paragraph gets one focused LLM call — the model decides
# if the paragraph is relevant to the user query, and if so,
# rewrites it preserving every detail.
#
# Public API:
#   split_paragraphs(text) -> List[str]
#   TextReader.read(text, query, llm, on_progress) -> str
#   maybe_read(text, query, llm, on_progress) -> str   ← tool integration helper

from __future__ import annotations

import re
from typing import Any, Callable, List, Optional

from buddy.logger.logger import get_logger

logger = get_logger("text_reader")

# ═══════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════

CHAR_THRESHOLD   = 3_000   # below this → skip reader, return as-is
_MIN_PARA_CHARS  = 80      # merge shorter fragments with next paragraph
_MAX_PARA_CHARS  = 1_500   # split oversized paragraphs at sentence boundary
_ROLLING_CAP     = 300     # max chars kept in rolling context
_NO_RESULT_MSG   = "No relevant content found for this query."


# ═══════════════════════════════════════════════════════════
# Paragraph splitter
# ═══════════════════════════════════════════════════════════


def split_paragraphs(text: str) -> List[str]:
    """
    Split text into paragraphs suitable for the reading loop.

    Rules:
    - Split on blank lines (\\n\\n)
    - Never split inside fenced code blocks (``` ... ```)
    - Never split a markdown list mid-way (lines starting with -, *, digits.)
    - Merge fragments shorter than _MIN_PARA_CHARS into the next paragraph
    - Split paragraphs longer than _MAX_PARA_CHARS at sentence boundaries
    """
    # ── Step 1: protect code blocks ───────────────────────
    # Replace code block content temporarily so we don't split inside them
    code_blocks: List[str] = []

    def _store_code(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODE{len(code_blocks) - 1}\x00"

    protected = re.sub(r"```[\s\S]*?```", _store_code, text)

    # ── Step 2: raw split on blank lines ──────────────────
    raw_chunks = re.split(r"\n{2,}", protected)

    # ── Step 3: merge list continuations ──────────────────
    # If a chunk starts with a list marker, merge with previous chunk
    merged: List[str] = []
    list_marker = re.compile(r"^\s*([-*]|\d+[.)]) ")
    for chunk in raw_chunks:
        stripped = chunk.strip()
        if not stripped:
            continue
        if merged and list_marker.match(stripped) and list_marker.match(merged[-1].strip().splitlines()[0] if merged[-1].strip() else ""):
            merged[-1] = merged[-1].rstrip() + "\n" + chunk
        else:
            merged.append(chunk)

    # ── Step 4: merge tiny fragments ──────────────────────
    result: List[str] = []
    for chunk in merged:
        stripped = chunk.strip()
        if not stripped:
            continue
        if result and len(stripped) < _MIN_PARA_CHARS:
            result[-1] = result[-1].rstrip() + "\n" + stripped
        else:
            result.append(stripped)

    # ── Step 5: split oversized paragraphs ────────────────
    final: List[str] = []
    for chunk in result:
        if len(chunk) <= _MAX_PARA_CHARS:
            final.append(chunk)
        else:
            final.extend(_split_at_sentences(chunk))

    # ── Step 6: restore code blocks ───────────────────────
    restored: List[str] = []
    for chunk in final:
        def _restore(m: re.Match) -> str:
            idx = int(m.group(1))
            return code_blocks[idx] if idx < len(code_blocks) else m.group(0)
        restored.append(re.sub(r"\x00CODE(\d+)\x00", _restore, chunk))

    return [p for p in restored if p.strip()]


def _split_at_sentences(text: str) -> List[str]:
    """Split a long paragraph at sentence boundaries to fit _MAX_PARA_CHARS."""
    sentence_end = re.compile(r"(?<=[.!?])\s+")
    sentences = sentence_end.split(text)
    chunks: List[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > _MAX_PARA_CHARS:
            chunks.append(current.strip())
            current = sentence
        else:
            current = (current + " " + sentence).strip() if current else sentence
    if current:
        chunks.append(current.strip())
    return chunks or [text]


# ═══════════════════════════════════════════════════════════
# TextReader
# ═══════════════════════════════════════════════════════════


class TextReader:
    """
    Reads large text paragraph by paragraph using the LLM.
    Each paragraph is one focused LLM call — model decides relevance
    and rewrites faithfully if relevant.
    """

    def read(
        self,
        text: str,
        query: str,
        brain: Any,
        on_progress: Optional[Callable[[str, bool], None]] = None,
    ) -> str:
        """
        Read text and return only the content relevant to query.

        Args:
            text:        Full raw text to read (large document/page)
            query:       The user's question or goal (step goal from executor)
            brain:       Brain instance — provides _build_context() and llm
            on_progress: Optional UI callback (message, is_done)

        Returns:
            Compact string of relevant content, or _NO_RESULT_MSG if nothing found.
        """
        paragraphs = split_paragraphs(text)
        total      = len(paragraphs)

        if on_progress:
            on_progress(f"Reading {total} paragraphs...", False)

        logger.info("TextReader: %d paragraphs, query=%r", total, query[:60])

        rolling_context = ""
        collected: List[str] = []

        for i, paragraph in enumerate(paragraphs, 1):
            if on_progress:
                on_progress(f"Reading paragraph {i}/{total}...", False)

            result = self._read_paragraph(paragraph, query, rolling_context, brain)

            if result.get("relevant") and result.get("content", "").strip():
                content = result["content"].strip()
                collected.append(content)

                # Update rolling context — 1-sentence carry-forward
                rolling_context = self._update_rolling(rolling_context, content)
                logger.debug("TextReader p%d/%d: relevant", i, total)
            else:
                logger.debug("TextReader p%d/%d: skipped", i, total)

        if not collected:
            if on_progress:
                on_progress("No relevant content found.", True)
            return _NO_RESULT_MSG

        output = "\n\n".join(collected)
        if on_progress:
            on_progress(
                f"Reading complete — {len(collected)}/{total} paragraphs relevant "
                f"({len(output)} chars).",
                True,
            )
        return output

    # ── Single paragraph call ──────────────────────────────

    def _read_paragraph(
        self,
        paragraph: str,
        query: str,
        rolling_context: str,
        brain: Any,
    ) -> dict:
        try:
            # All LLM calls go through Brain — same as every other prompt type
            return brain.run_reader(
                paragraph=paragraph,
                query=query,
                rolling_context=rolling_context,
            )
        except Exception as ex:
            logger.warning("TextReader paragraph call failed: %r", ex)
            return {"relevant": False, "content": ""}

    # ── Rolling context update ─────────────────────────────

    @staticmethod
    def _update_rolling(current: str, new_content: str) -> str:
        """
        Append a brief note about what was just found.
        Keeps rolling context under _ROLLING_CAP chars by dropping oldest entries.
        """
        # Take first sentence of new content as the summary
        first_sentence = re.split(r"(?<=[.!?])\s", new_content)[0][:120]
        entry = first_sentence.strip()

        combined = (current + " | " + entry).strip(" |") if current else entry

        # Trim from the left if over cap
        if len(combined) > _ROLLING_CAP:
            combined = combined[-_ROLLING_CAP:]
            # Trim to the first " | " boundary to avoid partial entries
            boundary = combined.find(" | ")
            if boundary != -1:
                combined = combined[boundary + 3:]

        return combined


# ═══════════════════════════════════════════════════════════
# Tool integration helper
# ═══════════════════════════════════════════════════════════


def maybe_read(
    text: str,
    query: str,
    brain: Any,
    on_progress: Optional[Callable[[str, bool], None]] = None,
) -> str:
    """
    Drop-in helper for tool integration.

    If text fits in context → return as-is.
    If text is too large → run through TextReader and return compact result.

    Usage in any tool:
        from buddy.brain.text_reader import maybe_read
        content = maybe_read(raw_content, goal, brain, on_progress)
    """
    if not text or len(text) <= CHAR_THRESHOLD:
        return text
    return TextReader().read(text, query, brain, on_progress)


