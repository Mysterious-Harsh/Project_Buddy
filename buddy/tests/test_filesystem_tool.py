"""
Tests for the split filesystem tools: fs_browse, fs_read, fs_write, fs_manage.

Run:
    mamba activate buddy
    pytest buddy/tests/test_filesystem_tool.py -v
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from buddy.tools.os.fs_browse import FsBrowse
from buddy.tools.os.fs_manage import FsManage
from buddy.tools.os.fs_read import FsRead
from buddy.tools.os.fs_write import FsWrite
from buddy.tools.registry import ToolRegistry


# ── helpers ───────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def browse():
    return FsBrowse()


@pytest.fixture
def reader():
    return FsRead()


@pytest.fixture
def writer():
    return FsWrite()


@pytest.fixture
def manager():
    return FsManage()


@pytest.fixture
def tmp_tree(tmp_path):
    (tmp_path / "notes.txt").write_text("hello buddy, this is a note")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "report.txt").write_text("quarterly report content")
    (tmp_path / "subdir" / "data.csv").write_text("name,age\nharsh,25")
    (tmp_path / ".hidden").write_text("hidden file")
    return tmp_path


# ===========================================================
# Registry — all 4 tools discoverable
# ===========================================================

class TestRegistry:
    def test_all_tools_discovered(self):
        reg = ToolRegistry()
        names = [t["name"] for t in reg.available_tools()]
        for name in ("fs_browse", "fs_read", "fs_write", "fs_manage"):
            assert name in names, f"'{name}' not discovered"

    def test_get_returns_correct_instances(self):
        reg = ToolRegistry()
        assert reg.get("fs_browse").tool_name == "fs_browse"
        assert reg.get("fs_read").tool_name == "fs_read"
        assert reg.get("fs_write").tool_name == "fs_write"
        assert reg.get("fs_manage").tool_name == "fs_manage"

    def test_filesystem_not_registered(self):
        reg = ToolRegistry()
        names = [t["name"] for t in reg.available_tools()]
        assert "filesystem" not in names, "old 'filesystem' should not be registered"


# ===========================================================
# fs_browse — ls
# ===========================================================

class TestLs:
    def test_lists_directory(self, browse, tmp_tree):
        r = run(browse.execute("ls", {"path": str(tmp_tree)}))
        assert r["STATUS"] == "success"
        names = [e["name"] for e in r["ENTRIES"]]
        assert "notes.txt" in names
        assert "subdir" in names

    def test_hidden_excluded_by_default(self, browse, tmp_tree):
        r = run(browse.execute("ls", {"path": str(tmp_tree)}))
        names = [e["name"] for e in r["ENTRIES"]]
        assert ".hidden" not in names

    def test_hidden_included_when_requested(self, browse, tmp_tree):
        r = run(browse.execute("ls", {"path": str(tmp_tree), "show_hidden": True}))
        names = [e["name"] for e in r["ENTRIES"]]
        assert ".hidden" in names

    def test_tree_view(self, browse, tmp_tree):
        r = run(browse.execute("ls", {"path": str(tmp_tree), "depth": 2}))
        assert r["STATUS"] == "success"
        assert "TREE_TEXT" in r
        assert "subdir" in r["TREE_TEXT"]

    def test_error_on_file_path(self, browse, tmp_tree):
        r = run(browse.execute("ls", {"path": str(tmp_tree / "notes.txt")}))
        assert r["STATUS"] == "failed"
        assert "file" in r["ERROR"].lower()

    def test_error_on_nonexistent(self, browse, tmp_tree):
        r = run(browse.execute("ls", {"path": str(tmp_tree / "ghost_dir")}))
        assert r["STATUS"] == "failed"
        assert "does not exist" in r["ERROR"]

    def test_error_missing_path(self, browse):
        r = run(browse.execute("ls", {}))
        assert r["STATUS"] == "failed"
        assert "path" in r["ERROR"].lower()


# ===========================================================
# fs_browse — find
# ===========================================================

class TestFind:
    def test_find_by_name_glob(self, browse, tmp_tree):
        r = run(browse.execute("find", {"path": str(tmp_tree), "pattern": "*.txt"}))
        assert r["STATUS"] == "success"
        names = [Path(e["path"]).name for e in r["RESULTS"]]
        assert "notes.txt" in names
        assert "report.txt" in names

    def test_find_non_recursive(self, browse, tmp_tree):
        r = run(browse.execute("find", {"path": str(tmp_tree), "pattern": "*.txt", "recursive": False}))
        names = [Path(e["path"]).name for e in r["RESULTS"]]
        assert "notes.txt" in names
        assert "report.txt" not in names

    def test_find_content(self, browse, tmp_tree):
        r = run(browse.execute("find", {"path": str(tmp_tree), "pattern": "quarterly", "type": "content"}))
        assert r["STATUS"] == "success"
        files = [e["file"] for e in r["RESULTS"]]
        assert any("report.txt" in f for f in files)

    def test_find_content_case_insensitive(self, browse, tmp_tree):
        r = run(browse.execute("find", {"path": str(tmp_tree), "pattern": "BUDDY", "type": "content"}))
        files = [e["file"] for e in r["RESULTS"]]
        assert any("notes.txt" in f for f in files)

    def test_find_no_results_is_success(self, browse, tmp_tree):
        r = run(browse.execute("find", {"path": str(tmp_tree), "pattern": "*.nonexistent"}))
        assert r["STATUS"] == "success"
        assert r["TOTAL_FOUND"] == 0

    def test_find_error_nonexistent_root(self, browse, tmp_tree):
        r = run(browse.execute("find", {"path": str(tmp_tree / "ghost_dir"), "pattern": "*.txt"}))
        assert r["STATUS"] == "failed"
        assert "does not exist" in r["ERROR"]

    def test_find_error_invalid_type(self, browse, tmp_tree):
        r = run(browse.execute("find", {"path": str(tmp_tree), "pattern": "*.txt", "type": "bad"}))
        assert r["STATUS"] == "failed"
        assert "'name' or 'content'" in r["ERROR"]


# ===========================================================
# fs_read — read
# ===========================================================

class TestRead:
    def test_reads_text_file(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree / "notes.txt")}))
        assert r["STATUS"] == "success"
        assert "hello buddy" in r["CONTENT"]
        assert r["SIZE_BYTES"] > 0

    def test_reads_csv(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree / "subdir" / "data.csv")}))
        assert r["STATUS"] == "success"
        # Either text fallback or table format
        assert r["CONTENT"]

    def test_truncates_at_max_chars(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree / "notes.txt"), "max_chars": 5}))
        assert r["STATUS"] == "success"
        assert r.get("TRUNCATED") is True
        assert r["CONTENT"].startswith("hello")  # first 5 chars of content preserved

    def test_info_mode(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree / "notes.txt"), "info": True}))
        assert r["STATUS"] == "success"
        assert r["EXISTS"] is True
        assert r["IS_FILE"] is True
        assert "SIZE_BYTES" in r

    def test_info_nonexistent(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree / "ghost.txt"), "info": True}))
        assert r["STATUS"] == "success"
        assert r["EXISTS"] is False

    def test_error_not_found(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree / "ghost.txt")}))
        assert r["STATUS"] == "failed"
        assert "does not exist" in r["ERROR"]
        assert "fs_browse.find" in r["ERROR"]

    def test_error_on_directory(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree)}))
        assert r["STATUS"] == "failed"
        assert "directory" in r["ERROR"].lower()
        assert "fs_browse.ls" in r["ERROR"]

    def test_line_range(self, reader, tmp_tree):
        path = str(tmp_tree / "subdir" / "report.txt")
        r = run(reader.execute("read", {"path": path, "start_line": 1, "end_line": 1}))
        assert r["STATUS"] == "success"
        assert "quarterly" in r["CONTENT"]

    def test_search_pattern(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree / "notes.txt"), "search_pattern": "buddy"}))
        assert r["STATUS"] == "success"
        assert "buddy" in r["CONTENT"].lower()

    def test_search_pattern_no_match(self, reader, tmp_tree):
        r = run(reader.execute("read", {"path": str(tmp_tree / "notes.txt"), "search_pattern": "ZZZNOMATCH"}))
        assert r["STATUS"] == "success"
        assert "matched 0 lines" in r.get("NOTE", "")


# ===========================================================
# fs_read — diff
# ===========================================================

class TestDiff:
    def test_diff_identical_files(self, reader, tmp_tree):
        p1 = str(tmp_tree / "notes.txt")
        p2 = str(tmp_tree / "subdir" / "report.txt")
        r = run(reader.execute("diff", {"path_a": p1, "path_b": p2}))
        assert r["STATUS"] == "success"
        assert "DIFF" in r

    def test_diff_same_file(self, reader, tmp_tree):
        p = str(tmp_tree / "notes.txt")
        r = run(reader.execute("diff", {"path_a": p, "path_b": p}))
        assert r["STATUS"] == "success"
        assert "identical" in r["DIFF"]

    def test_diff_missing_path(self, reader, tmp_tree):
        r = run(reader.execute("diff", {"path_a": str(tmp_tree / "notes.txt"), "path_b": str(tmp_tree / "ghost.txt")}))
        assert r["STATUS"] == "failed"
        assert "does not exist" in r["ERROR"]

    def test_diff_requires_both_paths(self, reader, tmp_tree):
        r = run(reader.execute("diff", {"path_a": str(tmp_tree / "notes.txt")}))
        assert r["STATUS"] == "failed"
        assert "path_b" in r["ERROR"]


# ===========================================================
# fs_write — write
# ===========================================================

class TestWrite:
    def test_create_new_file(self, writer, tmp_tree):
        path = str(tmp_tree / "new_file.txt")
        r = run(writer.execute("write", {"path": path, "action": "create", "content": "hello"}))
        assert r["STATUS"] == "success"
        assert Path(path).read_text() == "hello"

    def test_create_fails_if_exists_no_confirm(self, writer, tmp_tree):
        path = str(tmp_tree / "notes.txt")
        r = run(writer.execute("write", {"path": path, "action": "create", "content": "x"}))
        assert r["STATUS"] == "failed"
        assert r.get("NEEDS_CONFIRMATION") is True
        assert "already exists" in r["PREVIEW"]

    def test_create_overwrites_when_confirmed(self, writer, tmp_tree):
        path = str(tmp_tree / "notes.txt")
        r = run(writer.execute("write", {"path": path, "action": "create", "content": "replaced", "confirmed": True}))
        assert r["STATUS"] == "success"
        assert Path(path).read_text() == "replaced"

    def test_create_makes_parent_dirs(self, writer, tmp_tree):
        path = str(tmp_tree / "deep" / "nested" / "file.txt")
        r = run(writer.execute("write", {"path": path, "action": "create", "content": "deep"}))
        assert r["STATUS"] == "success"
        assert Path(path).exists()

    def test_append_creates_file(self, writer, tmp_tree):
        path = str(tmp_tree / "newfile.txt")
        run(writer.execute("write", {"path": path, "action": "append", "content": "line1\n"}))
        run(writer.execute("write", {"path": path, "action": "append", "content": "line2\n"}))
        content = Path(path).read_text()
        assert "line1" in content
        assert "line2" in content

    def test_patch_replaces_text(self, writer, tmp_tree):
        path = str(tmp_tree / "notes.txt")
        r = run(writer.execute("write", {"path": path, "action": "patch", "old_str": "hello buddy", "new_str": "hi buddy"}))
        assert r["STATUS"] == "success"
        assert "hi buddy" in Path(path).read_text()

    def test_patch_fails_old_str_not_found(self, writer, tmp_tree):
        path = str(tmp_tree / "notes.txt")
        r = run(writer.execute("write", {"path": path, "action": "patch", "old_str": "ZZZNOMATCH", "new_str": "x"}))
        assert r["STATUS"] == "failed"
        assert "not found" in r["ERROR"]
        assert "fs_read.read" in r["ERROR"]

    def test_patch_fails_ambiguous(self, writer, tmp_tree):
        path = str(tmp_tree / "repeat.txt")
        Path(path).write_text("abc abc abc")
        r = run(writer.execute("write", {"path": path, "action": "patch", "old_str": "abc", "new_str": "xyz"}))
        assert r["STATUS"] == "failed"
        assert "matched 3 times" in r["ERROR"]

    def test_patch_fails_missing_old_str(self, writer, tmp_tree):
        path = str(tmp_tree / "notes.txt")
        r = run(writer.execute("write", {"path": path, "action": "patch", "new_str": "x"}))
        assert r["STATUS"] == "failed"
        assert "old_str" in r["ERROR"]

    def test_invalid_action(self, writer, tmp_tree):
        r = run(writer.execute("write", {"path": str(tmp_tree / "f.txt"), "action": "obliterate"}))
        assert r["STATUS"] == "failed"
        assert "obliterate" in r["ERROR"]


# ===========================================================
# fs_manage — manage
# ===========================================================

class TestManage:
    def test_copy_file(self, manager, tmp_tree):
        src = str(tmp_tree / "notes.txt")
        dst = str(tmp_tree / "copy_dest")
        r = run(manager.execute("manage", {"action": "copy", "paths": [src], "destination_dir": dst, "confirmed": True}))
        assert r["STATUS"] == "success"
        assert r["SUCCEEDED"] == 1
        assert Path(src).exists()
        assert Path(dst, "notes.txt").exists()

    def test_move_file(self, manager, tmp_tree):
        src = str(tmp_tree / "notes.txt")
        dst = str(tmp_tree / "moved")
        r = run(manager.execute("manage", {"action": "move", "paths": [src], "destination_dir": dst, "confirmed": True}))
        assert r["STATUS"] == "success"
        assert not Path(src).exists()
        assert Path(dst, "notes.txt").exists()

    def test_delete_file_confirmed(self, manager, tmp_tree):
        path = str(tmp_tree / "notes.txt")
        r = run(manager.execute("manage", {"action": "delete", "paths": [path], "confirmed": True, "permanent": True}))
        assert r["STATUS"] == "success"
        assert not Path(path).exists()

    def test_delete_requires_confirmation(self, manager, tmp_tree):
        path = str(tmp_tree / "notes.txt")
        r = run(manager.execute("manage", {"action": "delete", "paths": [path]}))
        assert r["STATUS"] == "failed"
        assert r.get("NEEDS_CONFIRMATION") is True

    def test_delete_nonexistent_is_success(self, manager, tmp_tree):
        r = run(manager.execute("manage", {"action": "delete", "paths": [str(tmp_tree / "ghost.txt")], "confirmed": True, "permanent": True}))
        assert r["STATUS"] == "success"

    def test_mkdir(self, manager, tmp_tree):
        new_dir = str(tmp_tree / "created_dir")
        r = run(manager.execute("manage", {"action": "mkdir", "paths": [new_dir]}))
        assert r["STATUS"] == "success"
        assert Path(new_dir).is_dir()

    def test_copy_requires_destination(self, manager, tmp_tree):
        r = run(manager.execute("manage", {"action": "copy", "paths": [str(tmp_tree / "notes.txt")]}))
        assert r["STATUS"] == "failed"
        assert "destination_dir" in r["ERROR"]

    def test_invalid_action(self, manager, tmp_tree):
        r = run(manager.execute("manage", {"action": "explode", "paths": [str(tmp_tree / "notes.txt")]}))
        assert r["STATUS"] == "failed"
        assert "explode" in r["ERROR"]

    def test_partial_failure_reported(self, manager, tmp_tree):
        real = str(tmp_tree / "notes.txt")
        ghost = str(tmp_tree / "ghost.txt")
        dst = str(tmp_tree / "dest")
        r = run(manager.execute("manage", {"action": "copy", "paths": [real, ghost], "destination_dir": dst, "confirmed": True}))
        assert r["TOTAL"] == 2
        assert r["FAILED"] >= 1
        assert any("ghost.txt" in res.get("PATH", "") for res in r["RESULTS"] if res.get("STATUS") == "failed")


# ===========================================================
# fs_manage — rename
# ===========================================================

class TestRename:
    def test_rename_file(self, manager, tmp_tree):
        path = str(tmp_tree / "notes.txt")
        r = run(manager.execute("rename", {"renames": [{"path": path, "new_name": "renamed.txt"}]}))
        assert r["STATUS"] == "success"
        assert not Path(path).exists()
        assert (tmp_tree / "renamed.txt").exists()

    def test_rename_conflict_requires_confirm(self, manager, tmp_tree):
        (tmp_tree / "existing.txt").write_text("existing")
        r = run(manager.execute("rename", {"renames": [{"path": str(tmp_tree / "notes.txt"), "new_name": "existing.txt"}]}))
        assert r["STATUS"] == "failed"
        assert r.get("NEEDS_CONFIRMATION") is True

    def test_rename_rejects_slashes(self, manager, tmp_tree):
        r = run(manager.execute("rename", {"renames": [{"path": str(tmp_tree / "notes.txt"), "new_name": "sub/bad.txt"}]}))
        assert r["STATUS"] == "failed"
        assert "separator" in r["ERROR"].lower()

    def test_rename_nonexistent(self, manager, tmp_tree):
        r = run(manager.execute("rename", {"renames": [{"path": str(tmp_tree / "ghost.txt"), "new_name": "other.txt"}]}))
        assert r["FAILED"] == 1
        assert "does not exist" in r["RESULTS"][0]["ERROR"]


# ===========================================================
# unknown function error messages
# ===========================================================

class TestUnknownFunction:
    def test_browse_unknown(self, browse):
        r = run(browse.execute("obliterate", {}))
        assert r["STATUS"] == "failed"
        assert "fs_browse supports" in r["ERROR"]

    def test_read_unknown(self, reader):
        r = run(reader.execute("obliterate", {}))
        assert r["STATUS"] == "failed"
        assert "fs_read supports" in r["ERROR"]

    def test_write_unknown(self, writer):
        r = run(writer.execute("obliterate", {}))
        assert r["STATUS"] == "failed"
        assert "fs_write supports" in r["ERROR"]

    def test_manage_unknown(self, manager):
        r = run(manager.execute("obliterate", {}))
        assert r["STATUS"] == "failed"
        assert "fs_manage supports" in r["ERROR"]
