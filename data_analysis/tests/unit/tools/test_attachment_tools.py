"""直接读取已授权上传附件的回归测试。"""
from __future__ import annotations

import pytest

from data_analysis.agent import Action
from data_analysis.runtime import Harness, SubprocessSandbox
from data_analysis.tools.registry import create_agent_tool_registry


def test_uploaded_python_file_is_readable_without_project(tmp_path):
    attachment = tmp_path / "input" / "data_loader.py"
    attachment.parent.mkdir()
    attachment.write_text("raise RuntimeError('不得执行')\nvalue = 42\n", encoding="utf-8")
    registry = create_agent_tool_registry([attachment], tmp_path / "output")

    assert "read_workspace_file" not in registry
    result = registry.resolve("read_attachment_file").invoke(
        {"file_name": "data_loader.py"}
    )
    assert result["content"].startswith("raise RuntimeError")
    assert result["end_line"] == 2
    assert attachment.read_text(encoding="utf-8").endswith("value = 42\n")


def test_attachment_reader_supports_line_windows_and_rejects_unapproved_paths(tmp_path):
    attachment = tmp_path / "input" / "data_loader.py"
    attachment.parent.mkdir()
    attachment.write_text("first\nsecond\nthird\n", encoding="utf-8")
    tool = create_agent_tool_registry([attachment], tmp_path / "output").resolve(
        "read_attachment_file"
    )

    result = tool.invoke({"file_name": "data_loader.py", "offset": 2, "limit": 1})
    assert result["content"].splitlines() == ["second"]
    assert result["truncated"] is True
    with pytest.raises(PermissionError):
        tool.invoke({"file_name": "other.py"})
    with pytest.raises(ValueError):
        tool.invoke({"file_name": "../data_loader.py"})


def test_attachment_reader_not_registered_without_text_upload(tmp_path):
    registry = create_agent_tool_registry([], tmp_path / "output")
    assert "read_attachment_file" not in registry


def test_agent_harness_reads_uploaded_python_without_workspace(tmp_path):
    attachment = tmp_path / "input" / "data_loader.py"
    attachment.parent.mkdir()
    attachment.write_text("def load_data():\n    return [1, 2]\n", encoding="utf-8")
    registry = create_agent_tool_registry([attachment], tmp_path / "output")

    observation = Harness(
        registry, sandbox=SubprocessSandbox(working_directory=tmp_path)
    ).execute(Action(
        tool_name="read_attachment_file",
        arguments={"file_name": "data_loader.py"},
    ))

    assert observation.success is True
    assert "def load_data()" in observation.result["content"]