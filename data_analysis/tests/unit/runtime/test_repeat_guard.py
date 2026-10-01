"""重复调用拦截器的契约测试。"""

from __future__ import annotations

from data_analysis.agent.loop import Action
from data_analysis.agent.state import Observation
from data_analysis.runtime import RepeatGuard


def _action(tool_name: str, **arguments: object) -> Action:
    return Action(tool_name=tool_name, arguments=dict(arguments))


def _success(tool_name: str, result: object = None) -> Observation:
    return Observation(tool_name=tool_name, success=True, result=result)


def test_second_identical_call_is_blocked_with_previous_digest() -> None:
    guard = RepeatGuard()
    action = _action("list_workspace_directory")

    assert guard.check(action) is None
    guard.record(action, _success("list_workspace_directory", {"entries": ["a"]}))

    notice = guard.check(action)

    assert notice is not None
    assert notice.count == 2
    assert "重复调用已拦截" in notice.message()
    assert "list_workspace_directory" not in notice.message()


def test_previous_summary_is_reused_for_blocked_repeat() -> None:
    guard = RepeatGuard()
    action = _action("profile_data")
    guard.record(action, _success("profile_data", {"summary": {"rows": 150}}))

    notice = guard.check(action)

    assert notice is not None
    assert notice.previous_digest == "rows=150"
    assert "rows=150" in notice.message()


def test_different_arguments_are_not_treated_as_repeat() -> None:
    guard = RepeatGuard()
    guard.record(
        _action("read_attachment_file", file_path="D:/a.md", offset=1),
        _success("read_attachment_file", {"content": "第一段"}),
    )

    assert guard.check(_action("read_attachment_file", file_path="D:/a.md", offset=2)) is None
    # 参数顺序不同也视为同一次调用
    assert guard.check(_action("read_attachment_file", offset=1, file_path="D:/a.md")) is not None


def test_mutating_call_allows_reading_the_same_target_again() -> None:
    guard = RepeatGuard()
    read = _action("read_workspace_file", relative_path="report.md")
    guard.record(read, _success("read_workspace_file", {"content": "旧内容"}))
    assert guard.check(read) is not None

    write = _action("edit_workspace_file", relative_path="report.md", content="新内容")
    guard.record(write, _success("edit_workspace_file", {"path": "report.md"}))

    # 文件已被修改，允许重新读取复核
    assert guard.check(read) is None


def test_failed_call_is_not_recorded_so_retry_is_allowed() -> None:
    guard = RepeatGuard()
    action = _action("execute_python", code="print(1)")

    guard.record(
        action,
        Observation(tool_name="execute_python", success=False, error="SyntaxError"),
    )

    assert guard.check(action) is None


def test_blocked_calls_are_summarized_for_recovery_planning() -> None:
    guard = RepeatGuard()
    action = _action("list_workspace_directory")
    guard.record(action, _success("list_workspace_directory", {"entries": []}))

    guard.check(action)
    guard.check(action)

    assert guard.blocked_count() == 2
    assert guard.blocked_summary() == "list_workspace_directory ×2"
