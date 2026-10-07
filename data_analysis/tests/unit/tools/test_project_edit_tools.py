"""项目文本创建与编辑的权限边界测试。"""
from __future__ import annotations

import pytest

from data_analysis.tools.project_edit_tools import (
    edit_workspace_file,
    run_edit_workspace_file,
    write_workspace_file,
)
from data_analysis.tools.registry import create_agent_tool_registry
from data_analysis.tools.workspace_tools import WorkspacePathError


def test_project_change_tools_are_available_when_project_is_selected(tmp_path):
    output = tmp_path / "outputs"
    registry = create_agent_tool_registry(
        [], output, workspace_root=tmp_path,
    )
    names = {tool.name for tool in registry.specs()}
    assert {
        "write_workspace_file",
        "edit_workspace_file",
        "run_terminal_command",
    } <= names
    assert "run_project_command" not in names
    terminal = registry.resolve("run_terminal_command")
    assert terminal.context["terminal_shell"] == "powershell"
    assert "shell" not in terminal.spec.parameters["properties"]
    target = tmp_path / "existing.txt"
    target.write_text("before", encoding="utf-8")
    registry.resolve("edit_workspace_file").invoke({
        "relative_path": "existing.txt",
        "edits": [{"old_text": "before", "new_text": "after"}],
    })
    assert target.read_text(encoding="utf-8") == "after"
    assert not output.exists()
    unbound = create_agent_tool_registry([], output)
    assert not {
        "write_workspace_file",
        "edit_workspace_file",
        "run_terminal_command",
    } & {
        tool.name for tool in unbound.specs()
    }


@pytest.mark.parametrize(
    "edits",
    [
        [],
        [{"old_text": "before"}],
        [{"old_text": "before", "new_text": "after", "extra": True}],
        [{"old_text": 1, "new_text": "after"}],
    ],
)
def test_edit_tool_rejects_malformed_edits(tmp_path, edits):
    target = tmp_path / "existing.txt"
    target.write_text("before", encoding="utf-8")
    registry = create_agent_tool_registry([], tmp_path / "outputs", workspace_root=tmp_path)

    with pytest.raises(ValueError, match="参数无效"):
        registry.resolve("edit_workspace_file").invoke({
            "relative_path": "existing.txt",
            "edits": edits,
        })

    assert target.read_text(encoding="utf-8") == "before"


def test_write_and_edit_project_file_returns_diff(tmp_path):
    created = write_workspace_file(tmp_path, "src/calculator.py", "return a - b\n")
    overwritten = write_workspace_file(tmp_path, "src/calculator.py", "return a - b\n")
    edited = edit_workspace_file(
        tmp_path,
        "src/calculator.py",
        [{"old_text": "return a - b", "new_text": "return a + b"}],
    )
    assert created["created"] is True
    assert created["overwritten"] is False
    assert overwritten["created"] is False
    assert overwritten["overwritten"] is True
    assert edited["edit_count"] == 1
    assert "-1 return a - b" in edited["diff"]
    assert "+1 return a + b" in edited["diff"]
    assert "-return a - b" in edited["patch"]
    assert "+return a + b" in edited["patch"]
    assert edited["first_changed_line"] == 1
    assert (tmp_path / "src/calculator.py").read_text(encoding="utf-8") == "return a + b\n"


