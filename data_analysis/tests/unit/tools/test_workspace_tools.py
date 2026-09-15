"""项目只读文件工具测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from data_analysis.tools import create_agent_tool_registry
from data_analysis.tools.workspace_tools import (
    WorkspacePathError,
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


def test_workspace_tools_reject_path_traversal(tmp_path: Path):
    with pytest.raises(WorkspacePathError):
        read_workspace_file(tmp_path, "../secret.txt")


def test_project_registry_adds_workspace_tools_only_when_bound(tmp_path: Path):
    unbound = create_agent_tool_registry([], tmp_path / "output")
    bound = create_agent_tool_registry(
        [],
        tmp_path / "output",
        workspace_root=tmp_path,
    )

    assert "read_workspace_file" not in unbound
    assert "read_workspace_file" in bound
    result = bound.resolve("list_workspace_files").invoke({"pattern": "*.md"})
    assert result["files"] == []