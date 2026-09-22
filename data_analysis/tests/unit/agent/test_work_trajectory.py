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
        result={"summary": "接口约定为 POST /v1/analyze"},
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
    assert trajectory.notes == [
        "read_workspace_file docs/spec.md: 接口约定为 POST /v1/analyze"
    ]


def test_work_trajectory_notes_dedupe_and_cap() -> None:
    trajectory = WorkTrajectory()
    trajectory.observe_tool(
        "profile_data",
        result={"summary": {"rows": 150, "missing_rate": 0.01}},
    )
    trajectory.observe_tool(
        "profile_data",
        result={"summary": {"rows": 150, "missing_rate": 0.01}},
    )
    trajectory.add_note("X" * 500)

    assert len(trajectory.notes) == 2
    assert trajectory.notes[0].startswith("profile_data:")
    assert "rows=150" in trajectory.notes[0]
    assert len(trajectory.notes[1]) <= 240


def test_work_trajectory_merge_and_decisions() -> None:
    trajectory = WorkTrajectory()
    trajectory.merge(
        {
            "files_read": ["a.md"],
            "key_decisions": ["采用含税口径"],
            "notes": ["profile_data: rows=100"],
        }
    )
    trajectory.add_read("a.md")
    trajectory.add_decision("  采用含税口径  ")
    trajectory.add_note("profile_data: rows=100")
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
    assert trajectory.notes == ["profile_data: rows=100"]


def test_collect_work_trajectory_from_session_merges_trace_and_metadata() -> None:
    entries = [
        {
            "role": "assistant",
            "content": "上一轮完成",
            "metadata": {
                "work_trajectory": {
                    "files_read": ["old.md"],
                    "key_decisions": ["旧结论"],
                    "notes": ["inspect_data_rows: 前15行含频段/时间/次数"],
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
                    {
                        "type": "agent_observation",
                        "tool_name": "profile_data",
                        "success": True,
                        "summary": {"rows": 150},
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
    assert "inspect_data_rows: 前15行含频段/时间/次数" in trajectory.notes
    assert any(
        note.startswith("profile_data:") and "rows=150" in note
        for note in trajectory.notes
    )


def test_artifact_descriptions_persist_and_enrich() -> None:
    from data_analysis.agent.state import ArtifactReference

    trajectory = WorkTrajectory()
    trajectory.describe_artifact("outputs/report.md", "复购分析 Markdown 报告")
    trajectory.describe_artifact("./outputs\\chart.png", "频段分布柱状图")

    assert trajectory.description_for("outputs/report.md") == "复购分析 Markdown 报告"
    assert trajectory.description_for("outputs/chart.png") == "频段分布柱状图"

    merged = WorkTrajectory()
    merged.merge(trajectory.model_dump())
    assert merged.description_for("outputs/report.md") == "复购分析 Markdown 报告"

    bare = ArtifactReference(
        relative_path="outputs/report.md",
        absolute_path="C:/tmp/outputs/report.md",
        name="report.md",
        size_bytes=10,
        media_type="text/markdown",
        source_tool="existing_session",
    )
    enriched = trajectory.enrich_artifacts([bare])
    assert enriched[0].description == "复购分析 Markdown 报告"


def test_write_artifact_records_description_from_content() -> None:
    trajectory = WorkTrajectory()
    trajectory.observe_tool(
        "write_artifact",
        arguments={
            "relative_path": "outputs/report.md",
            "content": "# 复购分析建议\n\n正文……",
        },
        result={"path": "outputs/report.md"},
    )
    assert "复购分析建议" in (trajectory.description_for("outputs/report.md") or "")
