"""专家注册表与 Agent 运行时共享的数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExpertSpec(BaseModel):
    """经过校验且可固定到一次运行中的专家定义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expert_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    version: str = Field(min_length=1, max_length=40)
    bundle_hash: str = Field(min_length=64, max_length=64)
    enabled: bool = True
    glyph: str = Field(default="专", min_length=1, max_length=4)
    tags: tuple[str, ...] = ()
    instructions: str = Field(min_length=1)
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
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = value.strip()
            if not item:
                raise ValueError("专家列表字段不能包含空项")
            identity = item.casefold()
            if identity not in seen:
                normalized.append(item)
                seen.add(identity)
        return tuple(normalized)


class ExpertContext(BaseModel):
    """一次 Agent 运行固定的可选专家快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expert: ExpertSpec | None = None

    @property
    def expert_id(self) -> str | None:
        """返回当前专家稳定标识；通用 Agent 返回空值。"""
        return self.expert.expert_id if self.expert else None
