"""FastAPI 入口。

启动：在项目根目录执行
    开发模式：uv run python -m server.dev
    常规模式：uvicorn server.main:app --host 0.0.0.0 --port 8000

开发模式只监控 server/、scr/ 与 data_analysis/。不要直接使用项目根目录下的
``uvicorn server.main:app --reload``，否则 artifacts/ 中的建模产物会触发重载。
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from zipfile import BadZipFile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from docx.opc.exceptions import PackageNotFoundError
from openpyxl.utils.exceptions import InvalidFileException

from data_analysis.agent.graph import iter_graph
from data_analysis.agent.loop import Action, AgentLoop, Finish
from data_analysis.agent.state import AgentMessage, AgentState, Observation
from data_analysis.context import compile_rolling_context
from data_analysis.runtime import Harness, SubprocessSandbox, resolve_python_environment
from data_analysis.tools import create_agent_tool_registry, supports_data_profile
from server import store
from server.files import list_figures, resolve_artifact
from server.project_workspaces import (
    ProjectWorkspaceError,
    create_project as register_project,
    list_project_file_tree,
    load_project_context,
    require_project,
    resolve_project_python,
)
from server.discussions import delete_discussion, get_discussion, list_discussions, rename_discussion, save_discussion_message
from KnowledgeBase.chunk import DEFAULT_WINDOW_SIZE, chunk_knowledge_documents
from KnowledgeBase.catalog import (
    KnowledgeBaseCatalogError,
    create_knowledge_base,
    delete_knowledge_base,
    list_knowledge_bases,
    require_knowledge_base,
    require_knowledge_bases,
    update_knowledge_base,
)
from KnowledgeBase.embedding import build_local_qdrant_index, delete_document_vectors
from KnowledgeBase.upload_file import (
    DOCUMENTS_ROOT as KNOWLEDGE_DOCUMENTS_ROOT,
    KnowledgePreparationError,
    UPLOAD_MANIFEST_NAME,
    convert_documents,
    delete_upload_records,
    list_upload_records,
    migrate_legacy_document_ids,
    prepare_upload,
    reconcile_conversion_records,
    update_embedding_status,
)
from server.runs import (
    cancel_run,
    cleanup_stale_runs,
    create_run,
    delete_run,
    execute_run,
    get_pending_budget,
    get_pending_clarification,
    get_run,
    get_run_monitor,
    list_runs,
    rename_run,
    submit_budget_decision,
    submit_clarification_decision,
)
from scr.runtime.budget import BudgetType
from scr.math_modeling_agent.llm import (
    LLM_CALL_TIMEOUT_SECONDS as AGENT_LLM_TIMEOUT_SECONDS,
    create_llm,
)
from server.schemas import (
    AgentCommandDecisionBody,
    BudgetConfirmBody,
    BrainstormDiscussion,
    BrainstormDiscussionSummary,
    BrainstormDiscussionMessage,
    CreateRunResponse,
    KnowledgeDocument,
    KnowledgeBaseCreateBody,
    KnowledgeBaseSummary,
    KnowledgeBaseUpdateBody,
    KnowledgeChunkEmbedRequest,
    KnowledgeChunkEmbedResponse,
    KnowledgeChunkEmbedProgress,
    KnowledgeDocumentsDeleteBody,
    KnowledgeStatus,
    ModelConfig,
    ProjectCreateBody,
    ProjectSummary,
    ProjectUpdateBody,
    RunDetail,
    RunMonitor,
    RunSummary,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"
WEB_DIST = PROJECT_ROOT / "web" / "dist"
API_SETTINGS_PATH = Path(__file__).resolve().parent / "api_settings.json"
AGENT_TOOL_TIMEOUT_SECONDS = 2 * 60 * 60          # Agent 单次工具执行超时时间（2 小时）
AGENT_RUN_TIMEOUT_SECONDS = 3 * 60 * 60           # Agent 整轮运行硬超时时间（3 小时）
AGENT_COMMAND_APPROVAL_TIMEOUT_SECONDS = 300      # 单条项目命令等待人工批准的上限
_pending_agent_commands: dict[str, asyncio.Future[bool]] = {}
MAX_AGENT_ATTACHMENTS = 10                        # 数据 Agent 单次最多接收的附件数
MAX_AGENT_ATTACHMENT_BYTES = 100 * 1024 * 1024    # 数据 Agent 单附件大小上限（20MB）
MAX_AGENT_PREVIEW_TEXT_BYTES = 2 * 1024 * 1024    # 文本预览最多读取 2 MB
MAX_AGENT_PREVIEW_ROWS = 200                      # 表格预览最多展示 200 行
MAX_AGENT_PREVIEW_COLUMNS = 40                    # 表格预览最多展示 40 列
MAX_AGENT_PREVIEW_SHEETS = 8                      # 工作簿预览最多展示 8 个工作表
AGENT_ATTACHMENT_SUFFIXES = frozenset(
    {
        ".bmp",
        ".csv",
        ".docx",
        ".gif",
        ".jpeg",
        ".jpg",
        ".json",
        ".jsonl",
        ".markdown",
        ".md",
        ".ndjson",
        ".pdf",
        ".png",
        ".py",
        ".tif",
        ".tiff",
        ".webp",
        ".xls",
        ".xlsx",
    }
)
AGENT_SYSTEM_PROMPT_PATH = PROJECT_ROOT / "data_analysis" / "prompts" / "system.md"
AGENT_PREVIEW_IMAGE_SUFFIXES = frozenset(
    {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}
)
AGENT_PREVIEW_TEXT_SUFFIXES = frozenset(
    {
        ".bat", ".c", ".cc", ".cpp", ".cs", ".css", ".csv", ".go", ".h",
        ".hpp", ".htm", ".html", ".ini", ".java", ".js", ".json", ".jsonl",
        ".ipynb", ".jsx", ".log", ".markdown", ".md", ".ndjson", ".ps1", ".py",
        ".r", ".rmd", ".rs", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml",
        ".yml",
    }
)

logger = logging.getLogger(__name__)

app = FastAPI(title="MMAgent Web", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本地单机开发；生产应改为前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """index.html 禁止缓存，确保前端重新构建后浏览器立即加载新资源。"""
    response = await call_next(request)
    if request.url.path in {"", "/", "/index.html"}:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# 保留后台任务引用，避免被 GC
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_KNOWLEDGE_PROCESSING_LOCK = Lock()
_KNOWLEDGE_PROGRESS_LOCK = Lock()
_knowledge_processing_progress = {"stage": "idle", "error": None}


def _set_knowledge_processing_progress(stage: str, error: str | None = None) -> None:
    """Record the current real indexing stage for the frontend."""
    with _KNOWLEDGE_PROGRESS_LOCK:
        _knowledge_processing_progress["stage"] = stage
        _knowledge_processing_progress["error"] = error


def _get_knowledge_processing_progress() -> KnowledgeChunkEmbedProgress:
    """Return a consistent snapshot of the current indexing stage."""
    with _KNOWLEDGE_PROGRESS_LOCK:
        return KnowledgeChunkEmbedProgress(**_knowledge_processing_progress)


def _list_knowledge_documents(
    knowledge_base_id: str | None = None,
) -> list[KnowledgeDocument]:
    """Return locally archived source documents for the knowledge-base MVP."""
    records = reconcile_conversion_records()
    if records:
        return [
            KnowledgeDocument(
                id=record.document_id,
                name=record.name,
                size_bytes=record.size_bytes,
                uploaded_at=record.uploaded_at,
                upload_success=record.upload_success,
                is_markdown=record.is_markdown,
                is_conversion=record.is_conversion,
                embedding_status=record.embedding_status,
                indexed_chunks=record.indexed_chunks,
                knowledge_base_id=record.knowledge_base_id,
            )
            for record in records
            if knowledge_base_id is None or record.knowledge_base_id == knowledge_base_id
        ]
    if not KNOWLEDGE_DOCUMENTS_ROOT.exists():
        return []
    files = [
        path
        for path in sorted(KNOWLEDGE_DOCUMENTS_ROOT.iterdir(), key=lambda item: item.name.lower())
        if path.is_file() and path.name != UPLOAD_MANIFEST_NAME
    ]
    return [
        KnowledgeDocument(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, str(path.resolve()))),
            name=path.name,
            size_bytes=path.stat().st_size,
            uploaded_at=datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            upload_success=True,
            is_markdown=path.suffix.lower() in {".md", ".markdown", ".mdown"},
            is_conversion=True,
            knowledge_base_id="default",
        )
        for index, path in enumerate(files)
        if knowledge_base_id in {None, "default"}
    ]


@app.on_event("startup")
async def _on_startup() -> None:
    """启动时清理因服务重启而留下的\"僵尸\"运行（status=running 但无对应后台线程）。"""
    migrate_legacy_document_ids()
    cleaned = cleanup_stale_runs(max_age_seconds=120)
    if cleaned:
        print(f"[startup] cleaned {cleaned} stale running run(s)")


