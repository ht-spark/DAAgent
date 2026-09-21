"""文档转换接口与历史存储单元测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from server import converter_store, main
from server.schemas import DocumentSaveBody


@pytest.fixture
def temp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把转换历史库指向临时目录，避免污染本机 converter.db。"""
    monkeypatch.setattr(converter_store, "_DB_PATH", tmp_path / "converter.db")
    monkeypatch.setattr(converter_store, "_conn", None)
    yield converter_store
    if converter_store._conn is not None:
        converter_store._conn.close()
    monkeypatch.setattr(converter_store, "_conn", None)


class TestSafeMarkdownFilename:
    """保存文件名清洗：去掉路径成分、非法字符，并强制 .md 后缀。"""

    def test_strips_path_components(self):
        assert main._safe_markdown_filename("../../etc/passwd") == "passwd.md"

    def test_replaces_illegal_chars_and_keeps_suffix(self):
        assert main._safe_markdown_filename("题目?.md") == "题目_.md"

    def test_rejects_blank_name(self):
        with pytest.raises(HTTPException):
            main._safe_markdown_filename("   .md  ")


class TestConverterStore:
    def test_add_list_read_delete(self, temp_store):
        record = temp_store.add_record(
            filename="a.pdf",
            ok=True,
            char_count=5,
            size_bytes=10,
            elapsed_ms=12,
            markdown="# hi",
        )
        assert record["ok"] is True
        assert record["converted_at"]

        records = temp_store.list_records()
        assert [item["filename"] for item in records] == ["a.pdf"]
        # 列表只返回元数据，正文单独读取
        assert "markdown" not in records[0]
        assert temp_store.read_markdown(record["id"]) == "# hi"

        assert temp_store.delete_record(record["id"]) is True
        assert temp_store.list_records() == []

    def test_records_keep_conversion_order(self, temp_store):
        for name in ("1.pdf", "2.pdf", "3.pdf"):
            temp_store.add_record(filename=name, ok=True, markdown="x")
        assert [item["filename"] for item in temp_store.list_records()] == [
            "1.pdf",
            "2.pdf",
            "3.pdf",
        ]

    def test_failed_record_keeps_error_without_content(self, temp_store):
        record = temp_store.add_record(filename="bad.pdf", ok=False, error="文档内容为空")
        assert record["ok"] is False
        assert record["error"] == "文档内容为空"
        assert temp_store.read_markdown(record["id"]) == ""


class TestFormatsEndpoint:
    def test_exposes_extensions_and_default_save_dir(self):
        payload = asyncio.run(main.list_convertible_formats_endpoint())
        assert ".pdf" in payload["extensions"]
        assert Path(str(payload["default_save_dir"])).is_absolute()


class TestHistoryEndpoints:
    def test_list_read_and_delete(self, temp_store):
        record = temp_store.add_record(filename="a.pdf", ok=True, markdown="# hi")

        listed = asyncio.run(main.list_conversion_history_endpoint())
        assert [item["id"] for item in listed["records"]] == [record["id"]]

        content = asyncio.run(main.read_conversion_content_endpoint(record["id"]))
        assert content.body.decode("utf-8") == "# hi"

        deleted = asyncio.run(main.delete_conversion_record_endpoint(record["id"]))
        assert deleted["deleted_id"] == record["id"]
        assert temp_store.list_records() == []

    def test_unknown_record_returns_404(self, temp_store):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.read_conversion_content_endpoint(9999))
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.delete_conversion_record_endpoint(9999))
        assert exc.value.status_code == 404


class TestSaveEndpoint:
    def test_writes_record_content_into_target_directory(self, temp_store, tmp_path: Path):
        record = temp_store.add_record(filename="题目.pdf", ok=True, markdown="# 标题\n\n正文")
        body = DocumentSaveBody(
            record_id=record["id"],
            filename="题目.md",
            directory=str(tmp_path),
        )
        result = asyncio.run(main.save_converted_markdown_endpoint(body))

        saved = Path(str(result["saved_path"]))
        assert saved.parent == tmp_path
        assert saved.name == "题目.md"
        assert saved.read_text(encoding="utf-8") == "# 标题\n\n正文"
        assert result["bytes"] > 0

    def test_rejects_unknown_record(self, temp_store, tmp_path: Path):
        body = DocumentSaveBody(record_id=9999, filename="a.md", directory=str(tmp_path))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.save_converted_markdown_endpoint(body))
        assert exc.value.status_code == 404

    def test_rejects_missing_directory(self, temp_store, tmp_path: Path):
        record = temp_store.add_record(filename="a.pdf", ok=True, markdown="x")
        body = DocumentSaveBody(
            record_id=record["id"],
            filename="a.md",
            directory=str(tmp_path / "absent"),
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.save_converted_markdown_endpoint(body))
        assert exc.value.status_code == 400

    def test_rejects_relative_directory(self, temp_store):
        record = temp_store.add_record(filename="a.pdf", ok=True, markdown="x")
        body = DocumentSaveBody(record_id=record["id"], filename="a.md", directory="relative/dir")
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main.save_converted_markdown_endpoint(body))
        assert exc.value.status_code == 400
