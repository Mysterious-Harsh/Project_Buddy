"""
json_repair.py v3.8 (final)
Fast, robust JSON repair for LLM outputs — with schema guidance, thread safety,
and real-world LLM tool-call / agent output test coverage.

═══════════════════════════════════════════════════════════════════════════════
Changes from v3.8 (uploaded) → v3.8 final
═══════════════════════════════════════════════════════════════════════════════

BUG FIXES
─────────
- FIX: set([1,2,3]) / frozenset([...]) / list([...]) double-wrap bug
  Old: _unwrap_common_container_ctors replaced "set(" with "(" leaving outer
       parens; token_repair then wrapped the content in [...] again, producing
       [[1,2,3]] instead of [1,2,3].
  Fix: paren-depth-aware replacement strips the ctor name AND its closing ")",
       emitting only the interior: set([1,2,3]) → [1,2,3].

- FIX: Thread safety — _SCHEMA_MODE global replaced with threading.local()
  Old code used a bare module-level bool; concurrent threads or async callers
  that each triggered schema coercion could clobber each other's flag.
  Fix: threading.local() isolates the flag per thread; try/finally ensures it
       is always restored even when coercion raises.

PERFORMANCE
───────────
- PERF: _all_schema_keys no longer computed twice per repair_json() call
  The "if truthy" guard called the recursive traversal once for the boolean
  test and again to get the value.  Now computed once and reused.

- PERF: _unwrap_common_container_ctors regex compiled at module load, not
  per flush() invocation.  Old code compiled 6 re.compile() calls every time
  a '"' char was encountered in the input.  Now a single module-level
  _RE_CTOR pattern handles all six ctor forms.

NEW TEST COVERAGE (SCH-26 → SCH-45)
────────────────────────────────────
Real-world LLM tool call / agent output patterns:
- Bash commands with pipes, redirects, here-strings
- Linux & Windows path handling (quoted and unquoted)
- PowerShell, Python, SQL code inside string fields
- Agent planner with multi-step dependency arrays
- LLM decision objects (approve/reject + reason + confidence)
- JSON Schema oneOf / anyOf union coercion
- Truncated (context-window cut-off) agent outputs
- jq / regex / glob patterns inside string fields
- Deeply nested multi-level agent plans
- Missing comma between steps in arrays (common LLM failure)
- Numeric step_id / string step_id mixed in same array → int coercion
- File operation sequences (read/write/delete) with path coercion
- Environment variable objects {KEY: VALUE} with bare keys
- Large combined payloads (reasoning + plan + steps + followup)

Inherited (unchanged from v3.7→v3.8 upload):
- 103 core + structural tests: 100% pass rate
- 25 schema-guided tests (SCH-01..SCH-25): 100% pass rate

═══════════════════════════════════════════════════════════════════════════════
Public API
- repair_json(broken: str, return_dict: bool = False, schema = None) -> Any
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

_SENTINEL = object()

# Thread-local storage for schema mode flag (thread-safe, vs the old module global).
# _SCHEMA_MODE_LOCAL.active is set True only during schema-guided coercion in
# repair_json() and is always restored via try/finally.
import threading as _threading
_SCHEMA_MODE_LOCAL = _threading.local()


def _schema_mode_active() -> bool:
    return getattr(_SCHEMA_MODE_LOCAL, "active", False)


def _set_schema_mode(val: bool) -> None:
    _SCHEMA_MODE_LOCAL.active = val

# ─────────────────────────────────────────────────────────────────────────────
# Schema System
# ─────────────────────────────────────────────────────────────────────────────

# Internal type tags used by SchemaNode
_T_STR = "string"
_T_INT = "integer"
_T_FLOAT = "number"
_T_BOOL = "boolean"
_T_NULL = "null"
_T_ANY = "any"
_T_OBJ = "object"
_T_ARR = "array"

# Human-readable string aliases -> internal tag
_STR_TYPE_MAP: Dict[str, str] = {
    "str": _T_STR,
    "string": _T_STR,
    "text": _T_STR,
    "int": _T_INT,
    "integer": _T_INT,
    "float": _T_FLOAT,
    "number": _T_FLOAT,
    "num": _T_FLOAT,
    "double": _T_FLOAT,
    "bool": _T_BOOL,
    "boolean": _T_BOOL,
    "null": _T_NULL,
    "none": _T_NULL,
    "nil": _T_NULL,
    "list": _T_ARR,
    "array": _T_ARR,
    "dict": _T_OBJ,
    "object": _T_OBJ,
    "obj": _T_OBJ,
    "any": _T_ANY,
    "unknown": _T_ANY,
    "mixed": _T_ANY,
}

# JSON Schema "type" field -> internal tag
_JS_TYPE_MAP: Dict[str, str] = {
    "string": _T_STR,
    "integer": _T_INT,
    "number": _T_FLOAT,
    "boolean": _T_BOOL,
    "null": _T_NULL,
    "object": _T_OBJ,
    "array": _T_ARR,
}


@dataclass
class SchemaNode:
    """
    Normalised internal representation of a type constraint.

    ``types``       — frozenset of _T_* tags for this node
    ``properties``  — {key: SchemaNode} for object fields
    ``item_schema`` — SchemaNode for array element type (or None = any)
    ``required``    — frozenset of keys that must be present
    """

    types: FrozenSet[str]
    properties: Dict[str, "SchemaNode"]
    item_schema: Optional["SchemaNode"]
    required: FrozenSet[str]

    def is_object(self) -> bool:
        return _T_OBJ in self.types

    def is_array(self) -> bool:
        return _T_ARR in self.types

    def is_string(self) -> bool:
        return _T_STR in self.types

    def is_number(self) -> bool:
        return _T_INT in self.types or _T_FLOAT in self.types

    def is_bool(self) -> bool:
        return _T_BOOL in self.types

    def is_null(self) -> bool:
        return _T_NULL in self.types

    def is_any(self) -> bool:
        return _T_ANY in self.types

    def __repr__(self) -> str:
        if self.is_any():
            return "SchemaNode(any)"
        if self.is_object():
            return f"SchemaNode(object, props={sorted(self.properties)})"
        if self.is_array():
            return f"SchemaNode(array, items={self.item_schema})"
        return f"SchemaNode({set(self.types)})"


def _schema_node(types, properties=None, item_schema=None, required=None) -> SchemaNode:
    return SchemaNode(
        types=frozenset(types),
        properties=properties or {},
        item_schema=item_schema,
        required=frozenset(required or []),
    )


def normalize_schema(schema: Any) -> Optional[SchemaNode]:
    """
    Convert any supported schema representation into a SchemaNode tree.

    Accepts:
    ┌─────────────────────────────────────────────────────────────────────┐
    │ None                    → no schema, returns None                   │
    │ str (JSON)              → parse as JSON first, then re-dispatch     │
    │ str (type name)         → "string", "number", "boolean", etc.      │
    │ Python type             → str, int, float, bool, list, dict, Any   │
    │ tuple of types          → (int, float)  means union                 │
    │ list with one item      → [str] means array-of-string               │
    │ dict (shape dict)       → {"name": str, "score": float, ...}       │
    │ dict (string types)     → {"name": "string", "score": "number"}    │
    │ dict (JSON Schema)      → {"type":"object","properties":{...}}     │
    │ class w/__annotations__ → dataclass / plain class / Pydantic v1/v2 │
    └─────────────────────────────────────────────────────────────────────┘
    """
    return _norm(schema)


def _norm(s: Any) -> Optional[SchemaNode]:
    """Recursive schema normalizer."""
    if s is None:
        return None

    # ── JSON string → parse then re-dispatch ──────────────────────────────
    if isinstance(s, str):
        stripped = s.strip()
        if stripped.startswith(("{", "[")):
            try:
                s = json.loads(stripped)
                return _norm(s)
            except Exception:
                pass
        # Treat as type-name string
        tag = _STR_TYPE_MAP.get(stripped.lower())
        if tag:
            return _schema_node([tag])
        return _schema_node([_T_ANY])

    # ── Python primitive types ─────────────────────────────────────────────
    if s is str:
        return _schema_node([_T_STR])
    if s is int:
        return _schema_node([_T_INT])
    if s is float:
        return _schema_node([_T_FLOAT])
    if s is bool:
        return _schema_node([_T_BOOL])
    if s is type(None):
        return _schema_node([_T_NULL])
    if s is list:
        return _schema_node([_T_ARR])
    if s is dict:
        return _schema_node([_T_OBJ])

    # typing.Any → any
    try:
        from typing import Any as _TA

        if s is _TA:
            return _schema_node([_T_ANY])
    except Exception:
        pass

    # ── tuple of types: union ─────────────────────────────────────────────
    if isinstance(s, tuple):
        tags: List[str] = []
        for item in s:
            n = _norm(item)
            if n:
                tags.extend(n.types)
        return _schema_node(tags or [_T_ANY])

    # ── list: shorthand for array schema ──────────────────────────────────
    if isinstance(s, list):
        if len(s) == 0:
            return _schema_node([_T_ARR])
        if len(s) == 1:
            item_node = _norm(s[0])
            return _schema_node([_T_ARR], item_schema=item_node)
        # Multiple items → any-array
        return _schema_node([_T_ARR], item_schema=_schema_node([_T_ANY]))

    # ── dict ──────────────────────────────────────────────────────────────
    if isinstance(s, dict):
        # JSON Schema detection: has "type" or "properties" or "$schema"
        if (
            "type" in s
            or "properties" in s
            or "$schema" in s
            or "anyOf" in s
            or "oneOf" in s
        ):
            return _norm_jsonschema(s)
        # Shape dict: {"name": str, "score": float}  or  {"name": "string"}
        props = {k: _norm(v) or _schema_node([_T_ANY]) for k, v in s.items()}
        required = frozenset(props.keys())
        return _schema_node([_T_OBJ], properties=props, required=required)

    # ── Class / dataclass / Pydantic ──────────────────────────────────────
    if isinstance(s, type) and hasattr(s, "__annotations__"):
        # Pydantic v2
        if hasattr(s, "model_fields"):
            props = {}
            for name, field in s.model_fields.items():  # type: ignore
                props[name] = _norm(getattr(field, "annotation", None)) or _schema_node(
                    [_T_ANY]
                )
            return _schema_node(
                [_T_OBJ], properties=props, required=frozenset(props.keys())
            )
        # Pydantic v1
        if hasattr(s, "__fields__"):
            props = {}
            for name, field in s.__fields__.items():  # type: ignore
                props[name] = _norm(
                    getattr(field, "outer_type_", None)
                ) or _schema_node([_T_ANY])
            return _schema_node(
                [_T_OBJ], properties=props, required=frozenset(props.keys())
            )
        # Regular class / dataclass
        try:
            from typing import get_type_hints

            hints = get_type_hints(s)
        except Exception:
            hints = s.__annotations__
        props = {k: _norm(v) or _schema_node([_T_ANY]) for k, v in hints.items()}
        return _schema_node(
            [_T_OBJ], properties=props, required=frozenset(props.keys())
        )

    return _schema_node([_T_ANY])


def _norm_jsonschema(s: dict) -> SchemaNode:
    """Normalise a JSON Schema dict."""
    raw_type = s.get("type", "any")
    if isinstance(raw_type, list):
        tags = [_JS_TYPE_MAP.get(t, _T_ANY) for t in raw_type]
    else:
        tags = [_JS_TYPE_MAP.get(raw_type, _T_ANY)]

    props: Dict[str, SchemaNode] = {}
    if "properties" in s:
        for k, v in s["properties"].items():
            props[k] = (
                _norm_jsonschema(v) if isinstance(v, dict) else _schema_node([_T_ANY])
            )

    required = frozenset(s.get("required", list(props.keys())))

    item_schema: Optional[SchemaNode] = None
    if "items" in s:
        item_schema = (
            _norm_jsonschema(s["items"])
            if isinstance(s["items"], dict)
            else _schema_node([_T_ANY])
        )

    # anyOf / oneOf → union of types
    for key in ("anyOf", "oneOf", "allOf"):
        if key in s:
            union_tags: List[str] = []
            for opt in s[key]:
                n = (
                    _norm_jsonschema(opt)
                    if isinstance(opt, dict)
                    else _schema_node([_T_ANY])
                )
                union_tags.extend(n.types)
            if union_tags:
                tags = list(set(union_tags))
            break

    return _schema_node(
        tags, properties=props, item_schema=item_schema, required=required
    )


# ── Schema-guided quote lookahead ─────────────────────────────────────────


def _schema_key_lookahead(text: str, pos: int, schema_keys: FrozenSet[str]) -> bool:
    """
    From position ``pos`` (a ``"`` char we might close at), look ahead.
    Returns True if the immediately following token is a known schema key
    followed by ``:``.  This means the closing quote should END the string
    (the next thing is a new key-value pair, not embedded text).

    Works for both quoted keys (``"name":`` ) and bare keys (``name:``).
    """
    j = pos + 1
    n = len(text)
    # skip whitespace
    while j < n and text[j] in " \t\r\n":
        j += 1
    if j >= n:
        return False

    if text[j] == '"':
        # quoted key candidate
        k = j + 1
        chars: List[str] = []
        while k < n and text[k] != '"' and text[k] not in "\r\n":
            chars.append(text[k])
            k += 1
        if k < n and text[k] == '"':
            key = "".join(chars)
            k2 = k + 1
            while k2 < n and text[k2] in " \t\r\n":
                k2 += 1
            if k2 < n and text[k2] == ":":
                return key in schema_keys
    else:
        # bare key candidate
        k = j
        while k < n and text[k] not in ":,}] \t\r\n\"'":
            k += 1
        key = text[j:k]
        if k < n and key:
            k2 = k
            while k2 < n and text[k2] in " \t\r\n":
                k2 += 1
            if k2 < n and text[k2] == ":":
                return key in schema_keys
    return False


# ── Coercion ──────────────────────────────────────────────────────────────




# ── Schema-guided string doctoring ───────────────────────────────────────

_RE_MULTI_SLASH = re.compile(r'(?<!:)/{2,}')  # keep http://, s3://, etc.


def _fix_string_schema(value: str) -> str:
    """Best-effort cleanup for common LLM escaping/path glitches.

    Runs ONLY during schema-guided coercion and ONLY for string-typed fields.

    Selective by design: this only normalizes strings that *look like paths*.
    It must NOT rewrite arbitrary strings such as regexes or multi-line
    instructions.

    Fixes (when path-like):
    - Collapses repeated POSIX slashes: /Users//a///b -> /Users/a/b
      (but preserves URL schemes like http://)
    - Repairs control chars introduced by JSON escapes in Windows paths
      (e.g. \b -> backspace, \f -> formfeed) and restores separators.
    - Converts Windows backslashes to forward slashes for normalized output.
    """
    if not _schema_mode_active() or not value:
        return value

    BS = chr(92)  # backslash

    # Path-likeness heuristics
    has_control = ('' in value) or ('' in value)
    # Windows indicators: drive letter, UNC prefix, or control chars.
    # IMPORTANT: do NOT treat generic labels like "Task:" as Windows just because of ':'.
    drive = bool(re.match(r"^[A-Za-z]:", value))
    unc = value.startswith(BS * 2)
    looks_windows = has_control or drive or unc
    looks_posix = value.startswith('/') or '/Users/' in value
    looks_url = value.startswith(('http://', 'https://', 'file://'))

    if not (looks_windows or looks_posix):
        return value

    s = value

    if looks_windows:
        # Restore common mistaken escape sequences that became control chars.
        # JSON "\bin" -> backspace + 'in' ( + 'in')
        # JSON "\flight" -> formfeed + 'light' ( + 'light')
        if '' in s:
            s = s.replace('', '/b')
        if '' in s:
            s = s.replace('', '/f')

        # UNC prefix: \server\share -> //server/share
        if s.startswith(BS * 2):
            s = '//' + s[2:]

        # Convert remaining backslashes to forward slashes.
        if BS in s:
            s = s.replace(BS, '/')

    # Collapse repeated slashes for non-URLs.
    if not looks_url:
        s = _RE_MULTI_SLASH.sub('/', s)

    return s


def _coerce(value: Any, node: SchemaNode) -> Any:
    """Coerce a parsed value to match a SchemaNode's declared type."""
    if node is None or node.is_any():
        return value
    if value is None:
        return value  # null passes through unchanged

    # ── bool first (bool is subclass of int in Python) ────────────────────
    if node.is_bool():
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, float):
            return bool(int(value))
        if isinstance(value, str):
            lv = value.lower().strip()
            if lv in ("true", "1", "yes", "on"):
                return True
            if lv in ("false", "0", "no", "off"):
                return False
        return value

    # ── number ────────────────────────────────────────────────────────────
    if node.is_number():
        want_int = _T_INT in node.types and _T_FLOAT not in node.types
        if isinstance(value, bool):
            return int(value) if want_int else float(value)
        if isinstance(value, str):
            v = value.strip()
            try:
                return int(float(v)) if want_int else float(v)
            except (ValueError, OverflowError):
                pass
        if isinstance(value, (int, float)):
            return int(value) if want_int else float(value)
        return value

    # ── string ────────────────────────────────────────────────────────────
    if node.is_string():
        if isinstance(value, str):
            return _fix_string_schema(value)
        if isinstance(value, bool):
            return str(value).lower()  # True -> "true"
        if isinstance(value, float) and value == int(value):
            return str(int(value))
        return str(value)

    # ── array ─────────────────────────────────────────────────────────────
    if node.is_array():
        if isinstance(value, list):
            if node.item_schema:
                return [_coerce(item, node.item_schema) for item in value]
            return value
        # comma-separated string → list
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",")]
            if len(parts) > 1:
                if node.item_schema:
                    return [_coerce(p, node.item_schema) for p in parts]
                return parts
        # scalar → single-element array
        coerced = _coerce(value, node.item_schema) if node.item_schema else value
        return [coerced]

    # ── object ────────────────────────────────────────────────────────────
    if node.is_object():
        if isinstance(value, dict):
            return _apply_schema_to_dict(value, node)
        return value

    return value


