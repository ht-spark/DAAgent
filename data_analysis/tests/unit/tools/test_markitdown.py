"""文档转换共享实现与普通工具入口迁移的回归测试。"""
from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from data_analysis.tools.markitdown import convert_attachment_to_markdown
from data_analysis.tools.registry import create_agent_tool_registry


def _make_docx(path: Path, paragraphs: int = 3) -> Path:
    """生成一个最小 Word 附件，避免依赖外部样例文件。"""
    from docx import Document

    document = Document()
    document.add_heading("数据说明", level=1)
    for index in range(paragraphs):
        document.add_paragraph(f"第 {index} 段：附件包含城市经济数据，用于建模。")
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return path


class TestRegistration:
    def test_conversion_is_a_skill_not_an_ordinary_tool(self, tmp_path: Path):
        attachment = _make_docx(tmp_path / "input" / "题目.docx")
        registry = create_agent_tool_registry([attachment], tmp_path / "output")
        assert "markitdown" not in registry

    def test_not_registered_for_text_only_or_markdown_attachments(self, tmp_path: Path):
        markdown = tmp_path / "input" / "题目.md"
        markdown.parent.mkdir(parents=True)
        markdown.write_text("# 题目\n", encoding="utf-8")
        registry = create_agent_tool_registry([markdown], tmp_path / "output")

        assert "read_attachment_file" in registry
        assert "markitdown" not in registry

    def test_not_registered_without_attachments(self, tmp_path: Path):
        assert "markitdown" not in create_agent_tool_registry([], tmp_path / "output")


def _converter(attachment: Path, output: Path):
    """直接测试共享处理器；普通工具注册表不再公开文档转换。"""
    return partial(
        convert_attachment_to_markdown,
        context={"authorized_paths": [str(attachment.resolve())], "converted_dir": str(output)},
    )


class TestConversion:
    def test_converts_docx_and_returns_content(self, tmp_path: Path):
        attachment = _make_docx(tmp_path / "input" / "题目.docx")
        convert = _converter(attachment, tmp_path / "output")

        result = convert({"file_path": str(attachment.resolve())})

        assert "第 0 段" in result["content"]
        assert result["converted_now"] is True
        assert result["total_lines"] > 0
        # 完整转换结果落盘，供后续分页复用
        assert Path(result["markdown_path"]).is_file()
        assert not result["truncated"]

    def test_second_call_reuses_converted_file(self, tmp_path: Path):
        attachment = _make_docx(tmp_path / "input" / "题目.docx")
        convert = _converter(attachment, tmp_path / "output")

        first = convert({"file_path": str(attachment.resolve())})
        second = convert({"file_path": str(attachment.resolve())})

        assert first["converted_now"] is True
        assert second["converted_now"] is False
        assert first["markdown_path"] == second["markdown_path"]

    def test_line_window_and_next_offset(self, tmp_path: Path):
        attachment = _make_docx(tmp_path / "input" / "题目.docx", paragraphs=20)
        convert = _converter(attachment, tmp_path / "output")

        first = convert({"file_path": str(attachment.resolve()), "limit": 3})
        assert first["truncated"] is True
        assert first["next_offset"] == first["end_line"] + 1

        second = convert(
            {"file_path": str(attachment.resolve()), "offset": first["next_offset"], "limit": 3}
        )
        assert second["start_line"] == first["next_offset"]
        assert second["content"] != first["content"]


class TestGuards:
    def test_rejects_unauthorized_path(self, tmp_path: Path):
        attachment = _make_docx(tmp_path / "input" / "题目.docx")
        convert = _converter(attachment, tmp_path / "output")

        with pytest.raises(PermissionError):
            convert({"file_path": str((tmp_path / "other.docx").resolve())})

    def test_rejects_text_attachment_via_handler(self, tmp_path: Path):
        """纯文本附件应改用 read_attachment_file。

        技能调用共享处理器时仍须拒绝不需要转换的文本附件。
        """
        text = tmp_path / "input" / "题目.txt"
        text.parent.mkdir(parents=True)
        text.write_text("hello", encoding="utf-8")

        with pytest.raises(ValueError, match="纯文本"):
            convert_attachment_to_markdown(
                {"file_path": str(text.resolve())},
                {
                    "authorized_paths": [str(text.resolve())],
                    "converted_dir": str(tmp_path / "converted"),
                },
            )

    def test_rejects_unknown_arguments_and_bad_limit(self, tmp_path: Path):
        attachment = _make_docx(tmp_path / "input" / "题目.docx")
        convert = _converter(attachment, tmp_path / "output")

        with pytest.raises(ValueError, match="markitdown 需要"):
            convert({"file_path": str(attachment.resolve()), "extra": 1})
        with pytest.raises(ValueError, match="limit"):
            convert({"file_path": str(attachment.resolve()), "limit": 9999})

    def test_corrupted_document_reports_failure(self, tmp_path: Path):
        broken = tmp_path / "input" / "损坏.pdf"
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"this is not a real pdf")
        convert = _converter(broken, tmp_path / "output")

        with pytest.raises(RuntimeError, match="内容与扩展名"):
            convert({"file_path": str(broken.resolve())})
