# buddy/buddy_core/llama_installer.py
# ═══════════════════════════════════════════════════════════
# LLAMA.CPP BINARY INSTALLER
# ═══════════════════════════════════════════════════════════
#
# Downloads the latest prebuilt llama-server binary from the
# official ggml-org/llama.cpp GitHub releases.
#
# Platform detection:
#   macOS arm64   → macos-arm64        (Metal — Apple Silicon)
#   macOS x64     → macos-x64          (Metal — Intel Mac)
#   Linux x64     + GPU → ubuntu-vulkan-x64   (Vulkan, NVIDIA/AMD)
#   Linux x64     CPU   → ubuntu-x64
#   Linux arm64         → ubuntu-arm64
#   Windows x64   + NVIDIA → win-cuda-12.4-x64
#   Windows x64   + AMD    → win-hip-radeon-x64
#   Windows x64   CPU      → win-cpu-x64
#   Windows arm64          → win-cpu-arm64
#
# Install path:  ~/.buddy/bin/llama-server  (llama-server.exe on Windows)
# Skip if binary already found in PATH or ~/.buddy/bin/
#
# Public API:
#   ensure_llama_binary(bin_dir, on_progress) -> Path | None

from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

from buddy.logger.logger import get_logger

logger = get_logger("llama_installer")

_GITHUB_API  = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
_TIMEOUT_CON = 8.0   # connection timeout
_TIMEOUT_READ = 30.0  # read timeout for API calls
_CHUNK        = 65536  # download chunk size


# ═══════════════════════════════════════════════════════════
# Platform detection
# ═══════════════════════════════════════════════════════════


def _detect_platform() -> dict:
    """
    Return a dict describing the current platform and GPU backend.

    Keys:
      system   : "darwin" | "linux" | "windows"
      arch     : "arm64" | "x64"
      gpu      : "metal" | "vulkan" | "cuda" | "hip" | "cpu"
      has_gpu  : bool
    """
    system  = platform.system().lower()
    machine = platform.machine().lower()

    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"

    if system == "darwin":
        return {"system": "darwin", "arch": arch, "gpu": "metal", "has_gpu": True}

    if system == "linux":
        gpu, has_gpu = _detect_linux_gpu()
        return {"system": "linux", "arch": arch, "gpu": gpu, "has_gpu": has_gpu}

    if system == "windows":
        gpu, has_gpu = _detect_windows_gpu()
        return {"system": "windows", "arch": arch, "gpu": gpu, "has_gpu": has_gpu}

    # Unknown — CPU fallback
    return {"system": system, "arch": arch, "gpu": "cpu", "has_gpu": False}


def _cmd_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run_silent(cmd: list[str]) -> str:
    """Run a command, return stdout stripped, empty string on failure."""
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _detect_linux_gpu() -> tuple[str, bool]:
    # NVIDIA via nvidia-smi → Vulkan backend (no separate Linux CUDA binary in releases)
    if _cmd_exists("nvidia-smi") and _run_silent(["nvidia-smi", "-L"]):
        return "vulkan", True
    # AMD via rocm-smi
    if _cmd_exists("rocm-smi"):
        return "vulkan", True
    # Generic Vulkan check (lspci)
    lspci = _run_silent(["lspci"])
    if lspci and any(k in lspci.lower() for k in ("nvidia", "amd", "radeon", "intel")):
        return "vulkan", True
    return "cpu", False


def _detect_windows_gpu() -> tuple[str, bool]:
    # NVIDIA
    if _cmd_exists("nvidia-smi") and _run_silent(["nvidia-smi", "-L"]):
        return "cuda", True
    # AMD ROCm/HIP on Windows
    if _cmd_exists("hipcc") or _run_silent(["wmic", "path", "win32_VideoController",
                                            "get", "name"]).lower().find("radeon") != -1:
        return "hip", True
    return "cpu", False


# ═══════════════════════════════════════════════════════════
# Asset selection
# ═══════════════════════════════════════════════════════════

