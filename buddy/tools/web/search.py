from __future__ import annotations

# ==========================================================
# search.py  —  v3.0.0
#
# Web Search tool — search only.
# Returns title, URL, and short snippet (≤400 chars) per result.
#
# Engine: SearXNG (self-hosted) or DuckDuckGo — toggled via buddy.toml.
# For full page content use the separate web_fetch tool.
# ==========================================================

import tomllib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from buddy.logger.logger import get_logger
from buddy.prompts.web_search_prompts import WEB_SEARCH_TOOL_PROMPT

logger = get_logger("web_search")

# ==========================================================
# Constants
# ==========================================================

_MAX_RESULTS_HARD_LIMIT = 20
_DEFAULT_MAX_RESULTS = 5
_SNIPPET_CAP = 400
_SEARXNG_TIMEOUT_S = 8
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# SearXNG built-in categories — these map to engine configs; unknown values return 0 results.
KNOWN_CATEGORIES = frozenset({
    "general", "images", "videos", "news", "map",
    "music", "it", "science", "files", "social media",
})

# SearXNG time_range values — unknown values return 0 results.
KNOWN_TIME_RANGES = frozenset({"day", "week", "month", "year"})


def _user_config_path() -> Path:
    """Resolve ~/.buddy/config/buddy.toml (user data dir, platform-aware)."""
    import os as _os

    if _os.name == "nt":
        base = _os.environ.get("LOCALAPPDATA") or _os.environ.get("APPDATA")
        root = (Path(base) / "Buddy") if base else (Path.home() / "Buddy")
    else:
        root = Path.home() / ".buddy"
    return root / "config" / "buddy.toml"


# ==========================================================
# Config
# ==========================================================


def _load_config() -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "engine": "duckduckgo",
        "searxng_url": "http://127.0.0.1:8888",
    }
    try:
        with _user_config_path().open("rb") as f:
            cfg = tomllib.load(f)
        defaults.update(cfg.get("web_search", {}))
    except Exception:
        pass
    return defaults


# ==========================================================
# Input model
# ==========================================================


class WebSearchCall(BaseModel):
    query: str = Field(..., min_length=1)
    max_results: int = Field(default=_DEFAULT_MAX_RESULTS)
    region: str = Field(default="wt-wt")
    safe_search: bool = Field(default=True)
    categories: str = Field(default="general")
    time_range: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def _validate(self) -> "WebSearchCall":
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("query must not be empty")
        self.max_results = max(1, min(self.max_results, _MAX_RESULTS_HARD_LIMIT))
        self.categories = self.categories.strip().lower()
        if self.categories not in KNOWN_CATEGORIES:
            raise ValueError(
                f"Unknown category {self.categories!r}. "
                f"Valid: {sorted(KNOWN_CATEGORIES)}"
            )
        if self.time_range is not None:
            self.time_range = self.time_range.strip().lower()
            if self.time_range not in KNOWN_TIME_RANGES:
                raise ValueError(
                    f"Unknown time_range {self.time_range!r}. "
                    f"Valid: {sorted(KNOWN_TIME_RANGES)}"
                )
        return self


# ==========================================================
# Tool
# ==========================================================


