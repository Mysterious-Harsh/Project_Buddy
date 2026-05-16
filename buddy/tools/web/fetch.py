from __future__ import annotations

# ==========================================================
# fetch.py  —  v1.3.0
#
# Web Fetch tool — full page extraction + binary file download.
# fetch:    extract readable text from HTML pages.
# download: save binary/arbitrary files to disk.
# ==========================================================

import asyncio
import random
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import certifi
import httpx
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
_DEFAULT_MAX_IMAGES = 3
_MAX_IMAGES_HARD_LIMIT = 8

_USER_AGENTS = [
    # Chrome — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome — Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari — macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Edge — Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Chrome — Android
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36",
]

# Extensions that indicate the URL itself is an image file (not an HTML page).
_DIRECT_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"
})

# Alt/src substrings that indicate non-content noise images.
_NOISE_PATTERNS = frozenset({
    "pixel", "1x1", "spacer", "blank", "logo", "icon", "avatar",
    "badge", "button", "sprite", "tracking", "beacon", "/ads/",
    "banner", "thumbnail", "favicon",
})


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _build_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        verify=certifi.where(),
        follow_redirects=True,
        timeout=httpx.Timeout(connect=10.0, read=timeout, write=10.0, pool=10.0),
    )


# ==========================================================
# Input model
# ==========================================================


class WebFetchCall(BaseModel):
    urls: List[str] = Field(..., min_length=1)
    max_chars: int = Field(default=_DEFAULT_MAX_CHARS)
    visual_analysis: bool = Field(default=False)
    max_images: int = Field(default=_DEFAULT_MAX_IMAGES)

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
        self.max_images = max(1, min(self.max_images, _MAX_IMAGES_HARD_LIMIT))
        return self


