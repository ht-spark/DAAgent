"""使用当前 LLM 为顶层 Plan-and-Execute 生成结构化 Todo List。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any, Sequence

from pydantic import ValidationError

from ..tools.registry import ToolSpec
from ..cancellation import invoke_cancellable, raise_if_cancelled
from .loop import Finish, LlmClient, extract_model_reasoning
from .state import AgentState, AnalysisPlan, clip_text

_PLANNER_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "planner.md"

# Planner 会话原文视野：短会话时滚动摘要为空，必须补上最近轮次以免误规划
MAX_PLANNER_HISTORY_MESSAGES = 8          # 注入的最近对话条数
MAX_PLANNER_HISTORY_ITEM_CHARS = 1_500    # 单条对话字符上限
MAX_PLANNER_HISTORY_CHARS = 6_000         # 最近对话总字符预算

# Planner 可见的工作轨迹上限（每次规划只调用一次，可比内层 ReAct 宽松）
PLANNER_TRAJECTORY_PATHS = 80
PLANNER_TRAJECTORY_DECISIONS = 30
PLANNER_TRAJECTORY_NOTES = 25
PLANNER_TRAJECTORY_FAILURES = 15
PLANNER_TRAJECTORY_ITEM_CHARS = 600

MAX_RECOVERY_RESULT_CHARS = 1_000         # 重规划时单条已完成结果的字符上限


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
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        if max_todos < 1 or max_todos > 12:
            raise ValueError("max_todos 必须在 1 到 12 之间")
        if retries < 0:
            raise ValueError("retries 不能为负数")
        self._llm = llm
        self._tools = tuple(tools)
        self._max_todos = max_todos
        self._retries = retries
        self._cancel_check = cancel_check

    def create_plan(
        self,
        state: AgentState,
        *,
        allow_direct: bool = True,
    ) -> AnalysisPlan | Finish:
        """根据请求复杂度直接回答，或生成经过校验的执行计划。

        Args:
            state: 当前 Agent 状态。
            allow_direct: 是否允许无需规划时直接返回最终回答。

        Returns:
            直接回答或结构化执行计划。
        """
        messages = self.build_messages(state)
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            raise_if_cancelled(self._cancel_check)
            response = invoke_cancellable(
                lambda: self._llm.invoke(messages),
                cancel_check=self._cancel_check,
            )
            raw = _response_text(response)
            # 抽取模型真实思考过程，挂到计划上供前端展示
            model_reasoning = extract_model_reasoning(response, raw)
            try:
                decision = _parse_planner_decision(raw)
                if isinstance(decision, Finish):
                    if not allow_direct:
                        raise PlannerError("当前请求必须返回可执行计划，不能直接回答")
                    if model_reasoning:
                        decision = decision.model_copy(update={"reasoning": model_reasoning})
                    return decision
                if model_reasoning:
                    decision.model_reasoning = model_reasoning
                if len(decision.todos) > self._max_todos:
                    raise PlannerError(
                        f"Todo 数量 {len(decision.todos)} 超过上限 {self._max_todos}"
                    )
                if any(
                    todo.status != "pending" or todo.result_summary is not None
                    for todo in decision.todos
                ):
                    raise PlannerError("Planner 生成的 Todo 必须从 pending 状态开始且不能预填结果")
                return decision
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
                "result_summary": clip_text(todo.result_summary or "", MAX_RECOVERY_RESULT_CHARS),
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
            run_budget=state.run_budget,
        )
        recovery = self.create_plan(recovery_state, allow_direct=False)
        if isinstance(recovery, Finish):  # pragma: no cover - allow_direct=False 已拦截
            raise PlannerError("失败恢复必须返回可执行计划")
        return recovery

    def build_messages(self, state: AgentState) -> list[tuple[str, str]]:
        """构造负责请求分流与规划、不执行工具的 Planner 消息。"""
        tools = [
            {"name": tool.name, "description": tool.description}
            for tool in self._tools
        ]
        trajectory = state.work_trajectory
        enriched_artifacts = trajectory.enrich_artifacts(state.available_artifacts)
        trajectory_view = trajectory.prompt_view(
            max_paths=PLANNER_TRAJECTORY_PATHS,
            max_decisions=PLANNER_TRAJECTORY_DECISIONS,
            max_notes=PLANNER_TRAJECTORY_NOTES,
            max_failures=PLANNER_TRAJECTORY_FAILURES,
            max_item_chars=PLANNER_TRAJECTORY_ITEM_CHARS,
        )
        trajectory_view["artifact_descriptions"] = dict(
            trajectory.artifact_descriptions
        )
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
            "recent_conversation": _recent_conversation(state),
            "work_trajectory": trajectory_view,
        }
        prompt = _render_planner_prompt(context, self._max_todos)
        return [
            ("system", "你是请求路由器与执行计划编译器：简单请求直接回答，复杂请求才生成 Todo List。"),
            ("user", prompt),
        ]


def _recent_conversation(state: AgentState) -> list[dict[str, str]]:
    """取最近若干轮对话原文，供 Planner 理解追问所指。

    滚动摘要只在历史很长时存在；短会话下 Planner 若无原文视野，
    会看不到上一轮的口径与结论，因此这里始终补上最近轮次。

    Args:
        state: 当前运行状态。

    Returns:
        不含空白内容、按时间升序、总长度受限的最近对话列表。
    """
    selected: list[dict[str, str]] = []
    used = 0
    for message in reversed(list(state.messages)):
        role, content = _message_role_and_content(message)
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        text = clip_text(content.strip(), MAX_PLANNER_HISTORY_ITEM_CHARS)
        if not text:
            continue
        if len(selected) >= MAX_PLANNER_HISTORY_MESSAGES:
            break
        if selected and used + len(text) > MAX_PLANNER_HISTORY_CHARS:
            break
        selected.append({"role": role, "content": text})
        used += len(text)
    selected.reverse()
    return selected


def _message_role_and_content(message: Any) -> tuple[str, Any]:
    """兼容模型对象与字典两种消息形态。"""
    if isinstance(message, Mapping):
        role = str(message.get("role") or "").strip().lower()
        return role, message.get("content")
    role = str(getattr(message, "role", "") or "").strip().lower()
    return role, getattr(message, "content", None)


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


def _parse_planner_decision(raw: str) -> AnalysisPlan | Finish:
    """从常见文本包装中提取并校验直接回答或计划对象。"""
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
            if isinstance(candidate, dict) and (
                "todos" in candidate or candidate.get("type") == "finish"
            ):
                candidates.append(candidate)
        if len(candidates) != 1:
            raise PlannerError("Planner 响应必须包含唯一的直接回答或计划 JSON 对象") from exc
        payload = candidates[0]
    if not isinstance(payload, dict):
        raise PlannerError("Planner 响应必须是 JSON 对象")
    try:
        if payload.get("type") == "finish":
            return Finish.model_validate(payload)
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
