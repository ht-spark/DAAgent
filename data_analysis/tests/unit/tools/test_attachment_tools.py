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
    assert "edit_attachment_file" in {tool.name for tool in registry.specs()}
    result = registry.resolve("read_attachment_file").invoke(
        {"file_path": str(attachment.resolve())}
    )
    assert result["file_path"] == str(attachment.resolve())
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

    result = tool.invoke({"file_path": str(attachment.resolve()), "offset": 2, "limit": 1})
    assert result["content"].splitlines() == ["second"]
    assert result["truncated"] is True
    with pytest.raises(PermissionError):
        tool.invoke({"file_path": str((tmp_path / "other.py").resolve())})
    with pytest.raises(ValueError):
        tool.invoke({"file_path": ""})


def test_attachment_reader_not_registered_without_text_upload(tmp_path):
    registry = create_agent_tool_registry([], tmp_path / "output")
    assert "read_attachment_file" not in registry
    assert "edit_attachment_file" not in registry


def test_attachment_editor_outputs_one_updated_copy_with_original_name(tmp_path):
    attachment = tmp_path / "input" / "notes.md"
    attachment.parent.mkdir()
    original = "# 标题\r\n\r\n旧段落\r\n结尾\r\n"
    attachment.write_bytes(b"\xef\xbb\xbf" + original.encode("utf-8"))
    output_dir = tmp_path / "output"
    registry = create_agent_tool_registry([attachment], output_dir)

    result = registry.resolve("edit_attachment_file").invoke({
        "file_path": str(attachment.resolve()),
        "edits": [
            {"old_text": "旧段落", "new_text": "更新后的段落"},
            {"old_text": "结尾", "new_text": "新的结尾"},
        ],
    })

    assert result["path"] == "notes.md"
    assert result["edit_count"] == 2
    assert "+更新后的段落" in result["diff"]
    assert list(path.name for path in output_dir.iterdir()) == ["notes.md"]
    assert (output_dir / "notes.md").read_bytes() == (
        b"\xef\xbb\xbf" + original.replace("旧段落", "更新后的段落")
        .replace("结尾", "新的结尾")
        .encode("utf-8")
    )
    assert attachment.read_bytes() == b"\xef\xbb\xbf" + original.encode("utf-8")


@pytest.mark.parametrize(
    "arguments",
    [
        {"file_path": "missing", "edits": []},
        {"file_path": "missing", "edits": [{"old_text": "x"}]},
        {
            "file_path": "missing",
            "edits": [{"old_text": "x", "new_text": "y", "extra": True}],
        },
    ],
)
def test_attachment_editor_rejects_malformed_edits(tmp_path, arguments):
    attachment = tmp_path / "input" / "notes.md"
    attachment.parent.mkdir()
    attachment.write_text("原文", encoding="utf-8")
    registry = create_agent_tool_registry([attachment], tmp_path / "output")

    with pytest.raises(ValueError, match="参数无效"):
        registry.resolve("edit_attachment_file").invoke(arguments)


def test_agent_harness_reads_uploaded_python_without_workspace(tmp_path):
    attachment = tmp_path / "input" / "data_loader.py"
    attachment.parent.mkdir()
    attachment.write_text("def load_data():\n    return [1, 2]\n", encoding="utf-8")
    registry = create_agent_tool_registry([attachment], tmp_path / "output")

    observation = Harness(
        registry, sandbox=SubprocessSandbox(working_directory=tmp_path)
    ).execute(Action(
        tool_name="read_attachment_file",
        arguments={"file_path": str(attachment.resolve())},
    ))

    assert observation.success is True
    assert "def load_data()" in observation.result["content"]
