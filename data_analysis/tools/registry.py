"""定义工具规格、可执行工具和注册表。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

from pydantic import JsonValue

from ..python_environment import (
    PythonEnvironmentError,
    resolve_python_environment,
)
from .artifact_tools import MAX_ARTIFACT_CONTENT_BYTES, write_artifact
from .attachment_tools import (
    TEXT_ATTACHMENT_SUFFIXES,
    edit_attachment_file,
    read_attachment_file,
)
from .markitdown import (
    CONVERTIBLE_DOCUMENT_SUFFIXES,
    DEFAULT_PAGE_LINES,
    MAX_PAGE_LINES,
    convert_attachment_to_markdown,
)
from .code_tools import execute_python_code
from .project_edit_tools import (
    MAX_FILE_EDITS,
    run_create_workspace_file,
    run_edit_workspace_file,
    run_project_command_tool,
)
from .data_tools import (
    MAX_INSPECT_DATA_ROWS,
    inspect_data_rows,
    profile_data,
    supports_data_profile,
)
from .errors import DuplicateToolError, ToolArgumentsError, ToolNotFoundError
from .modeling_tool import (
    MAX_RELATIONSHIP_LAWS,
    explore_math_models,
    supports_modeling_explore,
)
from .knowledge_tools import MAX_KNOWLEDGE_QUERY_CHARS, search_knowledge_base
from .web_search import MAX_WEB_SEARCH_QUERY_CHARS, search_web
from .workspace_tools import (
    list_workspace_directory,
    list_workspace_files,
    read_workspace_file,
    search_workspace_text,
)

__all__ = [
    "DuplicateToolError",
    "RegisteredTool",
    "ToolArgumentsError",
    "ToolHandler",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolSpec",
    "create_agent_tool_registry",
    "create_data_profile_registry",
]

_MAX_CODE_CHARS = 50_000

ToolHandler: TypeAlias = Callable[
    [Mapping[str, JsonValue], Mapping[str, JsonValue]],
    JsonValue,
]


@dataclass(frozen=True)
class ToolSpec:
    """暴露给 LLM 的工具元数据，不包含执行函数。"""

    name: str
    description: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """规范化文本并冻结参数模式的顶层映射。"""
        normalized_name = self.name.strip()
        normalized_description = self.description.strip()
        if not normalized_name:
            raise ValueError("工具名称不能为空")
        if not normalized_description:
            raise ValueError("工具描述不能为空")
        parameters = dict(self.parameters)
        try:
            json.dumps(parameters, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("工具参数模式必须是合法 JSON") from exc
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "description", normalized_description)
        object.__setattr__(self, "parameters", MappingProxyType(parameters))


@dataclass(frozen=True)
class RegisteredTool:
    """注册表内部保存的工具规格与执行函数。"""

    spec: ToolSpec
    handler: ToolHandler
    context: Mapping[str, Any] = field(default_factory=dict)
    run_in_process: bool = False

    def __post_init__(self) -> None:
        """拒绝不可调用的执行函数。"""
        if not callable(self.handler):
            raise TypeError("工具处理器必须可调用")
        context = dict(self.context)
        if not self.run_in_process:
            try:
                json.dumps(context, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("工具运行上下文必须是合法 JSON") from exc
        object.__setattr__(self, "context", MappingProxyType(context))

    @property
    def entrypoint(self) -> str:
        """返回供子进程导入的处理器入口。"""
        module = getattr(self.handler, "__module__", "")
        name = getattr(self.handler, "__qualname__", "")
        if not module or not name.isidentifier():
            raise ValueError(f"工具处理器不是可导入的顶层函数: {self.spec.name}")
        return f"{module}:{name}"

    def invoke(self, arguments: Mapping[str, JsonValue]) -> JsonValue:
        """在当前进程调用处理器，主要用于单元测试。"""
        return self.handler(arguments, self.context)


class ToolRegistry:
    """维护工具名称到可执行工具的有序映射。"""

    def __init__(self, tools: Sequence[RegisteredTool] = ()) -> None:
        """初始化工具注册表。

        Args:
            tools: 可选的初始工具，按给定顺序注册。

        Raises:
            DuplicateToolError: 初始工具存在重名。
        """
        self._tools: dict[str, RegisteredTool] = {}
        for tool in tools:
            self.register(tool.spec, tool.handler, context=tool.context)

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        context: Mapping[str, JsonValue] | None = None,
    ) -> RegisteredTool:
        """注册一个工具。

        Args:
            spec: 暴露给 LLM 的工具规格。
            handler: 由 Harness 调用的执行函数。
            context: 仅供处理器使用且不会暴露给 LLM 的只读上下文。

        Returns:
            已注册的不可变工具对象。

        Raises:
            DuplicateToolError: 已存在同名工具。
            TypeError: 处理器不可调用。
        """
        if spec.name in self._tools:
            raise DuplicateToolError(f"工具已注册: {spec.name}")
        tool = RegisteredTool(spec=spec, handler=handler, context=context or {})
        self._tools[spec.name] = tool
        return tool

    def register_in_process(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> RegisteredTool:
        """注册必须在主进程执行的工具，例如复用当前 LLM 的视觉调用。"""
        if spec.name in self._tools:
            raise DuplicateToolError(f"工具已注册: {spec.name}")
        tool = RegisteredTool(
            spec=spec,
            handler=handler,
            context=context or {},
            run_in_process=True,
        )
        self._tools[spec.name] = tool
        return tool

    def resolve(self, name: str) -> RegisteredTool:
        """按名称获取可执行工具。

        Args:
            name: LLM 选择的工具名称。

        Returns:
            对应的已注册工具。

        Raises:
            ToolNotFoundError: 工具名称未注册。
        """
        normalized_name = name.strip()
        try:
            return self._tools[normalized_name]
        except KeyError as exc:
            raise ToolNotFoundError(f"工具未注册: {normalized_name or '<空>'}") from exc

    def specs(self) -> tuple[ToolSpec, ...]:
        """按注册顺序返回仅供决策层使用的工具规格。"""
        return tuple(tool.spec for tool in self._tools.values())

    def __contains__(self, name: object) -> bool:
        """判断名称是否已注册。"""
        return isinstance(name, str) and name.strip() in self._tools

    def __len__(self) -> int:
        """返回已注册工具数量。"""
        return len(self._tools)


def create_data_profile_registry(
    authorized_paths: Sequence[str | Path],
) -> ToolRegistry:
    """创建绑定本次授权文件的数据分析工具注册表。

    Args:
        authorized_paths: 当前 Agent 状态允许读取的数据文件路径。

    Returns:
        包含数据画像与真实行查看能力的工具注册表。

    Raises:
        ValueError: 未提供任何授权数据文件。
    """
    bound_paths = tuple(authorized_paths)
    if not bound_paths:
        raise ValueError("创建数据画像工具注册表前必须提供授权文件")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="profile_data",
            description=(
                "对本次运行已授权的全部数据文件生成增强画像，包括表规模、字段类型、"
                "缺失与重复、数值和分类分布、离群点、候选键、时间与空间字段、"
                "单位线索及高相关列对。文件已由运行时绑定，可直接调用，"
                "无需查询文件列表或传入路径。"
            ),
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        _run_profile_data,
        context={"authorized_paths": [str(path) for path in bound_paths]},
    )
    registry.register(
        ToolSpec(
            name="inspect_data_rows",
            description=(
                "按用户指定范围查看一张表的真实数据记录，用于核实字段取值、"
                "定位异常样本或基于具体行形成洞察。start_row 从 1 开始且不含表头，"
                "row_count 是返回行数。存在多个文件时传 file_name，Excel 包含多个"
                "工作表时再传 sheet_name；不要虚构文件名或工作表名。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "row_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_INSPECT_DATA_ROWS,
                        "description": "本次查看的真实数据行数",
                    },
                    "start_row": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "起始数据行号，从 1 开始且不包含表头",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "存在多个授权文件时要查看的文件名",
                    },
                    "sheet_name": {
                        "type": "string",
                        "description": "Excel 存在多个工作表时要查看的工作表名",
                    },
                },
                "required": ["row_count"],
                "additionalProperties": False,
            },
        ),
        _run_inspect_data_rows,
        context={"authorized_paths": [str(path) for path in bound_paths]},
    )
    return registry


def create_agent_tool_registry(
    authorized_paths: Sequence[str | Path],
    output_dir: str | Path,
    *,
    vision_llm: Any | None = None,
    knowledge_base_ids: Sequence[str] = (),
    web_search_api_key: str | None = None,
    workspace_root: str | Path | None = None,
    python_executable: str | Path | None = None,
    python_environment_source: str = "host",
    python_environment_name: str = "MMAgent",
) -> ToolRegistry:
    """创建同时支持确定性工具和动态 Python 的 Agent 注册表。

    Args:
        authorized_paths: 当前运行允许读取的数据文件路径，可以为空。
        output_dir: 当前会话允许生成产物的目录。
        vision_llm: 当前会话模型；提供时为图片附件注册识图工具。
        knowledge_base_ids: 用户为当前会话预选的知识库 ID。
        web_search_api_key: 服务端保存的 Tavily API 密钥；不会暴露给模型。
        workspace_root: 当前项目的文件根目录；为空时不注册项目工具。
        python_executable: 运行 Agent 分析代码的已验证 Python 解释器。
        python_environment_source: Python 环境来源。
        python_environment_name: Python 环境显示名称。

    Returns:
        根据附件情况注册数据工具，并始终注册 Python 代码执行能力。
    """
    bound_paths = tuple(authorized_paths)
    profile_paths = tuple(path for path in bound_paths if supports_data_profile(path))
    registry = (
        create_data_profile_registry(profile_paths)
        if profile_paths
        else ToolRegistry()
    )
    modeling_paths = tuple(
        path for path in bound_paths if supports_modeling_explore(path)
    )
    if modeling_paths:
        registry.register(
            ToolSpec(
                name="explore_math_models",
                description=(
                    "对已授权数据做数学建模探索：发现变量间函数规律"
                    "（线性/多项式/对数/指数/幂律拟合），可选目标列时拟合多元"
                    "线性模型并给出特征重要性。返回公式、系数、R²、结构结论与"
                    "建模约束。用于日常「数据间有什么规律、可用什么数学模型」"
                    "探索；不要用它写论文或跑完整竞赛建模流程。多文件时传"
                    " file_name，Excel 多表时传 sheet_name；已知因变量时传"
                    " target_column。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "focus": {
                            "type": "string",
                            "description": "探索重点或业务问题，原样回显以便串联上下文",
                        },
                        "target_column": {
                            "type": "string",
                            "description": "可选的目标/因变量列名；提供后额外拟合多元模型",
                        },
                        "file_name": {
                            "type": "string",
                            "description": "存在多个授权数据文件时指定文件名",
                        },
                        "sheet_name": {
                            "type": "string",
                            "description": "Excel 存在多个工作表时指定表名",
                        },
                        "max_relationships": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_RELATIONSHIP_LAWS,
                            "default": 8,
                            "description": "返回的两两函数规律条数上限",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            _run_explore_math_models,
            context={"authorized_paths": [str(path) for path in modeling_paths]},
        )
    text_attachment_paths = tuple(
        path for path in bound_paths
        if Path(path).suffix.lower() in TEXT_ATTACHMENT_SUFFIXES
    )
    if text_attachment_paths:
        attachment_context = {
            "authorized_paths": [str(path) for path in text_attachment_paths],
            "output_dir": str(output_dir),
        }
        registry.register(
            ToolSpec(
                name="read_attachment_file",
                description=(
                    "按 available_data_files 中的 file_path 读取用户上传的文本或代码附件，"
                    "包括 .py；不执行脚本。上传附件不属于项目目录，不要用"
                    " read_workspace_file 读取它。大文件使用 offset 和 limit 分段读取。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "minLength": 1},
                        "offset": {"type": "integer", "minimum": 1, "default": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 300},
                    },
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            ),
            read_attachment_file,
            context=attachment_context,
        )
        registry.register(
            ToolSpec(
                name="edit_attachment_file",
                description=(
                    "按 Pi 风格 edits 精确修改用户上传的 UTF-8 文本附件，并只以原文件名"
                    "输出一个更新副本。每个 old_text 必须在原附件中唯一匹配，各修改范围"
                    "不得重叠；保留 UTF-8 BOM 和原始换行格式。仅当用户明确要求修改上传"
                    "附件时使用；不要同时生成独立章节、改名合并稿或其他替代文件。浏览器"
                    "上传无法取得并覆盖用户电脑上的原文件。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "minLength": 1,
                            "description": "available_data_files 中的附件绝对路径",
                        },
                        "edits": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_FILE_EDITS,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_text": {"type": "string", "minLength": 1},
                                    "new_text": {"type": "string"},
                                },
                                "required": ["old_text", "new_text"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["file_path", "edits"],
                    "additionalProperties": False,
                },
            ),
            edit_attachment_file,
            context=attachment_context,
        )
    # 非 Markdown 文档附件：先转换成 Markdown 才能阅读
    document_attachment_paths = tuple(
        path
        for path in bound_paths
        if Path(path).suffix.lower() in CONVERTIBLE_DOCUMENT_SUFFIXES
    )
    if document_attachment_paths:
        registry.register(
            ToolSpec(
                name="markitdown",
                description=(
                    "把用户上传的非 Markdown 文档附件（PDF、Word、PPT、Excel、HTML、EPUB、Notebook）"
                    "转换為 Markdown 后读取内容。附件不是 .md/.txt 而又需要看其中内容时调用它；"
                    "纯文本与代码附件请改用 read_attachment_file。返回结果按行分页：默认读取开头，"
                    "truncated 为 true 时用返回的 next_offset 继续读取，不要一次索取全文。"
                    "同一附件只会真正转换一次，完整结果保存在 markdown_path，可多次分页读取。"
                    "内容较长时应结合用户目标挑选相关段落，不要逐页念完。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "minLength": 1},
                        "offset": {"type": "integer", "minimum": 1, "default": 1},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_PAGE_LINES,
                            "default": DEFAULT_PAGE_LINES,
                        },
                    },
                    "required": ["file_path"],
                    "additionalProperties": False,
                },
            ),
            convert_attachment_to_markdown,
            context={
                "authorized_paths": [str(path) for path in document_attachment_paths],
                # 转换成绝对路径：工具在子进程执行，相对路径会随 cwd 落到别处
                "converted_dir": str(Path(output_dir).expanduser().resolve() / "converted"),
            },
        )
    selected_base_ids = list(dict.fromkeys(
        base_id.strip()
        for base_id in knowledge_base_ids
        if isinstance(base_id, str) and base_id.strip()
    ))
    if selected_base_ids:
        registry.register_in_process(
            ToolSpec(
                name="search_knowledge_base",
                description=(
                    "按需检索用户为当前会话预先选定的知识库。仅当回答依赖业务口径、"
                    "内部资料、历史结论或其他知识库事实时调用；普通问答、纯计算或现有"
                    "信息已经充分时不要调用。工具只会搜索已授权知识库，不能切换或扩大"
                    "范围。返回相关片段及 source_file，回答采用检索内容时必须注明来源。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_KNOWLEDGE_QUERY_CHARS,
                            "description": "围绕当前任务提出的具体检索问题",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            search_knowledge_base,
            context={"knowledge_base_ids": selected_base_ids},
        )
    registry.register_in_process(
        ToolSpec(
            name="search_web",
            description=(
                "按需联网检索公开网页。仅当问题依赖近期变化、外部事实、最新资料或"
                "需要可核验网页来源，而现有对话、附件和知识库信息不足时调用；不要"
                "用它检索用户私有知识。返回摘要和 results，其中包含 title、url、"
                "content 等来源信息；采用搜索结果回答时必须注明对应 URL。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_WEB_SEARCH_QUERY_CHARS,
                        "description": "围绕当前任务提出的具体联网检索问题",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        search_web,
        context={"api_key": web_search_api_key or ""},
    )
    if workspace_root is not None:
        workspace_context = {"workspace_root": str(workspace_root)}
        registry.register(
            ToolSpec(
                name="list_workspace_directory",
                description=(
                    "列出当前项目目录的直接子目录和文件，用于逐层探索项目布局。"
                    "默认查看项目根目录；自动跳过依赖、版本库和缓存目录。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "relative_dir": {"type": "string", "default": "."},
                    },
                    "additionalProperties": False,
                },
            ),
            _run_list_workspace_directory,
            context=workspace_context,
        )
        registry.register(
            ToolSpec(
                name="list_workspace_files",
                description=(
                    "列出当前项目目录中的文件。只读取项目目录，自动跳过 .git、"
                    ".venv、node_modules、outputs 和缓存目录。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "relative_dir": {"type": "string", "default": "."},
                        "pattern": {"type": "string", "default": "*"},
                    },
                    "additionalProperties": False,
                },
            ),
            _run_list_workspace_files,
            context=workspace_context,
        )
        registry.register(
            ToolSpec(
                name="read_workspace_file",
                description=(
                    "读取当前项目目录内一个 UTF-8 文本文件。路径必须来自"
                    " list_workspace_directory 或 list_workspace_files，不能使用绝对路径或 '..'。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "relative_path": {"type": "string", "minLength": 1},
                        "offset": {"type": "integer", "minimum": 1, "default": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 500},
                    },
                    "required": ["relative_path"],
                    "additionalProperties": False,
                },
            ),
            _run_read_workspace_file,
            context=workspace_context,
        )
        registry.register(
            ToolSpec(
                name="search_workspace_text",
                description="在当前项目的 UTF-8 文本文件中搜索固定字符串。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1},
                        "pattern": {"type": "string", "default": "*"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            _run_search_workspace_text,
            context=workspace_context,
        )
    registry.register(
        ToolSpec(
            name="write_artifact",
            description=(
                "将内容安全写入本轮产物目录。relative_path 只能是相对路径，"
                "默认禁止覆盖，也不能写入 input。支持代码、Markdown、文本、"
                "JSON、CSV、XLSX 和 DOCX；DOCX 的 content 使用 Markdown 字符串；"
                "CSV 使用记录数组；XLSX 可使用记录数组，或"
                '{"sheets":{"工作表名":[{"列":"值"}]}}。'
            ),
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 240,
                        "description": "产物目录内的相对文件路径，必须包含扩展名",
                    },
                    "content": {
                        "description": (
                            "文本内容或 JSON 结构化内容，序列化后最大"
                            f" {MAX_ARTIFACT_CONTENT_BYTES} 字节"
                        ),
                    },
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": "是否明确覆盖已有产物",
                    },
                },
                "required": ["relative_path", "content"],
                "additionalProperties": False,
            },
        ),
        _run_write_artifact,
        context={"output_dir": str(output_dir)},
    )
    registry.register(
        ToolSpec(
            name="execute_python",
            description=(
                "运行针对当前任务动态编写的 Python 代码，并把 stdout、stderr、"
                "result 和产物清单返回给下一轮思考。代码中可直接使用 DATA_FILES"
                "（授权文件路径列表）、DATA_FILE_MAP（文件名到路径）、OUTPUT_DIR"
                "（产物目录）、WORKSPACE_DIR（当前项目目录，未绑定时为空）和"
                " ARTIFACTS（安全产物工作区）；应将关键结论赋给"
                " JSON 可序列化变量 result。生成文件时必须填写 expected_artifacts，运行时会验证"
                "文件确实产生在当前 Run。运行环境已由项目配置自动绑定，返回值"
                "中的 runtime 会说明实际解释器。仅当用户明确指定了环境路径，或你从"
                "项目文件中确认了环境目录时，才传 python_environment；不要猜测路径，"
                "也不要在代码中激活虚拟环境。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_CODE_CHARS,
                        "description": "本轮需要执行的完整 Python 源代码",
                    },
                    "expected_artifacts": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 100,
                        "description": "本次代码承诺创建或更新的产物相对路径；不生成文件时省略",
                    },
                    "python_environment": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1000,
                        "description": (
                            "可选的虚拟环境目录或 Python 解释器路径；只允许使用用户"
                            "明确提供的路径，或项目中已确认存在的相对路径"
                        ),
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        ),
        _run_python_code,
        context={
            "authorized_paths": [str(path) for path in bound_paths],
            "output_dir": str(output_dir),
            "workspace_root": str(workspace_root) if workspace_root else "",
            "python_executable": str(python_executable) if python_executable else "",
            "python_environment_source": python_environment_source,
            "python_environment_name": python_environment_name,
        },
    )
    from .artifact_inspection import inspect_artifact

    registry.register(
        ToolSpec(
            name="inspect_artifact",
            description=(
                "读取并复核当前会话中已经生成的文本、代码、Markdown、JSON 或 CSV "
                "产物。生成或修改代码及文本产物后，在最终回答前调用此工具确认实际"
                "文件内容；relative_path 必须来自工具返回的 artifacts 或 path。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "当前会话产物目录内的相对路径",
                    },
                },
                "required": ["relative_path"],
                "additionalProperties": False,
            },
        ),
        inspect_artifact,
        context={"output_dir": str(output_dir)},
    )
    if vision_llm is not None:
        from .image_tools import inspect_generated_image

        registry.register_in_process(
            ToolSpec(
                name="inspect_generated_image",
                description=(
                    "调用当前视觉模型复核已经生成的图片产物。生成或修改图片后，在"
                    "最终回答前调用此工具检查图表数据表达、标题、坐标轴、图例、布局"
                    "和遮挡；relative_path 必须来自 execute_python 返回的 artifacts。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "relative_path": {
                            "type": "string",
                            "description": "当前会话图片产物的相对路径",
                        },
                        "question": {
                            "type": "string",
                            "description": "需要复核的具体视觉问题",
                        },
                    },
                    "required": ["relative_path", "question"],
                    "additionalProperties": False,
                },
            ),
            inspect_generated_image,
            context={"llm": vision_llm, "output_dir": str(output_dir)},
        )
    image_paths = tuple(
        path
        for path in bound_paths
        if Path(path).suffix.lower() in {".gif", ".jpeg", ".jpg", ".png", ".webp"}
    )
    if image_paths and vision_llm is not None:
        from .image_tools import inspect_image

        registry.register_in_process(
            ToolSpec(
                name="inspect_image",
                description=(
                    "调用当前视觉模型查看一张已上传图片。适用于读取图片中的文字、"
                    "表格、公式、图表、几何关系和建模约束。file_path 必须从"
                    " available_data_files 中逐字选择，question 应说明需要提取或"
                    "核实的具体内容。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Runtime 授权图片的本地绝对路径",
                        },
                        "question": {
                            "type": "string",
                            "description": "需要模型回答的图片问题",
                        },
                    },
                    "required": ["file_path", "question"],
                    "additionalProperties": False,
                },
            ),
            inspect_image,
            context={
                "llm": vision_llm,
                "authorized_paths": [str(path) for path in image_paths],
            },
        )
    if workspace_root is not None:
        change_context = {
            "workspace_root": str(workspace_root),
            "python_executable": str(python_executable or ""),
            "python_environment_source": python_environment_source,
        }
        project_tools = (
            ("create_workspace_file", "在当前项目指定位置创建 UTF-8 文本或代码文件，不覆盖已有文件。路径可为项目相对路径或项目内绝对路径。",
             {"relative_path": {"type": "string", "description": "项目相对路径或项目内绝对路径"}, "content": {"type": "string"}},
             ["relative_path", "content"], run_create_workspace_file),
            ("edit_workspace_file", "一次精确修改已有项目文本文件的一处或多处内容；每个 old_text 必须在原文件中唯一匹配，各修改范围不得重叠。保留 UTF-8 BOM 和原始换行格式，并返回 diff。路径可为项目相对路径或项目内绝对路径。",
             {"relative_path": {"type": "string", "description": "项目相对路径或项目内绝对路径"},
              "edits": {"type": "array", "minItems": 1, "maxItems": MAX_FILE_EDITS, "items": {
                  "type": "object", "properties": {
                      "old_text": {"type": "string", "minLength": 1},
                      "new_text": {"type": "string"},
                  }, "required": ["old_text", "new_text"], "additionalProperties": False,
              }}},
             ["relative_path", "edits"], run_edit_workspace_file),
            ("run_project_command", "需人工逐条批准的项目命令。支持 Git 状态/差异、测试、构建，python -m venv、uv venv、conda create -p 创建项目内环境，以及 pip、uv pip、conda install、uv sync 安装依赖。命令独立运行；用 python_environment 指定后续命令的环境，不要使用无效的 shell activate。",
             {"command": {"type": "string", "maxLength": 1000},
              "python_environment": {"type": "string", "description": "可选，项目内已有环境目录或 Python 路径；每条命令单独指定"}},
             ["command"], run_project_command_tool),
        )
        for name, description, properties, required, handler in project_tools:
            registry.register(
                ToolSpec(name=name, description=description, parameters={
                    "type": "object", "properties": properties,
                    "required": required, "additionalProperties": False,
                }),
                handler,
                context=change_context,
            )
    return registry


def _workspace_root(context: Mapping[str, JsonValue], tool_name: str) -> str:
    """从工具上下文读取当前项目根目录。"""
    root = context.get("workspace_root")
    if not isinstance(root, str) or not root.strip():
        raise ToolArgumentsError(f"{tool_name} 缺少有效的项目目录上下文")
    return root


def _run_list_workspace_directory(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行项目浅层目录列表工具。"""
    if set(arguments) - {"relative_dir"}:
        raise ToolArgumentsError("list_workspace_directory 收到未声明参数")
    relative_dir = arguments.get("relative_dir", ".")
    if not isinstance(relative_dir, str):
        raise ToolArgumentsError("relative_dir 必须是字符串")
    return list_workspace_directory(
        _workspace_root(context, "list_workspace_directory"),
        relative_dir,
    )


