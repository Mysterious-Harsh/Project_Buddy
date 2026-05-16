# buddy/tools/vision/image_encoder.py
#
# Image path validation and base64 encoding for vision tool.
# Used by brain.run_vision() before passing image_data to llama.cpp.

from __future__ import annotations

import base64
import os
import unicodedata
from pathlib import Path
from typing import List, Optional

_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
)
_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB guard
_URL_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _space_norm(s: str) -> str:
    """Collapse all Unicode space variants (Zs category) to ASCII space."""
    return "".join(" " if unicodedata.category(c) == "Zs" else c for c in s)


def resolve_image_path(raw: str) -> Optional[Path]:
    """
    Resolve a user-supplied image path to an existing Path, or return None.

    Handles:
      - ~ expansion
      - macOS screenshot filenames that use narrow no-break space (U+202F) between
        the time and AM/PM — typed paths use regular space, actual file uses U+202F.
        Resolved by scanning the parent directory with Unicode space normalization.
    """
    p = Path(raw).expanduser().resolve()
    if p.exists():
        return p
    # Space-variant scan: compare filenames after collapsing all Unicode spaces.
    parent = p.parent
    target = _space_norm(p.name)
    if parent.is_dir():
        for entry in parent.iterdir():
            if _space_norm(entry.name) == target:
                return entry
    return None


def is_image_path(token: str) -> bool:
    """
    Quick check: does this string look like an image file path?
    Does NOT check whether the file exists.
    """
    if not token or not isinstance(token, str):
        return False
    ext = Path(token).suffix.lower()
    return ext in _IMAGE_EXTENSIONS


def encode_image(path: str) -> str:
    """
    Read an image file and return its base64-encoded contents (no data-URI prefix).

    Raises:
        FileNotFoundError  — path does not exist
        ValueError         — not a recognized image extension, or exceeds size limit
        OSError            — unreadable file
    """
    p = Path(path).expanduser().resolve()

    if not p.exists():
        raise FileNotFoundError(f"Image not found: {path}")
    if not p.is_file():
        raise ValueError(f"Path is not a file: {path}")

    ext = p.suffix.lower()
    if ext not in _IMAGE_EXTENSIONS:
        raise ValueError(
            f"Unrecognized image extension '{ext}'. Supported:"
            f" {sorted(_IMAGE_EXTENSIONS)}"
        )

    size = p.stat().st_size
    if size == 0:
        raise ValueError(f"Image file is empty: {path}")
    if size > _MAX_SIZE_BYTES:
        raise ValueError(
            f"Image too large ({size / 1_048_576:.1f} MB). Max allowed: 20 MB."
        )

    with open(p, "rb") as f:
        data = f.read()

    return base64.b64encode(data).decode("utf-8")


def encode_image_to_data_uri(path: str) -> str:
    """
    Read an image file and return a data URI for /v1/chat/completions image_url.

    JPEG/PNG  → read bytes directly, no conversion.
    All other formats (WEBP, GIF, BMP, TIFF, ...) → convert to PNG via Pillow:
      - Animated formats: first frame only.
      - Transparency preserved: RGBA if alpha channel present, RGB otherwise.

    Raises:
        FileNotFoundError / ValueError / OSError  — same as encode_image()
        ImportError  — non-JPEG/PNG image but Pillow not installed
    """
    p = Path(path).expanduser().resolve()
    ext = p.suffix.lower()

    # JPEG and PNG: pass bytes through directly
    if ext in (".jpg", ".jpeg"):
        return f"data:image/jpeg;base64,{encode_image(path)}"
    if ext == ".png":
        return f"data:image/png;base64,{encode_image(path)}"

    # All other formats: validate first, then convert via Pillow
    # encode_image() runs all size/existence/extension checks
    encode_image(path)  # validation only — we discard the return value

    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise ImportError(
            f"Pillow is required to convert {ext!r} images to PNG. "
            "Install with: pip install Pillow"
        )

    import io

    with Image.open(str(p)) as img:
        # Animated formats (GIF, WEBP): use first frame only
        try:
            img.seek(0)
        except EOFError:
            pass

        # Preserve alpha channel if present, otherwise strip to RGB
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        )
        target_mode = "RGBA" if has_alpha else "RGB"
        if img.mode != target_mode:
            img = img.convert(target_mode)

        buf = io.BytesIO()
        img.save(buf, format="PNG")

    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def is_image_url(url: str) -> bool:
    """
    Quick check: does this URL point directly to an image file?
    Checks file extension only — no network request.
    """
    if not url or not isinstance(url, str):
        return False
    if not url.startswith(("http://", "https://")):
        return False
    from urllib.parse import urlparse
    path = urlparse(url).path.lower()
    return Path(path).suffix in _IMAGE_EXTENSIONS


def encode_url_to_data_uri(url: str) -> str:
    """
    Download an image from a URL and return a data URI for /v1/chat/completions.

    JPEG/PNG  → pass bytes through directly.
    WebP/GIF/BMP/TIFF/unknown → convert to PNG via Pillow.

    Raises:
        requests.HTTPError  — non-2xx HTTP response
        ValueError          — empty body or image exceeds 20 MB
        ImportError         — non-JPEG/PNG but Pillow not installed
    """
    import io

    import requests as _requests

    resp = _requests.get(
        url,
        timeout=15,
        headers={"User-Agent": _URL_USER_AGENT},
        allow_redirects=True,
    )
    resp.raise_for_status()

    data = resp.content
    if not data:
        raise ValueError(f"Empty response from {url}")
    if len(data) > _MAX_SIZE_BYTES:
        raise ValueError(
            f"Image too large ({len(data) / 1_048_576:.1f} MB). Max: 20 MB."
        )

    ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()

    if ct in ("image/jpeg", "image/jpg"):
        return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
    if ct == "image/png":
        return f"data:image/png;base64,{base64.b64encode(data).decode()}"

    # WebP, GIF, BMP, TIFF, or unknown content-type — convert via Pillow
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise ImportError(
            f"Pillow is required to convert remote images (content-type: {ct!r}). "
            "Install with: pip install Pillow"
        )

    with Image.open(io.BytesIO(data)) as img:
        try:
            img.seek(0)
        except EOFError:
            pass
        has_alpha = img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        )
        target_mode = "RGBA" if has_alpha else "RGB"
        if img.mode != target_mode:
            img = img.convert(target_mode)
        buf = io.BytesIO()
        img.save(buf, format="PNG")

    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"


def extract_image_paths(text: str) -> List[str]:
    """
    Scan a message string for tokens that look like existing image file paths.

    Strategy (in order):
      1. Quoted paths — anything inside "..." or '...' that has an image extension.
      2. Whitespace-split tokens for simple no-space paths.
    Paths with spaces (e.g. macOS screenshots) are only caught via quoting.

    Returns deduplicated resolved absolute path strings (may be empty).
    """
    import re
    if not text:
        return []

    seen: List[str] = []

    def _try(raw: str) -> None:
        raw = raw.strip("\"'(),;")
        if not raw or not is_image_path(raw):
            return
        try:
            p = resolve_image_path(raw)
            if p is not None and p.is_file() and str(p) not in seen:
                seen.append(str(p))
        except Exception:
            pass

    # Pass 1: quoted strings — catches paths with spaces
    for m in re.finditer(r'["\']([^"\']+)["\']', text):
        _try(m.group(1))

    # Pass 2: whitespace-split tokens — catches simple no-space paths
    for token in text.split():
        _try(token)

    return seen
