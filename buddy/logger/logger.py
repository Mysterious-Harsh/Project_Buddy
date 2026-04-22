from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Log directory ─────────────────────────────────────────────────────────────

def _logs_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) / "Buddy" if base else Path.home() / "Buddy"
    else:
        root = Path.home() / ".buddy"
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


LOG_DIR = _logs_dir()

# ── Format ────────────────────────────────────────────────────────────────────

_FMT       = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
_DATE      = "%Y-%m-%d %H:%M:%S"
_FORMATTER = logging.Formatter(_FMT, _DATE)

# ── Internal state ────────────────────────────────────────────────────────────

_ROOT_NAME   = "buddy"
_INITIALIZED = False
_LOGGERS: dict[str, logging.Logger] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _file_handler(path: Path, level: int, *, max_mb: int = 5, backups: int = 3) -> RotatingFileHandler:
    h = RotatingFileHandler(path, maxBytes=max_mb * 1024 * 1024, backupCount=backups, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(_FORMATTER)
    return h


# ── Root initialiser (runs once) ──────────────────────────────────────────────

def _init_root() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False


# ── Public API ────────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger under the 'buddy' namespace.
    All levels (DEBUG → ERROR) written to ~/.buddy/logs/<name>.log.

    Usage:
        logger = get_logger("brain")          # → logs/brain.log
        logger = get_logger("action_router")  # → logs/action_router.log
        logger = get_logger(__name__)         # → logs/buddy_<module>.log
    """
    if name in _LOGGERS:
        return _LOGGERS[name]

    _init_root()

    qualified = name if name.startswith(_ROOT_NAME) else f"{_ROOT_NAME}.{name}"
    logger = logging.getLogger(qualified)
    logger.setLevel(logging.DEBUG)

    safe_name = name.replace(".", "_").replace("/", "_")
    logger.addHandler(_file_handler(LOG_DIR / f"{safe_name}.log", logging.DEBUG, max_mb=5, backups=3))

    _LOGGERS[name] = logger
    return logger


if __name__ == "__main__":
    lg = get_logger("test")
    lg.debug("debug message")
    lg.info("info message")
    lg.warning("warning message")
    lg.error("error message")
    print("Logs at:", LOG_DIR)
