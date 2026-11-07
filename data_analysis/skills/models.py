"""Skill 注册表与运行时共享的数据模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class SkillResource(BaseModel):
    """Skill 中通过稳定标识公开的资源。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1)
    description: str = ""
    media_type: str | None = None


class SkillScript(BaseModel):
    """Skill 中可由运行时调用的固定脚本入口。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entrypoint_id: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1)
    description: str = ""
    runtime: Literal["python", "python-cli"] = "python"
    # 单次脚本最长运行秒数；限制在 1～300 秒，防止 Skill 长时间占用执行进程。
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    requires_approval: bool = True
    read_data_files: bool = False
    read_assets: tuple[str, ...] = ()
    write_artifacts: bool = False
    network: bool = False


class SkillMetadata(BaseModel):
    """发现阶段可公开给前端和模型的 Skill 索引。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str
    name: str
    description: str
    bundle_hash: str
    # 来源由注册表判定，不接受上传包自行声明为系统技能。
    source: Literal["system", "personal"] = "personal"
    references: tuple[SkillResource, ...] = ()
    assets: tuple[SkillResource, ...] = ()
    scripts: tuple[SkillScript, ...] = ()


class ActiveSkill(BaseModel):
    """本次运行已由用户手动启用的 Skill。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata: SkillMetadata
    instructions: str


class SkillContext(BaseModel):
    """本次 Agent 运行固定的 Skill 快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    active_skills: tuple[ActiveSkill, ...] = ()

    @property
    def active_ids(self) -> tuple[str, ...]:
        """返回所有已启用 Skill 的稳定标识。"""
        return tuple(item.metadata.skill_id for item in self.active_skills)

    def require(self, skill_id: str) -> ActiveSkill:
        """读取已启用 Skill；未启用时拒绝访问。"""
        normalized = skill_id.strip().casefold()
        for item in self.active_skills:
            if item.metadata.skill_id.casefold() == normalized:
                return item
        raise ValueError(f"Skill 未在本次运行中启用: {skill_id}")


class SkillAction(BaseModel):
    """智能体针对已激活 Skill 发出的受控操作。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["skill_action"] = "skill_action"
    thought: str = ""
    operation: Literal[
        "read_reference",
        "read_asset",
        "materialize_asset",
        "run_script",
    ]
    skill_id: str = Field(min_length=1)
    resource_id: str | None = None
    entrypoint_id: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    offset: int = Field(default=0, ge=0)
    # 单次文本资源最多注入 32,000 字符，默认 8,000，避免大资料挤占模型上下文。
    max_chars: int = Field(default=8_000, ge=1, le=32_000)
    reasoning: str = ""

    @field_validator("skill_id", "resource_id", "entrypoint_id")
    @classmethod
    def normalize_identifier(cls, value: str | None) -> str | None:
        """清理可选标识并拒绝空白字符串。"""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Skill 标识不能为空")
        return normalized

    @property
    def is_finish(self) -> bool:
        """Skill 操作不是最终回答。"""
        return False


class SkillObservation(BaseModel):
    """SkillRuntime 返回的标准化执行结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    skill_id: str
    success: bool
    resource_id: str | None = None
    entrypoint_id: str | None = None
    result: Any = None
    error: str | None = None
    duration_ms: int = 0
