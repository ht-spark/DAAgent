"""
FastAPI 入口
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
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

from data_analysis.agent.graph import (
    PlanCreated,
    TodoCompleted,
    TodoFailed,
    TodoStarted,
    iter_plan_graph,
)
from data_analysis.agent.loop import Action, AgentLoop, Finish
from data_analysis.agent.planner import Planner
from data_analysis.agent.state import (
    AgentMessage,
    AgentState,
    Observation,
    collect_work_trajectory_from_session,
    discover_artifacts,
)
from data_analysis.experts import (
    ExpertEditableSpec,
    ExpertRegistry,
    ExpertRegistryError,
)
from data_analysis.skills import (
    SkillAction,
    SkillObservation,
    SkillRegistry,
    SkillRegistryError,
    SkillRuntime,
)
from data_analysis.context import (
    AttachmentText,
    compile_attachment_context,
    compile_rolling_context,
    compose_user_message,
    estimate_context_usage_after_turn,
)
from data_analysis.cancellation import TaskCancelledError
from data_analysis.llm import (
    LLM_CALL_TIMEOUT_SECONDS as AGENT_LLM_TIMEOUT_SECONDS,
    create_llm,
)
from data_analysis.runtime import (
    ExecutionContext,
    Harness,
    PythonEnvironmentError,
    SubprocessSandbox,
    resolve_python_environment,
)
from data_analysis.tools import create_agent_tool_registry, supports_data_profile
from data_analysis.tools.terminal_tools import (
    DEFAULT_TERMINAL_SHELL,
    TERMINAL_SHELLS,
    TERMINAL_TOOL_NAMES,
)
from server import store
from server import converter_store
from server.project_workspaces import (
    ProjectWorkspaceError,
    create_project as register_project,
    list_project_file_tree,
    load_project_context,
    require_project,
    resolve_project_file,
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
from server.document_tools import (
    MAX_DOCUMENT_BYTES,
    DocumentConversionError,
    supported_extensions,
    to_markdown,
    to_markdown_from_bytes,
)
from server.schemas import (
    AgentCommandDecisionBody,
    BrainstormDiscussion,
    BrainstormDiscussionSummary,
    BrainstormDiscussionMessage,
    DocumentSaveBody,
    KnowledgeDocument,
    KnowledgeBaseCreateBody,
    KnowledgeBaseSummary,
    KnowledgeBaseUpdateBody,
    KnowledgeChunkEmbedRequest,
    KnowledgeChunkEmbedResponse,
    KnowledgeChunkEmbedProgress,
    KnowledgeDocumentsDeleteBody,
    KnowledgeStatus,
    ProjectCreateBody,
    ProjectSummary,
    ProjectUpdateBody,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"
AGENT_RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "agent_uploads"
SKILLS_ROOT = PROJECT_ROOT / "data_analysis" / "skills"
EXPERTS_ROOT = PROJECT_ROOT / "data_analysis" / "experts"
# 启动时只清理超过 24 小时的附件运行目录，避免误删另一个仍在运行的服务实例。
AGENT_RUNTIME_MAX_AGE_SECONDS = 24 * 60 * 60
WEB_DIST = PROJECT_ROOT / "web" / "dist"
API_SETTINGS_PATH = Path(__file__).resolve().parent / "api_settings.json"
AGENT_TOOL_TIMEOUT_SECONDS = 2 * 60 * 60          # Agent 单次工具执行超时时间（2 小时）
AGENT_RUN_TIMEOUT_SECONDS = 3 * 60 * 60           # Agent 整轮运行硬超时时间（3 小时）
AGENT_MAX_DECISIONS = 160                         # 整轮运行共享的决策轮数上限（含全部 Todo 与最终汇总）
AGENT_COMMAND_APPROVAL_TIMEOUT_SECONDS = 300      # 单次终端命令等待人工批准的上限
_pending_agent_commands: dict[str, asyncio.Future[bool]] = {}
_skill_registry = SkillRegistry(SKILLS_ROOT)
_expert_registry = ExpertRegistry(EXPERTS_ROOT, _skill_registry)


@dataclass
class AgentChatRun:
    """保存可重连的 Agent 流事件和协作式终止状态。"""

    discussion_id: str
    message: str
    events: list[str] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    done: bool = False
    cancel_requested: bool = False
    approval_id: str | None = None
    runtime_dir: Path | None = None


_agent_chat_runs: dict[str, AgentChatRun] = {}


async def _replay_agent_chat_run(run: AgentChatRun):
    """向当前订阅者回放历史事件并继续等待新事件。"""
    index = 0
    while True:
        async with run.condition:
            await run.condition.wait_for(lambda: index < len(run.events) or run.done)
            pending = run.events[index:]
            index = len(run.events)
            done = run.done
        for line in pending:
            payload = json.loads(line)
            if (
                payload.get("type") == "agent_command_approval_required"
                and run.approval_id != payload.get("approval_id")
            ):
                continue
            yield line
        if done:
            return


async def _publish_agent_chat_run(run: AgentChatRun, stream) -> None:
    """独立于浏览器连接运行 Agent 并缓存可重放事件。"""
    try:
        async for line in stream:
            async with run.condition:
                run.events.append(line)
                run.condition.notify_all()
    except Exception as exc:
        logger.exception("Background Agent chat run failed")
        async with run.condition:
            run.events.append(json.dumps({"type": "error", "detail": f"Agent 运行失败：{exc}"}, ensure_ascii=False) + "\n")
            run.condition.notify_all()
    finally:
        if run.runtime_dir is not None:
            await asyncio.to_thread(_cleanup_agent_runtime_dir, run.runtime_dir)
        async with run.condition:
            run.done = True
            run.condition.notify_all()
MAX_AGENT_ATTACHMENTS = 10                        # 数据 Agent 单次最多接收的附件数
MAX_AGENT_ATTACHMENT_BYTES = 100 * 1024 * 1024    # 数据 Agent 单附件大小上限
MAX_AGENT_PREVIEW_TEXT_BYTES = 20 * 1024 * 1024    #文本预览最多读取20MB
MAX_AGENT_PREVIEW_ROWS = 200                      # 表格预览最多展示200行
MAX_AGENT_PREVIEW_COLUMNS = 40                    # 表格预览最多展示40列
MAX_AGENT_PREVIEW_SHEETS = 8                      # 工作簿预览最多展示8个工作表
# 轨迹单行摘要允许下发的参数白名单与单值长度上限。
# 这些参数是「读取 · <路径>」「运行命令 · <命令>」这类摘要的唯一来源；
# 其余参数（content / old_text / new_text 等）可能很长，既不适合公开展示，
# 也会撑大会话持久化体积，因此一律不下发。
SUMMARY_ARGUMENT_KEYS = (
    "file_path",
    "relative_path",
    "path",
    "relative_dir",
    "pattern",
    "query",
    "command",
    "file_name",
)
SUMMARY_ARGUMENT_MAX_LENGTH = 200
FINAL_ANSWER_CHUNK_CHARS = 16                   # 最终回答每个流事件的最大字符数
FINAL_ANSWER_CHUNK_DELAY_SECONDS = 0.016        # 让浏览器逐帧渲染，避免网络层合并成整段
AGENT_ATTACHMENT_SUFFIXES = frozenset(
    {
        ".bmp",
        ".csv",
        ".docx",
        ".gif",
        ".htm",
        ".html",
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
        ".svg",
        ".tif",
        ".tiff",
        ".txt",
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


@app.middleware("http")
async def _guard_process_level_exceptions(request: Request, call_next):
    """兜住请求处理中冒出的进程级异常，避免单个请求带走整个后端。

    少数第三方库在清理索引 / 临时文件时会抛出 ``SystemExit``（例如删除动作被
    外部安全守卫中止）。它属于 ``BaseException``：既不会被业务代码的
    ``except Exception`` 捕获，Starlette 的错误中间件也不处理，于是会一路冒到
    事件循环所在的主线程，直接把 uvicorn 进程结束掉——表现为「点一下按钮后端就没了」。

    这里统一转成 500 响应并记录日志，服务本身继续存活。``asyncio.CancelledError``
    不在拦截范围内，任务取消语义保持不变。
    """
    try:
        return await call_next(request)
    except (SystemExit, KeyboardInterrupt):
        logging.getLogger(__name__).exception(
            "请求处理中捕获进程级异常，已拦截以免服务退出：%s %s",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    "服务内部异常（第三方库主动中止进程）已被拦截，"
                    "本次操作未完成且未生效，请查看后端日志后重试"
                )
            },
        )

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
    """启动时完成统一 Agent 所需的数据迁移。"""
    migrate_legacy_document_ids()
    await asyncio.to_thread(_cleanup_stale_agent_runtime_dirs)


def _read_api_settings() -> dict | None:
    """Load the persisted API settings snapshot, or None when absent/corrupt."""
    if not API_SETTINGS_PATH.exists():
        return None
    try:
        data = json.loads(API_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_api_settings(data: dict) -> None:
    """以同目录原子替换方式写入本机设置文件。"""
    tmp_path = API_SETTINGS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(API_SETTINGS_PATH)


def _configured_terminal_shell(settings: dict | None = None) -> str:
    """读取已保存终端类型，缺失或非法时使用 PowerShell。"""
    data = settings if settings is not None else (_read_api_settings() or {})
    general = data.get("general")
    value = general.get("terminal_type") if isinstance(general, dict) else None
    return value if value in TERMINAL_SHELLS else DEFAULT_TERMINAL_SHELL


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


@app.get("/api/projects/{project_id}/files/previews/{file_path:path}")
async def preview_project_file_endpoint(project_id: str, file_path: str) -> Response:
    """在项目目录边界内返回文件的只读预览。"""
    try:
        target = await asyncio.to_thread(resolve_project_file, project_id, file_path)
    except ProjectWorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await asyncio.to_thread(_agent_artifact_preview, target)


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


_WINDOWS_FOLDER_DIALOG_SCRIPT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$initial = [Environment]::GetEnvironmentVariable('DAAGENT_INITIAL_FOLDER')
if (-not $initial -or -not (Test-Path -LiteralPath $initial -PathType Container)) {
    $initial = 0
}
$shell = New-Object -ComObject Shell.Application
try {
    $folder = $shell.BrowseForFolder(0, '选择项目文件夹', 65, $initial)
    if ($null -ne $folder) {
        [Console]::Out.Write($folder.Self.Path)
    }
} finally {
    if ($null -ne $folder) {
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($folder)
    }
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}
"""


