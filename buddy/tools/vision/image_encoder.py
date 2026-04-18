# buddy/tools/vision/image_encoder.py
#
# Image path validation and base64 encoding for vision tool.
# Used by brain.run_vision() before passing image_data to llama.cpp.

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import List

_IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
)
_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB guard


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


def extract_image_paths(text: str) -> List[str]:
    """
    Scan a message string for tokens that look like existing image file paths.

    Tokens are split on whitespace. A token qualifies if:
      - It has a recognized image extension
      - The file actually exists on disk

    Returns a list of resolved absolute path strings (may be empty).
    """
    if not text:
        return []

    found: List[str] = []
    for token in text.split():
        token = token.strip("\"'(),;")
        if not token:
            continue
        if not is_image_path(token):
            continue
        try:
            p = Path(token).expanduser().resolve()
            if p.is_file():
                found.append(str(p))
        except Exception:
            continue

    return found
