"""结构化工作轨迹的提取与合并测试。"""

from __future__ import annotations

from data_analysis.agent.state import (
    AnalysisPlan,
    WorkTrajectory,
    collect_work_trajectory_from_session,
)


def test_work_trajectory_observe_tool_tracks_read_and_write() -> None:
    trajectory = WorkTrajectory()
    trajectory.observe_tool(
        "read_workspace_file",
        arguments={"relative_path": "docs/spec.md"},
    )
    trajectory.observe_tool(
        "write_artifact",
        arguments={"relative_path": "outputs/report.md"},
        result={"path": "outputs/report.md"},
    )
    trajectory.observe_tool(
        "read_attachment_file",
        arguments={"file_path": "C:/tmp/input.csv"},
        success=False,
    )

    assert trajectory.files_read == ["docs/spec.md"]
    assert trajectory.files_modified == ["outputs/report.md"]


def test_work_trajectory_merge_and_decisions() -> None:
    trajectory = WorkTrajectory()
    trajectory.merge(
        {
            "files_read": ["a.md"],
            "key_decisions": ["采用含税口径"],
        }
    )
    trajectory.add_read("a.md")
    trajectory.add_decision("  采用含税口径  ")
    trajectory.absorb_completed_todos(
        AnalysisPlan.model_validate({
            "objective": "分析",
            "todos": [
                {
                    "id": "todo_1",
                    "objective": "核对数据",
                    "expected_outputs": ["概况"],
                    "status": "completed",
                    "result_summary": "数据完整",
                },
                {
                    "id": "todo_2",
                    "objective": "未完成",
                    "expected_outputs": ["结论"],
                    "status": "pending",
                    "result_summary": None,
                },
            ],
        })
    )

    assert trajectory.files_read == ["a.md"]
    assert trajectory.key_decisions == ["采用含税口径", "todo_1: 数据完整"]


def test_collect_work_trajectory_from_session_merges_trace_and_metadata() -> None:
    entries = [
        {
            "role": "assistant",
            "content": "上一轮完成",
            "metadata": {
                "work_trajectory": {
                    "files_read": ["old.md"],
                    "key_decisions": ["旧结论"],
                },
                "agent_trace": [
                    {
                        "type": "agent_action",
                        "tool_name": "read_attachment_file",
                        "arguments": {"file_path": "C:/tmp/input.csv"},
                    },
                    {
                        "type": "agent_action",
                        "tool_name": "write_artifact",
                        "arguments": {"relative_path": "outputs/chart.png"},
                    },
                ],
            },
        }
    ]

    trajectory = collect_work_trajectory_from_session(entries)

    assert "old.md" in trajectory.files_read
    assert "C:/tmp/input.csv" in trajectory.files_read
    assert trajectory.files_modified == ["outputs/chart.png"]
    assert trajectory.key_decisions == ["旧结论"]
