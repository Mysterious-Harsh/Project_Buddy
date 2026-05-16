"""
explore_accessibility_tree.py

Exploration script — dumps the macOS Accessibility (AX) tree for running
apps so we can understand the structure before building the screen-control
tool.

Shows per-element: role, subrole, title, description, value, position, size,
enabled/focused state, available attributes, available actions.

Run:
    mamba activate buddy
    python buddy/tests/explore_accessibility_tree.py
    python buddy/tests/explore_accessibility_tree.py --app Finder
    python buddy/tests/explore_accessibility_tree.py --app Terminal --depth 4
    python buddy/tests/explore_accessibility_tree.py --list-apps

Requirements:
    pip install pyobjc-framework-ApplicationServices pyobjc-framework-AppKit
    -- OR --
    pip install atomacos

macOS IMPORTANT:
    System Settings → Privacy & Security → Accessibility
    → grant access to Terminal (or whichever app runs this script).
    Without it you'll get AXError -25211 (kAXErrorAPIDisabled).
"""
from __future__ import annotations

import argparse
import signal
import sys
import re
from typing import Any

# ─────────────────────────── backend detection ────────────────────────────

BACKEND: str = "none"
_ax = None  # module alias used in helpers

try:
    from ApplicationServices import (  # type: ignore
        AXUIElementCreateApplication,
        AXUIElementCreateSystemWide,
        AXUIElementCopyAttributeNames,
        AXUIElementCopyAttributeValue,
        AXUIElementCopyActionNames,
        kAXErrorSuccess,
    )
    import AppKit  # type: ignore
    BACKEND = "pyobjc"
except ImportError:
    pass

if BACKEND == "none":
    try:
        import atomacos  # type: ignore
        BACKEND = "atomacos"
    except ImportError:
        pass

# ──────────────────────────── helpers ─────────────────────────────────────

_INDENT = "  "

AX_ATTRS_OF_INTEREST = [
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue",
    "AXHelp", "AXIdentifier", "AXLabel",
    "AXEnabled", "AXFocused", "AXSelected",
    "AXPosition", "AXSize",
    "AXChildren",
]

AX_SKIP_ROLES = {
    "AXStaticText",  # noisy for tree overview — flip off to see all text nodes
}


# ── subprocess / helper process names to always skip ──────────────────────
# These are child processes of real apps.  They expose no usable UI and
# their AX queries hang forever, causing the infinite-loop behaviour.
_SKIP_PROCESS_SUBSTRINGS = {
    "Web Content", "Networking", "Graphics and Media", "GPU Process",
    "Utility", "Renderer", "Helper", "Plugin",
    "QLPreviewGeneration", "QuickLookUIService", "ShareSheetUI",
    "ThemeWidgetControlView", "LinkedNotesUI",
    "LocalAuthenticationRemote",
}


def _is_helper_process(name: str) -> bool:
    """Return True if the app name looks like a non-GUI helper subprocess."""
    for sub in _SKIP_PROCESS_SUBSTRINGS:
        if sub in name:
            return True
    return False


def _section(title: str) -> None:
    print(f"\n{'=' * 64}")
    print(f"  {title}")
    print(f"{'=' * 64}")


# ── timeout guard for AX calls (macOS / Linux only) ──────────────────────

class _AXTimeout(Exception):
    pass

def _timeout_handler(signum, frame):
    raise _AXTimeout()

_AX_TIMEOUT_SEC = 2  # max seconds for any single AX attribute query


def _ax_get(element: Any, attr: str) -> tuple[bool, Any]:
    """Return (ok, value) for an AX attribute using pyobjc bindings.
    Times out after _AX_TIMEOUT_SEC to prevent hanging on zombie processes."""
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(_AX_TIMEOUT_SEC)
    try:
        err, value = AXUIElementCopyAttributeValue(element, attr, None)
        signal.alarm(0)  # cancel alarm
        return err == kAXErrorSuccess, value
    except _AXTimeout:
        return False, None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _ax_attrs(element: Any) -> list[str]:
    err, names = AXUIElementCopyAttributeNames(element, None)
    if err != kAXErrorSuccess or not names:
        return []
    return list(names)


def _ax_actions(element: Any) -> list[str]:
    err, names = AXUIElementCopyActionNames(element, None)
    if err != kAXErrorSuccess or not names:
        return []
    return list(names)


def _fmt_val(value: Any) -> str:
    """Compact single-line representation of an AX attribute value."""
    if value is None:
        return "None"
    s = str(value)
    s = s.replace("\n", "↵").replace("\r", "")
    return s[:120] if len(s) > 120 else s


