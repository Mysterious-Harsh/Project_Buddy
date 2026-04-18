from __future__ import annotations

# ==========================================================
# fetch.py  —  v1.0.0
#
# Web Fetch tool — full page extraction from multiple URLs.
# Use after web_search: pass search result URLs as input.
#
# Extraction: trafilatura (primary), BeautifulSoup (fallback).
# Multi-URL: fetches all URLs in one step, returns per-URL results.
# ==========================================================

import re
from typing import Any, Callable, Dict, List, Optional

import certifi
import requests
from pydantic import BaseModel, Field, model_validator

from buddy.logger.logger import get_logger
from buddy.prompts.web_fetch_prompts import (
    WEB_FETCH_ERROR_RECOVERY_PROMPT,
    WEB_FETCH_TOOL_PROMPT,
    tool_call_format,
)

logger = get_logger("web_fetch")

# ==========================================================
# Constants
# ==========================================================

_MAX_URLS = 5
_DEFAULT_MAX_CHARS = 8_000
_MAX_CHARS_HARD_LIMIT = 20_000
_FETCH_TIMEOUT_S = 10
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ==========================================================
# Input model
# ==========================================================


class WebFetchCall(BaseModel):
    urls: List[str] = Field(..., min_length=1)
    max_chars: int = Field(default=_DEFAULT_MAX_CHARS)

    @model_validator(mode="after")
    def _validate(self) -> "WebFetchCall":
        self.urls = [u.strip() for u in self.urls if u.strip()]
        if not self.urls:
            raise ValueError("urls must not be empty")
        for url in self.urls:
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"Invalid URL (must start with http/https): {url}")
        if len(self.urls) > _MAX_URLS:
            self.urls = self.urls[:_MAX_URLS]
        self.max_chars = max(500, min(self.max_chars, _MAX_CHARS_HARD_LIMIT))
        return self


# ==========================================================
# Tool
# ==========================================================


class WebFetch:
    """
    Web Fetch tool — downloads and extracts full page text from URLs.
    Accepts multiple URLs (up to 5) in one call.
    Use after web_search, passing URLs from search results as input.
    """

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": TOOL_NAME,
            "version": "1.0.0",
            "description": (
                "Fetch full readable text from one or more URLs (up to 5). Use AFTER"
                " web_search — pass the URLs from search results as input. Returns full"
                " article/page content extracted as plain text. Do NOT use on weather"
                " sites, maps, or social media — they use JavaScript and return empty"
                " content; use web_search snippets for those instead."
            ),
            "prompt": WEB_FETCH_TOOL_PROMPT,
            "error_prompt": WEB_FETCH_ERROR_RECOVERY_PROMPT,
            "tool_call_format": tool_call_format,
        }

    def parse_call(self, payload: Dict[str, Any]) -> WebFetchCall:
        return WebFetchCall.model_validate(payload)

    async def execute(
        self,
        call: WebFetchCall,
        *,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        goal: str = "",
        brain: Optional[Any] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        from buddy.brain.text_reader import maybe_read

        results: List[Dict[str, Any]] = []
        fetched = 0

        for url in call.urls:
            if on_progress:
                on_progress(f"Fetching: {url}", False)
            result = self._fetch_one(url, call.max_chars)

            # Run large content through TextReader if brain + goal are available
            if result["error"] is None and result.get("content") and goal and brain:
                result["content"] = maybe_read(
                    result["content"], goal, brain, on_progress
                )
                result["size_chars"] = len(result["content"])

            results.append(result)
            if result["error"] is None:
                fetched += 1

        return {
            "OK": fetched > 0,
            "RESULTS": results,
            "TOTAL_FETCHED": fetched,
            "ERROR": None if fetched > 0 else "All URLs failed to fetch",
        }

    # ── Single URL ────────────────────────────────────────

    def _fetch_one(self, url: str, max_chars: int) -> Dict[str, Any]:
        try:
            resp = requests.get(
                url,
                timeout=_FETCH_TIMEOUT_S,
                headers={"User-Agent": _USER_AGENT},
                allow_redirects=True,
                verify=certifi.where(),
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            return _err(url, "Request timed out")
        except requests.exceptions.HTTPError as e:
            return _err(url, f"HTTP {e.response.status_code}: {e.response.reason}")
        except Exception as e:
            return _err(url, f"{type(e).__name__}: {e}")

        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return _err(url, f"Non-text content type: {content_type}")

        content, title = _extract(resp.text, url)

        if not content or len(content) < 50:
            return _err(url, "Empty content — site likely uses JavaScript rendering")

        if len(content) > max_chars:
            content = content[:max_chars] + f"\n[truncated at {max_chars} chars]"

        return {
            "url": url,
            "title": title,
            "content": content,
            "size_chars": len(content),
            "error": None,
        }


# ==========================================================
# Helpers
# ==========================================================


def _err(url: str, msg: str) -> Dict[str, Any]:
    logger.warning("web_fetch error [%s]: %s", url, msg)
    return {"url": url, "title": None, "content": None, "size_chars": 0, "error": msg}


def _extract(html: str, url: str) -> tuple[str, str]:
    """trafilatura primary, BeautifulSoup fallback."""

    # ── trafilatura ───────────────────────────────────────
    try:
        import trafilatura  # type: ignore

        content = trafilatura.extract(
            html,
            url=url or None,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        if content and len(content.strip()) > 50:
            title = ""
            try:
                meta = trafilatura.extract_metadata(html, default_url=url or None)
                if meta:
                    title = meta.title or ""
            except Exception:
                pass
            return content.strip(), title
    except Exception:
        pass

    # ── BeautifulSoup fallback ────────────────────────────
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "lxml")
        for tag in soup([
            "script",
            "style",
            "nav",
            "footer",
            "header",
            "aside",
            "form",
            "noscript",
            "iframe",
        ]):
            tag.decompose()

        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        body = soup.find("article") or soup.find("main") or soup.find("body") or soup
        raw = body.get_text(separator="\n")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
        return text, title
    except Exception:
        pass

    return "", ""


# ==========================================================
# Registry contract
# ==========================================================

TOOL_NAME = "web_fetch"
TOOL_CLASS = WebFetch


def get_tool() -> WebFetch:
    return WebFetch()