def _open_windows_folder_dialog(initial_dir: str | None = None) -> str | None:
    """通过 Windows Shell COM 打开资源管理器文件夹选择器。"""
    environment = os.environ.copy()
    environment["DAAGENT_INITIAL_FOLDER"] = initial_dir or str(Path.home())
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-STA",
                "-WindowStyle",
                "Hidden",
                "-Command",
                _WINDOWS_FOLDER_DIALOG_SCRIPT,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=300,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("系统缺少 PowerShell，无法打开文件夹选择器") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("文件夹选择器等待超时") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "未知错误"
        raise RuntimeError(f"无法打开 Windows 文件夹选择器：{detail}")
    selected = result.stdout.strip()
    if not selected:
        return None
    return str(Path(selected).expanduser().resolve())


def _open_native_folder_dialog(initial_dir: str | None = None) -> str | None:
    """打开系统文件夹选择框，返回绝对路径；用户取消时返回 None。"""
    if sys.platform == "win32":
        return _open_windows_folder_dialog(initial_dir)
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
    existing = _read_api_settings() or {}
    payload = {
        "configs": body.get("configs", []),
        "active_id": body.get("active_id") if isinstance(body.get("active_id"), str) else None,
        "external_services": body.get("external_services")
        if isinstance(body.get("external_services"), dict)
        else {},
        "general": existing.get("general", {}),
    }
    _write_api_settings(payload)
    return {"ok": True}


@app.get("/api/settings/general")
async def get_general_settings_endpoint() -> dict:
    """返回终端类型等本机常规设置。"""
    data = _read_api_settings() or {}
    general = data.get("general")
    saved = (
        isinstance(general, dict)
        and general.get("terminal_type") in TERMINAL_SHELLS
    )
    return {
        "saved": saved,
        "terminal_type": _configured_terminal_shell(data),
    }


@app.put("/api/settings/general")
async def save_general_settings_endpoint(body: dict) -> dict:
    """校验并保存本机常规设置，同时保留 API 配置。"""
    terminal_type = body.get("terminal_type")
    if terminal_type not in TERMINAL_SHELLS:
        raise HTTPException(
            status_code=422,
            detail="terminal_type 只支持 powershell、bash 或 git-bash",
        )
    payload = _read_api_settings() or {}
    general = payload.get("general")
    preserved = dict(general) if isinstance(general, dict) else {}
    preserved["terminal_type"] = terminal_type
    payload["general"] = preserved
    _write_api_settings(payload)
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