def _run_list_workspace_files(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行项目文件列表工具。"""
    if set(arguments) - {"relative_dir", "pattern"}:
        raise ToolArgumentsError("list_workspace_files 收到未声明参数")
    relative_dir = arguments.get("relative_dir", ".")
    pattern = arguments.get("pattern", "*")
    if not isinstance(relative_dir, str) or not isinstance(pattern, str):
        raise ToolArgumentsError("relative_dir 和 pattern 必须是字符串")
    return list_workspace_files(
        _workspace_root(context, "list_workspace_files"),
        relative_dir,
        pattern,
    )


def _run_read_workspace_file(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行项目文本文件读取工具。"""
    if set(arguments) - {"relative_path", "offset", "limit"} or "relative_path" not in arguments:
        raise ToolArgumentsError("read_workspace_file 需要 relative_path，可选 offset 和 limit")
    relative_path = arguments.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ToolArgumentsError("relative_path 必须是非空字符串")
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", 500)
    if type(offset) is not int or type(limit) is not int:
        raise ToolArgumentsError("offset 和 limit 必须是整数")
    return read_workspace_file(
        _workspace_root(context, "read_workspace_file"),
        relative_path, offset=offset, limit=limit,
    )


def _run_search_workspace_text(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行项目文本搜索工具。"""
    if set(arguments) - {"query", "pattern"} or "query" not in arguments:
        raise ToolArgumentsError("search_workspace_text 需要 query，可选 pattern")
    query = arguments.get("query")
    pattern = arguments.get("pattern", "*")
    if not isinstance(query, str) or not query.strip() or not isinstance(pattern, str):
        raise ToolArgumentsError("query 必须是非空字符串，pattern 必须是字符串")
    return search_workspace_text(
        _workspace_root(context, "search_workspace_text"),
        query,
        pattern,
    )


def _run_profile_data(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行已绑定授权路径的数据画像工具。"""
    if arguments:
        raise ToolArgumentsError("profile_data 不接受调用参数")
    raw_paths = _authorized_paths(context, "profile_data")
    return profile_data(raw_paths, authorized_paths=raw_paths)


def _run_explore_math_models(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行已绑定授权路径的数学建模探索工具。"""
    allowed_arguments = {
        "focus",
        "target_column",
        "file_name",
        "sheet_name",
        "max_relationships",
    }
    unknown_arguments = set(arguments) - allowed_arguments
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        raise ToolArgumentsError(f"explore_math_models 收到未声明参数: {names}")

    focus = arguments.get("focus", "")
    if not isinstance(focus, str):
        raise ToolArgumentsError("focus 必须是字符串")
    target_column = _optional_string_argument(arguments, "target_column")
    file_name = _optional_string_argument(arguments, "file_name")
    sheet_name = _optional_string_argument(arguments, "sheet_name")
    max_relationships = _integer_argument(arguments, "max_relationships", default=8)
    if not 1 <= max_relationships <= MAX_RELATIONSHIP_LAWS:
        raise ToolArgumentsError(
            f"max_relationships 必须在 1 到 {MAX_RELATIONSHIP_LAWS} 之间"
        )
    raw_paths = _authorized_paths(context, "explore_math_models")
    return explore_math_models(
        raw_paths,
        authorized_paths=raw_paths,
        focus=focus,
        target_column=target_column,
        file_name=file_name,
        sheet_name=sheet_name,
        max_relationships=max_relationships,
    )


def _run_inspect_data_rows(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行已绑定授权路径的真实数据行查看工具。"""
    allowed_arguments = {"row_count", "start_row", "file_name", "sheet_name"}
    unknown_arguments = set(arguments) - allowed_arguments
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        raise ToolArgumentsError(f"inspect_data_rows 收到未声明参数: {names}")

    row_count = _integer_argument(arguments, "row_count", required=True)
    start_row = _integer_argument(arguments, "start_row", default=1)
    if not 1 <= row_count <= MAX_INSPECT_DATA_ROWS:
        raise ToolArgumentsError(
            f"row_count 必须在 1 到 {MAX_INSPECT_DATA_ROWS} 之间"
        )
    if start_row < 1:
        raise ToolArgumentsError("start_row 必须大于等于 1")
    file_name = _optional_string_argument(arguments, "file_name")
    sheet_name = _optional_string_argument(arguments, "sheet_name")
    raw_paths = _authorized_paths(context, "inspect_data_rows")
    return inspect_data_rows(
        raw_paths,
        authorized_paths=raw_paths,
        row_count=row_count,
        start_row=start_row,
        file_name=file_name,
        sheet_name=sheet_name,
    )


def _run_python_code(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行 Agent 动态生成的 Python 代码。"""
    if not set(arguments) <= {"code", "expected_artifacts", "python_environment"} or "code" not in arguments:
        raise ToolArgumentsError(
            "execute_python 只接受 code、expected_artifacts 和 python_environment"
        )
    code = arguments.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ToolArgumentsError("code 必须是非空字符串")
    if len(code) > _MAX_CODE_CHARS:
        raise ToolArgumentsError(f"code 不能超过 {_MAX_CODE_CHARS} 个字符")
    output_dir = context.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ToolArgumentsError("execute_python 缺少有效的产物目录上下文")
    raw_paths = _authorized_paths(context, "execute_python")
    selected_executable = context.get("python_executable") or None
    selected_source = str(context.get("python_environment_source") or "host")
    selected_name = str(context.get("python_environment_name") or "MMAgent")
    requested_environment = arguments.get("python_environment")
    if context.get("workspace_root") and selected_source == "host" and requested_environment is None:
        raise ToolArgumentsError(
            "当前项目未发现 Python 环境；不能使用 Agent 宿主环境执行代码。"
            "请先创建项目环境，或明确指定已存在的 python_environment。"
        )
    if requested_environment is not None:
        if not isinstance(requested_environment, str) or not requested_environment.strip():
            raise ToolArgumentsError("python_environment 必须是非空路径")
        candidate = Path(requested_environment.strip()).expanduser()
        workspace_root = context.get("workspace_root") or None
        if not candidate.is_absolute():
            if not isinstance(workspace_root, str) or not workspace_root:
                raise ToolArgumentsError("相对环境路径只能在已绑定项目中使用")
            candidate = Path(workspace_root) / candidate
        try:
            environment = resolve_python_environment(workspace_root, candidate)
        except (OSError, PythonEnvironmentError) as exc:
            raise ToolArgumentsError(str(exc)) from exc
        selected_executable = environment.executable
        selected_source = "requested"
        selected_name = environment.name
    expected_artifacts = arguments.get("expected_artifacts", [])
    if not isinstance(expected_artifacts, list) or not all(
        isinstance(item, str) for item in expected_artifacts
    ):
        raise ToolArgumentsError("expected_artifacts 必须是字符串数组")
    return execute_python_code(
        code,
        data_paths=raw_paths,
        output_dir=output_dir,
        workspace_root=context.get("workspace_root") or None,
        python_executable=selected_executable,
        environment_source=selected_source,
        environment_name=selected_name,
        expected_artifacts=expected_artifacts,
    )


def _run_write_artifact(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """将 LLM 提供的内容写入本轮安全产物目录。"""
    allowed_arguments = {"relative_path", "content", "overwrite"}
    unknown_arguments = set(arguments) - allowed_arguments
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        raise ToolArgumentsError(f"write_artifact 收到未声明参数: {names}")
    missing_arguments = {"relative_path", "content"} - set(arguments)
    if missing_arguments:
        names = ", ".join(sorted(missing_arguments))
        raise ToolArgumentsError(f"write_artifact 缺少必填参数: {names}")
    relative_path = arguments.get("relative_path")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ToolArgumentsError("relative_path 必须是非空字符串")
    if len(relative_path) > 240:
        raise ToolArgumentsError("relative_path 不能超过 240 个字符")
    overwrite = arguments.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ToolArgumentsError("overwrite 必须是布尔值")
    output_dir = context.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ToolArgumentsError("write_artifact 缺少有效的产物目录上下文")
    return write_artifact(
        relative_path.strip(),
        arguments.get("content"),
        output_dir=output_dir,
        overwrite=overwrite,
    )


def _authorized_paths(
    context: Mapping[str, JsonValue],
    tool_name: str,
) -> list[str]:
    """从工具上下文读取已授权路径。"""
    raw_paths = context.get("authorized_paths")
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) and path for path in raw_paths
    ):
        raise ToolArgumentsError(f"{tool_name} 缺少有效的授权文件上下文")
    return raw_paths


def _integer_argument(
    arguments: Mapping[str, JsonValue],
    name: str,
    *,
    required: bool = False,
    default: int = 0,
) -> int:
    """读取严格整数参数，拒绝被 Python 当作整数的布尔值。"""
    if name not in arguments:
        if required:
            raise ToolArgumentsError(f"inspect_data_rows 缺少必填参数: {name}")
        return default
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentsError(f"{name} 必须是整数")
    return value


def _optional_string_argument(
    arguments: Mapping[str, JsonValue],
    name: str,
) -> str | None:
    """读取可选的非空字符串参数。"""
    if name not in arguments:
        return None
    value = arguments[name]
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentsError(f"{name} 必须是非空字符串")
    return value.strip()
