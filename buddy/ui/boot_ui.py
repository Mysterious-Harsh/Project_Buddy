# -*- coding: utf-8 -*-
"""
buddy/ui/boot_ui.py  —  Aurora Gradient Theme (Style 5)

PALETTE (single source of truth — the AURORA dict):
  Logo rows:  bright-cyan -> cyan -> bright-blue -> blue -> magenta -> violet  (top -> bottom)
  Borders:    bright cyan
  Keys:       bright cyan
  Accent:     bright blue  — greeting, spinner dots
  Tagline:    bright magenta/violet — subtitle lines
  Deco lines: bright cyan  * —- *
  Matrix:     same aurora arc, cyan -> blue -> indigo -> violet during neural activation

boot.py imports:  Spinner, _birth_animation, _c, _center_visible, _color_frame,
                  _fail, _frame, _info, _matrix_stream_reveal, _ok, _term_clear,
                  _term_size, _warn, print_banner_centered
widgets.py imports: AURORA, _buddy_title_lines, _logo_row_code, _supports_unicode
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import threading
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

# =======================================================================
# AURORA — single source of truth for every color in Buddy
#
# main.py and bootstrap.py import this dict directly so there is
# exactly one place to change the entire visual palette.
#
# Aurora arc: bright-cyan (top) -> cyan -> bright-blue -> blue -> magenta -> violet (bottom)
# =======================================================================

AURORA: Dict[str, str] = {
    # — Logo gradient — 256-color codes matching the preview exactly —
    #    Hex targets from preview JS: #00e5ff -> #22ccff -> #44aaee -> #7c80ff -> #aa66ff -> #7c4dff
    "logo_r0": "\033[38;5;51m",  # #00ffff  ~= #00e5ff — bright cyan (aurora peak)
    "logo_r1": "\033[38;5;45m",  # #00d7ff  ~= #22ccff — light cyan
    "logo_r2": "\033[38;5;75m",  # #5fafff  ~= #44aaee — cyan-blue
    "logo_r3": "\033[38;5;105m",  # #8787ff  ~= #7c80ff — blue-indigo
    "logo_r4": "\033[38;5;135m",  # #af5fff  ~= #aa66ff — violet
    "logo_r5": "\033[38;5;99m",  # #875fff  ~= #7c4dff — deep violet (aurora base)
    # — 8/16-color fallback codes (used when 256-color is unavailable) -
    "logo_r0_basic": "\033[96m",  # bright cyan
    "logo_r1_basic": "\033[96m",  # bright cyan
    "logo_r2_basic": "\033[94m",  # bright blue
    "logo_r3_basic": "\033[94m",  # bright blue
    "logo_r4_basic": "\033[95m",  # bright magenta
    "logo_r5_basic": "\033[95m",  # bright magenta
    # — UI roles ———————————————————————————-
    "border": "\033[38;5;51m",  # bright cyan    — bullet dots, separators
    "key": "\033[38;5;51m",  # bright cyan    — command keywords
    "accent": "\033[38;5;51m",  # bright cyan    — greeting, spinner
    "tagline": "\033[38;5;99m",  # deep violet    — subtitle / tagline
    "deco": "\033[38;5;51m",  # bright cyan    — * decorative chars
    "ok": "\033[92m",  # bright green   — success ticks
    "warn": "\033[93m",  # bright yellow  — warnings / system info
    "err": "\033[91m",  # bright red     — errors
    "info": "\033[38;5;75m",  # cyan-blue      — info dots in bootstrap
    "dim": "\033[2m",  # dim            — secondary / meta text
    "white": "\033[97m",  # bright white   — heartbeat flash
    "reset": "\033[0m",
}

_LOGO_ROW_KEYS: Tuple[str, ...] = (
    "logo_r0",
    "logo_r1",
    "logo_r2",
    "logo_r3",
    "logo_r4",
    "logo_r5",
)

# Cached 256-color support flag
_256COLOR_SUPPORT: Optional[bool] = None


def _supports_256color() -> bool:
    """Return True when the terminal can render 256-colour ANSI codes."""
    global _256COLOR_SUPPORT
    if _256COLOR_SUPPORT is not None:
        return _256COLOR_SUPPORT
    if os.getenv("NO_COLOR"):
        _256COLOR_SUPPORT = False
        return False
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        _256COLOR_SUPPORT = True
        return True
    term = os.environ.get("TERM", "")
    if "256color" in term or "kitty" in term or "xterm" in term:
        _256COLOR_SUPPORT = True
        return True
    try:
        import subprocess

        r = subprocess.run(
            ["tput", "colors"], capture_output=True, text=True, timeout=0.3
        )
        if r.returncode == 0 and int(r.stdout.strip()) >= 256:
            _256COLOR_SUPPORT = True
            return True
    except Exception:
        pass
    _256COLOR_SUPPORT = False
    return False


def _logo_row_code(i: int) -> str:
    """Return the best ANSI color code for logo row i (0-5)."""
    key = _LOGO_ROW_KEYS[i]
    if _supports_256color():
        return AURORA[key]
    return AURORA[key + "_basic"]


# =======================================================================
# Terminal capability
# =======================================================================

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _supports_ansi() -> bool:
    return not os.getenv("NO_COLOR") and sys.stdout.isatty()


def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _supports_unicode() -> bool:
    # 1. Explicit opt-out
    if os.getenv("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    # 2. Locale encoding — reliable even when stdout is redirected (e.g. Textual)
    try:
        import locale
        if "utf" in (locale.getpreferredencoding(False) or "").lower():
            return True
    except Exception:
        pass
    # 3. LANG / LC_ALL / LC_CTYPE env vars
    lang_env = (
        os.environ.get("LANG", "")
        + os.environ.get("LC_ALL", "")
        + os.environ.get("LC_CTYPE", "")
    ).lower()
    if "utf" in lang_env:
        return True
    # 4. Fallback: stdout encoding
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in enc


# =======================================================================
# Color helpers
# =======================================================================


def _c(s: str, role: str) -> str:
    """Apply a named AURORA role to a string."""
    if not _supports_ansi():
        return s
    code = AURORA.get(role, "")
    return f"{code}{s}{AURORA['reset']}" if code else s


def _raw_c(code: str, s: str) -> str:
    """Apply a raw ANSI escape string (e.g. for per-row logo codes)."""
    if not _supports_ansi():
        return s
    return f"{code}{s}{AURORA['reset']}"


def _aurora_phase_color(p: float) -> str:
    """
    Progress-mapped color for the neural activation matrix reveal.
    Uses 256-color codes when available for a smooth 6-step aurora arc.
    Arc: bright-cyan -> light-cyan -> cyan-blue -> indigo -> violet -> deep-violet
    """
    if p < 0.17:
        return _logo_row_code(0)
    if p < 0.34:
        return _logo_row_code(1)
    if p < 0.51:
        return _logo_row_code(2)
    if p < 0.68:
        return _logo_row_code(3)
    if p < 0.84:
        return _logo_row_code(4)
    return _logo_row_code(5)


# =======================================================================
# Display geometry
# =======================================================================


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _display_width(s: str) -> int:
    s = _strip_ansi(s)
    w = 0
    for ch in s:
        if ch == "\n" or unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _term_width(default: int = 96) -> int:
    """Current terminal column count — sampled fresh each call."""
    try:
        return max(60, shutil.get_terminal_size((default, 24)).columns)
    except Exception:
        return default


def _term_size() -> Tuple[int, int]:
    """Return (columns, rows) sampled in one syscall to avoid race on resize."""
    try:
        sz = shutil.get_terminal_size((96, 24))
        return max(60, sz.columns), max(10, sz.lines)
    except Exception:
        return 96, 24


def _center_visible(s: str, width: int) -> str:
    vis = _display_width(s)
    return s if vis >= width else (" " * ((width - vis) // 2)) + s


def _pad_to_width(s: str, width: int) -> str:
    w = _display_width(s)
    return s if w >= width else s + (" " * (width - w))


def _truncate_visible(s: str, width: int) -> str:
    raw = _strip_ansi(s)
    if _display_width(raw) <= width:
        return s
    out, cur = [], 0
    for ch in raw:
        if ch == "\n":
            continue
        step = (
            0
            if unicodedata.combining(ch)
            else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        )
        if cur + step > width:
            break
        out.append(ch)
        cur += step
    return "".join(out)


# =======================================================================
# Frame / box drawing
# =======================================================================


def _frame(lines: List[str], inner_width: int) -> str:
    top = "╭" + ("─" * inner_width) + "╮"
    bot = "╰" + ("─" * inner_width) + "╯"
    body = []
    for ln in lines:
        ln2 = _truncate_visible(ln, inner_width)
        ln2 = _pad_to_width(ln2, inner_width)
        body.append("│" + ln2 + "│")
    return "\n".join([top, *body, bot])


def _color_frame(block: str) -> str:
    """Apply aurora border color to a _frame()-generated box."""
    if not _supports_ansi():
        return block
    bc = AURORA["border"]
    rst = AURORA["reset"]
    out = []
    for ln in block.splitlines():
        if not ln:
            out.append(ln)
            continue
        if ln[0] in ("╭", "╰"):
            out.append(f"{bc}{ln}{rst}")
            continue
        if ln[0] == "│" and ln[-1] == "│":
            out.append(f"{bc}│{rst}{ln[1:-1]}{bc}│{rst}")
            continue
        out.append(ln)
    return "\n".join(out)


# =======================================================================
# Banner
# =======================================================================


def _buddy_title_lines() -> List[str]:
    """
    Return the logo glyph rows.
    Uses full block-art on UTF-8 terminals, plain ASCII art elsewhere.
    """
    if _supports_unicode():
        return [
            "██████╗ ██╗   ██╗██████╗ ██████╗ ██╗   ██╗",
            "██╔══██╗██║   ██║██╔══██╗██╔══██╗╚██╗ ██╔╝",
            "██████╔╝██║   ██║██║  ██║██║  ██║ ╚████╔╝ ",
            "██╔══██╗██║   ██║██║  ██║██║  ██║  ╚██╔╝  ",
            "██████╔╝╚██████╔╝██████╔╝██████╔╝   ██║   ",
            "╚═════╝  ╚═════╝ ╚═════╝ ╚═════╝    ╚═╝   ",
        ]
    # — Pure ASCII fallback (non-UTF-8 / dumb terminals) ———————
    return [
        r" ____  _   _ ____  ____  _  _",
        r"| __ )| | | |  _ \|  _ \| || |",
        r"|  _ \| | | | | | | | | | || |_",
        r"| |_) | |_| | |_| | |_| |__   _|",
        r"|____/ \___/|____/|____/   |_|",
    ]


def _banner_centered(
    *,
    user_name: str = "",
    gpu_label: str = "",
    ram_gb: str = "",
    llm_label: str = "",
    web_engine: str = "",
    stt: bool = False,
    tts: bool = False,
    _cols: int = 0,  # live terminal columns; 0 = auto-detect
) -> str:
    """
    Full Aurora Gradient banner — printed at the TOP of the terminal,
    horizontally centred in the live terminal width.

    Every text line is individually centred in `cols` — so on a 200-col
    terminal the logo, tagline, and commands all sit exactly in the middle.
    No inner sub-box capping at 100 cols.
    """
    cols = _cols if _cols > 0 else _term_width()

    def cen(s: str) -> str:
        return _center_visible(s, cols)

    out: List[str] = [""]

    # — aurora gradient logo, centred in full terminal width —————
    # Use _fade_reveal_logo at full brightness (fade_p=1.0)
    out.extend(_fade_reveal_logo(_buddy_title_lines(), 1.0, cols))
    out.append("")

    # — tagline — deep violet ————————————————————-
    for part in textwrap.wrap(
        "Cognitive AI Assistant  \u00b7  Offline-first  \u00b7  Memory-driven",
        width=max(24, cols - 10),
    ):
        out.append(cen(_c(part, "tagline")))

    # — version dim —————————————————————————
    for part in textwrap.wrap(
        "v1.0  \u00b7  llama.cpp  \u00b7  Qdrant memory  \u00b7  Sentence-Transformers",
        width=max(24, cols - 10),
    ):
        out.append(cen(_c(part, "dim")))

    out.append("")

    # — hardware row (optional) ———————————————————-
    info: List[str] = []
    if gpu_label:
        info.append(f"GPU: {gpu_label}")
    if ram_gb:
        info.append(f"RAM: {ram_gb} GB")
    if llm_label:
        info.append(f"LLM: {llm_label}")
    if info:
        out.append(cen(_c("  \u00b7  ".join(info), "warn")))

    # — settings row (web engine + voice) ————————————-
    settings: List[str] = []
    if web_engine:
        settings.append(f"Web: {web_engine}")
    voice_parts = (["STT"] if stt else []) + (["TTS"] if tts else [])
    settings.append("Voice: " + ("+".join(voice_parts) if voice_parts else "off"))
    out.append(cen(_c("  \u00b7  ".join(settings), "dim")))
    out.append("")

    # — faint separator ———————————————————————-
    sep_len = min(32, max(16, cols // 4))
    out.append(cen(_c("\u2500" * sep_len, "dim")))
    out.append("")

    # — greeting — bright cyan * ——————————————————-
    greet = f"Welcome back, {user_name}." if user_name else "Ready for commands."
    out.append(cen(_c(f"\u2726   {greet}   \u2726", "accent")))
    out.append("")
    out.append(cen(_c("\u2500\u2500\u2500 Quick reference \u2500\u2500\u2500", "dim")))
    out.append("")

    # — commands — deep-violet bullet, bright-cyan key ————————
    cmds = [
        ("exit", "Exit Buddy"),
        ("F2", "Toggle Mute"),
        ("F3", "Toggle Sleep"),
        ("Ctrl+C", "Interrupt Buddy"),
    ]
    CMD_W = max(len(k) for k, _ in cmds) + 2
    DESC_W = max(len(v) for _, v in cmds)
    row_vis = 2 + 1 + 2 + CMD_W + 2 + DESC_W
    indent = " " * max(0, (cols - row_vis) // 2)

    bullet_code = _logo_row_code(5)  # deep violet  #875fff
    key_code = _logo_row_code(0)  # bright cyan  #00ffff

    # _banner_centered is only reached when _supports_ansi() is True — no check needed
    bullet = _raw_c(bullet_code, "\u2022")
    for k, v in cmds:
        key = _raw_c(key_code, f"{k:<{CMD_W}}")
        desc = _c(f"{v:<{DESC_W}}", "dim")
        out.append(indent + "  " + bullet + "  " + key + "  " + desc)

    out.append("")
    return "\n".join(out)


def _banner_plain(
    *,
    user_name: str = "",
    gpu_label: str = "",
    ram_gb: str = "",
    llm_label: str = "",
    web_engine: str = "",
    stt: bool = False,
    tts: bool = False,
    cols: int = 0,
) -> str:
    """
    Completely plain text banner — no ANSI, no unicode box-drawing.
    Used when the terminal cannot render colors or unicode.
    Centered horizontally with spaces; vertical centering handled by caller.
    """
    w = cols if cols > 0 else _term_width()
    inner = min(96, max(50, w - 4))

    def cen(s: str) -> str:
        pad = max(0, (inner - len(s)) // 2)
        return " " * pad + s

    lines: List[str] = [""]

    # ASCII logo (no colors)
    for ln in _buddy_title_lines():  # already ASCII-safe from _buddy_title_lines()
        lines.append(cen(ln))
    lines.append("")

    # Tagline
    for part in textwrap.wrap(
        "Cognitive AI Assistant  |  Offline-first  |  Memory-driven",
        width=max(24, inner - 8),
    ):
        lines.append(cen(part))

    # Version
    lines.append(cen("v1.0  |  llama.cpp  |  Qdrant  |  Sentence-Transformers"))
    lines.append("")

    # Hardware row
    info: List[str] = []
    if gpu_label:
        info.append(f"GPU: {gpu_label}")
    if ram_gb:
        info.append(f"RAM: {ram_gb} GB")
    if llm_label:
        info.append(f"LLM: {llm_label}")
    if info:
        lines.append(cen("  |  ".join(info)))

    # Settings row
    settings: List[str] = []
    if web_engine:
        settings.append(f"Web: {web_engine}")
    voice_parts = (["STT"] if stt else []) + (["TTS"] if tts else [])
    settings.append("Voice: " + ("+".join(voice_parts) if voice_parts else "off"))
    lines.append(cen("  |  ".join(settings)))
    lines.append("")

    # Separator
    lines.append(cen("-" * min(28, inner // 3)))
    lines.append("")

    # Greeting
    greet = f"Welcome back, {user_name}." if user_name else "Ready for commands."
    lines.append(cen(f" *   {greet}   *"))
    lines.append("")
    lines.append(cen("-- Quick reference --"))
    lines.append("")

    # Commands plain
    cmds = [
        ("exit", "Exit Buddy"),
        ("F2", "Toggle Mute"),
        ("F3", "Toggle Sleep"),
        ("Ctrl+C", "Interrupt Buddy"),
    ]
    _CMD_W = max(len(k) for k, _ in cmds) + 2
    _DESC_W = max(len(v) for _, v in cmds)
    _row_vis = 2 + 1 + 2 + _CMD_W + 2 + _DESC_W
    _indent = " " * max(0, (inner - _row_vis) // 2)
    for k, v in cmds:
        lines.append(_indent + "  *  " + f"{k:<{_CMD_W}}" + "  " + f"{v:<{_DESC_W}}")

    lines.append("")
    return "\n".join(lines)


def print_banner_centered(
    *,
    user_name: str = "",
    gpu_label: str = "",
    ram_gb: str = "",
    llm_label: str = "",
    web_engine: str = "",
    stt: bool = False,
    tts: bool = False,
) -> None:
    """
    Print the Buddy banner:
      * Horizontally centred in the LIVE terminal width (re-queried each call)
      * Printed from the TOP of the terminal — no vertical padding
      * Auto-selects render path:
          ANSI + 256-color + UTF-8  ->  full aurora gradient (8-level ramps)
          ANSI + UTF-8, no 256-col  ->  8-color gradient fallback
          ANSI, no UTF-8            ->  ASCII art with color
          No ANSI (pipe / CI)       ->  plain text, no escape codes
    """
    cols, _ = _term_size()

    if _supports_ansi():
        banner = _banner_centered(
            user_name=user_name,
            gpu_label=gpu_label,
            ram_gb=ram_gb,
            llm_label=llm_label,
            web_engine=web_engine,
            stt=stt,
            tts=tts,
            _cols=cols,
        )
    else:
        banner = _banner_plain(
            user_name=user_name,
            gpu_label=gpu_label,
            ram_gb=ram_gb,
            llm_label=llm_label,
            web_engine=web_engine,
            stt=stt,
            tts=tts,
            cols=cols,
        )

    # Print from top — no vertical offset.
    # The caller (bootstrap) is responsible for any surrounding spacing.
    sys.stdout.write(banner)
    if not banner.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


# =======================================================================
# Neural activation sequence — Aurora edition
# Color arc: bright-cyan -> cyan -> bright-blue -> magenta -> violet
# =======================================================================

_NEURAL_PHASES: List[Tuple[float, str]] = [
    (0.00, "SUBSTRATE OFFLINE        initializing base layer"),
    (0.10, "STORAGE LAYER            mounting SQLite + Qdrant"),
    (0.22, "SYSTEM PROFILER          reading hardware topology"),
    (0.34, "MODEL SELECTION          scanning capability matrix"),
    (0.46, "LANGUAGE ENGINE          loading quantized weights"),
    (0.58, "VECTOR INDEX             hydrating memory embeddings"),
    (0.70, "CONTEXT ENGINE           restoring conversation state"),
    (0.82, "EXECUTIVE PLANNER        binding tool executor"),
    (0.91, "IDENTITY STABLE          cognitive loop online"),
    (0.97, "CONSCIOUSNESS ESTABLISHED"),
]


def _current_phase(p: float) -> str:
    label = _NEURAL_PHASES[0][1]
    for threshold, msg in _NEURAL_PHASES:
        if p >= threshold:
            label = msg
        else:
            break
    return label


# — Per-row 8-level brightness ramps — simultaneous opacity 0->1 fade —-
# fade_p (0.0->1.0) applied uniformly to EVERY character at once,
# so the whole logo fades in together like CSS opacity 0->1.
# 8 steps: near-black(0) ... full aurora(7)  — verified 256-color indices.
_AURORA_RAMPS: Tuple[Tuple[str, ...], ...] = (
    # row 0 — bright cyan   target=51  (#00ffff)
    (
        "[38;5;235m",
        "[38;5;23m",
        "[38;5;30m",
        "[38;5;37m",
        "[38;5;44m",
        "[38;5;45m",
        "[38;5;51m",
        "[38;5;51m",
    ),
    # row 1 — light cyan    target=45  (#00d7ff)
    (
        "[38;5;235m",
        "[38;5;23m",
        "[38;5;30m",
        "[38;5;37m",
        "[38;5;38m",
        "[38;5;45m",
        "[38;5;45m",
        "[38;5;45m",
    ),
    # row 2 — cyan-blue     target=75  (#5fafff)
    (
        "[38;5;235m",
        "[38;5;17m",
        "[38;5;25m",
        "[38;5;32m",
        "[38;5;68m",
        "[38;5;74m",
        "[38;5;75m",
        "[38;5;75m",
    ),
    # row 3 — blue-indigo   target=105 (#8787ff)
    (
        "[38;5;235m",
        "[38;5;17m",
        "[38;5;55m",
        "[38;5;61m",
        "[38;5;97m",
        "[38;5;98m",
        "[38;5;105m",
        "[38;5;105m",
    ),
    # row 4 — violet        target=135 (#af5fff)
    (
        "[38;5;235m",
        "[38;5;53m",
        "[38;5;91m",
        "[38;5;127m",
        "[38;5;128m",
        "[38;5;134m",
        "[38;5;135m",
        "[38;5;135m",
    ),
    # row 5 — deep violet   target=99  (#875fff)
    (
        "[38;5;235m",
        "[38;5;54m",
        "[38;5;60m",
        "[38;5;91m",
        "[38;5;92m",
        "[38;5;98m",
        "[38;5;99m",
        "[38;5;99m",
    ),
)
_RAMP_STEPS = 8  # length of each row ramp


def _fade_reveal_logo(
    logo_lines: List[str],
    fade_p: float,
    cols: int,
) -> List[str]:
    """
    Simultaneous opacity fade-in: ALL characters reveal together.

    fade_p = 0.0  -> every character near-black  (opacity ~= 0)
    fade_p = 1.0  -> every character at full aurora colour  (opacity = 1)

    Each row uses its own gradient colour from _AURORA_RAMPS, so the
    per-row aurora colours are preserved, but ALL rows advance through
    brightness levels at the same speed — identical to CSS opacity 0->1.

    The logo is horizontally centred in the live terminal width (cols).
    """
    RST = AURORA["reset"]
    logo_w = max(len(ln) for ln in logo_lines)  # raw char width (44)
    logo_pad = max(0, (cols - logo_w) // 2)  # centering offset

    # Map fade_p -> ramp step index (0 ... _RAMP_STEPS-1)
    step = min(int(fade_p * _RAMP_STEPS), _RAMP_STEPS - 1)

    out: List[str] = []
    for row_i, raw_ln in enumerate(logo_lines):
        ramp = _AURORA_RAMPS[min(row_i, len(_AURORA_RAMPS) - 1)]
        color = ramp[step]
        # Colour the entire row at once (one ANSI code per line, not per char)
        # Spaces are left as-is so centering pads work correctly.
        colored = ""
        for ch in raw_ln:
            colored += " " if ch == " " else f"{color}{ch}{RST}"
        out.append(" " * logo_pad + colored)
    return out


def _neural_nodes(p: float, width: int) -> str:
    if not _supports_unicode():
        return ""
    n = min(38, (width - 8) // 2)
    lit = int(p * n)
    pc = _aurora_phase_color(p)
    parts = []
    for i in range(n):
        if i < lit:
            parts.append(
                _raw_c(AURORA["white"], "◉")
                if i == lit - 1
                else _raw_c(pc, "●") if i % 5 == 0 else _raw_c(pc, "·")
            )
        else:
            parts.append(_c("○", "dim"))
    return " ".join(parts)


def _progress_bar(p: float, width: int = 30) -> str:
    filled = int(p * width)
    pc = _aurora_phase_color(p)
    return (
        _c("[", "dim")
        + _raw_c(pc, "█" * filled)
        + _c("░" * (width - filled), "dim")
        + _c("]", "dim")
        + _raw_c(pc, f"  {int(p * 100):>3d}%")
    )


def _draw_frame(lines: List[str], cols: int, rows: int) -> None:
    """
    In-place redraw: vertically and horizontally centres all lines
    within the live terminal dimensions.  Uses a single write to
    minimise flicker on every frame.
    """
    content_h = len(lines)
    top_pad = max(0, (rows - content_h) // 2)

    HOME = "\033[H"
    ERASE = "\033[2K"
    CLEAR = "\033[J"

    buf: List[str] = [HOME]
    for _ in range(top_pad):
        buf.append(ERASE + "\n")

    for ln in lines:
        buf.append(ERASE + ln + "\n")

    buf.append(CLEAR)
    sys.stdout.write("".join(buf))
    sys.stdout.flush()


def _birth_animation() -> None:
    """
    First-boot only. Heartbeat sequence that plays before the regular
    matrix reveal — gives the feeling of Buddy being born.

    Sequence:
      1. Three heartbeat pulses:  ·  →  · ·  →  · · ·
         Each dot appears in aurora-cyan, then pulses white, then dims.
      2. A line of text: "first awakening..."  in dim
      3. Brief pause, then returns — caller fires _matrix_stream_reveal() next.

    Falls back to a plain print on non-TTY / no-ANSI terminals.
    """
    if not (_supports_ansi() and _is_tty()):
        print("Buddy — first awakening...")
        return

    _hide_cursor()
    try:
        cols, rows = _term_size()
        RST = AURORA["reset"]
        WH = AURORA["white"]
        DIM = AURORA["dim"]
        C0 = _logo_row_code(0)   # bright cyan
        C5 = _logo_row_code(5)   # deep violet

        def _draw(lines: List[str]) -> None:
            _draw_frame(lines, cols, rows)

        def _centered(s: str) -> str:
            return _center_visible(s, cols)

        # — Phase 1: dot pulses ————————————————————————
        dot_sequences = ["·", "· ·", "· · ·"]
        for seq in dot_sequences:
            # dim appear
            _draw([_centered(f"{DIM}{seq}{RST}")])
            time.sleep(0.25)
            # cyan flash
            _draw([_centered(f"{C0}{seq}{RST}")])
            time.sleep(0.15)
            # white heartbeat pulse
            _draw([_centered(f"{WH}{seq}{RST}")])
            time.sleep(0.12)
            # back to cyan
            _draw([_centered(f"{C0}{seq}{RST}")])
            time.sleep(0.20)

        # — Phase 2: hold · · · and show awakening text ———————
        awaken_text = _c("first awakening . . .", "dim")
        _draw([
            _centered(f"{C0}· · ·{RST}"),
            "",
            _centered(awaken_text),
        ])
        time.sleep(0.9)

        # — Phase 3: expand dots into aurora arc ——————————-
        # Brief cascade: dots grow from · · · to a horizontal pulse line
        arc_chars = ["·", "·", "·", "·", "·", "◉", "·", "·", "·", "·", "·"]
        for step in range(len(arc_chars)):
            lit = step + 1
            parts = []
            for i, ch in enumerate(arc_chars[:lit]):
                color = _logo_row_code(min(i, 5))
                parts.append(f"{color}{ch}{RST}")
            arc = "  ".join(parts)
            _draw([
                _centered(arc),
                "",
                _centered(awaken_text),
            ])
            time.sleep(0.07)

        # — Phase 4: white flash of full arc ——————————————-
        arc_full = "  ".join(f"{WH}{ch}{RST}" for ch in arc_chars)
        _draw([
            _centered(arc_full),
            "",
            _centered(awaken_text),
        ])
        time.sleep(0.18)

        # — Phase 5: fade line to violet + identity line ——————-
        id_text = _c("◈  B U D D Y  ◈", "tagline")
        _draw([
            _centered(f"{C5}{'  '.join(arc_chars)}{RST}"),
            "",
            _centered(id_text),
            "",
            _centered(awaken_text),
        ])
        time.sleep(0.70)

        _term_clear()

    finally:
        _show_cursor()


def _matrix_stream_reveal(duration: float = 3.2) -> None:
    """
    Aurora opacity fade-in boot animation.

    Phase 1 (p: 0 -> FADE_END):
        logo fades from invisible -> full aurora brightness simultaneously,
        like CSS opacity 0 -> 1. All characters advance through the same
        8-level brightness ramp at the same time.
    Phase 2 (p: FADE_END -> 1.0):
        logo holds at full brightness; status bar + progress continue.

    Everything is horizontally centred in the live terminal width,
    recomputed fresh every frame so window resize works seamlessly.
    Falls back to a plain one-liner when ANSI / UTF-8 unavailable.
    """
    if not (_supports_ansi() and _is_tty() and _supports_unicode()):
        print("BUDDY — booting...")
        return

    logo_lines = _buddy_title_lines()  # raw strings, no ANSI

    # — timing constants ——————————————————————-
    FADE_END = 0.45  # logo fully visible by 45 % of total duration
    HOLD_FLASH = 0.88  # brief white heartbeat at 88 %

    _hide_cursor()
    try:
        _term_clear()
        start_t = time.time()

        while True:
            elapsed = time.time() - start_t
            p = min(elapsed / duration, 1.0)

            # fade_p: 0->1 over first FADE_END of animation, then stays 1
            fade_p = min(p / FADE_END, 1.0)

            # Brief white heartbeat flash when logo first hits full brightness
            if FADE_END <= p <= FADE_END + 0.07:
                fade_p = 1.0  # already full — just note we can pulse
                flash = True
            else:
                flash = False

            pc = _aurora_phase_color(p)

            # — live terminal size ——————————————————
            cols, rows = _term_size()

            # — logo with simultaneous opacity fade —————————-
            if flash:
                # One-frame white flash at full-reveal moment
                RST = AURORA["reset"]
                wh = AURORA["white"]
                logo_w = max(len(ln) for ln in logo_lines)
                logo_pad = max(0, (cols - logo_w) // 2)
                logo_rows = []
                for ln in logo_lines:
                    colored = "".join(
                        " " if ch == " " else f"{wh}{ch}{RST}" for ch in ln
                    )
                    logo_rows.append(" " * logo_pad + colored)
            else:
                logo_rows = _fade_reveal_logo(logo_lines, fade_p, cols)

            # — status area (horizontally centred in cols) ——————
            header = (
                "COGNITIVE SYSTEM OFFLINE"
                if p < 0.08
                else (
                    "AURORA ACTIVATION IN PROGRESS"
                    if p < 0.93
                    else "CONSCIOUSNESS ESTABLISHED"
                )
            )
            bar_w = min(40, max(20, cols // 3))
            nodes_str = _neural_nodes(p, cols)
            phase_str = _raw_c(pc, f"  \u25b8  {_current_phase(p)}")
            bar_str = _progress_bar(p, width=bar_w)

            # Fade the status text in alongside the logo (slightly delayed)
            status_fade = min(max((p - 0.15) / 0.35, 0.0), 1.0)
            dim_code = AURORA["dim"] if status_fade < 0.5 else ""

            status_lines: List[str] = [
                "",
                _center_visible(_raw_c(pc, header), cols),
                "",
                _center_visible(nodes_str, cols) if nodes_str else "",
                "",
                _center_visible(phase_str, cols),
                "",
                _center_visible(bar_str, cols),
                "",
            ]

            frame = logo_rows + status_lines
            _draw_frame(frame, cols, rows)

            if p >= 1.0:
                break

            # Frame rate: 30 fps during fade (smooth), 25 fps after (lighter)
            time.sleep(0.033 if fade_p < 1.0 else 0.040)

        # — Final hold frame: full brightness, 0.55 s, then clear ——-
        cols, rows = _term_size()
        logo_rows = _fade_reveal_logo(logo_lines, 1.0, cols)
        sep_len = min(32, max(16, cols // 4))
        final = logo_rows + [
            "",
            _center_visible(_c("\u2500" * sep_len, "dim"), cols),
            "",
            _center_visible(_c("\u25c8  CONSCIOUSNESS ONLINE  \u25c8", "accent"), cols),
            "",
            _center_visible(_neural_nodes(1.0, cols), cols),
            "",
            _center_visible(_progress_bar(1.0, width=min(40, cols // 3)), cols),
            "",
            _center_visible(
                _c("all systems nominal  \u00b7  memory intact  \u00b7  ready", "dim"),
                cols,
            ),
            "",
        ]
        _draw_frame(final, cols, rows)
        time.sleep(0.55)

    finally:
        _term_clear()
        _show_cursor()


# =======================================================================
# Cursor / screen helpers
# =======================================================================


def _hide_cursor() -> None:
    if _supports_ansi():
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()


def _show_cursor() -> None:
    if _supports_ansi():
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def _term_clear() -> None:
    if _supports_ansi():
        sys.stdout.write("\033[3J\033[2J\033[H")
        sys.stdout.flush()


# =======================================================================
# Status helpers  (used by bootstrap.py)
# =======================================================================


def _ok(msg: str) -> None:
    sym = "  + " if not _supports_unicode() else "  ✓ "
    print(_c(sym, "ok") + msg)


def _warn(msg: str) -> None:
    print(_c("  ! ", "warn") + msg)


def _fail(msg: str) -> None:
    sym = "  x " if not _supports_unicode() else "  ✗ "
    print(_c(sym, "err") + msg)


def _info(msg: str) -> None:
    sym = "  . " if not _supports_unicode() else "  · "
    print(_c(sym, "info") + _c(msg, "dim"))


# =======================================================================
# Spinner
# =======================================================================


class Spinner:
    """
    Lightweight braille spinner for deterministic boot steps.

    *  DO NOT wrap tqdm-based downloads in a Spinner.
       snapshot_download / hf_hub_download own stdout via tqdm.
       Pattern: spinner.stop() -> print static line -> let tqdm run.
    """

    def __init__(self, enabled: bool, label: str) -> None:
        self.enabled = bool(enabled) and _is_tty()
        self.label = label
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._io_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._max_len = 0
        self._frames = (
            ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
            if _supports_unicode()
            else ["|", "/", "-", "\\"]
        )

    def start(self) -> None:
        print(_c("  . ", "info") + _c(self.label, "dim"))
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def update(self, label: str) -> None:
        self.label = label

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.25)
        with self._io_lock:
            sys.stdout.write("\r" + (" " * max(120, self._max_len)) + "\r")
            sys.stdout.flush()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def prompt_input(self, prompt: str) -> str:
        with self._io_lock:
            if self.enabled:
                self.pause()
                sys.stdout.write("\r" + (" " * max(120, self._max_len)) + "\r")
                sys.stdout.flush()
            try:
                return input(prompt)
            finally:
                if self.enabled:
                    self.resume()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            if self._pause.is_set():
                time.sleep(0.03)
                continue
            frame = self._frames[i % len(self._frames)]
            # Aurora: accent (bright blue) on the spinner dot
            line = (
                "\r" + _c("  " + frame + " ", "accent") + _c(self.label, "dim")
                if _supports_ansi()
                else f"\r  {frame} {self.label}"
            )
            vis = sum(
                (
                    0
                    if unicodedata.combining(ch)
                    else 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                )
                for ch in _strip_ansi(line)
            )
            self._max_len = max(self._max_len, vis)
            with self._io_lock:
                sys.stdout.write(line + " " * (self._max_len - vis))
                sys.stdout.flush()
            i += 1
            time.sleep(0.08)