def _read_api_settings() -> dict | None:
    """Load the persisted API settings snapshot, or None when absent/corrupt."""
    if not API_SETTINGS_PATH.exists():
        return None
    try:
        data = json.loads(API_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@app.get("/api/projects", response_model=list[ProjectSummary])
async def list_projects_endpoint() -> list[ProjectSummary]:
    """列出已注册的项目文件夹。"""
    items = await asyncio.to_thread(store.list_projects)
    return [ProjectSummary(**item) for item in items]


@app.post("/api/projects", response_model=ProjectSummary)
async def create_project_endpoint(body: ProjectCreateBody) -> ProjectSummary:
    """注册一个真实项目文件夹。"""
    try:
        project = await asyncio.to_thread(register_project, body.root_path, body.title)
    except ProjectWorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ProjectSummary(**project)


@app.patch("/api/projects/{project_id}", response_model=ProjectSummary)
async def update_project_endpoint(
    project_id: str,
    body: ProjectUpdateBody,
) -> ProjectSummary:
    """修改项目显示名称。"""
    renamed = await asyncio.to_thread(store.rename_project, project_id, body.title)
    if not renamed:
        raise HTTPException(status_code=404, detail="项目不存在")
    project = await asyncio.to_thread(store.get_project, project_id)
    return ProjectSummary(**project)


@app.delete("/api/projects/{project_id}")
async def delete_project_endpoint(project_id: str) -> dict[str, bool]:
    """取消项目注册，但不删除真实文件夹或其中产物。"""
    deleted = await asyncio.to_thread(store.delete_project, project_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="项目不存在")
    return {"deleted": True}


@app.get("/api/projects/{project_id}/files/tree")
async def list_project_files_tree_endpoint(project_id: str) -> dict[str, object]:
    """返回项目工作区文件树（仅结构，不含文件内容）。"""
    try:
        tree = await asyncio.to_thread(list_project_file_tree, project_id)
    except ProjectWorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return tree


@app.post("/api/projects/{project_id}/open-folder")
async def open_project_folder_endpoint(project_id: str) -> dict[str, bool]:
    """在本机文件管理器中打开项目根目录。"""
    try:
        project = await asyncio.to_thread(require_project, project_id)
    except ProjectWorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    target = Path(str(project["root_path"]))
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="项目文件夹不存在")
    try:
        _open_local_folder(target)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"opened": True}


