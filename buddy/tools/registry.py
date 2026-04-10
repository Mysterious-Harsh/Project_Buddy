# buddy/tools/registry.py
from __future__ import annotations

import importlib
import pkgutil
import sys
from typing import Any, Dict, List, Optional


class ToolRegistry:
    """
    Dynamic tool registry.

    - Discovers ALL tools under the root package: buddy.tools
    - A tool module must define:
        TOOL_NAME = "terminal"
        and either:
            def get_tool() -> Tool: ...
          or:
            TOOL_CLASS = ToolClass
    - Discovery runs once and is cached. Call invalidate_cache() to force rediscovery
      (e.g. after adding a new tool at runtime).
    - get() always returns a fresh tool instance (calls get_tool()/TOOL_CLASS() each time)
      without reloading the module.
    """

    ROOT_PACKAGE = "buddy.tools"

    def __init__(self) -> None:
        self._module_map: Dict[str, str] = {}   # tool_name -> module_path
        self._discovered: bool = False

    # -------------------------
    # Discovery
    # -------------------------

    def _import_module(self, module_path: str) -> Any:
        if module_path not in sys.modules:
            return importlib.import_module(module_path)
        return sys.modules[module_path]

    def _discover(self) -> Dict[str, str]:
        """
        Walk all modules under buddy.tools and collect those defining TOOL_NAME.
        Result is cached; call invalidate_cache() to re-run.
        """
        if self._discovered:
            return self._module_map

        root = importlib.import_module(self.ROOT_PACKAGE)
        mapping: Dict[str, str] = {}

        for it in pkgutil.walk_packages(root.__path__, root.__name__ + "."):
            module = importlib.import_module(it.name)
            tool_name = getattr(module, "TOOL_NAME", None)
            if isinstance(tool_name, str) and tool_name.strip():
                mapping[tool_name.strip()] = it.name

        self._module_map = mapping
        self._discovered = True
        return mapping

    def invalidate_cache(self) -> None:
        """Force rediscovery and module reload on next access (dev / plugin install)."""
        for module_path in self._module_map.values():
            if module_path in sys.modules:
                importlib.reload(sys.modules[module_path])
        self._module_map = {}
        self._discovered = False

    def _instantiate_tool(self, module: Any) -> Any:
        if hasattr(module, "get_tool"):
            return module.get_tool()
        tool_cls = getattr(module, "TOOL_CLASS", None)
        if tool_cls is None:
            raise RuntimeError(
                f"Tool module {module.__name__} has no TOOL_CLASS or get_tool()"
            )
        return tool_cls()

    # -------------------------
    # Public API
    # -------------------------

    def get(self, tool_name: str) -> Any:
        """
        Get a fresh tool instance by name. Module is NOT reloaded; call
        invalidate_cache() first if you need hot-reload behaviour.
        """
        mapping = self._discover()
        if tool_name not in mapping:
            raise KeyError(f"Tool not found: {tool_name}")

        module = self._import_module(mapping[tool_name])
        return self._instantiate_tool(module)

    def tool_info(self, tool_name: str) -> Dict[str, Any]:
        return self.get(tool_name).get_info()

    def available_tools(self) -> List[Dict[str, Any]]:
        """
        Used by Planner. Returns tool list with name/description/version.
        Single discovery pass; no module reloads.
        """
        mapping = self._discover()
        out: List[Dict[str, Any]] = []

        for name in sorted(mapping.keys()):
            module = self._import_module(mapping[name])
            tool = self._instantiate_tool(module)
            info = tool.get_info()
            out.append({
                "name": name,
                "description": info.get("description", ""),
                "version": info.get("version", ""),
            })

        return out

    def module_path(self, tool_name: str) -> Optional[str]:
        """Debug helper: where did this tool come from?"""
        return self._discover().get(tool_name)


# ==========================================================
# Inline tests (REAL) using buddy.tools.os.terminal
# Run from repo root:
#   python -m buddy.tools.registry
# ==========================================================


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _test_discovery_contains_terminal() -> None:
    reg = ToolRegistry()
    tools = reg.available_tools()
    names = [t["name"] for t in tools]
    _assert(
        "terminal" in names,
        f"'terminal' not discovered under buddy.tools. Found: {names}",
    )


def _test_get_returns_fresh_instance_each_time() -> None:
    reg = ToolRegistry()
    t1 = reg.get("terminal")
    t2 = reg.get("terminal")
    _assert(t1 is not t2, "Expected fresh instance on each get().")


def _test_terminal_execute_echo() -> None:
    """
    Integration test: registry -> terminal tool -> parse_call -> execute
    Uses a safe command: echo
    """
    reg = ToolRegistry()
    term = reg.get("terminal")

    payload = {
        "followup": False,
        "followup_question": "",
        "confidence": 0.9,
        "args": {
            "cwd": None,
            "commands": [["echo", "hello_registry_test"]],
        },
    }

    call_obj = term.parse_call(payload)

    runtime = type(
        "Runtime", (), {"now_iso": "2026-02-03T12:00:00", "timezone": "America/Moncton"}
    )()
    result = term.execute(call_obj, runtime=runtime, on_progress=None)

    _assert(isinstance(result, dict), "terminal.execute must return dict-like result")
    _assert(result.get("ok") is True, f"Expected ok=true, got: {result}")
    cmds = result.get("commands") or []
    _assert(len(cmds) >= 1, "Expected at least one command result")
    stdout = cmds[0].get("stdout") or ""
    _assert(
        "hello_registry_test" in stdout,
        f"stdout missing expected text. stdout={stdout!r}",
    )


def run_tests() -> None:
    _test_discovery_contains_terminal()
    _test_get_returns_fresh_instance_each_time()
    _test_terminal_execute_echo()
    print("registry.py inline tests: OK")


if __name__ == "__main__":
    run_tests()
