"""定义工具规格、可执行工具和注册表。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias

from pydantic import JsonValue

from .data_tools import profile_data

__all__ = [
    "DuplicateToolError",
    "RegisteredTool",
    "ToolArgumentsError",
    "ToolHandler",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolSpec",
    "create_data_profile_registry",
]

ToolHandler: TypeAlias = Callable[
    [Mapping[str, JsonValue], Mapping[str, JsonValue]],
    JsonValue,
]


class DuplicateToolError(ValueError):
    """注册表中已经存在同名工具。"""


class ToolNotFoundError(LookupError):
    """注册表中不存在指定工具。"""


class ToolArgumentsError(ValueError):
    """工具收到不符合约定的调用参数。"""


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
    context: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """拒绝不可调用的执行函数。"""
        if not callable(self.handler):
            raise TypeError("工具处理器必须可调用")
        context = dict(self.context)
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
    """创建绑定本次授权文件的数据画像工具注册表。

    Args:
        authorized_paths: 当前 Agent 状态允许读取的数据文件路径。

    Returns:
        仅包含 ``profile_data`` 的工具注册表。

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
                "对本次运行已授权的全部数据文件生成基础画像，包括表规模、字段类型、"
                "缺失情况、重复行和数值统计。文件已由运行时绑定，可直接调用，"
                "无需先查询文件列表或获取路径。"
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
    return registry


def _run_profile_data(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> JsonValue:
    """执行已绑定授权路径的数据画像工具。"""
    if arguments:
        raise ToolArgumentsError("profile_data 不接受调用参数")
    raw_paths = context.get("authorized_paths")
    if not isinstance(raw_paths, list) or not all(
        isinstance(path, str) and path for path in raw_paths
    ):
        raise ToolArgumentsError("profile_data 缺少有效的授权文件上下文")
    return profile_data(raw_paths, authorized_paths=raw_paths)
