from __future__ import annotations

# ==========================================================
# terminal.py  —  v1.2.0
#
# v1.0.0 → v1.1.0
# ────────────────
# 1. Platform-independent execution (Linux / macOS / Windows)
# 2. Daemon command auto-detection + DaemonRegistry
# 3. Bug fix — TimeoutExpired now kills orphaned child process
# 4. TerminalResult: IS_DAEMON, PID fields added (backward-compat)
#
# v1.1.0 → v1.2.0
# ────────────────
# 5. Output truncation — stdout/stderr capped at _MAX_OUTPUT_CHARS
#    (8 000 chars / ~2 k tokens) with a "...[truncated]" notice
# 6. Daemon reader thread memory fix — lines read but not accumulated
#    after the startup deadline, preventing unbounded list growth
# 7. Removed unused _argv_to_display() helper
#
# All existing public names, signatures, and result fields are UNCHANGED.
# ==========================================================

import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator
from buddy.prompts.terminal_prompts import (
    tool_call_format,
    TERMINAL_ERROR_RECOVERY_PROMPT,
    TERMINAL_TOOL_PROMPT,
)

# ──────────────────────────────────────────────────────────
# Platform detection
# ──────────────────────────────────────────────────────────

_WINDOWS: bool = sys.platform == "win32"

# Seconds to wait for early output from a daemon before returning
_DAEMON_STARTUP_WAIT: float = 5.0

# Max chars captured from stdout/stderr before truncation (~2 k tokens)
_MAX_OUTPUT_CHARS: int = 8_000


