from __future__ import annotations

# ==========================================================
# search.py  —  v1.0.0
#
# Web Search tool for Buddy.
# Provides: search (DuckDuckGo), fetch (URL → plain text).
#
# Design rules:
#   - search uses duckduckgo-search (no API key, no tracking).
#   - fetch uses requests + BeautifulSoup to extract clean text.
#   - All results are capped to protect LLM context window.
#   - No cookies, no sessions — stateless per call.
# ==========================================================

import re
from typing import Any, Callable, Dict, List, Literal, Optional

import certifi
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from pydantic import BaseModel, Field, model_validator

from buddy.prompts.web_search_prompts import (
    WEB_SEARCH_ERROR_RECOVERY_PROMPT,
    WEB_SEARCH_TOOL_PROMPT,
    tool_call_format,
)

# ==========================================================
# Constants
# ==========================================================

_MAX_RESULTS_HARD_LIMIT = 20
_DEFAULT_MAX_RESULTS = 5
_MAX_CHARS_HARD_LIMIT = 20_000
_DEFAULT_MAX_CHARS = 8_000
_FETCH_TIMEOUT_S = 10
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ==========================================================
# Input model
# ==========================================================


class WebSearchCall(BaseModel):
    action: Literal["search", "fetch"]
    # search fields
    query: Optional[str] = None
    max_results: int = Field(default=_DEFAULT_MAX_RESULTS)
    region: str = Field(default="wt-wt")
    safe_search: bool = Field(default=True)
    # fetch fields
    url: Optional[str] = None
    max_chars: int = Field(default=_DEFAULT_MAX_CHARS)

    @model_validator(mode="after")
    def _validate(self) -> "WebSearchCall":
        if self.action == "search":
            if not self.query or not self.query.strip():
                raise ValueError("query is required for action=search")
            self.query = self.query.strip()
            if self.max_results < 1:
                self.max_results = _DEFAULT_MAX_RESULTS
            if self.max_results > _MAX_RESULTS_HARD_LIMIT:
                self.max_results = _MAX_RESULTS_HARD_LIMIT
        elif self.action == "fetch":
            if not self.url or not self.url.strip():
                raise ValueError("url is required for action=fetch")
            self.url = self.url.strip()
            if not self.url.startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://")
            if self.max_chars < 1:
                self.max_chars = _DEFAULT_MAX_CHARS
            if self.max_chars > _MAX_CHARS_HARD_LIMIT:
                self.max_chars = _MAX_CHARS_HARD_LIMIT
        return self


# ==========================================================
# Tool implementation
# ==========================================================


