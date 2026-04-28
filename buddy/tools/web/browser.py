from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import random
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from buddy.logger.logger import get_logger
from buddy.prompts.browser_prompts import BROWSER_TOOL_PROMPT

logger = get_logger("browser")

TOOL_NAME = "browser"

# ── Constants ─────────────────────────────────────────────────────────────────
_MAX_ACTIONS = 20
_MAX_RETRIES = 3
_SCREENSHOT_QUALITY = 80  # JPEG — STB-compatible; WebP NOT supported by llama.cpp
_NAV_TIMEOUT = 30_000  # ms
_ELEMENT_TIMEOUT = 5_000  # ms
_PAGE_SETTLE = 1.0  # s — wait after navigation before acting
_ACTION_DELAY = (0.2, 0.5)
_TYPE_DELAY = (0.05, 0.15)
_FOCUS_DELAY = 0.15  # s — wait after click-to-focus before typing
_SCROLL_STEP_PX = 200
_SCROLL_STEP_DELAY = (0.1, 0.3)
_MEMORY_TOP_K = 3
_NAV_DETECT_TIMEOUT = 5_000  # ms — wait for post-click navigation
_LOOP_HISTORY_SIZE = 3
_DEFAULT_SCROLL_PX = 300
_HUMAN_TYPE_PAUSE_INTERVAL = 5
_CLICK_MOVE_DELAY = (0.10, 0.20)

# Actions that are exempt from infinite-loop detection
_LOOP_EXEMPT: frozenset = frozenset(
    {"wait", "scroll", "fetch_memory", "ask_user", "done", "error"}
)

# Compiled once — detects CSS metacharacters in a selector target
_CSS_META = re.compile(r"[\[\]#.>~+:()\\/]")

# Selector regex patterns — compiled once to avoid per-call overhead
_RE_HAS_TEXT_OR_CONTAINS = re.compile(r":(?:has-text|contains)\(['\"]([^'\"]+)['\"]\)")
_RE_ATTR_SELECTOR = re.compile(
    r'\[(?:name|placeholder|aria-label|title|id)=["\']([^"\']+)["\']\]'
)
_RE_CONTAINS_SUB = re.compile(r":contains\((['\"])([^'\"]*)\1\)")
_RE_TEXT_ATTR_EXTRACT = re.compile(r"\s+text=(['\"])([^'\"]+)\1")

_SESSION_DIR = Path.home() / ".buddy" / "browser_sessions"

# playwright-stealth is optional — import once at module level
try:
    from playwright_stealth import Stealth as _Stealth  # type: ignore

    _STEALTH: Any = _Stealth()
except ImportError:
    _STEALTH = None

# JS snippets for mouse-fallback coord search — kept at module level to avoid
# re-allocating the string on every failed click/fill.
_JS_CLICK_SEARCH = """(text) => {
    const tags = [
        'a', 'button', 'input[type="submit"]', 'input[type="button"]',
        '[role="button"]', '[role="link"]', '[role="menuitem"]',
        '[role="tab"]', 'span', 'div', 'li', 'td', 'th', 'label'
    ];
    const lc = text.toLowerCase();
    for (const sel of tags) {
        for (const el of document.querySelectorAll(sel)) {
            const t = (el.innerText || el.value ||
                       el.getAttribute('aria-label') ||
                       el.getAttribute('title') || '').trim().toLowerCase();
            if (!t.includes(lc)) continue;
            el.scrollIntoView({behavior: 'instant', block: 'center'});
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
        }
    }
    return null;
}"""

_JS_FILL_SEARCH = """(text) => {
    const lc = text.toLowerCase();
    const inputs = document.querySelectorAll(
        'input:not([type="hidden"]):not([type="submit"]):not([type="button"]),' +
        'textarea,[contenteditable="true"],[role="textbox"],[role="combobox"]'
    );
    for (const el of inputs) {
        const hint = (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                      el.getAttribute('name') || el.getAttribute('id') ||
                      el.getAttribute('title') || '').toLowerCase();
        if (!hint.includes(lc)) continue;
        el.scrollIntoView({behavior: 'instant', block: 'center'});
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0)
            return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
    }
    return null;
}"""


# ==========================================================
# Utilities
# ==========================================================


def _playwright_not_installed() -> Dict[str, Any]:
    return {
        "OK": False,
        "ERROR": (
            "playwright not installed — run: pip install playwright && playwright"
            " install chromium"
        ),
    }


async def _safe_close(obj: Any) -> None:
    if obj is None:
        return
    try:
        await (obj.close if hasattr(obj, "close") else obj.stop)()
    except Exception:
        pass


# ==========================================================
# Tool
# ==========================================================


