"""Tests for the local hybrid knowledge retrieval pipeline."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from KnowledgeBase import main as retrieval


class FakeEmbeddingModel:
    """Embedding stub that records the query passed to it."""

    def __init__(self) -> None:
        self.questions: list[str] = []

    def embed_query(self, question: str) -> list[float]:
        self.questions.append(question)
        return [0.1, 0.2]


class FakeQdrantClient:
    """Minimal Qdrant substitute for deterministic retrieval tests."""

    records = [
        (1, {"text": "销量增长与用户复购", "window": "销量增长与用户复购", "source_file": "sales.md"}),
        (2, {"text": "库存周转和供应链成本", "window": "库存周转和供应链成本", "source_file": "supply.md"}),
        (3, {"text": "客户留存和复购率", "window": "客户留存和复购率", "source_file": "customer.md"}),
    ]

    def __init__(self, *, path: str) -> None:
        self.path = path

    def collection_exists(self, _: str) -> bool:
        return True

    def query_points(self, **_: object) -> SimpleNamespace:
        points = [
            SimpleNamespace(id=3, payload=self.records[2][1]),
            SimpleNamespace(id=1, payload=self.records[0][1]),
        ]
        return SimpleNamespace(points=points)

    def scroll(self, **_: object) -> tuple[list[SimpleNamespace], None]:
        return ([SimpleNamespace(id=point_id, payload=payload) for point_id, payload in self.records], None)

    def close(self) -> None:
        pass


def test_retrieve_uses_original_question_and_fuses_dense_bm25(monkeypatch, tmp_path):
    """The user question directly drives both branches before RRF fusion."""
    model = FakeEmbeddingModel()
    monkeypatch.setattr(retrieval, "QdrantClient", FakeQdrantClient)

    chunks = retrieval.retrieve(
        "客户留存",
        model=model,
        qdrant_path=tmp_path / "qdrant",
    )

    assert model.questions == ["客户留存"]
    assert [chunk.point_id for chunk in chunks[:2]] == [3, 1]
    assert chunks[0].dense_rank == 1
    assert chunks[0].bm25_rank == 1
    assert chunks[0].rrf_score > chunks[1].rrf_score


def test_bm25_recall_uses_embedding_text_and_excludes_zero_scores():
    """BM25 uses the same structure-aware text as the vector index."""
    records = [
        (1, {"text": "库存管理", "embedding_text": "文档：客户复购\n正文：复购率"}),
        (2, {"text": "客户复购分析", "embedding_text": "文档：库存\n正文：周转"}),
    ]

    hits = retrieval._bm25_recall("复购率", records)

    assert [point_id for point_id, _ in hits] == [1]


def test_retrieve_rejects_blank_question():
    """A blank question must not reach the embedding model."""
    with pytest.raises(ValueError, match="不能为空"):
        retrieval.retrieve("   ", model=FakeEmbeddingModel())


def test_retrieve_filters_dense_and_bm25_candidates_to_target_base(tmp_path):
    qdrant_path = tmp_path / "qdrant"
    client = QdrantClient(path=str(qdrant_path))
    client.create_collection(
        collection_name="routed",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    client.upsert(
        collection_name="routed",
        points=[
            PointStruct(
                id=1,
                vector=[1.0, 0.0],
                payload={"text": "默认库内容", "source_file": "default.md"},
            ),
            PointStruct(
                id=2,
                vector=[1.0, 0.0],
                payload={
                    "text": "市场库内容",
                    "source_file": "market.md",
                    "knowledge_base_id": "market-kb",
                },
            ),
        ],
    )
    client.close()

    market = retrieval.retrieve(
        "内容",
        model=FakeEmbeddingModel(),
        qdrant_path=qdrant_path,
        collection_name="routed",
        knowledge_base_id="market-kb",
    )
    default = retrieval.retrieve(
        "内容",
        model=FakeEmbeddingModel(),
        qdrant_path=qdrant_path,
        collection_name="routed",
    )
    combined = retrieval.retrieve(
        "内容",
        model=FakeEmbeddingModel(),
        qdrant_path=qdrant_path,
        collection_name="routed",
        knowledge_base_ids=["default", "market-kb"],
    )

    assert [chunk.source_file for chunk in market] == ["market.md"]
    assert [chunk.source_file for chunk in default] == ["default.md"]
    assert {chunk.source_file for chunk in combined} == {"default.md", "market.md"}


def test_llm_chain_filter_keeps_only_selected_chunks(monkeypatch):
    """Compression uses LangChain Classic's LLMChainFilter output."""
    chunks = [
        retrieval.RetrievedChunk(1, "无关内容", "窗口一", "a.md", "", "a", 1, None, 0.1),
        retrieval.RetrievedChunk(2, "相关内容", "窗口二", "b.md", "", "b", 2, None, 0.09),
    ]

    class FakeFilter:
        @classmethod
        def from_llm(cls, llm):
            assert llm is fake_llm
            return cls()

        def compress_documents(self, documents, question):
            assert question == "问题"
            return [documents[1]]

    from langchain_classic.retrievers import document_compressors

    fake_llm = object()
    monkeypatch.setattr(document_compressors, "LLMChainFilter", FakeFilter)

    retained = retrieval._compress_with_llm("问题", chunks, fake_llm)

    assert retained == [chunks[1]]


def test_llm_compression_keeps_rrf_results_when_model_is_unavailable():
    """A temporary compression-model failure must not abort knowledge retrieval."""
    chunks = [
        retrieval.RetrievedChunk(1, "候选内容", "窗口", "a.md", "", "a", 1, None, 0.1),
    ]

    class UnavailableLlm:
        def invoke(self, _messages):
            raise RuntimeError("502")

    assert retrieval._compress_with_llm("问题", chunks, UnavailableLlm()) == chunks