class WebSearch:
    """
    Web Search tool — returns titles, URLs, and short snippets.
    Use web_fetch for full page content.
    """

    def get_info(self) -> Dict[str, Any]:
        cfg = _load_config()
        return {
            "name": TOOL_NAME,
            "version": "3.1.0",
            "description": (
                "WHEN: URLs for a topic are unknown and need to be discovered before fetching content.\n\n"
                "FUNCTIONS:\n"
                "  search(query, num_results?)   — returns list of {url, title, snippet}; snippets are ≤400 chars — never sufficient alone to answer a real query\n\n"
                "CHAIN: always follow with web_fetch — pass the returned URLs to fetch full page content. "
                "Exception: if the task only needs a list of links/URLs, search alone is enough.\n"
                "NOT: URL already known → skip directly to web_fetch | login/form/click tasks → browser | reading full page content alone → always pair with web_fetch"
            ),
            "engine": cfg.get("engine", "duckduckgo"),
            "prompt": WEB_SEARCH_TOOL_PROMPT,
        }

    def parse_call(self, payload: Dict[str, Any]) -> WebSearchCall:
        return WebSearchCall.model_validate(payload)

    async def execute(
        self,
        function: str = "",
        arguments: Dict[str, Any] = {},
        on_progress: Optional[Callable[[str, bool], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        try:
            call = self.parse_call(arguments)
        except Exception as e:
            query = str(arguments.get("query") or "").strip()
            return {"STATUS": "failed", "QUERY": query, "RESULTS": [], "TOTAL_FOUND": 0, "ERROR": f"Invalid search arguments: {e}"}

        if on_progress:
            on_progress(f"Searching: {call.query}", False)

        cfg = _load_config()
        engine = cfg.get("engine", "duckduckgo")

        if engine == "searxng":
            return self._searxng(call, cfg)
        return self._ddg(call)

    # ── SearXNG ───────────────────────────────────────────

    def _searxng(self, call: WebSearchCall, cfg: Dict[str, Any]) -> Dict[str, Any]:
        import requests

        base = cfg.get("searxng_url", "http://127.0.0.1:8888").rstrip("/")
        params: Dict[str, Any] = {
            "q": call.query,
            "format": "json",
            "safesearch": "1" if call.safe_search else "0",
            "language": call.region,
            "categories": call.categories,
        }
        if call.time_range:
            params["time_range"] = call.time_range

        try:
            resp = requests.get(
                f"{base}/search",
                params=params,
                timeout=_SEARXNG_TIMEOUT_S,
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            raw = resp.json().get("results", [])
        except Exception as e:
            logger.warning("SearXNG failed, falling back to DDG: %r", e)
            return self._ddg(call)

        results = [_format_result(item, call.categories) for item in raw[: call.max_results]]
        out: Dict[str, Any] = {
            "STATUS": "success",
            "QUERY": call.query,
            "RESULTS": results,
            "TOTAL_FOUND": len(results),
        }
        if call.time_range:
            out["TIME_RANGE"] = call.time_range
        return out

    # ── DuckDuckGo ────────────────────────────────────────

    def _ddg(self, call: WebSearchCall) -> Dict[str, Any]:
        try:
            from ddgs import DDGS  # type: ignore

            with DDGS() as ddgs:
                raw = list(
                    ddgs.text(
                        call.query,
                        region=call.region,
                        safesearch="moderate" if call.safe_search else "off",
                        max_results=call.max_results,
                    )
                )
        except Exception as e:
            return {
                "STATUS": "failed",
                "ENGINE": "duckduckgo",
                "QUERY": call.query,
                "RESULTS": [],
                "TOTAL_FOUND": 0,
                "ERROR": f"{type(e).__name__}: {e}",
            }

        results = [
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("href") or item.get("url") or ""),
                "snippet": str(item.get("body") or "")[:_SNIPPET_CAP],
            }
            for item in raw
        ]
        return {
            "STATUS": "success",
            "QUERY": call.query,
            "RESULTS": results,
            "TOTAL_FOUND": len(results),
        }


# ==========================================================
# Helpers
# ==========================================================


def _format_result(item: Dict[str, Any], categories: str) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "title": str(item.get("title") or ""),
        "url": str(item.get("url") or ""),
        "snippet": str(item.get("content") or "")[:_SNIPPET_CAP],
    }
    if categories == "images":
        img_src = str(item.get("img_src") or "")
        thumbnail = str(item.get("thumbnail_src") or item.get("thumbnail") or "")
        resolution = str(item.get("resolution") or "")
        img_format = str(item.get("img_format") or "")
        if img_src:
            base["img_src"] = img_src
        if thumbnail:
            base["thumbnail"] = thumbnail
        if resolution:
            base["resolution"] = resolution
        if img_format:
            base["img_format"] = img_format
    return base


# ==========================================================
# Registry contract
# ==========================================================

TOOL_NAME = "web_search"
TOOL_CLASS = WebSearch


def get_tool() -> WebSearch:
    return WebSearch()