class WebDownloadCall(BaseModel):
    url: str
    dest_path: str
    overwrite: bool = False

    @model_validator(mode="after")
    def _validate(self) -> "WebDownloadCall":
        self.url = self.url.strip()
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid URL (must start with http/https): {self.url}")
        self.dest_path = self.dest_path.strip()
        if not self.dest_path:
            raise ValueError("dest_path must not be empty")
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
            "version": "1.3.0",
            "description": (
                "WHEN: a URL is already known — from the task, memory, or a prior web_search step — and full page content is needed.\n\n"
                "FUNCTIONS:\n"
                "  fetch(url, selector?)     — returns full readable text from the page; selector= CSS selector to extract a specific section\n"
                "  download(url, path)       — download a binary file (PDF, image, zip) to disk at given absolute path\n\n"
                "CHAIN: receives URLs from web_search output. fetch text output feeds analyzer to extract specific data, or feeds responder directly. "
                "download output path feeds fs_read or word/pdf for further processing.\n"
                "NOT: URL unknown → web_search first | multi-step interaction (login, forms, clicking) → browser"
            ),
            "prompt": WEB_FETCH_TOOL_PROMPT,
        }

    def parse_call(self, payload: Dict[str, Any]) -> WebFetchCall:
        return WebFetchCall.model_validate(payload)

    async def execute(
        self,
        function: str = "",
        arguments: Dict[str, Any] = {},
        on_progress: Optional[Callable[[str, bool], None]] = None,
        goal: str = "",
        brain: Optional[Any] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        if function == "download":
            return await self._execute_download(arguments, on_progress)
        return await self._execute_fetch(arguments, on_progress, goal, brain)

    # ── fetch ─────────────────────────────────────────────

    async def _execute_fetch(
        self,
        arguments: Dict[str, Any],
        on_progress: Optional[Callable[[str, bool], None]],
        goal: str,
        brain: Any,
    ) -> Dict[str, Any]:
        try:
            call = self.parse_call(arguments)
        except Exception as e:
            urls = arguments.get("urls") or []
            return {
                "STATUS": "failed",
                "RESULTS": [],
                "TOTAL_FETCHED": 0,
                "ERROR": f"web_fetch.fetch: invalid arguments — {e}. URLs attempted: {urls}",
            }

        from buddy.brain.text_reader import maybe_read

        results: List[Dict[str, Any]] = []
        fetched = 0

        async with _build_client(_FETCH_TIMEOUT_S) as client:
            for url in call.urls:
                if on_progress:
                    on_progress(f"Fetching: {url}", False)

                # Direct image URL — skip HTML entirely, go straight to vision.
                if call.visual_analysis and brain and _is_direct_image_url(url):
                    result = await self._fetch_direct_image(url, goal, brain, on_progress)
                    results.append(result)
                    if result["error"] is None:
                        fetched += 1
                    continue

                # HTML fetch — optionally capture raw HTML for image extraction.
                result = await self._fetch_one(client, url, call.max_chars, call.visual_analysis)

                # LLM reading loop — always run for web content (noisy by nature).
                if result["error"] is None and result.get("content") and goal and brain:
                    result["content"] = await asyncio.to_thread(
                        maybe_read, result["content"], goal, brain, on_progress,
                        force_read=True,
                    )
                    result["size_chars"] = len(result["content"])

                # Visual analysis — extract image candidates from captured HTML.
                if call.visual_analysis and brain and result["error"] is None:
                    raw_html = result.pop("_html", None)
                    visual_findings: List[Dict[str, Any]] = []
                    if raw_html:
                        candidates = _extract_image_candidates(
                            raw_html, url, goal, call.max_images
                        )
                        for c in candidates:
                            if on_progress:
                                on_progress(
                                    f"Analysing image: {c['context'] or 'image'}", False
                                )
                            vf = await self._analyse_image(
                                c["img_url"], url, goal, brain, c["context"]
                            )
                            if vf:
                                visual_findings.append(vf)
                    result["visual_findings"] = visual_findings
                else:
                    result.pop("_html", None)

                results.append(result)
                if result["error"] is None:
                    fetched += 1

        return {
            "STATUS": "success" if fetched > 0 else "failed",
            "RESULTS": results,
            "TOTAL_FETCHED": fetched,
            **({"ERROR": "All URLs failed to fetch"} if fetched == 0 else {}),
        }

    # ── download ──────────────────────────────────────────

    async def _execute_download(
        self,
        arguments: Dict[str, Any],
        on_progress: Optional[Callable[[str, bool], None]],
    ) -> Dict[str, Any]:
        try:
            call = WebDownloadCall.model_validate(arguments)
        except Exception as e:
            return {
                "STATUS": "failed",
                "URL": arguments.get("url", ""),
                "DEST_PATH": "",
                "SIZE_BYTES": 0,
                "ERROR": str(e),
            }
        return await self._download_one(call, on_progress)

    async def _download_one(
        self,
        call: WebDownloadCall,
        on_progress: Optional[Callable[[str, bool], None]],
    ) -> Dict[str, Any]:
        dest = Path(call.dest_path).expanduser()

        if dest.is_dir():
            raw = urlparse(call.url).path.rstrip("/").split("/")[-1] or "download"
            dest = dest / raw

        if dest.exists() and not call.overwrite:
            return _dl_err(call.url, dest, f"File already exists: {dest}. Set overwrite=true to replace it.")

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return _dl_err(call.url, dest, f"Cannot create directory: {e}")

        if on_progress:
            on_progress(f"Downloading: {call.url}", False)

        try:
            async with _build_client(_DOWNLOAD_TIMEOUT_S) as client:
                async with client.stream(
                    "GET",
                    call.url,
                    headers={"User-Agent": _random_ua()},
                ) as resp:
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "unknown")
                    size = 0
                    try:
                        with dest.open("wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=_CHUNK_SIZE):
                                if chunk:
                                    f.write(chunk)
                                    size += len(chunk)
                    except OSError as e:
                        return _dl_err(call.url, dest, f"Write error: {e}")
        except httpx.TimeoutException:
            return _dl_err(call.url, dest, "Request timed out")
        except httpx.HTTPStatusError as e:
            return _dl_err(call.url, dest, f"HTTP {e.response.status_code}: {e.response.reason_phrase}")
        except Exception as e:
            return _dl_err(call.url, dest, f"{type(e).__name__}: {e}")

        return {
            "STATUS": "success",
            "URL": call.url,
            "DEST_PATH": str(dest),
            "SIZE_BYTES": size,
            "CONTENT_TYPE": content_type,
        }

    # ── Visual analysis helpers ───────────────────────────

    async def _fetch_direct_image(
        self,
        url: str,
        goal: str,
        brain: Any,
        on_progress: Optional[Callable[[str, bool], None]],
    ) -> Dict[str, Any]:
        """URL is itself an image — skip HTML parsing, analyse directly."""
        if on_progress:
            on_progress(f"Analysing image: {url}", False)
        vf = await self._analyse_image(url, url, goal, brain, "")
        return {
            "url": url,
            "title": "",
            "content": "",
            "size_chars": 0,
            "error": None,
            "visual_findings": [vf] if vf else [],
        }

    async def _analyse_image(
        self,
        img_url: str,
        source_page: str,
        goal: str,
        brain: Any,
        context: str,
    ) -> Optional[Dict[str, Any]]:
        """Run vision on one image URL and return a visual_findings entry."""
        try:
            finding = await asyncio.to_thread(
                brain.run_vision,
                image_paths=[img_url],
                query=goal or "Describe this image in detail.",
            )
            if "error" in finding:
                logger.warning(
                    "visual analysis failed url=%r err=%r", img_url, finding["error"]
                )
                return None
            return {
                "img_url": img_url,
                "context": context,
                "source_page": source_page,
                "objects": finding.get("objects") or [],
                "text_found": finding.get("text_found") or "",
                "finding": finding.get("key_finding") or "",
            }
        except Exception as exc:
            logger.warning("visual analysis exception url=%r err=%r", img_url, exc)
            return None

    # ── Single URL ────────────────────────────────────────

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        url: str,
        max_chars: int,
        capture_html: bool = False,
    ) -> Dict[str, Any]:
        try:
            resp = await client.get(url, headers={"User-Agent": _random_ua()})
            resp.raise_for_status()
        except httpx.TimeoutException:
            return _err(url, "Request timed out")
        except httpx.HTTPStatusError as e:
            return _err(url, f"HTTP {e.response.status_code}: {e.response.reason_phrase}")
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


