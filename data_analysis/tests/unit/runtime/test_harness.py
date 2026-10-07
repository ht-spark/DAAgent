from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from data_analysis.agent import Action
from data_analysis.runtime import (
    ExecutionContext,
    Harness,
    SandboxProtocolError,
    SandboxTimeoutError,
    SubprocessSandbox,
)
from data_analysis.tools import (
    ToolRegistry,
    ToolSpec,
    create_agent_tool_registry,
    create_data_profile_registry,
)


def test_harness_profiles_data_in_subprocess(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1, 2, 3]}).to_csv(path, index=False)
    harness = Harness(create_data_profile_registry([path]))

    observation = harness.execute(
        Action(tool_name="profile_data", arguments={})
    )

    assert observation.success is True
    assert observation.error is None
    assert observation.result["summary"]["row_count"] == 3
    assert observation.duration_ms >= 0


def test_harness_converts_tool_failure_to_observation(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1]}).to_csv(path, index=False)
    harness = Harness(create_data_profile_registry([path]))

    observation = harness.execute(
        Action(
            tool_name="profile_data",
            arguments={"data_paths": ["other.csv"]},
        )
    )

    assert observation.success is False
    assert "ToolArgumentsError" in observation.error
    assert observation.result is None


def test_harness_converts_unknown_tool_to_observation() -> None:
    observation = Harness(ToolRegistry()).execute(
        Action(tool_name="missing", arguments={})
    )

    assert observation.success is False
    assert "工具未注册" in observation.error


def test_sandbox_converts_timeout(monkeypatch, tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1]}).to_csv(path, index=False)
    tool = create_data_profile_registry([path]).resolve("profile_data")

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=0.01)

    monkeypatch.setattr(
        "data_analysis.runtime.sandbox.run_cancellable_process", raise_timeout
    )

    with pytest.raises(SandboxTimeoutError, match="超过"):
        SubprocessSandbox(timeout_seconds=0.01).execute(tool, {})


def test_sandbox_rejects_invalid_worker_response(monkeypatch, tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1]}).to_csv(path, index=False)
    tool = create_data_profile_registry([path]).resolve("profile_data")

    def return_invalid(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["python"],
            returncode=0,
            stdout="不是 JSON",
            stderr="",
        )

    monkeypatch.setattr(
        "data_analysis.runtime.sandbox.run_cancellable_process", return_invalid
    )

    with pytest.raises(SandboxProtocolError, match="合法 JSON"):
        SubprocessSandbox().execute(tool, {})


def test_harness_executes_generated_python_in_subprocess(tmp_path) -> None:
    registry = create_agent_tool_registry([], tmp_path / "output")
    harness = Harness(registry)

    observation = harness.execute(
        Action(
            tool_name="execute_python",
            arguments={"code": "print(6 * 7)\nresult = {'answer': 42}"},
        )
    )

    assert observation.success is True
    assert observation.result["stdout"] == "42\n"
    assert observation.result["result"] == {"answer": 42}


def test_harness_returns_generated_code_failure_for_rethinking(tmp_path) -> None:
    registry = create_agent_tool_registry([], tmp_path / "output")

    observation = Harness(registry).execute(
        Action(
            tool_name="execute_python",
            arguments={"code": "result = missing_name"},
        )
    )

    assert observation.success is False
    assert "NameError" in observation.error


def test_harness_writes_artifact_in_subprocess(tmp_path) -> None:
    output_dir = tmp_path / "output"
    registry = create_agent_tool_registry([], output_dir)

    observation = Harness(registry).execute(
        Action(
            tool_name="write_artifact",
            arguments={
                "relative_path": "reports/result.md",
                "content": "# 运行结果\n\n已完成。",
            },
        )
    )

    assert observation.success is True
    assert observation.result["path"] == "reports/result.md"
    assert (output_dir / "reports" / "result.md").read_text(
        encoding="utf-8"
    ) == "# 运行结果\n\n已完成。"

def test_sandbox_runs_workspace_tool_from_external_project_directory(tmp_path) -> None:
    workspace = tmp_path / "external-project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("项目资料", encoding="utf-8")
    registry = create_agent_tool_registry(
        [],
        workspace / "outputs" / "session-1",
        workspace_root=workspace,
    )
    sandbox = SubprocessSandbox(working_directory=workspace)

    result = sandbox.execute(
        registry.resolve("list_workspace_files"),
        {"pattern": "*.txt"},
    )

    assert result["files"][0]["path"] == "notes.txt"


def test_harness_returns_nonzero_terminal_command_for_rethinking(tmp_path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry = create_agent_tool_registry(
        [], workspace / "outputs", workspace_root=workspace,
    )
    observation = Harness(
        registry, sandbox=SubprocessSandbox(working_directory=workspace),
        terminal_approval=lambda _action: True,
    ).execute(Action(
        tool_name="run_terminal_command", arguments={"command": "exit 7"},
    ))
    assert observation.success is False
    assert observation.result["exit_code"] != 0
    assert "命令退出码" in observation.error


def test_harness_rejects_terminal_command_without_approval(tmp_path) -> None:
    registry = create_agent_tool_registry(
        [], tmp_path / "outputs", workspace_root=tmp_path,
    )
    observation = Harness(registry).execute(Action(
        tool_name="run_terminal_command", arguments={"command": "echo blocked"},
    ))
    assert observation.success is False
    assert observation.result is None
    assert "未获人工批准" in observation.error


def test_terminal_approval_is_consumed_before_tool_resolution(tmp_path) -> None:
    """即使注册表配置错误，终端动作也必须先经过人工审批门禁。"""
    requested: list[str] = []
    observation = Harness(
        ToolRegistry(),
        terminal_approval=lambda action: requested.append(action.tool_name) or False,
    ).execute(Action(
        tool_name="run_terminal_command",
        arguments={"command": "echo blocked"},
    ))

    assert requested == ["run_terminal_command"]
    assert observation.success is False
    assert "未获人工批准" in observation.error


def test_harness_inherits_run_python_environment(tmp_path) -> None:
    captured: dict[str, object] = {}

    def handler(arguments, context):
        captured["arguments"] = dict(arguments)
        captured["context"] = dict(context)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register_in_process(
        ToolSpec(name="execute_python", description="测试工具"),
        handler,
    )
    from data_analysis.python_environment import PythonEnvironment

    runtime_context = ExecutionContext(
        output_dir=tmp_path / "output",
        workspace_root=tmp_path,
        python_environment=PythonEnvironment(
            executable=Path(sys.executable).resolve(),
            source="project",
            name=".venv",
        ),
    )
    observation = Harness(
        registry,
        execution_context=runtime_context,
    ).execute(Action(tool_name="execute_python", arguments={"code": "result = 1"}))

    assert observation.success is True
    assert captured["arguments"]["python_environment"] == str(Path(sys.executable).resolve())
    assert captured["context"]["output_dir"] == str((tmp_path / "output").resolve())
