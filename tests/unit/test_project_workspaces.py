"""项目文件夹注册、会话绑定和项目上下文测试。"""
from __future__ import annotations

import asyncio

import pytest

from server import main, store
from server.project_workspaces import create_project, load_project_context
from server.schemas import ProjectCreateBody


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    monkeypatch.setattr(store, "_DB_PATH", db_path)
    monkeypatch.setattr(store, "_conn", None)
    store._connect()
    yield
    if store._conn is not None:
        store._conn.close()
        monkeypatch.setattr(store, "_conn", None)


def test_same_canonical_folder_reuses_project(tmp_path):
    first = create_project(str(tmp_path), "项目一")
    second = create_project(str(tmp_path / "."), "另一个名称")

    assert first["id"] == second["id"]
    assert len(store.list_projects()) == 1


def test_session_binding_is_immutable_and_project_removal_keeps_folder(tmp_path):
    marker = tmp_path / "data.csv"
    marker.write_text("x\n1\n", encoding="utf-8")
    first = create_project(str(tmp_path), "项目一")
    other_root = tmp_path / "other"
    other_root.mkdir()
    second = create_project(str(other_root), "项目二")
    session_id = store.create_session(
        "会话",
        project_id=first["id"],
        cwd=first["root_path"],
    )

    assert store.bind_session_project(session_id, second["id"], second["root_path"]) is False
    assert store.delete_project(first["id"]) is True
    assert marker.is_file()
    session = store.get_session(session_id)
    assert session["project_id"] is None
    assert session["cwd"] is None


def test_project_context_loads_only_root_instruction_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("项目规则", encoding="utf-8")
    (tmp_path / "Project.md").write_text("项目背景", encoding="utf-8")

    context = load_project_context(tmp_path)

    assert "项目规则" in context
    assert "项目背景" in context


def test_project_api_registers_existing_folder(tmp_path):
    result = asyncio.run(
        main.create_project_endpoint(ProjectCreateBody(root_path=str(tmp_path), title="数据项目"))
    )

    assert result.title == "数据项目"
    assert result.root_path == str(tmp_path.resolve())


def test_select_folder_endpoint_returns_path(tmp_path, monkeypatch):
    """系统文件夹选择接口应把对话框结果原样返回。"""
    monkeypatch.setattr(main, "_open_native_folder_dialog", lambda initial_dir=None: str(tmp_path))

    result = asyncio.run(main.select_system_folder_endpoint())

    assert result == {"path": str(tmp_path)}


def test_select_folder_endpoint_cancel_returns_null(monkeypatch):
    """用户取消选择时返回 path=null，前端据此静默返回。"""
    monkeypatch.setattr(main, "_open_native_folder_dialog", lambda initial_dir=None: None)

    result = asyncio.run(main.select_system_folder_endpoint())

    assert result == {"path": None}


def test_select_folder_endpoint_maps_gui_failure(monkeypatch):
    """GUI 不可用时映射为 503，便于前端回退手输路径。"""
    def _boom(initial_dir=None):
        raise RuntimeError("当前环境缺少 tkinter，无法打开系统文件夹选择框")

    monkeypatch.setattr(main, "_open_native_folder_dialog", _boom)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(main.select_system_folder_endpoint())

    assert getattr(exc_info.value, "status_code", None) == 503


def test_project_output_directory_and_artifact_listing(tmp_path):
    output_dir = main._agent_output_root("session-output", tmp_path)
    output_dir.mkdir(parents=True)
    (output_dir / "report.md").write_text("结果", encoding="utf-8")

    artifacts = main._list_agent_artifacts(
        "session-output",
        "run-1",
        tmp_path,
    )

    assert output_dir == (tmp_path / "outputs" / "session-output").resolve()
    assert artifacts[0]["path"] == "report.md"


def test_existing_session_cannot_switch_projects(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = create_project(str(first_root), "项目一")
    second = create_project(str(second_root), "项目二")
    session_id = store.create_session(
        "固定会话",
        project_id=first["id"],
        cwd=first["root_path"],
    )

    with pytest.raises(ValueError, match="其他项目"):
        main._resolve_agent_project(store.get_session(session_id), second["id"])
