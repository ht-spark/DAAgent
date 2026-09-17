"""项目编辑与受控命令的权限边界测试。"""
from __future__ import annotations

import sys

import pytest

from data_analysis.tools.project_edit_tools import (
    _environment_command,
    create_workspace_file,
    edit_workspace_file,
    run_project_command,
    run_project_command_tool,
)
from data_analysis.tools.registry import create_agent_tool_registry
from data_analysis.tools.workspace_tools import WorkspacePathError


def test_project_change_tools_are_available_when_project_is_selected(tmp_path):
    output = tmp_path / "outputs"
    registry = create_agent_tool_registry(
        [], output, workspace_root=tmp_path, python_executable=sys.executable,
    )
    names = {tool.name for tool in registry.specs()}
    assert {"create_workspace_file", "edit_workspace_file", "run_project_command"} <= names
    target = tmp_path / "existing.txt"
    target.write_text("before", encoding="utf-8")
    registry.resolve("edit_workspace_file").invoke({
        "relative_path": "existing.txt", "old_text": "before", "new_text": "after",
    })
    assert target.read_text(encoding="utf-8") == "after"
    assert not output.exists()
    unbound = create_agent_tool_registry([], output)
    assert not {"create_workspace_file", "edit_workspace_file", "run_project_command"} & {
        tool.name for tool in unbound.specs()
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


def test_create_and_edit_accept_absolute_path_inside_project(tmp_path):
    target = tmp_path / "nested" / "result.md"
    created = create_workspace_file(tmp_path, str(target), "before\n")
    edited = edit_workspace_file(tmp_path, str(target), "before", "after")
    assert created["created"] is True
    assert "+after" in edited["diff"]
    assert target.read_text(encoding="utf-8") == "after\n"


def test_project_file_tools_reject_absolute_path_outside_project(tmp_path):
    outside = tmp_path.parent / "outside.md"
    with pytest.raises(WorkspacePathError, match="超出当前项目目录"):
        create_workspace_file(tmp_path, str(outside), "not allowed")
    with pytest.raises(WorkspacePathError, match="超出当前项目目录"):
        edit_workspace_file(tmp_path, str(outside), "before", "after")
    assert not outside.exists()


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


def test_environment_commands_target_project_and_selected_python(tmp_path, monkeypatch):
    monkeypatch.setattr("data_analysis.tools.project_edit_tools.shutil.which", lambda name: name)
    python = str(sys.executable)
    assert _environment_command(tmp_path, "python -m venv .venv", python) == [
        python, "-m", "venv", str(tmp_path / ".venv"),
    ]
    assert _environment_command(tmp_path, "pip install pandas", python) == [
        python, "-m", "pip", "install", "pandas",
    ]
    assert _environment_command(tmp_path, "uv pip install numpy", python) == [
        "uv", "pip", "install", "--python", python, "numpy",
    ]
    (tmp_path / "requirements.txt").write_text("pandas\n", encoding="utf-8")
    assert _environment_command(tmp_path, "pip install -r requirements.txt", python) == [
        python, "-m", "pip", "install", "-r", str(tmp_path / "requirements.txt"),
    ]
    assert _environment_command(tmp_path, "conda create -p env python=3.11", python) == [
        "conda", "create", "-p", str(tmp_path / "env"), "python=3.11", "-y",
    ]
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert _environment_command(tmp_path, "uv sync", python) == ["uv", "sync"]


def test_environment_commands_reject_escape_and_shell_syntax(tmp_path):
    with pytest.raises(WorkspacePathError):
        _environment_command(tmp_path, "python -m venv ../outside", sys.executable)
    with pytest.raises(ValueError, match="不支持的安装参数"):
        _environment_command(tmp_path, "pip install pandas && whoami", sys.executable)
    with pytest.raises(WorkspacePathError, match="当前项目内"):
        run_project_command_tool(
            {"command": "pip install pandas", "python_environment": str(tmp_path.parent)},
            {"workspace_root": str(tmp_path), "python_executable": sys.executable},
        )


def test_missing_project_environment_only_allows_bootstrap_command(tmp_path):
    context = {
        "workspace_root": str(tmp_path),
        "python_executable": sys.executable,
        "python_environment_source": "host",
    }
    with pytest.raises(ValueError, match="当前项目未发现 Python 环境"):
        run_project_command_tool({"command": "pip install pandas"}, context)
    with pytest.raises(ValueError, match="当前项目未发现 Python 环境"):
        run_project_command_tool({"command": "python -m pytest"}, context)
    result = run_project_command_tool({"command": "python -m venv .venv"}, context)
    assert result["exit_code"] == 0
    assert (tmp_path / ".venv" / "pyvenv.cfg").is_file()
