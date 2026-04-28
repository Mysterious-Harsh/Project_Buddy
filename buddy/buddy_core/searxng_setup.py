# buddy/buddy_core/searxng_setup.py
# ═══════════════════════════════════════════════════════════
# SEARXNG SETUP & LIFECYCLE
# ═══════════════════════════════════════════════════════════
#
# Manages a self-hosted SearXNG instance as a local subprocess.
#
# Setup (first time):
#   1. Locate a real Python 3.8+ interpreter (see find_python).
#      If not found and running as frozen binary:
#        → ask user via ask_install_python() callback
#        → if yes, download python-build-standalone (~30MB) to ~/.buddy/python/
#        → use that as the interpreter going forward
#        → if no or download fails, return False → caller falls back to DDG
#   2. Clone searxng/searxng → ~/.buddy/searxng/repo/
#   3. Create venv at ~/.buddy/searxng/venv/
#   4. pip install -e . inside venv
#   5. Write ~/.buddy/searxng/settings.yml
#
# Python detection (find_python):
#   - Not frozen → sys.executable is real Python → return it immediately.
#   - Frozen (PyInstaller/Nuitka): search PATH + common install locations.
#   - Returns None if nothing found.
#
# Bundled Python (install_bundled_python):
#   - Downloads python-build-standalone from GitHub (indygreg/python-build-standalone).
#   - Installs to ~/.buddy/python/ — no admin rights, no system-wide changes.
#   - ~30 MB download, extracts to a self-contained runtime.
#   - Saved path registered so find_python() finds it on next call.
#
# Start:
#   - Skip if already listening on configured port
#   - Spawn subprocess: venv python -m searx.webapp
#   - Probe HTTP until ready (or timeout)
#   - Write PID to ~/.buddy/state/searxng.pid
#
# Stop:
#   - Read PID file, SIGTERM, wait, SIGKILL if needed
#
# Public API:
#   find_python() -> Optional[str]
#   install_bundled_python(python_dir, on_progress) -> Optional[str]
#   setup_searxng(searxng_dir, port, python_dir, ask_install_python, on_progress) -> bool
#   start_searxng(searxng_dir, state_dir, port, on_progress) -> bool
#   stop_searxng(state_dir)
#   is_running(port) -> bool

from __future__ import annotations

import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

from buddy.logger.logger import get_logger

logger = get_logger("searxng_setup")

_REPO_URL       = "https://github.com/searxng/searxng.git"
_PID_FILE       = "searxng.pid"
_LOG_FILE       = "searxng.log"
_READY_TIMEOUT  = 60.0   # seconds to wait for SearXNG to come online
_PROBE_INTERVAL = 0.5
_HTTP           = requests.Session()

# Candidate interpreter names searched in PATH when Buddy is frozen.
_PYTHON_CANDIDATES = ["python3", "python3.12", "python3.11", "python3.10",
                       "python3.9", "python3.8", "python"]

# Common install prefixes checked on each platform when PATH search fails.
_EXTRA_SEARCH_PATHS: list[str] = {
    "Darwin":  ["/usr/bin", "/usr/local/bin", "/opt/homebrew/bin",
                "/opt/homebrew/opt/python3/bin"],
    "Linux":   ["/usr/bin", "/usr/local/bin"],
    "Windows": [
        r"C:\Python312", r"C:\Python311", r"C:\Python310",
        r"C:\Program Files\Python312", r"C:\Program Files\Python311",
        r"C:\Users\Public\AppData\Local\Programs\Python\Python312",
    ],
}.get(platform.system(), [])


# ═══════════════════════════════════════════════════════════
# Python discovery
# ═══════════════════════════════════════════════════════════


