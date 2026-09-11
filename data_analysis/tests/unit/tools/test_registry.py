from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import pandas as pd
import pytest
from pydantic import JsonValue

from data_analysis.agent import AgentLoop, AgentState, ToolSpec as AgentToolSpec
from data_analysis.tools import (
    DuplicateToolError,
    RegisteredTool,
    ToolArgumentsError,
    ToolNotFoundError,
    ToolRegistry,
    ToolSpec,
    create_data_profile_registry,
)


def return_ok(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, JsonValue],
) -> dict[str, bool]:
    return {"ok": not arguments and not context}


def test_registry_preserves_registration_order_and_hides_handlers() -> None:
    registry = ToolRegistry()
    first = registry.register(ToolSpec("first", "第一个工具"), return_ok)
    second = registry.register(ToolSpec("second", "第二个工具"), return_ok)

    assert len(registry) == 2
    assert " first " in registry
    assert registry.specs() == (first.spec, second.spec)
    assert all(not hasattr(spec, "handler") for spec in registry.specs())


def test_registry_resolves_registered_handler() -> None:
    registry = ToolRegistry(
        [RegisteredTool(ToolSpec("check", "检查数据"), return_ok)]
    )

    tool = registry.resolve(" check ")

    assert tool.invoke({}) == {"ok": True}


def test_registry_rejects_duplicate_name() -> None:
    registry = ToolRegistry()
    registry.register(ToolSpec("profile_data", "生成画像"), return_ok)

    with pytest.raises(DuplicateToolError, match="profile_data"):
        registry.register(ToolSpec("profile_data", "再次生成画像"), return_ok)


def test_registry_rejects_unknown_name() -> None:
    with pytest.raises(ToolNotFoundError, match="missing"):
        ToolRegistry().resolve("missing")


@pytest.mark.parametrize(
    ("name", "description"),
    [("", "描述"), ("   ", "描述"), ("tool", ""), ("tool", "   ")],
)
def test_tool_spec_rejects_blank_text(name: str, description: str) -> None:
    with pytest.raises(ValueError):
        ToolSpec(name, description)


def test_tool_spec_rejects_non_json_parameter_schema() -> None:
    with pytest.raises(ValueError, match="合法 JSON"):
        ToolSpec("tool", "描述", {"invalid": {1, 2}})


def test_tool_spec_freezes_parameter_schema() -> None:
    spec = ToolSpec("tool", "描述", {"type": "object"})

    assert isinstance(spec.parameters, MappingProxyType)
    with pytest.raises(TypeError):
        spec.parameters["type"] = "array"


def test_registered_tool_rejects_non_callable_handler() -> None:
    with pytest.raises(TypeError, match="必须可调用"):
        RegisteredTool(ToolSpec("tool", "描述"), None)


def test_registered_tool_rejects_non_top_level_entrypoint() -> None:
    def nested_handler(arguments, context):
        return None

    tool = RegisteredTool(ToolSpec("nested", "嵌套处理器"), nested_handler)

    with pytest.raises(ValueError, match="顶层函数"):
        _ = tool.entrypoint


def test_profile_registry_binds_authorized_files(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1, 2, 3]}).to_csv(path, index=False)
    registry = create_data_profile_registry([path])

    tool = registry.resolve("profile_data")
    result = tool.invoke({})

    assert len(registry) == 1
    assert registry.specs()[0].parameters == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert result["summary"]["row_count"] == 3


def test_profile_registry_rejects_llm_arguments(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1]}).to_csv(path, index=False)
    tool = create_data_profile_registry([path]).resolve("profile_data")

    with pytest.raises(ToolArgumentsError, match="不接受调用参数"):
        tool.invoke({"data_paths": ["other.csv"]})


def test_profile_registry_requires_authorized_files() -> None:
    with pytest.raises(ValueError, match="授权文件"):
        create_data_profile_registry([])


def test_agent_loop_consumes_registry_specs() -> None:
    class FakeLlm:
        def invoke(self, messages: list[tuple[str, str]]) -> str:
            return (
                '{"type":"action","tool_name":"profile_data",'
                '"arguments":{}}'
            )

    registry = ToolRegistry()
    registry.register(ToolSpec("profile_data", "生成数据画像"), return_ok)
    loop = AgentLoop(FakeLlm(), registry.specs(), "依据工具结果回答")

    decision = loop.step(AgentState(user_query="分析数据"))

    assert decision.tool_name == "profile_data"
    assert AgentToolSpec is ToolSpec