def _create_llm_from_api_config(
    config: dict,
    reasoning_effort: str | None = None,
) -> object | None:
    """Build an LLM client from one persisted API settings entry."""
    return create_llm(
        provider=config.get("provider"),
        api_key=config.get("apiKey"),
        base_url=config.get("baseUrl"),
        model=config.get("model"),
        max_tokens=config.get("maxTokens"),
        temperature=config.get("temperature"),
        reasoning_effort=(
            reasoning_effort
            if reasoning_effort is not None
            else config.get("reasoningEffort")
        ),
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


def _discussion_llm_by_id(
    config_id: str | None,
    reasoning_effort: str | None = None,
):
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
    return _create_llm_from_api_config(config, reasoning_effort)


def _agent_context_window_tokens(config_id: str | None) -> int | None:
    """读取当前模型声明的上下文窗口，用于推导滚动压缩阈值。

    取值顺序：API 配置中的 ``contextWindow`` → 环境变量 ``DAAGENT_CONTEXT_WINDOW``。
    未声明时返回 None，由滚动记忆使用保守的默认窗口。

    Args:
        config_id: 前端选择的 API 配置 id；缺省时使用当前激活配置。

    Returns:
        可用的上下文窗口 token 数；无法确定时返回 None。
    """
    settings = _read_api_settings() or {}
    configs = settings.get("configs")
    active_id = settings.get("active_id")
    selected: dict[str, Any] | None = None
    if isinstance(configs, list):
        wanted = config_id if isinstance(config_id, str) and config_id else active_id
        selected = next(
            (
                item
                for item in configs
                if isinstance(item, dict) and item.get("id") == wanted
            ),
            None,
        )
    candidates: list[Any] = [
        selected.get("contextWindow") if isinstance(selected, dict) else None,
        os.environ.get("DAAGENT_CONTEXT_WINDOW"),
    ]
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit() and int(value) > 0:
            return int(value)
    return None


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
            size_limit_mb = MAX_AGENT_ATTACHMENT_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=400,
                detail=f"附件 {original_name} 超过 {size_limit_mb} MB 限制",
            )

        candidate = original_name
        serial = 2
        while candidate.casefold() in used_names:
            candidate = f"{Path(original_name).stem}-{serial}{suffix}"
            serial += 1
        used_names.add(candidate.casefold())
        prepared.append((candidate, content))

    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", discussion_id) is None:
        raise HTTPException(status_code=400, detail="Agent 会话标识无效")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id) is None:
        raise HTTPException(status_code=400, detail="Agent 运行标识无效")
    input_dir = (AGENT_RUNTIME_ROOT / run_id / "input").resolve()
    runtime_root = AGENT_RUNTIME_ROOT.resolve()
    if runtime_root not in input_dir.parents:
        raise HTTPException(status_code=400, detail="附件运行目录越界")
    runtime_dir = input_dir.parent
    saved_paths: list[Path] = []
    try:
        input_dir.mkdir(parents=True, exist_ok=False)
        for name, content in prepared:
            target = (input_dir / name).resolve()
            if input_dir not in target.parents:
                raise HTTPException(status_code=400, detail="附件路径无效")
            await asyncio.to_thread(target.write_bytes, content)
            saved_paths.append(target)
    except Exception:
        await asyncio.to_thread(_cleanup_agent_runtime_dir, runtime_dir)
        raise
    return saved_paths


def _compile_uploaded_attachment_context(paths: list[Path], llm: object) -> str:
    """提取本轮附件文本，并编译为可持久化的上下文。"""
    attachments: list[AttachmentText] = []
    supported_documents = frozenset(supported_extensions())
    for path in paths:
        suffix = path.suffix.casefold()
        try:
            if supports_data_profile(path):
                text = (
                    f"[结构化数据附件：{path.name}（{path.stat().st_size} 字节）。"
                    "完整数据未直接注入模型上下文；文件已授权给本轮 Agent，"
                    "应使用 profile_data 获取字段与质量画像，并按需使用 "
                    "inspect_data_rows 读取真实记录。]"
                )
            elif suffix in AGENT_PREVIEW_TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            elif suffix in supported_documents:
                text = to_markdown(path, max_bytes=MAX_AGENT_ATTACHMENT_BYTES)
            else:
                text = (
                    f"[非文本附件：{path.name}。本轮运行可使用附件工具检查其内容，"
                    "该文件不计入字符型上下文。]"
                )
        except (OSError, ValueError, DocumentConversionError) as exc:
            text = f"[附件 {path.name} 无法提取文本：{exc}]"
        attachments.append(AttachmentText(name=path.name, text=text))
    return compile_attachment_context(attachments, llm)


def _attachment_metadata(paths: list[Path]) -> list[dict[str, str]]:
    """生成不含临时磁盘路径的附件展示元数据。"""
    return [
        {
            "kind": "data" if supports_data_profile(path) else "file",
            "name": path.name,
            "content": "已加载到会话上下文",
        }
        for path in paths
    ]


def _cleanup_agent_runtime_dir(runtime_dir: Path) -> None:
    """只清理受控运行根目录下的本轮临时附件。"""
    root = AGENT_RUNTIME_ROOT.resolve()
    target = runtime_dir.resolve(strict=False)
    if root not in target.parents:
        logger.error("拒绝清理越界的 Agent 临时目录: %s", target)
        return
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("清理 Agent 临时目录失败: %s", target, exc_info=True)


def _cleanup_stale_agent_runtime_dirs(*, now: float | None = None) -> list[Path]:
    """清理服务异常退出后遗留的过期附件目录，并返回成功删除的路径。"""
    root = AGENT_RUNTIME_ROOT.resolve(strict=False)
    if not root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - AGENT_RUNTIME_MAX_AGE_SECONDS
    removed: list[Path] = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        target = candidate.resolve(strict=False)
        if target.parent != root:
            logger.warning("跳过越界的 Agent 临时目录: %s", target)
            continue
        try:
            modified_at = candidate.stat().st_mtime
        except OSError:
            logger.warning("无法读取 Agent 临时目录时间: %s", candidate, exc_info=True)
            continue
        if modified_at > cutoff:
            continue
        try:
            shutil.rmtree(target)
        except OSError:
            logger.warning("清理过期 Agent 临时目录失败: %s", target, exc_info=True)
            continue
        removed.append(target)
    return removed


def _load_agent_system_prompt(workspace_root: Path | None = None) -> str:
    """读取系统提示词，并附加当前项目根目录中的 AGENTS.md 核心约束。"""
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


def _final_answer_chunks(answer: str) -> list[str]:
    """把最终回答切成可逐步下发的小块，并保证拼接后内容不变。"""
    return [
        answer[index:index + FINAL_ANSWER_CHUNK_CHARS]
        for index in range(0, len(answer), FINAL_ANSWER_CHUNK_CHARS)
    ]