def find_python() -> Optional[str]:
    """
    Return a path to a usable Python 3.8+ interpreter, or None.

    Logic:
      - Not frozen → sys.executable is real Python → return it immediately.
      - Frozen (prebuilt binary) → search PATH + common locations.
    """
    is_frozen = getattr(sys, "frozen", False)

    if not is_frozen:
        # Running from source or conda env — sys.executable is Python.
        return sys.executable

    # ── Frozen binary: hunt for system Python ─────────────────
    import shutil

    def _check(path: str) -> bool:
        """Return True if path is an executable Python >= 3.8."""
        try:
            r = subprocess.run(
                [path, "-c",
                 "import sys; v=sys.version_info; "
                 "print('ok') if v>=(3,8) else print('old')"],
                capture_output=True, text=True, timeout=3.0,
            )
            return r.returncode == 0 and "ok" in r.stdout
        except Exception:
            return False

    # 1. Search PATH
    for name in _PYTHON_CANDIDATES:
        found = shutil.which(name)
        if found and _check(found):
            logger.info("Found system Python in PATH: %s", found)
            return found

    # 2. Check extra locations
    is_windows = platform.system() == "Windows"
    exe_name = "python.exe" if is_windows else "python3"
    for prefix in _EXTRA_SEARCH_PATHS:
        candidate = str(Path(prefix) / exe_name)
        if Path(candidate).exists() and _check(candidate):
            logger.info("Found system Python at: %s", candidate)
            return candidate

    logger.warning("No system Python 3.8+ found — SearXNG cannot be set up.")
    return None


# ═══════════════════════════════════════════════════════════
# Bundled Python (python-build-standalone)
# ═══════════════════════════════════════════════════════════

_PBS_API = "https://api.github.com/repos/indygreg/python-build-standalone/releases/latest"
_PBS_CHUNK = 65536

# Maps (system, arch) → substring to match in asset filename.
# We target Python 3.11, install_only variant (smallest usable build).
_PBS_PATTERNS: dict[tuple[str, str], list[str]] = {
    ("darwin",  "arm64"): ["cpython-3.11", "aarch64-apple-darwin",    "install_only"],
    ("darwin",  "x64"):   ["cpython-3.11", "x86_64-apple-darwin",     "install_only"],
    ("linux",   "x64"):   ["cpython-3.11", "x86_64-unknown-linux-gnu","install_only"],
    ("linux",   "arm64"): ["cpython-3.11", "aarch64-unknown-linux-gnu","install_only"],
    ("windows", "x64"):   ["cpython-3.11", "x86_64-pc-windows-msvc",  "install_only"],
}


def _pbs_platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    if system == "darwin":
        sys_key = "darwin"
    elif system == "windows":
        sys_key = "windows"
    else:
        sys_key = "linux"
    machine = platform.machine().lower()
    arch_key = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return sys_key, arch_key


def _pick_pbs_asset(assets: list[dict]) -> Optional[dict]:
    key = _pbs_platform_key()
    patterns = _PBS_PATTERNS.get(key, [])
    if not patterns:
        return None
    for asset in assets:
        name = asset.get("name", "")
        if all(p in name for p in patterns):
            return asset
    return None


