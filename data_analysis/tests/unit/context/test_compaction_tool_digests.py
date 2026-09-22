"""滚动记忆压缩纳入工具观察的测试。"""

from __future__ import annotations

from data_analysis.context.rolling_memory import (
    _collect_tool_digests,
    _generate_summary,
)


class ScriptedLlm:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[list[tuple[str, str]]] = []

    def invoke(self, messages: list[tuple[str, str]]) -> str:
        self.messages.append(messages)
        return self.response


def test_collect_tool_digests_from_trace_and_notes() -> None:
    entries = [
        {
            "role": "user",
            "content": "分析这份数据",
            "seq": 1,
            "metadata": {},
        },
        {
            "role": "assistant",
            "content": "已完成画像。",
            "seq": 2,
            "metadata": {
                "work_trajectory": {"notes": ["inspect_data_rows: 150行"]},
                "agent_trace": [
                    {
                        "type": "agent_observation",
                        "tool_name": "profile_data",
                        "success": True,
                        "summary": {"rows": 150, "missing": 0},
                    },
                    {
                        "type": "agent_observation",
                        "tool_name": "execute_python",
                        "success": False,
                        "summary": {"err": "boom"},
                    },
                ],
            },
        },
    ]

    digests = _collect_tool_digests(entries)

    assert "inspect_data_rows: 150行" in digests
    assert "profile_data: rows=150, missing=0" in digests
    assert "execute_python" not in digests


def test_generate_summary_prompt_includes_tool_observations() -> None:
    llm = ScriptedLlm("## Goal\n接手")
    messages = [
        {
            "role": "assistant",
            "content": "看过了",
            "seq": 2,
            "metadata": {
                "agent_trace": [
                    {
                        "type": "agent_observation",
                        "tool_name": "read_workspace_file",
                        "success": True,
                        "summary": "共 12 个字段，主键无缺失",
                    }
                ]
            },
        }
    ]

    _generate_summary(llm, "旧摘要", messages)

    prompt = llm.messages[0][1][1]
    assert "<tool_observations>" in prompt
    assert "read_workspace_file: 共 12 个字段，主键无缺失" in prompt
    assert "工具观察中的字段" in prompt