def _extract_coords(val_str: str) -> tuple[float, float] | None:
    """Extracts the last two numbers from a string, useful for AXValue positions/sizes."""
    # Remove hex addresses like 0x60000 first
    clean_str = re.sub(r"0x[0-9a-fA-F]+", "", val_str)
    matches = re.findall(r"[-+]?\d*\.\d+|[-+]?\d+", clean_str)
    if len(matches) >= 2:
        return float(matches[-2]), float(matches[-1])
    return None

def _calc_centroid(pos: Any, size: Any) -> tuple[float, float] | None:
    if not pos or not size:
        return None
    pos_coords = _extract_coords(str(pos))
    size_coords = _extract_coords(str(size))
    if pos_coords and size_coords:
        x, y = pos_coords
        w, h = size_coords
        if w > 0 and h > 0:
            return (x + w / 2.0, y + h / 2.0)
    return None


# ──────────────────────── pyobjc tree walker ──────────────────────────────

def _walk_pyobjc(element: Any, depth: int, max_depth: int,
                 show_attrs: bool, show_actions: bool,
                 skip_roles: set[str],
                 flattened_list: list[dict] | None = None,
                 id_counter: list[int] | None = None) -> None:
    prefix = _INDENT * depth

    ok_role, role = _ax_get(element, "AXRole")
    role_str = str(role) if ok_role else "?"

    if role_str in skip_roles:
        return

    ok_sub, subrole = _ax_get(element, "AXSubrole")
    role_display = role_str
    if ok_sub and subrole:
        role_display += f"/{subrole}"

    ok_title,  title       = _ax_get(element, "AXTitle")
    ok_desc,   description = _ax_get(element, "AXDescription")
    ok_val,    value       = _ax_get(element, "AXValue")
    ok_en,     enabled     = _ax_get(element, "AXEnabled")
    ok_foc,    focused     = _ax_get(element, "AXFocused")
    ok_pos,    position    = _ax_get(element, "AXPosition")
    ok_sz,     size        = _ax_get(element, "AXSize")
    ok_id,     identifier  = _ax_get(element, "AXIdentifier")

    parts = []
    
    # Check if this element should be interactive / stamped with an ID
    is_interactive = False
    centroid = _calc_centroid(position, size) if ok_pos and ok_sz else None
    
    element_id = None
    if id_counter is not None and centroid is not None:
        # Filter out structural containers from getting IDs
        if role_str not in {"AXWindow", "AXApplication", "AXScrollArea", "AXGroup", "AXMenuBar", "AXMenu"}:
            is_interactive = True
            element_id = id_counter[0]
            id_counter[0] += 1
            parts.append(f"[{element_id}]")
            
            if flattened_list is not None:
                flattened_list.append({
                    "id": element_id,
                    "role": role_display,
                    "title": _fmt_val(title) if ok_title and title else "",
                    "description": _fmt_val(description) if ok_desc and description else "",
                    "centroid": centroid,
                    "enabled": enabled if ok_en else True
                })

    parts.append(f"[{role_display}]")
    if ok_title and title:
        parts.append(f'title="{_fmt_val(title)}"')
    if ok_desc and description:
        parts.append(f'desc="{_fmt_val(description)}"')
    if ok_val and value and role_str not in {"AXScrollArea", "AXGroup", "AXWindow", "AXApplication"}:
        parts.append(f'val="{_fmt_val(value)}"')
    if ok_id and identifier:
        parts.append(f'id="{_fmt_val(identifier)}"')
    if ok_en and enabled is not None:
        parts.append(f"enabled={enabled}")
    if ok_foc and focused:
        parts.append("FOCUSED")
    if ok_pos and position:
        pos_coords = _extract_coords(str(position))
        if pos_coords:
            parts.append(f"pos=({pos_coords[0]:.1f}, {pos_coords[1]:.1f})")
    if ok_sz and size:
        sz_coords = _extract_coords(str(size))
        if sz_coords:
            parts.append(f"size=({sz_coords[0]:.1f}, {sz_coords[1]:.1f})")
    if centroid:
        parts.append(f"centroid=({centroid[0]:.1f}, {centroid[1]:.1f})")

    if flattened_list is None or is_interactive:
        print(prefix + " ".join(parts))

    if show_attrs and depth == 0:
        all_attrs = _ax_attrs(element)
        print(prefix + f"  attrs      : {all_attrs}")
        all_actions = _ax_actions(element)
        if all_actions:
            print(prefix + f"  actions    : {all_actions}")

    if depth >= max_depth:
        _, children = _ax_get(element, "AXChildren")
        count = len(children) if children else 0
        if count:
            print(prefix + _INDENT + f"... ({count} children, max depth reached)")
        return

    ok_ch, children = _ax_get(element, "AXChildren")
    if ok_ch and children:
        for child in children:
            _walk_pyobjc(child, depth + 1, max_depth,
                         show_attrs, show_actions, skip_roles,
                         flattened_list, id_counter)


# ─────────────────────── atomacos tree walker ────────────────────────────