def install_bundled_python(
    python_dir: Path,
    on_progress: Optional[Callable[[str, bool], None]] = None,
) -> Optional[str]:
    """
    Download python-build-standalone for this platform and install it to
    python_dir (~/.buddy/python/). No admin rights required.

    Returns path to the python executable, or None on failure.
    """
    def _prog(msg: str, done: bool = False) -> None:
        if on_progress:
            on_progress(msg, done)

    is_windows = platform.system() == "Windows"

    # Check if already installed
    exe = "python.exe" if is_windows else "python3"
    existing = python_dir / "install" / ("" if is_windows else "bin") / exe
    if existing.exists():
        logger.info("Bundled Python already installed: %s", existing)
        _prog(f"Bundled Python already installed: {existing}", True)
        return str(existing)

    _prog("Fetching python-build-standalone release info...", False)
    try:
        resp = requests.get(
            _PBS_API,
            timeout=(8.0, 20.0),
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "buddy-installer/1.0"},
        )
        resp.raise_for_status()
        release = resp.json()
    except Exception as ex:
        logger.error("Failed to fetch PBS release info: %r", ex)
        _prog(f"Failed to fetch Python release info: {ex}", True)
        return None

    asset = _pick_pbs_asset(release.get("assets", []))
    if not asset:
        sys_key, arch_key = _pbs_platform_key()
        _prog(f"No bundled Python found for {sys_key}/{arch_key}.", True)
        return None

    asset_name = asset["name"]
    asset_url  = asset["browser_download_url"]
    size_mb    = round(asset.get("size", 0) / (1024 * 1024), 1)
    _prog(f"Downloading Python runtime ({size_mb} MB)...", False)

    python_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="buddy_python_") as tmp:
        tmp_path     = Path(tmp)
        archive_path = tmp_path / asset_name

        # Stream download
        last_pct = [-1]
        try:
            dl = requests.get(asset_url, stream=True, timeout=(8.0, None),
                              headers={"User-Agent": "buddy-installer/1.0"})
            dl.raise_for_status()
            total = int(dl.headers.get("content-length", 0))
            downloaded = 0
            with archive_path.open("wb") as f:
                for chunk in dl.iter_content(chunk_size=_PBS_CHUNK):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(downloaded * 100 / total)
                            if pct != last_pct[0] and pct % 10 == 0:
                                last_pct[0] = pct
                                _prog(f"  downloading Python... {pct}%", False)
        except Exception as ex:
            logger.error("PBS download failed: %r", ex)
            _prog(f"Python download failed: {ex}", True)
            return None

        # Extract
        _prog("Extracting Python runtime...", False)
        try:
            if asset_name.endswith(".zip"):
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(python_dir)
            else:
                with tarfile.open(archive_path, "r:gz") as tf:
                    tf.extractall(python_dir)
        except Exception as ex:
            logger.error("PBS extraction failed: %r", ex)
            _prog(f"Python extraction failed: {ex}", True)
            return None

    # Locate the executable
    if is_windows:
        py_path = python_dir / "python" / "install" / "python.exe"
    else:
        py_path = python_dir / "python" / "install" / "bin" / "python3"

    if not py_path.exists():
        # Fallback: search recursively
        hits = list(python_dir.rglob("python3" if not is_windows else "python.exe"))
        py_path = hits[0] if hits else None

    if not py_path or not py_path.exists():
        _prog("Python runtime installed but executable not found.", True)
        return None

    # Make executable on Unix
    if not is_windows:
        import stat
        py_path.chmod(py_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    logger.info("Bundled Python installed: %s", py_path)
    _prog(f"Python runtime ready: {py_path}", True)
    return str(py_path)


# ═══════════════════════════════════════════════════════════
# Paths helper
# ═══════════════════════════════════════════════════════════


def _paths(searxng_dir: Path) -> dict:
    return {
        "repo":     searxng_dir / "repo",
        "venv":     searxng_dir / "venv",
        "settings": searxng_dir / "settings.yml",
    }


def _venv_python(searxng_dir: Path) -> Path:
    p = _paths(searxng_dir)
    venv = p["venv"]
    if platform.system() == "Windows":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


# ═══════════════════════════════════════════════════════════
# Status checks
# ═══════════════════════════════════════════════════════════


def is_running(port: int = 8888) -> bool:
    """True if something is listening on the SearXNG port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except Exception:
        return False


def is_ready(port: int = 8888) -> bool:
    """True if SearXNG responds to a JSON search query."""
    try:
        r = _HTTP.get(
            f"http://127.0.0.1:{port}/search",
            params={"q": "test", "format": "json"},
            timeout=(1.0, 3.0),
        )
        return r.status_code == 200
    except Exception:
        return False


def is_installed(searxng_dir: Path) -> bool:
    """True if SearXNG has been cloned and the venv python exists."""
    p = _paths(searxng_dir)
    return (
        (p["repo"] / "searx" / "webapp.py").exists()
        and _venv_python(searxng_dir).exists()
    )


# ═══════════════════════════════════════════════════════════
# Settings file
# ═══════════════════════════════════════════════════════════


def _write_settings(settings_path: Path, port: int) -> None:
    """
    Write a minimal SearXNG settings.yml.
    Uses use_default_settings: true so only overrides are needed.
    Generates a fresh secret_key on first write; preserves existing key on update.
    """
    # Preserve existing secret key if present
    secret_key = None
    if settings_path.exists():
        try:
            text = settings_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "secret_key" in line and ":" in line:
                    secret_key = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass

    if not secret_key:
        secret_key = secrets.token_hex(32)

    content = f"""\
# SearXNG settings — managed by Buddy. Do not edit manually.
use_default_settings: true

server:
  port: {port}
  bind_address: "127.0.0.1"
  secret_key: "{secret_key}"
  public_instance: false

general:
  debug: false
  instance_name: "Buddy Search"

search:
  safe_search: 0
  formats:
    - html
    - json

ui:
  default_theme: simple
  default_locale: en
"""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(content, encoding="utf-8")
    logger.info("Wrote SearXNG settings: %s", settings_path)


# ═══════════════════════════════════════════════════════════
# Clone + install
# ═══════════════════════════════════════════════════════════


def _run(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    timeout: float = 300.0,
    on_progress: Optional[Callable] = None,
    label: str = "",
) -> bool:
    """Run a subprocess, stream output lines to on_progress. Returns success."""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        t0 = time.time()
        while True:
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                stripped = line.rstrip()
                logger.debug("%s: %s", label or cmd[0], stripped)
                if on_progress and stripped:
                    on_progress(f"  {stripped}", False)
            elif proc.poll() is not None:
                break
            if time.time() - t0 > timeout:
                proc.kill()
                logger.error("%s timed out after %.0fs", label, timeout)
                return False

        return proc.returncode == 0
    except Exception as ex:
        logger.error("%s failed: %r", label, ex)
        return False


def _clone(repo_dir: Path, on_progress: Optional[Callable]) -> bool:
    if (repo_dir / ".git").exists():
        logger.info("SearXNG repo already cloned: %s", repo_dir)
        if on_progress:
            on_progress("SearXNG repo already present — skipping clone.", False)
        return True

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress("Cloning SearXNG repository...", False)

    ok = _run(
        ["git", "clone", "--depth=1", _REPO_URL, str(repo_dir)],
        timeout=120.0,
        on_progress=on_progress,
        label="git clone",
    )
    if ok:
        logger.info("SearXNG cloned → %s", repo_dir)
    else:
        logger.error("git clone failed")
    return ok


def _create_venv(
    venv_dir: Path,
    python_exe: str,
    on_progress: Optional[Callable],
) -> bool:
    if _venv_python(venv_dir.parent).exists():
        logger.info("SearXNG venv already exists: %s", venv_dir)
        if on_progress:
            on_progress("SearXNG venv already present — skipping.", False)
        return True

    if on_progress:
        on_progress("Creating Python venv for SearXNG...", False)

    ok = _run(
        [python_exe, "-m", "venv", str(venv_dir)],
        timeout=60.0,
        label="venv create",
    )
    if not ok:
        logger.error("venv creation failed")
    return ok


def _install_searxng(
    repo_dir: Path,
    venv_dir: Path,
    on_progress: Optional[Callable],
) -> bool:
    py = (
        venv_dir / ("Scripts" if platform.system() == "Windows" else "bin") / (
            "python.exe" if platform.system() == "Windows" else "python"
        )
    )

    # Check if already installed (searxng package present in venv)
    check = subprocess.run(
        [str(py), "-c", "import searx; print('ok')"],
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    if check.returncode == 0 and "ok" in check.stdout:
        logger.info("SearXNG already installed in venv.")
        if on_progress:
            on_progress("SearXNG already installed in venv — skipping.", False)
        return True

    if on_progress:
        on_progress("Installing SearXNG into venv (this takes ~1–2 min)...", False)

    # Upgrade pip first
    _run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"],
        timeout=60.0, label="pip upgrade",
    )

    # Install SearXNG's declared dependencies first.
    # searx/__init__.py imports msgspec at module level, which causes pip's
    # isolated build phase to fail before any deps are installed. Pre-installing
    # from requirements.txt means all deps (including msgspec) are present when
    # the editable build hook runs.
    req_file = repo_dir / "requirements.txt"
    if req_file.exists():
        if on_progress:
            on_progress("Installing SearXNG requirements...", False)
        _run(
            [str(py), "-m", "pip", "install", "-r", str(req_file), "--quiet"],
            cwd=repo_dir,
            timeout=300.0,
            on_progress=on_progress,
            label="pip install requirements",
        )

    # Install SearXNG in editable mode
    ok = _run(
        [str(py), "-m", "pip", "install", "--no-build-isolation", "-e", ".", "--quiet"],
        cwd=repo_dir,
        timeout=300.0,
        on_progress=on_progress,
        label="pip install searxng",
    )
    if not ok:
        logger.error("pip install searxng failed")
    return ok


# ═══════════════════════════════════════════════════════════
# Public: update
# ═══════════════════════════════════════════════════════════


def _git_head(repo_dir: Path) -> str:
    """Return the current git HEAD hash, or empty string on failure."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=5.0,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def update_searxng(
    searxng_dir: Path,
    on_progress: Optional[Callable[[str, bool], None]] = None,
) -> bool:
    """
    Pull the latest SearXNG commits and reinstall deps if anything changed.
    Safe to call every boot — no-op if already up-to-date or offline.
    Always returns True (update is best-effort).
    """
    if not is_installed(searxng_dir):
        return True

    p        = _paths(searxng_dir)
    repo_dir = p["repo"]

    if on_progress:
        on_progress("Checking SearXNG for updates...", False)

    old_head = _git_head(repo_dir)

    # fetch --depth=1 works reliably with shallow clones
    ok = _run(
        ["git", "fetch", "--depth=1", "origin"],
        cwd=repo_dir,
        timeout=30.0,
        label="git fetch",
    )
    if not ok:
        logger.warning("SearXNG git fetch failed — skipping update")
        if on_progress:
            on_progress("SearXNG update check skipped (offline?)", True)
        return True

    _run(
        ["git", "reset", "--hard", "FETCH_HEAD"],
        cwd=repo_dir,
        timeout=10.0,
        label="git reset",
    )

    new_head = _git_head(repo_dir)

    if old_head and old_head == new_head:
        logger.info("SearXNG already up-to-date (%s)", new_head[:8])
        if on_progress:
            on_progress(f"SearXNG up-to-date ({new_head[:8]})", True)
        return True

    logger.info(
        "SearXNG updated: %s → %s",
        old_head[:8] if old_head else "?",
        new_head[:8] if new_head else "?",
    )
    if on_progress:
        on_progress("SearXNG updated — reinstalling deps...", False)

    _install_searxng(repo_dir, p["venv"], on_progress)

    if on_progress:
        on_progress(f"SearXNG updated ({new_head[:8] if new_head else 'unknown'})", True)
    return True


