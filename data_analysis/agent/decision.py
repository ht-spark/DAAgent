"""供应商原生工具绑定与全批次解析。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from .loop import Action, ActionBatch, DecisionParseError, Finish, parse_decision
from ..skills.models import SkillAction
from ..tools.registry import ToolSpec


class NativeToolClient:
    """直接复用注册表 schema；绑定只在本地完成，不额外探测供应商。"""

    def __init__(self, client: Any, tools: Sequence[ToolSpec]) -> None:
        """保存当前运行的客户端与工具快照。"""
        self._client = client
        self._tools = tools

    def bind(self) -> Any:
        """返回供应商的绑定客户端，保持模型层原有计量装饰。"""
        binder = getattr(self._client, "bind_tools", None)
        if not callable(binder):
            raise NotImplementedError("当前模型客户端不支持原生工具绑定")
        schemas = [{
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description + (
                    "\n运行约束：只读且可与独立查询并行。" if not tool.mutating and tool.parallel_safe
                    else "\n运行约束：必须按顺序执行。"
                ),
                "parameters": dict(tool.parameters) or {"type": "object", "properties": {}},
            },
        } for tool in self._tools]
        return binder(schemas)


def is_native_unsupported(error: Exception) -> bool:
    """仅将明确的不支持工具协议视为可回退；认证、限流和超时直接上抛。"""
    if isinstance(error, (AttributeError, NotImplementedError)):
        return True
    status = getattr(error, "status_code", None)
    text = str(error).lower()
    return status in {400, 404, 405, 422} and any(
        keyword in text for keyword in ("tool", "function", "parallel")
    )


def parse_native_response(
    response: Any, text: str, tool_names: frozenset[str],
) -> ActionBatch | SkillAction | Finish:
    """先校验全部调用，再交给执行层，任何畸形调用都不会被静默略过。"""
    metadata = getattr(response, "response_metadata", None)
    if isinstance(metadata, dict) and metadata.get("finish_reason") in {"length", "content_filter"}:
        raise DecisionParseError("原生回复被截断或过滤，不能执行其中的调用")
    if getattr(response, "invalid_tool_calls", None):
        raise DecisionParseError("模型返回了无法解析的工具参数")
    # 流式片段会被 SDK 宽松解析成部分 JSON；执行前必须确认原始参数已经完整闭合。
    for chunk in getattr(response, "tool_call_chunks", None) or []:
        try:
            raw_arguments = json.loads(chunk["args"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DecisionParseError("流式工具参数不完整") from exc
        if not isinstance(raw_arguments, dict):
            raise DecisionParseError("流式工具参数必须为 JSON 对象")
    calls = getattr(response, "tool_calls", None)
    if not calls:
        decision = parse_decision(text)
        if isinstance(decision, Action):
            if decision.tool_name not in tool_names:
                raise DecisionParseError("模型返回了未注册的工具名")
            return ActionBatch(actions=[decision])
        return decision
    if not isinstance(calls, list) or len(calls) > 16:
        raise DecisionParseError("tool_calls 必须为不超过 16 个调用的列表")
    actions = []
    seen_ids: set[str] = set()
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str) or call["name"] not in tool_names:
            raise DecisionParseError("模型返回了未注册的工具名")
        arguments = call.get("args")
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
            raise DecisionParseError("工具参数必须为 JSON 对象")
        try:
            json.dumps(arguments, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise DecisionParseError("工具参数不是有效 JSON") from exc
        call_id = call.get("id")
        if call_id is None:
            call_id = uuid.uuid4().hex
        if not isinstance(call_id, str) or not call_id.strip() or call_id in seen_ids:
            raise DecisionParseError("工具调用标识为空或重复")
        seen_ids.add(call_id)
        actions.append(Action(tool_name=call["name"], arguments=arguments, call_id=call_id))
    try:
        return ActionBatch(actions=actions)
    except ValidationError as exc:
        raise DecisionParseError("原生调用批次为空或超过 16 个工具") from exc