def _open_native_folder_dialog(initial_dir: str | None = None) -> str | None:
    """打开系统文件夹选择框，返回绝对路径；用户取消时返回 None。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise RuntimeError("当前环境缺少 tkinter，无法打开系统文件夹选择框") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        # 置顶，避免被浏览器窗口盖住
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title="选择或新建项目文件夹",
            initialdir=initial_dir or str(Path.home()),
            mustexist=False,
        )
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass

    if not selected:
        return None
    return str(Path(selected).expanduser().resolve())


@app.post("/api/system/select-folder")
async def select_system_folder_endpoint() -> dict[str, str | None]:
    """打开操作系统原生文件夹选择对话框，返回所选路径。"""
    try:
        path = await asyncio.to_thread(_open_native_folder_dialog)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— GUI 环境异常时给前端可读错误
        raise HTTPException(status_code=503, detail=f"无法打开系统文件夹选择框：{exc}") from exc
    return {"path": path}


@app.get(
    "/api/projects/{project_id}/sessions",
    response_model=list[BrainstormDiscussionSummary],
)
async def list_project_sessions_endpoint(project_id: str) -> list[BrainstormDiscussionSummary]:
    """列出一个项目下的所有会话。"""
    if await asyncio.to_thread(store.get_project, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return [
        BrainstormDiscussionSummary(**item)
        for item in await asyncio.to_thread(list_discussions, project_id)
    ]


@app.post("/api/projects/{project_id}/sessions", response_model=BrainstormDiscussionSummary)
async def create_project_session_endpoint(
    project_id: str,
    body: dict,
) -> BrainstormDiscussionSummary:
    """在指定项目中创建一个空会话。"""
    try:
        project = await asyncio.to_thread(require_project, project_id)
    except ProjectWorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    title = str(body.get("title") or "新讨论").strip()[:80] or "新讨论"
    session_id = await asyncio.to_thread(
        store.create_session,
        title,
        project_id=project_id,
        cwd=str(project["root_path"]),
    )
    session = await asyncio.to_thread(store.get_session, session_id)
    return BrainstormDiscussionSummary(**session)

@app.get("/api/settings/api")
async def get_api_settings_endpoint() -> dict:
    """Return the locally persisted API settings snapshot.

    ``saved=False`` means nothing has ever been persisted, so the frontend
    keeps its browser cache untouched instead of overwriting it with empties.
    """
    data = _read_api_settings() or {}
    return {
        "saved": bool(data),
        "configs": data.get("configs", []),
        "active_id": data.get("active_id"),
        "external_services": data.get("external_services", {}),
    }


@app.put("/api/settings/api")
async def save_api_settings_endpoint(body: dict) -> dict:
    """Persist the API settings snapshot to a local file (atomic replace)."""
    if not isinstance(body.get("configs"), list):
        raise HTTPException(status_code=422, detail="configs must be a list")
    payload = {
        "configs": body.get("configs", []),
        "active_id": body.get("active_id") if isinstance(body.get("active_id"), str) else None,
        "external_services": body.get("external_services")
        if isinstance(body.get("external_services"), dict)
        else {},
    }
    tmp_path = API_SETTINGS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(API_SETTINGS_PATH)
    return {"ok": True}


@app.get("/api/knowledge/bases", response_model=list[KnowledgeBaseSummary])
async def list_knowledge_bases_endpoint() -> list[KnowledgeBaseSummary]:
    """List selectable knowledge bases with their current document totals."""
    records = list_upload_records()
    return [
        KnowledgeBaseSummary(
            **base.__dict__,
            document_count=sum(1 for item in records if item.knowledge_base_id == base.id),
            indexed_chunks=sum(
                item.indexed_chunks for item in records if item.knowledge_base_id == base.id
            ),
        )
        for base in list_knowledge_bases()
    ]


@app.post("/api/knowledge/bases", response_model=KnowledgeBaseSummary)
async def create_knowledge_base_endpoint(
    body: KnowledgeBaseCreateBody,
) -> KnowledgeBaseSummary:
    """Create a knowledge base that can be selected for upload and retrieval."""
    try:
        base = create_knowledge_base(body.name, body.description)
    except KnowledgeBaseCatalogError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return KnowledgeBaseSummary(**base.__dict__)


@app.put("/api/knowledge/bases/{knowledge_base_id}", response_model=KnowledgeBaseSummary)
async def update_knowledge_base_endpoint(
    knowledge_base_id: str,
    body: KnowledgeBaseUpdateBody,
) -> KnowledgeBaseSummary:
    """Rename or describe any knowledge base, including the legacy default route."""
    try:
        base = update_knowledge_base(knowledge_base_id, body.name, body.description)
    except KnowledgeBaseCatalogError as exc:
        raise HTTPException(status_code=404 if "不存在" in str(exc) else 400, detail=str(exc)) from exc
    records = list_upload_records()
    matching = [item for item in records if item.knowledge_base_id == base.id]
    return KnowledgeBaseSummary(
        **base.__dict__,
        document_count=len(matching),
        indexed_chunks=sum(item.indexed_chunks for item in matching),
    )


@app.delete("/api/knowledge/bases/{knowledge_base_id}")
async def delete_knowledge_base_endpoint(knowledge_base_id: str) -> dict[str, Any]:
    """删除知识库，并同步删除其本地源文件、Markdown 与索引向量。"""
    try:
        base = require_knowledge_base(knowledge_base_id)
    except KnowledgeBaseCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    document_ids = {
        record.document_id
        for record in list_upload_records()
        if record.knowledge_base_id == base.id
    }

    # 1) 优先删除本地文件（raw 原始件 + documents Markdown + 清单记录）
    try:
        if document_ids:
            delete_upload_records(document_ids)
    except KnowledgePreparationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 2) 向量清理失败不阻断目录删除，避免本地文件已删但库仍残留
    if document_ids:
        try:
            delete_document_vectors(document_ids)
        except Exception:
            logging.getLogger(__name__).exception(
                "删除知识库 %s 时清理向量失败", base.id
            )

    # 3) 从目录移除知识库，并按剩余文件重建分块索引
    try:
        delete_knowledge_base(base.id)
        if document_ids:
            chunk_knowledge_documents(
                document_ids_by_source=_document_ids_by_source(),
                knowledge_base_ids_by_source=_knowledge_base_ids_by_source(),
            )
    except (KnowledgeBaseCatalogError, KnowledgePreparationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "deleted_id": base.id,
        "deleted_document_ids": sorted(document_ids),
    }


@app.get("/api/knowledge/status", response_model=KnowledgeStatus)
async def knowledge_status_endpoint(
    knowledge_base_id: str | None = None,
) -> KnowledgeStatus:
    """Expose the knowledge-base MVP state without claiming retrieval is ready."""
    if knowledge_base_id is None:
        return KnowledgeStatus(documents=_list_knowledge_documents())
    try:
        base = require_knowledge_base(knowledge_base_id)
    except KnowledgeBaseCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return KnowledgeStatus(documents=_list_knowledge_documents(base.id))


@app.post("/api/knowledge/documents", response_model=list[KnowledgeDocument])
async def upload_knowledge_document_endpoint(
    document: UploadFile = File(..., description="知识库原始文档"),
    knowledge_base_id: str | None = Form(None, description="目标知识库 ID"),
) -> list[KnowledgeDocument]:
    """Archive one uploaded document or ZIP; Markdown conversion is triggered separately."""
    filename = Path(document.filename or "knowledge-document").name
    if not filename:
        raise HTTPException(status_code=400, detail="文档名称不能为空")

    try:
        base = require_knowledge_base(knowledge_base_id)
    except KnowledgeBaseCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    content = await document.read()
    try:
        prepared = prepare_upload(filename, content, knowledge_base_id=base.id)
    except (ValueError, KnowledgePreparationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return [
        KnowledgeDocument(
            id=item.document_id,
            name=item.source_name,
            size_bytes=item.source_size_bytes,
            uploaded_at=item.uploaded_at,
            upload_success=True,
            is_markdown=item.is_markdown,
            is_conversion=item.is_conversion,
            embedding_status="pending",
            indexed_chunks=0,
            knowledge_base_id=base.id,
        )
        for item in prepared
    ]


@app.delete("/api/knowledge/documents")
async def delete_knowledge_documents_endpoint(
    body: KnowledgeDocumentsDeleteBody,
) -> dict[str, list[str]]:
    """Delete selected prepared knowledge-base documents by their stable IDs."""
    selected_ids = {
        record.document_id
        for record in list_upload_records()
        if record.document_id in body.document_ids
    }
    if not selected_ids:
        raise HTTPException(status_code=404, detail="未找到可删除的知识库文件")
    try:
        delete_document_vectors(selected_ids)
        deleted_ids = delete_upload_records(selected_ids)
        chunk_knowledge_documents(
            document_ids_by_source=_document_ids_by_source(),
            knowledge_base_ids_by_source=_knowledge_base_ids_by_source(),
        )
    except KnowledgePreparationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted_ids": deleted_ids}


@app.post("/api/knowledge/documents/convert")
async def convert_knowledge_documents_endpoint(
    body: KnowledgeDocumentsDeleteBody,
) -> dict[str, Any]:
    """Convert selected non-Markdown documents to Markdown via MinerU."""
    settings = _read_api_settings() or {}
    mineru = settings.get("external_services", {}).get("mineru", {})
    try:
        converted_ids, failed_messages = await asyncio.to_thread(
            convert_documents,
            set(body.document_ids),
            mineru_api_key=mineru.get("apiKey"),
            mineru_base_url=mineru.get("baseUrl"),
        )
    except KnowledgePreparationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"converted_ids": converted_ids, "failed": failed_messages}


@app.post("/api/knowledge/chunk-embed", response_model=KnowledgeChunkEmbedResponse)
async def chunk_and_embed_knowledge_endpoint(
    body: KnowledgeChunkEmbedRequest | None = None,
) -> KnowledgeChunkEmbedResponse:
    """Run local sentence-window chunking and HuggingFace embedding on demand."""
    window_size = body.window_size if body else DEFAULT_WINDOW_SIZE
    if not _KNOWLEDGE_PROCESSING_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有知识库分块与嵌入任务正在执行")
    _set_knowledge_processing_progress("chunking")
    try:
        result = await asyncio.to_thread(_build_knowledge_vector_index, window_size)
        document_chunks = _document_chunk_counts(result.source_chunk_counts)
        update_embedding_status(document_chunks)
        _set_knowledge_processing_progress("done")
        return KnowledgeChunkEmbedResponse(
            collection_name=result.collection_name,
            documents_processed=len(document_chunks),
            chunks_indexed=result.indexed_nodes,
            vector_size=result.vector_size,
            document_chunks=document_chunks,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _set_knowledge_processing_progress("failed", str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        _set_knowledge_processing_progress("failed", str(exc))
        raise
    finally:
        _KNOWLEDGE_PROCESSING_LOCK.release()


@app.get("/api/knowledge/chunk-embed/progress", response_model=KnowledgeChunkEmbedProgress)
async def knowledge_chunk_embed_progress_endpoint() -> KnowledgeChunkEmbedProgress:
    """Return the real processing stage of the active knowledge indexing job."""
    return _get_knowledge_processing_progress()


def _build_knowledge_vector_index(window_size: int = DEFAULT_WINDOW_SIZE):
    """Regenerate chunks, then rebuild the local Qdrant collection."""
    chunk_knowledge_documents(
        window_size=window_size,
        document_ids_by_source=_document_ids_by_source(),
        knowledge_base_ids_by_source=_knowledge_base_ids_by_source(),
    )
    _set_knowledge_processing_progress("embedding")
    return build_local_qdrant_index()


def _document_ids_by_source() -> dict[str, str]:
    """Map generated Markdown filenames to stable upload UUIDs."""
    return {
        record.output_name: record.document_id
        for record in list_upload_records()
    }


def _knowledge_base_ids_by_source() -> dict[str, str]:
    """Map generated Markdown filenames to their retrieval route."""
    return {
        record.output_name: record.knowledge_base_id
        for record in list_upload_records()
    }


def _document_chunk_counts(source_chunk_counts: dict[str, int]) -> dict[str, int]:
    """Map generated Markdown filenames back to stable upload-table IDs."""
    records = list_upload_records()
    if records:
        return {
            record.document_id: source_chunk_counts.get(record.output_name, 0)
            for record in records
        }
    return {
        document.id: source_chunk_counts.get(document.name, 0)
        for document in _list_knowledge_documents()
    }


def _create_llm_from_api_config(config: dict) -> object | None:
    """Build an LLM client from one persisted API settings entry."""
    return create_llm(
        provider=config.get("provider"),
        api_key=config.get("apiKey"),
        base_url=config.get("baseUrl"),
        model=config.get("model"),
        max_tokens=config.get("maxTokens"),
        temperature=config.get("temperature"),
        reasoning_effort=config.get("reasoningEffort"),
        fallback_env=False,
    )


def _active_discussion_llm():
    """Create the LLM selected in the persisted API settings."""
    settings = _read_api_settings() or {}
    configs = settings.get("configs")
    active_id = settings.get("active_id")
    if not isinstance(configs, list) or not isinstance(active_id, str):
        return None
    config = next(
        (item for item in configs if isinstance(item, dict) and item.get("id") == active_id),
        None,
    )
    if config is None:
        return None
    return _create_llm_from_api_config(config)


def _discussion_llm_by_id(config_id: str | None):
    """Create the LLM for a requested API config id, falling back to the active one."""
    if not isinstance(config_id, str) or not config_id:
        return _active_discussion_llm()
    settings = _read_api_settings() or {}
    configs = settings.get("configs")
    if not isinstance(configs, list):
        return None
    config = next(
        (item for item in configs if isinstance(item, dict) and item.get("id") == config_id),
        None,
    )
    if config is None:
        return None
    return _create_llm_from_api_config(config)


async def _prepare_agent_data_files(
    files: list[UploadFile] | None,
    discussion_id: str,
    run_id: str,
) -> list[Path]:
    """校验并保存本轮数据 Agent 获授权的附件。

    Args:
        files: 前端上传的附件。
        discussion_id: 当前会话标识。
        run_id: 服务端生成的本轮运行标识。

    Returns:
        已保存且可交给工具注册表授权的附件绝对路径。

    Raises:
        HTTPException: 文件数量、类型或大小不符合限制。
    """
    if not files:
        return []
    if len(files) > MAX_AGENT_ATTACHMENTS:
        raise HTTPException(status_code=400, detail=f"最多可上传 {MAX_AGENT_ATTACHMENTS} 个附件")

    prepared: list[tuple[str, bytes]] = []
    used_names: set[str] = set()
    for index, file in enumerate(files, start=1):
        original_name = Path(file.filename or f"data-{index}").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in AGENT_ATTACHMENT_SUFFIXES:
            supported = "、".join(sorted(AGENT_ATTACHMENT_SUFFIXES))
            raise HTTPException(
                status_code=400,
                detail=f"数据 Agent 不支持 {original_name}，当前支持 {supported}",
            )
        content = await file.read()
        if len(content) > MAX_AGENT_ATTACHMENT_BYTES:
            raise HTTPException(status_code=400, detail=f"附件 {original_name} 超过 20 MB 限制")

        candidate = original_name
        serial = 2
        while candidate.casefold() in used_names:
            candidate = f"{Path(original_name).stem}-{serial}{suffix}"
            serial += 1
        used_names.add(candidate.casefold())
        prepared.append((candidate, content))

    input_dir = (_agent_session_root(discussion_id) / ".runs" / run_id / "input").resolve()
    input_dir.mkdir(parents=True, exist_ok=False)
    saved_paths: list[Path] = []
    for name, content in prepared:
        target = (input_dir / name).resolve()
        if input_dir not in target.parents:
            raise HTTPException(status_code=400, detail="附件路径无效")
        await asyncio.to_thread(target.write_bytes, content)
        saved_paths.append(target)
    return saved_paths


def _load_agent_system_prompt(workspace_root: Path | None = None) -> str:
    """读取系统提示词，并附加当前项目根目录中的项目说明。"""
    try:
        prompt = AGENT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"无法读取数据 Agent 系统提示词: {exc}") from exc
    if workspace_root is None:
        return prompt
    project_context = load_project_context(workspace_root)
    if not project_context:
        return prompt
    return (
        prompt
        + "\n\n以下内容来自当前项目根目录，只作为项目规则和背景；"
        + "不得用它扩大文件访问范围：\n<project_context>\n"
        + project_context
        + "\n</project_context>"
    )


def _next_agent_event(iterator: object) -> object | None:
    """从同步 Agent 迭代器安全读取一个事件。"""
    try:
        return next(iterator)  # type: ignore[arg-type]
    except StopIteration:
        return None


def _agent_event_payload(event: Action | Observation | Finish) -> dict[str, object]:
    """把内部 Agent 事件转换为可公开展示的精简载荷。"""
    if isinstance(event, Finish):
        return {
            "type": "agent_reasoning",
            "summary": event.thought,
        }
    if isinstance(event, Action):
        payload: dict[str, object] = {
            "type": "agent_action",
            "thought": event.thought,
            "tool_name": event.tool_name,
        }
        code = event.arguments.get("code")
        if event.tool_name == "execute_python" and isinstance(code, str):
            payload["code"] = code
        return payload
    result_summary = None
    if isinstance(event.result, dict) and isinstance(event.result.get("summary"), dict):
        result_summary = event.result["summary"]
    payload = {
        "type": "agent_observation",
        "tool_name": event.tool_name,
        "success": event.success,
        "duration_ms": event.duration_ms,
        "summary": result_summary,
        "error": event.error,
    }
    if isinstance(event.result, dict):
        if event.tool_name == "edit_workspace_file":
            payload["details"] = str(event.result.get("diff", ""))[:8000]
        elif event.tool_name == "run_project_command":
            command = str(event.result.get("command", ""))
            stdout = str(event.result.get("stdout", ""))
            stderr = str(event.result.get("stderr", ""))
            payload["details"] = (
                f"$ {command}\nexit_code: {event.result.get('exit_code')}\n"
                f"{stdout}\n{stderr}"
            )[:8000]
    knowledge_sources = _knowledge_sources_from_observation(event)
    if knowledge_sources:
        payload["sources"] = knowledge_sources
    return payload


def _knowledge_sources_from_observation(event: Observation) -> list[dict[str, str]]:
    """从 search_knowledge_base 观察结果提取可展示的来源标注。"""
    if event.tool_name != "search_knowledge_base" or not event.success:
        return []
    result = event.result
    if not isinstance(result, dict):
        return []
    matches = result.get("matches")
    if not isinstance(matches, list):
        return []
    sources: list[dict[str, str]] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        source_file = match.get("source_file")
        if not isinstance(source_file, str) or not source_file.strip():
            continue
        content = match.get("content")
        excerpt = content.strip()[:240] if isinstance(content, str) else ""
        sources.append(
            {
                "source_file": source_file.strip(),
                "document_id": str(match.get("document_id") or ""),
                "knowledge_base_id": str(match.get("knowledge_base_id") or ""),
                "excerpt": excerpt,
            }
        )
    return sources


def _merge_knowledge_sources(
    collected: list[dict[str, str]],
    incoming: list[dict[str, str]],
) -> list[dict[str, str]]:
    """按来源文件去重合并知识库引用，保留首次出现顺序。"""
    seen: set[tuple[str, str]] = {
        (item.get("document_id", ""), item.get("source_file", ""))
        for item in collected
    }
    merged = list(collected)
    for item in incoming:
        key = (item.get("document_id", ""), item.get("source_file", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _agent_session_root(discussion_id: str) -> Path:
    """返回经过校验的 Agent 会话产物目录。"""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", discussion_id) is None:
        raise ValueError("Agent 会话标识无效")
    root = (ARTIFACTS_ROOT / "agent_sessions" / discussion_id).resolve()
    sessions_root = (ARTIFACTS_ROOT / "agent_sessions").resolve()
    if sessions_root not in root.parents:
        raise ValueError("Agent 会话产物目录越界")
    return root


def _agent_output_root(discussion_id: str, workspace_root: Path | None = None) -> Path:
    """返回项目内或旧版内部目录中的会话产物根目录。"""
    _agent_session_root(discussion_id)
    root = workspace_root
    if root is None:
        session = store.get_session(discussion_id)
        cwd = session.get("cwd") if session else None
        root = Path(str(cwd)).resolve(strict=True) if cwd else None
    if root is None:
        return _agent_session_root(discussion_id)
    project_root = root.resolve(strict=True)
    outputs_root = (project_root / "outputs").resolve(strict=False)
    if project_root not in outputs_root.parents:
        raise ValueError("Agent 项目输出目录越过项目根目录")
    target = (outputs_root / discussion_id).resolve(strict=False)
    if outputs_root not in target.parents:
        raise ValueError("Agent 项目产物目录越界")
    return target


def _resolve_agent_project(
    stored_session: dict[str, object] | None,
    requested_project_id: str | None,
) -> dict[str, object] | None:
    """解析会话的固定项目，并拒绝把已有会话切换到其他项目。"""
    bound_project_id = stored_session.get("project_id") if stored_session else None
    if isinstance(bound_project_id, str) and bound_project_id:
        if requested_project_id and requested_project_id != bound_project_id:
            raise ProjectWorkspaceError("当前会话已经绑定到其他项目")
        project = require_project(bound_project_id)
        if stored_session.get("cwd") != project["root_path"]:
            raise ProjectWorkspaceError("会话项目目录与注册记录不一致")
        return project
    if requested_project_id:
        return require_project(requested_project_id)
    return None


def _restore_agent_data_files(
    session_entries: list[dict[str, object]],
    discussion_id: str,
) -> list[Path]:
    """恢复同一会话中仍然有效的历史输入附件。"""
    session_root = _agent_session_root(discussion_id)
    restored_by_name: dict[str, Path] = {}
    for entry in reversed(session_entries):
        if entry.get("role") != store.ROLE_USER:
            continue
        metadata = entry.get("metadata")
        if not isinstance(metadata, dict):
            continue
        attachments = metadata.get("attachments")
        if not isinstance(attachments, list):
            continue
        for attachment in reversed(attachments):
            if not isinstance(attachment, dict):
                continue
            if attachment.get("kind") not in {"data", "file"}:
                continue
            raw_path = attachment.get("content")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            relative_path = Path(raw_path)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                continue
            candidate = (PROJECT_ROOT / relative_path).resolve()
            try:
                session_relative = candidate.relative_to(session_root)
            except ValueError:
                continue
            parts = session_relative.parts
            if (
                len(parts) != 4
                or parts[0].casefold() != ".runs"
                or parts[2].casefold() != "input"
                or candidate.suffix.casefold() not in AGENT_ATTACHMENT_SUFFIXES
                or not candidate.is_file()
            ):
                continue
            restored_by_name.setdefault(candidate.name.casefold(), candidate)
    return list(reversed(restored_by_name.values()))


def _merge_agent_data_files(
    restored_paths: list[Path],
    uploaded_paths: list[Path],
) -> list[Path]:
    """合并历史和本轮附件；同名时优先使用本轮上传版本。"""
    uploaded_names = {path.name.casefold() for path in uploaded_paths}
    merged_paths = [
        *(path for path in restored_paths if path.name.casefold() not in uploaded_names),
        *uploaded_paths,
    ]
    return merged_paths[-MAX_AGENT_ATTACHMENTS:]


def _open_local_folder(target: Path) -> None:
    """使用当前操作系统的文件管理器打开指定目录。"""
    if sys.platform == "win32":
        command = ["explorer.exe", str(target)]
    elif sys.platform == "darwin":
        command = ["open", str(target)]
    else:
        command = ["xdg-open", str(target)]
    try:
        subprocess.Popen(command, close_fds=True)
    except OSError as exc:
        raise RuntimeError("无法启动系统文件管理器") from exc


@app.post("/api/agent/discussions/{discussion_id}/open-folder")
def open_agent_artifact_folder(discussion_id: str) -> dict[str, bool]:
    """在本机文件管理器中打开指定讨论的产物目录。"""
    try:
        target = _agent_output_root(discussion_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="当前讨论还没有产物目录")
    try:
        _open_local_folder(target)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"opened": True}


def _legacy_agent_run_root(run_id: str) -> Path:
    """返回旧版按运行保存的 Agent 产物目录。"""
    if re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise ValueError("Agent 运行标识无效")
    root = (ARTIFACTS_ROOT / "agent_runs" / run_id).resolve()
    runs_root = (ARTIFACTS_ROOT / "agent_runs").resolve()
    if runs_root not in root.parents:
        raise ValueError("Agent 运行目录越界")
    return root


def _resolve_agent_artifact(discussion_id: str, file_path: str) -> Path:
    """解析 Agent 输出产物，并阻止访问输入附件或目录外文件。"""
    root = _agent_output_root(discussion_id)
    relative_path = Path(file_path)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or not relative_path.parts
        or relative_path.parts[0].casefold() == ".runs"
    ):
        raise ValueError("Agent 产物路径无效")
    target = (root / relative_path).resolve()
    if root not in target.parents or not target.is_file():
        raise FileNotFoundError(f"Agent 产物不存在: {file_path}")
    return target


def _resolve_legacy_agent_artifact(run_id: str, file_path: str) -> Path:
    """解析旧版按运行保存的输出产物。"""
    root = _legacy_agent_run_root(run_id)
    relative_path = Path(file_path)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or not relative_path.parts
        or relative_path.parts[0].casefold() == "input"
    ):
        raise ValueError("Agent 产物路径无效")
    target = (root / relative_path).resolve()
    if root not in target.parents or not target.is_file():
        raise FileNotFoundError(f"Agent 产物不存在: {file_path}")
    return target


def _list_agent_artifacts(
    discussion_id: str,
    run_id: str,
    workspace_root: Path | None = None,
) -> list[dict[str, object]]:
    """列出会话产物区内的输出文件，排除各轮用户输入附件。"""
    root = _agent_output_root(discussion_id, workspace_root)
    if not root.is_dir():
        return []
    artifacts: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if relative_path.parts[0].casefold() == ".runs":
            continue
        artifacts.append(
            {
                "discussion_id": discussion_id,
                "run_id": run_id,
                "path": relative_path.as_posix(),
                "name": path.name,
                "size_bytes": path.stat().st_size,
            }
        )
    return artifacts


def _preview_cell_value(value: object) -> object:
    """把工作表单元格转换为 JSON 可序列化值。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _agent_docx_preview(target: Path) -> JSONResponse:
    """提取 DOCX 中可安全展示的段落和表格。"""
    from docx import Document

    document = Document(str(target))
    paragraphs = [
        paragraph.text
        for paragraph in document.paragraphs[:MAX_AGENT_PREVIEW_ROWS]
        if paragraph.text.strip()
    ]
    tables: list[list[list[str]]] = []
    truncated = len(document.paragraphs) > MAX_AGENT_PREVIEW_ROWS
    for table in document.tables[:MAX_AGENT_PREVIEW_SHEETS]:
        rows = [
            [cell.text for cell in row.cells[:MAX_AGENT_PREVIEW_COLUMNS]]
            for row in table.rows[:MAX_AGENT_PREVIEW_ROWS]
        ]
        tables.append(rows)
        truncated = truncated or len(table.rows) > MAX_AGENT_PREVIEW_ROWS
        truncated = truncated or any(
            len(row.cells) > MAX_AGENT_PREVIEW_COLUMNS for row in table.rows
        )
    truncated = truncated or len(document.tables) > MAX_AGENT_PREVIEW_SHEETS
    return JSONResponse(
        {
            "kind": "docx",
            "paragraphs": paragraphs,
            "tables": tables,
            "truncated": truncated,
        }
    )