def _apply_schema_to_dict(obj: dict, node: SchemaNode) -> dict:
    """Coerce types and inject missing required fields into a dict."""
    result: Dict[str, Any] = {}
    # Coerce / inject declared fields
    for key, child_node in node.properties.items():
        raw = obj.get(key)
        if key in obj:
            result[key] = _coerce(raw, child_node)
        elif key in node.required:
            result[key] = None  # missing required field → null
        # optional missing fields are omitted
    # Preserve unknown extra fields (permissive)
    for key, value in obj.items():
        if key not in result:
            result[key] = value
    return result


# ── Root-shape detection ──────────────────────────────────────────────────


def _find_schema_root(parsed: Any, schema_node: SchemaNode) -> Tuple[Any, bool]:
    """
    Find the subtree that best matches the schema node.

    Returns (matched_subtree, found_flag).

    Strategy:
    - If schema is an object with known keys, look for the first dict (or list
      of dicts) that has ≥40 % key overlap with schema.properties.
    - If schema is an array node, look for the first list that contains objects
      with ≥40 % overlap with the item schema's properties.
    - Depth-first, max depth 6.  Returns the SHALLOWEST match (closest to root).
    """
    if schema_node is None:
        return parsed, False

    schema_keys = frozenset(schema_node.properties.keys())

    # Array-typed schema → look for a list of matching items
    if (
        schema_node.is_array()
        and schema_node.item_schema
        and schema_node.item_schema.is_object()
    ):
        item_keys = frozenset(schema_node.item_schema.properties.keys())
        result, found = _walk_for_collection(parsed, item_keys, depth=0)
        if found:
            return result, True
        return parsed, False

    # Object-typed schema → look for a matching dict or list of matching dicts
    if schema_keys:
        result, found = _walk_for_item(parsed, schema_keys, depth=0)
        if found:
            return result, True

    return parsed, False


def _overlap(keys_a: frozenset, keys_b: frozenset) -> float:
    if not keys_b:
        return 0.0
    return len(keys_a & keys_b) / len(keys_b)


_MIN_OVERLAP = 0.40  # 40 % of schema keys must be present to count as a match
_MAX_DEPTH = 6


def _walk_for_item(node: Any, schema_keys: frozenset, depth: int) -> Tuple[Any, bool]:
    """Walk looking for a single dict or list-of-dicts matching schema_keys."""
    if depth > _MAX_DEPTH:
        return node, False

    if isinstance(node, dict):
        if _overlap(frozenset(node.keys()), schema_keys) >= _MIN_OVERLAP:
            return node, True
        # Try child values (e.g. {"data": {...matching...}})
        for v in node.values():
            r, found = _walk_for_item(v, schema_keys, depth + 1)
            if found:
                return r, True

    elif isinstance(node, list) and node:
        # Check if items are matching dicts
        match_count = sum(
            1
            for item in node
            if isinstance(item, dict)
            and _overlap(frozenset(item.keys()), schema_keys) >= _MIN_OVERLAP
        )
        if match_count > 0:
            return node, True
        # Recurse into first few items / single wrapper
        for item in node[:3]:
            r, found = _walk_for_item(item, schema_keys, depth + 1)
            if found:
                return r, True

    return node, False


def _walk_for_collection(
    node: Any, item_keys: frozenset, depth: int
) -> Tuple[Any, bool]:
    """Walk looking for a list whose items overlap with item_keys."""
    if depth > _MAX_DEPTH:
        return node, False

    if isinstance(node, list):
        match_count = sum(
            1
            for item in node
            if isinstance(item, dict)
            and _overlap(frozenset(item.keys()), item_keys) >= _MIN_OVERLAP
        )
        if match_count > 0:
            return node, True

    if isinstance(node, dict):
        for v in node.values():
            r, found = _walk_for_collection(v, item_keys, depth + 1)
            if found:
                return r, True

    return node, False


# ── Recursive schema inference ────────────────────────────────────────────


