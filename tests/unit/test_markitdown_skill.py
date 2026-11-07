"""MarkItDown 技能发现、授权、分页和真实脚本协议的回归测试。"""
from __future__ import annotations

import asyncio
import io
import json
import runpy
import shutil
import sys
from pathlib import Path

import pytest

from data_analysis.agent.state import AgentState
from data_analysis.skills import SkillAction, SkillRegistry, SkillRuntime
from data_analysis.tools import markitdown as conversion
from server import main


SKILLS_ROOT = Path(__file__).resolve().parents[2] / "data_analysis" / "skills"
SCRIPT_PATH = SKILLS_ROOT / "markitdown" / "scripts" / "convert.py"
# 直接加载脚本用于边界测试，不在技能包内生成影响版本快照的字节码文件。
SCRIPT = runpy.run_path(str(SCRIPT_PATH), run_name="markitdown_skill_test")
convert_request = SCRIPT["convert_request"]


@pytest.fixture
def request_payload(tmp_path: Path, monkeypatch):
    source = tmp_path / "参考资料.docx"
    source.write_bytes(b"PK\x03\x04")
    monkeypatch.setattr(conversion, "to_markdown", lambda _path: "# 资料\n\n真实转换内容")
    return {
        "arguments": {"file_path": str(source)},
        "runtime": {"data_files": [str(source)], "output_dir": str(tmp_path / "output")},
    }


def test_skill_is_discoverable_through_skill_page_api(monkeypatch):
    """技能页所用接口直接返回新包的元数据和可执行入口。"""
    monkeypatch.setattr(main, "_skill_registry", SkillRegistry(SKILLS_ROOT))
    response = asyncio.run(main.list_skills_endpoint())
    skill = next(item for item in response["skills"] if item["skill_id"] == "markitdown")
    assert skill["description"]
    assert len(skill["scripts"]) == 1
    script = skill["scripts"][0]
    assert script["entrypoint_id"] == "convert-markdown"
    assert script["runtime"] == "python"
    assert script["requires_approval"] is True
    assert script["read_data_files"] is True
    assert script["write_artifacts"] is True
    assert script["network"] is False


def test_request_returns_reusable_markdown_artifact(request_payload, monkeypatch):
    calls = []

    def convert(path):
        calls.append(path)
        return "# 资料\n\n真实转换内容"

    monkeypatch.setattr(conversion, "to_markdown", convert)
    first = convert_request(request_payload)
    second = convert_request(request_payload)
    output = Path(request_payload["runtime"]["output_dir"])
    assert first["ok"] is True
    assert len(calls) == 1
    assert first["result"]["converted_now"] is True
    assert second["result"]["converted_now"] is False
    assert (output / first["artifacts"][0]).read_text(encoding="utf-8") == "# 资料\n\n真实转换内容"
    assert Path(first["artifacts"][0]).is_absolute() is False


@pytest.mark.parametrize("payload", [None, [], {}, {"arguments": [], "runtime": {}}, {"arguments": {}, "runtime": []}])
def test_request_rejects_malformed_json_objects(payload):
    with pytest.raises(ValueError):
        convert_request(payload)


@pytest.mark.parametrize("output_dir", [None, "", "relative-output"])
def test_request_requires_runtime_artifact_directory(request_payload, output_dir):
    request_payload["runtime"]["output_dir"] = output_dir
    with pytest.raises(ValueError, match="output_dir"):
        convert_request(request_payload)


def test_request_rejects_unauthorized_source_without_creating_artifacts(request_payload):
    request_payload["runtime"]["data_files"] = []
    with pytest.raises(PermissionError):
        convert_request(request_payload)
    assert not Path(request_payload["runtime"]["output_dir"]).exists()


def test_long_line_pagination_does_not_lose_characters(request_payload, monkeypatch):
    markdown = "正文" * conversion.MAX_PAGE_CHARS + "\n后续内容"
    monkeypatch.setattr(conversion, "to_markdown", lambda _path: markdown)
    pieces = []
    for _ in range(4):
        response = convert_request(request_payload)["result"]
        pieces.append(response["content"])
        assert len(response["content"]) <= conversion.MAX_PAGE_CHARS
        if not response["truncated"]:
            break
        request_payload["arguments"].update(
            offset=response["next_offset"], char_offset=response["next_char_offset"],
        )
    else:
        pytest.fail("分页未能在预期页数内结束")
    assert "".join(pieces) == markdown


