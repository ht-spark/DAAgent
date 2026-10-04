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
        "relative_path": "existing.txt",
        "edits": [{"old_text": "before", "new_text": "after"}],
    })
    assert target.read_text(encoding="utf-8") == "after"
    assert not output.exists()
    unbound = create_agent_tool_registry([], output)
    assert not {"create_workspace_file", "edit_workspace_file", "run_project_command"} & {
        tool.name for tool in unbound.specs()
    }


@pytest.mark.parametrize(
    "edits",
    [
        [],
        [{"old_text": "before"}],
        [{"old_text": "before", "new_text": "after", "extra": True}],
        [{"old_text": 1, "new_text": "after"}],
    ],
)
def test_edit_tool_rejects_malformed_edits(tmp_path, edits):
    target = tmp_path / "existing.txt"
    target.write_text("before", encoding="utf-8")
    registry = create_agent_tool_registry([], tmp_path / "outputs", workspace_root=tmp_path)

    with pytest.raises(ValueError, match="参数无效"):
        registry.resolve("edit_workspace_file").invoke({
            "relative_path": "existing.txt",
            "edits": edits,
        })

    assert target.read_text(encoding="utf-8") == "before"


def test_create_and_edit_project_file_returns_diff(tmp_path):
    created = create_workspace_file(tmp_path, "src/calculator.py", "return a - b\n")
    edited = edit_workspace_file(
        tmp_path,
        "src/calculator.py",
        [{"old_text": "return a - b", "new_text": "return a + b"}],
    )
    assert created["created"] is True
    assert edited["edit_count"] == 1
    assert "-return a - b" in edited["diff"]
    assert "+return a + b" in edited["diff"]
    assert (tmp_path / "src/calculator.py").read_text(encoding="utf-8") == "return a + b\n"
    with pytest.raises(FileExistsError):
        create_workspace_file(tmp_path, "src/calculator.py", "overwrite")


def test_edit_rejects_ambiguous_and_outside_paths(tmp_path):
    (tmp_path / "repeat.py").write_text("pass\npass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="匹配 2 次"):
        edit_workspace_file(
            tmp_path,
            "repeat.py",
            [{"old_text": "pass", "new_text": "return"}],
        )
    with pytest.raises(WorkspacePathError):
        create_workspace_file(tmp_path, "../escape.py", "x")
    with pytest.raises(WorkspacePathError):
        create_workspace_file(tmp_path, ".env", "SECRET=x")


def test_create_and_edit_accept_absolute_path_inside_project(tmp_path):
    target = tmp_path / "nested" / "result.md"
    created = create_workspace_file(tmp_path, str(target), "before\n")
    edited = edit_workspace_file(
        tmp_path,
        str(target),
        [{"old_text": "before", "new_text": "after"}],
    )
    assert created["created"] is True
    assert "+after" in edited["diff"]
    assert target.read_text(encoding="utf-8") == "after\n"


def test_edit_falls_back_to_direct_write_when_windows_replace_is_denied(
    tmp_path, monkeypatch,
):
    target = tmp_path / "locked.md"
    target.write_text("before\n", encoding="utf-8")

    def deny_replace(source, destination):
        raise PermissionError(5, "拒绝访问", str(destination))

    monkeypatch.setattr(
        "data_analysis.tools.project_edit_tools.os.replace",
        deny_replace,
    )

    result = edit_workspace_file(
        tmp_path,
        "locked.md",
        [{"old_text": "before", "new_text": "after"}],
    )

    assert "+after" in result["diff"]
    assert target.read_text(encoding="utf-8") == "after\n"
    assert not list(tmp_path.glob(".locked.md.*.tmp"))


def test_project_file_tools_reject_absolute_path_outside_project(tmp_path):
    outside = tmp_path.parent / "outside.md"
    with pytest.raises(WorkspacePathError, match="超出当前项目目录"):
        create_workspace_file(tmp_path, str(outside), "not allowed")
    with pytest.raises(WorkspacePathError, match="超出当前项目目录"):
        edit_workspace_file(
            tmp_path,
            str(outside),
            [{"old_text": "before", "new_text": "after"}],
        )
    assert not outside.exists()


def test_edit_applies_multiple_replacements_against_original_file(tmp_path):
    target = tmp_path / "settings.py"
    target.write_text(
        'host = "localhost"\nport = 3000\ndebug = False\n',
        encoding="utf-8",
    )

    result = edit_workspace_file(
        tmp_path,
        "settings.py",
        [
            {"old_text": 'host = "localhost"', "new_text": 'host = "0.0.0.0"'},
            {"old_text": "port = 3000", "new_text": "port = 8080"},
        ],
    )

    assert result["edit_count"] == 2
    assert target.read_text(encoding="utf-8") == (
        'host = "0.0.0.0"\nport = 8080\ndebug = False\n'
    )
    assert '-host = "localhost"' in result["diff"]
    assert "+port = 8080" in result["diff"]


def test_edit_is_atomic_when_any_replacement_is_invalid(tmp_path):
    target = tmp_path / "settings.py"
    original = "port = 3000\ndebug = False\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="第 2 项.*匹配 0 次"):
        edit_workspace_file(
            tmp_path,
            "settings.py",
            [
                {"old_text": "port = 3000", "new_text": "port = 8080"},
                {"old_text": "missing = True", "new_text": "missing = False"},
            ],
        )

    assert target.read_text(encoding="utf-8") == original


def test_edit_rejects_overlapping_replacements(tmp_path):
    target = tmp_path / "example.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    with pytest.raises(ValueError, match="修改范围重叠"):
        edit_workspace_file(
            tmp_path,
            "example.txt",
            [
                {"old_text": "alpha beta", "new_text": "first"},
                {"old_text": "beta gamma", "new_text": "second"},
            ],
        )

    assert target.read_text(encoding="utf-8") == "alpha beta gamma\n"


def test_edit_preserves_utf8_bom_and_crlf(tmp_path):
    target = tmp_path / "windows.txt"
    target.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")

    edit_workspace_file(
        tmp_path,
        "windows.txt",
        [{"old_text": "first\nsecond", "new_text": "one\ntwo"}],
    )

    assert target.read_bytes() == b"\xef\xbb\xbfone\r\ntwo\r\n"


def test_edit_preserves_lf_on_windows(tmp_path):
    target = tmp_path / "unix.txt"
    target.write_bytes(b"first\nsecond\n")

    edit_workspace_file(
        tmp_path,
        "unix.txt",
        [{"old_text": "first\r\nsecond", "new_text": "one\r\ntwo"}],
    )

    assert target.read_bytes() == b"one\ntwo\n"


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