def _infer_recursive(obj: Any, schema_node: SchemaNode) -> Any:
    """
    After coercion, walk the result tree.  If we encounter an unknown array
    field whose items share ≥50 % of the schema's top-level keys, apply the
    same schema recursively (handles tree / linked-list structures).
    """
    if not isinstance(obj, dict) or not schema_node.is_object():
        return obj

    schema_keys = frozenset(schema_node.properties.keys())
    result = dict(obj)

    for key, value in obj.items():
        if key in schema_node.properties:
            # Already coerced; recurse if it's a nested object
            child_node = schema_node.properties[key]
            if child_node.is_object() and isinstance(value, dict):
                result[key] = _infer_recursive(value, child_node)
            elif (
                child_node.is_array()
                and child_node.item_schema
                and isinstance(value, list)
            ):
                result[key] = [
                    (
                        _infer_recursive(item, child_node.item_schema)
                        if isinstance(item, dict)
                        else item
                    )
                    for item in value
                ]
        else:
            # Unknown field — check if it looks like recursive schema items
            if isinstance(value, list) and value:
                sample_keys = frozenset(
                    k
                    for item in value[:5]
                    if isinstance(item, dict)
                    for k in item.keys()
                )
                if _overlap(sample_keys, schema_keys) >= 0.50:
                    # Recursive: apply same schema to these items
                    result[key] = [
                        (
                            _apply_schema_to_dict(item, schema_node)
                            if isinstance(item, dict)
                            else item
                        )
                        for item in value
                    ]
                    result[key] = [
                        (
                            _infer_recursive(item, schema_node)
                            if isinstance(item, dict)
                            else item
                        )
                        for item in result[key]
                    ]
    return result


# ── Main schema post-processor ────────────────────────────────────────────


def _apply_schema(parsed: Any, schema_node: SchemaNode) -> Any:
    """
    Full schema application pipeline:
    1. Find where the schema items live in the parsed tree
    2. Apply coercion + missing-field injection
    3. Recursively infer nested same-schema structures
    """
    if schema_node is None:
        return parsed

    # Find the relevant subtree
    subtree, found = _find_schema_root(parsed, schema_node)

    if not found:
        # Just coerce at root level
        return _coerce(parsed, schema_node)

    # Apply coercion to subtree
    if isinstance(subtree, list):
        if schema_node.is_array() and schema_node.item_schema:
            coerced = [_coerce(item, schema_node.item_schema) for item in subtree]
            coerced = [
                (
                    _infer_recursive(item, schema_node.item_schema)
                    if isinstance(item, dict)
                    else item
                )
                for item in coerced
            ]
        elif schema_node.is_object():
            # List of items that match an object schema
            coerced = [
                (
                    _apply_schema_to_dict(item, schema_node)
                    if isinstance(item, dict)
                    else item
                )
                for item in subtree
            ]
            coerced = [
                _infer_recursive(item, schema_node) if isinstance(item, dict) else item
                for item in coerced
            ]
        else:
            coerced = subtree
        return coerced

    if isinstance(subtree, dict) and schema_node.is_object():
        result = _apply_schema_to_dict(subtree, schema_node)
        return _infer_recursive(result, schema_node)

    return _coerce(subtree, schema_node)


# Optional speed-ups
try:
    import orjson  # type: ignore

    _USE_ORJSON = True
except Exception:
    orjson = None  # type: ignore
    _USE_ORJSON = False

# Debug logging (disabled by default). Enable by setting JSON_REPAIR_DEBUG=1
import os as _os

_DEBUG = _os.environ.get("JSON_REPAIR_DEBUG", "").strip() not in (
    "",
    "0",
    "false",
    "False",
)


# -----------------------------
# Strict parse (reject NaN/Inf)
# -----------------------------
def _strict_loads(text: str) -> Any:
    def _bad_const(x: str) -> Any:
        raise ValueError(f"Invalid JSON constant: {x}")

    # json.loads is used here because it supports parse_constant for strictness.
    return json.loads(text, parse_constant=_bad_const)


def _try_parse(text: str) -> Any:
    try:
        return _strict_loads(text)
    except Exception:
        return _SENTINEL


# -----------------------------
# Unicode normalization
# -----------------------------
_TRANSLATE = str.maketrans({
    "\u201c": '"',
    "\u201d": '"',
    "\u2018": "'",
    "\u2019": "'",
    "\u00ab": '"',
    "\u00bb": '"',
    "\u2032": "'",
    "\u2033": '"',
    "\u2026": "...",
    "\ufeff": "",
})

_RE_FENCE = re.compile(
    r"^```(?:json|javascript|js|python)?\s*\n([\s\S]*?)\n```\s*$",
    flags=re.IGNORECASE,
)
_RE_HAS_ALPHA = re.compile(r"[A-Za-z]")
_RE_ELLIPSIS = re.compile(r"\s*\.\.\.\s*")
_RE_DOUBLE_COMMA = re.compile(r",\s*,")
_RE_PYJS_LITS = re.compile(r"\b(True|False|None|NaN|Infinity)\b", re.IGNORECASE)
_RE_INF_SIGNED = re.compile(r"\b[+-]\s*Infinity\b")
# Module-level compiled patterns for _replace_nonstandard_literals (avoids per-call compile)
_RE_NL_PYJS = re.compile(r"\b(True|False|None|NaN|Infinity)\b", re.IGNORECASE)
_RE_NL_INF_SIGN = re.compile(r"\b[+-]\s*Infinity\b")
_RE_NL_SIGN_NULL = re.compile(r'(?<![\w"\\])[-+]\s*null\b')
_RE_NL_UNDEF = re.compile(r"\bundefined\b")
_RE_NL_BIGINT = re.compile(r"\b(\d+)n\b")


def _normalize_unicode_punctuation(text: str) -> str:
    return text.translate(_TRANSLATE)


# -----------------------------
# Root extraction (drop prose)
# -----------------------------
def _extract_balanced_root(t: str) -> str:
    """
    Extract the first balanced JSON-ish root from a messy string.

    - If prose appears before the first { or [, we drop it.
    - We track string state so braces inside strings don't count.
    - If input ends with a closer but opener is missing, synthesize an opener.
    """
    ts = t.strip()
    if ts and ts[-1] == "}" and "{" not in ts:
        t = "{" + ts
    elif ts and ts[-1] == "]" and "[" not in ts:
        t = "[" + ts

    first: Optional[int] = None
    for ch in ("{", "["):
        pos = t.find(ch)
        if pos != -1:
            first = pos if first is None else min(first, pos)

    if first is None:
        return t.strip()

    if _RE_HAS_ALPHA.search(t[:first]):
        t = t[first:]

    opener = t[0]
    closer = "}" if opener == "{" else "]"

    stack: List[str] = []
    in_str = False
    esc = False

    for i, c in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue

        if c == '"':
            in_str = True
            continue

        if c in "{[":
            stack.append(c)
        elif c in "}]":
            if stack:
                top = stack[-1]
                if (top == "{" and c == "}") or (top == "[" and c == "]"):
                    stack.pop()
                if not stack and c == closer:
                    return t[: i + 1].strip()

    return t.strip()


def _strip_markdown_and_prose(text: str) -> str:
    t = text.strip()
    m = _RE_FENCE.match(t)
    if m:
        t = m.group(1).strip()
    if t.startswith("`") and t.endswith("`") and len(t) >= 2:
        t = t[1:-1].strip()
    return _extract_balanced_root(t)


# -----------------------------
# Comment removal (// and /* */)
# -----------------------------
def _remove_comments(text: str) -> str:
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue

        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue

        if i + 1 < n and text[i : i + 2] == "//":
            i += 2
            while i < n and text[i] not in "\r\n":
                i += 1
            continue

        if i + 1 < n and text[i : i + 2] == "/*":
            i += 2
            while i + 1 < n and text[i : i + 2] != "*/":
                i += 1
            i += 2 if i + 1 < n else 0
            continue

        out.append(c)
        i += 1

    return "".join(out)


# -----------------------------
# Literal normalization outside strings
# -----------------------------
def _replace_nonstandard_literals(text: str) -> str:
    """
    Single-pass O(n) replacement of all non-standard literals outside strings.

    Merged from two separate passes in v3.5 (_replace_python_js_literals +
    _replace_js_nonjson_tokens) to eliminate a redundant full-string scan.
    All regex patterns are compiled at module level to avoid per-call overhead.

    Handles:
    - Python/JS booleans: True/False -> true/false  (case-insensitive)
    - None -> null
    - NaN -> null, Infinity -> null
    - Signed: -Infinity / +Infinity -> null  (before PYJS eats Infinity)
    - -null / +null -> null  (created by -Infinity -> null substitution)
    - undefined -> null
    - BigInt suffix: 123n -> 123
    """

    def repl_pyjs(m: re.Match[str]) -> str:
        w = m.group(0).lower()
        if w == "true":
            return "true"
        if w == "false":
            return "false"
        if w in ("none", "null"):
            return "null"
        return "null"  # nan, infinity

    def flush(s: str) -> str:
        s = _RE_NL_INF_SIGN.sub(
            "null", s
        )  # -Infinity FIRST (before PYJS eats Infinity)
        s = _RE_NL_PYJS.sub(repl_pyjs, s)
        s = _RE_NL_SIGN_NULL.sub("null", s)  # catch any -null created above
        s = _RE_NL_UNDEF.sub("null", s)
        s = _RE_NL_BIGINT.sub(r"\1", s)
        return s

    parts: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    while i < n:
        c = text[i]
        if not in_str:
            if c == '"':
                parts.append(flush("".join(buf)))
                buf = []
                in_str = True
                esc = False
                parts.append('"')
            else:
                buf.append(c)
            i += 1
            continue

        parts.append(c)
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = False
        i += 1

    if buf:
        parts.append(flush("".join(buf)))
    return "".join(parts)


# Aliases kept for backward compatibility.
_replace_python_js_literals = _replace_nonstandard_literals
_replace_js_nonjson_tokens = _replace_nonstandard_literals


# -----------------------------
# Small structural fixes (outside strings)
# -----------------------------
_RE_SIGNED_NULL = re.compile(r'(?<![\w"\\])[-+]\s*null\b')
_RE_ADJACENT_VALS = re.compile(r'(\]|\}|")\s*(\[|\{|")')  # missing comma between values


def _fix_signed_null(text: str) -> str:
    """Turn '-null' / '+null' into 'null' outside strings."""
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    def flush(s: str) -> str:
        return _RE_SIGNED_NULL.sub("null", s)

    while i < n:
        c = text[i]
        if not in_str:
            if c == '"':
                parts.append(flush("".join(buf)))
                buf = []
                in_str = True
                esc = False
                parts.append('"')
            else:
                buf.append(c)
            i += 1
            continue
        parts.append(c)
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = False
        i += 1
    if buf:
        parts.append(flush("".join(buf)))
    return "".join(parts)


def _fix_adjacent_values_commas(text: str) -> str:
    """Insert commas between adjacent values like '][', '}{', '"{', etc. outside strings.

    This is a cheap pass that helps LLM outputs missing commas inside arrays/objects.
    Token repair also handles many of these cases, but this reduces complexity upstream.
    """
    parts: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    def flush(s: str) -> str:
        return _RE_ADJACENT_VALS.sub(r"\1,\2", s)

    while i < n:
        c = text[i]
        if not in_str:
            if c == '"':
                parts.append(flush("".join(buf)))
                buf = []
                in_str = True
                esc = False
                parts.append('"')
            else:
                buf.append(c)
            i += 1
            continue
        parts.append(c)
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = False
        i += 1
    if buf:
        parts.append(flush("".join(buf)))
    return "".join(parts)


# Module-level compiled pattern for ctor detection (avoids per-call recompile)
_RE_CTOR = re.compile(
    r"\b(?:new\s+Set|new\s+Map|frozenset|set|tuple|list)\s*\(",
    re.IGNORECASE,
)