class BrowserTool:

    def __init__(self) -> None:
        # Persistent across execute() calls so the browser window survives retries.
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._headless: Optional[bool] = None

    def _browser_alive(self) -> bool:
        try:
            return self._browser is not None and self._browser.is_connected()
        except Exception:
            return False

    async def _close_ctx(self) -> None:
        await _safe_close(self._ctx)
        self._ctx = None

    async def _close_browser(self) -> None:
        await self._close_ctx()
        await _safe_close(self._browser)
        self._browser = None
        await _safe_close(self._pw)
        self._pw = None
        self._headless = None

    def get_info(self) -> Dict[str, Any]:
        return {
            "name": TOOL_NAME,
            "version": "1.2.0",
            "description": (
                " Use this tool to complete web tasks autonomously. It runs a"
                " self-contained ReAct loop that handles navigation, clicking, form"
                " filling, login, and all other browser interactions internally — do"
                " NOT break a single web task into multiple steps. Always pass the full"
                " end-to-end goal with all necessary instructions in single browser"
                " step. The only exception: if the request contains multiple clearly"
                " separate web goals, you may make one execution step per distinct"
                " goal."
            ),
            "prompt": BROWSER_TOOL_PROMPT,
        }

    async def execute(
        self,
        function: str = "",
        arguments: Dict[str, Any] = {},
        brain: Any = None,
        memory_manager: Any = None,
        on_progress: Optional[Callable[[str, bool], None]] = None,
        ui_output: Optional[Callable] = None,
        ui_input: Optional[Callable] = None,
        **_kwargs: Any,
    ) -> Dict[str, Any]:
        fn = (function or "").strip().lower()
        if "/" in fn:
            fn = fn.split("/")[-1]

        if fn == "run_task":
            return await self._run_task(
                arguments, brain, memory_manager, on_progress, ui_output, ui_input
            )
        if fn == "fill_form":
            return await self._fill_form(arguments, on_progress)
        if fn == "screenshot_query":
            return await self._screenshot_query(arguments, brain, on_progress)
        if fn == "check_page":
            return await self._check_page(arguments, on_progress)
        if fn == "manage_session":
            return self._manage_session(arguments)

        return {
            "OK": False,
            "ERROR": (
                f"Unknown function: {fn!r}. Must be run_task, fill_form,"
                " screenshot_query, check_page, or manage_session."
            ),
        }

    # ----------------------------------------------------------
    # run_task — full autonomous loop
    # ----------------------------------------------------------

    async def _run_task(
        self,
        args: Dict[str, Any],
        brain: Any,
        memory_manager: Any,
        on_progress: Optional[Callable],
        ui_output: Optional[Callable] = None,
        ui_input: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        url = _normalize_url(str(args.get("url") or "").strip())
        task = str(args.get("task") or "").strip()
        headless = bool(args.get("headless", False))

        if not task:
            return {"OK": False, "ERROR": "task is required"}
        if brain is None:
            return {"OK": False, "ERROR": "brain is required for run_task"}

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return _playwright_not_installed()

        if on_progress:
            on_progress("Starting browser…", False)

        # Reuse the live window when headless mode matches; fresh context each time
        # so cookies/storage are clean on every attempt.
        if self._browser_alive() and self._headless == headless:
            await self._close_ctx()
            logger.info("browser: reusing live browser (headless=%s)", headless)
        else:
            await self._close_browser()
            self._pw = await async_playwright().start()
            self._browser = await _launch_browser(self._pw, headless)
            self._headless = headless
            logger.info("browser: launched new browser (headless=%s)", headless)

        self._ctx = await _make_context(self._browser, url)
        page = await _new_page(self._ctx)

        result: Dict[str, Any] = {"OK": False, "ERROR": "unexpected exit"}
        try:
            result = await self._run_loop(
                page=page,
                ctx=self._ctx,
                url=url,
                task=task,
                brain=brain,
                memory_manager=memory_manager,
                on_progress=on_progress,
                ui_output=ui_output,
                ui_input=ui_input,
            )
        except Exception as exc:
            result = {"OK": False, "ERROR": str(exc)}

        keep_open = result.pop("_keep_open", False)
        run_ok = result.get("OK", False)

        _ie = getattr(brain, "_interrupt_event", None) if brain else None
        interrupted = _ie is not None and _ie.is_set()

        # Close strategy:
        #   interrupted                 → always close (user cancelled — no retry)
        #   success + user chose close  → close
        #   failure + headless=True     → close  (background; no visible window)
        #   failure + headless=False    → KEEP OPEN for retry reuse
        #   user explicitly said "keep" → keep
        if interrupted:
            await self._close_browser()
        elif keep_open:
            pass
        elif not run_ok and not headless:
            logger.info("browser: keeping window open for retry (headless=False)")
        else:
            await self._close_browser()

        return result

    async def _run_loop(
        self,
        *,
        page: Any,
        ctx: Any,
        url: str,
        task: str,
        brain: Any,
        memory_manager: Any,
        on_progress: Optional[Callable],
        ui_output: Optional[Callable] = None,
        ui_input: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        if url:
            nav_err = await _navigate(page, url)
            if nav_err:
                return {"OK": False, "ERROR": nav_err}
            await _wait_for_interactive(page)

        _register_dialog_handler(page)

        domain = _domain(url) if url else ""
        progress_summary = ""
        memory_ctx: Dict[str, str] = {}
        memory_str = ""  # recomputed only when memory_ctx changes
        ask_history: collections.deque = collections.deque(maxlen=20)
        consecutive_errors = 0
        last_error = ""
        action_history: collections.deque = collections.deque(maxlen=_LOOP_HISTORY_SIZE)

        for step in range(1, _MAX_ACTIONS + 1):
            _ie = getattr(brain, "_interrupt_event", None)
            if _ie is not None and _ie.is_set():
                logger.info("browser: interrupted at step=%d — exiting loop", step)
                return {
                    "OK": False,
                    "ACTION": "run_task",
                    "ERROR": "Interrupted",
                    "STEPS": step,
                }

            if on_progress:
                on_progress(f"Step {step}/{_MAX_ACTIONS}…", False)

            page = await _get_active_page(ctx, page)

            # Gather page state concurrently — screenshot, DOM, and title are independent
            screenshot_uri, dom_hints, page_title = await asyncio.gather(
                _screenshot(page),
                _extract_interactive_elements(page),
                page.title(),
            )
            page_url = page.url
            dom_hints = f"[current_url={page_url}] [page_title={page_title!r}]" + (
                "\n" + dom_hints if dom_hints else ""
            )

            try:
                act = await asyncio.to_thread(
                    brain.run_browser_action,
                    screenshot_uri=screenshot_uri,
                    task=task,
                    progress=progress_summary,
                    memory_context=memory_str,
                    dom_hints=dom_hints,
                    ask_history=list(ask_history),
                    last_error=last_error,
                )
            except Exception as exc:
                consecutive_errors += 1
                last_error = f"Brain call failed: {exc}"
                logger.warning(
                    "browser step=%d brain error: %r consecutive=%d",
                    step,
                    exc,
                    consecutive_errors,
                )
                if consecutive_errors >= _MAX_RETRIES:
                    return {
                        "OK": False,
                        "ACTION": "run_task",
                        "ERROR": (
                            f"Brain failed {_MAX_RETRIES}× consecutively. Last: {exc}"
                        ),
                        "STEPS": step,
                    }
                await asyncio.sleep(random.uniform(1.0, 2.0))
                continue

            fn = act.get("function", "error")
            arguments = act.get("arguments", {})
            progress_summary = act.get("summary") or progress_summary
            last_error = ""

            logger.info(
                "browser step=%d function=%s summary=%r",
                step,
                fn,
                progress_summary[:80],
            )

            fields = arguments.get("fields")
            loop_key = str(
                fields[0].get("selector", "")
                if isinstance(fields, list) and fields
                else arguments.get("selector", arguments.get("url", ""))
            )
            action_history.append((fn, loop_key))
            if (
                len(action_history) == _LOOP_HISTORY_SIZE
                and len(set(action_history)) == 1
                and fn not in _LOOP_EXEMPT
            ):
                logger.warning(
                    "browser: infinite loop detected at step=%d fn=%s", step, fn
                )
                return {
                    "OK": False,
                    "ACTION": "run_task",
                    "ERROR": f"Stuck in a loop repeating {fn!r} — cannot proceed.",
                    "STEPS": step,
                }

            if fn == "done":
                save_domain = domain or _domain(page.url)
                if save_domain:
                    _save_session(ctx, save_domain)
                answer = await _ask_user(
                    "Done! Want me to close the browser, or keep it open for now?",
                    ui_output=ui_output,
                    ui_input=ui_input,
                )
                keep_open = not _parse_close_intent(answer)
                return {
                    "OK": True,
                    "ACTION": "run_task",
                    "TASK": task,
                    "STEPS": step,
                    "SUMMARY": progress_summary,
                    "_keep_open": keep_open,
                }

            if fn == "error":
                reason = str(arguments.get("reason", "")).strip()
                error_msg = reason or progress_summary or "micro-planner reported error"
                logger.warning(
                    "browser micro-planner error at step=%d: %r act=%r",
                    step,
                    error_msg,
                    act,
                )
                return {
                    "OK": False,
                    "ACTION": "run_task",
                    "ERROR": error_msg,
                    "STEPS": step,
                }

            if fn == "fetch_memory":
                query = str(arguments.get("query", "")).strip()
                if query not in memory_ctx:
                    found = _search_memory(memory_manager, query)
                    if found:
                        memory_ctx[query] = found
                        memory_str = "; ".join(
                            f"{k}={v}" for k, v in memory_ctx.items()
                        )
                        logger.info("browser memory hit %r → %r", query, found[:40])
                    else:
                        q_text = (
                            f"What is your {query}?"
                            if query
                            else "Can you provide this value?"
                        )
                        answer = await _ask_user(
                            q_text, ui_output=ui_output, ui_input=ui_input
                        )
                        if answer:
                            memory_ctx[query] = answer
                            memory_str = "; ".join(
                                f"{k}={v}" for k, v in memory_ctx.items()
                            )
                            _store_memory(memory_manager, query, answer)
                            ask_history.append({"q": q_text, "a": answer})
                await asyncio.sleep(random.uniform(*_ACTION_DELAY))
                continue

            if fn == "ask_user":
                question = (
                    str(arguments.get("question", "")).strip() or progress_summary
                )
                answer = await _ask_user(
                    question, ui_output=ui_output, ui_input=ui_input
                )
                if answer:
                    ask_history.append({"q": question, "a": answer})
                await asyncio.sleep(random.uniform(*_ACTION_DELAY))
                continue

            url_before = page.url
            err = await _execute_action(page, fn, arguments)

            if err:
                consecutive_errors += 1
                last_error = f"{fn} failed: {err}"
                logger.warning(
                    "browser step=%d function=%s err=%r consecutive=%d",
                    step,
                    fn,
                    err,
                    consecutive_errors,
                )
                if consecutive_errors >= _MAX_RETRIES:
                    return {
                        "OK": False,
                        "ACTION": "run_task",
                        "ERROR": f"Failed {_MAX_RETRIES}× consecutively. Last: {err}",
                        "STEPS": step,
                    }
                await asyncio.sleep(random.uniform(*_ACTION_DELAY))
                continue
            else:
                consecutive_errors = 0

            if fn in ("click", "navigate"):
                switched = await _get_active_page(ctx, page)
                if switched is not page:
                    page = switched
                    domain = _domain(page.url)
                    logger.info("browser: new tab after %s → %s", fn, page.url)
                else:
                    navigated = await _wait_for_navigation_if_needed(page, url_before)
                    if navigated:
                        new_domain = _domain(page.url)
                        if new_domain and new_domain != domain:
                            domain = new_domain
                        logger.debug(
                            "browser: navigation detected → new url=%s", page.url
                        )
            else:
                await asyncio.sleep(random.uniform(*_ACTION_DELAY))

        return {
            "OK": False,
            "ACTION": "run_task",
            "ERROR": f"Reached max {_MAX_ACTIONS} actions without completing task",
            "STEPS": _MAX_ACTIONS,
        }

    # ----------------------------------------------------------
    # fill_form — targeted HTML-first fill (no vision loop)
    # ----------------------------------------------------------

    async def _fill_form(
        self,
        args: Dict[str, Any],
        on_progress: Optional[Callable],
    ) -> Dict[str, Any]:
        url = _normalize_url(str(args.get("url") or "").strip())
        fields: Dict[str, str] = args.get("fields") or {}
        submit = bool(args.get("submit", True))

        if not url:
            return {"OK": False, "ERROR": "url is required"}
        if not fields:
            return {"OK": False, "ERROR": "fields dict is required"}

        if on_progress:
            on_progress("Filling form…", False)

        filled: List[str] = []
        failed: List[str] = []

        try:
            async with _headless_page(url) as page:
                nav_err = await _navigate(page, url)
                if nav_err:
                    return {"OK": False, "ERROR": nav_err}

                for label, value in fields.items():
                    err = await _do_fill(page, label, str(value))
                    (failed if err else filled).append(
                        f"{label}: {err}" if err else label
                    )

                if submit and filled:
                    s_err = await _submit_form(page)
                    if s_err:
                        failed.append(f"submit: {s_err}")
        except ImportError:
            return _playwright_not_installed()

        return {
            "OK": len(failed) == 0,
            "ACTION": "fill_form",
            "URL": url,
            "FILLED": filled,
            "FAILED": failed or None,
        }

    # ----------------------------------------------------------
    # screenshot_query — visual inspection only
    # ----------------------------------------------------------

    async def _screenshot_query(
        self,
        args: Dict[str, Any],
        brain: Any,
        on_progress: Optional[Callable],
    ) -> Dict[str, Any]:
        url = _normalize_url(str(args.get("url") or "").strip())
        query = str(args.get("query") or "").strip()

        if not url:
            return {"OK": False, "ERROR": "url is required"}
        if not query:
            return {"OK": False, "ERROR": "query is required"}
        if brain is None:
            return {"OK": False, "ERROR": "brain is required"}

        if on_progress:
            on_progress("Navigating…", False)

        try:
            async with _headless_page(url) as page:
                nav_err = await _navigate(page, url)
                if nav_err:
                    return {"OK": False, "ERROR": nav_err}
                if on_progress:
                    on_progress("Analysing…", False)
                screenshot_uri = await _screenshot(page)
        except ImportError:
            return _playwright_not_installed()

        result = await asyncio.to_thread(
            brain.run_vision, image_paths=[screenshot_uri], query=query
        )

        if "error" in result:
            return {
                "OK": False,
                "ACTION": "screenshot_query",
                "URL": url,
                "ERROR": result["error"],
            }

        return {
            "OK": True,
            "ACTION": "screenshot_query",
            "URL": url,
            "DESCRIPTION": result.get("description", ""),
            "KEY_FINDING": result.get("key_finding", ""),
            "TEXT_FOUND": result.get("text_found", ""),
        }

    # ----------------------------------------------------------
    # manage_session
    # ----------------------------------------------------------

    def _manage_session(self, args: Dict[str, Any]) -> Dict[str, Any]:
        action = str(args.get("action") or "").strip().lower()
        domain = str(args.get("domain") or "").strip()

        if action == "list":
            _SESSION_DIR.mkdir(parents=True, exist_ok=True)
            sessions = [f.stem for f in _SESSION_DIR.glob("*.json")]
            return {"OK": True, "ACTION": "list", "SESSIONS": sessions}

        if not action:
            return {"OK": False, "ERROR": "action required (list/load/clear)"}
        if not domain:
            return {"OK": False, "ERROR": "domain required"}

        sf = _session_path(domain)

        if action == "clear":
            sf.unlink(missing_ok=True)
            return {"OK": True, "ACTION": "clear", "DOMAIN": domain}

        if action == "load":
            return {
                "OK": True,
                "ACTION": "load",
                "DOMAIN": domain,
                "EXISTS": sf.exists(),
            }

        return {
            "OK": False,
            "ERROR": f"Unknown session action: {action!r}. Use list/load/clear",
        }

    # ----------------------------------------------------------
    # check_page — metadata only, no interaction
    # ----------------------------------------------------------

    async def _check_page(
        self,
        args: Dict[str, Any],
        on_progress: Optional[Callable],
    ) -> Dict[str, Any]:
        url = _normalize_url(str(args.get("url") or "").strip())
        if not url:
            return {"OK": False, "ERROR": "url is required"}

        if on_progress:
            on_progress("Checking page…", False)

        try:
            async with _headless_page(url) as page:
                nav_err = await _navigate(page, url)
                if nav_err:
                    return {"OK": False, "ERROR": nav_err}

                title = await page.title()
                final_url = page.url
                inputs = await page.locator(
                    "input:not([type='hidden']):not([type='submit']):not([type='button'])"
                ).count()
                selects = await page.locator("select").count()
                textareas = await page.locator("textarea").count()
                buttons = await page.locator(
                    "button, input[type='submit'], input[type='button']"
                ).count()
                has_captcha = bool(
                    await page.locator(
                        "iframe[src*='recaptcha'], iframe[src*='hcaptcha'],"
                        " .g-recaptcha, .h-captcha"
                    ).count()
                )
        except ImportError:
            return _playwright_not_installed()

        return {
            "OK": True,
            "ACTION": "check_page",
            "URL": final_url,
            "TITLE": title,
            "FORM_FIELDS": inputs + selects + textareas,
            "BUTTONS": buttons,
            "HAS_CAPTCHA": has_captcha,
        }


# ==========================================================
# Headless browser context manager — shared by fill_form /
# screenshot_query / check_page
# ==========================================================


@asynccontextmanager
async def _headless_page(url: str):
    from playwright.async_api import async_playwright  # raises ImportError if missing

    pw = await async_playwright().start()
    browser = await _launch_browser(pw, headless=False)
    ctx = await _make_context(browser, url)
    page = await _new_page(ctx)
    _register_dialog_handler(page)
    try:
        yield page
    finally:
        await _safe_close(browser)
        await _safe_close(pw)


# ==========================================================
# Dialog handler — alert / confirm / prompt
# ==========================================================


def _register_dialog_handler(page: Any) -> None:
    async def _on_dialog(dialog: Any) -> None:
        try:
            dtype = dialog.type
            msg = dialog.message[:80] if dialog.message else ""
            logger.info(
                "browser: dialog type=%s message=%r — auto-accepting", dtype, msg
            )
            await dialog.accept("" if dtype == "prompt" else None)
        except Exception as exc:
            logger.warning("browser: dialog handler error: %r", exc)

    page.on("dialog", _on_dialog)


# ==========================================================
# New-tab detection
# ==========================================================


async def _get_active_page(ctx: Any, current_page: Any) -> Any:
    """
    Return the newest open tab if one appeared, else current_page.
    Called at the top of every loop iteration and after click/navigate.
    """
    try:
        pages = [p for p in ctx.pages if not p.is_closed()]
        if pages and pages[-1] is not current_page:
            new_page = pages[-1]
            await new_page.wait_for_load_state("domcontentloaded", timeout=8_000)
            await _wait_for_interactive(new_page)
            _register_dialog_handler(new_page)
            logger.info("browser: switched to new tab url=%s", new_page.url)
            return new_page
    except Exception as exc:
        logger.debug("browser: _get_active_page error: %r", exc)
    return current_page


# ==========================================================
# Post-click navigation detection
# ==========================================================


async def _wait_for_navigation_if_needed(page: Any, url_before: str) -> bool:
    """
    After a click, wait briefly to see if navigation fired.
    Returns True if the URL changed.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=_NAV_DETECT_TIMEOUT)
        if page.url != url_before:
            await asyncio.sleep(_PAGE_SETTLE)
            return True
        await asyncio.sleep(random.uniform(*_ACTION_DELAY))
        return False
    except Exception:
        await asyncio.sleep(random.uniform(*_ACTION_DELAY))
        return False


# ==========================================================
# Page action dispatcher
# ==========================================================


async def _execute_action(
    page: Any, function: str, arguments: Dict[str, Any]
) -> Optional[str]:
    """Dispatch navigate/fill/click/scroll/wait. Returns error string or None."""
    try:
        if function == "navigate":
            target_url = _normalize_url(str(arguments.get("url", "")).strip())
            if not target_url:
                return "navigate: url is required"
            err = await _navigate(page, target_url)
            if err:
                return err
            await _wait_for_interactive(page)
            return None
        if function == "fill":
            fields = arguments.get("fields")
            if isinstance(fields, list):
                for f in fields:
                    err = await _do_fill(
                        page, str(f.get("selector", "")), str(f.get("value", ""))
                    )
                    if err:
                        return err
                return None
            return await _do_fill(
                page,
                str(arguments.get("selector", "")),
                str(arguments.get("value", "")),
            )
        if function == "click":
            return await _do_click(page, str(arguments.get("selector", "")))
        if function == "scroll":
            raw_px = arguments.get("px", _DEFAULT_SCROLL_PX)
            px = (
                int(raw_px) if str(raw_px).lstrip("-").isdigit() else _DEFAULT_SCROLL_PX
            )
            return await _do_scroll(page, px)
        if function == "wait":
            selector = str(arguments.get("selector", ""))
            raw_ms = arguments.get("timeout_ms", _ELEMENT_TIMEOUT)
            ms = int(raw_ms) if str(raw_ms).isdigit() else _ELEMENT_TIMEOUT
            return await _do_wait(page, selector, ms)
        return f"Unknown function: {function!r}"
    except Exception as exc:
        return str(exc)


# ==========================================================
# Action implementations
# ==========================================================


async def _do_fill(page: Any, target: str, value: str) -> Optional[str]:
    if not target:
        return "fill: target is required"

    coords = _parse_coords(target)
    if coords:
        try:
            await _human_click(page, *coords)
            await _human_type(page, value)
            return None
        except Exception as exc:
            return str(exc)

    for loc in _locator_chain(page, target):
        try:
            el = loc.first
            await el.wait_for(state="visible", timeout=_ELEMENT_TIMEOUT)
            await el.scroll_into_view_if_needed(timeout=2_000)
            await el.click()
            await _human_type(page, value)
            return None
        except Exception:
            continue

    return await _fill_by_coord_search(page, target, value)


async def _do_click(page: Any, target: str) -> Optional[str]:
    if not target:
        return "click: target is required"

    coords = _parse_coords(target)
    if coords:
        try:
            await _human_click(page, *coords)
            return None
        except Exception as exc:
            return str(exc)

    for i, loc in enumerate(_locator_chain(page, target)):
        try:
            el = loc.first
            await el.wait_for(state="visible", timeout=_ELEMENT_TIMEOUT)
            await el.scroll_into_view_if_needed(timeout=2_000)
            bbox = await el.bounding_box()
            if bbox:
                await _human_click(
                    page, bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2
                )
            else:
                await el.click()
            return None
        except Exception as exc:
            logger.debug("click locator[%d] failed target=%r: %s", i, target, exc)
            continue

    return await _click_by_coord_search(page, target)


async def _do_scroll(page: Any, px: int) -> Optional[str]:
    try:
        step_dir = _SCROLL_STEP_PX if px > 0 else -_SCROLL_STEP_PX
        for _ in range(max(1, abs(px) // _SCROLL_STEP_PX)):
            await page.mouse.wheel(0, step_dir)
            await asyncio.sleep(random.uniform(*_SCROLL_STEP_DELAY))
        return None
    except Exception as exc:
        return str(exc)


async def _do_wait(page: Any, target: str, timeout_ms: int) -> Optional[str]:
    try:
        if target:
            await page.wait_for_selector(target, timeout=timeout_ms)
        else:
            await asyncio.sleep(timeout_ms / 1000)
        return None
    except Exception as exc:
        return str(exc)


async def _submit_form(page: Any) -> Optional[str]:
    for sel in (
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Send')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Sign up')",
        "button:has-text('Register')",
        "button:has-text('Sign In')",
        "button:has-text('Log in')",
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click()
                await asyncio.sleep(_PAGE_SETTLE)
                return None
        except Exception:
            continue
    return "No submit button found"


# ==========================================================
# Mouse fallback — JS DOM coord search
# ==========================================================


async def _js_find_coords(
    page: Any, js_body: str, search_text: str
) -> Optional[Tuple[float, float]]:
    """Run a JS DOM text-search, return (x, y) of element center or None."""
    try:
        coords = await page.evaluate(js_body, search_text)
        if coords:
            return float(coords["x"]), float(coords["y"])
    except Exception as exc:
        logger.debug("browser: js coord search error: %r", exc)
    return None


async def _click_by_coord_search(page: Any, target: str) -> Optional[str]:
    """Last resort: find element by visible text via JS, click its pixel center."""
    search_text = _extract_search_text(target)
    if not search_text:
        return f"click: element not found — {target!r}"
    coords = await _js_find_coords(page, _JS_CLICK_SEARCH, search_text)
    if coords:
        logger.info("browser: mouse fallback click %r → %s", search_text, coords)
        await _human_click(page, *coords)
        return None
    return f"click: element not found — {target!r}"


async def _fill_by_coord_search(page: Any, target: str, value: str) -> Optional[str]:
    """Last resort: find input by label/placeholder/name via JS, click to focus, type."""
    hint = _extract_field_hint(target)
    if not hint:
        return f"fill: element not found — {target!r}"
    coords = await _js_find_coords(page, _JS_FILL_SEARCH, hint)
    if coords:
        logger.info("browser: mouse fallback fill %r → %s", hint, coords)
        await _human_click(page, *coords)
        await asyncio.sleep(_FOCUS_DELAY)
        await _human_type(page, value)
        return None
    return f"fill: element not found — {target!r}"


# ==========================================================
# Selector helpers
# ==========================================================


def _extract_search_text(target: str) -> Optional[str]:
    """
    Extract a plain-text search term from any selector format for JS DOM fallback.
    Returns None when target is purely structural (no text hint).
    """
    _, extracted = _normalize_selector(target)
    if extracted:
        return extracted
    m = _RE_HAS_TEXT_OR_CONTAINS.search(target)
    if m:
        return m.group(1)
    if not _CSS_META.search(target):
        return target.strip() or None
    return None


def _extract_field_hint(target: str) -> Optional[str]:
    """Like _extract_search_text but also extracts values from CSS attribute selectors."""
    m = _RE_ATTR_SELECTOR.search(target)
    if m:
        return m.group(1)
    return _extract_search_text(target)


def _normalize_selector(target: str) -> Tuple[str, Optional[str]]:
    """
    Returns (css_selector, extracted_text).
    css_selector  — jQuery :contains() converted to Playwright :has-text().
    extracted_text — text from mixed "css text='value'" patterns.
    """
    s = _RE_CONTAINS_SUB.sub(r":has-text(\1\2\1)", target)
    m = _RE_TEXT_ATTR_EXTRACT.search(s)
    if m:
        return s[: m.start()].strip(), m.group(2)
    return s, None


def _locator_chain(page: Any, target: str) -> List[Any]:
    css_sel, extracted_text = _normalize_selector(target)
    plain = not _CSS_META.search(target)  # bare attribute value — no CSS syntax

    locators: List[Any] = []
    if plain:
        # data-testid / data-cy only make sense for plain attribute values
        locators += [
            page.locator(f"[data-testid='{target}']"),
            page.locator(f"[data-cy='{target}']"),
        ]
    locators += [
        page.locator(css_sel),
        page.get_by_label(target),
        page.get_by_placeholder(target),
        page.get_by_role("button", name=target),
        page.get_by_role("textbox", name=target),
        page.get_by_role("link", name=target),
        page.get_by_text(target, exact=False),
    ]
    if extracted_text and extracted_text != target:
        locators += [
            page.get_by_text(extracted_text, exact=True),
            page.get_by_text(extracted_text, exact=False),
            page.get_by_role("button", name=extracted_text),
            page.get_by_role("link", name=extracted_text),
        ]
    return locators


# ==========================================================
# Human behavior layer
# ==========================================================


async def _human_type(page: Any, text: str) -> None:
    for i, char in enumerate(text):
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(*_TYPE_DELAY))
        if (i + 1) % _HUMAN_TYPE_PAUSE_INTERVAL == 0:
            await asyncio.sleep(random.uniform(0.08, 0.20))


async def _human_click(page: Any, x: float, y: float) -> None:
    jx = x + random.uniform(-3, 3)
    jy = y + random.uniform(-3, 3)
    await page.mouse.move(jx, jy)
    await asyncio.sleep(random.uniform(*_CLICK_MOVE_DELAY))
    await page.mouse.click(jx, jy)


# ==========================================================
# DOM hint extraction
# ==========================================================


async def _extract_interactive_elements(page: Any) -> str:
    """
    Return a compact list of up to 30 visible interactive elements for the LLM.
    Format: <selector> [role=X] [label=Y] [text=Z] [state=S] at=(cx,cy) [off-screen]
    """
    try:
        elements = await page.evaluate("""() => {
            const sel = [
                'a[href]', 'button', 'input:not([type="hidden"])', 'select', 'textarea',
                '[role="button"]', '[role="link"]', '[role="checkbox"]', '[role="radio"]',
                '[role="combobox"]', '[role="textbox"]', '[role="menuitem"]', '[role="tab"]',
            ].join(', ');

            return Array.from(document.querySelectorAll(sel))
                .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
                .slice(0, 30)
                .map(el => {
                    const r = el.getBoundingClientRect();
                    const states = [];
                    if (el.disabled)                                    states.push('disabled');
                    if (el.checked)                                     states.push('checked');
                    if (el.getAttribute('aria-checked') === 'true')     states.push('aria-checked');
                    if (el.getAttribute('aria-expanded') === 'true')    states.push('expanded');
                    if (el.getAttribute('aria-selected') === 'true')    states.push('selected');
                    if (el.getAttribute('aria-disabled') === 'true')    states.push('aria-disabled');

                    let bestSel = el.tagName.toLowerCase();
                    const testId = el.getAttribute('data-testid') || el.getAttribute('data-cy');
                    if (testId)                           { bestSel = `[data-testid="${testId}"]`; }
                    else if (el.id)                       { bestSel = `#${el.id}`; }
                    else if (el.getAttribute('name'))     {
                        const t = el.getAttribute('type');
                        bestSel = t
                            ? `${el.tagName.toLowerCase()}[name="${el.getAttribute('name')}"][type="${t}"]`
                            : `${el.tagName.toLowerCase()}[name="${el.getAttribute('name')}"]`;
                    } else if (el.getAttribute('aria-label')) {
                        bestSel = `${el.tagName.toLowerCase()}[aria-label="${el.getAttribute('aria-label').slice(0,40)}"]`;
                    } else if (el.getAttribute('type')) {
                        bestSel = `${el.tagName.toLowerCase()}[type="${el.getAttribute('type')}"]`;
                    }

                    return {
                        sel:        bestSel,
                        role:       el.getAttribute('role') || el.tagName.toLowerCase(),
                        ariaLabel:  (el.getAttribute('aria-label') || '').trim().slice(0, 50),
                        text:       (el.innerText || el.value || el.placeholder || '').trim().slice(0, 60),
                        states:     states.join(','),
                        cx:         Math.round(r.x + r.width  / 2),
                        cy:         Math.round(r.y + r.height / 2),
                        inViewport: r.top >= 0 && r.bottom <= window.innerHeight,
                    };
                });
        }""")
    except Exception as exc:
        logger.debug("extract_interactive_elements failed: %r", exc)
        return ""

    lines: List[str] = []
    for el in elements:
        parts = [el["sel"]]
        if el["role"] and el["role"] not in (
            "input",
            "button",
            "a",
            "select",
            "textarea",
        ):
            parts.append(f"role={el['role']}")
        if el["ariaLabel"]:
            parts.append(f"label={el['ariaLabel']!r}")
        if el["text"]:
            parts.append(f"text={el['text']!r}")
        if el["states"]:
            parts.append(f"state={el['states']}")
        parts.append(f"at=({el['cx']},{el['cy']})")
        if not el["inViewport"]:
            parts.append("off-screen")
        lines.append(" ".join(parts))

    return "\n".join(lines)


# ==========================================================
# SPA hydration wait
# ==========================================================


async def _wait_for_interactive(page: Any, timeout_ms: int = 8_000) -> None:
    """Wait until at least one interactive element is visible after navigation."""
    try:
        await page.wait_for_selector(
            "input:not([type='hidden']), button:not([disabled]), "
            "[role='button'], [role='textbox'], [role='combobox']",
            timeout=timeout_ms,
            state="visible",
        )
    except Exception:
        pass


# ==========================================================
# Screenshot — JPEG only (STB does not support WebP)
# ==========================================================


async def _screenshot(page: Any) -> str:
    raw = await page.screenshot(type="jpeg", quality=_SCREENSHOT_QUALITY)
    return f"data:image/jpeg;base64,{base64.b64encode(raw).decode('utf-8')}"


# ==========================================================
# Navigation
# ==========================================================


async def _navigate(page: Any, url: str) -> Optional[str]:
    try:
        await page.goto(url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")
        await asyncio.sleep(_PAGE_SETTLE)
        return None
    except Exception:
        try:
            await page.goto(url, timeout=_NAV_TIMEOUT * 2, wait_until="load")
            await asyncio.sleep(_PAGE_SETTLE)
            return None
        except Exception as exc:
            return f"Navigation failed: {exc}"


# ==========================================================
# Browser context helpers
# ==========================================================


_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=TranslateUI,ChromeWhatsNewUI",
]

# Suppress the --enable-automation flag Playwright injects by default.
# That flag sets navigator.webdriver=true and triggers Google's bot gate.
_IGNORE_DEFAULT_ARGS = ["--enable-automation"]

# Realistic Chrome 136 UA — keeps Google from flagging the browser as headless/bot.
# Update the version number when Chrome releases a major update.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


async def _launch_browser(pw: Any, headless: bool) -> Any:
    """Try real Chrome first (passes Google OAuth); fall back to Chromium."""
    import shutil

    kwargs = {
        "headless": headless,
        "args": _LAUNCH_ARGS,
        "ignore_default_args": _IGNORE_DEFAULT_ARGS,
    }
    if (
        shutil.which("google-chrome")
        or shutil.which("chrome")
        or shutil.which("google-chrome-stable")
    ):
        try:
            return await pw.chromium.launch(channel="chrome", **kwargs)
        except Exception:
            pass
    return await pw.chromium.launch(**kwargs)


async def _make_context(browser: Any, url: str) -> Any:
    domain = _domain(url)
    sf = _session_path(domain)
    kwargs: Dict[str, Any] = {
        "viewport": {"width": 1280, "height": 800},
        "device_scale_factor": 1,
        "user_agent": _USER_AGENT,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "color_scheme": "light",
    }
    if sf.exists():
        kwargs["storage_state"] = str(sf)
        logger.info("browser: loaded session for %s", domain)
    return await browser.new_context(**kwargs)


async def _new_page(ctx: Any) -> Any:
    page = await ctx.new_page()
    if _STEALTH is not None:
        await _STEALTH.apply_stealth_async(page)
    return page


def _save_session(ctx: Any, domain: str) -> None:
    """Fire-and-forget session save — errors are non-fatal."""

    async def _save() -> None:
        try:
            _SESSION_DIR.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=str(_session_path(domain)))
        except Exception as exc:
            logger.warning("browser: session save failed: %r", exc)

    try:
        asyncio.ensure_future(_save(), loop=asyncio.get_running_loop())
    except RuntimeError:
        pass


# ==========================================================
# URL / domain / session path helpers
# ==========================================================


def _normalize_url(url: str) -> str:
    if url and "://" not in url:
        return "https://" + url
    return url


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def _session_path(domain: str) -> Path:
    h = hashlib.sha256(domain.encode()).hexdigest()[:16]
    return _SESSION_DIR / f"{h}.json"


# ==========================================================
# Memory helpers
# ==========================================================


def _search_memory(memory_manager: Any, query: str) -> Optional[str]:
    if not memory_manager or not query:
        return None
    try:
        hits = memory_manager.search_candidates(query_text=query, top_k=_MEMORY_TOP_K)
        if hits:
            text = getattr(hits[0], "memory_text", None) or str(hits[0])
            return text.strip() or None
    except Exception as exc:
        logger.warning("browser: memory search failed: %r", exc)
    return None


def _store_memory(memory_manager: Any, key: str, value: str) -> None:
    if not memory_manager:
        return
    try:
        memory_manager.add_text(f"{key}: {value}", memory_type="flash", salience=0.6)
    except Exception as exc:
        logger.warning("browser: memory store failed: %r", exc)


# ==========================================================
# UI interaction
# ==========================================================


async def _ask_user(
    question: str,
    *,
    ui_output: Optional[Callable] = None,
    ui_input: Optional[Callable] = None,
) -> str:
    if ui_output is None or ui_input is None:
        logger.info("browser ask_user (no UI callbacks): %r", question)
        return ""
    try:
        await ui_output(question)
        answer = await ui_input()
        return (answer or "").strip()
    except Exception as exc:
        logger.warning("browser ask_user failed: %r", exc)
        return ""


# ==========================================================
# Close-intent classifier
# ==========================================================

_CLOSE_WORDS = {
    "close",
    "shut",
    "exit",
    "quit",
    "stop",
    "kill",
    "terminate",
    "done",
    "finished",
}
_KEEP_WORDS = {"keep", "open", "leave", "stay", "running", "alive"}
_YES_WORDS = {
    "yes",
    "yep",
    "yeah",
    "sure",
    "ok",
    "okay",
    "fine",
    "please",
    "do",
    "go",
    "yup",
    "absolutely",
}
_NO_WORDS = {"no", "nope", "nah", "dont", "don't", "not"}


def _parse_close_intent(answer: str) -> bool:
    """Return True = close the browser, False = keep it open."""
    words = set(re.sub(r"[^\w\s]", "", answer.lower()).split())
    if words & _CLOSE_WORDS:
        return True
    if words & _KEEP_WORDS:
        return False
    if words & _YES_WORDS:
        return True
    if words & _NO_WORDS:
        return False
    return True


# ==========================================================
# Coordinate parser — "at:X,Y" or "X,Y"
# ==========================================================


def _parse_coords(target: str) -> Optional[Tuple[float, float]]:
    t = target.strip()
    if t.lower().startswith("at:"):
        t = t[3:]
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*", t)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


# ==========================================================
# Registry entry point — singleton so browser state persists
# across execute() calls (retry reuse)
# ==========================================================

_TOOL_INSTANCE: Optional[BrowserTool] = None


def get_tool() -> BrowserTool:
    global _TOOL_INSTANCE
    if _TOOL_INSTANCE is None:
        _TOOL_INSTANCE = BrowserTool()
    return _TOOL_INSTANCE
