from __future__ import annotations

import subprocess

import pandas as pd
import pytest

from data_analysis.agent import Action
from data_analysis.runtime import (
    Harness,
    SandboxProtocolError,
    SandboxTimeoutError,
    SubprocessSandbox,
)
from data_analysis.tools import ToolRegistry, create_data_profile_registry


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

    monkeypatch.setattr(subprocess, "run", raise_timeout)

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

    monkeypatch.setattr(subprocess, "run", return_invalid)

    with pytest.raises(SandboxProtocolError, match="合法 JSON"):
        SubprocessSandbox().execute(tool, {})