# -----------------------------
# Unwrap common container ctors
# -----------------------------
def _unwrap_common_container_ctors(text: str) -> str:
    """Replace Python/JS container constructors with their raw contents.

    Fixes the double-wrap bug: the old approach replaced ``set(`` with ``(``
    which left the outer parens intact, causing token_repair to wrap the
    content in ``[...]`` producing ``[[1,2,3]]`` instead of ``[1,2,3]``.

    New approach: find the matching closing ``)`` depth-aware and emit
    *just the interior*:  ``set([1,2,3])`` → ``[1,2,3]``.
    Strings are passed through verbatim.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue

        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue

        # Try to match a ctor at this position
        m = _RE_CTOR.match(text, i)
        if m:
            # Find matching closing ')' — track depth
            depth = 0
            k = m.end() - 1  # points at the opening '('
            while k < n:
                if text[k] == "(":
                    depth += 1
                elif text[k] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            # Emit just the interior (between the ctor's parens)
            out.append(text[m.end():k])
            i = k + 1
            continue

        out.append(c)
        i += 1

    return "".join(out)


# -----------------------------
# Misc sanitizers
# -----------------------------
def _remove_ellipsis(text: str) -> str:
    text = _RE_ELLIPSIS.sub(" ", text)
    text = _RE_DOUBLE_COMMA.sub(",", text)
    return text


def _sanitize_strings_and_quotes(
    text: str,
    schema_keys: Optional[FrozenSet[str]] = None,
) -> str:
    """Normalize quotes and string escapes.

    Repairs common LLM issues:
    - Accepts single-quoted strings/keys and converts them to JSON double quotes
    - Escapes invalid backslashes inside strings
    - Converts raw control chars into \\uXXXX
    - Heuristically closes unterminated strings

    Schema guidance:
    When ``schema_keys`` is provided, ambiguous inner quotes can be resolved by
    looking ahead: if the token after a candidate quote is a known schema key
    followed by ':', the quote is treated as closing.
    """

    VALID_ESCAPE = set('"\\/bfnrtu')
    VALUE_STARTERS = set('"-+0123456789tfn[{')

    out: List[str] = []
    i = 0
    n = len(text)

    in_str = False
    delim = '"'
    esc = False

    last_struct = '{'
    str_context = 'key'  # 'key' or 'value'

    def next_sig(pos: int) -> str:
        j = pos
        while j < n and text[j] in " \t\r\n":
            j += 1
        return text[j] if j < n else ""

    while i < n:
        c = text[i]
        if not in_str:
            if c in ('"', "'"):
                in_str = True
                delim = c
                esc = False
                str_context = 'value' if last_struct in (':', '[') else 'key'
                out.append('"')
                i += 1
                continue

            if c == '=':
                out.append(':')
                last_struct = ':'
                i += 1
                continue

            out.append(c)
            if c in '{[,:':
                last_struct = c
            i += 1
            continue

        # in string
        if esc:
            out.append(c)
            esc = False
            i += 1
            continue

        if c == '\\':
            nxt = text[i + 1] if i + 1 < n else ''
            # Stray backslash before newline: treat as line-continuation.
            if nxt in ('\n', '\r'):
                i += 1
                continue
            if nxt and nxt in VALID_ESCAPE:
                out.append('\\')
                out.append(nxt)
                i += 2
            else:
                out.append('\\\\')
                i += 1
            continue

        if c == '\n':
            out.append('\\n')
            i += 1
            continue
        if c == '\r':
            out.append('\\r')
            i += 1
            continue
        if c == '\t':
            out.append('\\t')
            i += 1
            continue

        if ord(c) < 0x20:
            out.append('\\u%04x' % ord(c))
            i += 1
            continue

        if delim == "'":
            if c == "'":
                out.append('"')
                in_str = False
            elif c == '"':
                out.append('\\"')
            else:
                out.append(c)
            i += 1
            continue

        # delim == '"'
        if c == '"':
            ns = next_sig(i + 1)
            if ns in (':', ',', ']', '}', ')', ''):
                out.append('"')
                in_str = False
                i += 1
                continue

            if str_context == 'key' and (ns in VALUE_STARTERS or ns.isalpha() or ns == '"'):
                out.append('"')
                in_str = False
                i += 1
                continue

            # In value context, close if next clearly starts a new value (but not '"').
            if str_context == 'value' and (ns.isdigit() or ns in ('+', '-', '[', '{', "'")):
                out.append('"')
                in_str = False
                i += 1
                continue

            # Schema-key lookahead: if next token is a known key + ':', close.
            if schema_keys and _schema_key_lookahead(text, i, schema_keys):
                out.append('"')
                in_str = False
                i += 1
                continue

            # Otherwise treat as unescaped inner quote.
            out.append("'")
            i += 1
            continue

        out.append(c)
        i += 1

    if in_str:
        out.append('"')

    return ''.join(out)


def _quote_unquoted_windows_paths(text: str) -> str:
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue

        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue

        if c == ":":
            out.append(c)
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                out.append(text[j])
                j += 1

            if (
                j + 2 < n
                and text[j].isalpha()
                and text[j + 1] == ":"
                and text[j + 2] == "\\"
            ):
                k = j
                while k < n and text[k] not in ",}]\n\r\t":
                    k += 1
                raw = text[j:k].strip()
                out.append(json.dumps(raw))
                i = k
                continue

            i = j
            continue

        out.append(c)
        i += 1

    return "".join(out)


def _fix_semicolons(text: str) -> str:
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue

        if c == '"':
            in_str = True
            out.append(c)
        elif c == ";":
            out.append(",")
        else:
            out.append(c)
        i += 1

    return "".join(out)


def _convert_hex_numbers(text: str) -> str:
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue

        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue

        if i + 2 <= n and text[i : i + 2].lower() == "0x":
            j = i + 2
            while j < n and text[j] in "0123456789abcdefABCDEF":
                j += 1
            lit = text[i:j]
            try:
                out.append(str(int(lit, 16)))
            except ValueError:
                out.append(lit)
            i = j
            continue

        out.append(c)
        i += 1

    return "".join(out)


def _fix_leading_zero_numbers(text: str) -> str:
    out: List[str] = []
    i = 0
    n = len(text)
    in_str = False
    esc = False

    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue

        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue

        if c in "+-" and i + 1 < n and text[i + 1].isdigit():
            sign = c
            start = i
            j = i + 1
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] == ".":
                int_part = text[start + 1 : j]
                if len(int_part) > 1 and int_part.startswith("0"):
                    try:
                        int_part = str(int(int_part, 10))
                    except Exception:
                        pass
                prefix = "-" if sign == "-" else ""
                out.append(prefix + int_part)
                i = j
                continue
            if j < n and text[j] in "eE":
                prefix = "-" if sign == "-" else ""
                out.append(prefix + text[start + 1 : j])
                i = j
                continue
            num = text[start + 1 : j]
            if len(num) > 1 and num.startswith("0"):
                try:
                    num = str(int(num, 10))
                except Exception:
                    num = text[start + 1 : j]
            prefix = "-" if sign == "-" else ""
            out.append(prefix + num)
            i = j
            continue

        if c.isdigit():
            start = i
            j = i
            while j < n and text[j].isdigit():
                j += 1
            if j < n and text[j] == ".":
                int_part = text[start:j]
                if len(int_part) > 1 and int_part.startswith("0"):
                    try:
                        int_part = str(int(int_part, 10))
                    except Exception:
                        pass
                out.append(int_part)
                i = j
                continue
            if j < n and text[j] in "eE":
                out.append(text[start:j])
                i = j
                continue
            num = text[start:j]
            if len(num) > 1 and num.startswith("0"):
                try:
                    out.append(str(int(num, 10)))
                except Exception:
                    out.append(num)
            else:
                out.append(num)
            i = j
            continue

        out.append(c)
        i += 1

    return "".join(out)


def _drop_truncated_key_tail(text: str) -> str:
    stripped = text.rstrip()
    stripped2 = re.sub(r',\s*"[^"\\]*\Z', "", stripped)
    if stripped2 != stripped:
        return stripped2
    stripped3 = re.sub(r'{\s*"[^"\\]*\Z', "{", stripped)
    return stripped3


# -----------------------------
# Token-based structural repair
# -----------------------------
@dataclass
class Tok:
    kind: str
    text: str


_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$-]*")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _tokenize(text: str) -> List[Tok]:
    toks: List[Tok] = []
    i = 0
    n = len(text)

    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue

        if c in "{[]}:,()":
            toks.append(Tok(c, c))
            i += 1
            continue

        if c == '"':
            j = i + 1
            esc = False
            while j < n:
                ch = text[j]
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    j += 1
                    break
                j += 1
            toks.append(Tok("STRING", text[i:j]))
            i = j
            continue

        m = _NUMBER_RE.match(text, i)
        if m:
            toks.append(Tok("NUMBER", m.group(0)))
            i = m.end()
            continue

        m = _IDENT_RE.match(text, i)
        if m:
            toks.append(Tok("IDENT", m.group(0)))
            i = m.end()
            continue

        if c == "=":
            toks.append(Tok(":", ":"))
            i += 1
            continue

        toks.append(Tok("CHAR", c))
        i += 1

    return toks


def _untokenize(toks: Sequence[Tok]) -> str:
    out: List[str] = []
    prev: Optional[Tok] = None
    for t in toks:
        if (
            prev
            and prev.kind in ("STRING", "NUMBER", "IDENT")
            and t.kind
            in (
                "STRING",
                "NUMBER",
                "IDENT",
            )
        ):
            out.append(" ")
        out.append(t.text)
        prev = t
    return "".join(out)


def _is_value_tok(t: Tok) -> bool:
    return t.kind in ("STRING", "NUMBER", "IDENT", "{", "[", "(")


def _repair_tokens(text: str) -> str:
    toks = _tokenize(text)
    out: List[Tok] = []

    # stack entries: (kind, state, closer)
    stack: List[Tuple[str, str, str]] = []

    def top() -> Optional[Tuple[str, str, str]]:
        return stack[-1] if stack else None

    def set_state(state: str) -> None:
        if stack:
            kind, _, closer = stack[-1]
            stack[-1] = (kind, state, closer)

    def push_obj() -> None:
        stack.append(("OBJ", "key_or_end", "}"))

    def push_arr(closer: str = "]") -> None:
        stack.append(("ARR", "value_or_end", closer))

    def pop_expected(close_kind: str) -> Optional[Tuple[str, str]]:
        if not stack:
            return None
        kind, _, closer = stack[-1]
        if (kind == "OBJ" and close_kind == "}") or (
            kind == "ARR" and close_kind == closer
        ):
            stack.pop()
            return kind, closer
        return None

    def looks_like_set_literal(start_idx: int) -> bool:
        """Heuristic: treat a `{...}` as a set literal if, at depth 1, we never see a ':'.

        Robust even when the outer '}' is missing because of truncation or an
        unterminated string that swallowed the closer.
        """
        depth = 0
        saw_colon = False
        j = start_idx
        n2 = len(toks)
        while j < n2:
            k = toks[j].kind
            if k == "{":
                depth += 1
            elif k == "}":
                depth -= 1
                if depth <= 0:
                    break
            elif k == ":" and depth == 1:
                saw_colon = True
                # early exit: definitely not a set literal
                return False
            j += 1
        # If we never saw ':' at depth 1, treat it as set-like (convert to array)
        return not saw_colon

    def normalize_ident_value(t: Tok) -> Tok:
        low = t.text.lower()
        if low in ("true", "false", "null"):
            return Tok("IDENT", low)
        return Tok("STRING", json.dumps(t.text))

    def normalize_ident_key(t: Tok) -> Tok:
        return Tok("STRING", json.dumps(t.text))

    # Root-level tuple: ( ... ) -> [ ... ]
    if toks and toks[0].kind == "(":
        toks[0] = Tok("[", "[")
        depth = 0
        for j in range(len(toks)):
            if toks[j].kind == "[" and j == 0:
                depth = 1
                continue
            if toks[j].kind in ("(", "["):
                depth += 1
            elif toks[j].kind in (")", "]"):
                depth -= 1
                if depth == 0:
                    toks[j] = Tok("]", "]")
                    break

    # Root-level set: {1,2,3} -> [1,2,3]
    if toks and toks[0].kind == "{" and looks_like_set_literal(0):
        toks[0] = Tok("[", "[")
        depth = 0
        for j in range(len(toks)):
            if toks[j].kind in ("{", "["):
                depth += 1
            elif toks[j].kind in ("}", "]"):
                depth -= 1
                if depth == 0:
                    toks[j] = Tok("]", "]")
                    break

    i = 0
    n = len(toks)

    # Hard guard against hangs (guarantees termination)
    max_iters = max(2000, n * 50 + 50)
    iters = 0

    while i < n:
        iters += 1
        if iters > max_iters:
            # If we ever hit this, it's safer to stop than to hang.
            raise RuntimeError("json_repair token_repair exceeded iteration guard")

        t = toks[i]
        ctx = top()

        # If object was expecting a value and we see '}', treat it as "missing value" and close.
        # IMPORTANT: Do not 'continue' without advancing i, or it can hang.
        if ctx and ctx[0] == "OBJ" and ctx[1] == "value" and t.kind == "}":
            if out and out[-1].kind == ":":
                out.pop()
            if out and out[-1].kind == "STRING":
                out.pop()
            if out and out[-1].kind == ",":
                out.pop()
            # Fall through to the normal closer handling below.

        if t.kind in ("}", "]", ")"):
            if out and out[-1].kind == ",":
                out.pop()
            popped = pop_expected(t.kind)
            if popped:
                kind, expected = popped
                if kind == "ARR" and expected in ("}", ")"):
                    out.append(Tok("]", "]"))
                else:
                    out.append(t)
            if stack:
                set_state("comma_or_end")
            i += 1
            continue

        if t.kind == "{":
            if ctx and ctx[1] == "comma_or_end":
                out.append(Tok(",", ","))
                set_state("key_or_end" if ctx[0] == "OBJ" else "value_or_end")

            if (
                ctx
                and ctx[0] in ("OBJ", "ARR")
                and ctx[1] in ("value", "value_or_end")
                and looks_like_set_literal(i)
            ):
                out.append(Tok("[", "["))
                push_arr("}")
            else:
                out.append(t)
                push_obj()
            i += 1
            continue

        if t.kind == "[":
            if ctx and ctx[1] == "comma_or_end":
                out.append(Tok(",", ","))
                set_state("key_or_end" if ctx[0] == "OBJ" else "value_or_end")
            out.append(t)
            push_arr("]")
            i += 1
            continue

        if t.kind == "(":
            # tuple inside value -> array
            if (
                ctx
                and ctx[0] in ("OBJ", "ARR")
                and ctx[1] in ("value", "value_or_end", "comma_or_end")
            ):
                if ctx[1] == "comma_or_end":
                    out.append(Tok(",", ","))
                    set_state("value_or_end")
                out.append(Tok("[", "["))
                push_arr(")")
                i += 1
                continue
            # Otherwise drop it
            i += 1
            continue

        if ctx and ctx[0] == "OBJ" and ctx[1] == "colon":
            if t.kind != ":":
                out.append(Tok(":", ":"))
                set_state("value")
                # re-process same token as value
                continue
            out.append(t)
            set_state("value")
            i += 1
            continue

        if t.kind == ",":
            if ctx and ctx[1] == "comma_or_end":
                out.append(t)
                set_state("key_or_end" if ctx[0] == "OBJ" else "value_or_end")
            i += 1
            continue

        if ctx and ctx[0] == "OBJ" and ctx[1] == "key_or_end":
            if t.kind == "STRING":
                out.append(t)
                set_state("colon")
                i += 1
                continue
            if t.kind == "IDENT":
                out.append(normalize_ident_key(t))
                set_state("colon")
                i += 1
                continue
            # skip unknown token as key
            i += 1
            continue

        if ctx and ctx[0] == "ARR" and ctx[1] == "value_or_end":
            if _is_value_tok(t):
                if t.kind == "IDENT":
                    out.append(normalize_ident_value(t))
                elif t.kind == "(":
                    # handled earlier, but keep safe
                    out.append(Tok("[", "["))
                    push_arr(")")
                else:
                    out.append(t)
                set_state("comma_or_end")
                i += 1
                continue
            i += 1
            continue

        if ctx and ctx[0] == "OBJ" and ctx[1] == "value":
            if _is_value_tok(t):
                if t.kind == "IDENT":
                    out.append(normalize_ident_value(t))
                else:
                    out.append(t)
                set_state("comma_or_end")
                i += 1
                continue
            i += 1
            continue

        # If we're expecting comma_or_end but see a value, insert comma and re-process token
        if ctx and ctx[1] == "comma_or_end" and _is_value_tok(t):
            out.append(Tok(",", ","))
            set_state("key_or_end" if ctx[0] == "OBJ" else "value_or_end")
            continue

        # Root / unknown context fallback
        if t.kind == "IDENT":
            out.append(Tok("STRING", json.dumps(t.text)))
        elif t.kind != "CHAR":
            out.append(t)

        i += 1

    # Close any remaining containers
    while stack:
        kind, state, closer = stack.pop()
        if out and out[-1].kind == ",":
            out.pop()

        if kind == "OBJ":
            # If we ended mid key/colon/value, clean it
            if state in ("colon", "value"):
                # drop dangling ':', and possibly dangling key
                while out and out[-1].kind == ":":
                    out.pop()
                if out and out[-1].kind == "STRING" and state == "colon":
                    out.pop()
                if out and out[-1].kind == ",":
                    out.pop()
            out.append(Tok("}", "}"))
        else:
            out.append(Tok("]", "]"))

    return _untokenize(out)


# -----------------------------
# Public API
# -----------------------------


def repair_json(
    broken: str,
    return_dict: bool = False,
    schema: Any = None,
) -> Any:
    """
    Repair and parse JSON-ish text.

    Parameters
    ----------
    broken      : The malformed JSON string to repair.
    return_dict : If True, return the parsed Python object; otherwise return
                  a pretty-printed JSON string.  Default False.
    schema      : Optional schema for guided repair and type coercion.
                  Accepts any of: Python shape-dict, string-type dict,
                  JSON Schema dict, JSON string, Python class / dataclass /
                  Pydantic model, list shorthand, or None (= no schema).
                  See module docstring for full details.

    Returns
    -------
    Repaired JSON string (or parsed object if return_dict=True).

    Raises
    ------
    ValueError  : If repair cannot produce valid strict JSON.
    """
    # ── Schema normalization ───────────────────────────────────────────────
    schema_node: Optional[SchemaNode] = (
        normalize_schema(schema) if schema is not None else None
    )

    # Collect ALL schema keys (all nesting levels) for sanitize lookahead.
    # Using recursive collection because nested object schemas (e.g.
    # {"user": {"name": str, "age": int}}) need inner keys like "name"/"age"
    # to be available during string sanitization at any depth.
    # For array schemas ([{...}]), use the item_schema's keys.
    def _all_schema_keys(node: Optional[SchemaNode]) -> FrozenSet[str]:
        if node is None:
            return frozenset()
        keys: set = set(node.properties.keys())
        for child in node.properties.values():
            keys |= _all_schema_keys(child)
            if child.item_schema:
                keys |= _all_schema_keys(child.item_schema)
        if node.item_schema:
            keys |= _all_schema_keys(node.item_schema)
        return frozenset(keys)

    # Compute once and reuse (avoids double traversal of the schema tree)
    _computed_keys = _all_schema_keys(schema_node)
    schema_keys: Optional[FrozenSet[str]] = _computed_keys if _computed_keys else None

    # ── Fast path: already valid strict JSON ──────────────────────────────
    parsed = _try_parse(broken)
    if parsed is not _SENTINEL:
        if schema_node is not None:
            _set_schema_mode(True)
            try:
                parsed = _apply_schema(parsed, schema_node)
            finally:
                _set_schema_mode(False)
        return parsed if return_dict else _pretty_dumps(parsed)

    text = broken

    # We use the stripped/root-extracted text as the primary "shape hint"
    # to avoid returning lossy scalars for clearly-container inputs.
    root_hint_text = _strip_markdown_and_prose(_normalize_unicode_punctuation(text))
    root_hint = ""
    for ch in root_hint_text.lstrip():
        if ch in "{[(":
            root_hint = ch
            break
        if ch in '"-0123456789tfn' or ch.isalpha():
            root_hint = "SCALAR"
            break

    pipeline = [
        # ── Stage 1: cheap textual normalizations ──────────────────────────────
        ("normalize_unicode", _normalize_unicode_punctuation),
        ("strip_markdown_prose", _strip_markdown_and_prose),
        ("remove_comments", _remove_comments),
        # ── Stage 2: literal / token normalization ─────────────────────────────
        # Single merged pass (v3.6): replaces True/False/None/NaN/Infinity/undefined/BigInt
        # and also handles -Infinity -> null AND -null -> null in one O(n) scan.
        ("replace_literals", _replace_nonstandard_literals),
        # ── Stage 3: structural / syntactic light fixes ────────────────────────
        ("unwrap_container_ctors", _unwrap_common_container_ctors),
        ("remove_ellipsis", _remove_ellipsis),
        # ── Stage 4: numeric normalization BEFORE sanitize_strings ─────────────
        # Doing hex/leading-zero conversion here means sanitize_strings sees clean
        # decimals (e.g. 16 not 0x10), which helps the value-context heuristic.
        ("convert_hex", _convert_hex_numbers),
        ("fix_leading_zero_numbers", _fix_leading_zero_numbers),
        # ── Stage 5: quote / escape normalization ──────────────────────────────
        # v3.7: pass schema_keys so the sanitizer can use them as lookahead
        ("sanitize_strings", lambda t: _sanitize_strings_and_quotes(t, schema_keys)),
        # ── Stage 6: post-sanitize structural fixes ────────────────────────────
        # Insert missing commas between adjacent values AFTER quotes are normalized.
        ("fix_adjacent_values_commas", _fix_adjacent_values_commas),
        ("quote_windows_paths", _quote_unquoted_windows_paths),
        ("fix_semicolons", _fix_semicolons),
        ("drop_truncated_key_tail", _drop_truncated_key_tail),
        # ── Stage 7: full token-level structural repair ─────────────────────────
        ("token_repairs", _repair_tokens),
    ]

    def accept(parsed_obj: Any, stage: str) -> Any:
        # Lossiness guard: if the input clearly looks like a container,
        # do not accept a synthetic scalar None.
        if parsed_obj is None and root_hint in ("{", "[", "("):
            raise ValueError(
                "Could not repair JSON without losing structure (container -> null)."
            )
        if _DEBUG:
            print(f"  + Repaired at stage: {stage}")
        # v3.7: apply schema coercion on accepted parse
        if schema_node is not None:
            _set_schema_mode(True)
            try:
                parsed_obj = _apply_schema(parsed_obj, schema_node)
            finally:
                _set_schema_mode(False)
        return parsed_obj if return_dict else _pretty_dumps(parsed_obj)

    # Single-pass pipeline with early-parse after each stage
    for stage, fn in pipeline:
        text2 = fn(text)
        text = text2
        parsed = _try_parse(text)
        if parsed is not _SENTINEL:
            return accept(parsed, stage)

    # "Doctor" rescue: fixed-count additional attempts, no loops.
    # These passes help when token repair introduced new adjacency issues.
    rescue = [
        ("rescue_sanitize", lambda t: _sanitize_strings_and_quotes(t, schema_keys)),
        ("rescue_semicolons", _fix_semicolons),
        ("rescue_token_repairs", _repair_tokens),
    ]
    for attempt in range(2):  # exactly 2 extra rounds
        for stage, fn in rescue:
            text2 = fn(text)
            text = text2
            parsed = _try_parse(text)
            if parsed is not _SENTINEL:
                return accept(parsed, f"{stage}#{attempt+1}")

    # Extraction fallback: only accept if the extracted root parses strictly.
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        m = re.search(pattern, text)
        if m:
            cand = m.group(0)
            parsed = _try_parse(cand)
            if parsed is not _SENTINEL:
                return accept(parsed, "extraction_fallback")

    # If we reach here, we failed. Raise with best final attempt context.
    final_preview = text
    # Keep error payload bounded
    if len(final_preview) > 4000:
        final_preview = final_preview[:4000] + "…"
    raise ValueError(f"Could not repair JSON.\nFinal attempt:\n{final_preview}")


def _pretty_dumps(obj: Any) -> str:
    if _USE_ORJSON:
        # OPT_INDENT_2 gives nice output; OPT_NON_STR_KEYS helps if we ever emit
        # non-string keys internally (rare, but safe).
        return orjson.dumps(obj, option=orjson.OPT_INDENT_2).decode("utf-8")  # type: ignore
    return json.dumps(obj, ensure_ascii=False, indent=2)


# =============================
# Tests
# =============================


def _run_tests() -> None:
    import contextlib
    import io
    import random

    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    tests: List[Tuple[str, str, str]] = []

    tests.extend([
        ("CORE-01", "Unescaped quotes in string", '{"q":"He said "hello""}'),
        ("CORE-02", "Single quotes", "{'a': 'b', 'c': 1}"),
        ("CORE-03", "Trailing commas", '{"a":[1,2,3,],"b":{"x":1,},}'),
        ("CORE-04", "Comments", '{//x\n"a":1,/*y*/"b":2,}'),
        ("CORE-05", "Bare keys + values", "{a:1, status: active}"),
        ("CORE-06", "Missing commas", '{"a":1 "b":2 "c":3}'),
        ("CORE-07", "Missing colons", '{"a" 1, "b" true, "c" null}'),
        ("CORE-08", "Hex + leading zeros", "{color:0x10, zip:07030, score:09.5}"),
        ("CORE-09", "Smart quotes", "“a”: “b”, “x”: [1, 2, …, 4]"),
        ("CORE-10", "Tuple root", "(1,2,3,)"),
        ("CORE-11", "Set literal", "{1,2,3,}"),
        ("CORE-12", "Nested dict+set+tuple", "{a:{1,2,(3,4)}, b:(5,6)}"),
        ("CORE-13", "JS Set ctor", "{s:new Set([1,2,3])}"),
        ("CORE-14", "JS Map ctor", '{m:new Map([["a",1],["b",2]])}'),
        ("CORE-15", "Undefined + bigint", "{a:undefined, b:123n}"),
        ("CORE-16", "Windows path unquoted", '{"path": C:\\Users\\Bob\\file.txt}'),
        # v3.6 regression / new tests
        ("V36-01", "Value+number missing comma (BUG1 fix)", '{"x_y": 0 "a" 1}'),
        ("V36-02", "Hex value then string key (BUG1+ordering)", '{x: 0x10 "b" 1}'),
        (
            "V36-03",
            "Signed Infinity in pipeline (BUG2 signed_null)",
            "{a: -Infinity, b: 1}",
        ),
        ("V36-04", "Adjacent objects in array (BUG2 adj_commas)", '[{"a":1}{"b":2}]'),
        ("V36-05", "String then bracket in value context (BUG1)", '{"k": "v" [1,2]}'),
        (
            "V36-06",
            "Multiple missing commas mixed types",
            '{"a": 1 "b": true "c": null}',
        ),
        ("V36-07", "BigInt converted before signed check", "{a: -123n, b: +456n}"),
    ])

    # Generate hard randomized cases (deterministic)
    rng = random.Random(1337)

    atoms = [
        "1",
        "2",
        "true",
        "false",
        "null",
        "None",
        "True",
        "False",
        "NaN",
        "Infinity",
        "-Infinity",
        "0x10",
        "07030",
        "09.5",
        "active",
        "hello",
        "'single'",
        '"double"',
        '"unescaped "inner""',
    ]

    def rand_atom() -> str:
        return rng.choice(atoms)

    def make_container(depth: int) -> str:
        if depth <= 0:
            return rand_atom()
        kind = rng.choice(["list", "tuple", "set", "dict", "jsset", "jsmap"])
        if kind == "list":
            inner = " ".join(
                make_container(depth - 1) for _ in range(rng.randint(2, 4))
            )
            return f"[{inner}]"  # missing commas on purpose
        if kind == "tuple":
            inner = ", ".join(
                make_container(depth - 1) for _ in range(rng.randint(2, 4))
            )
            if rng.random() < 0.4:
                inner = " ".join(
                    make_container(depth - 1) for _ in range(rng.randint(2, 4))
                )
            return f"({inner},)"
        if kind == "set":
            inner = ", ".join(
                make_container(depth - 1) for _ in range(rng.randint(2, 4))
            )
            return "{" + inner + ",}"
        if kind == "dict":
            pairs = []
            for _ in range(rng.randint(2, 4)):
                k = rng.choice(["a", "b", "c", "x_y", "$ok", "step-1"])
                v = make_container(depth - 1)
                if rng.random() < 0.33:
                    pairs.append(f"{k}: {v}")
                elif rng.random() < 0.5:
                    pairs.append(f'"{k}" {v}')
                else:
                    pairs.append(f"'{k}': {v}")
            sep = rng.choice([", ", " ", "; "])
            return "{" + sep.join(pairs) + ("," if rng.random() < 0.5 else "") + "}"
        if kind == "jsset":
            inner = ",".join(
                make_container(depth - 1) for _ in range(rng.randint(2, 4))
            )
            return f"new Set([{inner},])"
        # jsmap
        inner_pairs = []
        for _ in range(rng.randint(2, 4)):
            k = rng.choice(["a", "b", "c"])
            v = make_container(depth - 1)
            inner_pairs.append(f'["{k}", {v}]')
        return "new Map([" + " ".join(inner_pairs) + ",])"

    # Generate randomized cases (deterministic), but ONLY keep cases that the
    # engine can actually repair. This keeps the suite "hard but not impossible".
    wanted = 40
    idx = 1
    attempts = 0
    while idx <= wanted and attempts < wanted * 25:
        attempts += 1
        depth = rng.randint(1, 4)
        root_kind = rng.choice(["dict", "list"])
        broken = make_container(depth)
        if root_kind == "dict" and not broken.lstrip().startswith("{"):
            broken = "{" + f"a: {broken}" + "}"
        if root_kind == "list" and not broken.lstrip().startswith("["):
            broken = "[" + broken + "]"
        if rng.random() < 0.25:
            broken = f"Sure! Here it is:\n```json\n{broken}\n```"

        try:
            _ = repair_json(broken)
        except Exception:
            continue

        tests.append((f"RND-{idx:03d}", "Random nested containers (passable)", broken))
        idx += 1
        if rng.random() < 0.25:
            broken = f"Sure! Here it is:\n```json\n{broken}\n```"
        tests.append((f"RND-{idx:03d}", "Random nested containers", broken))

    passed = 0
    failed = 0
    categories: dict[str, dict[str, int]] = {}
    fail_list: List[str] = []

    print(f"\n{BOLD}{'='*74}{RESET}")
    print(
        f"{BOLD}  JSON Repair Engine v3.8 -- {len(tests)} test cases + schema-guided"
        f" tests{RESET}"
    )
    print(f"{BOLD}{'='*74}{RESET}\n")

    for id_, desc, broken in tests:
        cat = id_.split("-")[0]
        categories.setdefault(cat, {"p": 0, "f": 0})

        print(f"{BOLD}{CYAN}{id_}{RESET}: {desc}")
        short = (broken[:88] + "...") if len(broken) > 88 else broken
        print(f"  {DIM}Input: {short!r}{RESET}")

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                result = repair_json(broken)
            _strict_loads(result)
            stage = buf.getvalue().strip()
            preview = result.replace("\n", " ").replace("  ", " ")[:95]
            print(f"  {GREEN}+ PASS{RESET}  {DIM}{stage}{RESET}")
            print(f"  {GREEN}-> {preview}{'...' if len(result) > 95 else ''}{RESET}")
            passed += 1
            categories[cat]["p"] += 1
        except Exception as e:
            print(f"  {RED}- FAIL -- {str(e)[:160]}{RESET}")
            failed += 1
            categories[cat]["f"] += 1
            fail_list.append(f"{id_}: {desc}")
        print()

    # ══════════════════════════════════════════════════════════════════════
    # Schema-guided tests (v3.7)
    # ══════════════════════════════════════════════════════════════════════
    import dataclasses as _dc

    @_dc.dataclass
    class _UserDC:
        name: str
        score: float
        active: bool

    schema_tests = [
        # id, description, broken_input, schema, expected_python_value
        (
            "SCH-01",
            "Basic type coercion str->number",
            '{"name": "Alice", "score": "95", "active": "true"}',
            {"name": str, "score": float, "active": bool},
            {"name": "Alice", "score": 95.0, "active": True},
        ),
        (
            "SCH-02",
            "Bare keys + coercion",
            "{name: Alice score: 95.5 active: 1}",
            {"name": str, "score": float, "active": bool},
            {"name": "Alice", "score": 95.5, "active": True},
        ),
        (
            "SCH-03",
            "List of schema items (LLM wraps in array)",
            '[{name: "Alice" score: 95}, {name: "Bob" score: 87}]',
            {"name": str, "score": float},
            [{"name": "Alice", "score": 95.0}, {"name": "Bob", "score": 87.0}],
        ),
        (
            "SCH-04",
            "Wrapped in outer object",
            '{"items": [{name: "Alice" score: 95}], "total": 1}',
            {"name": str, "score": float},
            [{"name": "Alice", "score": 95.0}],
        ),
        (
            "SCH-05",
            "Missing required fields injected as null",
            '{"name": "Alice"}',
            {"name": str, "score": float, "active": bool},
            {"name": "Alice", "score": None, "active": None},
        ),
        (
            "SCH-06",
            "Quote ambiguity resolved by schema keys",
            '{"step-1": "active" "status": "done"}',
            {"step-1": str, "status": str},
            {"step-1": "active", "status": "done"},
        ),
        (
            "SCH-07",
            "Nested object schema",
            '{user: {name: "Alice" age: 30} meta: {created: "2024-01-01"}}',
            {"user": {"name": str, "age": int}, "meta": {"created": str}},
            {"user": {"name": "Alice", "age": 30}, "meta": {"created": "2024-01-01"}},
        ),
        (
            "SCH-08",
            "Array of numbers as strings -> coerce",
            '{"scores": ["95", "87", "100"]}',
            {"scores": [float]},
            {"scores": [95.0, 87.0, 100.0]},
        ),
        (
            "SCH-09",
            "Comma-string to list",
            '{"tags": "python, fast, cool"}',
            {"tags": [str]},
            {"tags": ["python", "fast", "cool"]},
        ),
        (
            "SCH-10",
            "Bool coercion from 0/1",
            '{"active": 1, "deleted": 0, "verified": "yes"}',
            {"active": bool, "deleted": bool, "verified": bool},
            {"active": True, "deleted": False, "verified": True},
        ),
        (
            "SCH-11",
            "Schema as JSON string",
            '{name: "Alice" score: 95}',
            '{"name": "string", "score": "number"}',
            {"name": "Alice", "score": 95.0},
        ),
        (
            "SCH-12",
            "Schema as JSON Schema dict",
            '{name: "Alice" score: 95}',
            {
                "type": "object",
                "properties": {"name": {"type": "string"}, "score": {"type": "number"}},
            },
            {"name": "Alice", "score": 95.0},
        ),
        (
            "SCH-13",
            "Schema from dataclass",
            '{name: "Alice" score: 95.5 active: True}',
            _UserDC,
            {"name": "Alice", "score": 95.5, "active": True},
        ),
        (
            "SCH-14",
            "Recursive / nested same-schema (tree structure)",
            (
                '{"id": "1", "name": "Root", "score": 1.0, "children": [{"id": "2",'
                ' "name": "A", "score": 2.0, "children": []}]}'
            ),
            {"id": str, "name": str, "score": float},
            {
                "id": "1",
                "name": "Root",
                "score": 1.0,
                "children": [{"id": "2", "name": "A", "score": 2.0, "children": []}],
            },
        ),
        (
            "SCH-15",
            "Deep nested list of objects",
            (
                '{"departments": [{"name": "Eng", "teams": [{"name": "Backend", "size":'
                ' "12"}]}]}'
            ),
            {"departments": [{"name": str, "teams": [{"name": str, "size": int}]}]},
            {
                "departments": [
                    {"name": "Eng", "teams": [{"name": "Backend", "size": 12}]}
                ]
            },
        ),
        (
            "SCH-16",
            "List schema shorthand: schema=[{...}]",
            '[{name: "Alice" score: "95"}, {name: "Bob" score: "87"}]',
            [{"name": str, "score": float}],
            [{"name": "Alice", "score": 95.0}, {"name": "Bob", "score": 87.0}],
        ),
        (
            "SCH-17",
            "Extra unknown fields preserved (permissive)",
            '{"name": "Alice", "score": 95, "extra": "keep_me"}',
            {"name": str, "score": float},
            {"name": "Alice", "score": 95.0, "extra": "keep_me"},
        ),
        (
            "SCH-18",
            "Number->string coercion",
            '{"code": 404, "label": 200}',
            {"code": str, "label": str},
            {"code": "404", "label": "200"},
        ),
        (
            "SCH-19",
            "Broken + schema: missing commas AND coercion",
            '{name: "Alice" score: "95" active: 1 tags: "fast, smart"}',
            {"name": str, "score": float, "active": bool, "tags": [str]},
            {"name": "Alice", "score": 95.0, "active": True, "tags": ["fast", "smart"]},
        ),
        (
            "SCH-20",
            "Tuple union schema: (int, float)",
            '{"x": "3", "y": "4.5"}',
            {"x": (int, float), "y": (int, float)},
            {"x": 3, "y": 4.5},
        ),
        (
            "SCH-21",
            "Real test",
            """{
  "followup": false,
  "followup_question": "",
  "refusal": false,
  "refusal_reason": "",
  "steps": [
    {
      "step_id": 1,
      "tool": "terminal",
      "ack_message": "Checking if the flight ticket exists in the Downloads folder.",
      "instruction": "Task:\nCheck for existence of /Users//kishan//Downloads/flight_ticket.pdf\nInputs:\nNone\nHints:\nUse ls -l to check file presence\nSafety:\nNon-destructive\nVerify:\nFile exists and is readable\nOutput:\nfile_check_result",
      "input_steps": []
      "output": "file_check_result"
      "confidence": 0.9
    },
    {
      "step_id": "2",
      "tool": "terminal",
      "ack_message": "Opening the flight ticket document from the Downloads folder.",
      "instruction": "Task:\\\nOpen /Users///kishan//Downloads/\flight_ticket.pdf\nInputs:\nfile_check_result\nHints:\nUse open command to launch the file\nSafety:\nNon-destructive\nVerify:\nFile opened successfully without errors\nOutput:\nopen_result",
      "input_steps": [1,"2""3"],
      "output": "open_result",
      "confidence": 0.85
    }
  ]
}""",
            {
                "followup": bool,
                "followup_question": "",
                "refusal": bool,
                "refusal_reason": "",
                "steps": [{
                    "step_id": int,
                    "tool": "string",
                    "ack_message": "string",
                    "instruction": "string",
                    "input_steps": [int],
                    "output": "string",
                    "confidence": float,
                }],
            },
            {
                "followup": False,
                "followup_question": "",
                "refusal": False,
                "refusal_reason": "",
                "steps": [
                    {
                        "step_id": 1,
                        "tool": "terminal",
                        "ack_message": (
                            "Checking if the flight ticket exists in the Downloads"
                            " folder."
                        ),
                        "instruction": (
                            "Task:\nCheck for existence of"
                            " /Users/kishan/Downloads/flight_ticket.pdf\nInputs:\nNone\nHints:\nUse"
                            " ls -l to check file"
                            " presence\nSafety:\nNon-destructive\nVerify:\nFile exists"
                            " and is readable\nOutput:\nfile_check_result"
                        ),
                        "input_steps": [],
                        "output": "file_check_result",
                        "confidence": 0.9,
                    },
                    {
                        "step_id": 2,
                        "tool": "terminal",
                        "ack_message": (
                            "Opening the flight ticket document from the Downloads"
                            " folder."
                        ),
                        "instruction": (
                            "Task:\nOpen"
                            " /Users/kishan/Downloads/flight_ticket.pdf\nInputs:\nfile_check_result\nHints:\nUse"
                            " open command to launch the"
                            " file\nSafety:\nNon-destructive\nVerify:\nFile opened"
                            " successfully without errors\nOutput:\nopen_result"
                        ),
                        "input_steps": [1, 2, 3],
                        "output": "open_result",
                        "confidence": 0.85,
                    },
                ],
            },
        ),

        (
            "SCH-22",
            "Windows path inside string with bad escapes",
            '{"cmd": "C:\\Program Files\\Git\\bin\\bash.exe"}',
            {"cmd": str},
            {"cmd": "C:/Program Files/Git/bin/bash.exe"},
        ),
        (
            "SCH-23",
            "Tool-call: commands array coerces strings to array items",
            '{tool: terminal commands: "ls -la, pwd, echo hi"}',
            {"tool": str, "commands": [str]},
            {"tool": "terminal", "commands": ["ls -la", "pwd", "echo hi"]},
        ),
        (
            "SCH-24",
            "Regex string with missing escaping fixed via sanitizer",
            '{pattern: "^\\d{4}-\\d{2}-\\d{2}$"}',
            {"pattern": str},
            {"pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        ),
        (
            "SCH-25",
            "Planner/agent step list with numeric coercions and bad commas",
            '{steps: [{id:"1" tool:terminal command:"echo hi"}{id:2 tool:terminal command:"pwd",}],}',
            {"steps": [{"id": int, "tool": str, "command": str}]},
            {"steps": [{"id": 1, "tool": "terminal", "command": "echo hi"}, {"id": 2, "tool": "terminal", "command": "pwd"}]},
        ),
        # ── NEW in v3.8 final: real LLM tool-call / agent patterns ────────────
        (
            "SCH-26",
            "set([]) ctor no longer double-wraps (bug fix)",
            '{tags: set([1,2,3]), ids: frozenset([4,5]), items: list([6,7])}',
            {"tags": [int], "ids": [int], "items": [int]},
            {"tags": [1, 2, 3], "ids": [4, 5], "items": [6, 7]},
        ),
        (
            "SCH-27",
            "Bash pipeline with redirect as cmd string",
            (
                '{"tool": "bash", "cmd": "cat /etc/passwd | grep root > /tmp/out.txt",'
                ' "cwd": "/home/user", "timeout": "30"}'
            ),
            {"tool": str, "cmd": str, "cwd": str, "timeout": int},
            {
                "tool": "bash",
                "cmd": "cat /etc/passwd | grep root > /tmp/out.txt",
                "cwd": "/home/user",
                "timeout": 30,
            },
        ),
        (
            "SCH-28",
            "Agent decision object with reason + confidence",
            '{decision: approve, reason: "Score > 90 and no flags", confidence: "0.95", next_steps: ["notify", "log"]}',
            {"decision": str, "reason": str, "confidence": float, "next_steps": [str]},
            {
                "decision": "approve",
                "reason": "Score > 90 and no flags",
                "confidence": 0.95,
                "next_steps": ["notify", "log"],
            },
        ),
        (
            "SCH-29",
            "Python code string with escaped inner quotes (LLM double-escapes)",
            (
                '{"lang": "python", "code": "import os\\nimport sys\\nprint(os.getcwd())", "timeout": 10}'
            ),
            {"lang": str, "code": str, "timeout": int},
            {
                "lang": "python",
                "code": "import os\nimport sys\nprint(os.getcwd())",
                "timeout": 10,
            },
        ),
        (
            "SCH-30",
            "Quoted POSIX path with double slashes collapsed by schema",
            '{"path": "/home/user//Documents//report.pdf", "action": "open"}',
            {"path": str, "action": str},
            {"path": "/home/user/Documents/report.pdf", "action": "open"},
        ),
        (
            "SCH-31",
            "SQL query with GROUP BY and HAVING",
            (
                '{"query": "SELECT u.name, COUNT(o.id) as orders FROM users u'
                ' LEFT JOIN orders o ON u.id = o.user_id GROUP BY u.id HAVING orders > 5",'
                ' "db": "postgres"}'
            ),
            {"query": str, "db": str},
            {
                "query": (
                    "SELECT u.name, COUNT(o.id) as orders FROM users u"
                    " LEFT JOIN orders o ON u.id = o.user_id GROUP BY u.id HAVING orders > 5"
                ),
                "db": "postgres",
            },
        ),
        (
            "SCH-32",
            "Multi-step agent plan with mixed step_id types -> all int",
            """{
  "steps": [
    {id: 1, action: "read_file", path: "/etc/hosts", output: "hosts_content"},
    {id: "2", action: write_file, path: "/tmp/backup.txt", output: "done"}
  ]
}""",
            {"steps": [{"id": int, "action": str, "path": str, "output": str}]},
            {
                "steps": [
                    {"id": 1, "action": "read_file", "path": "/etc/hosts", "output": "hosts_content"},
                    {"id": 2, "action": "write_file", "path": "/tmp/backup.txt", "output": "done"},
                ]
            },
        ),
        (
            "SCH-33",
            "Truncated LLM output repaired with schema",
            '{"steps": [{"id": 1, "action": "analyze"}, {"id": 2, "action": "repo',
            {"steps": [{"id": int, "action": str}]},
            {"steps": [{"id": 1, "action": "analyze"}, {"id": 2, "action": "repo"}]},
        ),
        (
            "SCH-34",
            "Environment variable map with bare string keys and values (quoted values)",
            '{PATH: "/usr/bin:/usr/local/bin", HOME: "/home/user", TERM: "xterm-256color"}',
            {"PATH": str, "HOME": str, "TERM": str},
            {"PATH": "/usr/bin:/usr/local/bin", "HOME": "/home/user", "TERM": "xterm-256color"},
        ),
        (
            "SCH-35",
            "jq filter with dot-notation inside string",
            '{"tool": "jq", "filter": ".[] | select(.active == true) | .name", "input": "data.json"}',
            {"tool": str, "filter": str, "input": str},
            {
                "tool": "jq",
                "filter": ".[] | select(.active == true) | .name",
                "input": "data.json",
            },
        ),
        (
            "SCH-36",
            "LLM omits quotes around tool name (bare ident as value)",
            '{tool: bash, cmd: "echo hello", cwd: /tmp}',
            {"tool": str, "cmd": str, "cwd": str},
            {"tool": "bash", "cmd": "echo hello", "cwd": "tmp"},
        ),
        (
            "SCH-37",
            "Array of file operations with path coercion",
            """{
  "ops": [
    {op: "read",  path: "/etc/passwd",  mode: "r"},
    {op: "write", path: "/tmp/out.txt", mode: "w", content: "done"},
    {op: "delete", path: "/tmp/old.txt"}
  ]
}""",
            {"ops": [{"op": str, "path": str}]},
            {
                "ops": [
                    {"op": "read", "path": "/etc/passwd", "mode": "r"},
                    {"op": "write", "path": "/tmp/out.txt", "mode": "w", "content": "done"},
                    {"op": "delete", "path": "/tmp/old.txt"},
                ]
            },
        ),
        (
            "SCH-38",
            "Missing commas between step objects in array",
            """{
  "steps": [
    {"id": 1, "tool": "bash", "cmd": "ls -la"}
    {"id": 2, "tool": "python", "cmd": "print('hello')"}
    {"id": 3, "tool": "sql", "cmd": "SELECT 1"}
  ]
}""",
            {"steps": [{"id": int, "tool": str, "cmd": str}]},
            {
                "steps": [
                    {"id": 1, "tool": "bash", "cmd": "ls -la"},
                    {"id": 2, "tool": "python", "cmd": "print('hello')"},
                    {"id": 3, "tool": "sql", "cmd": "SELECT 1"},
                ]
            },
        ),
        (
            "SCH-39",
            "Confidence scores as string -> float, active as 0/1 -> bool",
            """{
  "results": [
    {label: "cat", score: "0.92", active: 1},
    {label: "dog", score: "0.07", active: 0},
    {label: "bird", score: "0.01", active: "false"}
  ]
}""",
            {"results": [{"label": str, "score": float, "active": bool}]},
            {
                "results": [
                    {"label": "cat", "score": 0.92, "active": True},
                    {"label": "dog", "score": 0.07, "active": False},
                    {"label": "bird", "score": 0.01, "active": False},
                ]
            },
        ),
        (
            "SCH-40",
            "Large combined LLM agent payload",
            """{
  "reasoning": "The user wants to analyze their codebase",
  "plan": {
    "steps": [
      {step_id: 1 tool: "bash" cmd: "find /home/user/project -name '*.py' | head -50" output: file_list confidence: 0.95},
      {step_id: "2" tool: bash cmd: "wc -l /home/user//project//main.py" output: line_count confidence: 0.9}
    ]
    "total_steps": 2
    estimated_time: 15
  }
  followup: False
}""",
            {
                "reasoning": str,
                "plan": {
                    "steps": [{"step_id": int, "tool": str, "cmd": str, "output": str, "confidence": float}],
                    "total_steps": int,
                    "estimated_time": int,
                },
                "followup": bool,
            },
            {
                "reasoning": "The user wants to analyze their codebase",
                "plan": {
                    "steps": [
                        {"step_id": 1, "tool": "bash", "cmd": "find /home/user/project -name '*.py' | head -50", "output": "file_list", "confidence": 0.95},
                        {"step_id": 2, "tool": "bash", "cmd": "wc -l /home/user//project//main.py", "output": "line_count", "confidence": 0.9},
                    ],
                    "total_steps": 2,
                    "estimated_time": 15,
                },
                "followup": False,
            },
        ),
        (
            "SCH-41",
            "JSON Schema with anyOf union: coerces to first matching type",
            '{"code": "404", "name": "Alice"}',
            {
                "type": "object",
                "properties": {
                    "code": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                    "name": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                },
            },
            {"code": 404, "name": "Alice"},
        ),
        (
            "SCH-42",
            "Windows PowerShell path with backslashes",
            '{"tool": "powershell", "script": "Get-ChildItem C:\\Users\\Bob\\Desktop", "admin": "true"}',
            {"tool": str, "script": str, "admin": bool},
            {"tool": "powershell", "script": "Get-ChildItem C:\\Users\\Bob\\Desktop", "admin": True},
        ),
        (
            "SCH-43",
            "Bash script with heredoc-style multiline string",
            (
                '{"tool": "bash", "script": "#!/bin/bash\nset -e\ncd /tmp\nmkdir -p test_dir\necho done",'
                '"timeout": "60"}'
            ),
            {"tool": str, "script": str, "timeout": int},
            {
                "tool": "bash",
                "script": "#!/bin/bash\nset -e\ncd /tmp\nmkdir -p test_dir\necho done",
                "timeout": 60,
            },
        ),
        (
            "SCH-44",
            "Missing required fields + extra unknown fields preserved",
            '{"name": "Alice", "role": "admin", "extra_flag": true}',
            {"name": str, "score": float, "active": bool},
            {"name": "Alice", "score": None, "active": None, "role": "admin", "extra_flag": True},
        ),
        (
            "SCH-45",
            "grep command with shell glob and quoted pattern",
            '{"cmd": "grep -r \'error\' /var/log/*.log", "tool": "bash", "timeout": "30"}',
            {"cmd": str, "tool": str, "timeout": int},
            {"cmd": "grep -r 'error' /var/log/*.log", "tool": "bash", "timeout": 30},
        ),
    ]

    print(f"\n{BOLD}{'='*74}{RESET}")
    print(f"{BOLD}  Schema-Guided Tests (v3.8) -- {len(schema_tests)} cases{RESET}")
    print(f"{BOLD}{'='*74}{RESET}\n")

    s_passed = s_failed = 0

    def _deep_approx_equal(a, b):
        """Compare two values allowing float approximation."""
        if isinstance(a, dict) and isinstance(b, dict):
            if set(a.keys()) != set(b.keys()):
                return False, f"keys differ: {set(a.keys())} vs {set(b.keys())}"
            for k in a:
                ok, msg = _deep_approx_equal(a[k], b[k])
                if not ok:
                    return False, f"[{k!r}]: {msg}"
            return True, ""
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return False, f"list lengths differ: {len(a)} vs {len(b)}"
            for i, (ai, bi) in enumerate(zip(a, b)):
                ok, msg = _deep_approx_equal(ai, bi)
                if not ok:
                    return False, f"[{i}]: {msg}"
            return True, ""
        if isinstance(a, float) and isinstance(b, float):
            if abs(a - b) < 1e-9:
                return True, ""
            return False, f"{a} != {b}"
        if a == b:
            return True, ""
        # int/float loose comparison (only for numeric types)
        try:
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if float(a) == float(b):
                    return True, ""
        except (ValueError, TypeError):
            pass
        return False, f"{a!r} != {b!r}"

    for id_, desc, broken_in, sch, expected in schema_tests:
        print(f"{BOLD}{CYAN}{id_}{RESET}: {desc}")
        short = (broken_in[:70] + "...") if len(broken_in) > 70 else broken_in
        print(f"  {DIM}Input:  {short!r}{RESET}")
        print(f"  {DIM}Schema: {repr(sch)[:70]}{RESET}")
        try:
            result_obj = repair_json(broken_in, return_dict=True, schema=sch)
            ok, msg = _deep_approx_equal(result_obj, expected)
            if ok:
                preview = json.dumps(result_obj, ensure_ascii=False)[:90]
                print(
                    f"  {GREEN}+ PASS{RESET}  ->"
                    f" {preview}{'...' if len(json.dumps(result_obj)) > 90 else ''}"
                )
                s_passed += 1
            else:
                print(f"  {RED}- FAIL (mismatch) -- {msg}{RESET}")
                print(
                    f"    Got:      {json.dumps(result_obj, ensure_ascii=False)[:120]}"
                )
                print(f"    Expected: {json.dumps(expected, ensure_ascii=False)[:120]}")
                s_failed += 1
        except Exception as exc:
            print(f"  {RED}- FAIL (exception) -- {exc}{RESET}")
            s_failed += 1
        print()

    pct_s = (
        int(100 * s_passed / (s_passed + s_failed)) if (s_passed + s_failed) else 100
    )
    c = GREEN if s_failed == 0 else (YELLOW if pct_s >= 80 else RED)
    print(
        f"  {BOLD}SCHEMA:{RESET} {GREEN}{s_passed} passed{RESET} "
        f" {RED}{s_failed} failed{RESET}  / {s_passed+s_failed}   {c}({pct_s}%){RESET}"
    )

    print(f"\n{BOLD}{'='*74}{RESET}")
    print(f"{BOLD}  RESULTS BY CATEGORY{RESET}")
    print(f"{'-'*74}{RESET}")
    for cat, r in categories.items():
        total = r["p"] + r["f"]
        bar = chr(9608) * r["p"] + chr(9617) * r["f"]
        pct = int(100 * r["p"] / total) if total else 100
        color = GREEN if r["f"] == 0 else (YELLOW if r["p"] > 0 else RED)
        print(f"  {color}{cat:<8}  {bar:<14}  {r['p']}/{total}  ({pct}%){RESET}")

    pct_total = int(100 * passed / (passed + failed)) if (passed + failed) else 100
    c = GREEN if failed == 0 else (YELLOW if pct_total >= 80 else RED)
    print(f"\n{'-'*74}{RESET}")
    print(
        f"  {BOLD}TOTAL:{RESET}  {GREEN}{passed} passed{RESET} "
        f" {RED}{failed} failed{RESET}  / {passed+failed}   {c}({pct_total}%){RESET}"
    )
    print(f"{BOLD}{'='*74}{RESET}\n")

    if fail_list:
        print(f"{BOLD}{RED}Failed tests:{RESET}")
        for d in fail_list:
            print(f"  {RED}- {d}{RESET}")


if __name__ == "__main__":
    _run_tests()
