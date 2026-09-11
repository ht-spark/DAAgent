from __future__ import annotations

import json

from KnowledgeBase.chunk import (
    _split_markdown_sections,
    build_sentence_window_nodes,
    chunk_knowledge_documents,
)


def test_sentence_window_chunking_loads_only_markdown(tmp_path):
    documents_root = tmp_path / "documents"
    documents_root.mkdir()
    (documents_root / "reference.md").write_text(
        "First sentence. Second sentence. Third sentence.",
        encoding="utf-8",
    )
    (documents_root / "ignored.txt").write_text("not prepared markdown", encoding="utf-8")
    (documents_root / ".uploads.json").write_text("[]", encoding="utf-8")

    nodes = build_sentence_window_nodes(
        documents_root,
        window_size=1,
        document_ids_by_source={"reference.md": "document-uuid"},
        knowledge_base_ids_by_source={"reference.md": "market-kb"},
    )

    assert len(nodes) == 3
    assert {node.metadata["source_file"] for node in nodes} == {"reference.md"}
    assert all("window" in node.metadata for node in nodes)
    assert all("original_text" in node.metadata for node in nodes)
    assert all(node.metadata["document_id"] == "document-uuid" for node in nodes)
    assert all(node.metadata["knowledge_base_id"] == "market-kb" for node in nodes)
    assert [node.metadata["chunk_index"] for node in nodes] == [1, 2, 3]
    assert all(node.metadata["embedding_text"].endswith(node.text) for node in nodes)


def test_sentence_window_chunking_inherits_markdown_heading_metadata(tmp_path):
    documents_root = tmp_path / "documents"
    documents_root.mkdir()
    (documents_root / "reference.md").write_text(
        "# Modeling Report\n\nIntroduction sentence.\n\n## Model Assumptions\n\nParameters are independent. Another sentence.",
        encoding="utf-8",
    )

    markdown = (documents_root / "reference.md").read_text(encoding="utf-8")
    sections = _split_markdown_sections(
        markdown,
        {"source_file": "reference.md"},
        default_title="reference",
    )
    assert sections[0].metadata["document_title"] == "Modeling Report"

    nodes = build_sentence_window_nodes(documents_root, window_size=1)

    assert len(nodes) == 3
    assert nodes[0].metadata["document_title"] == "Modeling Report"
    assert nodes[0].metadata["heading_path"] == ["Modeling Report"]
    assert nodes[1].metadata["section_title"] == "Model Assumptions"
    assert nodes[1].metadata["heading_path"] == ["Modeling Report", "Model Assumptions"]
    assert "章节：Modeling Report > Model Assumptions" in nodes[1].metadata["embedding_text"]


def test_chunk_knowledge_documents_writes_jsonl_nodes(tmp_path):
    documents_root = tmp_path / "documents"
    chunks_root = tmp_path / "chunks"
    documents_root.mkdir()
    (documents_root / "reference.md").write_text(
        "One sentence. Another sentence.", encoding="utf-8"
    )

    output_path = chunk_knowledge_documents(
        documents_root,
        chunks_root,
        window_size=1,
        document_ids_by_source={"reference.md": "document-uuid"},
    )

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert output_path.name == "sentence_window_nodes.jsonl"
    assert len(rows) == 2
    assert rows[0]["metadata"]["source_file"] == "reference.md"
    assert rows[0]["metadata"]["document_id"] == "document-uuid"
    assert rows[0]["metadata"]["embedding_text"]
