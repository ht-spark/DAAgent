"""定义 Agent 决策循环使用的可持久化状态。"""

from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


class TodoItem(BaseModel):
    """顶层执行计划中的一个可独立完成的任务。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    objective: str
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"
    depends_on: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    result_summary: str | None = None

    @field_validator("id", "objective")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """清理任务标识与目标并拒绝空白值。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("Todo 标识和目标不能为空")
        return normalized


class AnalysisPlan(BaseModel):
    """Planner 生成的顶层 Todo List。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    objective: str
    todos: list[TodoItem] = Field(min_length=1, max_length=12)

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        """清理计划目标并拒绝空白值。"""
        normalized = value.strip()
        if not normalized:
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


class AgentState(BaseModel):
    """一次思考—执行—观察循环的完整可变状态。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    user_query: str
    memory_summary: str | None = None
    data_paths: list[str] = Field(default_factory=list)
    output_dir: str | None = None
    workspace_root: str | None = None
    project_id: str | None = None
    messages: list[AgentMessage] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    iteration: int = Field(default=0, ge=0)
    analysis_plan: AnalysisPlan | None = None
    current_todo_id: str | None = None

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
        """清理并去重本次运行已授权的数据文件路径。"""
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            path = value.strip()
            if not path:
                raise ValueError("数据文件路径不能为空")
            if path not in seen:
                normalized.append(path)
                seen.add(path)
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
        """记录工具观察结果并推进一次决策轮次。

        Args:
            observation: Harness 返回的标准化工具结果。
        """
        self.observations.append(observation)
        self.iteration += 1
