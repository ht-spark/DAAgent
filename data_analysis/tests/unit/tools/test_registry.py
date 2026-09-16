from __future__ import annotations

import sys
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
    create_agent_tool_registry,
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

    assert len(registry) == 2
    assert registry.specs()[0].parameters == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert result["summary"]["row_count"] == 3


def test_registry_runs_real_row_inspection(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [10, 20, 30]}).to_csv(path, index=False)
    registry = create_data_profile_registry([path])

    result = registry.resolve("inspect_data_rows").invoke(
        {"start_row": 2, "row_count": 2}
    )

    assert [spec.name for spec in registry.specs()] == [
        "profile_data",
        "inspect_data_rows",
    ]
    assert result["rows"] == [
        {"row_number": 2, "values": [20]},
        {"row_number": 3, "values": [30]},
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"row_count": True},
        {"row_count": 201},
        {"row_count": 1, "unknown": 1},
    ],
)
def test_inspect_registry_rejects_malformed_arguments(tmp_path, arguments) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1]}).to_csv(path, index=False)
    tool = create_data_profile_registry([path]).resolve("inspect_data_rows")

    with pytest.raises(ToolArgumentsError):
        tool.invoke(arguments)


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


def test_agent_registry_always_exposes_dynamic_python(tmp_path) -> None:
    registry = create_agent_tool_registry([], tmp_path / "output")

    assert [spec.name for spec in registry.specs()] == [
        "search_web",
        "write_artifact",
        "execute_python",
        "inspect_artifact",
    ]
    result = registry.resolve("execute_python").invoke(
        {"code": "print('ok')\nresult = {'value': 2 + 3}"}
    )

    assert result["stdout"] == "ok\n"
    assert result["result"] == {"value": 5}


def test_agent_registry_binds_selected_knowledge_bases(monkeypatch, tmp_path) -> None:
    from data_analysis.tools import knowledge_tools
    from KnowledgeBase.main import RetrievedChunk

    captured: dict[str, object] = {}

    def retrieve(query: str, **kwargs):
        captured.update(query=query, **kwargs)
        return [
            RetrievedChunk(
                point_id=1,
                text="指标正文",
                context="指标口径上下文",
                source_file="metrics.md",
                source_path="",
                document_id="doc-1",
                dense_rank=1,
                bm25_rank=1,
                rrf_score=0.03,
                knowledge_base_id="metrics-kb",
            )
        ]

    monkeypatch.setattr(knowledge_tools, "retrieve_knowledge", retrieve)
    registry = create_agent_tool_registry(
        [],
        tmp_path / "output",
        knowledge_base_ids=["metrics-kb", "history-kb", "metrics-kb"],
    )

    tool = registry.resolve("search_knowledge_base")
    result = tool.invoke({"query": "  复购率口径  "})

    assert tool.run_in_process is True
    assert captured == {
        "query": "复购率口径",
        "knowledge_base_ids": ["metrics-kb", "history-kb"],
    }
    assert result["matches"] == [
        {
            "content": "指标口径上下文",
            "source_file": "metrics.md",
            "document_id": "doc-1",
            "knowledge_base_id": "metrics-kb",
            "relevance_score": 0.03,
        }
    ]


def test_agent_registry_exposes_web_search(monkeypatch, tmp_path) -> None:
    from data_analysis.tools import web_search

    captured: dict[str, str | None] = {}

    class FakeSearchTool:
        def __init__(self, api_key: str | None = None):
            captured["api_key"] = api_key

        def search(self, query: str):
            return {
                "query": query,
                "answer": "最新公开信息",
                "results": [
                    {
                        "title": "来源标题",
                        "url": "https://example.com/source",
                        "content": "来源摘要",
                        "score": 0.9,
                    }
                ],
            }

    monkeypatch.setattr(web_search, "TavilySearchTool", FakeSearchTool)
    tool = create_agent_tool_registry(
        [],
        tmp_path / "output",
        web_search_api_key="saved-tavily-key",
    ).resolve("search_web")

    result = tool.invoke({"query": "  最近发布的数据  "})

    assert tool.run_in_process is True
    assert captured["api_key"] == "saved-tavily-key"
    assert result["query"] == "最近发布的数据"
    assert result["results"][0]["url"] == "https://example.com/source"


