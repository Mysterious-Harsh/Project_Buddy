from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, Optional

from buddy.prompts.clipboard_prompts import CLIPBOARD_TOOL_PROMPT

_TOOL = "clipboard"


def _ok(**extra: Any) -> Dict[str, Any]:
    return {"OK": True, "TOOL": _TOOL, **extra}


def _err(msg: str) -> Dict[str, Any]:
    return {"OK": False, "TOOL": _TOOL, "ERROR": msg}


# ── platform helpers ─────────────────────────────────────────────────────────


def _get() -> str:
    if sys.platform == "darwin":
        r = subprocess.run(["pbpaste"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "pbpaste failed")
        return r.stdout

    elif sys.platform == "win32":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "PowerShell Get-Clipboard failed")
        return r.stdout.rstrip("\r\n")

    else:  # Linux / FreeBSD
        if shutil.which("xclip"):
            r = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                _raise_linux_error(r.stderr.strip(), "xclip")
            return r.stdout

        if shutil.which("xsel"):
            r = subprocess.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                _raise_linux_error(r.stderr.strip(), "xsel")
            return r.stdout

        if shutil.which("wl-paste"):
            r = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or "wl-paste failed")
            return r.stdout

        raise RuntimeError(
            "No clipboard backend found. Install xclip: sudo apt install xclip"
        )


def _set(text: str) -> None:
    if sys.platform == "darwin":
        r = subprocess.run(["pbcopy"], input=text, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "pbcopy failed")

    elif sys.platform == "win32":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
            input=text, capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "PowerShell Set-Clipboard failed")

    else:  # Linux / FreeBSD
        if shutil.which("xclip"):
            r = subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text, capture_output=True, text=True,
            )
            if r.returncode != 0:
                _raise_linux_error(r.stderr.strip(), "xclip")
            return

        if shutil.which("xsel"):
            r = subprocess.run(
                ["xsel", "--clipboard", "--input"],
                input=text, capture_output=True, text=True,
            )
            if r.returncode != 0:
                _raise_linux_error(r.stderr.strip(), "xsel")
            return

        if shutil.which("wl-copy"):
            r = subprocess.run(
                ["wl-copy"],
                input=text, capture_output=True, text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip() or "wl-copy failed")
            return

        raise RuntimeError(
            "No clipboard backend found. Install xclip: sudo apt install xclip"
        )


def _raise_linux_error(stderr: str, tool: str) -> None:
    if "Cannot open display" in stderr or "DISPLAY" in stderr:
        raise RuntimeError(
            "No display available. Clipboard requires a running graphical session."
        )
    raise RuntimeError(stderr or f"{tool} failed")


# ── tool ─────────────────────────────────────────────────────────────────────


class Clipboard:
    tool_name = _TOOL
    version = "1.0.0"

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.tool_name,
            "version": self.version,
            "description": "Read from or write to the system clipboard.",
            "prompt": CLIPBOARD_TOOL_PROMPT,
        }

    async def execute(
        self,
        function: str,
        arguments: Dict[str, Any],
        on_progress: Optional[Callable] = None,
        **_: Any,
    ) -> Dict[str, Any]:
        fn = str(function or "").strip().lower()
        if fn == "read":
            return self._read()
        if fn == "write":
            return self._write(arguments)
        return _err(f"Unknown function: {function!r}. Must be read or write.")

    def _read(self) -> Dict[str, Any]:
        try:
            content = _get()
            return _ok(CONTENT=content, LENGTH=len(content))
        except RuntimeError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"{type(e).__name__}: {e}")

    def _write(self, args: Dict[str, Any]) -> Dict[str, Any]:
        text = args.get("text")
        if text is None:
            return _err("text is required")
        text = str(text)
        try:
            _set(text)
            return _ok(LENGTH=len(text))
        except RuntimeError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"{type(e).__name__}: {e}")


# ── registry hooks ────────────────────────────────────────────────────────────

TOOL_NAME = "clipboard"
TOOL_CLASS = Clipboard


def get_tool() -> Clipboard:
    return Clipboard()
