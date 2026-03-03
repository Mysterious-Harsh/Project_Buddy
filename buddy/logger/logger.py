from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# ==================================================
# Runtime log location (user dir, not repo)
# ==================================================
# macOS / Linux : ~/.buddy/logs
# Windows       : %LOCALAPPDATA%\Buddy\logs


def _runtime_logs_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) / "Buddy" if base else Path.home() / "Buddy"
    else:
        root = Path.home() / ".buddy"

    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


LOG_DIR = _runtime_logs_dir()

# ==================================================
# Formats
# ==================================================
FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
CONSOLE_FORMAT = "%(levelname)-5s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ==================================================
# Cache
# ==================================================
_LOGGERS: dict[str, logging.Logger] = {}

# ==================================================
# Helpers
# ==================================================
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _sanitize_logger_name(name: str) -> str:
    """
    Convert logger name into safe filename.
    Example:
      buddy.brain.brain -> buddy_brain_brain.log
    """
    safe = _SANITIZE_RE.sub("_", name.strip())
    safe = safe.replace(".", "_")
    return safe or "unknown"


def _make_file_handler(path: Path) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
    return handler


def _env_debug_default() -> bool:
    return os.getenv("BUDDY_DEBUG", "0").lower() in {"1", "true", "yes"}


# ==================================================
# Public API
# ==================================================
def get_logger(name: str, *, debug: Optional[bool] = None) -> logging.Logger:
    """
    Static logging policy (Buddy v1):

    - One logger → one file
    - File name derived from logger name
    - Always logs to file
    - Console logging only in debug mode

    Examples:
      get_logger("vector_store") → ~/.buddy/logs/vector_store.log
      get_logger(__name__)       → ~/.buddy/logs/buddy_brain_brain.log
    """
    if name in _LOGGERS:
        return _LOGGERS[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # prevent root duplication

    if debug is None:
        debug = _env_debug_default()

    # ----------------------------
    # File handler (always on)
    # ----------------------------
    filename = _sanitize_logger_name(name) + ".log"
    file_handler = _make_file_handler(LOG_DIR / filename)
    logger.addHandler(file_handler)

    # ----------------------------
    # Console handler (debug only)
    # ----------------------------
    if debug:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        logger.addHandler(console)

    _LOGGERS[name] = logger
    return logger


if __name__ == "__main__":
    lg = get_logger("test", debug=True)
    lg.debug("Logger is working")
    print("Logs directory:", LOG_DIR)