def _agent_xlsx_preview(target: Path) -> JSONResponse:
    """读取 XLSX 的有限工作表区域供前端预览。"""
    from openpyxl import load_workbook

    workbook = load_workbook(target, read_only=True, data_only=True)
    try:
        sheets: list[dict[str, object]] = []
        truncated = len(workbook.worksheets) > MAX_AGENT_PREVIEW_SHEETS
        for sheet in workbook.worksheets[:MAX_AGENT_PREVIEW_SHEETS]:
            max_row = min(sheet.max_row or 1, MAX_AGENT_PREVIEW_ROWS)
            max_column = min(sheet.max_column or 1, MAX_AGENT_PREVIEW_COLUMNS)
            rows = [
                [_preview_cell_value(value) for value in row]
                for row in sheet.iter_rows(
                    min_row=1,
                    max_row=max_row,
                    max_col=max_column,
                    values_only=True,
                )
            ]
            sheets.append({"name": sheet.title, "rows": rows})
            truncated = truncated or (sheet.max_row or 0) > MAX_AGENT_PREVIEW_ROWS
            truncated = truncated or (sheet.max_column or 0) > MAX_AGENT_PREVIEW_COLUMNS
        return JSONResponse(
            {"kind": "xlsx", "sheets": sheets, "truncated": truncated}
        )
    finally:
        workbook.close()


