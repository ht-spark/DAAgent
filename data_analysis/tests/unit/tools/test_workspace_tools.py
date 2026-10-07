"""项目只读文件工具测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from data_analysis.tools import create_agent_tool_registry
from data_analysis.tools.workspace_tools import (
    WorkspacePathError,
    list_workspace_directory,
    list_workspace_files,
    read_workspace_file,
    search_workspace_text,
)


def test_workspace_tools_list_read_and_search(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "model.py").write_text("目标函数 = 42\n", encoding="utf-8")
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "package.js").write_text("目标函数", encoding="utf-8")

    listed = list_workspace_files(tmp_path, pattern="*.py")
    loaded = read_workspace_file(tmp_path, "src/model.py")
    searched = search_workspace_text(tmp_path, "目标函数", "*.py")

    assert listed["files"][0]["path"] == "src/model.py"
    assert listed["files"][0]["size_bytes"] > 0
    assert loaded["content"].replace("\r\n", "\n") == "目标函数 = 42\n"
    assert searched["matches"][0]["line"] == 1


def test_workspace_tools_hide_runtime_internal_directory(tmp_path: Path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "secret.txt").write_text("内部状态", encoding="utf-8")

    listed = list_workspace_files(tmp_path)
    searched = search_workspace_text(tmp_path, "内部状态")

    assert listed["files"] == []
    assert searched["matches"] == []
    with pytest.raises(WorkspacePathError, match="运行时内部目录"):
        list_workspace_directory(tmp_path, ".runtime")


def test_workspace_search_propagates_file_listing_truncation(tmp_path: Path):
    for index in range(501):
        (tmp_path / f"file-{index:03d}.txt").write_text("无匹配", encoding="utf-8")

    result = search_workspace_text(tmp_path, "不存在的内容")

    assert result["matches"] == []
    assert result["files_scanned"] == 500
    assert result["truncated"] is True


def test_workspace_directory_lists_one_level_and_skips_dependencies(tmp_path: Path):
    """项目探索应显示直接子目录和文件，不扫描依赖目录。"""
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.py").write_text("print('ok')", encoding="utf-8")
    (tmp_path / "README.md").write_text("项目说明", encoding="utf-8")
    dependency = tmp_path / "node_modules"
    dependency.mkdir()
    (dependency / "package.js").write_text("ignored", encoding="utf-8")

    root = list_workspace_directory(tmp_path)
    nested = list_workspace_directory(tmp_path, "src")

    assert root["directories"] == ["src"]
    assert [item["path"] for item in root["files"]] == ["README.md"]
    assert nested["directories"] == []
    assert [item["path"] for item in nested["files"]] == ["src/main.py"]


def test_workspace_directory_rejects_parent_traversal(tmp_path: Path):
    """逐层查看目录不能逃出已绑定的项目根目录。"""
    with pytest.raises(WorkspacePathError):
        list_workspace_directory(tmp_path, "..")


def test_workspace_tools_reject_path_traversal(tmp_path: Path):
    with pytest.raises(WorkspacePathError):
        read_workspace_file(tmp_path, "../secret.txt")
    with pytest.raises(WorkspacePathError, match="运行时内部目录"):
        read_workspace_file(tmp_path, "nested/.runtime/secret.txt")


def test_project_registry_adds_workspace_tools_only_when_bound(tmp_path: Path):
    unbound = create_agent_tool_registry([], tmp_path / "output")
    bound = create_agent_tool_registry(
        [],
        tmp_path / "output",
        workspace_root=tmp_path,
    )

    assert "list_workspace_directory" not in unbound
    assert "read_workspace_file" not in unbound
    assert "list_workspace_directory" in bound
    assert "read_workspace_file" in bound
    directory = bound.resolve("list_workspace_directory").invoke({})
    assert directory["files"] == []
    result = bound.resolve("list_workspace_files").invoke({"pattern": "*.md"})
    assert result["files"] == []

def test_read_workspace_file_supports_line_windows(tmp_path):
    (tmp_path / "long.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    result = read_workspace_file(tmp_path, "long.py", offset=2, limit=1)
    assert result["content"].splitlines() == ["two"]
    assert result["total_lines"] == 3
    assert result["truncated"] is True
    assert result["truncated_by"] == "lines"
    assert result["next_offset"] == 3


def test_read_workspace_file_accepts_project_absolute_and_at_paths(tmp_path):
    target = tmp_path / "src" / "model.py"
    target.parent.mkdir()
    target.write_bytes(b"value = 42\n")

    absolute = read_workspace_file(tmp_path, str(target))
    mentioned = read_workspace_file(tmp_path, f"@{target}")

    assert absolute["content"] == "value = 42\n"
    assert mentioned["path"] == "src/model.py"


def test_read_workspace_file_rejects_absolute_path_outside_project(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(WorkspacePathError, match="超出当前项目目录"):
        read_workspace_file(tmp_path, str(outside))


def test_read_workspace_file_truncates_at_pi_line_limit(tmp_path):
    target = tmp_path / "many-lines.txt"
    target.write_text("".join(f"{index}\n" for index in range(1, 2102)), encoding="utf-8")

    result = read_workspace_file(tmp_path, "many-lines.txt")

    assert len(result["content"].splitlines()) == 2000
    assert result["total_lines"] == 2101
    assert result["truncated_by"] == "lines"
    assert result["next_offset"] == 2001


def test_read_workspace_file_truncates_without_partial_lines_at_50kb(tmp_path):
    target = tmp_path / "wide-lines.txt"
    target.write_text("".join(f"{index}:" + "x" * 9990 + "\n" for index in range(1, 8)), encoding="utf-8")

    result = read_workspace_file(tmp_path, "wide-lines.txt")

    assert len(result["content"].splitlines()) == 5
    assert result["truncated_by"] == "bytes"
    assert result["next_offset"] == 6
    assert result["first_line_exceeds_limit"] is False


def test_read_workspace_file_reports_oversized_first_line(tmp_path):
    target = tmp_path / "single-wide-line.txt"
    target.write_text("x" * (51 * 1024), encoding="utf-8")

    result = read_workspace_file(tmp_path, "single-wide-line.txt")

    assert result["content"] == ""
    assert result["truncated_by"] == "bytes"
    assert result["next_offset"] == 2
    assert result["first_line_exceeds_limit"] is True


def test_read_workspace_file_rejects_offset_beyond_eof(tmp_path):
    (tmp_path / "short.txt").write_text("one\ntwo\n", encoding="utf-8")

    with pytest.raises(ValueError, match="超出文件末尾"):
        read_workspace_file(tmp_path, "short.txt", offset=3)
