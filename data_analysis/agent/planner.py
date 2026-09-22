"""使用当前 LLM 为顶层 Plan-and-Execute 生成结构化 Todo List。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from ..tools.registry import ToolSpec
from .loop import LlmClient
from .state import AgentState, AnalysisPlan

_PLANNER_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "planner.md"


class PlannerError(RuntimeError):
    """Planner 无法生成有效执行计划。"""


class Planner:
    """将当前用户目标编译为带依赖关系的 Todo List。"""

    def __init__(
        self,
        llm: LlmClient,
        tools: Sequence[ToolSpec],
        *,
        max_todos: int = 8,
        retries: int = 2,
    ) -> None:
        if max_todos < 1 or max_todos > 12:
            raise ValueError("max_todos 必须在 1 到 12 之间")
        if retries < 0:
            raise ValueError("retries 不能为负数")
        self._llm = llm
        self._tools = tuple(tools)
        self._max_todos = max_todos
        self._retries = retries

    def create_plan(self, state: AgentState) -> AnalysisPlan:
        """根据用户目标、附件与工具生成经过校验的执行计划。"""
        messages = self.build_messages(state)
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            response = self._llm.invoke(messages)
            raw = _response_text(response)
            try:
                plan = _parse_plan(raw)
                if len(plan.todos) > self._max_todos:
                    raise PlannerError(
                        f"Todo 数量 {len(plan.todos)} 超过上限 {self._max_todos}"
                    )
                if any(
                    todo.status != "pending" or todo.result_summary is not None
                    for todo in plan.todos
                ):
                    raise PlannerError("Planner 生成的 Todo 必须从 pending 状态开始且不能预填结果")
                return plan
            except (PlannerError, ValidationError) as exc:
                last_error = exc
                if attempt >= self._retries:
                    break
                messages.extend([
                    ("assistant", raw),
                    (
                        "user",
                        f"上一份计划无效：{exc}。请修正后只输出一个合法 JSON 对象。",
                    ),
                ])
        raise PlannerError(f"无法生成有效执行计划：{last_error}")

    def create_recovery_plan(
        self,
        state: AgentState,
        plan: AnalysisPlan,
        *,
        failed_todo_id: str,
        failure_summary: str,
    ) -> AnalysisPlan:
        """根据已完成结果和真实失败证据重新规划尚未完成的工作。"""
        completed = [
            {
                "id": todo.id,
                "objective": todo.objective,
                "result_summary": todo.result_summary,
            }
            for todo in plan.todos
            if todo.status == "completed"
        ]
        remaining = [
            {
                "objective": todo.objective,
                "expected_outputs": todo.expected_outputs,
            }
            for todo in plan.todos
            if todo.status == "pending"
        ]
        recovery_query = (
            f"原始目标：{state.user_query}\n\n"
            f"已完成结果：{json.dumps(completed, ensure_ascii=False)}\n"
            f"失败 Todo：{failed_todo_id}\n"
            f"失败证据：{failure_summary}\n"
            f"原计划剩余工作：{json.dumps(remaining, ensure_ascii=False)}\n\n"
            "请重新规划完成原始目标所需的剩余工作。不得原样重复已经熔断的调用；"
            "优先更换工具、参数或执行策略。若某项无法继续，可规划一个明确说明限制的 Todo。"
        )
        recovery_state = AgentState(
            user_query=recovery_query,
            memory_summary=state.memory_summary,
            data_paths=list(state.data_paths),
            output_dir=state.output_dir,
            workspace_root=state.workspace_root,
            project_id=state.project_id,
            available_artifacts=list(state.available_artifacts),
            messages=list(state.messages),
            work_trajectory=state.work_trajectory.model_copy(deep=True),
        )
        return self.create_plan(recovery_state)

    def build_messages(self, state: AgentState) -> list[tuple[str, str]]:
        """构造只负责规划、不执行工具的 Planner 消息。"""
        tools = [
            {"name": tool.name, "description": tool.description}
            for tool in self._tools
        ]
        trajectory = state.work_trajectory
        enriched_artifacts = trajectory.enrich_artifacts(state.available_artifacts)
        context = {
            "user_query": state.user_query,
            "available_data_files": [
                {
                    "file_name": Path(path).name,
                    "file_path": str(Path(path).expanduser().resolve(strict=False)),
                    "absolute_path": str(
                        Path(path).expanduser().resolve(strict=False)
                    ),
                }
                for path in state.data_paths
            ],
            "artifact_reuse_status": "已有产物可直接复用",
            "available_artifacts": [
                artifact.model_dump(mode="json")
                for artifact in enriched_artifacts
            ],
            "workspace_enabled": bool(state.workspace_root),
            "available_tools": tools,
            "session_memory": state.memory_summary,
            "work_trajectory": {
                "files_read": list(trajectory.files_read),
                "files_modified": list(trajectory.files_modified),
                "key_decisions": list(trajectory.key_decisions),
                "notes": list(trajectory.notes),
                "artifact_descriptions": dict(trajectory.artifact_descriptions),
            },
        }
        prompt = _render_planner_prompt(context, self._max_todos)
        return [
            ("system", "你是执行计划编译器，只生成简洁、可执行、可验证的 Todo List。"),
            ("user", prompt),
        ]


def _render_planner_prompt(context: dict[str, Any], max_todos: int) -> str:
    """加载 Planner 模板并注入本轮结构化上下文。"""
    try:
        template = _PLANNER_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlannerError(f"无法读取 Planner 提示词: {exc}") from exc
    return (
        template.replace("{{MAX_TODOS}}", str(max_todos))
        .replace(
            "{{CONTEXT_JSON}}",
            json.dumps(context, ensure_ascii=False, indent=2),
        )
        .strip()
    )


def _parse_plan(raw: str) -> AnalysisPlan:
    """从常见文本包装中提取并校验唯一计划对象。"""
    text = re.sub(
        r"^\s*<think>.*?</think>\s*",
        "",
        raw,
        count=1,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"}:
            text = "\n".join(lines[1:-1]).strip()
    decoder = json.JSONDecoder(strict=False)
    try:
        payload = decoder.decode(text)
    except json.JSONDecodeError as exc:
        candidates: list[dict[str, Any]] = []
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "todos" in candidate:
                candidates.append(candidate)
        if len(candidates) != 1:
            raise PlannerError("Planner 响应必须包含唯一计划 JSON 对象") from exc
        payload = candidates[0]
    if not isinstance(payload, dict):
        raise PlannerError("Planner 响应必须是 JSON 对象")
    try:
        return AnalysisPlan.model_validate(payload)
    except ValidationError as exc:
        raise PlannerError(f"计划字段无效: {exc.errors()}") from exc


def _response_text(response: Any) -> str:
    """提取常见聊天模型响应中的文本。"""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if parts:
            return "".join(parts)
    raise PlannerError("Planner 响应不包含文本")