def _agent_artifact_preview(target: Path) -> Response:
    """根据文件类型生成不可执行的安全预览响应。"""
    suffix = target.suffix.casefold()
    if suffix in AGENT_PREVIEW_IMAGE_SUFFIXES or suffix == ".pdf":
        media_type = mimetypes.guess_type(target.name)[0]
        if suffix == ".pdf":
            media_type = "application/pdf"
        return FileResponse(
            target,
            media_type=media_type,
            filename=target.name,
            content_disposition_type="inline",
        )
    if suffix in AGENT_PREVIEW_TEXT_SUFFIXES:
        size = target.stat().st_size
        with target.open("rb") as handle:
            content = handle.read(MAX_AGENT_PREVIEW_TEXT_BYTES)
        headers = {"X-Preview-Truncated": "true"} if size > len(content) else None
        return PlainTextResponse(
            content.decode("utf-8-sig", errors="replace"),
            headers=headers,
        )
    try:
        if suffix == ".docx":
            return _agent_docx_preview(target)
        if suffix == ".xlsx":
            return _agent_xlsx_preview(target)
    except (OSError, ValueError, KeyError, BadZipFile, InvalidFileException, PackageNotFoundError) as exc:
        raise HTTPException(status_code=422, detail="文件内容损坏或无法解析预览") from exc
    raise HTTPException(status_code=415, detail="该文件类型暂不支持预览")


