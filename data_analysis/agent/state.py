"""定义 Agent 决策循环使用的可持久化状态。"""

from __future__ import annotations

import mimetypes
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ..experts.models import ExpertContext
from ..skills.models import SkillContext, SkillObservation


class TodoItem(BaseModel):
    """顶层执行计划中的一个可独立完成的任务。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    objective: str
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"
    depends_on: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(min_length=1)
    result_summary: str | None = None

    @field_validator("id", "objective")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """清理任务标识与目标并拒绝空白值。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("Todo 标识和目标不能为空")
        return normalized

    @field_validator("expected_outputs")
    @classmethod
    def validate_expected_outputs(cls, values: list[str]) -> list[str]:
        """清理验收结果并拒绝空白或重复描述。"""
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            output = value.strip()
            if not output:
                raise ValueError("Todo 预期输出不能为空")
            if output not in seen:
                normalized.append(output)
                seen.add(output)
        return normalized


class AnalysisPlan(BaseModel):
    """Planner 生成的顶层 Todo List。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    objective: str
    reasoning_summary: str = ""
    # 兼容旧计划快照的内部字段；对外只公开 reasoning_summary。
    model_reasoning: str = ""
    todos: list[TodoItem] = Field(min_length=1, max_length=12)

    @field_validator("objective", "reasoning_summary")
    @classmethod
    def normalize_plan_text(cls, value: str, info: ValidationInfo) -> str:
        """清理计划文本，并拒绝空白的总体目标。"""
        normalized = value.strip()
        if getattr(info, "field_name", "") == "objective" and not normalized:
            raise ValueError("计划目标不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_dependencies(self) -> "AnalysisPlan":
        """保证 Todo 标识唯一、依赖存在且不存在循环依赖。"""
        identifiers = [todo.id for todo in self.todos]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Todo id 不能重复")
        known = set(identifiers)
        dependencies = {todo.id: set(todo.depends_on) for todo in self.todos}
        for todo_id, required in dependencies.items():
            if todo_id in required:
                raise ValueError(f"Todo {todo_id} 不能依赖自身")
            missing = required - known
            if missing:
                raise ValueError(f"Todo {todo_id} 依赖不存在的任务: {sorted(missing)}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(todo_id: str) -> None:
            if todo_id in visiting:
                raise ValueError("Todo 依赖不能形成循环")
            if todo_id in visited:
                return
            visiting.add(todo_id)
            for dependency in dependencies[todo_id]:
                visit(dependency)
            visiting.remove(todo_id)
            visited.add(todo_id)

        for identifier in identifiers:
            visit(identifier)
        return self


class AgentMessage(BaseModel):
    """一条由用户或 Agent 产生的对话消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        """拒绝空白消息，同时保留原始文本格式。"""
        if not value.strip():
            raise ValueError("消息内容不能为空")
        return value