# ═══════════════════════════════════════════════════════════
# Public: setup
# ═══════════════════════════════════════════════════════════


def setup_searxng(
    searxng_dir: Path,
    port: int = 8888,
    python_dir: Optional[Path] = None,
    ask_install_python: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[str, bool], None]] = None,
) -> bool:
    """
    Full first-time setup: find Python → clone → venv → install → write settings.
    Safe to re-run — skips steps already done.

    If no Python is found on the system and ask_install_python is provided,
    calls it to ask the user whether to download a bundled Python runtime
    (~30 MB) to python_dir (~/.buddy/python/). Falls back to DDG on refusal
    or download failure.

    Returns True if setup succeeded (or was already complete).
    Returns False (gracefully) on any unrecoverable failure.
    """
    def _prog(msg: str, done: bool = False) -> None:
        if on_progress:
            on_progress(msg, done)

    # ── Locate a real Python interpreter ──────────────────────
    python_exe = find_python()

    if python_exe is None:
        if ask_install_python is None or not ask_install_python():
            _prog(
                "SearXNG needs Python 3.8+ — skipping. "
                "DuckDuckGo will be used for web search.",
                True,
            )
            return False

        # User agreed — download bundled Python runtime
        install_dir = python_dir or searxng_dir.parent / "python"
        python_exe = install_bundled_python(install_dir, on_progress)

        if python_exe is None:
            _prog(
                "Python download failed. "
                "DuckDuckGo will be used for web search.",
                True,
            )
            return False

    logger.info("Using Python for SearXNG venv: %s", python_exe)

    searxng_dir.mkdir(parents=True, exist_ok=True)
    p = _paths(searxng_dir)

    _prog("Setting up SearXNG...")

    if not _clone(p["repo"], on_progress):
        _prog("SearXNG clone failed. Check git and internet connection.", True)
        return False

    if not _create_venv(p["venv"], python_exe, on_progress):
        _prog("SearXNG venv creation failed.", True)
        return False

    if not _install_searxng(p["repo"], p["venv"], on_progress):
        _prog("SearXNG install failed.", True)
        return False

    _write_settings(p["settings"], port)
    _prog("SearXNG setup complete.", True)
    return True


