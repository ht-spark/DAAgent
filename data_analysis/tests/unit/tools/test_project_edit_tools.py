"""项目编辑与受控命令的权限边界测试。"""
from __future__ import annotations

import sys

import pytest

from data_analysis.tools.project_edit_tools import (
    create_workspace_file,
    edit_workspace_file,
    run_project_command,
)
from data_analysis.tools.registry import create_agent_tool_registry
from data_analysis.tools.workspace_tools import WorkspacePathError


def test_project_change_tools_require_explicit_opt_in(tmp_path):
    output = tmp_path / "outputs"
    output.mkdir()
    names = {tool.name for tool in create_agent_tool_registry([], output, workspace_root=tmp_path).specs()}
    assert "edit_workspace_file" not in names
    enabled = create_agent_tool_registry(
        [], output, workspace_root=tmp_path, enable_project_changes=True,
        python_executable=sys.executable,
    )
    assert {"create_workspace_file", "edit_workspace_file", "run_project_command"} <= {
        tool.name for tool in enabled.specs()
    }


def test_create_and_edit_project_file_returns_diff(tmp_path):
    created = create_workspace_file(tmp_path, "src/calculator.py", "return a - b\n")
    edited = edit_workspace_file(tmp_path, "src/calculator.py", "return a - b", "return a + b")
    assert created["created"] is True
    assert "-return a - b" in edited["diff"]
    assert "+return a + b" in edited["diff"]
    assert (tmp_path / "src/calculator.py").read_text(encoding="utf-8") == "return a + b\n"
    with pytest.raises(FileExistsError):
        create_workspace_file(tmp_path, "src/calculator.py", "overwrite")


def test_edit_rejects_ambiguous_and_outside_paths(tmp_path):
    (tmp_path / "repeat.py").write_text("pass\npass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="匹配 2 次"):
        edit_workspace_file(tmp_path, "repeat.py", "pass", "return")
    with pytest.raises(WorkspacePathError):
        create_workspace_file(tmp_path, "../escape.py", "x")
    with pytest.raises(WorkspacePathError):
        create_workspace_file(tmp_path, ".env", "SECRET=x")


def test_command_rejects_shell_and_returns_failure_to_model(tmp_path):
    with pytest.raises(ValueError, match="只允许"):
        run_project_command(tmp_path, "powershell Remove-Item *", sys.executable)
    result = run_project_command(tmp_path, "python -m pytest", sys.executable)
    assert result["exit_code"] != 0
    assert result["stderr"] or result["stdout"]

def test_git_command_rejects_parent_repository(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nested = tmp_path / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="仓库根目录"):
        run_project_command(nested, "git status", sys.executable)