def _walk_atomacos(element: Any, depth: int, max_depth: int,
                   skip_roles: set[str],
                   flattened_list: list[dict] | None = None,
                   id_counter: list[int] | None = None) -> None:
    prefix = _INDENT * depth

    role = getattr(element, "AXRole", "?") or "?"
    if role in skip_roles:
        return

    subrole    = getattr(element, "AXSubrole", None)
    title      = getattr(element, "AXTitle", None)
    description = getattr(element, "AXDescription", None)
    value      = getattr(element, "AXValue", None)
    enabled    = getattr(element, "AXEnabled", None)
    focused    = getattr(element, "AXFocused", None)
    position   = getattr(element, "AXPosition", None)
    size       = getattr(element, "AXSize", None)

    role_display = role
    if subrole:
        role_display += f"/{subrole}"

    parts = []
    
    is_interactive = False
    centroid = _calc_centroid(position, size) if position and size else None
    
    element_id = None
    if id_counter is not None and centroid is not None:
        if role not in {"AXWindow", "AXApplication", "AXScrollArea", "AXGroup", "AXMenuBar", "AXMenu"}:
            is_interactive = True
            element_id = id_counter[0]
            id_counter[0] += 1
            parts.append(f"[{element_id}]")
            
            if flattened_list is not None:
                flattened_list.append({
                    "id": element_id,
                    "role": role_display,
                    "title": _fmt_val(title) if title else "",
                    "description": _fmt_val(description) if description else "",
                    "centroid": centroid,
                    "enabled": enabled if enabled is not None else True
                })

    parts.append(f"[{role_display}]")
    if title:
        parts.append(f'title="{_fmt_val(title)}"')
    if description:
        parts.append(f'desc="{_fmt_val(description)}"')
    if value and role not in {"AXScrollArea", "AXGroup", "AXWindow", "AXApplication"}:
        parts.append(f'val="{_fmt_val(str(value))}"')
    if enabled is not None:
        parts.append(f"enabled={enabled}")
    if focused:
        parts.append("FOCUSED")
    if position:
        pos_coords = _extract_coords(str(position))
        if pos_coords:
            parts.append(f"pos=({pos_coords[0]:.1f}, {pos_coords[1]:.1f})")
    if size:
        sz_coords = _extract_coords(str(size))
        if sz_coords:
            parts.append(f"size=({sz_coords[0]:.1f}, {sz_coords[1]:.1f})")
    if centroid:
        parts.append(f"centroid=({centroid[0]:.1f}, {centroid[1]:.1f})")

    if flattened_list is None or is_interactive:
        print(prefix + " ".join(parts))

    if depth >= max_depth:
        try:
            children = element.AXChildren or []
        except Exception:
            children = []
        if children:
            print(prefix + _INDENT + f"... ({len(children)} children, max depth reached)")
        return

    try:
        children = element.AXChildren or []
    except Exception:
        children = []

    for child in children:
        _walk_atomacos(child, depth + 1, max_depth, skip_roles, flattened_list, id_counter)


# ──────────────────────── running app listing ─────────────────────────────

def get_running_apps_pyobjc(include_helpers: bool = False) -> list[tuple[str, int]]:
    """Returns list of (app_name, pid) for running GUI apps.
    Filters out helper subprocesses unless include_helpers is True."""
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    apps = workspace.runningApplications()
    result = []
    for app in apps:
        name = app.localizedName() or ""
        pid  = app.processIdentifier()
        if not name:
            continue
        if not include_helpers and _is_helper_process(name):
            continue
        result.append((name, pid))
    return sorted(result, key=lambda x: x[0].lower())


def get_running_apps_atomacos(include_helpers: bool = False) -> list[tuple[str, int]]:
    import AppKit  # type: ignore
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    apps = workspace.runningApplications()
    result = []
    for app in apps:
        name = app.localizedName() or ""
        pid  = app.processIdentifier()
        if not name:
            continue
        if not include_helpers and _is_helper_process(name):
            continue
        result.append((name, pid))
    return sorted(result, key=lambda x: x[0].lower())


# ──────────────────────── per-app dump ────────────────────────────────────

def dump_app_pyobjc(name: str, pid: int, max_depth: int,
                    show_attrs: bool, show_actions: bool,
                    skip_roles: set[str], do_flatten: bool) -> None:
    _section(f"{name}  (pid={pid})")
    app_element = AXUIElementCreateApplication(pid)
    
    flat_list: list[dict] = [] if do_flatten else None
    id_counter = [1] if do_flatten else None
    
    _walk_pyobjc(app_element, depth=0, max_depth=max_depth,
                 show_attrs=show_attrs, show_actions=show_actions,
                 skip_roles=skip_roles, flattened_list=flat_list, id_counter=id_counter)
                 
    if do_flatten and flat_list is not None:
        _section(f"LLM Prompt Format ({len(flat_list)} interactable elements)")
        for item in flat_list:
            s_parts = [f"[{item['id']}] {item['role']}"]
            if item['title']:
                s_parts.append(f'"{item["title"]}"')
            if item['description'] and item['description'] != item['title']:
                s_parts.append(f'desc="{item["description"]}"')
            if not item.get('enabled', True):
                s_parts.append("(disabled)")
            print(" ".join(s_parts))


