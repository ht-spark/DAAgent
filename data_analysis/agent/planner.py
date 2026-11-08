"""使用当前 LLM 为顶层 Plan-and-Execute 生成结构化 Todo List。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..experts.models import ExpertContext
from ..tools.registry import ToolSpec
from ..cancellation import raise_if_cancelled
from .loop import (
    Finish,
    LlmClient,
    ProgressCallback,
    invoke_llm_with_public_progress,
    _project_observations,
)
from .state import AgentState, AnalysisPlan, TodoItem, clip_text

_PLANNER_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "planner.md"
_AGENT_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "system.md"
)
_REASSESS_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "reassess.md"

# Planner 会话原文视野：短会话时滚动摘要为空，必须补上最近轮次以免误规划
MAX_PLANNER_HISTORY_MESSAGES = 8          # 注入的最近对话条数
MAX_PLANNER_HISTORY_ITEM_CHARS = 8000    # 单条对话字符上限
MAX_PLANNER_HISTORY_CHARS = 20_000         # 最近对话总字符预算

# Planner 可见的工作轨迹上限（每次规划只调用一次，可比内层 ReAct 宽松）
PLANNER_TRAJECTORY_PATHS = 80
PLANNER_TRAJECTORY_DECISIONS = 30
PLANNER_TRAJECTORY_NOTES = 25
PLANNER_TRAJECTORY_FAILURES = 15
PLANNER_TRAJECTORY_ITEM_CHARS = 600

MAX_RECOVERY_RESULT_CHARS = 1_000         # 重规划时单条已完成结果的字符上限


class PlannerError(RuntimeError):
    """Planner 无法生成有效执行计划。"""


class PlanDecision(BaseModel):
    """只选择继续、改写未完成任务或提前汇总，不直接生成工具事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["continue", "revise", "finish_early"]
    reasoning_summary: str = Field(min_length=1)
    todos: list[TodoItem] = Field(default_factory=list, max_length=12)

    @field_validator("reasoning_summary")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        """公开理由不能为空白。"""
        if not value.strip():
            raise ValueError("复审理由不能为空")
        return value.strip()

    @model_validator(mode="after")
    def validate_revision(self) -> "PlanDecision":
        """只有改写动作可以携带待执行任务，禁止预填执行成功。"""
        if (self.action == "revise") != bool(self.todos):
            raise ValueError("revise 必须提供剩余任务，其他动作不得提供 todos")
        if any(todo.status != "pending" or todo.result_summary is not None for todo in self.todos):
            raise ValueError("新任务必须为 pending，不能预填结果")
        return self