class Observation(BaseModel):
    """Harness 执行一次工具调用后返回的标准化结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    success: bool
    result: JsonValue = None
    error: str | None = None
    duration_ms: int = Field(default=0, ge=0)

    @field_validator("tool_name")
    @classmethod
    def normalize_tool_name(cls, value: str) -> str:
        """规范化工具名称并拒绝空名称。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("工具名称不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_outcome(self) -> "Observation":
        """保证成功状态与错误信息保持一致。"""
        if self.success and self.error is not None:
            raise ValueError("工具执行成功时不能包含错误信息")
        if not self.success and (self.error is None or not self.error.strip()):
            raise ValueError("工具执行失败时必须包含错误信息")
        return self


class ArtifactReference(BaseModel):
    """可供后续决策检查或复用的真实产物引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    absolute_path: str
    name: str
    size_bytes: int = Field(ge=0)
    media_type: str
    source_tool: str
    reusable: bool = True
    description: str | None = None

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        """规范化并限制产物相对路径。"""
        candidate = Path(value.strip())
        if (
            not value.strip()
            or candidate.is_absolute()
            or ".." in candidate.parts
            or not candidate.parts
            or candidate.parts[0].casefold() in {".runs", ".runtime"}
        ):
            raise ValueError("产物必须是输出目录内的有效相对路径")
        return candidate.as_posix()

    @field_validator("absolute_path")
    @classmethod
    def normalize_absolute_path(cls, value: str) -> str:
        """把产物物理路径规范化为绝对路径。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("产物绝对路径不能为空")
        return str(Path(normalized).expanduser().resolve(strict=False))

    @field_validator("name", "media_type", "source_tool")
    @classmethod
    def validate_metadata_text(cls, value: str) -> str:
        """拒绝空白产物元数据。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("产物元数据不能为空")
        return normalized


def discover_artifacts(output_dir: str | Path | None) -> list[ArtifactReference]:
    """发现会话产物目录中可供当前运行复用的已有文件。

    Args:
        output_dir: 当前会话授权的产物目录。

    Returns:
        已验证、排除运行缓存和输入附件后的产物引用。
    """
    if output_dir is None:
        return []
    root = Path(output_dir).expanduser().resolve(strict=False)
    if not root.is_dir():
        return []
    artifacts: list[ArtifactReference] = []
    for target in sorted(root.rglob("*")):
        if not target.is_file():
            continue
        relative_path = target.relative_to(root)
        if relative_path.parts[0].casefold() in {".runs", ".runtime", "input"}:
            continue
        artifacts.append(
            _artifact_reference(
                root,
                relative_path.as_posix(),
                source_tool="existing_session",
            )
        )
    return artifacts


MAX_TRAJECTORY_NOTES = 40
MAX_TRAJECTORY_NOTE_CHARS = 240
MAX_TRAJECTORY_FAILURES = 20
MAX_TRAJECTORY_PATHS = 200
MAX_TRAJECTORY_DECISIONS = 100
MAX_TRAJECTORY_DECISION_CHARS = 400
MAX_ARTIFACT_DESCRIPTION_CHARS = 200
MAX_ARTIFACT_DESCRIPTIONS = 50

# 单条文本裁剪标记，供轨迹/结果摘要共用
CLIP_MARKER = "…（已截断）"


def clip_text(text: str, limit: int) -> str:
    """按字符上限裁剪文本，超长时追加统一的截断标记。

    Args:
        text: 原始文本。
        limit: 允许保留的最大字符数。

    Returns:
        不超过 limit 个字符的文本。
    """
    if limit < 1:
        raise ValueError("裁剪上限必须大于 0")
    if len(text) <= limit:
        return text
    tail = CLIP_MARKER if limit > len(CLIP_MARKER) else ""
    return text[: limit - len(tail)] + tail


def summarize_tool_payload(result: JsonValue = None) -> str | None:
    """从工具结果中提炼一行可读摘要（字段、质量结论等）。

    Args:
        result: 工具返回的 JSON 结果。

    Returns:
        单行摘要；无法提炼时返回 None。
    """
    if not isinstance(result, Mapping):
        return None
    for key in ("summary", "analysis", "message"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value.strip())
        if isinstance(value, Mapping):
            compact = ", ".join(
                f"{item_key}={item_value}"
                for item_key, item_value in list(value.items())[:8]
                if item_value is not None and str(item_value).strip()
            )
            if compact:
                return _clip(compact)
    fields = result.get("fields") or result.get("columns")
    if isinstance(fields, list) and fields:
        names = [str(item) for item in fields[:12]]
        return _clip("字段 " + ", ".join(names))
    return None


class WorkTrajectory(BaseModel):
    """跨轮可复用的结构化工作轨迹，供 Planner 避免重复规划与重复读文件。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    files_read: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # 已尝试且失败的调用要点（工具 + 路径 + 错误），供重规划与追问避免重复踩坑
    failures: list[str] = Field(default_factory=list)
    # 产物相对路径 → 简要描述（是什么/包含什么），跨轮保留
    artifact_descriptions: dict[str, str] = Field(default_factory=dict)

    def add_read(self, path: str | None) -> None:
        """登记已读文件路径。"""
        self._add_path("files_read", path)

    def add_modified(self, path: str | None) -> None:
        """登记已修改或已创建文件路径。"""
        self._add_path("files_modified", path)

    def add_decision(self, text: str | None) -> None:
        """登记一条关键决定或已完成结论。"""
        self._add_unique_text(
            "key_decisions",
            text,
            max_chars=MAX_TRAJECTORY_DECISION_CHARS,
            max_items=MAX_TRAJECTORY_DECISIONS,
        )

    def add_note(self, text: str | None) -> None:
        """登记一条工具观察要点，供跨 Todo / 跨轮复用。"""
        self._add_unique_text(
            "notes",
            text,
            max_chars=MAX_TRAJECTORY_NOTE_CHARS,
            max_items=MAX_TRAJECTORY_NOTES,
        )

    def add_failure(self, text: str | None) -> None:
        """登记一条失败调用要点，供重规划与跨轮追问复用。"""
        self._add_unique_text(
            "failures",
            text,
            max_chars=MAX_TRAJECTORY_NOTE_CHARS,
            max_items=MAX_TRAJECTORY_FAILURES,
        )

    def describe_artifact(self, path: str | None, description: str | None) -> None:
        """记录产物简要描述（路径 → 是什么）。新描述覆盖旧描述。"""
        if not isinstance(path, str) or not isinstance(description, str):
            return
        key = path.strip().replace("\\", "/")
        text = description.strip()[:MAX_ARTIFACT_DESCRIPTION_CHARS].strip()
        if not key or not text:
            return
        # 规范 key，避免同一文件出现两种写法
        key = key.lstrip("./")
        # 重新插入以把刚更新的描述移到末尾，裁剪时保留最近更新的条目
        self.artifact_descriptions.pop(key, None)
        self.artifact_descriptions[key] = text
        if len(self.artifact_descriptions) > MAX_ARTIFACT_DESCRIPTIONS:
            # 保留最近写入的描述
            trimmed = dict(
                list(self.artifact_descriptions.items())[-MAX_ARTIFACT_DESCRIPTIONS:]
            )
            self.artifact_descriptions = trimmed

    def prompt_view(
        self,
        *,
        max_paths: int = 40,
        max_decisions: int = 12,
        max_notes: int = 15,
        max_failures: int = 8,
        max_item_chars: int = 400,
    ) -> dict[str, list[str]]:
        """生成给 LLM 阅读的精简轨迹视图：只保留最近条目并裁剪单条长度。

        Args:
            max_paths: 已读/已改文件各自保留的条数。
            max_decisions: 关键决定保留的条数。
            max_notes: 观察要点保留的条数。
            max_failures: 失败要点保留的条数。
            max_item_chars: 单条文本的字符上限。

        Returns:
            可直接放入 prompt 的轨迹字典（按时间尾部取值）。
        """

        def tail(items: list[str], limit: int) -> list[str]:
            return [clip_text(item, max_item_chars) for item in items[-limit:]]

        return {
            "files_read": tail(self.files_read, max_paths),
            "files_modified": tail(self.files_modified, max_paths),
            "key_decisions": tail(self.key_decisions, max_decisions),
            "notes": tail(self.notes, max_notes),
            "failures": tail(self.failures, max_failures),
        }

    def description_for(self, path: str | None) -> str | None:
        """按相对路径读取产物描述。"""
        if not isinstance(path, str):
            return None
        key = path.strip().replace("\\", "/").lstrip("./")
        return self.artifact_descriptions.get(key)

    def enrich_artifacts(
        self, artifacts: Sequence[ArtifactReference]
    ) -> list[ArtifactReference]:
        """把轨迹中的简要描述合并进产物引用列表。"""
        enriched: list[ArtifactReference] = []
        for artifact in artifacts:
            if artifact.description:
                enriched.append(artifact)
                continue
            description = self.description_for(artifact.relative_path)
            if description:
                enriched.append(artifact.model_copy(update={"description": description}))
            else:
                enriched.append(artifact)
        return enriched

    def merge(self, other: "WorkTrajectory | Mapping[str, Any] | None") -> None:
        """合并另一份轨迹（会话历史或先前保存的 metadata）。"""
        if other is None:
            return
        if isinstance(other, WorkTrajectory):
            payload: Mapping[str, Any] = other.model_dump()
        elif isinstance(other, Mapping):
            payload = other
        else:
            return
        for raw in payload.get("files_read") or []:
            if isinstance(raw, str):
                self.add_read(raw)
        for raw in payload.get("files_modified") or []:
            if isinstance(raw, str):
                self.add_modified(raw)
        for raw in payload.get("key_decisions") or []:
            if isinstance(raw, str):
                self.add_decision(raw)
        for raw in payload.get("notes") or []:
            if isinstance(raw, str):
                self.add_note(raw)
        for raw in payload.get("failures") or []:
            if isinstance(raw, str):
                self.add_failure(raw)
        descriptions = payload.get("artifact_descriptions")
        if isinstance(descriptions, Mapping):
            for key, value in descriptions.items():
                if isinstance(key, str) and isinstance(value, str):
                    self.describe_artifact(key, value)

    def observe_tool(
        self,
        tool_name: str,
        *,
        arguments: Mapping[str, Any] | None = None,
        result: JsonValue = None,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        """根据一次工具调用更新已读/已改文件、观察要点与失败证据。

        Args:
            tool_name: 工具名。
            arguments: 本次调用的参数。
            result: 工具返回结果。
            success: 是否执行成功。
            error: 失败时的错误信息，用于记录失败证据。
        """
        name = (tool_name or "").strip()
        if not name:
            return
        args = arguments if isinstance(arguments, Mapping) else {}
        result_map = result if isinstance(result, Mapping) else {}

        if not success:
            # 失败调用既不登记为已读/已改，也要留下可复用的失败证据
            self.add_failure(_failure_text(name, args, result_map, error))
            return

        if name in {"read_attachment_file", "markitdown", "inspect_image"}:
            self.add_read(_first_str(args, result_map, "file_path", "path"))
        elif name in {"read_workspace_file", "inspect_artifact", "inspect_generated_image"}:
            self.add_read(_first_str(args, result_map, "relative_path", "path"))
        elif name in {
            "write_artifact",
            "edit_attachment_file",
            "write_workspace_file",
            "edit_workspace_file",
        }:
            self.add_modified(_first_str(args, result_map, "relative_path", "path"))
        elif name == "execute_python":
            artifacts = result_map.get("artifacts")
            if isinstance(artifacts, list):
                for item in artifacts:
                    if isinstance(item, str):
                        self.add_modified(item)
                    elif isinstance(item, Mapping):
                        self.add_modified(_first_str(item, item, "path", "relative_path"))

        note = _note_from_tool_result(name, args, result_map)
        if note:
            self.add_note(note)

        description = _artifact_description_from_result(name, args, result_map)
        if description and result_map.get("in_place") is not True:
            path = _first_str(args, result_map, "relative_path", "path", "file_path")
            if path:
                self.describe_artifact(path, description)

    def absorb_completed_todos(self, plan: "AnalysisPlan | None") -> None:
        """把已完成 Todo 的结果摘要并入关键决定。"""
        if plan is None:
            return
        for todo in plan.todos:
            if todo.status == "completed" and todo.result_summary:
                self.add_decision(f"{todo.id}: {todo.result_summary}")

    def _add_path(self, field: str, path: str | None) -> None:
        if not isinstance(path, str):
            return
        normalized = path.strip().replace("\\", "/")
        if not normalized:
            return
        items: list[str] = getattr(self, field)
        existing = {item.casefold() for item in items}
        if normalized.casefold() not in existing:
            items.append(normalized)
            if len(items) > MAX_TRAJECTORY_PATHS:
                # 只保留最近使用的路径，避免轨迹无界增长
                items = items[-MAX_TRAJECTORY_PATHS:]
            setattr(self, field, items)

    def _add_unique_text(
        self,
        field: str,
        text: str | None,
        *,
        max_chars: int | None = None,
        max_items: int | None = None,
    ) -> None:
        if not isinstance(text, str):
            return
        normalized = text.strip()
        if max_chars is not None:
            normalized = clip_text(normalized, max_chars).strip()
        if not normalized:
            return
        items: list[str] = getattr(self, field)
        existing = {item.casefold() for item in items}
        if normalized.casefold() in existing:
            return
        items.append(normalized)
        if max_items is not None and len(items) > max_items:
            items = items[-max_items:]
        setattr(self, field, items)


def _artifact_description_from_result(
    tool_name: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
) -> str | None:
    """从工具结果/写入参数提炼产物简要描述。"""
    for key in ("description", "summary", "analysis"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            compact = ", ".join(
                f"{item_key}={item_value}"
                for item_key, item_value in list(value.items())[:6]
                if item_value is not None and str(item_value).strip()
            )
            if compact:
                return compact

    # write_artifact：用正文首行概括「是什么」
    content = arguments.get("content")
    if isinstance(content, str) and content.strip():
        for line in content.strip().splitlines():
            text = line.strip().lstrip("#*- ").strip()
            if text:
                return f"写入：{text[:MAX_ARTIFACT_DESCRIPTION_CHARS]}"
    return None


def _note_from_tool_result(
    tool_name: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
) -> str | None:
    """从工具结果提取一条短观察要点，供跨 Todo 复用。"""
    summary = summarize_tool_payload(result)
    if summary is None:
        return None
    path = _first_str(arguments, result, "file_path", "relative_path", "path")
    prefix = f"{tool_name} {path}:" if path else f"{tool_name}:"
    return f"{prefix} {summary}"


def _failure_text(
    tool_name: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    error: str | None,
) -> str:
    """生成一行失败要点：工具 + 目标路径 + 错误原因。"""
    path = _first_str(arguments, result, "file_path", "relative_path", "path", "command")
    prefix = f"{tool_name} {path}:" if path else f"{tool_name}:"
    detail = error.strip() if isinstance(error, str) and error.strip() else "工具执行失败"
    return f"{prefix} {_clip(detail)}"


def _clip(text: str) -> str:
    """裁剪单条观察要点，避免轨迹被长输出撑爆。"""
    return clip_text(text, MAX_TRAJECTORY_NOTE_CHARS)


def _first_str(*sources_and_keys: Any) -> str | None:
    """从多个 Mapping 中按字段名顺序返回第一个非空字符串。

    用法：``_first_str(args, result_map, "file_path", "path")``，前面是映射，
    后面是候选键名。
    """
    sources: list[Mapping[str, Any]] = []
    keys: list[str] = []
    for item in sources_and_keys:
        if isinstance(item, Mapping):
            sources.append(item)
        elif isinstance(item, str):
            keys.append(item)
    for key in keys:
        for source in sources:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def collect_work_trajectory_from_session(
    entries: Sequence[Mapping[str, Any]],
) -> WorkTrajectory:
    """从持久化会话条目汇总跨轮工作轨迹。"""
    trajectory = WorkTrajectory()
    for entry in entries:
        metadata = entry.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        trajectory.merge(metadata.get("work_trajectory"))
        trace = metadata.get("agent_trace")
        if not isinstance(trace, list):
            continue
        for item in trace:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type == "agent_action":
                arguments = item.get("arguments")
                trajectory.observe_tool(
                    str(item.get("tool_name") or ""),
                    arguments=arguments if isinstance(arguments, Mapping) else None,
                )
            elif item_type == "agent_observation":
                success = bool(item.get("success", True))
                summary = item.get("summary")
                error = item.get("error")
                trajectory.observe_tool(
                    str(item.get("tool_name") or ""),
                    result={"summary": summary} if summary is not None else None,
                    success=success,
                    error=error if isinstance(error, str) else None,
                )
    return trajectory


class RunBudget(BaseModel):
    """一次 Agent 运行共享的决策预算。

    Plan-and-Execute 为每个 Todo 创建独立状态，若各自计数则总预算会被
    放大到「Todo 数 × 上限」；本对象按引用在所有 Todo 与最终汇总间共享。
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    iterations: int = Field(default=0, ge=0)


class AgentState(BaseModel):
    """一次思考—执行—观察循环的完整可变状态。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    user_query: str
    memory_summary: str | None = None
    data_paths: list[str] = Field(default_factory=list)
    output_dir: str | None = None
    workspace_root: str | None = None
    project_id: str | None = None
    available_artifacts: list[ArtifactReference] = Field(default_factory=list)
    messages: list[AgentMessage] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    expert_context: ExpertContext = Field(default_factory=ExpertContext)
    skill_context: SkillContext = Field(default_factory=SkillContext)
    skill_observations: list[SkillObservation] = Field(default_factory=list)
    iteration: int = Field(default=0, ge=0)
    analysis_plan: AnalysisPlan | None = None
    current_todo_id: str | None = None
    work_trajectory: WorkTrajectory = Field(default_factory=WorkTrajectory)
    # 同一次运行内所有 Todo 与最终汇总共享的决策预算
    run_budget: RunBudget = Field(default_factory=RunBudget)

    @field_validator("user_query")
    @classmethod
    def validate_user_query(cls, value: str) -> str:
        """拒绝空白用户问题，同时保留原始文本格式。"""
        if not value.strip():
            raise ValueError("用户问题不能为空")
        return value

    @field_validator("memory_summary")
    @classmethod
    def validate_memory_summary(cls, value: str | None) -> str | None:
        """清理滚动记忆摘要并拒绝空白摘要。"""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("滚动记忆摘要不能为空")
        return normalized

    @field_validator("data_paths")
    @classmethod
    def normalize_data_paths(cls, values: list[str]) -> list[str]:
        """规范化、绝对化并去重本次运行已授权的数据文件路径。"""
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            raw_path = value.strip()
            if not raw_path:
                raise ValueError("数据文件路径不能为空")
            path = str(Path(raw_path).expanduser().resolve(strict=False))
            identity = path.casefold()
            if identity not in seen:
                normalized.append(path)
                seen.add(identity)
        return normalized

    @field_validator("output_dir", "workspace_root", "project_id", "current_todo_id")
    @classmethod
    def validate_optional_context(cls, value: str | None) -> str | None:
        """拒绝空白的项目和目录上下文字段。"""
        if value is not None and not value.strip():
            raise ValueError("项目上下文字段不能为空")
        return value

    def append_message(self, role: Literal["user", "assistant"], content: str) -> None:
        """追加一条经过校验的对话消息。

        Args:
            role: 消息角色，只允许用户或 Agent。
            content: 消息正文。
        """
        self.messages.append(AgentMessage(role=role, content=content))

    def record_observation(self, observation: Observation) -> None:
        """记录本决策循环的一次工具观察，并推进一步决策轮次。

        Args:
            observation: Harness 返回的标准化工具结果。
        """
        self.observations.append(observation)
        self.absorb_observation(observation)
        self.iteration += 1
        self.run_budget.iterations += 1

    def absorb_observation(self, observation: Observation) -> None:
        """只聚合观察带来的轨迹与真实产物，不保留结果、不推进决策轮次。

        运行级状态用它收集跨 Todo 的产物与要点；决策轮次由正在执行决策的状态
        （Todo 内部循环）计数，避免同一次工具调用被重复计入运行预算。

        Args:
            observation: Harness 返回的标准化工具结果。
        """
        self.work_trajectory.observe_tool(
            observation.tool_name,
            result=observation.result,
            success=observation.success,
            error=observation.error,
        )
        if not observation.success or self.output_dir is None:
            return
        for artifact in _observation_artifacts(observation, self.output_dir):
            self.work_trajectory.add_modified(artifact.relative_path)
            self._upsert_artifact(artifact)

    def record_skill_observation(self, observation: SkillObservation) -> None:
        """记录 Skill 观察，并与普通工具共同消耗决策预算。"""
        self.skill_observations.append(observation)
        self.absorb_skill_observation(observation)
        self.iteration += 1
        self.run_budget.iterations += 1

    def absorb_skill_observation(self, observation: SkillObservation) -> None:
        """把 Skill 操作摘要加入跨 Todo 工作轨迹。"""
        target = observation.resource_id or observation.entrypoint_id or observation.operation
        if observation.success:
            self.work_trajectory.add_note(
                f"Skill {observation.skill_id} 已执行 {observation.operation}: {target}"
            )
        else:
            self.work_trajectory.add_failure(
                f"Skill {observation.skill_id} 的 {observation.operation} 失败: "
                f"{observation.error or '未知错误'}"
            )
        if not observation.success or self.output_dir is None:
            return
        if not isinstance(observation.result, Mapping):
            return
        raw_artifacts = observation.result.get("artifacts")
        if not isinstance(raw_artifacts, list):
            return
        for raw_path in raw_artifacts:
            if not isinstance(raw_path, str):
                continue
            try:
                artifact = _artifact_reference(
                    Path(self.output_dir).resolve(strict=False),
                    raw_path,
                    source_tool=f"skill:{observation.skill_id}:{observation.operation}",
                )
            except (OSError, ValueError):
                continue
            self.work_trajectory.add_modified(artifact.relative_path)
            self._upsert_artifact(artifact)

    def _upsert_artifact(self, artifact: ArtifactReference) -> None:
        """按相对路径更新产物状态，避免同一文件重复进入上下文。"""
        identity = artifact.relative_path.casefold()
        if not artifact.description:
            description = self.work_trajectory.description_for(artifact.relative_path)
            if description:
                artifact = artifact.model_copy(update={"description": description})
        self.available_artifacts = [
            current
            for current in self.available_artifacts
            if current.relative_path.casefold() != identity
        ]
        self.available_artifacts.append(artifact)


def _observation_artifacts(
    observation: Observation,
    output_dir: str | Path,
) -> list[ArtifactReference]:
    """从通用工具结果中提取当前输出目录内真实存在的产物。"""
    result = observation.result
    if not isinstance(result, Mapping):
        return []
    candidates: dict[str, Mapping[str, Any]] = {}
    details = result.get("artifact_details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                candidates[item["path"]] = item

    for key in ("changed_artifacts", "created_artifacts"):
        raw_paths = result.get(key)
        if isinstance(raw_paths, list):
            for raw_path in raw_paths:
                if isinstance(raw_path, str):
                    candidates.setdefault(raw_path, {})

    if (
        observation.tool_name in {"write_artifact", "edit_attachment_file"}
        and result.get("in_place") is not True
        and isinstance(result.get("path"), str)
    ):
        candidates.setdefault(result["path"], result)

    root = Path(output_dir).expanduser().resolve(strict=False)
    artifacts: list[ArtifactReference] = []
    for relative_path in candidates:
        try:
            artifacts.append(
                _artifact_reference(
                    root,
                    relative_path,
                    source_tool=observation.tool_name,
                )
            )
        except (OSError, ValueError):
            continue
    return artifacts


def _artifact_reference(
    root: Path,
    relative_path: str,
    *,
    source_tool: str,
) -> ArtifactReference:
    """为输出目录内的现存文件创建规范化产物引用。"""
    candidate = Path(relative_path.strip())
    if (
        not relative_path.strip()
        or candidate.is_absolute()
        or ".." in candidate.parts
        or not candidate.parts
        or candidate.parts[0].casefold() in {".runs", ".runtime", "input"}
    ):
        raise ValueError("产物路径无效")
    target = (root / candidate).resolve(strict=True)
    if root not in target.parents or not target.is_file():
        raise ValueError("产物不在当前输出目录")
    known_types = {
        ".csv": "text/csv",
        ".json": "application/json",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
    }
    media_type = (
        known_types.get(target.suffix.casefold())
        or mimetypes.guess_type(target.name)[0]
        or "application/octet-stream"
    )
    return ArtifactReference(
        relative_path=target.relative_to(root).as_posix(),
        absolute_path=str(target),
        name=target.name,
        size_bytes=target.stat().st_size,
        media_type=media_type,
        source_tool=source_tool,
    )
