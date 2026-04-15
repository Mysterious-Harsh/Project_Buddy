# buddy/tools/vision/image_encoder.py
#
# Image path validation and base64 encoding for vision tool.
# Used by brain.run_vision() before passing image_data to llama.cpp.

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import List

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"})
_MAX_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB guard

_MIME_MAP: dict = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".bmp":  "image/bmp",
    ".tiff": "image/tiff",
    ".tif":  "image/tiff",
}


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
            f"Unrecognized image extension '{ext}'. Supported: {sorted(_IMAGE_EXTENSIONS)}"
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

    return base64.b64encode(data).decode("ascii")


def encode_image_to_data_uri(path: str) -> str:
    """
    Read an image file and return a data URI: data:image/png;base64,...

    Used by llama_client.chat(images=[...]) for the OAI /v1/chat/completions
    multimodal format. Reuses encode_image() for all validation.
    """
    p = Path(path).expanduser().resolve()
    mime = _MIME_MAP.get(p.suffix.lower(), "image/jpeg")
    b64 = encode_image(path)
    return f"data:{mime};base64,{b64}"


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
