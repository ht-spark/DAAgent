"""文件化 Skill 注册与运行时的回归测试。"""

import io
from pathlib import Path
from zipfile import ZipFile

import pytest

from data_analysis.agent.graph import iter_graph
from data_analysis.agent.loop import Finish
from data_analysis.agent.state import AgentState
from data_analysis.runtime import ExecutionContext
from data_analysis.skills import SkillAction, SkillRegistry, SkillRegistryError, SkillRuntime
from data_analysis.tools.code_tools import execute_python_code


def _write_skill(root: Path) -> None:
    """写入一个只用于测试的最小 Skill。"""
    directory = root / "demo-skill"
    for name in ("references", "assets", "scripts"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\nname: demo-skill\ntitle: 示例技能\ndescription: 验证技能运行时\n---\n按需读取资料。",
        encoding="utf-8",
    )
    (directory / "references" / "rules.md").write_text("规则正文", encoding="utf-8")
    (directory / "assets" / "template.md").write_text("# 模板", encoding="utf-8")
    (directory / "scripts" / "echo.py").write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'ok': True, 'result': payload['arguments'], 'artifacts': []}))\n",
        encoding="utf-8",
    )
    (directory / "DAAGENT.yaml").write_text(
        """version: 1
references:
  - id: rules
    path: references/rules.md
assets:
  - id: template
    path: assets/template.md
scripts:
  - id: echo
    path: scripts/echo.py
    requires_approval: false
""",
        encoding="utf-8",
    )


def test_registry_and_runtime(tmp_path: Path) -> None:
    """用户加载后可按稳定 ID 读取资料并执行声明的脚本。"""
    root = tmp_path / "skills"
    _write_skill(root)
    registry = SkillRegistry(root)
    context = registry.load_selected(["demo-skill"])
    runtime = SkillRuntime(registry, context, output_dir=tmp_path / "output")

    reference = runtime.execute(SkillAction(
        operation="read_reference", skill_id="demo-skill", resource_id="rules"
    ))
    script = runtime.execute(SkillAction(
        operation="run_script",
        skill_id="demo-skill",
        entrypoint_id="echo",
        arguments={"value": 7},
    ))

    assert registry.discover()[0].skill_id == "demo-skill"
    assert reference.result["content"] == "规则正文"
    assert script.result["result"] == {"value": 7}


def test_registry_rejects_path_traversal(tmp_path: Path) -> None:
    """清单资源不得离开规定目录。"""
    _write_skill(tmp_path)
    manifest = tmp_path / "demo-skill" / "DAAGENT.yaml"
    manifest.write_text(
        "version: 1\nreferences:\n  - id: bad\n    path: ../outside.md\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillRegistryError, match="安全相对路径"):
        SkillRegistry(tmp_path).discover()


def test_runtime_rejects_unselected_skill(tmp_path: Path) -> None:
    """未由用户启用的 Skill 不能被模型自行调用。"""
    _write_skill(tmp_path)
    registry = SkillRegistry(tmp_path)
    runtime = SkillRuntime(registry, registry.load_selected([]), output_dir=tmp_path / "out")
    result = runtime.execute(SkillAction(
        operation="read_reference", skill_id="demo-skill", resource_id="rules"
    ))
    assert result.success is False
    assert "未在本次运行中启用" in (result.error or "")


def test_graph_dispatches_skill_action_without_using_harness(tmp_path: Path) -> None:
    """执行图应把 SkillAction 交给独立运行时并写回下一轮上下文。"""
    _write_skill(tmp_path)
    registry = SkillRegistry(tmp_path)
    context = registry.load_selected(["demo-skill"])
    runtime = SkillRuntime(registry, context, output_dir=tmp_path / "out")

    class Loop:
        def step(self, state: AgentState):
            if state.skill_observations:
                return Finish(answer="已读取")
            return SkillAction(
                operation="read_reference",
                skill_id="demo-skill",
                resource_id="rules",
            )

    class Harness:
        def execute(self, action):
            raise AssertionError("SkillAction 不应进入 Harness")

    state = AgentState(user_query="读取规则", skill_context=context)
    events = list(iter_graph(state, Loop(), Harness(), runtime))

    assert len(events) == 3
    assert isinstance(events[0], SkillAction)
    assert isinstance(events[2], Finish)
    assert state.skill_observations[0].success is True


def test_materialized_asset_is_available_to_python_tool(tmp_path: Path) -> None:
    """物化资产应进入共享上下文，并通过 SKILL_ASSETS 提供给 Python 工具。"""
    _write_skill(tmp_path)
    registry = SkillRegistry(tmp_path)
    context = registry.load_selected(["demo-skill"])
    execution_context = ExecutionContext(output_dir=tmp_path / "out")
    runtime = SkillRuntime(
        registry,
        context,
        output_dir=execution_context.output_dir,
        on_asset_materialized=execution_context.register_skill_asset,
    )

    observation = runtime.execute(SkillAction(
        operation="materialize_asset",
        skill_id="demo-skill",
        resource_id="template",
    ))
    result = execute_python_code(
        "from pathlib import Path\nresult = Path(SKILL_ASSETS['demo-skill:template']).read_text(encoding='utf-8')",
        data_paths=[],
        output_dir=execution_context.output_dir,
        skill_assets=execution_context.skill_assets,
    )

    assert observation.success is True
    assert result["result"] == "# 模板"


def test_uploaded_skill_is_exportable_and_forces_script_approval(tmp_path: Path) -> None:
    """前端导入的 Skill 可再导出，且脚本不可信声明不能跳过人工审批。"""
    source_root = tmp_path / "source"
    _write_skill(source_root)
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for path in (source_root / "demo-skill").rglob("*"):
            if path.is_file():
                archive.write(path, f"demo-skill/{path.relative_to(source_root / 'demo-skill').as_posix()}")

    registry = SkillRegistry(tmp_path / "installed")
    metadata = registry.install_archive(buffer.getvalue())

    assert metadata.scripts[0].requires_approval is True
    assert registry.export_archive("demo-skill").startswith(b"PK")
