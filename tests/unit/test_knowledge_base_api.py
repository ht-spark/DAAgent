from __future__ import annotations

import asyncio
from types import SimpleNamespace

from KnowledgeBase import upload_file
from server import main
from server.schemas import (
    KnowledgeBaseUpdateBody,
    KnowledgeChunkEmbedRequest,
    KnowledgeDocumentsDeleteBody,
)
from KnowledgeBase.embedding import IndexBuildResult


def test_knowledge_status_lists_archived_documents(monkeypatch, tmp_path):
    """The MVP status endpoint exposes locally archived source documents."""
    documents_root = tmp_path / "documents"
    documents_root.mkdir()
    (documents_root / "reference.md").write_text("reference", encoding="utf-8")
    monkeypatch.setattr(main, "KNOWLEDGE_DOCUMENTS_ROOT", documents_root)
    monkeypatch.setattr("KnowledgeBase.upload_file.DOCUMENTS_ROOT", documents_root)

    status = asyncio.run(main.knowledge_status_endpoint())

    assert status.retrieval_ready is False
    assert [(item.name, item.size_bytes) for item in status.documents] == [
        ("reference.md", 9),
    ]


def test_knowledge_status_prefers_persisted_upload_metadata(monkeypatch, tmp_path):
    """Status restores stable table fields instead of generated Markdown names."""
    documents_root = tmp_path / "documents"
    documents_root.mkdir()
    (documents_root / ".uploads.json").write_text(
        '[{"document_id": 7, "name": "source.pdf", "size_bytes": 12, '
        '"uploaded_at": "2026-08-15T00:00:00+00:00", "upload_success": true, '
        '"output_name": "source.md"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "KNOWLEDGE_DOCUMENTS_ROOT", documents_root)
    monkeypatch.setattr("KnowledgeBase.upload_file.DOCUMENTS_ROOT", documents_root)

    status = asyncio.run(main.knowledge_status_endpoint())

    assert [(item.id, item.name, item.is_markdown) for item in status.documents] == [
        ("7", "source.pdf", False),
    ]


def test_knowledge_status_recovers_conversion_state_from_markdown_output(monkeypatch, tmp_path):
    """Existing converted Markdown makes its source document eligible for chunking."""
    documents_root = tmp_path / "documents"
    documents_root.mkdir()
    (documents_root / "source.md").write_text("# Converted", encoding="utf-8")
    (documents_root / ".uploads.json").write_text(
        '[{"document_id": "document-id", "name": "source.pdf", "size_bytes": 12, '
        '"uploaded_at": "2026-08-15T00:00:00+00:00", "upload_success": true, '
        '"output_name": "source.pdf", "is_markdown": false, "is_conversion": false}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "KNOWLEDGE_DOCUMENTS_ROOT", documents_root)
    monkeypatch.setattr("KnowledgeBase.upload_file.DOCUMENTS_ROOT", documents_root)

    status = asyncio.run(main.knowledge_status_endpoint())

    assert status.documents[0].is_conversion is True
    assert '"output_name": "source.md"' in (documents_root / ".uploads.json").read_text(encoding="utf-8")


def test_delete_knowledge_documents_endpoint_removes_selected_record(monkeypatch, tmp_path):
    raw_root = tmp_path / "raw"
    documents_root = tmp_path / "documents"
    monkeypatch.setattr(upload_file, "RAW_ROOT", raw_root)
    monkeypatch.setattr(upload_file, "DOCUMENTS_ROOT", documents_root)
    prepared = upload_file.prepare_upload("reference.md", b"# Reference\n")[0]
    deleted_vector_ids: set[str] = set()
    refreshed_metadata: dict[str, str] = {}
    monkeypatch.setattr(main, "delete_document_vectors", lambda ids: deleted_vector_ids.update(ids))
    monkeypatch.setattr(
        main,
        "chunk_knowledge_documents",
        lambda **kwargs: refreshed_metadata.update(kwargs["document_ids_by_source"]),
    )

    result = asyncio.run(
        main.delete_knowledge_documents_endpoint(
            KnowledgeDocumentsDeleteBody(document_ids=[prepared.document_id])
        )
    )

    assert result == {"deleted_ids": [prepared.document_id]}
    assert not prepared.path.exists()
    assert upload_file.list_upload_records() == []
    assert deleted_vector_ids == {prepared.document_id}
    assert refreshed_metadata == {}


def test_update_knowledge_base_endpoint_returns_current_totals(monkeypatch):
    base = SimpleNamespace(
        id="default",
        name="通用资料",
        description="已修改",
        created_at="",
        is_default=False,
    )
    records = [
        type("Record", (), {"knowledge_base_id": "default", "indexed_chunks": 3})(),
        type("Record", (), {"knowledge_base_id": "other", "indexed_chunks": 9})(),
    ]
    monkeypatch.setattr(main, "update_knowledge_base", lambda *_args: base)
    monkeypatch.setattr(main, "list_upload_records", lambda: records)

    result = asyncio.run(
        main.update_knowledge_base_endpoint(
            "default",
            KnowledgeBaseUpdateBody(name="通用资料", description="已修改"),
        )
    )

    assert result.name == "通用资料"
    assert result.document_count == 1
    assert result.indexed_chunks == 3


def test_delete_knowledge_base_endpoint_cascades_documents_and_vectors(monkeypatch):
    base = type("Base", (), {"id": "default"})()
    records = [
        type("Record", (), {"document_id": "doc-a", "knowledge_base_id": "default"})(),
        type("Record", (), {"document_id": "doc-b", "knowledge_base_id": "other"})(),
    ]
    calls: dict[str, object] = {}
    monkeypatch.setattr(main, "require_knowledge_base", lambda _id: base)
    monkeypatch.setattr(main, "list_upload_records", lambda: records)
    monkeypatch.setattr(main, "delete_document_vectors", lambda ids: calls.update(vectors=ids))
    monkeypatch.setattr(main, "delete_upload_records", lambda ids: calls.update(files=ids))
    monkeypatch.setattr(main, "chunk_knowledge_documents", lambda **kwargs: calls.update(chunks=kwargs))
    monkeypatch.setattr(main, "delete_knowledge_base", lambda base_id: calls.update(base=base_id))
    monkeypatch.setattr(main, "_document_ids_by_source", lambda: {})
    monkeypatch.setattr(main, "_knowledge_base_ids_by_source", lambda: {})

    result = asyncio.run(main.delete_knowledge_base_endpoint("default"))

    assert result == {"deleted_id": "default", "deleted_document_ids": ["doc-a"]}
    assert calls["vectors"] == {"doc-a"}
    assert calls["files"] == {"doc-a"}
    assert calls["base"] == "default"


def test_delete_knowledge_base_removes_local_files_from_disk(monkeypatch, tmp_path):
    """删除知识库时必须同步删掉 raw 原始文件与 documents Markdown。"""
    from uuid import uuid4

    from KnowledgeBase import catalog as kb_catalog

    raw_root = tmp_path / "raw"
    documents_root = tmp_path / "documents"
    raw_root.mkdir()
    documents_root.mkdir()
    catalog_path = tmp_path / "catalog.json"

    monkeypatch.setattr(upload_file, "RAW_ROOT", raw_root)
    monkeypatch.setattr(upload_file, "DOCUMENTS_ROOT", documents_root)
    monkeypatch.setattr(main, "KNOWLEDGE_DOCUMENTS_ROOT", documents_root)

    doc_markdown = str(uuid4())
    doc_pdf = str(uuid4())
    (raw_root / "note.md").write_text("# note", encoding="utf-8")
    (documents_root / "note.md").write_text("# note", encoding="utf-8")
    (raw_root / "report.pdf").write_bytes(b"%PDF-1.4")
    (documents_root / "report.md").write_text("# report", encoding="utf-8")
    upload_file._write_upload_records_payload(
        [
            {
                "document_id": doc_markdown,
                "name": "note.md",
                "size_bytes": 6,
                "uploaded_at": "2026-01-01T00:00:00+00:00",
                "upload_success": True,
                "raw_name": "note.md",
                "output_name": "note.md",
                "is_markdown": True,
                "is_conversion": True,
                "embedding_status": "pending",
                "indexed_chunks": 0,
                "knowledge_base_id": "target-kb",
            },
            {
                "document_id": doc_pdf,
                "name": "report.pdf",
                "size_bytes": 8,
                "uploaded_at": "2026-01-01T00:00:00+00:00",
                "upload_success": True,
                "raw_name": "report.pdf",
                "output_name": "report.md",
                "is_markdown": False,
                "is_conversion": True,
                "embedding_status": "pending",
                "indexed_chunks": 0,
                "knowledge_base_id": "target-kb",
            },
            {
                "document_id": str(uuid4()),
                "name": "keep.md",
                "size_bytes": 5,
                "uploaded_at": "2026-01-01T00:00:00+00:00",
                "upload_success": True,
                "raw_name": "keep.md",
                "output_name": "keep.md",
                "is_markdown": True,
                "is_conversion": True,
                "embedding_status": "pending",
                "indexed_chunks": 0,
                "knowledge_base_id": "other-kb",
            },
        ]
    )
    (raw_root / "keep.md").write_text("# keep", encoding="utf-8")
    (documents_root / "keep.md").write_text("# keep", encoding="utf-8")

    base = kb_catalog.create_knowledge_base("目标库", description="", catalog_path=catalog_path)
    # create_knowledge_base 生成新 id；把清单里的目标库改成同一 id
    records = upload_file.list_upload_records()
    rewritten = []
    for record in records:
        rewritten.append(
            {
                "document_id": record.document_id,
                "name": record.name,
                "size_bytes": record.size_bytes,
                "uploaded_at": record.uploaded_at,
                "upload_success": record.upload_success,
                "raw_name": record.raw_name,
                "output_name": record.output_name,
                "is_markdown": record.is_markdown,
                "is_conversion": record.is_conversion,
                "embedding_status": record.embedding_status,
                "indexed_chunks": record.indexed_chunks,
                "knowledge_base_id": (
                    base.id if record.knowledge_base_id == "target-kb" else record.knowledge_base_id
                ),
            }
        )
    upload_file._write_upload_records_payload(rewritten)

    monkeypatch.setattr(
        main,
        "require_knowledge_base",
        lambda _id: kb_catalog.require_knowledge_base(base.id, catalog_path),
    )
    monkeypatch.setattr(
        main,
        "delete_knowledge_base",
        lambda base_id: kb_catalog.delete_knowledge_base(base_id, catalog_path),
    )
    monkeypatch.setattr(main, "delete_document_vectors", lambda _ids: None)
    monkeypatch.setattr(main, "chunk_knowledge_documents", lambda **_kwargs: None)
    monkeypatch.setattr(main, "_document_ids_by_source", lambda: {})
    monkeypatch.setattr(main, "_knowledge_base_ids_by_source", lambda: {})

    result = asyncio.run(main.delete_knowledge_base_endpoint(base.id))

    assert set(result["deleted_document_ids"]) == {doc_markdown, doc_pdf}
    # 目标库本地文件已删除
    assert not (raw_root / "note.md").exists()
    assert not (raw_root / "report.pdf").exists()
    assert not (documents_root / "note.md").exists()
    assert not (documents_root / "report.md").exists()
    # 其他知识库文件保留
    assert (raw_root / "keep.md").exists()
    assert (documents_root / "keep.md").exists()
    remaining_ids = {item.knowledge_base_id for item in upload_file.list_upload_records()}
    assert base.id not in remaining_ids


def test_chunk_embed_endpoint_returns_actual_index_counts(monkeypatch, tmp_path):
    documents_root = tmp_path / "documents"
    documents_root.mkdir()
    monkeypatch.setattr(upload_file, "DOCUMENTS_ROOT", documents_root)
    prepared = upload_file.prepare_upload("reference.md", b"# Reference\n")[0]
    chunk_kwargs_history: list[dict] = []

    def chunk_documents(**kwargs):
        assert main._get_knowledge_processing_progress().stage == "chunking"
        chunk_kwargs_history.append(kwargs)
        return tmp_path / "nodes.jsonl"

    def build_index():
        assert main._get_knowledge_processing_progress().stage == "embedding"
        return IndexBuildResult(
            collection_name="knowledge_chunks",
            indexed_nodes=3,
            vector_size=512,
            qdrant_path=tmp_path / "qdrant_db",
            source_chunk_counts={prepared.name: 3},
        )

    monkeypatch.setattr(main, "chunk_knowledge_documents", lambda **kwargs: chunk_documents(**kwargs))
    monkeypatch.setattr(
        main,
        "build_local_qdrant_index",
        build_index,
    )

    default_result = asyncio.run(main.chunk_and_embed_knowledge_endpoint())
    body_result = asyncio.run(
        main.chunk_and_embed_knowledge_endpoint(KnowledgeChunkEmbedRequest(window_size=3))
    )

    assert default_result.chunks_indexed == 3
    assert body_result.vector_size == 512
    assert body_result.document_chunks == {prepared.document_id: 3}
    assert main._get_knowledge_processing_progress().stage == "done"
    assert [kwargs["window_size"] for kwargs in chunk_kwargs_history] == [
        main.DEFAULT_WINDOW_SIZE,
        3,
    ]
    assert upload_file.list_upload_records()[0].embedding_status == "embedded"
    assert upload_file.list_upload_records()[0].indexed_chunks == 3


def test_brainstorm_discussion_endpoints_restore_saved_messages(monkeypatch):
    """Saved discussions can be listed and loaded for continued chat."""
    discussion = {
        "id": "discussion-uuid",
        "title": "如何建模？",
        "updated_at": "2026-08-18T00:00:00+00:00",
        "messages": [{"role": "user", "content": "如何建模？", "sources": []}],
    }
    monkeypatch.setattr(main, "list_discussions", lambda: [discussion])
    monkeypatch.setattr(main, "get_discussion", lambda _: discussion)

    summaries = asyncio.run(main.list_brainstorm_discussions_endpoint())
    restored = asyncio.run(main.get_brainstorm_discussion_endpoint("discussion-uuid"))

    assert summaries[0].title == "如何建模？"
    assert restored.messages[0].content == "如何建模？"