# ==========================================================
# Helpers
# ==========================================================


def _is_direct_image_url(url: str) -> bool:
    """True if the URL path has a recognised image file extension."""
    ext = Path(urlparse(url).path.lower()).suffix
    return ext in _DIRECT_IMAGE_EXTS


def _extract_image_candidates(
    html: str, base_url: str, goal: str, max_images: int
) -> List[Dict[str, Any]]:
    """
    Parse HTML and return the top-N image candidates most relevant to goal.

    Each entry: {img_url, context, score}
    Noise images (icons, trackers, tiny by size attribute) are filtered out.
    Relevance is scored by word overlap between context (alt/figcaption/heading)
    and the goal query.
    """
    from urllib.parse import urljoin

    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        return []

    soup = BeautifulSoup(html, "lxml")
    goal_words = set(goal.lower().split()) if goal else set()
    candidates: List[Dict[str, Any]] = []

    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-lazy-src")
            or ""
        )
        if not src or src.startswith("data:"):
            continue

        src = urljoin(base_url, src)
        if not src.startswith(("http://", "https://")):
            continue

        # Extension must be a known image format (or absent — some CDN URLs have none)
        ext = Path(urlparse(src).path.lower()).suffix
        if ext and ext not in _DIRECT_IMAGE_EXTS:
            continue

        # Size filter — skip images explicitly declared tiny
        try:
            w = int(img.get("width") or 0)
            h = int(img.get("height") or 0)
            if (w and w < 100) or (h and h < 100):
                continue
        except (ValueError, TypeError):
            pass

        # Noise filter on URL and alt
        src_lower = src.lower()
        if any(p in src_lower for p in _NOISE_PATTERNS):
            continue

        # Build context string from the richest available signal
        alt        = (img.get("alt") or "").strip()
        title_attr = (img.get("title") or "").strip()

        figcaption = ""
        parent_fig = img.find_parent("figure")
        if parent_fig:
            fc = parent_fig.find("figcaption")
            if fc:
                figcaption = fc.get_text(strip=True)[:120]

        heading = ""
        for el in img.find_all_previous(["h1", "h2", "h3", "h4"]):
            heading = el.get_text(strip=True)[:80]
            break

        context = figcaption or alt or title_attr or heading or ""

        # Relevance score — word overlap with goal query
        score = 0.0
        if goal_words and context:
            ctx_words = set(context.lower().split())
            score = len(goal_words & ctx_words) / max(len(goal_words), 1)

        candidates.append({"img_url": src, "context": context, "score": score})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:max_images]


def _err(url: str, msg: str) -> Dict[str, Any]:
    logger.warning("web_fetch error [%s]: %s", url, msg)
    return {"url": url, "title": None, "content": None, "size_chars": 0, "error": msg}


def _dl_err(url: str, dest: "Path", msg: str) -> Dict[str, Any]:
    logger.warning("web_download error [%s]: %s", url, msg)
    return {"STATUS": "failed", "URL": url, "DEST_PATH": str(dest), "SIZE_BYTES": 0, "ERROR": msg}


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