# Maps (system, arch, gpu) → substring patterns to match in asset filename.
# Ordered from most specific to least — first match wins.
_ASSET_PATTERNS: list[tuple[tuple, list[str]]] = [
    # macOS Apple Silicon — prefer kleidiai (optimized ARM)
    (("darwin", "arm64", "metal"),   ["macos-arm64-kleidiai", "macos-arm64"]),
    # macOS Intel
    (("darwin", "x64",   "metal"),   ["macos-x64"]),
    # Linux Vulkan (NVIDIA or AMD)
    (("linux",  "x64",   "vulkan"),  ["ubuntu-vulkan-x64"]),
    # Linux CPU x64
    (("linux",  "x64",   "cpu"),     ["ubuntu-x64"]),
    # Linux ARM64
    (("linux",  "arm64", "cpu"),     ["ubuntu-arm64"]),
    (("linux",  "arm64", "vulkan"),  ["ubuntu-vulkan-arm64", "ubuntu-arm64"]),
    # Windows NVIDIA CUDA
    (("windows","x64",   "cuda"),    ["win-cuda-12.4-x64", "win-cuda-13.1-x64",
                                      "win-cuda"]),
    # Windows AMD HIP
    (("windows","x64",   "hip"),     ["win-hip-radeon-x64"]),
    # Windows CPU x64
    (("windows","x64",   "cpu"),     ["win-cpu-x64"]),
    # Windows ARM64
    (("windows","arm64", "cpu"),     ["win-cpu-arm64"]),
]


def _pick_asset(assets: list[dict], plat: dict) -> Optional[dict]:
    """
    Choose the best asset from a GitHub release's asset list.
    Returns the asset dict or None if nothing matches.
    """
    key = (plat["system"], plat["arch"], plat["gpu"])

    # Find patterns for this platform key
    patterns: list[str] = []
    for (sys_, arch_, gpu_), pats in _ASSET_PATTERNS:
        if sys_ == key[0] and arch_ == key[1] and gpu_ == key[2]:
            patterns = pats
            break

    if not patterns:
        # Fallback: try cpu
        fallback_key = (key[0], key[1], "cpu")
        for (sys_, arch_, gpu_), pats in _ASSET_PATTERNS:
            if (sys_, arch_, gpu_) == fallback_key:
                patterns = pats
                break

    asset_names = {a["name"]: a for a in assets}

    for pat in patterns:
        for name, asset in asset_names.items():
            # Skip xcframework and cudart helpers
            if "xcframework" in name or "cudart" in name:
                continue
            if pat in name:
                return asset

    return None


# ═══════════════════════════════════════════════════════════
# Download + extract
# ═══════════════════════════════════════════════════════════


def _download(url: str, dest: Path, on_progress: Optional[Callable]) -> None:
    """Stream-download url → dest, calling on_progress(downloaded, total) each chunk."""
    resp = requests.get(
        url,
        stream=True,
        timeout=(_TIMEOUT_CON, None),  # no read timeout on large files
        headers={"User-Agent": "buddy-llama-installer/1.0"},
    )
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with dest.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=_CHUNK):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if on_progress:
                    on_progress(downloaded, total)


def _find_binary_in_dir(extract_dir: Path, is_windows: bool) -> Optional[Path]:
    """
    Locate the llama-server binary inside an extracted archive directory.
    Searches recursively; returns the first match.
    """
    target = "llama-server.exe" if is_windows else "llama-server"
    for p in extract_dir.rglob(target):
        if p.is_file():
            return p
    return None


def _extract_archive(archive: Path, extract_dir: Path, is_windows: bool) -> Optional[Path]:
    """Extract archive and return path to llama-server binary."""
    extract_dir.mkdir(parents=True, exist_ok=True)

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(extract_dir)
    elif archive.name.endswith(".tar.gz") or archive.name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(extract_dir)
    else:
        logger.warning("Unknown archive format: %s", archive.name)
        return None

    return _find_binary_in_dir(extract_dir, is_windows)


# ═══════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════


def find_existing_binary(bin_dir: Path) -> Optional[Path]:
    """
    Return path to llama-server if already downloaded to bin_dir.
    Only checks ~/.buddy/data/bin/ — PATH is ignored so Buddy always
    uses its own managed binary.
    """
    is_windows = sys.platform == "win32"
    exe = "llama-server.exe" if is_windows else "llama-server"
    local = bin_dir / exe
    if local.exists() and local.stat().st_size > 0:
        return local
    return None


