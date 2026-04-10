"""
Tests for buddy.tools.web.search (WebSearch tool).
Run: python -m pytest buddy/tests/test_web_search_tool.py -v
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from buddy.tools.web.search import (
    TOOL_CLASS,
    TOOL_NAME,
    WebSearch,
    WebSearchCall,
    get_tool,
    _MAX_CHARS_HARD_LIMIT,
    _MAX_RESULTS_HARD_LIMIT,
    _DEFAULT_MAX_CHARS,
    _DEFAULT_MAX_RESULTS,
)


# ==========================================================
# Registry
# ==========================================================


class TestRegistry:
    def test_tool_name_constant(self):
        assert TOOL_NAME == "web_search"

    def test_tool_class_constant(self):
        assert TOOL_CLASS is WebSearch

    def test_get_tool_returns_instance(self):
        tool = get_tool()
        assert isinstance(tool, WebSearch)

    def test_get_tool_fresh_each_call(self):
        assert get_tool() is not get_tool()

    def test_discovered_by_registry(self):
        from buddy.tools.registry import ToolRegistry
        reg = ToolRegistry()
        names = [t["name"] for t in reg.available_tools()]
        assert "web_search" in names

    def test_get_info_fields(self):
        ws = WebSearch()
        info = ws.get_info()
        assert info["name"] == "web_search"
        assert "description" in info
        assert "prompt" in info
        assert "error_prompt" in info
        assert "tool_call_format" in info


# ==========================================================
# Validation
# ==========================================================


class TestValidation:
    def test_search_requires_query(self):
        with pytest.raises(ValidationError):
            WebSearchCall(action="search")

    def test_search_empty_query_rejected(self):
        with pytest.raises(ValidationError):
            WebSearchCall(action="search", query="   ")

    def test_search_query_stripped(self):
        call = WebSearchCall(action="search", query="  hello world  ")
        assert call.query == "hello world"

    def test_search_defaults(self):
        call = WebSearchCall(action="search", query="test")
        assert call.max_results == _DEFAULT_MAX_RESULTS
        assert call.region == "wt-wt"
        assert call.safe_search is True

    def test_search_max_results_clamped(self):
        call = WebSearchCall(action="search", query="test", max_results=999)
        assert call.max_results == _MAX_RESULTS_HARD_LIMIT

    def test_search_max_results_negative_reset(self):
        call = WebSearchCall(action="search", query="test", max_results=-5)
        assert call.max_results == _DEFAULT_MAX_RESULTS

    def test_fetch_requires_url(self):
        with pytest.raises(ValidationError):
            WebSearchCall(action="fetch")

    def test_fetch_empty_url_rejected(self):
        with pytest.raises(ValidationError):
            WebSearchCall(action="fetch", url="   ")

    def test_fetch_url_must_be_http(self):
        with pytest.raises(ValidationError):
            WebSearchCall(action="fetch", url="ftp://example.com")

    def test_fetch_url_https_accepted(self):
        call = WebSearchCall(action="fetch", url="https://example.com")
        assert call.url == "https://example.com"

    def test_fetch_url_http_accepted(self):
        call = WebSearchCall(action="fetch", url="http://example.com")
        assert call.url == "http://example.com"

    def test_fetch_max_chars_defaults(self):
        call = WebSearchCall(action="fetch", url="https://example.com")
        assert call.max_chars == _DEFAULT_MAX_CHARS

    def test_fetch_max_chars_clamped(self):
        call = WebSearchCall(action="fetch", url="https://example.com", max_chars=999999)
        assert call.max_chars == _MAX_CHARS_HARD_LIMIT

    def test_invalid_action_rejected(self):
        with pytest.raises(ValidationError):
            WebSearchCall(action="browse", query="test")


# ==========================================================
# parse_call
# ==========================================================


class TestParseCall:
    def test_parse_search_dict(self):
        ws = WebSearch()
        call = ws.parse_call({"action": "search", "query": "python"})
        assert call.action == "search"
        assert call.query == "python"

    def test_parse_fetch_dict(self):
        ws = WebSearch()
        call = ws.parse_call({"action": "fetch", "url": "https://example.com"})
        assert call.action == "fetch"
        assert call.url == "https://example.com"

    def test_parse_invalid_raises(self):
        ws = WebSearch()
        with pytest.raises(ValidationError):
            ws.parse_call({"action": "search"})  # missing query


# ==========================================================
# _extract_text (static, no network needed)
# ==========================================================


class TestExtractText:
    def test_extracts_title(self):
        html = "<html><head><title>My Page</title></head><body><p>Hello</p></body></html>"
        text, title = WebSearch._extract_text(html)
        assert title == "My Page"
        assert "Hello" in text

    def test_strips_scripts(self):
        html = "<html><body><script>alert(1)</script><p>Content</p></body></html>"
        text, _ = WebSearch._extract_text(html)
        assert "alert" not in text
        assert "Content" in text

    def test_strips_style(self):
        html = "<html><body><style>.a{color:red}</style><p>Text</p></body></html>"
        text, _ = WebSearch._extract_text(html)
        assert "color" not in text
        assert "Text" in text

    def test_prefers_article_body(self):
        html = (
            "<html><body>"
            "<nav>NAV NOISE</nav>"
            "<article><p>Real content here</p></article>"
            "<footer>FOOTER NOISE</footer>"
            "</body></html>"
        )
        text, _ = WebSearch._extract_text(html)
        assert "Real content here" in text
        # nav/footer should not appear when article is present
        assert "NAV NOISE" not in text

    def test_collapses_blank_lines(self):
        html = "<html><body><p>A</p>\n\n\n\n<p>B</p></body></html>"
        text, _ = WebSearch._extract_text(html)
        assert "\n\n\n" not in text

    def test_empty_title_when_missing(self):
        html = "<html><body><p>No title</p></body></html>"
        _, title = WebSearch._extract_text(html)
        assert title == ""


# ==========================================================
# execute — error paths (no network)
# ==========================================================


class TestExecuteErrorPaths:
    def test_search_returns_ok_false_on_ddgs_error(self, monkeypatch):
        def bad_ddgs(*args, **kwargs):
            raise RuntimeError("network down")

        import buddy.tools.web.search as mod
        monkeypatch.setattr(mod, "DDGS", lambda: _BrokenDDGS())

        ws = WebSearch()
        call = WebSearchCall(action="search", query="test")
        result = ws.execute(call)
        assert result["OK"] is False
        assert "ERROR" in result
        assert result["TOTAL_FOUND"] == 0

    def test_fetch_returns_ok_false_on_network_error(self, monkeypatch):
        import requests as req
        import buddy.tools.web.search as mod

        def fail_get(*args, **kwargs):
            raise req.exceptions.ConnectionError("offline")

        monkeypatch.setattr(mod.requests, "get", fail_get)

        ws = WebSearch()
        call = WebSearchCall(action="fetch", url="https://example.com")
        result = ws.execute(call)
        assert result["OK"] is False
        assert result["CONTENT"] is None
        assert "ERROR" in result

    def test_fetch_rejects_non_html_content_type(self, monkeypatch):
        import buddy.tools.web.search as mod

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/octet-stream"}
            text = ""
            def raise_for_status(self): pass

        monkeypatch.setattr(mod.requests, "get", lambda *a, **kw: FakeResp())

        ws = WebSearch()
        call = WebSearchCall(action="fetch", url="https://example.com")
        result = ws.execute(call)
        assert result["OK"] is False
        assert "content type" in result["ERROR"].lower()

    def test_fetch_truncates_long_content(self, monkeypatch):
        import buddy.tools.web.search as mod

        big_text = "word " * 10_000  # 50k+ chars
        html = f"<html><body><p>{big_text}</p></body></html>"

        class FakeResp:
            status_code = 200
            headers = {"content-type": "text/html"}
            text = html
            def raise_for_status(self): pass

        monkeypatch.setattr(mod.requests, "get", lambda *a, **kw: FakeResp())

        ws = WebSearch()
        call = WebSearchCall(action="fetch", url="https://example.com", max_chars=500)
        result = ws.execute(call)
        assert result["OK"] is True
        assert result["SIZE_CHARS"] <= 540  # 500 + "\n[truncated at N chars]" suffix
        assert "truncated" in result["CONTENT"]


# ==========================================================
# Helpers
# ==========================================================


class _BrokenDDGS:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def text(self, *args, **kwargs):
        raise RuntimeError("simulated ddgs failure")
