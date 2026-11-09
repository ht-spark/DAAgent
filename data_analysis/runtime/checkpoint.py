"""以已落盘的决策与观察重建图状态，恢复时不重复调用已完成工具。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, Protocol

from pydantic import BaseModel

from ..agent.loop import Action, ActionBatch, Finish
from ..agent.planner import PlanDecision
from ..agent.state import AnalysisPlan, Observation
from ..agent.verifier import Verification
from ..skills.models import SkillAction, SkillObservation


class CheckpointStore(Protocol):
    """由宿主实现的持久化接口，不向运行时授予数据库或目录权限。"""

    def steps(self) -> list[dict[str, Any]]: ...
    def begin(self, kind: str, arguments: Any) -> int: ...
    def complete(self, sequence: int, payload: Any) -> None: ...


_MODELS = {model.__name__: model for model in (
    Action, ActionBatch, Finish, AnalysisPlan, PlanDecision, Observation, SkillAction, SkillObservation, Verification,
)}


def _encode(value: Any) -> Any:
    """只存可验证的数据模型和 JSON 值，不序列化模型客户端或执行对象。"""
    if isinstance(value, BaseModel):
        return {"model": type(value).__name__, "value": value.model_dump(mode="json")}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    """按显式白名单恢复类型，拒绝任意对象反序列化。"""
    if isinstance(value, dict) and "model" in value:
        return _MODELS[value["model"]].model_validate(value["value"])
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


class CheckpointJournal:
    """通过重演已保存返回值恢复共享预算、计划、重复保护和失败计数。"""

    def __init__(self, store: CheckpointStore) -> None:
        """读取有序日志；游标仅存在于当前恢复进程中。"""
        self.store = store
        self.entries = store.steps()
        self.cursor = 0
        self.position = 0
        self.live = not bool(self.entries)

    @property
    def replaying(self) -> bool:
        """历史结果尚未消费完时，不重复下发图事件或请求已处理的审批。"""
        return not self.live and self.cursor < len(self.entries)

    def wrap_trace(self, trace: Any) -> Any:
        """重演期间不伪造新的规划、任务或工具耗时。"""
        journal = self

        class TraceProxy:
            def begin(self, *args: Any, **kwargs: Any) -> Any:
                return trace.begin(*args, **kwargs) if journal.live else None

            def end(self, span: Any, **kwargs: Any) -> None:
                if span is not None:
                    trace.end(span, **kwargs)

            @contextmanager
            def span(self, *args: Any, **kwargs: Any):
                if journal.live:
                    with trace.span(*args, **kwargs) as span:
                        yield span
                else:
                    yield None

        return TraceProxy()

    def call(self, kind: str, arguments: Any, invoke: Callable[[], Any]) -> Any:
        """先写执行意图，再执行并保存返回值；未知工具结果不会自动重跑。

        Args:
            kind: 稳定的调用种类，用于检测运行图版本变化。
            arguments: 可序列化的调用参数，不含模型配置或凭据。
            invoke: 仅在尚未执行的边界调用的真实操作。

        Returns:
            已保存的返回值，或本次真实调用的返回值。
        """
        if self.cursor < len(self.entries):
            entry = self.entries[self.cursor]
            self.cursor += 1
            self.position = entry["seq"]
            if entry["kind"] != kind or entry["arguments"] != arguments:
                raise RuntimeError("检查点执行顺序不兼容，已停止恢复，未执行新工具")
            if entry["completed"]:
                value = entry["payload"]
                if isinstance(value, dict) and "exception" in value:
                    raise RuntimeError(value["exception"])
                return _decode(value)
            self.live = True
            if kind in {"harness.execute", "harness.execute_many", "skill.execute"}:
                message = "服务中断时该操作已开始，但结果尚未落盘，外部影响未知。不要直接重跑；请先检查真实文件或状态，再决定后续动作。"
                if kind == "skill.execute":
                    action = SkillAction.model_validate(arguments)
                    result = SkillObservation(operation=action.operation, skill_id=action.skill_id,
                        resource_id=action.resource_id, entrypoint_id=action.entrypoint_id, success=False, error=message)
                else:
                    actions = arguments if kind.endswith("execute_many") else [arguments]
                    results = [Observation(tool_name=item["tool_name"], call_id=item.get("call_id"),
                                           success=False, error=message) for item in actions]
                    result = results if kind.endswith("execute_many") else results[0]
                self.store.complete(entry["seq"], _encode(result))
                return result
            sequence = entry["seq"]
        else:
            self.live = True
            sequence = self.store.begin(kind, arguments)
        self.position = sequence
        try:
            result = invoke()
        except Exception as exc:
            from ..cancellation import TaskCancelledError
            if not isinstance(exc, TaskCancelledError):
                self.store.complete(sequence, {"exception": type(exc).__name__})
            raise
        self.store.complete(sequence, _encode(result))
        return result

    def wrap(self, target: Any, namespace: str, methods: tuple[str, ...]) -> Any:
        """代理指定方法，其他属性保持原对象行为。

        Args:
            target: 已由宿主重新授权的组件。
            namespace: 日志内的组件名称。
            methods: 需要保存返回值的方法白名单。

        Returns:
            透明转发其余属性的代理。
        """
        journal = self

        class Proxy:
            def __getattr__(self, name: str) -> Any:
                member = getattr(target, name)
                if name not in methods:
                    return member

                def invoke(*args: Any, **kwargs: Any) -> Any:
                    if namespace == "harness":
                        # 工具输入只保存动作数据，不把带类型标记的传输封套作为实际参数。
                        if name == "rejected":
                            data = [_encode(arg) for arg in args]
                        else:
                            data = [item.model_dump(mode="json") for item in args[0]] if isinstance(args[0], list) else args[0].model_dump(mode="json")
                    elif namespace == "skill":
                        data = args[0].model_dump(mode="json")
                    else:
                        data = {"kwargs": kwargs}
                    return journal.call(f"{namespace}.{name}", data, lambda: member(*args, **kwargs))
                return invoke

        return Proxy()