@pytest.mark.parametrize("char_offset", [-1, True, "1", 1_000_000])
def test_request_rejects_invalid_line_character_cursor(request_payload, char_offset):
    request_payload["arguments"]["char_offset"] = char_offset
    with pytest.raises(ValueError, match="char_offset"):
        convert_request(request_payload)


def test_artifact_destination_is_checked_before_conversion(request_payload, monkeypatch):
    output = Path(request_payload["runtime"]["output_dir"])
    monkeypatch.setattr(conversion, "_converted_path", lambda root, _source: root / ".." / "outside.md")
    with pytest.raises(ValueError, match="相对路径"):
        convert_request(request_payload)
    assert not (output.parent / "outside.md").exists()


def test_script_keeps_diagnostics_out_of_protocol_stdout(request_payload, monkeypatch, capsys):
    def noisy_converter(_path):
        sys.stdout.write("转换器诊断\n")
        return "# 正文"

    monkeypatch.setattr(conversion, "to_markdown", noisy_converter)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request_payload)))
    SCRIPT["main"]()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert "转换器诊断" in captured.err


def test_script_returns_structured_error_for_invalid_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{broken"))
    SCRIPT["main"]()
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is False
    assert response["artifacts"] == []
    assert response["error"]


def test_skill_runtime_requires_selection_and_approval(tmp_path: Path):
    registry = SkillRegistry(SKILLS_ROOT)
    action = SkillAction(operation="run_script", skill_id="markitdown", entrypoint_id="convert-markdown")
    unselected = SkillRuntime(registry, registry.load_selected([]), output_dir=tmp_path / "out")
    selected = SkillRuntime(
        registry, registry.load_selected(["markitdown"]), output_dir=tmp_path / "out",
        approval=lambda _action: False,
    )
    assert "未在本次运行中启用" in unselected.execute(action).error
    assert "未获得本次人工批准" in selected.execute(action).error
    assert not (tmp_path / "out").exists()


def test_exported_skill_converts_docx_in_isolated_subprocess(tmp_path: Path):
    """真实执行导出再导入的包，验证隔离子进程、转换缓存与产物登记。"""
    from docx import Document

    source = tmp_path / "分析资料.docx"
    document = Document()
    document.add_heading("数据说明", level=1)
    document.add_paragraph("附件包含城市经济数据。")
    document.save(source)
    package_root = tmp_path / "packages"
    package = package_root / "my-markitdown"
    shutil.copytree(SKILLS_ROOT / "markitdown", package)
    definition = package / "SKILL.md"
    definition.write_text(definition.read_text(encoding="utf-8").replace("markitdown", "my-markitdown"), encoding="utf-8")
    registry = SkillRegistry(package_root)
    installed = SkillRegistry(tmp_path / "installed")
    installed.install_archive(registry.export_archive("my-markitdown"))
    output = tmp_path / "output"
    runtime = SkillRuntime(
        installed, installed.load_selected(["my-markitdown"]), output_dir=output,
        data_paths=[str(source)], python_executable=sys.executable, approval=lambda _action: True,
    )
    action = SkillAction(
        operation="run_script", skill_id="my-markitdown", entrypoint_id="convert-markdown",
        arguments={"file_path": str(source), "limit": 1},
    )
    first = runtime.execute(action)
    assert first.success is True, first.error
    page = first.result["result"]
    assert "数据说明" in page["content"]
    assert page["truncated"] is True
    action = action.model_copy(update={"arguments": {
        "file_path": str(source), "offset": page["next_offset"], "char_offset": page["next_char_offset"],
    }})
    second = runtime.execute(action)
    assert second.success is True, second.error
    assert second.result["result"]["converted_now"] is False
    assert "城市经济数据" in second.result["result"]["content"]
    assert first.result["artifacts"] == second.result["artifacts"]
    artifact = first.result["artifacts"][0]
    assert artifact.startswith("skill-artifacts/my-markitdown/")
    assert (output / artifact).is_file()
    state = AgentState(user_query="读取资料", output_dir=str(output))
    state.record_skill_observation(first)
    assert state.available_artifacts[0].relative_path == artifact