def _agent_event_payload(
    event: Action | SkillAction | Observation | SkillObservation | Finish | PlanCreated | TodoStarted | TodoCompleted | TodoFailed,
) -> dict[str, object]:
    """把内部 Agent 事件转换为可公开展示的精简载荷。"""
    if isinstance(event, PlanCreated):
        payload: dict[str, object] = {
            "type": "agent_plan",
            "objective": event.plan.objective,
            "reasoning_summary": event.plan.reasoning_summary,
            "todos": [
                {
                    "id": todo.id,
                    "objective": todo.objective,
                    "depends_on": todo.depends_on,
                    "expected_outputs": todo.expected_outputs,
                }
                for todo in event.plan.todos
            ],
        }
        return payload
    if isinstance(event, TodoStarted):
        return {
            "type": "agent_todo_started",
            "todo_id": event.todo_id,
            "objective": event.objective,
        }
    if isinstance(event, TodoCompleted):
        return {
            "type": "agent_todo_completed",
            "todo_id": event.todo_id,
            "result_summary": event.result_summary,
        }
    if isinstance(event, TodoFailed):
        return {
            "type": "agent_todo_failed",
            "todo_id": event.todo_id,
            "error": event.error,
        }
    if isinstance(event, Finish):
        return {
            "type": "agent_reasoning",
            "summary": event.thought,
        }
    if isinstance(event, Action):
        payload = {
            "type": "agent_action",
            "thought": event.thought,
            "tool_name": event.tool_name,
        }
        # 只保留摘要类短参数：它们是「读取 · path」「运行命令 · command」这类
        # 单行轨迹摘要的唯一来源；完整 arguments 可能含大段正文（content、
        # old_text、new_text 等），既不适合公开展示也会撑大持久化体积。
        summary_args = {
            key: value.strip()[:SUMMARY_ARGUMENT_MAX_LENGTH]
            for key in SUMMARY_ARGUMENT_KEYS
            if isinstance((value := event.arguments.get(key)), str) and value.strip()
        }
        if summary_args:
            payload["arguments"] = summary_args
        code = event.arguments.get("code")
        if event.tool_name == "execute_python" and isinstance(code, str):
            payload["code"] = code
        return payload
    if isinstance(event, SkillAction):
        payload = {
            "type": "agent_skill_action",
            "thought": event.thought,
            "operation": event.operation,
            "skill_id": event.skill_id,
            "resource_id": event.resource_id,
            "entrypoint_id": event.entrypoint_id,
        }
        return payload
    if isinstance(event, SkillObservation):
        return {
            "type": "agent_skill_observation",
            "operation": event.operation,
            "skill_id": event.skill_id,
            "resource_id": event.resource_id,
            "entrypoint_id": event.entrypoint_id,
            "success": event.success,
            "duration_ms": event.duration_ms,
            "error": event.error,
        }
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
        if event.tool_name in {"edit_workspace_file", "edit_attachment_file"}:
            payload["details"] = str(event.result.get("diff", ""))[:8000]
        elif event.tool_name in TERMINAL_TOOL_NAMES:
            command = str(event.result.get("command", ""))
            output = str(event.result.get("output", ""))
            changed_files = event.result.get("changed_files")
            changed_text = ""
            if isinstance(changed_files, list) and changed_files:
                changed_text = "\n变更文件:\n" + "\n".join(
                    f"- {path}" for path in changed_files if isinstance(path, str)
                )
                if event.result.get("changes_truncated") is True:
                    changed_text += "\n- …变更列表或扫描范围已截断"
            payload["details"] = (
                f"[{event.result.get('shell', 'shell')}] {command}\n"
                f"exit_code: {event.result.get('exit_code')}\n"
                f"{output}{changed_text}"
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
        or relative_path.parts[0].casefold() in {".runs", ".runtime"}
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
    before_run: dict[str, tuple[int, int]] | None = None,
) -> list[dict[str, object]]:
    """列出会话产物区内的输出文件，可限定为本轮新增或更新的文件。"""
    root = _agent_output_root(discussion_id, workspace_root)
    if not root.is_dir():
        return []
    artifacts: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if relative_path.parts[0].casefold() in {".runs", ".runtime"}:
            continue
        stat = path.stat()
        artifact_path = relative_path.as_posix()
        if before_run is not None and before_run.get(artifact_path) == (
            stat.st_mtime_ns,
            stat.st_size,
        ):
            continue
        artifacts.append(
            {
                "discussion_id": discussion_id,
                "run_id": run_id,
                "path": artifact_path,
                "name": path.name,
                "size_bytes": stat.st_size,
            }
        )
    return artifacts


def _snapshot_agent_artifacts(
    discussion_id: str,
    workspace_root: Path | None = None,
) -> dict[str, tuple[int, int]]:
    """记录运行开始前的产物指纹，用于区分当前轮输出与历史输出。"""
    root = _agent_output_root(discussion_id, workspace_root)
    if not root.is_dir():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if relative_path.parts[0].casefold() in {".runs", ".runtime"}:
            continue
        stat = path.stat()
        snapshot[relative_path.as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


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
    """仅对当前待审的一次终端命令提交一次批准或拒绝。"""
    decision = _pending_agent_commands.get(approval_id)
    if decision is None or decision.done():
        raise HTTPException(status_code=409, detail="该命令已不再等待批准")
    decision.set_result(body.approved)
    return {"accepted": True}


@app.get("/api/skills")
async def list_skills_endpoint() -> dict[str, object]:
    """返回已安装 Skill 的发现阶段元数据。"""
    try:
        skills = await asyncio.to_thread(_skill_registry.discover)
    except SkillRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"skills": [item.model_dump(mode="json") for item in skills]}


@app.get("/api/experts")
async def list_experts_endpoint() -> dict[str, object]:
    """返回当前可启动的内置专家定义。"""
    try:
        experts = await asyncio.to_thread(_expert_registry.discover)
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "experts": [
            item.model_dump(mode="json") for item in experts if item.enabled
        ]
    }


@app.get("/api/expert-drafts")
async def list_expert_drafts_endpoint() -> dict[str, object]:
    """返回全部可编辑专家草稿。"""
    try:
        drafts = await asyncio.to_thread(_expert_registry.list_drafts)
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"drafts": [item.model_dump(mode="json") for item in drafts]}


@app.post("/api/expert-drafts")
async def create_expert_draft_endpoint(
    body: ExpertEditableSpec,
) -> dict[str, object]:
    """创建一个新的专家草稿。"""
    try:
        draft = await asyncio.to_thread(_expert_registry.create_draft, body)
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return draft.model_dump(mode="json")


@app.put("/api/expert-drafts/{expert_id}")
async def update_expert_draft_endpoint(
    expert_id: str,
    body: ExpertEditableSpec,
) -> dict[str, object]:
    """保存已有专家草稿。"""
    try:
        draft = await asyncio.to_thread(
            _expert_registry.update_draft,
            expert_id,
            body,
        )
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return draft.model_dump(mode="json")


@app.delete("/api/expert-drafts/{expert_id}")
async def delete_expert_draft_endpoint(expert_id: str) -> dict[str, str]:
    """删除一个未发布专家草稿。"""
    try:
        await asyncio.to_thread(_expert_registry.delete_draft, expert_id)
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": expert_id}


@app.post("/api/experts/{expert_id}/draft")
async def create_draft_from_expert_endpoint(expert_id: str) -> dict[str, object]:
    """从当前已发布专家创建或读取编辑草稿。"""
    try:
        draft = await asyncio.to_thread(
            _expert_registry.draft_from_published,
            expert_id,
        )
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return draft.model_dump(mode="json")


@app.post("/api/expert-drafts/{expert_id}/validate")
async def validate_expert_draft_endpoint(expert_id: str) -> dict[str, object]:
    """校验专家草稿和全部 Skill 依赖。"""
    try:
        draft = await asyncio.to_thread(_expert_registry.validate_draft, expert_id)
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "valid": True,
        "runtime_skill_ids": list(draft.runtime_skill_ids),
    }


@app.post("/api/expert-drafts/{expert_id}/publish")
async def publish_expert_draft_endpoint(expert_id: str) -> dict[str, object]:
    """发布一个专家草稿并生成不可变版本归档。"""
    try:
        expert = await asyncio.to_thread(_expert_registry.publish_draft, expert_id)
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return expert.model_dump(mode="json")


@app.get("/api/experts/{expert_id}")
async def get_expert_endpoint(expert_id: str) -> dict[str, object]:
    """返回一个可启动专家的完整版本定义。"""
    try:
        expert = await asyncio.to_thread(_expert_registry.get, expert_id)
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not expert.enabled:
        raise HTTPException(status_code=404, detail=f"专家已禁用: {expert_id}")
    return expert.model_dump(mode="json")


