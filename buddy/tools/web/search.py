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
from buddy.prompts.web_search_prompts import (
    WEB_SEARCH_ERROR_RECOVERY_PROMPT,
    WEB_SEARCH_TOOL_PROMPT,
    tool_call_format,
)

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

    @model_validator(mode="after")
    def _validate(self) -> "WebSearchCall":
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("query must not be empty")
        self.max_results = max(1, min(self.max_results, _MAX_RESULTS_HARD_LIMIT))
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
            "version": "3.0.0",
            "description": (
                "Search the web. Returns title, URL, and a short snippet (≤400 chars)"
                " per result. Snippets are ONLY sufficient for understanding the"
                " context. you MUST add a web_fetch step AFTER this step and pass these"
                " URLs as input. Do NOT skip web_fetch when the user needs actual"
                " content."
            ),
            "engine": cfg.get("engine", "duckduckgo"),
            "prompt": WEB_SEARCH_TOOL_PROMPT,
            "error_prompt": WEB_SEARCH_ERROR_RECOVERY_PROMPT,
            "tool_call_format": tool_call_format,
        }

    def parse_call(self, payload: Dict[str, Any]) -> WebSearchCall:
        return WebSearchCall.model_validate(payload)

    async def execute(
        self,
        call: WebSearchCall,
        *,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
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
        try:
            resp = requests.get(
                f"{base}/search",
                params={
                    "q": call.query,
                    "format": "json",
                    "safesearch": "1" if call.safe_search else "0",
                    "language": call.region,
                },
                timeout=_SEARXNG_TIMEOUT_S,
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            raw = resp.json().get("results", [])
        except Exception as e:
            logger.warning("SearXNG failed, falling back to DDG: %r", e)
            return self._ddg(call)

        results = [
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or "")[:_SNIPPET_CAP],
            }
            for item in raw[: call.max_results]
        ]
        return {
            "OK": True,
            "ENGINE": "searxng",
            "QUERY": call.query,
            "RESULTS": results,
            "TOTAL_FOUND": len(results),
            "ERROR": None,
        }

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
                "OK": False,
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
            "OK": True,
            "ENGINE": "duckduckgo",
            "QUERY": call.query,
            "RESULTS": results,
            "TOTAL_FOUND": len(results),
            "ERROR": None,
        }


# ==========================================================
# Registry contract
# ==========================================================

TOOL_NAME = "web_search"
TOOL_CLASS = WebSearch


def get_tool() -> WebSearch:
    return WebSearch()
