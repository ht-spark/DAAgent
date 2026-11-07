"""系统与个人技能分类、固定技能保护及上传接口回归测试。"""

import asyncio
import io
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import HTTPException, UploadFile

from data_analysis.skills import SkillRegistry
from data_analysis.skills.registry import SYSTEM_SKILL_IDS, SystemSkillError
from server import main


def _package(skill_id: str, description: str = "个人数据分析技能") -> bytes:
    """构造包含伪造来源声明的上传包，验证来源只由服务端判定。"""
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(f"{skill_id}/SKILL.md", f"---\nname: {skill_id}\ndescription: {description}\nsource: system\n---\n按需分析数据。")
    return buffer.getvalue()


@pytest.mark.parametrize("skill_id", sorted(SYSTEM_SKILL_IDS))
def test_system_skill_cannot_be_overwritten_or_deleted(tmp_path: Path, skill_id: str) -> None:
    """五个保留名称不能通过上传占用，也不能删除已存在的固定技能。"""
    registry = SkillRegistry(tmp_path)
    with pytest.raises(SystemSkillError):
        registry.install_archive(_package(skill_id))
    directory = tmp_path / skill_id
    directory.mkdir()
    definition = directory / "SKILL.md"
    original = f"---\nname: {skill_id}\ndescription: 系统内置技能\n---\n系统说明。"
    definition.write_text(original, encoding="utf-8")
    assert registry.get(skill_id).source == "system"
    with pytest.raises(SystemSkillError):
        registry.install_archive(_package(skill_id))
    with pytest.raises(SystemSkillError):
        registry.uninstall(skill_id)
    assert definition.read_text(encoding="utf-8") == original


def test_personal_skill_upload_update_export_delete(tmp_path: Path) -> None:
    """个人包支持完整管理流程，包内来源字段不能提升为系统技能。"""
    registry = SkillRegistry(tmp_path)
    assert registry.install_archive(_package("my-analysis")).source == "personal"
    updated = registry.install_archive(_package("my-analysis", "更新后的技能"))
    assert updated.description == "更新后的技能"
    assert updated.source == "personal"
    assert registry.export_archive("my-analysis")
    registry.uninstall("my-analysis")
    assert registry.discover() == []


def test_skill_api_enforces_sources_and_fixed_skill_protection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """列表返回来源，系统修改请求为 403，个人上传仍可正常使用。"""
    registry = SkillRegistry(tmp_path)
    monkeypatch.setattr(main, "_skill_registry", registry)
    directory = tmp_path / "docx"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nname: docx\ndescription: 系统文档技能\n---\n说明。", encoding="utf-8")
    for operation in (
        lambda: main.delete_skill_endpoint("docx"),
        lambda: main.import_skill_endpoint(UploadFile(filename="docx.zip", file=io.BytesIO(_package("docx")))),
    ):
        with pytest.raises(HTTPException) as error:
            asyncio.run(operation())
        assert error.value.status_code == 403
    uploaded = asyncio.run(main.import_skill_endpoint(UploadFile(filename="mine.zip", file=io.BytesIO(_package("mine")))))
    assert uploaded["source"] == "personal"
    catalog = asyncio.run(main.list_skills_endpoint())
    assert {item["skill_id"]: item["source"] for item in catalog["skills"]} == {"docx": "system", "mine": "personal"}
    assert asyncio.run(main.delete_skill_endpoint("mine")) == {"removed": "mine"}