class WebSearch:
    """
    Web Search tool — search the web or fetch a URL's text content.
    Plugs into Buddy's ToolRegistry via TOOL_NAME + TOOL_CLASS.
    """

    # -------------------------
    # Registry hooks
    # -------------------------

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": TOOL_NAME,
            "version": "1.0.0",
            "description": (
                "Search the web or fetch a URL's text content. "
                "Use for: current events, facts you don't know, documentation, "
                "looking up prices, weather, news, any online information. "
                "search → DuckDuckGo results (title + snippet + url). "
                "fetch → full page text from a URL."
            ),
            "prompt": WEB_SEARCH_TOOL_PROMPT,
            "error_prompt": WEB_SEARCH_ERROR_RECOVERY_PROMPT,
            "tool_call_format": tool_call_format,
        }

    def parse_call(self, payload: Dict[str, Any]) -> WebSearchCall:
        return WebSearchCall.model_validate(payload)

    def execute(
        self,
        call: WebSearchCall,
        *,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        if call.action == "search":
            return self._search(call, on_progress=on_progress)
        return self._fetch(call, on_progress=on_progress)

    # -------------------------
    # search
    # -------------------------

    def _search(
        self,
        call: WebSearchCall,
        *,
        on_progress: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        if on_progress:
            on_progress(f"Searching: {call.query}", False)

        try:
            safesearch = "moderate" if call.safe_search else "off"
            with DDGS() as ddgs:
                raw = list(
                    ddgs.text(
                        call.query,
                        region=call.region,
                        safesearch=safesearch,
                        max_results=call.max_results,
                    )
                )
        except Exception as e:
            return {
                "OK": False,
                "ACTION": "search",
                "QUERY": call.query,
                "ERROR": f"Search failed: {type(e).__name__}: {e}",
                "RESULTS": [],
                "TOTAL_FOUND": 0,
            }

        results: List[Dict[str, str]] = []
        for item in raw:
            results.append(
                {
                    "title": str(item.get("title") or ""),
                    "url": str(item.get("href") or item.get("url") or ""),
                    "snippet": str(item.get("body") or ""),
                }
            )

        return {
            "OK": True,
            "ACTION": "search",
            "QUERY": call.query,
            "RESULTS": results,
            "TOTAL_FOUND": len(results),
            "ERROR": None,
        }

    # -------------------------
    # fetch
    # -------------------------

    def _fetch(
        self,
        call: WebSearchCall,
        *,
        on_progress: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        if on_progress:
            on_progress(f"Fetching: {call.url}", False)

        try:
            resp = requests.get(
                call.url,
                timeout=_FETCH_TIMEOUT_S,
                headers={"User-Agent": _USER_AGENT},
                allow_redirects=True,
                verify=certifi.where(),
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            return {
                "OK": False,
                "ACTION": "fetch",
                "URL": call.url,
                "ERROR": "Request timed out",
                "CONTENT": None,
                "TITLE": None,
                "SIZE_CHARS": 0,
            }
        except requests.exceptions.HTTPError as e:
            return {
                "OK": False,
                "ACTION": "fetch",
                "URL": call.url,
                "ERROR": f"HTTP {e.response.status_code}: {e.response.reason}",
                "CONTENT": None,
                "TITLE": None,
                "SIZE_CHARS": 0,
            }
        except Exception as e:
            return {
                "OK": False,
                "ACTION": "fetch",
                "URL": call.url,
                "ERROR": f"{type(e).__name__}: {e}",
                "CONTENT": None,
                "TITLE": None,
                "SIZE_CHARS": 0,
            }

        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return {
                "OK": False,
                "ACTION": "fetch",
                "URL": call.url,
                "ERROR": f"Non-text content type: {content_type}",
                "CONTENT": None,
                "TITLE": None,
                "SIZE_CHARS": 0,
            }

        text, title = self._extract_text(resp.text)

        if len(text) > call.max_chars:
            text = text[: call.max_chars] + f"\n[truncated at {call.max_chars} chars]"

        return {
            "OK": True,
            "ACTION": "fetch",
            "URL": call.url,
            "TITLE": title,
            "CONTENT": text,
            "SIZE_CHARS": len(text),
            "ERROR": None,
        }

    # -------------------------
    # HTML → plain text
    # -------------------------

    @staticmethod
    def _extract_text(html: str) -> tuple[str, str]:
        """
        Parse HTML and return (clean_text, page_title).
        Strips scripts, styles, nav, footer, ads.
        """
        soup = BeautifulSoup(html, "lxml")

        # remove noise tags
        for tag in soup(["script", "style", "nav", "footer", "header",
                          "aside", "form", "noscript", "iframe"]):
            tag.decompose()

        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        # prefer article/main body if present
        body = soup.find("article") or soup.find("main") or soup.find("body") or soup

        raw = body.get_text(separator="\n")

        # collapse blank lines
        lines = [ln.strip() for ln in raw.splitlines()]
        lines = [ln for ln in lines if ln]
        text = "\n".join(lines)

        # collapse 3+ consecutive newlines
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text, title


# ==========================================================
# Registry contract
# ==========================================================

TOOL_NAME = "web_search"
TOOL_CLASS = WebSearch


def get_tool() -> WebSearch:
    return WebSearch()
