from buddy.tools.web.fetch import WebFetch
import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import certifi
import requests
from pydantic import BaseModel, Field, model_validator

from buddy.logger.logger import get_logger
from buddy.prompts.web_fetch_prompts import WEB_FETCH_TOOL_PROMPT

logger = get_logger("web_fetch")

# ==========================================================
# Constants
# ==========================================================

_MAX_URLS = 5
_DEFAULT_MAX_CHARS = 8_000
_MAX_CHARS_HARD_LIMIT = 20_000
_FETCH_TIMEOUT_S = 10
_DOWNLOAD_TIMEOUT_S = 60
_CHUNK_SIZE = 8_192
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_DEFAULT_MAX_IMAGES = 3
_MAX_IMAGES_HARD_LIMIT = 8

# Extensions that indicate the URL itself is an image file (not an HTML page).
_DIRECT_IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
)


# Alt/src substrings that indicate non-content noise images.
_NOISE_PATTERNS = frozenset({
    "pixel",
    "1x1",
    "spacer",
    "blank",
    "logo",
    "icon",
    "avatar",
    "badge",
    "button",
    "sprite",
    "tracking",
    "beacon",
    "/ads/",
    "banner",
    "thumbnail",
    "favicon",
})


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


def fetch_one(url: str, max_chars: int, capture_html: bool = False) -> Dict[str, Any]:
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

    raw_html = resp.text
    content, title = _extract(raw_html, url)

    if not content or len(content) < 50:
        return _err(url, "Empty content — site likely uses JavaScript rendering")

    if len(content) > max_chars:
        content = content[:max_chars] + f"\n[truncated at {max_chars} chars]"

    result: Dict[str, Any] = {
        "url": url,
        "title": title,
        "content": content,
        "size_chars": len(content),
        "error": None,
    }
    if capture_html:
        result["_html"] = raw_html  # stripped by _execute_fetch before returning
    return result


print(fetch_one("https://ca.finance.yahoo.com/quote/AMD/", 8000))