def _truncate(text: str) -> str:
    """Truncate output to _MAX_OUTPUT_CHARS with a notice appended."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    omitted = len(text) - _MAX_OUTPUT_CHARS
    return text[:_MAX_OUTPUT_CHARS] + f"\n...[truncated: {omitted} chars omitted]"


# ==========================================================
# Runtime context (minimal)  — UNCHANGED
# ==========================================================


@dataclass
class RuntimeContext:
    now_iso: str
    timezone: str


class TerminalCall(BaseModel):
    """
    Executor-facing tool call.
    """

    cwd: str
    command: str
    timeout: int = 30


# ==========================================================
# Tool execution result schema (EXECUTOR → SYSTEM)
# ==========================================================


class TerminalResult(BaseModel):
    """
    Aggregated result for ONE planner step (LOCKED v1).

    - ok: overall success (stops at first failure)
    - outputs: per-command results in the order executed
    - ms: total duration for the tool call

    v1.1 additions (both default — fully backward-compatible):
    - IS_DAEMON : True when the process is running in the background
    - PID       : PID of the background process; None for normal commands
    """

    OK: bool
    CWD: str
    COMMAND: str
    EXIT_CODE: int
    STDOUT: str
    STDERR: str
    TIMEOUT: int
    IS_DAEMON: bool = False
    PID: Optional[int] = None


# ==========================================================
# Platform-independent helpers
# ==========================================================


def _popen_kwargs() -> Dict[str, Any]:
    """
    Return extra kwargs for Popen that isolate the child from Buddy's terminal.

    Unix    — start_new_session=True puts the child in a new session and
              process group.  os.killpg() can kill the entire process tree.
    Windows — CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW achieve the same
              isolation; os.kill + CTRL_BREAK_EVENT or taskkill /F /T is
              used to kill the tree.
    """
    if _WINDOWS:
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW  # type: ignore
            )
        }
    return {"start_new_session": True}


def _kill_process(proc: subprocess.Popen) -> None:
    """
    Terminate *proc* and its entire child tree, cross-platform.

    Unix  — SIGTERM the process group; escalate to SIGKILL after 0.5 s.
    Windows — taskkill /F /T (force-terminates the whole tree reliably).

    Safe to call on an already-dead process.
    """
    if proc.poll() is not None:
        return  # already exited

    if _WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return

    # Unix: kill the process group
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:
            pass

    # Wait up to 0.5 s for graceful exit
    deadline = time.monotonic() + 0.5
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)

    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                pass

    # Reap the shell process so it doesn't linger as a zombie
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


# ==========================================================
# Daemon command detection
# ==========================================================

_DAEMON_PATTERNS: List[re.Pattern] = [
    # JS / Node dev servers
    re.compile(r"\b(npm|yarn|pnpm)\s+(start|run\s+dev|run\s+start)\b", re.I),
    re.compile(r"\b(npx|bunx)\s+\S*\b(server|serve|dev|watch)\b", re.I),
    re.compile(r"\b(vite|webpack-dev-server|next\s+dev|nuxt\s+dev)\b", re.I),
    re.compile(r"\b(react-scripts|vue-cli-service)\s+start\b", re.I),
    re.compile(r"\bng\s+serve\b", re.I),
    # Python ASGI / WSGI servers
    re.compile(r"\b(uvicorn|gunicorn|hypercorn|daphne)\b", re.I),
    re.compile(r"\bflask\s+run\b", re.I),
    re.compile(r"\bdjango.*runserver\b", re.I),
    re.compile(r"\bpython3?\s+\S*\b(app|server|main|run|manage)\.py\b", re.I),
    re.compile(r"\bpython3?\s+-m\s+(http\.server|flask|uvicorn|streamlit)\b", re.I),
    re.compile(r"\bstreamlit\s+run\b", re.I),
    re.compile(r"\bfastapi\s+run\b", re.I),
    # Ruby / PHP / misc
    re.compile(r"\b(ruby|rails)\s+s(erver)?\b", re.I),
    re.compile(r"\bphp\s+-S\b", re.I),
    re.compile(r"\bhttp-server\b", re.I),
    # Databases / brokers running in foreground
    re.compile(r"\b(redis-server|mongod|mysqld|postgres)\b", re.I),
    # Workers / queues
    re.compile(r"\b(celery|dramatiq)\s+worker\b", re.I),
    # Watchers / streaming
    re.compile(r"\btail\s+-f\b", re.I),
    re.compile(r"\bjupyter\s+(notebook|lab|server)\b", re.I),
]


def _is_daemon_command(cmd: str) -> bool:
    """Return True if *cmd* is expected to run indefinitely."""
    return any(pat.search(cmd) for pat in _DAEMON_PATTERNS)


# ==========================================================
# Daemon registry
# ==========================================================


class _DaemonHandle:
    """Internal handle for one background process."""

    __slots__ = (
        "cmd",
        "pid",
        "startup_stdout",
        "startup_stderr",
        "started_at",
        "_proc",
    )

    def __init__(
        self,
        cmd: str,
        proc: subprocess.Popen,
        startup_stdout: str,
        startup_stderr: str,
    ) -> None:
        self.cmd = cmd
        self.pid = proc.pid
        self.startup_stdout = startup_stdout
        self.startup_stderr = startup_stderr
        self.started_at = time.time()
        self._proc = proc

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    def kill(self) -> None:
        _kill_process(self._proc)


class DaemonRegistry:
    """
    Registry of background processes started by Terminal.

    Accessible via Terminal.daemons:

        Terminal.daemons.list_alive()
        Terminal.daemons.kill_by_pid(pid)
        Terminal.daemons.kill_matching("npm")
        Terminal.daemons.kill_all()          # call on Buddy shutdown
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: Dict[int, _DaemonHandle] = {}

    def _register(self, h: _DaemonHandle) -> None:
        with self._lock:
            self._handles[h.pid] = h

    def list_alive(self) -> List[Dict[str, Any]]:
        """Return info dicts for every still-running background process."""
        with self._lock:
            return [
                {"pid": h.pid, "cmd": h.cmd, "started_at": h.started_at}
                for h in self._handles.values()
                if h.is_alive()
            ]

    def kill_by_pid(self, pid: int) -> bool:
        """Kill a specific process. Returns True if found and killed."""
        with self._lock:
            h = self._handles.pop(pid, None)
        if h:
            h.kill()
            return True
        return False

    def kill_matching(self, pattern: str) -> List[int]:
        """Kill all background processes whose command contains *pattern*."""
        killed: List[int] = []
        with self._lock:
            for pid, h in list(self._handles.items()):
                if pattern.lower() in h.cmd.lower():
                    h.kill()
                    killed.append(pid)
            for pid in killed:
                self._handles.pop(pid, None)
        return killed

    def kill_all(self) -> None:
        """Terminate every tracked background process (use on Buddy shutdown)."""
        with self._lock:
            for h in self._handles.values():
                h.kill()
            self._handles.clear()