def merge_revised_plan(
    previous: AnalysisPlan, todos: Sequence[TodoItem | dict[str, Any]], revision: int,
) -> AnalysisPlan:
    """保留已结束结果，校验并命名剩余计划；未知或不可满足的依赖直接拒绝。

    Args:
        previous: 当前执行计划。
        todos: 复审提出的全部剩余任务，可引用已完成任务或本次新任务。
        revision: 本次自适应改写序号，用于分配不冲突的任务标识。

    Returns:
        已验证标识唯一、依赖可满足且无环的新计划。
    """
    proposed = [TodoItem.model_validate(todo) for todo in todos]
    preserved = [todo.model_copy(deep=True) for todo in previous.todos
                 if todo.status in {"completed", "failed", "skipped"}]
    preserved_ids = {todo.id for todo in preserved}
    completed_ids = {todo.id for todo in preserved if todo.status == "completed"}
    new_ids = [todo.id for todo in proposed]
    if len(set(new_ids)) != len(new_ids) or set(new_ids) & preserved_ids:
        raise ValueError("剩余任务标识重复或与已保留任务冲突")
    mapping = {todo.id: f"adaptive_{revision}_{todo.id}" for todo in proposed}
    if set(mapping.values()) & preserved_ids:
        raise ValueError("重映射后的标识与历史任务冲突")
    revised = []
    for todo in proposed:
        if todo.status != "pending" or todo.result_summary is not None:
            raise ValueError("剩余任务不能预填状态或结果")
        dependencies = []
        for required in todo.depends_on:
            if required in mapping:
                dependencies.append(mapping[required])
            elif required in completed_ids:
                dependencies.append(required)
            else:
                raise ValueError(f"剩余任务依赖未知或未完成的历史任务：{required}")
        revised.append(todo.model_copy(update={"id": mapping[todo.id], "depends_on": dependencies}, deep=True))
    return AnalysisPlan(
        objective=previous.objective, reasoning_summary=previous.reasoning_summary,
        todos=[*preserved, *revised],
    )


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
        progress_callback: ProgressCallback | None = None,
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
        self._progress_callback = progress_callback
        self._progress_sequence = 0

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
            self._progress_sequence += 1
            _, raw = invoke_llm_with_public_progress(
                self._llm,
                messages,
                summary_key="reasoning_summary",
                stream_id=f"planner-{self._progress_sequence}",
                initial_text="正在理解任务并制定执行计划…",
                progress_callback=self._progress_callback,
                cancel_check=self._cancel_check,
            )
            try:
                decision = _parse_planner_decision(raw)
                if isinstance(decision, Finish):
                    if not allow_direct:
                        raise PlannerError("当前请求必须返回可执行计划，不能直接回答")
                    return decision
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
            skill_context=state.skill_context,
            skill_observations=list(state.skill_observations),
            recent_observations=list(state.recent_observations),
            analysis_plan=plan,
            expert_context=state.expert_context,
        )
        recovery = self.create_plan(recovery_state, allow_direct=False)
        if isinstance(recovery, Finish):  # pragma: no cover - allow_direct=False 已拦截
            raise PlannerError("失败恢复必须返回可执行计划")
        return recovery

    def reassess(self, state: AgentState, plan: AnalysisPlan, trigger: str) -> PlanDecision:
        """基于实际观察复审剩余计划，有限纠错后仍失败时交给调度器保留原计划。

        Args:
            state: 含真实近期观察、轨迹与产物的运行状态。
            plan: 当前完整计划，已完成结果只能复用。
            trigger: 调度器提供的复审时机，不替模型决定分析路径。

        Returns:
            已校验的继续、改写或提前汇总决策。
        """
        context = self._build_context(state, plan)
        context["review_trigger"] = trigger
        context["review_budget_used"] = state.plan_reviews
        try:
            template = _REASSESS_PROMPT_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise PlannerError("无法读取计划复审提示词") from exc
        messages = [
            ("system", f"{_load_agent_system_prompt()}{_expert_directive(state.expert_context)}"),
            ("user", template.replace("{{CONTEXT_JSON}}", json.dumps(context, ensure_ascii=False, indent=2))),
        ]
        for attempt in range(self._retries + 1):
            raise_if_cancelled(self._cancel_check)
            self._progress_sequence += 1
            _, raw = invoke_llm_with_public_progress(
                self._llm, messages, summary_key="reasoning_summary",
                stream_id=f"planner-{self._progress_sequence}", initial_text="正在根据工具证据复审剩余计划…",
                progress_callback=self._progress_callback, cancel_check=self._cancel_check,
            )
            try:
                text = re.sub(r"^\s*<think>.*?</think>\s*", "", raw, count=1, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
                decision = PlanDecision.model_validate_json(text)
                if decision.action == "revise":
                    if len(decision.todos) > self._max_todos:
                        raise ValueError("剩余任务超过规划上限")
                    merge_revised_plan(plan, decision.todos, state.adaptive_replans + 1)
                return decision
            except (ValidationError, ValueError) as exc:
                if attempt >= self._retries:
                    raise PlannerError("无法生成有效计划复审决策") from exc
                messages.extend([("assistant", raw), ("user", f"复审决策无效：{exc}。请修正后只输出合法 JSON。")])
        raise PlannerError("计划复审未产生决策")

    def build_messages(self, state: AgentState) -> list[tuple[str, str]]:
        """构造负责请求分流与规划、不执行工具的 Planner 消息。"""
        context = self._build_context(state)
        prompt = _render_planner_prompt(context, self._max_todos)
        expert_directive = _expert_directive(state.expert_context)
        return [
            ("system", f"{_load_agent_system_prompt()}{expert_directive}"),
            ("user", prompt),
        ]

    def _build_context(self, state: AgentState, plan: AnalysisPlan | None = None) -> dict[str, Any]:
        """初始规划、失败恢复与成功路径复审共用同一证据投影。"""
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
        current_plan = plan or state.analysis_plan
        plan_view = current_plan.model_dump(mode="json") if current_plan else None
        if plan_view:
            for item in plan_view["todos"]:
                if item["result_summary"] is not None:
                    item["result_summary"] = clip_text(item["result_summary"], MAX_RECOVERY_RESULT_CHARS)
        context = {
            "user_query": state.user_query,
            "recent_observations": _project_observations(state.recent_observations or state.observations),
            "current_todo_id": state.current_todo_id,
            "current_plan": plan_view,
            "completed_todos": [
                {"id": todo.id, "objective": todo.objective,
                 "result_summary": clip_text(todo.result_summary or "", MAX_RECOVERY_RESULT_CHARS)}
                for todo in current_plan.todos if todo.status == "completed"
            ] if current_plan else [],
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
            "active_expert": _active_expert_view(state.expert_context),
            "active_skills": [
                {
                    "skill_id": active.metadata.skill_id,
                    "description": active.metadata.description,
                    "instructions": active.instructions,
                    "references": [
                        {"id": item.resource_id, "description": item.description}
                        for item in active.metadata.references
                    ],
                    "assets": [
                        {"id": item.resource_id, "description": item.description}
                        for item in active.metadata.assets
                    ],
                    "scripts": [
                        {
                            "id": item.entrypoint_id,
                            "description": item.description,
                            "runtime": item.runtime,
                        }
                        for item in active.metadata.scripts
                    ],
                }
                for active in state.skill_context.active_skills
            ],
        }
        return context


def _expert_directive(context: ExpertContext) -> str:
    """生成 Planner 使用的系统级专家约束。"""
    expert = context.expert
    if expert is None:
        return ""
    return (
        "\n\n当前运行启用专家角色；其方法和交付要求必须进入计划，"
        "但不得覆盖平台安全与事实约束。\n"
        f"专家：{expert.name}（{expert.expert_id}，版本 {expert.version}）\n"
        f"职责与方法：{expert.instructions}\n"
        "业务 SOP 是可解释的计划指引，不是预设建模方法；"
        "不适用的阶段可以跳过，但须在计划中说明。\n"
        f"业务 SOP：{json.dumps([item.model_dump(mode='json') for item in expert.sop_stages], ensure_ascii=False)}\n"
        f"交付要求：{json.dumps(expert.output_requirements, ensure_ascii=False)}\n"
        f"验证要求：{json.dumps(expert.validation_requirements, ensure_ascii=False)}"
    )


def _active_expert_view(context: ExpertContext) -> dict[str, Any] | None:
    """生成 Planner 上下文中的专家版本快照。"""
    expert = context.expert
    if expert is None:
        return None
    return {
        "expert_id": expert.expert_id,
        "name": expert.name,
        "version": expert.version,
        "bundle_hash": expert.bundle_hash,
        "instructions": expert.instructions,
        "skill_bindings": [
            item.model_dump(mode="json") for item in expert.skill_bindings
        ],
        "sop_stages": [item.model_dump(mode="json") for item in expert.sop_stages],
        "output_requirements": list(expert.output_requirements),
        "validation_requirements": list(expert.validation_requirements),
    }


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


def _load_agent_system_prompt() -> str:
    """加载 Planner 与执行 Agent 共用的系统提示词。"""
    try:
        return _AGENT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PlannerError(f"无法读取通用系统提示词: {exc}") from exc


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