@pytest.mark.parametrize(
    "arguments",
    [{}, {"query": ""}, {"query": "问题", "max_results": 10}],
)
def test_web_search_rejects_invalid_arguments(tmp_path, arguments) -> None:
    tool = create_agent_tool_registry([], tmp_path / "output").resolve("search_web")

    with pytest.raises(ToolArgumentsError):
        tool.invoke(arguments)


@pytest.mark.parametrize(
    "arguments",
    [{}, {"query": ""}, {"query": "问题", "knowledge_base_ids": ["other"]}],
)
def test_knowledge_tool_rejects_route_override_and_invalid_query(
    tmp_path,
    arguments,
) -> None:
    tool = create_agent_tool_registry(
        [],
        tmp_path / "output",
        knowledge_base_ids=["metrics-kb"],
    ).resolve("search_knowledge_base")

    with pytest.raises(ToolArgumentsError):
        tool.invoke(arguments)


def test_agent_registry_combines_data_and_code_tools(tmp_path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame({"销量": [1]}).to_csv(path, index=False)

    registry = create_agent_tool_registry([path], tmp_path / "output")

    assert [spec.name for spec in registry.specs()] == [
        "profile_data",
        "inspect_data_rows",
        "search_web",
        "write_artifact",
        "execute_python",
        "inspect_artifact",
    ]


def test_agent_registry_keeps_documents_out_of_profile_tool(tmp_path) -> None:
    csv_path = tmp_path / "data.csv"
    pdf_path = tmp_path / "report.pdf"
    pd.DataFrame({"销量": [1, 2]}).to_csv(csv_path, index=False)
    pdf_path.write_bytes(b"pdf-content")
    registry = create_agent_tool_registry(
        [csv_path, pdf_path],
        tmp_path / "output",
    )

    profile = registry.resolve("profile_data").invoke({})
    code_result = registry.resolve("execute_python").invoke(
        {
            "code": (
                "from pathlib import Path\n"
                "result = sorted(Path(path).suffix for path in DATA_FILES)"
            )
        }
    )

    assert profile["summary"]["file_count"] == 1
    assert profile["files"][0]["file_name"] == "data.csv"
    assert code_result["result"] == [".csv", ".pdf"]


@pytest.mark.parametrize("arguments", [{}, {"code": ""}, {"code": "x", "extra": 1}])
def test_agent_registry_rejects_invalid_python_arguments(
    tmp_path,
    arguments,
) -> None:
    tool = create_agent_tool_registry([], tmp_path / "output").resolve(
        "execute_python"
    )

    with pytest.raises(ToolArgumentsError):
        tool.invoke(arguments)


def test_agent_registry_accepts_user_selected_python_environment(tmp_path) -> None:
    tool = create_agent_tool_registry(
        [],
        tmp_path / "output",
        workspace_root=tmp_path,
    ).resolve("execute_python")

    result = tool.invoke(
        {
            "code": "import sys\nresult = {'executable': sys.executable}",
            "python_environment": sys.executable,
        }
    )

    assert result["runtime"]["source"] == "requested"
    assert result["runtime"]["name"]


def test_agent_registry_writes_artifact(tmp_path) -> None:
    output_dir = tmp_path / "output"
    tool = create_agent_tool_registry([], output_dir).resolve("write_artifact")

    result = tool.invoke(
        {
            "relative_path": "scripts/analysis.py",
            "content": "result = 42\n",
        }
    )

    assert result["path"] == "scripts/analysis.py"
    assert (output_dir / "scripts" / "analysis.py").read_text(
        encoding="utf-8"
    ) == "result = 42\n"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"relative_path": "result.md"},
        {"relative_path": "result.md", "content": "x", "overwrite": 1},
        {"relative_path": "result.md", "content": "x", "extra": True},
    ],
)
def test_agent_registry_rejects_invalid_artifact_arguments(
    tmp_path,
    arguments,
) -> None:
    tool = create_agent_tool_registry([], tmp_path / "output").resolve(
        "write_artifact"
    )

    with pytest.raises(ToolArgumentsError):
        tool.invoke(arguments)