# ═══════════════════════════════════════════════════════════
# Public: start
# ═══════════════════════════════════════════════════════════


def start_searxng(
    searxng_dir: Path,
    state_dir: Path,
    port: int = 8888,
    on_progress: Optional[Callable[[str, bool], None]] = None,
) -> bool:
    """
    Start SearXNG as a background subprocess.

    - Skip if already listening on port.
    - Probe until ready or timeout.
    - Write PID to state_dir/searxng.pid.

    Returns True if SearXNG came online.
    """
    def _prog(msg: str, done: bool = False) -> None:
        if on_progress:
            on_progress(msg, done)

    if is_running(port):
        _prog(f"SearXNG already running on port {port}.", True)
        return True

    if not is_installed(searxng_dir):
        _prog("SearXNG not installed. Run setup_searxng() first.", True)
        return False

    p       = _paths(searxng_dir)
    py      = _venv_python(searxng_dir)
    log_path = state_dir / _LOG_FILE
    pid_path = state_dir / _PID_FILE

    state_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "SEARXNG_SETTINGS_PATH": str(p["settings"])}

    _prog(f"Starting SearXNG on 127.0.0.1:{port}...")

    try:
        log_f = log_path.open("ab", buffering=0)
    except Exception:
        log_f = None

    try:
        proc = subprocess.Popen(
            [str(py), "-m", "searx.webapp"],
            cwd=str(p["repo"]),
            env=env,
            stdout=log_f or subprocess.DEVNULL,
            stderr=log_f or subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception as ex:
        logger.error("Failed to start SearXNG: %r", ex)
        _prog(f"Failed to start SearXNG: {ex}", True)
        return False

    # Write PID
    try:
        pid_path.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass

    logger.info("SearXNG started pid=%d", proc.pid)

    # Probe until ready
    t0 = time.time()
    while time.time() - t0 < _READY_TIMEOUT:
        if proc.poll() is not None:
            _prog(f"SearXNG exited early (rc={proc.returncode}).", True)
            return False
        if is_ready(port):
            elapsed = round(time.time() - t0, 1)
            _prog(f"SearXNG online ({elapsed}s)  http://127.0.0.1:{port}", True)
            return True
        time.sleep(_PROBE_INTERVAL)

    _prog(f"SearXNG did not respond within {_READY_TIMEOUT:.0f}s.", True)
    return False


# ═══════════════════════════════════════════════════════════
# Public: stop
# ═══════════════════════════════════════════════════════════


def stop_searxng(state_dir: Path, grace: float = 3.0) -> None:
    """
    Stop SearXNG by reading the PID file and sending SIGTERM.
    Called during Buddy shutdown alongside llama-server teardown.
    """
    pid_path = state_dir / _PID_FILE
    if not pid_path.exists():
        return

    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return

    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5.0)
        else:
            os.kill(pid, signal.SIGTERM)
            t0 = time.time()
            while time.time() - t0 < grace:
                try:
                    os.kill(pid, 0)  # check still alive
                    time.sleep(0.1)
                except ProcessLookupError:
                    break
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except ProcessLookupError:
        pass
    except Exception as ex:
        logger.warning("stop_searxng: %r", ex)
    finally:
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass

    logger.info("SearXNG stopped (pid=%d)", pid)
