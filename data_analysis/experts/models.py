"""专家注册表与 Agent 运行时共享的数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExpertSkillBinding(BaseModel):
    """专家对一个 Skill 的二元装配规则。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=300)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_mode(cls, value: object) -> object:
        """兼容旧版三态配置；绑定项在新版中一律自动加载。"""
        if not isinstance(value, dict) or "mode" not in value:
            return value
        migrated = dict(value)
        legacy_mode = migrated.pop("mode")
        if legacy_mode not in {"required", "default", "optional"}:
            migrated["mode"] = legacy_mode
        return migrated

    @field_validator("skill_id")
    @classmethod
    def strip_skill_id(cls, value: str) -> str:
        """清理 Skill 标识并拒绝空白值。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("Skill 标识不能为空")
        return normalized

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        """清理可选的绑定说明。"""
        return value.strip()


class ExpertSopStage(BaseModel):
    """供模型执行时参考的一个业务 SOP 阶段。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=500)
    guidance: tuple[str, ...] = ()
    required_inputs: tuple[str, ...] = ()
    blocking_conditions: tuple[str, ...] = ()

    @field_validator("stage_id", "name", "objective")
    @classmethod
    def strip_stage_text(cls, value: str) -> str:
        """清理 SOP 阶段必填文本。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("SOP 阶段必填文本不能为空")
        return normalized

    @field_validator("guidance", "required_inputs", "blocking_conditions")
    @classmethod
    def normalize_stage_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """清理 SOP 阶段列表字段并保持顺序去重。"""
        return _normalize_text_items(values, "SOP 阶段列表字段")


class ExpertEditableSpec(BaseModel):
    """可保存为草稿并发布的专家配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expert_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=40)
    enabled: bool = True
    glyph: str = Field(default="专", min_length=1, max_length=4)
    tags: tuple[str, ...] = ()
    instructions: str = Field(min_length=1)
    skill_bindings: tuple[ExpertSkillBinding, ...] = ()
    sop_stages: tuple[ExpertSopStage, ...] = ()
    # 兼容第一版专家定义；新建专家应使用 skill_bindings。
    default_skill_ids: tuple[str, ...] = ()
    output_requirements: tuple[str, ...] = ()
    validation_requirements: tuple[str, ...] = ()

    @field_validator(
        "expert_id",
        "name",
        "description",
        "version",
        "glyph",
        "instructions",
    )
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """清理必填文本并拒绝只包含空白的值。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("专家必填文本不能为空")
        return normalized

    @field_validator(
        "tags",
        "default_skill_ids",
        "output_requirements",
        "validation_requirements",
    )
    @classmethod
    def normalize_text_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """清理列表文本、拒绝空项并保持原顺序去重。"""
        return _normalize_text_items(values, "专家列表字段")

    @model_validator(mode="after")
    def validate_unique_structured_items(self) -> "ExpertEditableSpec":
        """拒绝重复 Skill 绑定和重复 SOP 阶段标识。"""
        skill_ids = [item.skill_id.casefold() for item in self.skill_bindings]
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("同一个 Skill 只能绑定一次")
        stage_ids = [item.stage_id.casefold() for item in self.sop_stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("SOP 阶段标识不能重复")
        return self

    @property
    def runtime_skill_ids(self) -> tuple[str, ...]:
        """返回运行时自动加载的全部已装配 Skill。"""
        selected = list(self.default_skill_ids)
        selected.extend(binding.skill_id for binding in self.skill_bindings)
        return tuple(dict.fromkeys(selected))


class ExpertSpec(ExpertEditableSpec):
    """经过校验且可固定到一次运行中的已发布专家定义。"""

    bundle_hash: str = Field(min_length=64, max_length=64)


class ExpertContext(BaseModel):
    """一次 Agent 运行固定的可选专家快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expert: ExpertSpec | None = None

    @property
    def expert_id(self) -> str | None:
        """返回当前专家稳定标识；通用 Agent 返回空值。"""
        return self.expert.expert_id if self.expert else None


def _normalize_text_items(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    """清理文本元组、拒绝空项并保持原顺序去重。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if not item:
            raise ValueError(f"{field_name}不能包含空项")
        identity = item.casefold()
        if identity not in seen:
            normalized.append(item)
            seen.add(identity)
    return tuple(normalized)