def ensure_llama_binary(
    bin_dir: Path,
    on_progress: Optional[Callable[[str, bool], None]] = None,
) -> Optional[Path]:
    """
    Ensure llama-server binary is available.

    1. If already in ~/.buddy/bin/ or PATH → return immediately.
    2. Fetch latest GitHub release, pick the right asset for this platform.
    3. Download → extract → install to ~/.buddy/bin/llama-server.
    4. Return Path to binary, or None on failure.

    on_progress(message, is_done) — optional UI callback.
    """
    is_windows = sys.platform == "win32"
    exe        = "llama-server.exe" if is_windows else "llama-server"

    # ── 1. Already present? ────────────────────────────────
    existing = find_existing_binary(bin_dir)
    if existing:
        logger.info("llama-server already present: %s", existing)
        if on_progress:
            on_progress(f"llama-server found: {existing}", True)
        return existing

    # ── 2. Detect platform ─────────────────────────────────
    plat = _detect_platform()
    logger.info(
        "Platform: system=%s arch=%s gpu=%s",
        plat["system"], plat["arch"], plat["gpu"],
    )
    if on_progress:
        on_progress(
            f"Detected: {plat['system']} {plat['arch']} "
            f"({plat['gpu'].upper()})",
            False,
        )

    # ── 3. Fetch latest release ────────────────────────────
    if on_progress:
        on_progress("Fetching latest llama.cpp release info...", False)
    try:
        resp = requests.get(
            _GITHUB_API,
            timeout=(_TIMEOUT_CON, _TIMEOUT_READ),
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "buddy-llama-installer/1.0"},
        )
        resp.raise_for_status()
        release = resp.json()
    except Exception as ex:
        logger.error("Failed to fetch release info: %r", ex)
        if on_progress:
            on_progress(f"Failed to fetch release info: {ex}", True)
        return None

    tag    = release.get("tag_name", "unknown")
    assets = release.get("assets", [])
    logger.info("Latest release: %s  (%d assets)", tag, len(assets))

    # ── 4. Pick matching asset ─────────────────────────────
    asset = _pick_asset(assets, plat)
    if not asset:
        logger.error(
            "No matching asset for platform %s/%s/%s",
            plat["system"], plat["arch"], plat["gpu"],
        )
        if on_progress:
            on_progress(
                f"No prebuilt binary found for {plat['system']}/{plat['arch']}"
                f"/{plat['gpu']}. Install llama-server manually.",
                True,
            )
        return None

    asset_name = asset["name"]
    asset_url  = asset["browser_download_url"]
    asset_size = asset.get("size", 0)
    size_mb    = round(asset_size / (1024 * 1024), 1)

    logger.info("Selected asset: %s  (%.1f MB)", asset_name, size_mb)
    if on_progress:
        on_progress(
            f"Downloading {asset_name}  ({size_mb} MB)  [{tag}]",
            False,
        )

    # ── 5. Download + extract ──────────────────────────────
    bin_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="buddy_llama_") as tmp:
        tmp_path     = Path(tmp)
        archive_path = tmp_path / asset_name
        extract_dir  = tmp_path / "extracted"

        # Download with progress
        last_pct = [-1]

        def _dl_progress(downloaded: int, total: int) -> None:
            if not on_progress or total == 0:
                return
            pct = int(downloaded * 100 / total)
            if pct != last_pct[0] and pct % 10 == 0:
                last_pct[0] = pct
                on_progress(f"  downloading... {pct}%", False)

        try:
            _download(asset_url, archive_path, _dl_progress)
        except Exception as ex:
            logger.error("Download failed: %r", ex)
            if on_progress:
                on_progress(f"Download failed: {ex}", True)
            return None

        # Extract
        binary_in_archive = _extract_archive(archive_path, extract_dir, is_windows)
        if not binary_in_archive:
            logger.error("llama-server not found in archive: %s", asset_name)
            if on_progress:
                on_progress("llama-server binary not found inside archive.", True)
            return None

        # Install
        dest = bin_dir / exe
        shutil.copy2(binary_in_archive, dest)

        # Make executable on Unix
        if not is_windows:
            current = dest.stat().st_mode
            dest.chmod(current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    logger.info("Installed llama-server → %s", dest)
    if on_progress:
        on_progress(f"llama-server installed → {dest}", True)

    return dest


def platform_summary() -> str:
    """Return a human-readable platform + GPU backend string for display."""
    plat = _detect_platform()
    sys_map  = {"darwin": "macOS", "linux": "Linux", "windows": "Windows"}
    gpu_map  = {
        "metal":  "Metal (Apple GPU)",
        "cuda":   "CUDA (NVIDIA)",
        "vulkan": "Vulkan (GPU)",
        "hip":    "HIP (AMD)",
        "cpu":    "CPU only",
    }
    sys_name = sys_map.get(plat["system"], plat["system"].title())
    gpu_name = gpu_map.get(plat["gpu"], plat["gpu"].upper())
    return f"{sys_name} {plat['arch']}  ·  {gpu_name}"
