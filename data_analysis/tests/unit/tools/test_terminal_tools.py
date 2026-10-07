"""Pi 风格项目终端工具测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from data_analysis.tools import terminal_tools
from data_analysis.tools.terminal_tools import (
    run_terminal_command,
    run_terminal_command_tool,
)


def _python_shell(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    """让测试使用当前 Python 模拟 Shell，避免依赖操作系统安装情况。"""
    monkeypatch.setattr(
        terminal_tools,
        "_shell_argv",
        lambda _shell, _command: ([sys.executable, "-c", code], "test-shell"),
    )


def test_terminal_executes_unrestricted_command_and_audits_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终端不再使用旧白名单，并保留命令前后文件审计。"""
    code = (
        "from pathlib import Path; "
        "Path('generated.txt').write_text('created', encoding='utf-8'); "
        "print('first'); print('second')"
    )
    _python_shell(monkeypatch, code)

    result = run_terminal_command(
        tmp_path,
        "custom-command --flag && another-command",
    )

    assert result["exit_code"] == 0
    assert result["shell"] == "test-shell"
    assert result["output"].splitlines() == ["first", "second"]
    assert result["changed_files"] == ["generated.txt"]
    assert result["changes_truncated"] is False


def test_terminal_merges_stderr_into_output_in_process_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = (
        "import sys; "
        "print('stdout-1', flush=True); "
        "print('stderr-1', file=sys.stderr, flush=True); "
        "print('stdout-2', flush=True)"
    )
    _python_shell(monkeypatch, code)

    result = run_terminal_command(tmp_path, "ignored")

    assert result["output"].splitlines() == ["stdout-1", "stderr-1", "stdout-2"]


def test_terminal_truncates_long_output_and_keeps_full_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _python_shell(monkeypatch, "print('x' * 200)")
    monkeypatch.setattr(terminal_tools, "MAX_TERMINAL_OUTPUT_BYTES", 32)

    result = run_terminal_command(tmp_path, "ignored")

    full_output_path = Path(result["full_output_path"])
    try:
        assert result["output_truncated"] is True
        assert len(result["output"].encode("utf-8")) <= 32
        assert full_output_path.read_text(encoding="utf-8") == "x" * 200 + "\n"
    finally:
        full_output_path.unlink(missing_ok=True)


def test_terminal_timeout_kills_process_tree_and_returns_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _python_shell(monkeypatch, "import time; time.sleep(5)")

    result = run_terminal_command(tmp_path, "ignored", timeout_seconds=0.05)

    assert result["exit_code"] == -1
    assert result["timed_out"] is True
    assert "超时" in result["output"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"command": ""}, "不能为空"),
        ({"command": "echo ok", "shell": "bash"}, "未声明参数"),
        ({"command": "echo ok", "timeout_seconds": 0}, "必须大于 0"),
        ({"command": "echo ok", "unexpected": True}, "未声明参数"),
    ],
)
def test_terminal_tool_rejects_invalid_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        run_terminal_command_tool(arguments, {"workspace_root": str(tmp_path)})


def test_terminal_tool_uses_configured_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(workspace_root, command, *, shell, timeout_seconds):
        captured.update(
            workspace_root=workspace_root,
            command=command,
            shell=shell,
            timeout_seconds=timeout_seconds,
        )
        return {"exit_code": 0}

    monkeypatch.setattr(terminal_tools, "run_terminal_command", fake_run)

    run_terminal_command_tool(
        {"command": "echo ok"},
        {"workspace_root": str(tmp_path), "terminal_shell": "git-bash"},
    )

    assert captured["shell"] == "git-bash"


def test_git_bash_and_bash_use_separate_resolvers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal_tools, "_find_bash", lambda: "system-bash")
    monkeypatch.setattr(terminal_tools, "_find_git_bash", lambda: "git-bash")

    assert terminal_tools._shell_argv("bash", "echo ok")[0][0] == "system-bash"
    assert terminal_tools._shell_argv("git-bash", "echo ok")[0][0] == "git-bash"