@app.delete("/api/experts/{expert_id}")
async def delete_expert_endpoint(expert_id: str) -> dict[str, str]:
    """删除一个已发布专家，连同其草稿与版本归档。"""
    try:
        await asyncio.to_thread(_expert_registry.delete_expert, expert_id)
    except ExpertRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": expert_id}


@app.post("/api/skills/import")
async def import_skill_endpoint(file: UploadFile = File(...)) -> dict[str, object]:
    """从用户选择的 ZIP 包安装或更新一个 Skill。"""
    if not file.filename or not file.filename.casefold().endswith(".zip"):
        raise HTTPException(status_code=400, detail="仅支持上传 .zip Skill 包")
    payload = await file.read()
    try:
        metadata = await asyncio.to_thread(_skill_registry.install_archive, payload)
    except SkillRegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return metadata.model_dump(mode="json")


@app.get("/api/skills/{skill_id}/export")
async def export_skill_endpoint(skill_id: str) -> Response:
    """下载一个已安装 Skill 的 ZIP 包。"""
    try:
        payload = await asyncio.to_thread(_skill_registry.export_archive, skill_id)
    except SkillRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{skill_id}.zip"'},
    )


@app.delete("/api/skills/{skill_id}")
async def delete_skill_endpoint(skill_id: str) -> dict[str, str]:
    """卸载一个已安装 Skill，删除其在 data_analysis/skills 下的目录。"""
    try:
        await asyncio.to_thread(_skill_registry.uninstall, skill_id)
    except SkillRegistryError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"removed": skill_id}


