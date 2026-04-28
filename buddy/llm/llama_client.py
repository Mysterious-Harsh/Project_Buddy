# 🔒 LOCKED — llama_client.py
# Contract: LlamaClient.generate() / .chat() — streaming + blocking, JSON extraction,
#           think/gate-marker extraction, interrupt support.
# Allowed: bug fixes, compatibility patches, perf improvements that preserve the public API.
# Not allowed: changing generate()/chat()/generate() signatures, removing n_predict /
#              json_extract / interrupt_event / think / gate_marker params, changing
#              SSE parsing or _JsonCapture behaviour, altering retry/backoff logic.
"""
llama.cpp HTTP client — O(n) SSE streaming, incremental JSON capture,
think-block + gate-marker extraction, JSON repair, interrupt support.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import re
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError, HTTPError, ReadTimeout, Timeout
from urllib3.util.retry import Retry
from buddy.llm.json_repair import repair_json

from buddy.logger.logger import get_logger

try:
    import orjson  # type: ignore

    USE_ORJSON = True
except Exception:
    USE_ORJSON = False

logger = get_logger("llama_client")

# Stop strings: empty by default (pass explicitly if needed)
DEFAULT_STOP_STRINGS: List[str] = []

# Connection pooling config
POOL_CONNECTIONS = 4
POOL_MAXSIZE = 16

# Preview lengths for logging
_ERR_BODY_PREVIEW = 1200
_JSON_PREVIEW = 1200
_JSON_FAILURE_PREVIEW = 400

# Think-block detection constants (used in streaming state machine)
_THINK_END = "</think>"
_THINK_TAIL_KEEP = len(_THINK_END) - 1  # overlap chars kept across chunk boundaries

# Pre-compiled patterns for JSON diagnosis
_RE_UNQUOTED_KEYS = re.compile(r"\{\s*[a-zA-Z_]\w*\s*:")
_RE_TRAILING_COMMA = re.compile(r",\s*[}\]]")

# Module-level SSE JSON parser — avoids re-creating a closure on every streaming call.
# USE_ORJSON is a constant so the right branch is chosen once at import time.
if USE_ORJSON:

    def _sse_loads(b: bytes) -> Any:
        return orjson.loads(b)

else:

    def _sse_loads(b: bytes) -> Any:
        return json.loads(b.decode("utf-8"))


@dataclass(frozen=True)
class LlamaStats:
    """Statistics from a llama.cpp request"""

    total_s: float
    ttfb_s: Optional[float]
    chunks: int
    out_len: int
    finish_reason: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    retries: int
    endpoint: str
    model: str
    req_id: str
    json_attempts: int = 0


class _JsonCapture:
    """
    Incremental JSON capture with O(1) buffer tracking.

    Supports object {...}, array [...], or either. Returns (captured, remainder)
    when a complete top-level JSON value has been seen.
    """

    __slots__ = (
        "_root",
        "_max_chars",
        "_started",
        "_root_ch",
        "_depth_obj",
        "_depth_arr",
        "_in_string",
        "_escape",
        "_buf",
        "_buf_len",
    )

    def __init__(self, *, root: str = "object", max_chars: int = 120_000) -> None:
        """
        Args:
            root: "object" | "array" | "either"
            max_chars: Safety limit to prevent unbounded growth
        """
        if root not in ("object", "array", "either"):
            raise ValueError("root must be one of: object | array | either")
        self._root = root
        self._max_chars = int(max_chars)
        self.reset()

    def reset(self) -> None:
        """Reset capture state (call after failed validation to search for next JSON)"""
        self._started = False
        self._root_ch = ""
        self._depth_obj = 0
        self._depth_arr = 0
        self._in_string = False
        self._escape = False
        self._buf: List[str] = []
        self._buf_len = 0

    def _root_ok(self, ch: str) -> bool:
        """Check if character is acceptable root"""
        if self._root == "either":
            return ch in ("{", "[")
        if self._root == "object":
            return ch == "{"
        return ch == "["

    def _depth_total(self) -> int:
        """Total nesting depth"""
        return self._depth_obj + self._depth_arr

    def started(self) -> bool:
        """Has JSON root been found?"""
        return self._started

    def feed(self, chunk: str) -> Optional[Tuple[str, str]]:
        """
        Feed chunk of text, return (captured_json, remainder) when complete.

        Returns:
            None if JSON not yet complete
            (json_text, remainder_after_json) when complete
        """
        if not chunk:
            return None

        # Find acceptable root character
        if not self._started:
            for i, ch in enumerate(chunk):
                if self._root_ok(ch):
                    self._started = True
                    self._root_ch = ch
                    self._depth_obj = 0
                    self._depth_arr = 0

                    s = chunk[i:]
                    self._buf.append(s)
                    self._buf_len += len(s)

                    return self._scan_for_end(s)
            return None

        # Already started, accumulate
        self._buf.append(chunk)
        self._buf_len += len(chunk)

        if self._buf_len > self._max_chars:
            raise RuntimeError(
                f"json_capture exceeded max_chars={self._max_chars}"
                f" (buf_len={self._buf_len})"
            )

        return self._scan_for_end(chunk)

    def _scan_for_end(self, s: str) -> Optional[Tuple[str, str]]:
        """
        Scan newly-added segment to detect JSON completion.

        Returns:
            (captured_json, remainder) when complete, None otherwise
        """
        for j in range(len(s)):
            ch = s[j]

            if self._in_string:
                if self._escape:
                    self._escape = False
                    continue
                if ch == "\\":
                    self._escape = True
                    continue
                if ch == '"':
                    self._in_string = False
                continue

            # Not in string
            if ch == '"':
                self._in_string = True
                continue

            if ch == "{":
                self._depth_obj += 1
            elif ch == "}":
                self._depth_obj -= 1
                if self._depth_obj < 0:
                    return None  # Malformed
            elif ch == "[":
                self._depth_arr += 1
            elif ch == "]":
                self._depth_arr -= 1
                if self._depth_arr < 0:
                    return None  # Malformed

            # Complete when all depths return to zero
            if self._depth_total() == 0:
                full = "".join(self._buf)
                end_pos = len(full) - (len(s) - (j + 1))
                captured = full[:end_pos]
                remainder = s[j + 1 :]
                return captured, remainder

        return None


def _iter_sse_data_lines(resp: requests.Response, *, chunk_size: int = 4096):
    """
    O(n) SSE parser using offset-based reading.

    Uses offset tracking with periodic compaction instead of `del buf[:n]` on every
    line, which avoids the O(n²) behaviour of the naive approach.

    Yields raw SSE data payloads as bytes.
    """
    buf = bytearray()
    offset = 0
    MAX_BUF = 2_000_000
    COMPACT_THRESHOLD = 8192

    for chunk in resp.iter_content(chunk_size=chunk_size):
        if not chunk:
            continue
        buf.extend(chunk)

        while True:
            nl = buf.find(b"\n", offset)
            if nl == -1:
                if offset > COMPACT_THRESHOLD:
                    del buf[:offset]
                    offset = 0
                break

            line = bytes(buf[offset:nl])
            offset = nl + 1

            if line.endswith(b"\r"):
                line = line[:-1]
            if not line:
                continue
            if not line.startswith(b"data:"):
                continue

            yield line[5:].lstrip()

        # Safety: prevent unbounded growth
        if len(buf) > MAX_BUF:
            buf.clear()
            offset = 0
            raise RuntimeError("SSE buffer overflow (no newlines)")


def _find_plausible_json_root(s: str) -> int:
    """
    Find index of plausible JSON root in string.

    Heuristics:
    - Object: '{' followed by '"' (key) or '}' (empty)
    - Array: '[' followed by ']', '{', '"', digit, 't', 'f', 'n', '-'

    Returns:
        Index of plausible root, or -1 if not found
    """
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        if ch not in "{[":
            i += 1
            continue

        j = i + 1
        while j < n and s[j] in " \t\r\n":
            j += 1

        if ch == "{":
            if j < n and (s[j] == '"' or s[j] == "}"):
                return i
        else:  # '['
            if j < n and (s[j] in ']"{' or s[j].isdigit() or s[j] in "tfn-"):
                return i

        i += 1

    return -1


def _diagnose_json_error(json_text: str) -> str:
    """Diagnose common JSON errors for warning logs."""
    if not json_text:
        return "empty"

    issues = []

    if _RE_UNQUOTED_KEYS.search(json_text):
        issues.append("unquoted_keys")
    if "'" in json_text and '"' not in json_text:
        issues.append("single_quotes")
    if _RE_TRAILING_COMMA.search(json_text):
        issues.append("trailing_commas")
    if "//" in json_text or "/*" in json_text:
        issues.append("comments")

    try:
        json.loads(json_text)
    except json.JSONDecodeError as e:
        issues.append(f"parse_error_line_{e.lineno}_col_{e.colno}")
    except Exception as e:
        issues.append(f"error_{type(e).__name__}")

    return "+".join(issues) if issues else "unknown"


class LlamaClient:
    """
    llama.cpp HTTP client with streaming JSON extraction and interrupt support.

    - O(n) SSE parsing with incremental _JsonCapture and early stream close
    - Per-request interrupt events (thread-safe)
    - JSON validation with repair fallback and think/gate-marker extraction
    - Connection pooling with keep-alive; orjson fast path when available
    """

    __slots__ = (
        "model",
        "base_url",
        "timeout",
        "max_retries",
        "backoff_base",
        "stream_idle_timeout",
        "debug",
        "api_key",
        "_session",
        "_perf",
        "_mono",
        "_req_counter",
        "_req_lock",
    )

    def __init__(
        self,
        *,
        model: str = "local-model",
        base_url: str = "http://127.0.0.1:8080",
        timeout: Union[float, Tuple[float, float]] = (3.0, 180.0),
        max_retries: int = 3,
        backoff_base: float = 0.35,
        stream_idle_timeout: float = 120.0,
        debug: bool = False,
        session_pool_maxsize: int = 16,
        api_key: Optional[str] = None,
    ):
        if not model:
            raise ValueError("model must be non-empty")
        if not base_url:
            raise ValueError("base_url must be non-empty")

        bu = base_url.rstrip("/")
        if "localhost" in bu:
            bu = bu.replace("localhost", "127.0.0.1")

        self.model = model
        self.base_url = bu
        self.timeout = timeout
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.stream_idle_timeout = float(stream_idle_timeout)
        self.debug = bool(debug)
        self.api_key = api_key

        self._session = self._create_session(api_key, session_pool_maxsize)

        self._perf = time.perf_counter
        self._mono = time.monotonic

        self._req_counter = 0
        self._req_lock = threading.Lock()

    # --------------------------
    # Session & Request IDs
    # --------------------------
    def _next_req_id(self) -> str:
        """Generate unique request ID"""
        with self._req_lock:
            self._req_counter += 1
            c = self._req_counter
        tid = threading.get_ident() % 100000
        ms = int(time.time() * 1000) % 100000000
        return f"llm-{ms}-{c}-{tid}"

    def _create_session(self, api_key: Optional[str], maxsize: int) -> requests.Session:
        """Create optimized session with connection pooling"""
        s = requests.Session()
        s.trust_env = False

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        s.headers.update(headers)

        # Disable adapter retries (we handle retries manually with better logging)
        retry = Retry(
            total=0,
            connect=0,
            read=0,
            redirect=0,
            status=0,
            backoff_factor=0,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=max(POOL_CONNECTIONS, int(maxsize)),
            pool_maxsize=max(POOL_MAXSIZE, int(maxsize)),
            pool_block=True,
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    def _stream_timeout(self) -> Tuple[float, float]:
        """Get (connect_timeout, read_timeout) for streaming"""
        connect_t = 3.0
        if isinstance(self.timeout, tuple) and len(self.timeout) == 2:
            connect_t = float(self.timeout[0])
        elif isinstance(self.timeout, (int, float)):
            connect_t = float(self.timeout)

        read_t = float(self.stream_idle_timeout)
        return (connect_t, read_t)

    # --------------------------
    # Control
    # --------------------------
    def close(self) -> None:
        """Close session and cleanup"""
        try:
            self._session.close()
        except Exception:
            pass

    # --------------------------
    # Public API
    # --------------------------
    def chat(
        self,
        *,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        stream: bool = True,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        repeat_last_n: Optional[int] = None,
        seed: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        options: Optional[Dict[str, Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        interrupt_event: Optional[threading.Event] = None,
        # Vision: data URIs for multimodal input ["data:image/jpeg;base64,...", ...]
        # Injected into the last user message as OAI content array (image_url entries).
        # Supports multiple images — one entry per image.
        images: Optional[List[str]] = None,
        # JSON extraction (streaming-optimized, mirrors generate())
        json_extract: bool = False,
        json_validate: bool = False,
        json_root: str = "object",
        json_max_chars: int = 120_000,
        # Think + gate: wait for </think> before JSON capture; look for gate_marker
        # before scanning for JSON (None = scan directly after think/start).
        think: bool = True,
        gate_marker: Optional[str] = None,
    ) -> str:
        """
        Chat completion endpoint (/v1/chat/completions).

        Args:
            messages: Conversation history [{"role": "user", "content": "..."}]
            system: Optional system message
            stream: Enable streaming (faster TTFB)
            temperature: Sampling temperature (0.0 = greedy)
            max_tokens: Maximum tokens to generate
            top_p: Nucleus sampling threshold
            repeat_penalty: Repetition penalty
            repeat_last_n: Look back N tokens for repetition
            seed: Random seed for reproducibility
            stop: Stop sequences (string or list)
            options: Additional model-specific options
            on_delta: Callback for streaming chunks
            interrupt_event: Event to cancel request (thread-safe)
            images: Data URIs for vision input (["data:image/png;base64,...", ...]).
                    Injected into the last user message as image_url content blocks.
            json_extract: Extract first valid JSON object/array from stream
            json_validate: Validate extracted JSON (retries if invalid)
            json_root: "object" | "array" | "either"
            json_max_chars: Safety limit for JSON buffer
            think: When True (default), wait for </think> before JSON capture.
            gate_marker: When set, wait for this marker after think before scanning
                         for JSON. When None (default), scan directly.

        Returns:
            Generated text (or extracted JSON if json_extract=True)
        """
        if not messages:
            raise ValueError("messages must be non-empty")

        payload = self._build_chat_payload(
            messages=messages,
            system=system,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            repeat_last_n=repeat_last_n,
            seed=seed,
            stop=stop,
            options=options,
            images=images,
        )

        # Streaming JSON extraction setup (mirrors generate())
        json_capture = None
        if stream and json_extract:
            json_capture = _JsonCapture(root=json_root, max_chars=int(json_max_chars))

        text = self._call(
            endpoint="/v1/chat/completions",
            payload=payload,
            on_delta=on_delta,
            json_capture=json_capture,
            json_validate=bool(json_validate) if json_extract else False,
            interrupt_event=interrupt_event,
            think=think,
            gate_marker=gate_marker,
        )

        # Blocking mode JSON extraction
        if json_extract and not stream:
            _src = text
            if think:
                _ti = _src.find("</think>")
                if _ti != -1:
                    _src = _src[_ti + len("</think>") :]
            if gate_marker:
                _gi = _src.find(gate_marker)
                if _gi != -1:
                    _src = _src[_gi + len(gate_marker) :]
            extracted = self._extract_first_json_value(
                _src,
                root=json_root,
                validate=json_validate,
                max_chars=int(json_max_chars),
            )
            if extracted is not None:
                return extracted

        return text

    def generate(
        self,
        *,
        prompt: str,
        system: Optional[str] = None,
        stream: bool = False,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        n_predict: Optional[int] = None,
        top_p: Optional[float] = None,
        repeat_penalty: Optional[float] = None,
        repeat_last_n: Optional[int] = None,
        seed: Optional[int] = None,
        stop: Optional[Union[str, List[str]]] = None,
        options: Optional[Dict[str, Any]] = None,
        on_delta: Optional[Callable[[str], None]] = None,
        # JSON extraction (streaming-optimized)
        json_extract: bool = False,
        json_validate: bool = False,
        json_root: str = "object",
        json_max_chars: int = 120_000,
        interrupt_event: Optional[threading.Event] = None,
        think: bool = True,
        gate_marker: Optional[str] = None,
    ) -> str:
        """
        Text completion endpoint (/completions).

        Args:
            prompt: Input prompt
            system: Optional system context (prepended to prompt)
            stream: Enable streaming
            temperature: Sampling temperature
            max_tokens: Maximum tokens
            n_predict: Alternative to max_tokens (llama.cpp specific)
            top_p: Nucleus sampling
            repeat_penalty: Repetition penalty
            repeat_last_n: Repetition window
            seed: Random seed
            stop: Stop sequences
            options: Additional options
            on_delta: Streaming callback
            json_extract: Extract first valid JSON object/array
            json_validate: Validate extracted JSON (retries if invalid)
            json_root: "object" | "array" | "either"
            json_max_chars: Safety limit for JSON buffer
            interrupt_event: Cancellation event
            think: When True (default), wait for </think> before JSON capture.
            gate_marker: When set, wait for this marker after think before scanning
                         for JSON. When None (default), scan directly.

        Returns:
            Generated text (or extracted JSON if json_extract=True)
        """
        if prompt is None:
            raise ValueError("prompt must be a string")

        final_prompt = prompt
        if system:
            final_prompt = f"{system.rstrip()}\n\n{prompt.lstrip()}"

        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": final_prompt,
            "stream": stream,
            "temperature": float(temperature),
        }

        user_opts: Dict[str, Any] = dict(options or {})

        # Stop handling
        if stop is None:
            if "stop" not in user_opts and DEFAULT_STOP_STRINGS:
                user_opts["stop"] = DEFAULT_STOP_STRINGS[:]
        else:
            user_opts["stop"] = stop

        # Token limits
        if n_predict is not None:
            user_opts["n_predict"] = int(n_predict)
            user_opts.pop("max_tokens", None)
        elif max_tokens is not None:
            user_opts["max_tokens"] = int(max_tokens)
            user_opts.pop("n_predict", None)

        if top_p is not None:
            user_opts["top_p"] = float(top_p)
        if repeat_penalty is not None:
            user_opts["repeat_penalty"] = float(repeat_penalty)
        if repeat_last_n is not None:
            user_opts["repeat_last_n"] = int(repeat_last_n)
        if seed is not None:
            user_opts["seed"] = int(seed)

        payload.update(user_opts)

        # Streaming JSON extraction setup
        json_capture = None
        if stream and json_extract:
            json_capture = _JsonCapture(root=json_root, max_chars=int(json_max_chars))

        # Always use the native /completion endpoint — it accepts all llama.cpp
        # parameters (repeat_penalty, repeat_last_n, n_predict, etc.) without
        # restriction. Newer llama.cpp builds reject these on /v1/completions
        # because that endpoint is now strictly OpenAI-spec only (400 Bad Request).
        # Response parsing already handles the native {"content": "..."} format.
        text = self._call(
            endpoint="/completion",
            payload=payload,
            on_delta=on_delta,
            json_capture=json_capture,
            json_validate=bool(json_validate) if json_extract else False,
            interrupt_event=interrupt_event,
            think=think,
            gate_marker=gate_marker,
        )

        # Blocking mode JSON extraction
        if json_extract and not stream:
            _src = text
            if think:
                _ti = _src.find("</think>")
                if _ti != -1:
                    _src = _src[_ti + len("</think>") :]
            if gate_marker:
                _gi = _src.find(gate_marker)
                if _gi != -1:
                    _src = _src[_gi + len(gate_marker) :]
            extracted = self._extract_first_json_value(
                _src,
                root=json_root,
                validate=json_validate,
                max_chars=int(json_max_chars),
            )
            if extracted is not None:
                return extracted

        return text

    def warmup(self) -> bool:
        """
        Warm up model with small requests.

        Returns:
            True if successful, False otherwise
        """
        try:
            t0 = self._perf()

            self.chat(
                messages=[{"role": "user", "content": "ready"}],
                stream=False,
                temperature=0.0,
                max_tokens=1,
            )

            if self.debug:
                logger.info(
                    "llama warmup ok: model=%s time=%.3fs",
                    self.model,
                    self._perf() - t0,
                )
            return True
        except Exception as e:
            logger.warning("llama warmup failed: model=%s err=%r", self.model, e)
            return False

    # --------------------------
    # Payload Building
    # --------------------------
    def _build_chat_payload(
        self,
        *,
        messages: List[Dict[str, Any]],
        system: Optional[str],
        stream: bool,
        temperature: float,
        max_tokens: Optional[int],
        top_p: Optional[float],
        repeat_penalty: Optional[float],
        repeat_last_n: Optional[int],
        seed: Optional[int],
        stop: Optional[Union[str, List[str]]],
        options: Optional[Dict[str, Any]],
        images: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build chat completion payload.

        When images (data URIs) are provided, the last user message's content is
        converted from a plain string into an OAI multimodal content array:
          [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:..."}}, ...]
        """
        final_msgs: List[Dict[str, Any]] = list(messages)
        if system:
            final_msgs = [{"role": "system", "content": system}] + final_msgs

        # Inject images into last user message as OAI multimodal content array
        if images:
            for i in range(len(final_msgs) - 1, -1, -1):
                if final_msgs[i].get("role") == "user":
                    original = final_msgs[i].get("content", "")
                    content: List[Dict[str, Any]] = [
                        {"type": "text", "text": str(original)}
                    ]
                    for uri in images:
                        content.append({"type": "image_url", "image_url": {"url": uri}})
                    final_msgs[i] = {**final_msgs[i], "content": content}
                    break

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": final_msgs,
            "stream": bool(stream),
            "temperature": float(temperature),
        }

        if options:
            payload.update(options)

        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
            payload.pop("n_predict", None)
        if top_p is not None:
            payload["top_p"] = float(top_p)
        if repeat_penalty is not None:
            payload["repeat_penalty"] = float(repeat_penalty)
        if repeat_last_n is not None:
            payload["repeat_last_n"] = int(repeat_last_n)
        if seed is not None:
            payload["seed"] = int(seed)

        if stop is not None:
            payload["stop"] = stop
        else:
            if "stop" not in payload and DEFAULT_STOP_STRINGS:
                payload["stop"] = DEFAULT_STOP_STRINGS[:]

        return payload

    def _dumps(self, payload: Dict[str, Any]) -> bytes:
        """Serialize payload to JSON bytes (uses orjson if available)"""
        if USE_ORJSON:
            return orjson.dumps(payload)
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _loads(self, b: bytes) -> Any:
        """Deserialize JSON bytes (uses orjson if available)"""
        if USE_ORJSON:
            return orjson.loads(b)
        return json.loads(b.decode("utf-8", errors="replace"))

    # --------------------------
    # Diagnostics
    # --------------------------
    def _payload_summary(
        self, payload: Dict[str, Any], endpoint: str
    ) -> Dict[str, Any]:
        """Generate payload summary for logging"""
        stream = bool(payload.get("stream", False))
        temp = payload.get("temperature", None)
        max_tokens = payload.get("max_tokens", None)
        n_predict = payload.get("n_predict", None)
        stop = payload.get("stop", None)

        msg_count = None
        msg_chars = None
        if "messages" in payload and isinstance(payload.get("messages"), list):
            msgs = payload["messages"]
            msg_count = len(msgs)
            try:
                msg_chars = sum(
                    len(str(m.get("content", ""))) for m in msgs if isinstance(m, dict)
                )
            except Exception:
                msg_chars = None

        prompt_chars = None
        if "prompt" in payload:
            try:
                prompt_chars = len(str(payload.get("prompt", "")))
            except Exception:
                prompt_chars = None

        return {
            "endpoint": endpoint,
            "stream": stream,
            "temperature": temp,
            "max_tokens": max_tokens,
            "n_predict": n_predict,
            "stop_set": isinstance(stop, (list, str)) and bool(stop),
            "stop_len": (
                len(stop)
                if isinstance(stop, list)
                else (1 if isinstance(stop, str) else 0)
            ),
            "messages": msg_count,
            "messages_chars": msg_chars,
            "prompt_chars": prompt_chars,
            "model": self.model,
        }

    def _log_http_response_debug(self, req_id: str, resp: requests.Response) -> None:
        """Log HTTP response details (debug mode only)"""
        if not self.debug:
            return
        try:
            ct = resp.headers.get("content-type", "-")
            cl = resp.headers.get("content-length", "-")
            logger.debug(
                "llama http: req_id=%s status=%s content-type=%s content-length=%s"
                " url=%s",
                req_id,
                resp.status_code,
                ct,
                cl,
                getattr(resp, "url", "-"),
            )
        except Exception:
            pass

    def _preview_bytes(self, b: bytes, limit: int) -> str:
        """Create preview of bytes for logging"""
        if not b:
            return ""
        try:
            s = b.decode("utf-8", errors="replace")
        except Exception:
            s = repr(b)
        if len(s) > limit:
            return s[:limit] + "…(truncated)"
        return s

    def _json_try_load(self, s: str) -> bool:
        """Test if string is valid JSON"""
        try:
            if USE_ORJSON:
                orjson.loads(s)
            else:
                json.loads(s)
            return True
        except Exception:
            return False

    def _json_try_repair(self, s: str) -> Optional[str]:
        """
        Attempt to repair broken JSON text and return a compact valid JSON string.
        Returns None if repair fails.
        """
        try:
            # repair_json may return dict if return_dict=True
            obj = repair_json(s, return_dict=True)

            # Normalize output (compact) using fastest encoder available
            if USE_ORJSON:
                return orjson.dumps(obj).decode("utf-8")
            return json.dumps(obj, ensure_ascii=False)
        except Exception:
            return None

    def _extract_first_json_value(
        self,
        text: str,
        *,
        root: str,
        validate: bool,
        max_chars: int,
    ) -> Optional[str]:
        if not text:
            return None

        cap = _JsonCapture(root=root, max_chars=int(max_chars))

        # Feed full text in one pass — text is already in memory, chunking gains nothing.
        # On invalid JSON try the remainder once (handles multiple JSON objects in text).
        pending: Optional[str] = text
        for _ in range(2):  # at most 2 attempts: main text, then remainder
            if pending is None:
                break
            res = cap.feed(pending)
            if res is None:
                break
            captured, remainder = res
            if not validate:
                return captured
            if self._json_try_load(captured):
                return captured
            repaired = self._json_try_repair(captured)
            if repaired is not None:
                return repaired
            cap.reset()
            pending = remainder or None

        return None

    # --------------------------
    # Core Request Logic
    # --------------------------
    @dataclass(frozen=True)
    class _RawStats:
        """Internal statistics before enrichment"""

        total_s: float
        ttfb_s: Optional[float]
        chunks: int
        finish_reason: Optional[str]
        prompt_tokens: Optional[int]
        completion_tokens: Optional[int]
        total_tokens: Optional[int]
        done_seen: Optional[bool] = None
        client_early_stop: Optional[bool] = None
        json_attempts: int = 0

    def _call(
        self,
        *,
        endpoint: str,
        payload: Dict[str, Any],
        on_delta: Optional[Callable[[str], None]],
        json_capture: Optional[_JsonCapture] = None,
        json_validate: bool = False,
        interrupt_event: Optional[threading.Event] = None,
        think: bool = True,
        gate_marker: Optional[str] = None,
    ) -> str:
        """Execute request with retry logic. Thread-safe: each request gets its own interrupt_event."""
        # Create per-request interrupt if not provided
        if interrupt_event is None:
            interrupt_event = threading.Event()

        req_id = self._next_req_id()
        url = f"{self.base_url}{endpoint}"
        want_stream = bool(payload.get("stream", False))

        if self.debug:
            logger.info(
                "llama start: req_id=%s url=%s %s",
                req_id,
                url,
                self._payload_summary(payload, endpoint),
            )

        last_err: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                if want_stream:
                    text, rs = self._request_streaming(
                        req_id=req_id,
                        url=url,
                        payload=payload,
                        on_delta=on_delta,
                        json_capture=json_capture,
                        json_validate=json_validate,
                        interrupt_event=interrupt_event,
                        think=think,
                        gate_marker=gate_marker,
                    )
                else:
                    text, rs = self._request_blocking(
                        req_id=req_id,
                        url=url,
                        payload=payload,
                        interrupt_event=interrupt_event,
                    )

                _pt = rs.prompt_tokens if rs.prompt_tokens is not None else "-"
                _ct = rs.completion_tokens if rs.completion_tokens is not None else "-"
                _tt = rs.total_tokens if rs.total_tokens is not None else "-"
                _ttfb = "-" if rs.ttfb_s is None else f"{rs.ttfb_s:.3f}s"

                if not text:
                    logger.warning(
                        "llama empty_output: req_id=%s endpoint=%s finish=%s chunks=%d"
                        " ttfb=%s pt=%s ct=%s tt=%s done=%s json_attempts=%d",
                        req_id,
                        endpoint,
                        rs.finish_reason or "-",
                        rs.chunks,
                        _ttfb,
                        _pt,
                        _ct,
                        _tt,
                        rs.done_seen,
                        rs.json_attempts,
                    )

                if (rs.finish_reason or "").lower() == "length":
                    logger.warning(
                        "llama truncated_by_length: req_id=%s endpoint=%s out_len=%d"
                        " pt=%s ct=%s tt=%s",
                        req_id,
                        endpoint,
                        len(text),
                        _pt,
                        _ct,
                        _tt,
                    )

                if self.debug:
                    logger.info(
                        "llama ok: req_id=%s endpoint=%s total=%.3fs ttfb=%s chunks=%d"
                        " out_len=%d finish=%s pt=%s ct=%s tt=%s retries=%d"
                        " json_attempts=%d model=%s",
                        req_id,
                        endpoint,
                        rs.total_s,
                        _ttfb,
                        rs.chunks,
                        len(text),
                        rs.finish_reason or "-",
                        _pt,
                        _ct,
                        _tt,
                        attempt,
                        rs.json_attempts,
                        self.model,
                    )

                return text

            except (
                ConnectionError,
                Timeout,
                ReadTimeout,
                HTTPError,
                requests.RequestException,
                OSError,
                RuntimeError,
            ) as ex:
                last_err = ex

                logger.warning(
                    "llama call_error: req_id=%s endpoint=%s attempt=%d/%d err=%r",
                    req_id,
                    endpoint,
                    attempt + 1,
                    self.max_retries + 1,
                    ex,
                )

                if interrupt_event.is_set():
                    break
                if attempt >= self.max_retries:
                    break

                time.sleep(self.backoff_base * (2**attempt))

        raise RuntimeError(
            f"llama call failed req_id={req_id} after retries={attempt}: {last_err!r}"
        ) from last_err

    # --------------------------
    # Blocking Request
    # --------------------------
    def _request_blocking(
        self,
        *,
        req_id: str,
        url: str,
        payload: Dict[str, Any],
        interrupt_event: threading.Event,
    ) -> Tuple[str, _RawStats]:
        """Execute blocking (non-streaming) request"""
        perf = self._perf
        t0 = perf()

        body = self._dumps(payload)

        resp: Optional[requests.Response] = None
        try:
            resp = self._session.post(url, data=body, timeout=self._stream_timeout())
            self._log_http_response_debug(req_id, resp)

            if resp.status_code >= 400:
                preview = self._preview_bytes(resp.content or b"", _ERR_BODY_PREVIEW)
                logger.error(
                    "llama http_error: req_id=%s status=%s body=%s",
                    req_id,
                    resp.status_code,
                    preview,
                )
                resp.raise_for_status()

            # Parse response
            try:
                data = self._loads(resp.content)
            except Exception as je:
                preview = self._preview_bytes(resp.content or b"", _JSON_PREVIEW)
                logger.error(
                    "llama json_decode_error: req_id=%s err=%r body=%s",
                    req_id,
                    je,
                    preview,
                )
                raise RuntimeError(
                    f"llama json decode failed req_id={req_id}: {je!r}"
                ) from je

            if not isinstance(data, dict):
                logger.warning(
                    "llama unexpected_json_root: req_id=%s type=%s",
                    req_id,
                    type(data).__name__,
                )
                data = {}

            # Check for server error
            if "error" in data and isinstance(data.get("error"), dict):
                err = data["error"]
                msg = err.get("message") if isinstance(err, dict) else None
                raise RuntimeError(f"llama server_error req_id={req_id}: {msg!r}")

            # Extract text and metadata
            text, finish_reason = self._extract_text_openai(data)
            pt, ct, tt = self._extract_usage_openai(data)

            if not text:
                has_choices = isinstance(data.get("choices"), list) and bool(
                    data.get("choices")
                )
                logger.warning(
                    "llama no_text_in_response: req_id=%s has_choices=%s finish=%s",
                    req_id,
                    has_choices,
                    finish_reason or "-",
                )

            dt = perf() - t0
            return text, self._RawStats(
                total_s=dt,
                ttfb_s=None,
                chunks=1 if text else 0,
                finish_reason=finish_reason,
                prompt_tokens=pt,
                completion_tokens=ct,
                total_tokens=tt,
            )

        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass

    # --------------------------
    # Streaming Request (SSE)
    # --------------------------
    def _request_streaming(
        self,
        *,
        req_id: str,
        url: str,
        payload: Dict[str, Any],
        on_delta: Optional[Callable[[str], None]],
        json_capture: Optional[_JsonCapture],
        json_validate: bool,
        interrupt_event: threading.Event,
        think: bool = True,
        gate_marker: Optional[str] = None,
    ) -> Tuple[str, _RawStats]:
        perf = self._perf
        interrupt_check = interrupt_event.is_set
        json_try_load = self._json_try_load

        t0 = perf()
        t_first: Optional[float] = None
        chunks = 0
        done_seen = False
        client_early_stop = False
        json_attempts = 0

        out_parts: List[str] = []
        out_append = out_parts.append

        finish_reason: Optional[str] = None
        pt = ct = tt = None

        body = self._dumps(payload)

        resp: Optional[requests.Response] = None

        cap = json_capture
        have_valid_json = False
        valid_json_text = ""

        think_passed = not think
        think_tail = ""

        gate_marker_str = gate_marker
        gate_open = think_passed and (gate_marker_str is None)
        gate_tail = ""
        ROOT_TAIL_KEEP = 256

        try:
            resp = self._session.post(
                url, data=body, stream=True, timeout=self._stream_timeout()
            )
            self._log_http_response_debug(req_id, resp)

            if resp.status_code >= 400:
                preview = self._preview_bytes(resp.content or b"", _ERR_BODY_PREVIEW)
                logger.error(
                    "llama http_error(stream): req_id=%s status=%s body=%s",
                    req_id,
                    resp.status_code,
                    preview,
                )
                resp.raise_for_status()

            resp.raise_for_status()

            for data_part in _iter_sse_data_lines(resp, chunk_size=4096):
                if interrupt_check():
                    logger.warning("llama interrupted: req_id=%s", req_id)
                    try:
                        resp.close()
                    except Exception:
                        pass
                    break

                if data_part == b"[DONE]":
                    done_seen = True
                    break

                # Parse SSE data
                try:
                    obj = _sse_loads(data_part)
                    if not isinstance(obj, dict):
                        continue
                except Exception as e:
                    if self.debug:
                        preview = self._preview_bytes(data_part, 200)
                        logger.debug(
                            "llama sse_parse_error: req_id=%s err=%r data=%s",
                            req_id,
                            e,
                            preview,
                        )
                    continue

                # Check for server error in stream
                if "error" in obj and isinstance(obj.get("error"), dict):
                    err = obj["error"]
                    msg = err.get("message") if isinstance(err, dict) else None
                    raise RuntimeError(
                        f"llama server_error(stream) req_id={req_id}: {msg!r}"
                    )

                # Extract content delta
                piece, fr = self._extract_delta_openai(obj)
                if fr is not None:
                    finish_reason = fr

                # Extract usage if present
                usage = obj.get("usage")
                if isinstance(usage, dict):
                    _pt, _ct, _tt = self._extract_usage_openai(obj)
                    if _pt is not None:
                        pt = _pt
                    if _ct is not None:
                        ct = _ct
                    if _tt is not None:
                        tt = _tt

                if not piece:
                    continue

                if t_first is None:
                    t_first = perf()
                chunks += 1

                if on_delta is not None:
                    try:
                        on_delta(piece)
                    except Exception as e:
                        if self.debug:
                            logger.debug(
                                "llama on_delta_error: req_id=%s chunk=%d err=%r",
                                req_id,
                                chunks,
                                e,
                            )
                        # Continue streaming despite callback failure

                # Buffer output (unless JSON gate has closed)
                if cap is None or not gate_open:
                    out_append(piece)

                # JSON extraction with think detection + parameterized gate
                if cap is not None and not have_valid_json:
                    res = None
                    _json_piece = piece

                    # PHASE 1: wait for </think>
                    if not think_passed:
                        scan = think_tail + _json_piece
                        idx = scan.find(_THINK_END)
                        if idx == -1:
                            think_tail = (
                                scan[-_THINK_TAIL_KEEP:]
                                if len(scan) > _THINK_TAIL_KEEP
                                else scan
                            )
                            continue
                        think_passed = True
                        think_tail = ""
                        _json_piece = scan[idx + len(_THINK_END) :]
                        # If no gate marker needed, open the gate now
                        if gate_marker_str is None:
                            gate_open = True

                    # PHASE 2: wait for gate marker (only when gate still closed)
                    if not gate_open and gate_marker_str is not None:
                        scan = gate_tail + _json_piece
                        idx = scan.find(gate_marker_str)
                        if idx == -1:
                            keep = max(0, len(gate_marker_str) - 1)
                            gate_tail = scan[-keep:] if keep else ""
                            continue
                        gate_open = True
                        after = scan[idx + len(gate_marker_str) :]
                        gate_tail = ""
                        cap.reset()
                        root_i = _find_plausible_json_root(after)
                        if root_i == -1:
                            gate_tail = (
                                after[-ROOT_TAIL_KEEP:]
                                if len(after) > ROOT_TAIL_KEEP
                                else after
                            )
                            continue
                        _json_piece = after[root_i:]
                        res = cap.feed(_json_piece)
                    else:
                        # PHASE 3: gate open, accumulate JSON
                        if not cap.started():
                            scan = gate_tail + _json_piece
                            root_i = _find_plausible_json_root(scan)
                            if root_i == -1:
                                gate_tail = (
                                    scan[-ROOT_TAIL_KEEP:]
                                    if len(scan) > ROOT_TAIL_KEEP
                                    else scan
                                )
                                continue
                            gate_tail = ""
                            _json_piece = scan[root_i:]
                            res = cap.feed(_json_piece)
                        else:
                            res = cap.feed(_json_piece)

                    while res is not None and not have_valid_json:
                        captured, remainder = res
                        json_attempts += 1

                        if not json_validate:
                            have_valid_json = True
                            valid_json_text = captured
                            break

                        if json_try_load(captured):
                            have_valid_json = True
                            valid_json_text = captured
                            break

                        repaired = self._json_try_repair(captured)
                        if repaired is not None:
                            have_valid_json = True
                            valid_json_text = repaired
                            if self.debug:
                                logger.info(
                                    "llama json_repaired: req_id=%s attempt=%d"
                                    " raw_len=%d repaired_len=%d",
                                    req_id,
                                    json_attempts,
                                    len(captured),
                                    len(repaired),
                                )
                            break

                        preview = (
                            captured[:_JSON_FAILURE_PREVIEW]
                            if len(captured) > _JSON_FAILURE_PREVIEW
                            else captured
                        )
                        diagnosis = _diagnose_json_error(captured)
                        logger.warning(
                            "llama json_invalid: req_id=%s attempt=%d len=%d"
                            " diagnosis=%s preview=%r",
                            req_id,
                            json_attempts,
                            len(captured),
                            diagnosis,
                            preview,
                        )

                        cap.reset()

                        # search for next JSON in remainder
                        if remainder:
                            root_i = _find_plausible_json_root(remainder)
                            if root_i != -1:
                                res = cap.feed(remainder[root_i:])
                            else:
                                res = None
                                gate_tail = (
                                    remainder[-ROOT_TAIL_KEEP:]
                                    if len(remainder) > ROOT_TAIL_KEEP
                                    else remainder
                                )
                        else:
                            res = None

                    if have_valid_json:
                        client_early_stop = True
                        break

        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass

        # Final text: use extracted JSON if valid, otherwise full output
        text = (
            valid_json_text
            if (have_valid_json and valid_json_text)
            else "".join(out_parts)
        )

        dt = perf() - t0
        ttfb = (t_first - t0) if t_first is not None else None

        # Warn if stream ended unexpectedly
        if (not done_seen) and (not client_early_stop):
            logger.warning(
                "llama stream_end_without_done: req_id=%s chunks=%d out_len=%d"
                " finish=%s json_attempts=%d",
                req_id,
                chunks,
                len(text),
                finish_reason or "-",
                json_attempts,
            )

        return text, self._RawStats(
            total_s=dt,
            ttfb_s=ttfb,
            chunks=chunks,
            finish_reason=finish_reason,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            done_seen=done_seen,
            client_early_stop=client_early_stop,
            json_attempts=json_attempts,
        )

    # --------------------------
    # OpenAI Format Extraction
    # --------------------------
    @staticmethod
    def _extract_usage_openai(
        obj: Dict[str, Any],
    ) -> Tuple[Optional[int], Optional[int], Optional[int]]:
        """Extract token usage from response"""
        usage = obj.get("usage")
        if not usage or not isinstance(usage, dict):
            return None, None, None

        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        tt = usage.get("total_tokens")

        try:
            prompt_tokens = int(pt) if pt is not None else None
            completion_tokens = int(ct) if ct is not None else None
            total_tokens = int(tt) if tt is not None else None
            return prompt_tokens, completion_tokens, total_tokens
        except (ValueError, TypeError):
            return None, None, None

    @staticmethod
    def _extract_text_openai(obj: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """Extract text from blocking response.

        Handles two response shapes:
        - OAI /v1/completions & /v1/chat/completions: {"choices": [...]}
        - Native llama.cpp /completion: {"content": "...", "stop": true}
        """
        choices = obj.get("choices")
        if choices and isinstance(choices, list):
            c0 = choices[0]
            if isinstance(c0, dict):
                fr = c0.get("finish_reason")
                finish_reason = str(fr) if fr is not None else None

                t = c0.get("text")
                if isinstance(t, str) and t:
                    return t, finish_reason

                msg = c0.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str) and content:
                        return content, finish_reason

                return "", finish_reason

        # Fallback: native llama.cpp /completion format {"content": "...", "stop": true}
        native = obj.get("content")
        if isinstance(native, str):
            stop = obj.get("stop")
            finish_reason = "stop" if stop else None
            return native, finish_reason

        return "", None

    @staticmethod
    def _extract_delta_openai(obj: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """Extract delta from streaming chunk.

        Handles two streaming shapes:
        - OAI SSE: {"choices": [{"delta": {"content": "..."}, "finish_reason": null}]}
        - Native llama.cpp /completion SSE: {"content": "...", "stop": false}
        """
        choices = obj.get("choices")
        if choices and isinstance(choices, list):
            c0 = choices[0]
            if isinstance(c0, dict):
                fr = c0.get("finish_reason")
                finish_reason = str(fr) if fr is not None else None

                t = c0.get("text")
                if isinstance(t, str) and t:
                    return t, finish_reason

                delta = c0.get("delta")
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        return content, finish_reason

                return "", finish_reason

        # Fallback: native llama.cpp /completion streaming {"content": "...", "stop": false}
        native = obj.get("content")
        if isinstance(native, str):
            stop = obj.get("stop")
            finish_reason = "stop" if stop else None
            return native, finish_reason

        return "", None


if __name__ == "__main__":
    import sys
    from buddy.tests.test_brain_prompt import test_brain_prompt

    client = LlamaClient(
        model="local-model",
        base_url="http://127.0.0.1:8080",
        timeout=(3.0, 300.0),
        max_retries=1,
        backoff_base=0.35,
        stream_idle_timeout=300.0,
        debug=True,
        session_pool_maxsize=16,
        api_key=None,
    )

    print("warmup:", client.warmup())

    def on_print(d: str) -> None:
        sys.stdout.write(d)
        sys.stdout.flush()

    try:
        print("\n[stream completion w/ json_extract+validate]")
        out = client.generate(
            prompt=test_brain_prompt,
            stream=True,
            temperature=0.6,
            top_p=0.96,
            repeat_last_n=256,
            repeat_penalty=1.0,
            on_delta=on_print,
            json_extract=True,
            json_validate=True,
            json_root="object",
            stop=["<|im_end|>"],
        )
        print("\n---\nEXTRACTED:\n", out)

    finally:
        client.close()