@app.get("/api/agent/discussions/{discussion_id}/previews/{file_path:path}")
def preview_agent_artifact(discussion_id: str, file_path: str) -> Response:
    """预览指定会话产物区内的输出文件。"""
    try:
        target = _resolve_agent_artifact(discussion_id, file_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _agent_artifact_preview(target)


@app.get("/api/agent/runs/{run_id}/previews/{file_path:path}")
def preview_legacy_agent_artifact(run_id: str, file_path: str) -> Response:
    """兼容预览旧版按运行保存的 Agent 产物。"""
    try:
        target = _resolve_legacy_agent_artifact(run_id, file_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _agent_artifact_preview(target)


@app.get("/api/agent/discussions/{discussion_id}/files/{file_path:path}")
def download_agent_artifact(discussion_id: str, file_path: str) -> FileResponse:
    """下载指定会话产物区内的输出文件。"""
    try:
        target = _resolve_agent_artifact(discussion_id, file_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(target, filename=target.name)


@app.get("/api/agent/runs/{run_id}/files/{file_path:path}")
def download_legacy_agent_artifact(run_id: str, file_path: str) -> FileResponse:
    """兼容下载旧版按运行保存的 Agent 产物。"""
    try:
        target = _resolve_legacy_agent_artifact(run_id, file_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(target, filename=target.name)


@app.post("/api/agent/commands/{approval_id}/decision")
async def decide_agent_command_endpoint(
    approval_id: str, body: AgentCommandDecisionBody,
) -> dict[str, bool]:
    """仅对当前待审的一条项目命令提交一次批准或拒绝。"""
    decision = _pending_agent_commands.get(approval_id)
    if decision is None or decision.done():
        raise HTTPException(status_code=409, detail="该命令已不再等待批准")
    decision.set_result(body.approved)
    return {"accepted": True}


@app.post("/api/agent/chat/stream")
@app.post("/api/agent/profile/stream", include_in_schema=False)
async def agent_profile_stream_endpoint(
    message: str = Form(""),
    discussion_id: str | None = Form(None),
    title: str | None = Form(None),
    files: list[UploadFile] | None = File(None),
    config_id: str | None = Form(None),
    knowledge_base_id: str | None = Form(None),
    knowledge_base_ids: list[str] | None = Form(None),
    project_id: str | None = Form(None),
) -> StreamingResponse:
    """启动自主决策 Agent，并流式返回回答及工具运行轨迹。"""
    message = message.strip() or "请对上传的数据生成基础画像"
    llm = _discussion_llm_by_id(config_id)
    if llm is None:
        raise HTTPException(status_code=400, detail="尚未配置可用的当前 API 模型")
    try:
        requested_base_ids = (
            knowledge_base_ids
            if isinstance(knowledge_base_ids, list)
            else [knowledge_base_id] if isinstance(knowledge_base_id, str) else None
        )
        bases = require_knowledge_bases(requested_base_ids) if requested_base_ids else []
    except KnowledgeBaseCatalogError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    selected_base_ids = [base.id for base in bases]
    run_id = uuid.uuid4().hex
    active_discussion_id = discussion_id or uuid.uuid4().hex

    stored_session = (
        await asyncio.to_thread(store.get_session, active_discussion_id)
        if discussion_id
        else None
    )
    try:
        project = await asyncio.to_thread(
            _resolve_agent_project,
            stored_session,
            project_id,
        )
    except ProjectWorkspaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    workspace_root = Path(str(project["root_path"])) if project else None
    session_entries = stored_session.get("messages", []) if stored_session else []
    rolling_context = await asyncio.to_thread(
        compile_rolling_context,
        session_entries,
        llm,
    )
    if rolling_context.compaction_error:
        logger.warning(
            "Agent rolling memory compaction failed; using bounded recent context: %s",
            rolling_context.compaction_error,
        )
    if rolling_context.compaction is not None:
        await asyncio.to_thread(
            store.append_message,
            active_discussion_id,
            store.ROLE_TOOL,
            rolling_context.compaction.summary,
            rolling_context.compaction.metadata(),
        )
    uploaded_paths = await _prepare_agent_data_files(files, active_discussion_id, run_id)
    restored_paths = _restore_agent_data_files(session_entries, active_discussion_id)
    data_paths = _merge_agent_data_files(restored_paths, uploaded_paths)
    history = [
        AgentMessage(role=str(item["role"]), content=str(item.get("content") or ""))
        for item in rolling_context.recent_messages
    ]
    output_dir = _agent_output_root(active_discussion_id, workspace_root)
    try:
        python_environment = (
            resolve_project_python(project)
            if project is not None
            else resolve_python_environment(None)
        )
    except ProjectWorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    api_settings = _read_api_settings() or {}
    tavily_settings = api_settings.get("external_services", {}).get("tavily", {})
    tavily_api_key = (
        tavily_settings.get("apiKey")
        if isinstance(tavily_settings, dict)
        else None
    )
    registry = create_agent_tool_registry(
        data_paths,
        output_dir,
        vision_llm=llm,
        knowledge_base_ids=selected_base_ids,
        web_search_api_key=tavily_api_key,
        workspace_root=workspace_root,
        python_executable=python_environment.executable,
        python_environment_source=python_environment.source,
        python_environment_name=python_environment.name,
    )
    state = AgentState(
        user_query=message,
        memory_summary=rolling_context.memory_summary,
        data_paths=[str(path) for path in data_paths],
        output_dir=str(output_dir),
        workspace_root=str(workspace_root) if workspace_root else None,
        project_id=str(project["id"]) if project else None,
        messages=history,
    )
    system_prompt = _load_agent_system_prompt(workspace_root)
    if workspace_root is not None and python_environment.source == "host":
        system_prompt += (
            "\n\n当前项目未扫描到 Python 环境。Agent 自身的 Python 仅可用于创建项目环境，"
            "不能代表当前项目环境，也不能默认用于 execute_python 或项目依赖安装。"
            "若已确认项目中有环境目录（包括项目根目录本身），请显式传入"
            " python_environment；否则先创建环境或向用户说明。"
        )
    loop = AgentLoop(
        llm,
        registry.specs(),
        system_prompt,
        max_iterations=30,
        decision_retries=5,
    )
    approved_command_actions: set[int] = set()

    def consume_command_approval(action: Action) -> bool:
        """只允许已批准的同一条命令执行一次。"""
        key = id(action)
        if key not in approved_command_actions:
            return False
        approved_command_actions.remove(key)
        return True

    harness = Harness(
        registry,
        sandbox=SubprocessSandbox(
            timeout_seconds=AGENT_TOOL_TIMEOUT_SECONDS,
            working_directory=workspace_root or PROJECT_ROOT,
        ),
        command_approval=consume_command_approval,
    )

    async def event_stream():
        started_event: dict[str, object] = {
            "type": "agent_started",
            "run_id": run_id,
            "files": [path.name for path in data_paths],
            "knowledge_base_ids": selected_base_ids,
        }
        trace: list[dict[str, object]] = [started_event]
        yield json.dumps(started_event, ensure_ascii=False) + "\n"
        answer = ""
        knowledge_sources: list[dict[str, str]] = []
        iterator = iter_graph(state, loop, harness)
        try:
            async with asyncio.timeout(AGENT_RUN_TIMEOUT_SECONDS):
                while (event := await asyncio.to_thread(_next_agent_event, iterator)) is not None:
                    if isinstance(event, Finish):
                        if event.thought.strip():
                            payload = _agent_event_payload(event)
                            trace.append(payload)
                            yield json.dumps(payload, ensure_ascii=False) + "\n"
                        answer = event.answer
                        yield json.dumps({"type": "token", "content": answer}, ensure_ascii=False) + "\n"
                        break
                    payload = _agent_event_payload(event)
                    trace.append(payload)
                    if isinstance(event, Action) and event.tool_name == "run_project_command":
                        approval_id = uuid.uuid4().hex
                        decision = asyncio.get_running_loop().create_future()
                        _pending_agent_commands[approval_id] = decision
                        request_payload = {
                            "type": "agent_command_approval_required",
                            "approval_id": approval_id,
                            "command": str(event.arguments.get("command", "")),
                            "python_environment": event.arguments.get("python_environment"),
                            "workspace_root": str(workspace_root),
                        }
                        trace.append(request_payload)
                        yield json.dumps(payload, ensure_ascii=False) + "\n"
                        yield json.dumps(request_payload, ensure_ascii=False) + "\n"
                        try:
                            approved = await asyncio.wait_for(
                                decision, AGENT_COMMAND_APPROVAL_TIMEOUT_SECONDS,
                            )
                        except TimeoutError:
                            approved = False
                        finally:
                            _pending_agent_commands.pop(approval_id, None)
                        if approved:
                            approved_command_actions.add(id(event))
                        decision_payload = {
                            "type": "agent_command_approval_decision",
                            "approval_id": approval_id,
                            "approved": approved,
                        }
                        trace.append(decision_payload)
                        yield json.dumps(decision_payload, ensure_ascii=False) + "\n"
                        continue
                    if isinstance(event, Observation):
                        knowledge_sources = _merge_knowledge_sources(
                            knowledge_sources,
                            _knowledge_sources_from_observation(event),
                        )
                    if (
                        isinstance(event, Observation)
                        and event.tool_name in {"inspect_image", "inspect_generated_image"}
                        and not event.success
                        and event.error == "当前模型不支持识图"
                    ):
                        yield json.dumps(
                            {"type": "error", "detail": event.error},
                            ensure_ascii=False,
                        ) + "\n"
                        return
                    yield json.dumps(payload, ensure_ascii=False) + "\n"
        except TimeoutError:
            yield json.dumps({"type": "error", "detail": "Agent 运行超时，请稍后重试。"}, ensure_ascii=False) + "\n"
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Data agent run failed")
            yield json.dumps({"type": "error", "detail": f"Agent 运行失败：{exc}"}, ensure_ascii=False) + "\n"
            return

        try:
            artifacts = _list_agent_artifacts(
                active_discussion_id,
                run_id,
                workspace_root,
            )
        except OSError:
            logger.exception("Failed to list Agent artifacts")
            artifacts = []
        attachments = [
            {
                "kind": "data" if supports_data_profile(path) else "file",
                "name": path.name,
                "content": path.relative_to(PROJECT_ROOT).as_posix(),
            }
            for path in uploaded_paths
        ]
        saved_discussion_id = await asyncio.to_thread(
            save_discussion_message,
            active_discussion_id,
            message,
            answer,
            knowledge_sources,
            title,
            attachments,
            knowledge_base_ids=selected_base_ids,
            mode="agent",
            assistant_metadata={
                "agent_run_id": run_id,
                "agent_trace": trace,
                "agent_artifacts": artifacts,
            },
            project_id=str(project["id"]) if project else None,
            cwd=str(workspace_root) if workspace_root else None,
        )
        yield json.dumps(
            {
                "type": "done",
                "discussion_id": saved_discussion_id,
                "run_id": run_id,
                "artifacts": artifacts,
                "sources": knowledge_sources,
            },
            ensure_ascii=False,
        ) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get(
    "/api/knowledge/discussions",
    response_model=list[BrainstormDiscussionSummary],
)
async def list_brainstorm_discussions_endpoint(
    project_id: str | None = None,
) -> list[BrainstormDiscussionSummary]:
    """按最近更新时间列出会话，可按项目筛选。"""
    items = await asyncio.to_thread(
        list_discussions,
        *([project_id] if project_id is not None else []),
    )
    return [BrainstormDiscussionSummary(**item) for item in items]


@app.get(
    "/api/knowledge/discussions/{discussion_id}",
    response_model=BrainstormDiscussion,
)
async def get_brainstorm_discussion_endpoint(
    discussion_id: str,
) -> BrainstormDiscussion:
    """Load a discussion so users can read it or continue chatting."""
    discussion = await asyncio.to_thread(get_discussion, discussion_id)
    if discussion is None:
        raise HTTPException(status_code=404, detail="未找到该讨论记录")
    messages = [
        BrainstormDiscussionMessage(
            role=message.get("role") or "user",
            content=message.get("content") or "",
            metadata=message.get("metadata") or {},
        )
        for message in discussion.get("messages", [])
    ]
    return BrainstormDiscussion(
        id=discussion["id"],
        title=discussion["title"],
        project_id=discussion.get("project_id"),
        updated_at=discussion["updated_at"],
        messages=messages,
    )


@app.delete("/api/knowledge/discussions/{discussion_id}")
async def delete_brainstorm_discussion_endpoint(discussion_id: str) -> dict[str, bool]:
    """Permanently delete a saved inspiration discussion."""
    try:
        artifact_dir = _agent_session_root(discussion_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deleted = await asyncio.to_thread(delete_discussion, discussion_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="未找到该讨论记录")
    if artifact_dir.is_dir():
        await asyncio.to_thread(shutil.rmtree, artifact_dir)
    return {"deleted": True}


@app.patch("/api/knowledge/discussions/{discussion_id}/title")
async def rename_brainstorm_discussion_endpoint(discussion_id: str, body: dict) -> dict[str, bool]:
    """Update the user-visible title of a saved discussion."""
    title = body.get("title")
    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=400, detail="讨论名称不能为空")
    renamed = await asyncio.to_thread(rename_discussion, discussion_id, title)
    if not renamed:
        raise HTTPException(status_code=404, detail="未找到该讨论记录")
    return {"renamed": True}


@app.post("/api/runs", response_model=CreateRunResponse)
async def create_run_endpoint(
    problem_text: str | None = Form(None, description="任务文本"),
    problem_file: UploadFile | None = File(None, description="任务文件(.md/.txt)"),
    data_files: list[UploadFile] | None = File(None, description="数据附件(可多个)"),
    llm_config: str = Form(
        "{}",
        description="JSON: provider/api_key/base_url/model/max_tokens/temperature/reasoning_effort",
    ),
    task_name: str | None = Form(None, description="任务名称（用户自定义）"),
    knowledge_base_id: str | None = Form(None, description="任务检索使用的知识库 ID"),
    knowledge_base_ids: list[str] | None = Form(
        None,
        description="任务检索使用的一个或多个知识库 ID",
    ),
):
    """提交一次解题任务（后台异步执行）。"""
    try:
        # 解析模型配置
        try:
                cfg = json.loads(llm_config) if llm_config else {}
                cfg = ModelConfig(**cfg).model_dump(exclude_none=True)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"llm_config 解析失败: {exc}")

        try:
            requested_base_ids = (
                knowledge_base_ids
                if isinstance(knowledge_base_ids, list)
                else [knowledge_base_id] if isinstance(knowledge_base_id, str) else None
            )
            bases = require_knowledge_bases(requested_base_ids)
        except KnowledgeBaseCatalogError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        selected_base_ids = [base.id for base in bases]

        # 任务：文本优先；其次读取文本文件
        problem = problem_text
        run_id = uuid.uuid4().hex[:8]
        output_dir = ARTIFACTS_ROOT / run_id
        input_dir = output_dir / "input"
        input_dir.mkdir(parents=True, exist_ok=True)

        if problem_file is not None and not problem:
            raw = await problem_file.read()
            try:
                problem = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="任务文件暂仅支持 UTF-8 文本(.md/.txt)；二进制请改用 problem_text 粘贴或后续版本接入解析。",
                )
            # 存档任务文件
            dest = input_dir / (problem_file.filename or "problem.txt")
            dest.write_bytes(raw)

        if not problem or not problem.strip():
            raise HTTPException(status_code=400, detail="必须提供 problem_text 或 problem_file")

        # 数据附件存档
        data_paths: list[str] = []
        if data_files:
            for uf in data_files:
                if uf is None:
                    continue
                content = await uf.read()
                if not content:
                    continue
                dest = input_dir / (uf.filename or f"data_{len(data_paths)}.bin")
                dest.write_bytes(content)
                data_paths.append(str(dest))

        preview = problem.strip()[:200].replace("\n", " ")
        create_run(
            run_id,
            preview,
            cfg,
            task_name=task_name,
            knowledge_base_ids=selected_base_ids,
        )

        task = asyncio.create_task(
            execute_run(
                run_id=run_id,
                problem_text=problem,
                data_paths=data_paths,
                output_dir=str(output_dir),
                model_config=cfg,
                knowledge_base_ids=selected_base_ids,
            )
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

        return {"run_id": run_id, "status": "queued", "output_dir": str(output_dir)}
    except HTTPException:
        # 已显式构造的 4xx 错误，原样返回
        raise
    except Exception as exc:  # noqa: BLE001
        # 未捕获异常：把完整堆栈打到 stderr，并把异常类型/消息作为 detail 返回，
        # 便于前端和本地终端直接看到根因（仅本地开发用，生产请收敛为通用提示）。
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"server error: {type(exc).__name__}: {exc}",
        )


@app.get("/api/runs", response_model=list[RunSummary])
async def list_runs_endpoint():
    """运行历史列表。"""
    return [RunSummary.from_row(r) for r in list_runs()]


@app.post("/api/runs/cleanup-stale")
async def cleanup_stale_endpoint(max_age_seconds: int = 300):
    """手动把因服务重启而卡在 running/queued 的僵尸任务转为 failed。

    正常情况启动时已自动清理；此接口用于运行中手动触发（无需重启）。
    """
    cleaned = cleanup_stale_runs(max_age_seconds=max_age_seconds)
    return {"cleaned": cleaned}


@app.get("/api/runs/{run_id}", response_model=RunDetail)
async def get_run_endpoint(run_id: str):
    """运行详情（含进度事件与产物清单）。"""
    row = get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    return RunDetail.from_row(row)


@app.get("/api/runs/{run_id}/monitor", response_model=RunMonitor)
async def get_run_monitor_endpoint(run_id: str):
    """返回实时 token / LLM 耗时；结束任务回退到最终预算汇总。"""
    row = get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run 不存在")

    live = get_run_monitor(run_id)
    if live is not None:
        return RunMonitor(active=True, **live)

    summary = next(
        (
            event
            for event in reversed(row.get("progress") or [])
            if event.get("type") == "budget_summary"
        ),
        None,
    )
    if summary is None:
        return RunMonitor()
    return RunMonitor(
        time_total=int(summary.get("time_total", 0)),
        token_total=int(summary.get("token_total", 0)),
    )


@app.delete("/api/runs/{run_id}")
async def delete_run_endpoint(run_id: str):
    """删除一条运行记录（DB 行 + 产物目录）。"""
    result = delete_run(run_id)
    if not result["deleted"]:
        if result["reason"] == "not_found":
            raise HTTPException(status_code=404, detail="run 不存在")
        raise HTTPException(status_code=409, detail=result["reason"])
    return {"ok": True, "run_id": run_id}


@app.patch("/api/runs/{run_id}/name")
async def rename_run_endpoint(run_id: str, name: str = Form(...)):
    """更新任务名称。"""
    if not rename_run(run_id, name):
        raise HTTPException(status_code=404, detail="run 不存在")
    return {"ok": True, "run_id": run_id, "task_name": name}


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run_endpoint(run_id: str):
    """中断一个正在执行（queued/running）的任务。

    若任务已结束（succeeded/failed/cancelled）则返回 409；
    若仍在执行则设置取消标志，后台线程在下一个节点边界退出并标记 cancelled。
    """
    result = cancel_run(run_id)
    if not result["cancelled"]:
        if result["reason"] == "not_found":
            raise HTTPException(status_code=404, detail="run 不存在")
        raise HTTPException(status_code=409, detail=result["reason"])
    return {"ok": True, "run_id": run_id, "status": "cancelled"}


_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


@app.get("/api/runs/{run_id}/progress/stream")
async def stream_run_progress(run_id: str, after: int = 0):
    """以 SSE 实时推送某个 run 的进度事件（节点级）。

    前端用原生 EventSource 订阅（GET，无 body）。服务端周期性读取注册表，
    把「新增」的 progress 事件逐条推给客户端；run 进入终态后发送 done 并关闭连接。
    相比前端 2s 轮询，这里延迟更低、体验更接近"实时输出建模进度"。

    after 参数：跳过前 N 条已加载的事件，用于断线重连/恢复模式时避免重复。
    """
    if get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run 不存在")

    async def gen():
        sent = max(0, after)
        # 连接建立即回执，便于前端确认订阅成功
        yield f'data: {json.dumps({"type": "subscribed", "run_id": run_id}, ensure_ascii=False)}\n\n'
        while True:
            row = get_run(run_id)
            if row is None:
                yield 'data: {"type":"error","message":"run 不存在"}\n\n'
                return
            events = row.get("progress") or []
            status = row.get("status")
            for ev in events[sent:]:
                yield f"data: {json.dumps({'type': 'event', 'event': ev}, ensure_ascii=False)}\n\n"
            sent = len(events)
            if status in _TERMINAL_STATUSES:
                done = {"type": "done", "status": status, "error": row.get("error")}
                yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
                return
            # SSE 注释行：保持连接活跃，避免代理/浏览器因空闲断开
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关闭反向代理缓冲，确保事件即时下发
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/runs/{run_id}/budget-confirm")
async def confirm_budget_endpoint(run_id: str, body: BudgetConfirmBody):
    """确认某小问的预算覆盖。

    当 run 暂停在 configure_question_budget 节点等待用户确认时，前端弹窗提交：
      - use_defaults=true：沿用默认预算；
      - limits：覆盖当前预算阶段允许调整的项目。
    无待确认请求时返回 409。
    """
    pending = get_pending_budget(run_id)
    if not pending:
        raise HTTPException(status_code=409, detail="当前没有待确认的预算请求")
    decision = None if body.use_defaults else (body.limits or None)
    # 仅保留已定义的、值为正整数的预算项；当前阶段会在后台回调中决定实际采用哪些项。
    if decision:
        decision = {
            key: value
            for key, value in decision.items()
            if key in {
                BudgetType.SEARCH.value,
                BudgetType.CODE_REPAIR.value,
                BudgetType.VALIDATION_ITERATION.value,
                BudgetType.INTAKE_RETRY.value,
                BudgetType.PAPER_REVISION.value,
            }
            and isinstance(value, int)
            and value > 0
        }
    ok = submit_budget_decision(run_id, decision)
    if not ok:
        raise HTTPException(status_code=409, detail="预算请求已被取消或已确认")
    return {"ok": True, "run_id": run_id, "use_defaults": body.use_defaults, "limits": decision}


@app.post("/api/runs/{run_id}/clarification")
async def submit_clarification_endpoint(
    run_id: str,
    action: str = Form(..., description="terminate 或 continue"),
    data_files: list[UploadFile] | None = File(None, description="补充材料(可多个)"),
):
    """G0 硬失败时用户选择终止或上传补充材料继续建模。

    - action=terminate：立即终止本次建模任务。
    - action=continue：上传补充材料后重跑输入摄入。
    无待确认请求时返回 409。
    """
    pending = get_pending_clarification(run_id)
    if not pending:
        raise HTTPException(status_code=409, detail="当前没有待确认的澄清请求")

    new_data_paths: list[str] = []
    if action == "continue" and data_files:
        input_dir = ARTIFACTS_ROOT / run_id / "input"
        input_dir.mkdir(parents=True, exist_ok=True)
        for uf in data_files:
            if uf is None:
                continue
            content = await uf.read()
            if not content:
                continue
            dest = input_dir / (uf.filename or f"supplement_{len(new_data_paths)}.bin")
            dest.write_bytes(content)
            new_data_paths.append(str(dest))

    decision = {"action": action, "new_data_paths": new_data_paths}
    ok = submit_clarification_decision(run_id, decision)
    if not ok:
        raise HTTPException(status_code=409, detail="澄清请求已被取消或已确认")
    return {"ok": True, "run_id": run_id, "action": action, "new_files": len(new_data_paths)}


@app.get("/api/runs/{run_id}/paper")
async def get_paper_endpoint(run_id: str):
    """报告 Markdown 文本。"""
    try:
        path = resolve_artifact(run_id, "paper.md")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.get("/api/runs/{run_id}/figures")
async def get_figures_endpoint(run_id: str):
    """图表文件清单。"""
    if not get_run(run_id):
        raise HTTPException(status_code=404, detail="run 不存在")
    return {"figures": list_figures(run_id)}


@app.get("/api/runs/{run_id}/files/{file_path:path}")
async def download_file_endpoint(run_id: str, file_path: str):
    """下载任意产物文件（安全限制在 artifacts/<run_id>/ 内）。"""
    try:
        target = resolve_artifact(run_id, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return FileResponse(target)


# ---------------------------------------------------------------------------
# 前端静态托管（若已构建 web/dist）
# ---------------------------------------------------------------------------
if WEB_DIST.exists():
    # 健康检查须在静态托管挂载之前注册，否则会被 "/" 挂载的 StaticFiles 吞掉（返回 404）
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
else:

    @app.get("/")
    async def root():
        return {
            "message": "MMAgent Web API 运行中",
            "docs": "/docs",
            "hint": "前端未构建；构建 web 后访问根路径即可使用界面。",
        }