@app.post("/api/agent/chat/stream")
@app.post("/api/agent/profile/stream", include_in_schema=False)
async def agent_profile_stream_endpoint(
    message: str = Form(""),
    discussion_id: str | None = Form(None),
    title: str | None = Form(None),
    files: list[UploadFile] | None = File(None),
    config_id: str | None = Form(None),
    reasoning_effort: str | None = Form(None),
    knowledge_base_id: str | None = Form(None),
    knowledge_base_ids: list[str] | None = Form(None),
    skill_ids: list[str] | None = Form(None),
    expert_id: str | None = Form(None),
    project_id: str | None = Form(None),
) -> StreamingResponse:
    """启动自主决策 Agent，并流式返回回答及工具运行轨迹。"""
    message = message.strip() or "请对上传的数据生成基础画像"
    active_discussion_id = discussion_id or uuid.uuid4().hex
    stored_session = (
        await asyncio.to_thread(store.get_session, active_discussion_id)
        if discussion_id
        else None
    )
    effective_config_id = config_id or (
        stored_session.get("model_config_id") if stored_session else None
    )
    valid_reasoning_efforts = {"close", "low", "medium", "high", "xhigh", "max"}
    if reasoning_effort is not None and reasoning_effort not in valid_reasoning_efforts:
        raise HTTPException(status_code=422, detail="不支持的思考程度")
    effective_reasoning_effort = (
        reasoning_effort
        if reasoning_effort is not None
        else stored_session.get("reasoning_effort") if stored_session else None
    )
    llm = _discussion_llm_by_id(effective_config_id, effective_reasoning_effort)
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
    try:
        expert_context = await asyncio.to_thread(
            _expert_registry.load_selected, expert_id
        )
        requested_skill_ids = list(skill_ids or [])
        if expert_context.expert is not None:
            requested_skill_ids.extend(expert_context.expert.runtime_skill_ids)
        skill_context = await asyncio.to_thread(
            _skill_registry.load_selected, requested_skill_ids
        )
    except (ExpertRegistryError, SkillRegistryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run_id = uuid.uuid4().hex

    if any(
        run.discussion_id == active_discussion_id and not run.done
        for run in _agent_chat_runs.values()
    ):
        raise HTTPException(status_code=409, detail="当前对话已有运行中的 Agent 任务")

    try:
        project = await asyncio.to_thread(
            _resolve_agent_project,
            stored_session,
            project_id,
        )
    except ProjectWorkspaceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    workspace_root = Path(str(project["root_path"])) if project else None
    if stored_session is None:
        await asyncio.to_thread(
            store.create_session,
            (title or message or "新讨论")[:80],
            active_discussion_id,
            project_id=str(project["id"]) if project else None,
            cwd=str(workspace_root) if workspace_root else None,
            model_config_id=effective_config_id,
            reasoning_effort=effective_reasoning_effort,
        )
    elif config_id:
        await asyncio.to_thread(
            store.set_session_model_config,
            active_discussion_id,
            config_id,
            effective_reasoning_effort,
        )
    session_entries = stored_session.get("messages", []) if stored_session else []
    context_window_tokens = _agent_context_window_tokens(effective_config_id)
    rolling_context = await asyncio.to_thread(
        compile_rolling_context,
        session_entries,
        llm,
        context_window_tokens=context_window_tokens,
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
    history = [
        AgentMessage(role=str(item["role"]), content=str(item.get("content") or ""))
        for item in rolling_context.recent_messages
    ]
    prior_trajectory = collect_work_trajectory_from_session(session_entries)
    output_dir = _agent_output_root(active_discussion_id, workspace_root)
    artifacts_before_run = _snapshot_agent_artifacts(
        active_discussion_id,
        workspace_root,
    )
    try:
        python_environment = (
            resolve_project_python(project)
            if project is not None
            else resolve_python_environment(None)
        )
    except (ProjectWorkspaceError, PythonEnvironmentError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    uploaded_paths = await _prepare_agent_data_files(files, active_discussion_id, run_id)
    runtime_dir = uploaded_paths[0].parents[1] if uploaded_paths else None
    attachment_context = ""
    data_paths = uploaded_paths
    api_settings = _read_api_settings() or {}
    terminal_shell = _configured_terminal_shell(api_settings)
    tavily_settings = api_settings.get("external_services", {}).get("tavily", {})
    tavily_api_key = (
        tavily_settings.get("apiKey")
        if isinstance(tavily_settings, dict)
        else None
    )
    try:
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
            terminal_shell=terminal_shell,
        )
        state = AgentState(
            # 附件上下文在流已启动后编译，避免前置模型调用阻塞 run_id 下发。
            user_query=message,
            memory_summary=rolling_context.memory_summary,
            data_paths=[str(path.resolve(strict=True)) for path in data_paths],
            output_dir=str(output_dir),
            workspace_root=str(workspace_root) if workspace_root else None,
            available_artifacts=prior_trajectory.enrich_artifacts(
                discover_artifacts(output_dir)
            ),
            project_id=str(project["id"]) if project else None,
            messages=history,
            work_trajectory=prior_trajectory,
            expert_context=expert_context,
            skill_context=skill_context,
        )
        system_prompt = _load_agent_system_prompt(workspace_root)
    except Exception:
        if runtime_dir is not None:
            await asyncio.to_thread(_cleanup_agent_runtime_dir, runtime_dir)
        raise
    chat_run = AgentChatRun(
        active_discussion_id,
        message,
        runtime_dir=runtime_dir,
    )

    def _agent_cancel_check() -> bool:
        return chat_run.cancel_requested

    agent_event_loop = asyncio.get_running_loop()
    progress_events: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def publish_agent_progress(stream_id: str, content: str, done: bool) -> None:
        """从模型工作线程向 HTTP 事件循环传递公开思考摘要。"""
        agent_event_loop.call_soon_threadsafe(
            progress_events.put_nowait,
            {
                "type": "agent_thinking",
                "stream_id": stream_id,
                "content": content,
                "done": done,
            },
        )

    loop = AgentLoop(
        llm,
        registry.specs(),
        system_prompt,
        max_iterations=AGENT_MAX_DECISIONS,
        decision_retries=8,
        cancel_check=_agent_cancel_check,
        progress_callback=publish_agent_progress,
    )
    planner = Planner(
        llm,
        registry.specs(),
        cancel_check=_agent_cancel_check,
        progress_callback=publish_agent_progress,
    )
    approved_terminal_actions: set[int] = set()
    approved_skill_actions: set[int] = set()

    def consume_terminal_approval(action: Action) -> bool:
        """只允许已批准的同一次终端调用执行一次。"""
        key = id(action)
        if key not in approved_terminal_actions:
            return False
        approved_terminal_actions.remove(key)
        return True

    def consume_skill_approval(action: SkillAction) -> bool:
        """只允许已批准的同一次 Skill 脚本调用执行一次。"""
        key = id(action)
        if key not in approved_skill_actions:
            return False
        approved_skill_actions.remove(key)
        return True

    execution_context = ExecutionContext(
        output_dir=output_dir,
        workspace_root=workspace_root,
        python_environment=python_environment,
        authorized_paths=[Path(path) for path in state.data_paths],
    )
    harness = Harness(
        registry,
        sandbox=SubprocessSandbox(
            timeout_seconds=AGENT_TOOL_TIMEOUT_SECONDS,
            working_directory=workspace_root or PROJECT_ROOT,
        ),
        terminal_approval=consume_terminal_approval,
        execution_context=execution_context,
        cancel_check=_agent_cancel_check,
    )
    skill_runtime = SkillRuntime(
        _skill_registry,
        skill_context,
        output_dir=output_dir,
        data_paths=state.data_paths,
        python_executable=python_environment.executable,
        approval=consume_skill_approval,
        on_asset_materialized=execution_context.register_skill_asset,
        cancel_check=_agent_cancel_check,
    )
    _agent_chat_runs[run_id] = chat_run

    async def event_stream():
        nonlocal attachment_context
        started_event: dict[str, object] = {
            "type": "agent_started",
            "run_id": run_id,
            "discussion_id": active_discussion_id,
            "message": message,
            "files": [path.name for path in data_paths],
            "knowledge_base_ids": selected_base_ids,
            "expert_id": expert_context.expert_id,
            "expert_version": (
                expert_context.expert.version if expert_context.expert else None
            ),
            "expert_bundle_hash": (
                expert_context.expert.bundle_hash if expert_context.expert else None
            ),
            "skill_ids": list(skill_context.active_ids),
        }
        trace: list[dict[str, object]] = [started_event]
        yield json.dumps(started_event, ensure_ascii=False) + "\n"
        answer = ""
        knowledge_sources: list[dict[str, str]] = []

        async def save_stopped_run(content: str) -> str:
            """保存未完成结果，使任务切换后仍能在历史对话中看到。"""
            attachments = _attachment_metadata(uploaded_paths)
            state.work_trajectory.absorb_completed_todos(state.analysis_plan)
            return await asyncio.to_thread(
                save_discussion_message,
                active_discussion_id,
                message,
                content,
                knowledge_sources,
                title,
                attachments,
                attachment_context=attachment_context,
                knowledge_base_ids=selected_base_ids,
                mode="agent",
                assistant_metadata={
                    "agent_run_id": run_id,
                    "agent_trace": trace,
                    "work_trajectory": state.work_trajectory.model_dump(),
                },
                project_id=str(project["id"]) if project else None,
                cwd=str(workspace_root) if workspace_root else None,
            )

        try:
            if uploaded_paths:
                attachment_progress = {
                    "type": "agent_thinking",
                    "stream_id": "attachment-preparation",
                    "content": "正在读取并整理上传附件…",
                    "done": False,
                }
                yield json.dumps(attachment_progress, ensure_ascii=False) + "\n"
                try:
                    attachment_context = await asyncio.to_thread(
                        _compile_uploaded_attachment_context,
                        uploaded_paths,
                        llm,
                    )
                    state.user_query = compose_user_message(
                        message,
                        attachment_context,
                    )
                finally:
                    yield json.dumps(
                        {
                            "type": "agent_thinking",
                            "stream_id": "attachment-preparation",
                            "content": "",
                            "done": True,
                        },
                        ensure_ascii=False,
                    ) + "\n"

            iterator = iter_plan_graph(
                state,
                planner,
                loop,
                harness,
                skill_runtime=skill_runtime,
            )
            async with asyncio.timeout(AGENT_RUN_TIMEOUT_SECONDS):
                while True:
                    next_event_task = asyncio.create_task(
                        asyncio.to_thread(_next_agent_event, iterator)
                    )
                    try:
                        while not next_event_task.done():
                            progress_task = asyncio.create_task(progress_events.get())
                            completed, _ = await asyncio.wait(
                                {next_event_task, progress_task},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if progress_task in completed:
                                progress_payload = progress_task.result()
                                yield json.dumps(
                                    progress_payload,
                                    ensure_ascii=False,
                                ) + "\n"
                            else:
                                progress_task.cancel()
                                await asyncio.gather(
                                    progress_task,
                                    return_exceptions=True,
                                )
                        event = await next_event_task
                        while True:
                            try:
                                progress_payload = progress_events.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            yield json.dumps(
                                progress_payload,
                                ensure_ascii=False,
                            ) + "\n"
                    except TaskCancelledError:
                        await save_stopped_run("任务已终止。")
                        yield json.dumps({"type": "cancelled", "run_id": run_id}, ensure_ascii=False) + "\n"
                        return
                    if event is None:
                        break
                    if chat_run.cancel_requested:
                        await save_stopped_run("任务已终止。")
                        yield json.dumps({"type": "cancelled", "run_id": run_id}, ensure_ascii=False) + "\n"
                        return
                    if isinstance(event, Finish):
                        # 最终回答前只推送模型明确生成的公开摘要。
                        if event.thought.strip():
                            payload = _agent_event_payload(event)
                            trace.append(payload)
                            yield json.dumps(payload, ensure_ascii=False) + "\n"
                        answer = event.answer
                        for chunk in _final_answer_chunks(answer):
                            if chat_run.cancel_requested:
                                await save_stopped_run("任务已终止。")
                                yield json.dumps(
                                    {"type": "cancelled", "run_id": run_id},
                                    ensure_ascii=False,
                                ) + "\n"
                                return
                            yield json.dumps(
                                {"type": "token", "content": chunk},
                                ensure_ascii=False,
                            ) + "\n"
                            await asyncio.sleep(FINAL_ANSWER_CHUNK_DELAY_SECONDS)
                        break
                    payload = _agent_event_payload(event)
                    trace.append(payload)
                    if (
                        isinstance(event, Action)
                        and event.tool_name in TERMINAL_TOOL_NAMES
                    ):
                        approval_id = uuid.uuid4().hex
                        decision = asyncio.get_running_loop().create_future()
                        _pending_agent_commands[approval_id] = decision
                        chat_run.approval_id = approval_id
                        request_payload = {
                            "type": "agent_command_approval_required",
                            "approval_id": approval_id,
                            "command": str(event.arguments.get("command", "")),
                            "shell": terminal_shell,
                            "timeout_seconds": event.arguments.get("timeout_seconds"),
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
                            chat_run.approval_id = None
                        if chat_run.cancel_requested:
                            await save_stopped_run("任务已终止。")
                            yield json.dumps({"type": "cancelled", "run_id": run_id}, ensure_ascii=False) + "\n"
                            return
                        if approved:
                            approved_terminal_actions.add(id(event))
                        decision_payload = {
                            "type": "agent_command_approval_decision",
                            "approval_id": approval_id,
                            "approved": approved,
                        }
                        trace.append(decision_payload)
                        yield json.dumps(decision_payload, ensure_ascii=False) + "\n"
                        continue
                    if isinstance(event, SkillAction) and skill_runtime.requires_approval(event):
                        approval_id = uuid.uuid4().hex
                        decision = asyncio.get_running_loop().create_future()
                        _pending_agent_commands[approval_id] = decision
                        chat_run.approval_id = approval_id
                        request_payload = {
                            "type": "agent_command_approval_required",
                            "approval_id": approval_id,
                            "command": (
                                f"运行 Skill 脚本：{event.skill_id}/"
                                f"{event.entrypoint_id or ''}"
                            ),
                            "shell": terminal_shell,
                            "timeout_seconds": None,
                            "workspace_root": str(workspace_root or PROJECT_ROOT),
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
                            chat_run.approval_id = None
                        if approved:
                            approved_skill_actions.add(id(event))
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
                        failure_payload = {
                            "type": "agent_failed",
                            "error": event.error,
                        }
                        trace.append(failure_payload)
                        await save_stopped_run(event.error)
                        yield json.dumps(failure_payload, ensure_ascii=False) + "\n"
                        yield json.dumps(
                            {"type": "error", "detail": event.error},
                            ensure_ascii=False,
                        ) + "\n"
                        return
                    yield json.dumps(payload, ensure_ascii=False) + "\n"
        except TimeoutError:
            detail = "Agent 运行超时，请稍后重试。"
            failure_payload = {"type": "agent_failed", "error": detail}
            trace.append(failure_payload)
            await save_stopped_run(detail)
            yield json.dumps(failure_payload, ensure_ascii=False) + "\n"
            yield json.dumps({"type": "error", "detail": detail}, ensure_ascii=False) + "\n"
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Data agent run failed")
            detail = f"Agent 运行失败：{exc}"
            failure_payload = {"type": "agent_failed", "error": detail}
            trace.append(failure_payload)
            await save_stopped_run(detail)
            yield json.dumps(failure_payload, ensure_ascii=False) + "\n"
            yield json.dumps({"type": "error", "detail": detail}, ensure_ascii=False) + "\n"
            return

        if chat_run.cancel_requested:
            await save_stopped_run("任务已终止。")
            yield json.dumps({"type": "cancelled", "run_id": run_id}, ensure_ascii=False) + "\n"
            return

        try:
            artifacts = _list_agent_artifacts(
                active_discussion_id,
                run_id,
                workspace_root,
                artifacts_before_run,
            )
        except OSError:
            logger.exception("Failed to list Agent artifacts")
            artifacts = []
        attachments = _attachment_metadata(uploaded_paths)
        context_usage = estimate_context_usage_after_turn(
            rolling_context.memory_summary,
            rolling_context.recent_messages,
            state.user_query,
            answer,
            context_window_tokens=context_window_tokens,
        )
        state.work_trajectory.absorb_completed_todos(state.analysis_plan)
        saved_discussion_id = await asyncio.to_thread(
            save_discussion_message,
            active_discussion_id,
            message,
            answer,
            knowledge_sources,
            title,
            attachments,
            attachment_context=attachment_context,
            knowledge_base_ids=selected_base_ids,
            mode="agent",
            assistant_metadata={
                "agent_run_id": run_id,
                "agent_trace": trace,
                "agent_artifacts": artifacts,
                "context_usage": context_usage,
                "work_trajectory": state.work_trajectory.model_dump(),
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
                "context_usage": context_usage,
            },
            ensure_ascii=False,
        ) + "\n"

    task = asyncio.create_task(_publish_agent_chat_run(chat_run, event_stream()))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return StreamingResponse(
        _replay_agent_chat_run(chat_run),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/agent/discussions/{discussion_id}/active-run")
async def get_active_agent_chat_run(discussion_id: str) -> dict[str, str] | None:
    """查询某个对话当前仍在运行的 Agent 任务。"""
    for run_id, run in reversed(list(_agent_chat_runs.items())):
        if run.discussion_id == discussion_id and not run.done:
            return {"run_id": run_id, "message": run.message}
    return None


@app.get("/api/agent/chat/runs/{run_id}/stream")
async def reconnect_agent_chat_run(run_id: str) -> StreamingResponse:
    """重连 Agent 运行并回放已产生的事件。"""
    run = _agent_chat_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent 任务不存在或服务已重启")
    return StreamingResponse(
        _replay_agent_chat_run(run),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent/chat/runs/{run_id}/cancel")
async def cancel_agent_chat_run(run_id: str) -> dict[str, bool]:
    """请求在当前工具返回后终止 Agent，不再执行下一步。"""
    run = _agent_chat_runs.get(run_id)
    if run is None or run.done:
        raise HTTPException(status_code=404, detail="Agent 任务不在运行中")
    run.cancel_requested = True
    if run.approval_id:
        decision = _pending_agent_commands.get(run.approval_id)
        if decision is not None and not decision.done():
            decision.set_result(False)
    return {"accepted": True}


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
        model_config_id=discussion.get("model_config_id"),
        reasoning_effort=discussion.get("reasoning_effort"),
        updated_at=discussion["updated_at"],
        messages=messages,
    )


@app.delete("/api/knowledge/discussions/{discussion_id}")
async def delete_brainstorm_discussion_endpoint(discussion_id: str) -> dict[str, bool]:
    """Permanently delete a saved inspiration discussion."""
    if any(
        run.discussion_id == discussion_id and not run.done
        for run in _agent_chat_runs.values()
    ):
        raise HTTPException(status_code=409, detail="该会话仍有运行中的任务，请先终止任务")
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


@app.patch("/api/knowledge/discussions/{discussion_id}/model")
async def update_brainstorm_discussion_model_endpoint(
    discussion_id: str,
    body: dict,
) -> dict[str, bool]:
    """保存当前会话默认使用的 API 模型配置和思考程度。"""
    config_id = body.get("config_id")
    if not isinstance(config_id, str) or not config_id.strip():
        raise HTTPException(status_code=400, detail="模型配置标识不能为空")
    if len(config_id.strip()) > 200:
        raise HTTPException(status_code=400, detail="模型配置标识过长")
    reasoning_effort = body.get("reasoning_effort")
    if reasoning_effort is not None and reasoning_effort not in {
        "close", "low", "medium", "high", "xhigh", "max",
    }:
        raise HTTPException(status_code=400, detail="不支持的思考程度")
    updated = await asyncio.to_thread(
        store.set_session_model_config,
        discussion_id,
        config_id,
        reasoning_effort,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="未找到该讨论记录")
    return {"updated": True}


@app.api_route("/api/runs", methods=["GET", "POST", "DELETE", "PATCH"])
@app.api_route("/api/runs/{legacy_path:path}", methods=["GET", "POST", "DELETE", "PATCH"])
async def removed_legacy_runs_endpoint(legacy_path: str = "") -> None:
    """旧建模运行器已随 scr 删除；统一 Agent 使用 /api/agent/chat/stream。"""
    raise HTTPException(status_code=410, detail="旧建模接口已移除，请使用统一 Agent 对话。")


# ---------------------------------------------------------------------------
# 文档转换（MarkItDown）
# ---------------------------------------------------------------------------
# 单次请求最多转换的文件数，避免一次上传过多文件长时间占用工作线程
MAX_CONVERT_FILES_PER_REQUEST = 20
# 单次另存 Markdown 的字符上限，防止异常大的请求体
MAX_SAVE_MARKDOWN_CHARS = 50_000_000


def _safe_markdown_filename(filename: str) -> str:
    """把用户输入的保存文件名规范化。

    只保留文件名本身（去掉任何路径成分），替换 Windows 非法字符，并强制 .md 后缀，
    避免通过文件名做目录穿越。

    Args:
        filename: 用户输入的文件名，可含或不含 .md 后缀。

    Returns:
        形如 ``报告.md`` 的安全文件名。

    Raises:
        HTTPException: 文件名为空或清洗后为空。
    """
    stem = Path(filename).name.strip()
    stem = re.sub(r'[<>:"|?*\x00-\x1f]', "_", stem)
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    stem = stem.strip(" .")
    if not stem:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    return f"{stem}.md"


def _default_save_directory() -> str:
    """建议的默认保存目录：优先用户「下载」目录，不存在时回退用户主目录。"""
    downloads = Path.home() / "Downloads"
    return str(downloads if downloads.is_dir() else Path.home())


@app.get("/api/documents/formats")
async def list_convertible_formats_endpoint() -> dict[str, object]:
    """返回文档转换支持的格式、体积上限与默认保存目录，供界面提示与校验。"""
    return {
        "extensions": list(supported_extensions()),
        "max_bytes": MAX_DOCUMENT_BYTES,
        "default_save_dir": _default_save_directory(),
    }


@app.post("/api/documents/save")
async def save_converted_markdown_endpoint(body: DocumentSaveBody) -> dict[str, object]:
    """把某条转换记录另存到用户指定的本机目录。

    浏览器无法指定下载路径，因此由本地后端完成落盘：目录既可手输，
    也可先调 /api/system/select-folder 用系统原生文件夹选择框取得。
    """
    markdown = await asyncio.to_thread(converter_store.read_markdown, body.record_id)
    if markdown is None:
        raise HTTPException(status_code=404, detail="转换记录不存在，无法保存")
    if not markdown.strip():
        raise HTTPException(status_code=400, detail="该记录没有可保存的内容")

    filename = _safe_markdown_filename(body.filename)
    target_dir = Path(body.directory).expanduser()

    if not target_dir.is_absolute():
        raise HTTPException(status_code=400, detail="保存目录必须是绝对路径")
    if not target_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"保存目录不存在：{target_dir}")

    if len(markdown) > MAX_SAVE_MARKDOWN_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"内容过大（{len(markdown)} 字符），超过 {MAX_SAVE_MARKDOWN_CHARS} 字符上限",
        )

    target = target_dir / filename
    try:
        await asyncio.to_thread(target.write_text, markdown, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"写入失败：{exc}") from exc

    return {
        "saved_path": str(target),
        "filename": filename,
        "bytes": target.stat().st_size,
    }


# ---------------------------------------------------------------------------
# 转换历史：未删除的记录会一直保留，每次打开「文档转换」页都能看到
# ---------------------------------------------------------------------------


@app.get("/api/documents/history")
async def list_conversion_history_endpoint() -> dict[str, object]:
    """按转换先后顺序返回全部转换记录（不含正文）。"""
    records = await asyncio.to_thread(converter_store.list_records)
    return {"records": records}


@app.get("/api/documents/history/{record_id}/content")
async def read_conversion_content_endpoint(record_id: int) -> PlainTextResponse:
    """返回某条转换记录的 Markdown 正文（预览、复制时按需读取）。"""
    markdown = await asyncio.to_thread(converter_store.read_markdown, record_id)
    if markdown is None:
        raise HTTPException(status_code=404, detail="转换记录不存在")
    return PlainTextResponse(markdown)


@app.delete("/api/documents/history/{record_id}")
async def delete_conversion_record_endpoint(record_id: int) -> dict[str, object]:
    """删除一条转换记录（删除后不再出现在列表里）。"""
    deleted = await asyncio.to_thread(converter_store.delete_record, record_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="转换记录不存在")
    return {"deleted_id": record_id}


@app.post("/api/documents/convert")
async def convert_documents_to_markdown_endpoint(
    files: list[UploadFile] = File(..., description="待转换文档（可多个）"),
) -> dict[str, object]:
    """把上传的文档转换为 Markdown 文本，并登记到转换历史。

    独立功能，与建模流程无关：逐份转换、逐份登记记录，
    单个文件失败不影响同一批次中的其余文件，失败原因同样入历史。

    返回每条记录的元数据（不含正文）；正文按需通过
    ``GET /api/documents/history/{record_id}/content`` 读取。
    """
    if not files:
        raise HTTPException(status_code=400, detail="请至少选择一个文档")
    if len(files) > MAX_CONVERT_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多转换 {MAX_CONVERT_FILES_PER_REQUEST} 个文档",
        )

    results: list[dict[str, object]] = []
    for upload in files:
        filename = Path(upload.filename or "document").name
        started = time.monotonic()
        content = await upload.read()
        try:
            # 转换是 CPU 密集的同步调用，放到线程避免阻塞事件循环
            markdown = await asyncio.to_thread(
                to_markdown_from_bytes, content, filename
            )
        except (ValueError, DocumentConversionError) as exc:
            results.append(
                await asyncio.to_thread(
                    converter_store.add_record,
                    filename=filename,
                    ok=False,
                    size_bytes=len(content),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=str(exc),
                )
            )
            continue
        results.append(
            await asyncio.to_thread(
                converter_store.add_record,
                filename=filename,
                ok=True,
                char_count=len(markdown),
                size_bytes=len(content),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                markdown=markdown,
            )
        )

    return {"results": results}


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
