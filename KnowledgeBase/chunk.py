"""Split prepared knowledge-base Markdown into sentence-window nodes.

This module is deliberately limited to loading and chunking documents. The
embedding model and vector-store writes belong to the subsequent pipeline
stage, so this output remains reusable regardless of the chosen vector store.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from llama_index.core import Document
from llama_index.core.node_parser import SentenceWindowNodeParser
from llama_index.core.schema import BaseNode

try:
    from .catalog import DEFAULT_KNOWLEDGE_BASE_ID
except ImportError:  # Allows ``python KnowledgeBase/chunk.py`` as well.
    from catalog import DEFAULT_KNOWLEDGE_BASE_ID


KNOWLEDGE_ROOT = Path(__file__).resolve().parent
DOCUMENTS_ROOT = KNOWLEDGE_ROOT / "documents"
CHUNKS_ROOT = KNOWLEDGE_ROOT / "chunks"
DEFAULT_WINDOW_SIZE = 5
NODE_OUTPUT_NAME = "sentence_window_nodes.jsonl"
MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown"}
MARKDOWN_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def load_markdown_documents(
    documents_root: Path = DOCUMENTS_ROOT,
    *,
    document_ids_by_source: dict[str, str] | None = None,
    knowledge_base_ids_by_source: dict[str, str] | None = None,
) -> list[Document]:
    """Load prepared Markdown files from the knowledge-base documents folder.

    Args:
        documents_root: Folder containing Markdown generated during upload.
        document_ids_by_source: Stable upload IDs keyed by generated Markdown filename.

    Returns:
        LlamaIndex documents, each carrying its source filename and path.
    """
    if not documents_root.exists():
        return []

    documents: list[Document] = []
    for path in sorted(documents_root.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in MARKDOWN_EXTENSIONS:
            continue
        metadata = {
            "source_file": path.name,
            "source_path": str(path.resolve()),
        }
        document_id = (document_ids_by_source or {}).get(path.name)
        if document_id:
            metadata["document_id"] = document_id
        metadata["knowledge_base_id"] = (knowledge_base_ids_by_source or {}).get(
            path.name,
            DEFAULT_KNOWLEDGE_BASE_ID,
        )
        documents.extend(
            _split_markdown_sections(
                path.read_text(encoding="utf-8"),
                metadata,
                default_title=path.stem,
            )
        )
    return documents


def _split_markdown_sections(
    markdown: str,
    base_metadata: dict[str, str],
    *,
    default_title: str,
) -> list[Document]:
    """Split Markdown into heading-scoped documents with inherited metadata."""
    documents: list[Document] = []
    heading_stack: list[str] = []
    document_title = default_title
    section_lines: list[str] = []

    def append_section() -> None:
        text = "\n".join(section_lines).strip()
        if not text:
            return
        metadata = {
            **base_metadata,
            "document_title": document_title,
            "section_title": heading_stack[-1] if heading_stack else "",
            "heading_path": list(heading_stack),
        }
        documents.append(Document(text=text, metadata=metadata))

    for line in markdown.splitlines():
        match = MARKDOWN_HEADING_PATTERN.match(line)
        if not match:
            section_lines.append(line)
            continue

        append_section()
        section_lines = []
        level = len(match.group(1))
        title = match.group(2).strip()
        heading_stack = heading_stack[: level - 1]
        heading_stack.append(title)
        if level == 1:
            document_title = title

    append_section()
    return documents


def build_sentence_window_nodes(
    documents_root: Path = DOCUMENTS_ROOT,
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    document_ids_by_source: dict[str, str] | None = None,
    knowledge_base_ids_by_source: dict[str, str] | None = None,
) -> list[BaseNode]:
    """Split prepared Markdown with LlamaIndex ``SentenceWindowNodeParser``.

    Args:
        documents_root: Folder containing prepared Markdown documents.
        window_size: Number of neighbouring sentences retained on each node.
        document_ids_by_source: Stable upload IDs keyed by generated Markdown filename.

    Returns:
        Sentence-level nodes with ``window`` and ``original_text`` metadata.

    Raises:
        ValueError: If ``window_size`` is not a positive integer.
    """
    if window_size < 1:
        raise ValueError("window_size 必须为正整数")

    documents = load_markdown_documents(
        documents_root,
        document_ids_by_source=document_ids_by_source,
        knowledge_base_ids_by_source=knowledge_base_ids_by_source,
    )
    if not documents:
        return []

    parser = SentenceWindowNodeParser.from_defaults(
        window_size=window_size,
        window_metadata_key="window",
        original_text_metadata_key="original_text",
    )
    nodes = parser.get_nodes_from_documents(documents)
    chunk_indexes: dict[str, int] = {}
    for node in nodes:
        source_file = str(node.metadata.get("source_file") or "")
        chunk_indexes[source_file] = chunk_indexes.get(source_file, 0) + 1
        node.metadata["chunk_index"] = chunk_indexes[source_file]
        node.metadata["embedding_text"] = _build_embedding_text(node.text, node.metadata)
    return nodes


def _build_embedding_text(text: str, metadata: dict[str, object]) -> str:
    """Add Markdown structure to the text used only for vector embedding."""
    heading_path = metadata.get("heading_path")
    heading_text = " > ".join(heading_path) if isinstance(heading_path, list) else ""
    fields = [
        ("文档", metadata.get("document_title")),
        ("章节", heading_text),
        ("正文", text),
    ]
    return "\n".join(f"{label}：{value}" for label, value in fields if value)


def write_sentence_window_nodes(
    nodes: list[BaseNode],
    chunks_root: Path = CHUNKS_ROOT,
) -> Path:
    """Persist nodes as JSON Lines for the embedding and vector-store stage.

    Args:
        nodes: Nodes returned by :func:`build_sentence_window_nodes`.
        chunks_root: Destination directory for the reusable node artifact.

    Returns:
        The JSONL output path. An empty node list creates an empty file.
    """
    chunks_root.mkdir(parents=True, exist_ok=True)
    output_path = chunks_root / NODE_OUTPUT_NAME
    with output_path.open("w", encoding="utf-8") as output:
        for node in nodes:
            output.write(json.dumps(node.to_dict(), ensure_ascii=False))
            output.write("\n")
    return output_path


def chunk_knowledge_documents(
    documents_root: Path = DOCUMENTS_ROOT,
    chunks_root: Path = CHUNKS_ROOT,
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    document_ids_by_source: dict[str, str] | None = None,
    knowledge_base_ids_by_source: dict[str, str] | None = None,
) -> Path:
    """Build and persist sentence-window nodes for all prepared documents.

    Args:
        documents_root: Source folder of prepared Markdown files.
        chunks_root: Destination folder for the JSONL node artifact.
        window_size: Number of neighbouring sentences in each context window.
        document_ids_by_source: Stable upload IDs keyed by generated Markdown filename.

    Returns:
        Path to the generated JSONL node artifact.
    """
    nodes = build_sentence_window_nodes(
        documents_root,
        window_size=window_size,
        document_ids_by_source=document_ids_by_source,
        knowledge_base_ids_by_source=knowledge_base_ids_by_source,
    )
    return write_sentence_window_nodes(nodes, chunks_root)


if __name__ == "__main__":
    output = chunk_knowledge_documents()
    print(f"已生成句子窗口切分结果：{output}")