# ==========================================================
# Terminal tool
# ==========================================================


class Terminal:
    """
    Terminal tool (LOCKED v1).
    Executes OS commands using argv lists (no shell).
    """

    tool_name = "terminal"
    version = "1.2.0"

    # Shared registry of all background processes
    daemons = DaemonRegistry()

    # --------------------------
    # Tool info (PROMPT + SCHEMA)
    # --------------------------

    def get_info(self) -> Dict[str, Any]:

        return {
            "name": self.tool_name,
            "version": self.version,
            "description": (
                "Runs shell commands.\nUSE FOR:\n  • Run code/scripts — python, node,"
                " go run, cargo run, java -jar, ruby, bash\n  • Tests — pytest, jest,"
                " go test, cargo test, rspec\n  • Compilers/builders — gcc, make, tsc,"
                " mvn, gradle\n  • Package managers — pip, npm, yarn, brew, apt,"
                " cargo\n  • Git and version control\n  • System utilities, network"
                " commands (curl, ping, ssh)\n  • Process management and installs\nDO"
                " NOT USE FOR: read/write/search files — use filesystem tool"
                " instead.\nPREFER structured tools over terminal when available;"
                " terminal returns raw text.\nDAEMON AWARE: servers/watchers (uvicorn,"
                " npm start, tail -f, etc.) are auto-detected, launched in background,"
                " and tracked in Terminal.daemons."
            ),
            "prompt": TERMINAL_TOOL_PROMPT,
            "error_prompt": TERMINAL_ERROR_RECOVERY_PROMPT,
            "tool_call_format": tool_call_format,
        }

    # --------------------------
    # Validation
    # --------------------------

    def parse_call(self, payload: Dict[str, Any]) -> TerminalCall:
        return TerminalCall.model_validate(payload)

    # --------------------------
    # Execution
    # --------------------------

    async def execute(
        self,
        call: TerminalCall,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        goal: str = "",
        brain: Optional[Any] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:

        cwd = call.cwd
        if cwd in ["", "None", "null"]:
            cwd = None

        # for idx, cmd in enumerate(call.command, start=1):
        cmd = str(call.command or "").strip()
        if on_progress:
            cmd_preview = cmd.replace("\r", " ").replace("\n", " ").strip()[:80]
            on_progress(f"Executing {cmd_preview}", False)

        if not cmd:
            result = TerminalResult(
                OK=False,
                CWD=cwd or "",
                COMMAND=cmd,
                EXIT_CODE=2,
                STDOUT="",
                STDERR="Empty command string",
                TIMEOUT=30,
            )
            return result.model_dump()

        timeout = int(call.timeout)

        if _is_daemon_command(cmd):
            return self._execute_daemon(cmd, cwd=cwd, timeout=timeout)
        return self._execute_normal(cmd, cwd=cwd, timeout=timeout)

    # --------------------------
    # Normal execution
    # --------------------------

    def _execute_normal(
        self,
        cmd: str,
        *,
        cwd: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """
        Run a command expected to exit on its own.

        Uses Popen + communicate() instead of subprocess.run() so that on
        TimeoutExpired we have the process handle available to kill it.
        subprocess.run() raises TimeoutExpired but does not kill the process
        when shell=True — this was the bug in v1.0.
        """
        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                **_popen_kwargs(),
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # ── Bug fix: kill before reading residual output ──────────
                _kill_process(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except Exception:
                    stdout, stderr = "", ""
                return TerminalResult(
                    OK=False,
                    CWD=cwd or "",
                    COMMAND=cmd,
                    EXIT_CODE=124,
                    STDOUT=stdout or "",
                    STDERR=f"TimeoutExpired: exceeded {timeout}s",
                    TIMEOUT=timeout,
                ).model_dump()

            ok = proc.returncode == 0
            return TerminalResult(
                OK=ok,
                CWD=cwd or "",
                COMMAND=cmd,
                EXIT_CODE=int(proc.returncode),
                STDOUT=_truncate(stdout or ""),
                STDERR=_truncate(stderr or ""),
                TIMEOUT=timeout,
            ).model_dump()

        except Exception as e:
            if proc is not None:
                _kill_process(proc)
            return TerminalResult(
                OK=False,
                CWD=cwd or "",
                COMMAND=cmd,
                EXIT_CODE=1,
                STDOUT="",
                STDERR=f"{type(e).__name__}: {e}",
                TIMEOUT=timeout,
            ).model_dump()

    # --------------------------
    # Daemon execution
    # --------------------------

    def _execute_daemon(
        self,
        cmd: str,
        *,
        cwd: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """
        Start a long-running command in the background and return immediately.

        The process is launched with Popen (non-blocking).  Two reader threads
        collect stdout/stderr for _DAEMON_STARTUP_WAIT seconds so that:
          • Boot messages and listening-port lines are captured and returned.
          • Immediate crashes are detected and surfaced (OK=False, EXIT_CODE set).

        The *timeout* field is intentionally ignored for daemon commands — they
        run until explicitly killed via Terminal.daemons.kill_by_pid(pid) or
        Terminal.daemons.kill_matching("pattern").
        """
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,  # line-buffered for real-time reads
                **_popen_kwargs(),
            )
        except Exception as e:
            return TerminalResult(
                OK=False,
                CWD=cwd or "",
                COMMAND=cmd,
                EXIT_CODE=1,
                STDOUT="",
                STDERR=f"{type(e).__name__}: {e}",
                TIMEOUT=timeout,
                IS_DAEMON=True,
                PID=None,
            ).model_dump()

        # Collect early output on background threads
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        deadline = time.monotonic() + _DAEMON_STARTUP_WAIT

        def _reader(stream, buf: List[str]) -> None:
            # Accumulate lines until deadline; after that drain to prevent
            # the OS pipe buffer from blocking the child, but stop storing.
            try:
                for line in stream:
                    if time.monotonic() <= deadline:
                        buf.append(line)
                    # else: keep reading (drain) but don't grow the buffer
            except Exception:
                pass

        t_out = threading.Thread(
            target=_reader, args=(proc.stdout, stdout_lines), daemon=True
        )
        t_err = threading.Thread(
            target=_reader, args=(proc.stderr, stderr_lines), daemon=True
        )
        t_out.start()
        t_err.start()
        t_out.join(timeout=_DAEMON_STARTUP_WAIT + 0.5)
        t_err.join(timeout=_DAEMON_STARTUP_WAIT + 0.5)

        early_out = _truncate("".join(stdout_lines))
        early_err = _truncate("".join(stderr_lines))

        # Register so Buddy can kill it later
        handle = _DaemonHandle(
            cmd=cmd,
            proc=proc,
            startup_stdout=early_out,
            startup_stderr=early_err,
        )
        Terminal.daemons._register(handle)

        # Did it crash during startup?
        exit_code = proc.poll()
        if exit_code is not None:
            return TerminalResult(
                OK=False,
                CWD=cwd or "",
                COMMAND=cmd,
                EXIT_CODE=int(exit_code),
                STDOUT=early_out,
                STDERR=early_err or f"Process exited immediately (code {exit_code})",
                TIMEOUT=timeout,
                IS_DAEMON=True,
                PID=proc.pid,
            ).model_dump()

        # Still alive — running in background
        return TerminalResult(
            OK=True,
            CWD=cwd or "",
            COMMAND=cmd,
            EXIT_CODE=0,
            STDOUT=early_out,
            STDERR=early_err,
            TIMEOUT=timeout,
            IS_DAEMON=True,
            PID=proc.pid,
        ).model_dump()


# ==========================================================
# Dynamic registry hooks  — UNCHANGED
# ==========================================================

TOOL_NAME = "terminal"
TOOL_CLASS = Terminal


def get_tool() -> Terminal:
    return Terminal()
