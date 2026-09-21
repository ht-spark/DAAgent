"""document_tools 文档转换单元测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from scr.tools import document_tools as dt


def _make_docx(path: Path) -> Path:
    """用 python-docx 生成一个最小 Word 文档，避免依赖外部样例文件。"""
    from docx import Document

    document = Document()
    document.add_heading("数据说明", level=1)
    document.add_paragraph("附件包含 5 个城市的 GDP 与人口数据。")
    document.save(str(path))
    return path


class TestValidate:
    def test_whitelist_rejects_legacy_doc(self, tmp_path: Path):
        """旧版 .doc 不在白名单内（MarkItDown 只支持 .docx）。"""
        target = tmp_path / "legacy.doc"
        target.write_bytes(b"legacy binary")
        assert dt.is_supported_document(target) is False
        with pytest.raises(ValueError):
            dt.to_markdown(target)

    def test_bytes_entry_rejects_unsupported_extension(self):
        with pytest.raises(ValueError):
            dt.to_markdown_from_bytes(b"abc", "note.doc")

    def test_empty_document_is_rejected(self, tmp_path: Path):
        target = tmp_path / "empty.txt"
        target.write_bytes(b"")
        with pytest.raises(dt.DocumentConversionError, match="内容为空"):
            dt.to_markdown(target)

    def test_size_limit_is_enforced(self, tmp_path: Path):
        target = tmp_path / "big.txt"
        target.write_text("x" * 64, encoding="utf-8")
        with pytest.raises(dt.DocumentConversionError, match="超过上限"):
            dt.to_markdown_from_bytes(target.read_bytes(), "big.txt", max_bytes=16)

    def test_binary_extension_with_mismatched_content_is_rejected(self):
        """扩展名是二进制格式但内容不是：必须判失败，不能被当作纯文本"转换成功"。"""
        with pytest.raises(dt.DocumentConversionError, match="内容与扩展名"):
            dt.to_markdown_from_bytes(b"this is not a real pdf", "broken.pdf")

    def test_pdf_header_within_leading_bytes_is_accepted(self, tmp_path: Path):
        """文件头前允许有少量前导字节（PDF 规范允许），不应被头部校验拦下。"""
        target = tmp_path / "leading.pdf"
        target.write_bytes(b"\n\n%PDF-1.4\n" + b"x" * 64)
        assert dt.is_supported_document(target)
        try:
            dt.to_markdown(target)
        except dt.DocumentConversionError as exc:
            # 允许在解析阶段失败，但不允许失败原因是文件头校验
            assert "内容与扩展名" not in str(exc)


class TestConvert:
    def test_docx_path_conversion_keeps_text(self, tmp_path: Path):
        source = _make_docx(tmp_path / "sample.docx")
        markdown = dt.to_markdown(source)
        assert "数据说明" in markdown
        assert "城市的 GDP" in markdown

    def test_bytes_entry_matches_path_entry(self, tmp_path: Path):
        """上传入口（内存字节流）与磁盘入口应得到一致结果。"""
        source = _make_docx(tmp_path / "sample.docx")
        assert dt.to_markdown_from_bytes(source.read_bytes(), source.name) == dt.to_markdown(source)

    def test_missing_file_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            dt.to_markdown(tmp_path / "absent.pdf")
