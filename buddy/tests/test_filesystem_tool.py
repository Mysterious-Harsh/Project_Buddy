"""
Tests for buddy/tools/os/filesystem.py

Run:
    mamba activate buddy
    pytest buddy/tests/test_filesystem_tool.py -v
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from buddy.tools.os.filesystem import Filesystem, FilesystemCall
from buddy.tools.registry import ToolRegistry


@pytest.fixture
def fs():
    return Filesystem()


@pytest.fixture
def tmp_dir(tmp_path):
    """A real temporary directory tree for testing."""
    (tmp_path / "notes.txt").write_text("hello buddy, this is a note")
    (tmp_path / "resume.pdf").write_bytes(b"%PDF fake binary")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "report.txt").write_text("quarterly report content")
    (tmp_path / "subdir" / "data.csv").write_text("name,age\nharsh,25")
    (tmp_path / ".hidden").write_text("hidden file")
    return tmp_path


# ===========================================================
# Registry integration
# ===========================================================

class TestRegistry:
    def test_filesystem_discovered(self):
        reg = ToolRegistry()
        names = [t["name"] for t in reg.available_tools()]
        assert "filesystem" in names

    def test_get_returns_filesystem(self):
        reg = ToolRegistry()
        tool = reg.get("filesystem")
        assert tool.tool_name == "filesystem"

    def test_get_info_has_required_keys(self):
        reg = ToolRegistry()
        info = reg.tool_info("filesystem")
        for key in ("name", "version", "description", "prompt", "tool_call_format"):
            assert key in info, f"missing key: {key}"


# ===========================================================
# Input validation
# ===========================================================

class TestValidation:
    def test_rejects_relative_path(self, fs):
        with pytest.raises(Exception, match="absolute"):
            fs.parse_call({"action": "info", "path": "relative/path"})

    def test_rejects_empty_path(self, fs):
        with pytest.raises(Exception):
            fs.parse_call({"action": "info", "path": ""})

    def test_rejects_relative_destination(self, fs):
        with pytest.raises(Exception, match="absolute"):
            fs.parse_call({"action": "copy", "paths": ["/tmp/a"], "destination_dir": "relative/dest"})

    def test_file_types_normalised(self, fs):
        call = fs.parse_call({"action": "search", "path": "/tmp", "file_types": [".PDF", "TXT"]})
        assert call.file_types == ["pdf", "txt"]

    def test_max_chars_clamped(self, fs):
        # Over hard limit should raise
        with pytest.raises(Exception):
            fs.parse_call({"action": "read", "path": "/tmp/f.txt", "max_chars": 999_999})


# ===========================================================
# info
# ===========================================================

class TestInfo:
    def test_existing_directory(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "info", "path": str(tmp_dir)}))
        assert r["OK"] is True
        assert r["EXISTS"] is True
        assert r["IS_DIR"] is True
        assert r["IS_FILE"] is False

    def test_existing_file(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "info", "path": str(tmp_dir / "notes.txt")}))
        assert r["OK"] is True
        assert r["EXISTS"] is True
        assert r["IS_FILE"] is True
        assert r["IS_DIR"] is False
        assert isinstance(r["SIZE_BYTES"], int) and r["SIZE_BYTES"] > 0

    def test_nonexistent_path(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "info", "path": str(tmp_dir / "ghost.txt")}))
        assert r["OK"] is True
        assert r["EXISTS"] is False


# ===========================================================
# list
# ===========================================================

class TestList:
    def test_lists_directory(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "list", "path": str(tmp_dir)}))
        assert r["OK"] is True
        names = [e["name"] for e in r["RESULTS"]]
        assert "notes.txt" in names
        assert "subdir" in names

    def test_hidden_excluded_by_default(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "list", "path": str(tmp_dir)}))
        names = [e["name"] for e in r["RESULTS"]]
        assert ".hidden" not in names

    def test_hidden_included_when_requested(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "list", "path": str(tmp_dir), "show_hidden": True}))
        names = [e["name"] for e in r["RESULTS"]]
        assert ".hidden" in names

    def test_error_on_file(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "list", "path": str(tmp_dir / "notes.txt")}))
        assert r["OK"] is False
        assert "directory" in r["ERROR"].lower()

    def test_error_on_nonexistent(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "list", "path": str(tmp_dir / "ghost")}))
        assert r["OK"] is False


# ===========================================================
# search
# ===========================================================

class TestSearch:
    def test_pattern_match(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir),
            "pattern": "*.txt", "recursive": True,
        }))
        assert r["OK"] is True
        names = [e["name"] for e in r["RESULTS"]]
        assert "notes.txt" in names
        assert "report.txt" in names
        assert "resume.pdf" not in names

    def test_non_recursive(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir),
            "pattern": "*.txt", "recursive": False,
        }))
        names = [e["name"] for e in r["RESULTS"]]
        assert "notes.txt" in names
        assert "report.txt" not in names  # in subdir

    def test_file_types_filter(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir),
            "recursive": True, "file_types": ["csv"],
        }))
        names = [e["name"] for e in r["RESULTS"]]
        assert "data.csv" in names
        assert "notes.txt" not in names

    def test_content_query_returns_only_files(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir),
            "content_query": "buddy", "recursive": True,
        }))
        assert r["OK"] is True
        for entry in r["RESULTS"]:
            assert entry["type"] == "file", f"directory in content results: {entry}"

    def test_content_query_matches(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir),
            "content_query": "quarterly", "recursive": True,
        }))
        names = [e["name"] for e in r["RESULTS"]]
        assert "report.txt" in names
        assert "notes.txt" not in names

    def test_content_query_case_insensitive(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir),
            "content_query": "BUDDY", "recursive": True,
        }))
        names = [e["name"] for e in r["RESULTS"]]
        assert "notes.txt" in names

    def test_max_results_respected(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir),
            "pattern": "*.txt", "recursive": True, "max_results": 1,
        }))
        assert len(r["RESULTS"]) <= 1

    def test_no_results_is_ok(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir),
            "pattern": "*.nonexistent",
        }))
        assert r["OK"] is True
        assert r["TOTAL_FOUND"] == 0

    def test_error_on_nonexistent_root(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "search", "path": str(tmp_dir / "ghost_dir"),
        }))
        assert r["OK"] is False


# ===========================================================
# read
# ===========================================================

class TestRead:
    def test_reads_text_file(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "read", "path": str(tmp_dir / "notes.txt")}))
        assert r["OK"] is True
        assert "hello buddy" in r["CONTENT"]
        assert r["SIZE_BYTES"] > 0
        assert r["TRUNCATED"] is False

    def test_truncates_at_max_chars(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({
            "action": "read", "path": str(tmp_dir / "notes.txt"), "max_chars": 5,
        }))
        assert r["OK"] is True
        assert len(r["CONTENT"]) == 5
        assert r["TRUNCATED"] is True

    def test_error_on_nonexistent(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "read", "path": str(tmp_dir / "ghost.txt")}))
        assert r["OK"] is False
        assert "not found" in r["ERROR"].lower()

    def test_error_on_directory(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "read", "path": str(tmp_dir)}))
        assert r["OK"] is False
        assert "directory" in r["ERROR"].lower()

    def test_error_on_binary(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "read", "path": str(tmp_dir / "resume.pdf")}))
        assert r["OK"] is False
        assert "binary" in r["ERROR"].lower()


# ===========================================================
# write / delete cycle
# ===========================================================

class TestWriteDelete:
    def test_write_creates_file(self, fs, tmp_dir):
        path = str(tmp_dir / "new_file.txt")
        r = fs.execute(fs.parse_call({"action": "write", "path": path, "content": "test content"}))
        assert r["OK"] is True
        assert Path(path).read_text() == "test content"

    def test_write_fails_if_exists_no_overwrite(self, fs, tmp_dir):
        path = str(tmp_dir / "notes.txt")
        r = fs.execute(fs.parse_call({"action": "write", "path": path, "content": "x"}))
        assert r["OK"] is False
        assert "overwrite" in r["ERROR"].lower()

    def test_write_overwrites_when_allowed(self, fs, tmp_dir):
        path = str(tmp_dir / "notes.txt")
        r = fs.execute(fs.parse_call({"action": "write", "path": path, "content": "replaced", "overwrite": True}))
        assert r["OK"] is True
        assert Path(path).read_text() == "replaced"

    def test_write_creates_parent_dirs(self, fs, tmp_dir):
        path = str(tmp_dir / "deep" / "nested" / "file.txt")
        r = fs.execute(fs.parse_call({"action": "write", "path": path, "content": "deep"}))
        assert r["OK"] is True
        assert Path(path).exists()

    def test_delete_file(self, fs, tmp_dir):
        path = str(tmp_dir / "notes.txt")
        r = fs.execute(fs.parse_call({"action": "delete", "paths": [path], "confirmed": True}))
        assert r["OK"] is True
        assert r["SUCCEEDED"] == 1
        assert not Path(path).exists()

    def test_delete_multiple(self, fs, tmp_dir):
        p1 = str(tmp_dir / "notes.txt")
        p2 = str(tmp_dir / "subdir" / "report.txt")
        r = fs.execute(fs.parse_call({"action": "delete", "paths": [p1, p2], "confirmed": True}))
        assert r["OK"] is True
        assert r["TOTAL"] == 2
        assert not Path(p1).exists()
        assert not Path(p2).exists()

    def test_delete_directory(self, fs, tmp_dir):
        path = str(tmp_dir / "subdir")
        r = fs.execute(fs.parse_call({"action": "delete", "paths": [path], "confirmed": True}))
        assert r["OK"] is True
        assert not Path(path).exists()

    def test_delete_nonexistent(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "delete", "paths": [str(tmp_dir / "ghost.txt")], "confirmed": True}))
        assert r["OK"] is True  # already absent counts as success


# ===========================================================
# copy / move
# ===========================================================

class TestCopyMove:
    def test_copy_file(self, fs, tmp_dir):
        src = str(tmp_dir / "notes.txt")
        dst_dir = str(tmp_dir / "copy_dest")
        r = fs.execute(fs.parse_call({"action": "copy", "paths": [src], "destination_dir": dst_dir}))
        assert r["OK"] is True
        assert r["SUCCEEDED"] == 1
        assert Path(src).exists()
        assert Path(dst_dir, "notes.txt").exists()

    def test_copy_multiple_files(self, fs, tmp_dir):
        src1 = str(tmp_dir / "notes.txt")
        src2 = str(tmp_dir / "subdir" / "report.txt")
        dst_dir = str(tmp_dir / "copy_dest")
        r = fs.execute(fs.parse_call({"action": "copy", "paths": [src1, src2], "destination_dir": dst_dir}))
        assert r["OK"] is True
        assert r["TOTAL"] == 2
        assert r["SUCCEEDED"] == 2

    def test_move_file(self, fs, tmp_dir):
        src = str(tmp_dir / "notes.txt")
        dst_dir = str(tmp_dir / "move_dest")
        r = fs.execute(fs.parse_call({"action": "move", "paths": [src], "destination_dir": dst_dir}))
        assert r["OK"] is True
        assert r["SUCCEEDED"] == 1
        assert not Path(src).exists()
        assert Path(dst_dir, "notes.txt").exists()

    def test_copy_requires_destination_dir(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "copy", "paths": [str(tmp_dir / "notes.txt")]}))
        assert r["OK"] is False

    def test_move_requires_destination_dir(self, fs, tmp_dir):
        r = fs.execute(fs.parse_call({"action": "move", "paths": [str(tmp_dir / "notes.txt")]}))
        assert r["OK"] is False
