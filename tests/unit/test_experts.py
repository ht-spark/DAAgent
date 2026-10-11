"""文件化专家注册与运行时注入的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_analysis.agent.graph import _final_state, _todo_state
from data_analysis.agent.loop import AgentLoop
from data_analysis.agent.state import AgentState, AnalysisPlan, TodoItem
from data_analysis.experts import (
    ExpertEditableSpec,
    ExpertRegistry,
    ExpertRegistryError,
    ExpertSkillBinding,
    ExpertSopStage,
)
from data_analysis.skills import SkillRegistry


class _UnusedLlm:
    """只用于构造决策组件，测试不应真实调用模型。"""

    def invoke(self, _messages: list[tuple[str, str]]) -> str:
        raise AssertionError("本测试不应调用模型")


def _write_expert(root: Path, *, default_skill_ids: list[str] | None = None) -> None:
    """写入最小专家定义，便于覆盖注册表边界。"""
    root.mkdir(parents=True, exist_ok=True)
    skills = json.dumps(default_skill_ids or [], ensure_ascii=False)
    (root / "demo-expert.yaml").write_text(
        "\n".join([
            "expert_id: demo-expert",
            "name: 示例专家",
            "description: 用于验证专家运行快照",
            'version: "1.0.0"',
            "enabled: true",
            "glyph: 专",
            "tags: [验证]",
            "instructions: 先核验数据，再形成结论。",
            f"default_skill_ids: {skills}",
            "output_requirements: [给出证据]",
            "validation_requirements: [检查数据完整性]",
        ]),
        encoding="utf-8",
    )


def _write_skill(root: Path, skill_id: str) -> None:
    """写入可被专家绑定的最小 Skill。"""
    directory = root / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "\n".join([
            "---",
            f"name: {skill_id}",
            f"title: {skill_id}",
            "description: 专家测试技能",
            "---",
            "按测试要求执行。",
        ]),
        encoding="utf-8",
    )


def _draft(*, version: str = "1.0.0") -> ExpertEditableSpec:
    """构造包含 Skill 与 SOP 的完整专家草稿。"""
    return ExpertEditableSpec(
        expert_id="operations-expert",
        name="运营分析专家",
        description="执行企业运营分析 SOP",
        version=version,
        glyph="运",
        tags=("运营", "分析"),
        instructions="先确认业务口径，再进行分析。",
        skill_bindings=(
            ExpertSkillBinding(skill_id="required-skill", mode="required"),
            ExpertSkillBinding(skill_id="default-skill", mode="default"),
            ExpertSkillBinding(skill_id="optional-skill", mode="optional"),
        ),
        sop_stages=(
            ExpertSopStage(
                stage_id="confirm-scope",
                name="确认口径",
                objective="确认指标、范围和约束",
                guidance=("列出仍需确认的信息",),
            ),
        ),
        output_requirements=("给出可追溯结论",),
        validation_requirements=("检查指标口径一致性",),
    )


def test_registry_discovers_and_loads_versioned_expert(tmp_path: Path) -> None:
    """专家应从 YAML 加载，并固定可复现的内容哈希。"""
    expert_root = tmp_path / "experts"
    _write_expert(expert_root)
    registry = ExpertRegistry(expert_root, SkillRegistry(tmp_path / "skills"))

    discovered = registry.discover()
    context = registry.load_selected("demo-expert")

    assert discovered[0].expert_id == "demo-expert"
    assert len(discovered[0].bundle_hash) == 64
    assert context.expert_id == "demo-expert"
    assert context.expert == discovered[0]


def test_registry_rejects_missing_default_skill(tmp_path: Path) -> None:
    """专家声明的默认 Skill 不存在时不能启动运行。"""
    expert_root = tmp_path / "experts"
    _write_expert(expert_root, default_skill_ids=["missing-skill"])
    registry = ExpertRegistry(expert_root, SkillRegistry(tmp_path / "skills"))

    with pytest.raises(ExpertRegistryError, match="缺少 Skill"):
        registry.load_selected("demo-expert")


def test_draft_crud_and_publish_preserve_version_history(tmp_path: Path) -> None:
    """草稿应可保存校验和发布，且发布版本不能被覆盖。"""
    skill_root = tmp_path / "skills"
    for skill_id in ("required-skill", "default-skill", "optional-skill"):
        _write_skill(skill_root, skill_id)
    expert_root = tmp_path / "experts"
    registry = ExpertRegistry(expert_root, SkillRegistry(skill_root))

    created = registry.create_draft(_draft())
    validated = registry.validate_draft(created.expert_id)
    published = registry.publish_draft(created.expert_id)

    assert validated.runtime_skill_ids == ("required-skill", "default-skill")
    assert published.version == "1.0.0"
    assert (expert_root / ".versions/operations-expert/1.0.0.yaml").is_file()
    assert registry.load_selected(created.expert_id).expert == published

    with pytest.raises(ExpertRegistryError, match="提高专家版本号"):
        registry.publish_draft(created.expert_id)

    updated = registry.update_draft(
        created.expert_id,
        created.model_copy(update={"version": "1.1.0"}),
    )
    republished = registry.publish_draft(updated.expert_id)

    assert republished.version == "1.1.0"
    assert (expert_root / ".versions/operations-expert/1.1.0.yaml").is_file()

    registry.delete_draft(updated.expert_id)
    assert registry.list_drafts() == []


def test_draft_validation_checks_optional_skill_dependency(tmp_path: Path) -> None:
    """可选 Skill 虽不自动加载，发布前仍必须确认其存在。"""
    skill_root = tmp_path / "skills"
    _write_skill(skill_root, "required-skill")
    _write_skill(skill_root, "default-skill")
    registry = ExpertRegistry(tmp_path / "experts", SkillRegistry(skill_root))
    registry.create_draft(_draft())

    with pytest.raises(ExpertRegistryError, match="optional-skill"):
        registry.validate_draft("operations-expert")


def test_expert_is_injected_and_preserved_across_plan_states(tmp_path: Path) -> None:
    """Planner 子状态和最终汇总必须共享同一专家版本快照。"""
    expert_root = tmp_path / "experts"
    _write_expert(expert_root)
    context = ExpertRegistry(
        expert_root, SkillRegistry(tmp_path / "skills")
    ).load_selected("demo-expert")
    plan = AnalysisPlan(
        objective="完成分析",
        todos=[TodoItem(id="todo_1", objective="核验", expected_outputs=["结果"])],
    )
    state = AgentState(user_query="分析实验", expert_context=context, analysis_plan=plan)

    messages = AgentLoop(_UnusedLlm(), [], "遵守事实。文件化专家测试").build_messages(state)
    todo_state = _todo_state(state, plan.todos[0])
    final_state = _final_state(state, plan)

    assert "示例专家" in messages[0][1]
    assert '"expert_id": "demo-expert"' in messages[-1][1]
    assert todo_state.expert_context == context
    assert final_state.expert_context == context