def dump_app_atomacos(name: str, pid: int, max_depth: int,
                      skip_roles: set[str], do_flatten: bool) -> None:
    _section(f"{name}  (pid={pid})")
    try:
        app = atomacos.getAppRefByPid(pid)
        flat_list: list[dict] = [] if do_flatten else None
        id_counter = [1] if do_flatten else None
        _walk_atomacos(app, depth=0, max_depth=max_depth, skip_roles=skip_roles,
                       flattened_list=flat_list, id_counter=id_counter)
                       
        if do_flatten and flat_list is not None:
            _section(f"LLM Prompt Format ({len(flat_list)} interactable elements)")
            for item in flat_list:
                s_parts = [f"[{item['id']}] {item['role']}"]
                if item['title']:
                    s_parts.append(f'"{item["title"]}"')
                if item['description'] and item['description'] != item['title']:
                    s_parts.append(f'desc="{item["description"]}"')
                if not item.get('enabled', True):
                    s_parts.append("(disabled)")
                print(" ".join(s_parts))
    except Exception as e:
        print(f"  [ERROR] {e}")


# ──────────────────────────── main ────────────────────────────────────────

DEFAULT_APPS = ["Finder", "Terminal", "Safari", "Google Chrome", "Notes"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump the macOS Accessibility tree for running apps."
    )
    parser.add_argument("--app", metavar="NAME",
                        help="App name to inspect (partial match OK). "
                             "Default: tries a few common apps.")
    parser.add_argument("--depth", type=int, default=3,
                        help="Max recursion depth (default: 3)")
    parser.add_argument("--list-apps", action="store_true",
                        help="List all running apps and exit")
    parser.add_argument("--show-attrs", action="store_true",
                        help="Show full attribute list on root element")
    parser.add_argument("--show-actions", action="store_true",
                        help="Show available actions on root element")
    parser.add_argument("--include-text", action="store_true",
                        help="Include AXStaticText nodes (verbose)")
    parser.add_argument("--flatten", action="store_true",
                        help="Simulate the Phase 1 tool: assign sequential IDs and calculate centroids for interactable elements.")
    args = parser.parse_args()

    skip_roles = set() if args.include_text else AX_SKIP_ROLES

    # ── backend check ─────────────────────────────────────────────────────
    if BACKEND == "none":
        print("[ERROR] No accessibility backend found.")
        print("Install one of:")
        print("  pip install pyobjc-framework-ApplicationServices pyobjc-framework-AppKit")
        print("  pip install atomacos")
        sys.exit(1)

    print(f"Backend : {BACKEND}")
    print(f"Platform: {sys.platform}")

    # ── list apps ─────────────────────────────────────────────────────────
    if BACKEND == "pyobjc":
        running = get_running_apps_pyobjc()
    else:
        running = get_running_apps_atomacos()

    if args.list_apps:
        _section("Running GUI applications")
        for name, pid in running:
            print(f"  {pid:7d}  {name}")
        return

    # ── select target apps ────────────────────────────────────────────────
    if args.app:
        query = args.app.lower()
        targets = [(n, p) for n, p in running if query in n.lower()]
        if not targets:
            print(f"[ERROR] No running app matching '{args.app}'")
            print("Use --list-apps to see what's running.")
            sys.exit(1)
    else:
        targets = [(n, p) for n, p in running
                   if any(d.lower() in n.lower() for d in DEFAULT_APPS)]
        if not targets:
            # Fall back to first 3 running apps
            targets = running[:3]

    # ── dump trees ────────────────────────────────────────────────────────
    print(f"\nMax depth : {args.depth}")
    print(f"Skip roles: {skip_roles or '(none)'}")
    print(f"Apps to inspect: {[n for n, _ in targets]}")

    for name, pid in targets:
        try:
            if BACKEND == "pyobjc":
                dump_app_pyobjc(name, pid, args.depth,
                                args.show_attrs, args.show_actions,
                                skip_roles, args.flatten)
            else:
                dump_app_atomacos(name, pid, args.depth, skip_roles, args.flatten)
        except Exception as e:
            _section(f"{name}  (pid={pid})  — FAILED")
            print(f"  {e}")
            print()
            print("  Likely cause: Accessibility permission not granted.")
            print("  → System Settings → Privacy & Security → Accessibility")
            print("    → add Terminal (or whatever runs this script)")

    _section("Done")


if __name__ == "__main__":
    main()