def test_edit_rejects_ambiguous_and_outside_paths(tmp_path):
    (tmp_path / "repeat.py").write_text("pass\npass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="匹配 2 次"):
        edit_workspace_file(
            tmp_path,
            "repeat.py",
            [{"old_text": "pass", "new_text": "return"}],
        )
    with pytest.raises(WorkspacePathError):
        write_workspace_file(tmp_path, "../escape.py", "x")
    with pytest.raises(WorkspacePathError):
        write_workspace_file(tmp_path, ".env", "SECRET=x")
    with pytest.raises(WorkspacePathError):
        write_workspace_file(tmp_path, ".runtime/cache.txt", "x")


def test_write_and_edit_accept_absolute_path_inside_project(tmp_path):
    target = tmp_path / "nested" / "result.md"
    created = write_workspace_file(tmp_path, str(target), "before\n")
    edited = edit_workspace_file(
        tmp_path,
        str(target),
        [{"old_text": "before", "new_text": "after"}],
    )
    assert created["created"] is True
    assert "+1 after" in edited["diff"]
    assert target.read_text(encoding="utf-8") == "after\n"


def test_edit_preserves_original_when_atomic_replace_is_denied(
    tmp_path, monkeypatch,
):
    target = tmp_path / "locked.md"
    target.write_text("before\n", encoding="utf-8")

    def deny_replace(source, destination):
        raise PermissionError(5, "拒绝访问", str(destination))

    monkeypatch.setattr(
        "data_analysis.tools.project_edit_tools.os.replace",
        deny_replace,
    )

    with pytest.raises(PermissionError):
        edit_workspace_file(
            tmp_path,
            "locked.md",
            [{"old_text": "before", "new_text": "after"}],
        )

    assert target.read_text(encoding="utf-8") == "before\n"
    assert not list(tmp_path.glob(".locked.md.*.tmp"))


def test_edit_rejects_stale_expected_content_without_writing(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text("newer\n", encoding="utf-8")

    with pytest.raises(ValueError, match="内容不一致"):
        edit_workspace_file(
            tmp_path,
            "notes.md",
            [{"old_text": "newer", "new_text": "changed"}],
            expected_content=b"older\n",
        )

    assert target.read_text(encoding="utf-8") == "newer\n"


def test_project_file_tools_reject_absolute_path_outside_project(tmp_path):
    outside = tmp_path.parent / "outside.md"
    with pytest.raises(WorkspacePathError, match="超出当前项目目录"):
        write_workspace_file(tmp_path, str(outside), "not allowed")
    with pytest.raises(WorkspacePathError, match="超出当前项目目录"):
        edit_workspace_file(
            tmp_path,
            str(outside),
            [{"old_text": "before", "new_text": "after"}],
        )
    assert not outside.exists()


def test_edit_applies_multiple_replacements_against_original_file(tmp_path):
    target = tmp_path / "settings.py"
    target.write_text(
        'host = "localhost"\nport = 3000\ndebug = False\n',
        encoding="utf-8",
    )

    result = edit_workspace_file(
        tmp_path,
        "settings.py",
        [
            {"old_text": 'host = "localhost"', "new_text": 'host = "0.0.0.0"'},
            {"old_text": "port = 3000", "new_text": "port = 8080"},
        ],
    )

    assert result["edit_count"] == 2
    assert target.read_text(encoding="utf-8") == (
        'host = "0.0.0.0"\nport = 8080\ndebug = False\n'
    )
    assert 'host = "localhost"' in result["diff"]
    assert "port = 8080" in result["diff"]
    assert '-host = "localhost"' in result["patch"]
    assert "+port = 8080" in result["patch"]


def test_edit_is_atomic_when_any_replacement_is_invalid(tmp_path):
    target = tmp_path / "settings.py"
    original = "port = 3000\ndebug = False\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="第 2 项.*无法.*old_text"):
        edit_workspace_file(
            tmp_path,
            "settings.py",
            [
                {"old_text": "port = 3000", "new_text": "port = 8080"},
                {"old_text": "missing = True", "new_text": "missing = False"},
            ],
        )

    assert target.read_text(encoding="utf-8") == original


def test_edit_rejects_overlapping_replacements(tmp_path):
    target = tmp_path / "example.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    with pytest.raises(ValueError, match="修改范围重叠"):
        edit_workspace_file(
            tmp_path,
            "example.txt",
            [
                {"old_text": "alpha beta", "new_text": "first"},
                {"old_text": "beta gamma", "new_text": "second"},
            ],
        )

    assert target.read_text(encoding="utf-8") == "alpha beta gamma\n"


def test_edit_preserves_utf8_bom_and_crlf(tmp_path):
    target = tmp_path / "windows.txt"
    target.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")

    edit_workspace_file(
        tmp_path,
        "windows.txt",
        [{"old_text": "first\nsecond", "new_text": "one\ntwo"}],
    )

    assert target.read_bytes() == b"\xef\xbb\xbfone\r\ntwo\r\n"


def test_edit_preserves_lf_on_windows(tmp_path):
    target = tmp_path / "unix.txt"
    target.write_bytes(b"first\nsecond\n")

    edit_workspace_file(
        tmp_path,
        "unix.txt",
        [{"old_text": "first\r\nsecond", "new_text": "one\r\ntwo"}],
    )

    assert target.read_bytes() == b"one\ntwo\n"


def test_edit_fuzzy_match_preserves_untouched_original_lines(tmp_path):
    target = tmp_path / "unicode.md"
    original = "标题\u00a0值  \n未触及“原样”  \n目标：“旧值”  \n"
    target.write_text(original, encoding="utf-8", newline="")

    result = edit_workspace_file(
        tmp_path,
        "unicode.md",
        [{"old_text": '目标："旧值"', "new_text": "目标：“新值”"}],
    )

    updated = target.read_text(encoding="utf-8")
    assert updated.startswith("标题\u00a0值  \n未触及“原样”  \n")
    assert updated.endswith("目标：“新值”\n")
    assert result["first_changed_line"] == 3


def test_edit_prefers_unique_exact_match_over_fuzzy_equivalent(tmp_path):
    target = tmp_path / "quotes.txt"
    target.write_text("target 'old'\ntarget ‘old’\n", encoding="utf-8", newline="")

    edit_workspace_file(
        tmp_path,
        "quotes.txt",
        [{"old_text": "target 'old'", "new_text": "target 'new'"}],
    )

    assert target.read_text(encoding="utf-8") == "target 'new'\ntarget ‘old’\n"


def test_edit_rejects_no_op_replacement(tmp_path):
    target = tmp_path / "same.txt"
    target.write_text("same\n", encoding="utf-8")

    with pytest.raises(ValueError, match="完全相同"):
        edit_workspace_file(
            tmp_path,
            "same.txt",
            [{"old_text": "same", "new_text": "same"}],
        )

    assert target.read_text(encoding="utf-8") == "same\n"


def test_edit_runtime_accepts_pi_compatible_argument_shapes(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before", encoding="utf-8")
    second.write_text("before", encoding="utf-8")
    context = {"workspace_root": str(tmp_path)}

    run_edit_workspace_file(
        {
            "relative_path": "first.txt",
            "edits": '{"oldText":"before","newText":"after"}',
        },
        context,
    )
    run_edit_workspace_file(
        {
            "relative_path": "second.txt",
            "old_text": "before",
            "new_text": "after",
        },
        context,
    )

    assert first.read_text(encoding="utf-8") == "after"
    assert second.read_text(encoding="utf-8") == "after"
