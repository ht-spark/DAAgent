"""请求 / 响应数据契约。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentCommandDecisionBody(BaseModel):
    """用户对单次 Agent 终端命令的批准决定。"""

    approved: bool


class KnowledgeDocument(BaseModel):
    """知识库中已归档的原始文档。"""

    id: str = ""
    name: str
    size_bytes: int
    uploaded_at: str
    upload_success: bool = True
    is_markdown: bool = False
    is_conversion: bool = False
    embedding_status: str = "pending"
    indexed_chunks: int = 0
    knowledge_base_id: str = "default"


class KnowledgeBaseSummary(BaseModel):
    """One selectable knowledge base and its current document totals."""

    id: str
    name: str
    description: str = ""
    created_at: str = ""
    is_default: bool = False
    document_count: int = 0
    indexed_chunks: int = 0


class KnowledgeBaseCreateBody(BaseModel):
    """Fields accepted when creating a knowledge base."""

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)


class KnowledgeBaseUpdateBody(BaseModel):
    """Editable fields of an existing knowledge base."""

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)


class KnowledgeStatus(BaseModel):
    """知识库能力与当前文档清单。"""

    retrieval_ready: bool = False
    documents: list[KnowledgeDocument] = Field(default_factory=list)


class KnowledgeDocumentsDeleteBody(BaseModel):
    """Selected knowledge-base document IDs to delete."""

    document_ids: list[str] = Field(min_length=1)


class KnowledgeChunkEmbedRequest(BaseModel):
    """Parameters for the local chunking and embedding operation."""

    window_size: int = Field(
        default=5,
        ge=1,
        le=20,
        description="句子窗口大小：每个分块向前后扩展的句子数",
    )


class KnowledgeChunkEmbedResponse(BaseModel):
    """Completed local chunking and embedding operation."""

    collection_name: str
    documents_processed: int
    chunks_indexed: int
    vector_size: int
    document_chunks: dict[str, int] = Field(default_factory=dict)


class KnowledgeChunkEmbedProgress(BaseModel):
    """Current server-side stage for the knowledge indexing operation."""

    stage: str = "idle"
    error: str | None = None


class BrainstormDiscussionSummary(BaseModel):
    """Saved discussion displayed in the history list."""

    id: str
    title: str
    project_id: str | None = None
    model_config_id: str | None = None
    reasoning_effort: str | None = None
    updated_at: str


class BrainstormDiscussionMessage(BaseModel):
    """One persisted message in a saved discussion."""

    role: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrainstormDiscussion(BaseModel):
    """Saved discussion with its complete message history."""

    id: str
    title: str
    project_id: str | None = None
    model_config_id: str | None = None
    reasoning_effort: str | None = None
    updated_at: str
    messages: list[BrainstormDiscussionMessage] = Field(default_factory=list)

class ProjectCreateBody(BaseModel):
    """注册项目文件夹的请求。"""

    root_path: str = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, max_length=80)


class ProjectUpdateBody(BaseModel):
    """修改项目显示名称的请求。"""

    title: str = Field(min_length=1, max_length=80)


class ProjectSummary(BaseModel):
    """一个已注册项目文件夹。"""

    id: str
    title: str
    root_path: str
    created_at: str
    updated_at: str


