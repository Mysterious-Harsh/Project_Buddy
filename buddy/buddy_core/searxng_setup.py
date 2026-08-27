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
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

import requests

from buddy.logger.logger import get_logger

logger = get_logger("searxng_setup")

_REPO_URL = "https://github.com/searxng/searxng.git"
_PID_FILE = "searxng.pid"
_LOG_FILE = "searxng.log"
_READY_TIMEOUT = 60.0  # seconds to wait for SearXNG to come online
_PROBE_INTERVAL = 0.5
_HTTP = requests.Session()

# Candidate interpreter names searched in PATH when Buddy is frozen.
_PYTHON_CANDIDATES = [
    "python3",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3.9",
    "python3.8",
    "python",
]

# Common install prefixes checked on each platform when PATH search fails.
_EXTRA_SEARCH_PATHS: list[str] = {
    "Darwin": [
        "/usr/bin",
        "/usr/local/bin",
        "/opt/homebrew/bin",
        "/opt/homebrew/opt/python3/bin",
    ],
    "Linux": ["/usr/bin", "/usr/local/bin"],
    "Windows": [
        r"C:\Python312",
        r"C:\Python311",
        r"C:\Python310",
        r"C:\Program Files\Python312",
        r"C:\Program Files\Python311",
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
    logger.debug("find_python: frozen=%s", is_frozen)

    if not is_frozen:
        # Running from source or conda env — sys.executable is Python.
        logger.info("Not frozen — using sys.executable as Python: %s", sys.executable)
        return sys.executable

    # ── Frozen binary: hunt for system Python ─────────────────
    logger.info(
        "Frozen build detected — searching PATH and known install locations for Python"
        " 3.8+"
    )
    import shutil

    def _check(path: str) -> bool:
        """Return True if path is an executable Python >= 3.8."""
        try:
            r = subprocess.run(
                [
                    path,
                    "-c",
                    (
                        "import sys; v=sys.version_info; "
                        "print('ok') if v>=(3,8) else print('old')"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            ok = r.returncode == 0 and "ok" in r.stdout
            if not ok:
                logger.debug(
                    "find_python: rejected candidate %s (rc=%s, stdout=%r, stderr=%r)",
                    path,
                    r.returncode,
                    r.stdout.strip(),
                    r.stderr.strip(),
                )
            return ok
        except Exception as ex:
            logger.debug("find_python: candidate %s failed check: %r", path, ex)
            return False

    # 1. Search PATH
    for name in _PYTHON_CANDIDATES:
        found = shutil.which(name)
        if not found:
            logger.debug("find_python: %s not found in PATH", name)
            continue
        if _check(found):
            logger.info("Found system Python in PATH: %s", found)
            return found

    # 2. Check extra locations
    is_windows = platform.system() == "Windows"
    exe_name = "python.exe" if is_windows else "python3"
    for prefix in _EXTRA_SEARCH_PATHS:
        candidate = str(Path(prefix) / exe_name)
        if not Path(candidate).exists():
            logger.debug("find_python: %s does not exist", candidate)
            continue
        if _check(candidate):
            logger.info("Found system Python at: %s", candidate)
            return candidate

    logger.warning("No system Python 3.8+ found — SearXNG cannot be set up.")
    return None


# ═══════════════════════════════════════════════════════════
# Bundled Python (python-build-standalone)
# ═══════════════════════════════════════════════════════════

_PBS_API = (
    "https://api.github.com/repos/indygreg/python-build-standalone/releases/latest"
)
_PBS_CHUNK = 65536

# Maps (system, arch) → substring to match in asset filename.
# We target Python 3.11, install_only variant (smallest usable build).
_PBS_PATTERNS: dict[tuple[str, str], list[str]] = {
    ("darwin", "arm64"): ["cpython-3.11", "aarch64-apple-darwin", "install_only"],
    ("darwin", "x64"): ["cpython-3.11", "x86_64-apple-darwin", "install_only"],
    ("linux", "x64"): ["cpython-3.11", "x86_64-unknown-linux-gnu", "install_only"],
    ("linux", "arm64"): ["cpython-3.11", "aarch64-unknown-linux-gnu", "install_only"],
    ("windows", "x64"): ["cpython-3.11", "x86_64-pc-windows-msvc", "install_only"],
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
        logger.info(msg)
        if on_progress:
            on_progress(msg, done)

    is_windows = platform.system() == "Windows"
    sys_key, arch_key = _pbs_platform_key()
    logger.info(
        "install_bundled_python: target_dir=%s platform=%s/%s",
        python_dir,
        sys_key,
        arch_key,
    )

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
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "buddy-installer/1.0",
            },
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
        logger.error(
            "No python-build-standalone asset matched pattern for %s/%s among %d"
            " assets",
            sys_key,
            arch_key,
            len(release.get("assets", [])),
        )
        _prog(f"No bundled Python found for {sys_key}/{arch_key}.", True)
        return None

    asset_name = asset["name"]
    asset_url = asset["browser_download_url"]
    size_mb = round(asset.get("size", 0) / (1024 * 1024), 1)
    logger.info("Selected PBS asset: %s (%.1f MB) → %s", asset_name, size_mb, asset_url)
    _prog(f"Downloading Python runtime ({size_mb} MB)...", False)

    python_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="buddy_python_") as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / asset_name

        # Stream download
        last_pct = [-1]
        try:
            dl = requests.get(
                asset_url,
                stream=True,
                timeout=(8.0, None),
                headers={"User-Agent": "buddy-installer/1.0"},
            )
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

        py_path.chmod(
            py_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

    logger.info("Bundled Python installed: %s", py_path)
    _prog(f"Python runtime ready: {py_path}", True)
    return str(py_path)


# ═══════════════════════════════════════════════════════════
# Paths helper
# ═══════════════════════════════════════════════════════════


def _paths(searxng_dir: Path) -> dict:
    return {
        "repo": searxng_dir / "repo",
        "venv": searxng_dir / "venv",
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
    return (p["repo"] / "searx" / "webapp.py").exists() and _venv_python(
        searxng_dir
    ).exists()


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
        except Exception as ex:
            logger.warning(
                "Could not read existing settings.yml to preserve secret_key: %r", ex
            )

    if not secret_key:
        secret_key = secrets.token_hex(32)
        logger.debug("Generated a new SearXNG secret_key")
    else:
        logger.debug("Reusing existing SearXNG secret_key")

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

# Tune outgoing HTTP for a single-user local instance.
outgoing:
  request_timeout: 6.0
  max_request_timeout: 12.0
  pool_connections: 10
  pool_maxsize: 15
  enable_http2: true

# Only enable engines that reliably work on a local self-hosted instance.
# Google direct scraping and Brave without an API key get blocked quickly.
engines:
  - name: brave
    disabled: true
  - name: brave.news
    disabled: true
  - name: karmasearch
    disabled: true
  - name: karmasearch videos
    disabled: true
  - name: google
    disabled: true
  - name: google news
    disabled: true
  - name: google videos
    disabled: true
  - name: google images
    disabled: true
  - name: google scholar
    disabled: true
  - name: duckduckgo
    disabled: false
  - name: bing
    disabled: false
  - name: bing news
    disabled: false
  - name: startpage
    disabled: false
  - name: mojeek
    disabled: true
  - name: wikipedia
    disabled: false
  - name: wikidata
    disabled: false
"""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(content, encoding="utf-8")
    logger.info("Wrote SearXNG settings: %s", settings_path)


# ═══════════════════════════════════════════════════════════
# Clone + install
# ═══════════════════════════════════════════════════════════


def _is_online(timeout: float = 6.0) -> bool:
    """Return True if any well-known host is reachable — quick offline guard."""
    # Mix of DNS (53), HTTPS (443), and HTTP (80) so at least one port gets
    # through on networks that block raw outbound DNS to public resolvers.
    hosts = [
        ("8.8.8.8", 53),
        ("1.1.1.1", 53),
        ("8.8.8.8", 443),
        ("1.1.1.1", 443),
        ("8.8.4.4", 80),
    ]
    result = threading.Event()
    succeeded: list[str] = []

    def _probe(host: tuple) -> None:
        try:
            with socket.create_connection(host, timeout=timeout):
                succeeded.append(f"{host[0]}:{host[1]}")
                result.set()
        except Exception as ex:
            logger.debug("_is_online: %s:%s unreachable (%r)", host[0], host[1], ex)

    threads = [threading.Thread(target=_probe, args=(h,), daemon=True) for h in hosts]
    for t in threads:
        t.start()
    result.wait(timeout=timeout + 0.5)
    online = result.is_set()
    if online:
        logger.debug("_is_online: reachable via %s", succeeded[0] if succeeded else "?")
    else:
        logger.warning("_is_online: no network reachability to any of %s", hosts)
    return online


def _run(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    timeout: float = 300.0,
    on_progress: Optional[Callable] = None,
    label: str = "",
) -> bool:
    """
    Run a subprocess and stream output lines to on_progress.

    Uses a reader thread so the timeout is enforced even when the subprocess
    produces no output (e.g. git fetch hanging on a dropped network).

    Two hardening details that matter in practice:
      - bufsize=1 (line buffering): without it, Python's default I/O
        buffering on a piped (non-tty) stdout can hold output back rather
        than releasing it line-by-line, so a real error from the child can
        fail to show up in our logs before the process exits.
      - New process group + group-kill: if the child (e.g. git) spawns a
        helper that inherits the pipe (a credential prompt, gpg, etc.),
        killing just the parent PID can leave that helper alive holding the
        pipe open, and our reader thread can then block forever on read()
        even though the process we *meant* to kill is long gone. Killing
        the whole group avoids that.
    """
    tag = label or cmd[0]
    logger.debug(
        "Running: %s%s (timeout=%.0fs)",
        " ".join(cmd),
        f"  [cwd={cwd}]" if cwd else "",
        timeout,
    )
    t0 = time.time()

    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True  # own process group on POSIX

    def _kill_tree(proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=5.0,
                )
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **popen_kwargs,
        )

        def _reader() -> None:
            try:
                for line in proc.stdout:
                    stripped = line.rstrip()
                    if stripped:
                        logger.debug("%s: %s", label or cmd[0], stripped)
                        if on_progress:
                            on_progress(f"  {stripped}", False)
            except Exception:
                pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            _kill_tree(proc)
            t.join(2.0)
            if t.is_alive():
                logger.warning(
                    "%s: reader thread still blocked after kill — a grandchild "
                    "process may still be holding the output pipe open.",
                    tag,
                )
            logger.error("%s timed out after %.0fs", tag, timeout)
            return False

        # The reader thread finishing only means stdout hit EOF — it does NOT
        # guarantee the OS has fully reaped the process and set returncode.
        # Reading proc.returncode before an explicit wait() is a race that can
        # spuriously read None (which fails the `== 0` check below even
        # though the process actually succeeded). wait() here is near-instant
        # since the process's output stream is already closed.
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            logger.warning(
                "%s: process did not report exit status within 5s of EOF", tag
            )
        finally:
            if proc.stdout:
                proc.stdout.close()

        elapsed = time.time() - t0
        if proc.returncode == 0:
            logger.debug("%s completed OK in %.1fs", tag, elapsed)
            return True

        logger.warning(
            "%s exited with code %s after %.1fs",
            tag,
            proc.returncode,
            elapsed,
        )
        return False
    except FileNotFoundError as ex:
        logger.error(
            "%s failed: command not found (%r). Is it installed and on PATH?",
            tag,
            ex,
        )
        return False
    except Exception as ex:
        logger.error("%s failed: %r", tag, ex)
        return False


def _git_env() -> dict:
    """
    Environment for git subprocess calls.

    GIT_TERMINAL_PROMPT=0 makes git fail immediately instead of trying to
    open an interactive credential/host-key prompt. An interactive prompt
    subprocess has no controlling terminal here anyway, but it can still
    inherit our piped stdout and sit there indefinitely — which would hang
    our reader thread even after "git" itself is considered done. Better to
    have git refuse outright than risk that.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _is_complete_clone(repo_dir: Path) -> bool:
    """
    True only if repo_dir holds a fully-cloned SearXNG repo.

    Checking for `.git` alone isn't enough: git creates the `.git` folder
    as the very first step of cloning, before any objects are fetched. A
    clone that dies partway through (network blip, killed process, etc.)
    leaves a `.git` folder behind with nothing usable inside it — and that
    stale folder then makes every subsequent `git clone` attempt fail with
    "destination path ... already exists and is not an empty directory",
    even though nothing was actually cloned.
    """
    has_git = (repo_dir / ".git").exists()
    has_webapp = (repo_dir / "searx" / "webapp.py").exists()
    if repo_dir.exists() and not (has_git and has_webapp):
        logger.debug(
            "_is_complete_clone(%s) = False (has_.git=%s, has_searx/webapp.py=%s)",
            repo_dir,
            has_git,
            has_webapp,
        )
    return has_git and has_webapp


def _wipe_incomplete_repo_path(path: Path) -> None:
    """
    Unconditionally remove `path` if it exists, with the same error-logging
    behavior as _wipe_incomplete_repo. Used for scratch/temp clone
    directories, which are never "the real repo" so don't need the
    completeness check — if it exists at all when we're done with it, it
    should go.
    """
    if not path.exists():
        return

    import shutil

    errors: list[str] = []

    def _onerror(func, p, exc_info):
        errors.append(f"{p}: {exc_info[1]}")

    shutil.rmtree(path, onerror=_onerror)

    if path.exists():
        logger.error(
            "Failed to fully remove temp dir %s. Errors: %s",
            path,
            errors or "none captured",
        )
    elif errors:
        logger.warning("Removed temp dir %s but with some errors: %s", path, errors)


def _wipe_incomplete_repo(repo_dir: Path) -> None:
    """
    Remove repo_dir if it exists but isn't a complete clone.

    Uses an onerror callback instead of ignore_errors=True so that if
    removal fails partway through (permissions, file-in-use, etc.) we log
    exactly which path and error caused it, rather than silently leaving a
    partial directory behind that makes the next `git clone` attempt fail
    with a confusing "already exists" error.
    """
    if not repo_dir.exists():
        logger.debug(
            "_wipe_incomplete_repo: %s does not exist, nothing to do", repo_dir
        )
        return
    if _is_complete_clone(repo_dir):
        logger.debug(
            "_wipe_incomplete_repo: %s is a complete clone, leaving it alone", repo_dir
        )
        return

    logger.info("Removing incomplete/stale SearXNG repo dir: %s", repo_dir)
    import shutil

    errors: list[str] = []

    def _onerror(func, path, exc_info):
        errors.append(f"{path}: {exc_info[1]}")

    shutil.rmtree(repo_dir, onerror=_onerror)

    if repo_dir.exists():
        remaining = []
        try:
            remaining = [str(p) for p in repo_dir.rglob("*")][:20]
        except Exception:
            pass
        logger.error(
            "Failed to fully remove stale repo dir %s. Errors: %s | Remaining: %s",
            repo_dir,
            errors or "none captured",
            remaining,
        )
    elif errors:
        logger.warning(
            "Removed %s but with some errors along the way: %s", repo_dir, errors
        )
    else:
        logger.info("Successfully removed stale repo dir: %s", repo_dir)


def _clone(
    repo_dir: Path,
    on_progress: Optional[Callable],
    retries: int = 3,
    retry_delay: float = 3.0,
) -> bool:
    """
    Clone SearXNG into repo_dir.

    Clones into a fresh, uniquely-named temp directory next to repo_dir and
    atomically renames it into place on success, rather than cloning
    directly into repo_dir. This sidesteps two problems with cloning
    straight into the final path:

      1. git creates its target directory as the very first step, before
         any objects are fetched — so an interrupted clone (network blip,
         killed process) leaves a broken partial directory sitting exactly
         where the next attempt needs to clone into, causing a confusing
         "destination path already exists" failure that has nothing to do
         with whether the network actually works.
      2. Two processes racing to set up SearXNG at once (e.g. a stray
         leftover instance) can each wipe and recreate the same target
         path out from under each other. Two processes cloning into two
         different, uniquely-named temp dirs can never collide — only the
         final rename is a shared operation, and renames are atomic.
    """
    if _is_complete_clone(repo_dir):
        logger.info("SearXNG repo already cloned: %s", repo_dir)
        if on_progress:
            on_progress("SearXNG repo already present — skipping clone.", False)
        return True

    repo_dir.parent.mkdir(parents=True, exist_ok=True)

    # A leftover broken directory from a much older version of this code
    # (which used to clone directly into repo_dir) could still be sitting
    # here — clear it so it doesn't block the final rename.
    _wipe_incomplete_repo(repo_dir)

    logger.info("Cloning SearXNG into %s (up to %d attempts)", repo_dir, retries)
    t0 = time.time()
    env = _git_env()

    for attempt in range(1, retries + 1):
        # Re-check in case a sibling process finished the clone while we
        # were retrying (it renames into repo_dir atomically, so this is
        # always either "not there yet" or "fully there" — never partial).
        if _is_complete_clone(repo_dir):
            logger.info(
                "SearXNG repo appeared during retry (cloned by another process): %s",
                repo_dir,
            )
            return True

        tmp_dir = (
            repo_dir.parent
            / f".{repo_dir.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        )
        _wipe_incomplete_repo_path(
            tmp_dir
        )  # clear any leftover from a crashed previous run

        if on_progress:
            suffix = f" (attempt {attempt}/{retries})" if attempt > 1 else ""
            on_progress(f"Cloning SearXNG repository...{suffix}", False)
        logger.debug("git clone attempt %d/%d → temp dir %s", attempt, retries, tmp_dir)

        attempt_t0 = time.time()
        ok = _run(
            ["git", "clone", "--depth=1", "--progress", _REPO_URL, str(tmp_dir)],
            env=env,
            timeout=120.0,
            on_progress=on_progress,
            label="git clone",
        )

        if ok and not _is_complete_clone(tmp_dir):
            # Clone reported success but the result doesn't look right —
            # treat as a failure rather than trusting the exit code alone.
            logger.error(
                "git clone reported success but %s doesn't look like a complete repo",
                tmp_dir,
            )
            ok = False

        if ok:
            try:
                os.replace(str(tmp_dir), str(repo_dir))
            except OSError as ex:
                # Someone else finished first, or repo_dir is a stale
                # non-empty leftover. Handle each case explicitly rather
                # than guessing.
                if _is_complete_clone(repo_dir):
                    logger.info(
                        "Another process completed the clone first — "
                        "discarding our temp clone %s",
                        tmp_dir,
                    )
                    _wipe_incomplete_repo_path(tmp_dir)
                    return True
                logger.warning(
                    "Rename %s → %s failed (%r); clearing destination and retrying"
                    " rename",
                    tmp_dir,
                    repo_dir,
                    ex,
                )
                _wipe_incomplete_repo(repo_dir)
                try:
                    os.replace(str(tmp_dir), str(repo_dir))
                except OSError as ex2:
                    logger.error("Rename retry also failed: %r", ex2)
                    ok = False

        if ok:
            logger.info(
                "SearXNG cloned → %s (attempt %d/%d, %.1fs, total %.1fs)",
                repo_dir,
                attempt,
                retries,
                time.time() - attempt_t0,
                time.time() - t0,
            )
            return True

        logger.error(
            "git clone failed (attempt %d/%d, %.1fs elapsed this attempt)",
            attempt,
            retries,
            time.time() - attempt_t0,
        )
        _wipe_incomplete_repo_path(tmp_dir)  # don't leave failed temp dirs behind
        if attempt < retries:
            logger.info("Retrying clone in %.0fs...", retry_delay)
            time.sleep(retry_delay)

    logger.error(
        "git clone failed after %d attempts (%.1fs total)",
        retries,
        time.time() - t0,
    )
    return False


def _bootstrap_pip(venv_dir: Path, on_progress: Optional[Callable]) -> bool:
    """
    Install pip into a venv that was created with --without-pip.

    Tries `ensurepip` first (works on most standard Pythons). Some
    conda/miniforge Python builds ship without a working ensurepip bundle,
    so if that fails, fall back to downloading get-pip.py and running it.
    """
    venv_py = _venv_python(venv_dir.parent)
    logger.info("Bootstrapping pip into venv via ensurepip: %s", venv_py)

    ok = _run(
        [str(venv_py), "-m", "ensurepip", "--upgrade"],
        timeout=60.0,
        on_progress=on_progress,
        label="ensurepip bootstrap",
    )
    if ok:
        logger.info("pip bootstrapped successfully via ensurepip")
        return True

    logger.warning("ensurepip bootstrap failed — falling back to get-pip.py")
    if on_progress:
        on_progress("ensurepip unavailable — downloading get-pip.py...", False)

    try:
        resp = requests.get(
            "https://bootstrap.pypa.io/get-pip.py",
            timeout=(8.0, 30.0),
        )
        resp.raise_for_status()
        logger.debug("get-pip.py downloaded (%d bytes)", len(resp.content))
    except Exception as ex:
        logger.error("get-pip.py download failed: %r", ex)
        if on_progress:
            on_progress(f"Failed to download get-pip.py: {ex}", False)
        return False

    with tempfile.TemporaryDirectory(prefix="buddy_getpip_") as tmp:
        get_pip_path = Path(tmp) / "get-pip.py"
        get_pip_path.write_bytes(resp.content)
        ok = _run(
            [str(venv_py), str(get_pip_path)],
            timeout=90.0,
            on_progress=on_progress,
            label="get-pip.py",
        )

    if ok:
        logger.info("pip bootstrapped successfully via get-pip.py")
    else:
        logger.error("get-pip.py bootstrap failed")
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

    logger.info("Creating SearXNG venv at %s using %s", venv_dir, python_exe)
    if on_progress:
        on_progress("Creating Python venv for SearXNG...", False)
    t0 = time.time()

    # Create the venv WITHOUT pip. Some conda/miniforge Python builds don't
    # ship a working ensurepip bundle, which makes the stdlib venv module's
    # default pip-bootstrap step fail (and the failure can be silent/opaque
    # to the caller). Bootstrapping pip ourselves afterward is more reliable
    # and gives us a clear, logged error if it still fails.
    ok = _run(
        [python_exe, "-m", "venv", "--without-pip", str(venv_dir)],
        timeout=60.0,
        on_progress=on_progress,
        label="venv create",
    )
    if not ok:
        logger.error("venv creation failed after %.1fs", time.time() - t0)
        return False
    logger.info("venv shell created in %.1fs (without pip)", time.time() - t0)

    if not _bootstrap_pip(venv_dir, on_progress):
        logger.error(
            "venv creation failed: could not bootstrap pip (%.1fs total)",
            time.time() - t0,
        )
        if on_progress:
            on_progress(
                "Could not install pip into the SearXNG venv "
                "(ensurepip and get-pip.py both failed).",
                False,
            )
        return False

    logger.info("SearXNG venv ready at %s (%.1fs total)", venv_dir, time.time() - t0)
    return True


def _install_searxng(
    repo_dir: Path,
    venv_dir: Path,
    on_progress: Optional[Callable],
) -> bool:
    py = (
        venv_dir
        / ("Scripts" if platform.system() == "Windows" else "bin")
        / ("python.exe" if platform.system() == "Windows" else "python")
    )

    # Check if already installed (searxng package present in venv)
    check = subprocess.run(
        [str(py), "-c", "import searx; print('ok')"],
        capture_output=True,
        text=True,
        cwd=str(repo_dir),
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
        timeout=60.0,
        label="pip upgrade",
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
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if r.returncode != 0:
            logger.debug(
                "_git_head: rev-parse failed (rc=%s): %s",
                r.returncode,
                r.stderr.strip(),
            )
            return ""
        return r.stdout.strip()
    except Exception as ex:
        logger.debug("_git_head: %r", ex)
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

    if not _is_online():
        logger.info("SearXNG update check skipped — offline")
        if on_progress:
            on_progress("SearXNG update check skipped (offline)", True)
        return True

    p = _paths(searxng_dir)
    repo_dir = p["repo"]

    if on_progress:
        on_progress("Checking SearXNG for updates...", False)

    old_head = _git_head(repo_dir)
    logger.debug("update_searxng: current HEAD=%s", old_head[:8] if old_head else "?")

    env = _git_env()

    # fetch --depth=1 works reliably with shallow clones
    ok = _run(
        ["git", "fetch", "--depth=1", "origin"],
        cwd=repo_dir,
        env=env,
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
        env=env,
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
        on_progress(
            f"SearXNG updated ({new_head[:8] if new_head else 'unknown'})", True
        )
    return True


def _pid_alive(pid: int) -> bool:
    """Best-effort cross-platform check for whether a PID is still running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            return str(pid) in r.stdout
        except Exception:
            return True  # unknown -> assume alive, safer than stealing the lock
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except Exception:
        return True


@contextmanager
def _setup_lock(searxng_dir: Path, timeout: float = 60.0, poll: float = 0.5):
    """
    Advisory lock so two concurrent processes (a stray leftover instance,
    two app copies, a dev auto-reloader, etc.) can't both run setup_searxng()
    against the same searxng_dir at once. Without this, one process's
    cleanup of a partial repo/venv can race with the other process's clone,
    each repeatedly undoing the other's work — which looks like a permanent
    "already exists" failure even though nothing is actually broken.

    Yields True if the lock was acquired, False if another live process
    still holds it after `timeout` seconds (caller should treat that as
    "let the other process finish" rather than as an error).
    """
    searxng_dir.mkdir(parents=True, exist_ok=True)
    lock_path = searxng_dir / ".setup.lock"
    acquired = False
    t0 = time.time()

    while time.time() - t0 < timeout:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                holder_pid = int(lock_path.read_text(encoding="utf-8").strip())
            except Exception:
                holder_pid = -1

            if holder_pid == os.getpid() or not _pid_alive(holder_pid):
                # Stale lock left by a crashed/killed process — clear it
                # and retry immediately rather than waiting out the timeout.
                logger.warning(
                    "Clearing stale SearXNG setup lock (pid=%s no longer running)",
                    holder_pid,
                )
                try:
                    lock_path.unlink()
                except Exception:
                    pass
                continue

            time.sleep(poll)

    try:
        if not acquired:
            logger.warning(
                "Could not acquire SearXNG setup lock within %.0fs — "
                "another process appears to be running setup.",
                timeout,
            )
        yield acquired
    finally:
        if acquired:
            try:
                lock_path.unlink()
            except Exception:
                pass


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

    Guarded by an advisory lock (searxng_dir/.setup.lock) so two concurrent
    callers can't race each other's clone/venv/install steps.

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
                "Python download failed. DuckDuckGo will be used for web search.",
                True,
            )
            return False

    logger.info("Using Python for SearXNG venv: %s", python_exe)

    searxng_dir.mkdir(parents=True, exist_ok=True)
    p = _paths(searxng_dir)

    with _setup_lock(searxng_dir) as acquired:
        if not acquired:
            # Another live process is already doing setup. Rather than
            # racing it, wait briefly and defer to whatever it produces.
            if is_installed(searxng_dir):
                _prog("SearXNG already set up by another process.", True)
                return True
            _prog(
                "Another process is already setting up SearXNG — skipping "
                "this attempt.",
                True,
            )
            return False

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

    p = _paths(searxng_dir)
    py = _venv_python(searxng_dir)
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
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5.0
            )
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


# ═══════════════════════════════════════════════════════════
# Self-tests
# ═══════════════════════════════════════════════════════════
#
# Run directly with:
#     python searxng_setup.py
#     python searxng_setup.py -v          (verbose)
#     python searxng_setup.py TestClone   (just one class)
#
# These exercise the clone/wipe/atomic-rename logic, the setup lock, pid
# liveness checks, venv + pip bootstrap, and settings writing — all
# offline (a local throwaway git repo stands in for the real SearXNG
# remote) and fast, so you can validate a change here without running the
# whole app and waiting on a real network clone every time.
#
# NOT covered (needs real network / the real searxng deps, so it's left
# to a real run of setup_searxng against the actual repo):
#   - install_bundled_python() (downloads python-build-standalone)
#   - a full fresh "pip install -r requirements.txt && pip install -e ."
#     against the real SearXNG project

if __name__ == "__main__":
    import shutil
    import unittest

    def _make_fixture_repo(root: Path) -> Path:
        """
        Build a tiny local git repo that satisfies _is_complete_clone()'s
        checks (.git + searx/webapp.py) and is also `pip install -e .`
        installable, so it stands in for the real SearXNG remote in tests
        without needing network access.
        """
        src = root / "fixture_src"
        (src / "searx").mkdir(parents=True)
        (src / "searx" / "__init__.py").write_text("", encoding="utf-8")
        (src / "searx" / "webapp.py").write_text("# fake webapp\n", encoding="utf-8")
        (src / "requirements.txt").write_text("", encoding="utf-8")
        (src / "pyproject.toml").write_text(
            "[build-system]\n"
            'requires = ["setuptools>=61.0"]\n'
            'build-backend = "setuptools.build_meta"\n\n'
            "[project]\n"
            'name = "searx"\n'
            'version = "0.0.1"\n',
            encoding="utf-8",
        )

        run_kwargs = dict(cwd=str(src), capture_output=True, text=True, check=True)
        subprocess.run(["git", "init", "-q"], **run_kwargs)
        subprocess.run(["git", "config", "user.email", "test@test.local"], **run_kwargs)
        subprocess.run(["git", "config", "user.name", "test"], **run_kwargs)
        subprocess.run(["git", "add", "-A"], **run_kwargs)
        subprocess.run(["git", "commit", "-q", "-m", "init"], **run_kwargs)
        return src

    def _spawn_dead_pid() -> int:
        """Spawn a trivial subprocess, wait for it, and return its (now-dead) PID."""
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    class TestIsCompleteClone(unittest.TestCase):
        def setUp(self):
            self.tmp = Path(tempfile.mkdtemp(prefix="buddy_test_"))

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def test_missing_dir_is_incomplete(self):
            self.assertFalse(_is_complete_clone(self.tmp / "nope"))

        def test_git_dir_alone_is_incomplete(self):
            repo = self.tmp / "repo"
            (repo / ".git").mkdir(parents=True)
            self.assertFalse(_is_complete_clone(repo))

        def test_git_dir_plus_webapp_is_complete(self):
            repo = self.tmp / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "searx").mkdir()
            (repo / "searx" / "webapp.py").write_text("x")
            self.assertTrue(_is_complete_clone(repo))

    class TestWipeHelpers(unittest.TestCase):
        def setUp(self):
            self.tmp = Path(tempfile.mkdtemp(prefix="buddy_test_"))

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def test_wipe_incomplete_repo_removes_partial(self):
            repo = self.tmp / "repo"
            (repo / ".git").mkdir(parents=True)  # partial: no searx/webapp.py
            _wipe_incomplete_repo(repo)
            self.assertFalse(repo.exists())

        def test_wipe_incomplete_repo_keeps_complete(self):
            repo = self.tmp / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "searx").mkdir()
            (repo / "searx" / "webapp.py").write_text("x")
            _wipe_incomplete_repo(repo)
            self.assertTrue(repo.exists())

        def test_wipe_incomplete_repo_path_is_unconditional(self):
            d = self.tmp / "scratch"
            (d / "sub").mkdir(parents=True)
            (d / "sub" / "f.txt").write_text("x")
            _wipe_incomplete_repo_path(d)
            self.assertFalse(d.exists())

        def test_wipe_missing_dir_is_a_noop(self):
            _wipe_incomplete_repo(self.tmp / "nope")  # should not raise
            _wipe_incomplete_repo_path(self.tmp / "nope2")  # should not raise

    class TestClone(unittest.TestCase):
        """Exercises _clone() end-to-end against a local fixture repo — no network."""

        @classmethod
        def setUpClass(cls):
            cls.root = Path(tempfile.mkdtemp(prefix="buddy_test_clone_"))
            cls.fixture = _make_fixture_repo(cls.root)

        @classmethod
        def tearDownClass(cls):
            shutil.rmtree(cls.root, ignore_errors=True)

        def setUp(self):
            self.work = self.root / f"work_{secrets.token_hex(4)}"
            self.work.mkdir()
            self._orig_repo_url = globals()["_REPO_URL"]
            globals()["_REPO_URL"] = str(self.fixture)  # point _clone() at our fixture

        def tearDown(self):
            globals()["_REPO_URL"] = self._orig_repo_url
            shutil.rmtree(self.work, ignore_errors=True)

        def test_fresh_clone_succeeds(self):
            repo_dir = self.work / "repo"
            ok = _clone(repo_dir, on_progress=None)
            self.assertTrue(ok)
            self.assertTrue(_is_complete_clone(repo_dir))
            # no leftover temp dirs
            leftovers = list(self.work.glob(".repo.tmp-*"))
            self.assertEqual(leftovers, [], f"leftover temp dirs: {leftovers}")

        def test_already_cloned_is_skipped_fast(self):
            repo_dir = self.work / "repo"
            self.assertTrue(_clone(repo_dir, on_progress=None))
            t0 = time.time()
            self.assertTrue(_clone(repo_dir, on_progress=None))
            self.assertLess(time.time() - t0, 2.0, "skip path should be near-instant")

        def test_recovers_from_partial_leftover_git_dir(self):
            repo_dir = self.work / "repo"
            # Simulate an interrupted clone: .git exists, nothing else does.
            (repo_dir / ".git").mkdir(parents=True)
            (repo_dir / ".git" / "some_partial_file").write_text("junk")
            ok = _clone(repo_dir, on_progress=None)
            self.assertTrue(ok)
            self.assertTrue(_is_complete_clone(repo_dir))

        def test_sibling_finished_first_is_detected_via_rename_race(self):
            """
            If repo_dir becomes a complete clone in between attempts (as if a
            sibling process finished first), _clone should notice and return
            True instead of erroring on the rename collision.
            """
            repo_dir = self.work / "repo"
            self.assertTrue(_clone(repo_dir, on_progress=None))  # "sibling" clones it
            # Now call again as if we were racing — should short-circuit True.
            ok = _clone(repo_dir, on_progress=None)
            self.assertTrue(ok)

        def test_progress_callback_receives_messages(self):
            messages = []
            repo_dir = self.work / "repo"
            ok = _clone(repo_dir, on_progress=lambda msg, done: messages.append(msg))
            self.assertTrue(ok)
            self.assertTrue(any("Cloning" in m for m in messages))

    class TestPidAlive(unittest.TestCase):
        def test_self_is_alive(self):
            self.assertTrue(_pid_alive(os.getpid()))

        def test_finished_process_is_not_alive(self):
            dead_pid = _spawn_dead_pid()
            # Small grace window for the OS to fully reap zombie state.
            for _ in range(20):
                if not _pid_alive(dead_pid):
                    break
                time.sleep(0.1)
            self.assertFalse(_pid_alive(dead_pid))

        def test_pid_zero_or_negative_is_not_alive(self):
            self.assertFalse(_pid_alive(0))
            self.assertFalse(_pid_alive(-1))

    class TestSetupLock(unittest.TestCase):
        def setUp(self):
            self.tmp = Path(tempfile.mkdtemp(prefix="buddy_test_lock_"))

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def test_acquire_and_release(self):
            with _setup_lock(self.tmp) as acquired:
                self.assertTrue(acquired)
                self.assertTrue((self.tmp / ".setup.lock").exists())
            self.assertFalse((self.tmp / ".setup.lock").exists())

        def test_stale_lock_from_dead_pid_is_cleared(self):
            dead_pid = _spawn_dead_pid()
            for _ in range(20):
                if not _pid_alive(dead_pid):
                    break
                time.sleep(0.1)
            (self.tmp / ".setup.lock").write_text(str(dead_pid), encoding="utf-8")

            t0 = time.time()
            with _setup_lock(self.tmp, timeout=10.0) as acquired:
                self.assertTrue(acquired)
            self.assertLess(
                time.time() - t0, 5.0, "stale lock should clear almost immediately"
            )

        def test_blocked_while_held_by_a_live_process(self):
            # Spawn a real, separate live process and claim its PID owns the lock.
            helper = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(2.5)"]
            )
            try:
                (self.tmp / ".setup.lock").write_text(str(helper.pid), encoding="utf-8")
                with _setup_lock(self.tmp, timeout=1.0, poll=0.2) as acquired:
                    self.assertFalse(
                        acquired, "should not steal a lock held by a live process"
                    )
            finally:
                helper.wait(timeout=10)

            # Once the helper has exited, a fresh attempt should succeed.
            with _setup_lock(self.tmp, timeout=10.0) as acquired:
                self.assertTrue(acquired)

    class TestVenvAndPipBootstrap(unittest.TestCase):
        """Uses the real interpreter running this file — no conda quirks to
        reproduce here, but confirms the --without-pip + ensurepip path works
        end-to-end, which is the part that broke on conda/miniforge."""

        def setUp(self):
            self.tmp = Path(tempfile.mkdtemp(prefix="buddy_test_venv_"))

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def test_create_venv_and_bootstrap_pip(self):
            venv_dir = self.tmp / "venv"
            ok = _create_venv(venv_dir, sys.executable, on_progress=None)
            self.assertTrue(ok)
            py = _venv_python(self.tmp)
            self.assertTrue(py.exists())
            check = subprocess.run(
                [str(py), "-m", "pip", "--version"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(check.returncode, 0, check.stderr)

        def test_create_venv_is_idempotent(self):
            venv_dir = self.tmp / "venv"
            self.assertTrue(_create_venv(venv_dir, sys.executable, on_progress=None))
            t0 = time.time()
            self.assertTrue(_create_venv(venv_dir, sys.executable, on_progress=None))
            self.assertLess(
                time.time() - t0, 2.0, "existing venv should be detected instantly"
            )

    class TestInstallSearxngSkipDetection(unittest.TestCase):
        """
        Doesn't run a real 'pip install -e .' (needs network for real SearXNG
        deps) — just confirms _install_searxng() correctly detects an
        already-installed package and skips reinstalling it.
        """

        def setUp(self):
            self.tmp = Path(tempfile.mkdtemp(prefix="buddy_test_install_"))
            self.venv_dir = self.tmp / "venv"
            self.assertTrue(
                _create_venv(self.venv_dir, sys.executable, on_progress=None)
            )

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def test_skips_when_already_importable(self):
            py = _venv_python(self.tmp)
            site_packages = subprocess.run(
                [
                    str(py),
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            (Path(site_packages) / "searx.py").write_text("# stub\n", encoding="utf-8")

            repo_dir = self.tmp / "repo"
            repo_dir.mkdir()
            messages = []
            ok = _install_searxng(
                repo_dir, self.venv_dir, on_progress=lambda m, d: messages.append(m)
            )
            self.assertTrue(ok)
            self.assertTrue(any("already installed" in m.lower() for m in messages))

    class TestWriteSettings(unittest.TestCase):
        def setUp(self):
            self.tmp = Path(tempfile.mkdtemp(prefix="buddy_test_settings_"))

        def tearDown(self):
            shutil.rmtree(self.tmp, ignore_errors=True)

        def test_generates_and_preserves_secret_key(self):
            settings_path = self.tmp / "settings.yml"
            _write_settings(settings_path, 8888)
            text1 = settings_path.read_text(encoding="utf-8")
            key1 = [l for l in text1.splitlines() if "secret_key" in l][0]

            _write_settings(settings_path, 8889)  # re-run with a different port
            text2 = settings_path.read_text(encoding="utf-8")
            key2 = [l for l in text2.splitlines() if "secret_key" in l][0]

            self.assertEqual(
                key1, key2, "secret_key should be preserved across rewrites"
            )
            self.assertIn("port: 8889", text2)

    unittest.main(verbosity=2)
