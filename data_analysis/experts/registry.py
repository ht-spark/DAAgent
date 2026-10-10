"""从项目专家目录发现、校验并加载文件化专家。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from data_analysis.skills import SkillRegistry, SkillRegistryError

from .models import ExpertContext, ExpertSpec

EXPERT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ExpertRegistryError(ValueError):
    """专家定义格式、状态或依赖不合法。"""


class ExpertRegistry:
    """管理独立于 Skill 的文件化专家注册表。"""

    def __init__(self, root: str | Path, skill_registry: SkillRegistry) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.skill_registry = skill_registry

    def discover(self) -> list[ExpertSpec]:
        """返回全部格式合法的专家，包含禁用项以供管理界面展示。"""
        if not self.root.is_dir():
            return []
        return [self._load(path) for path in sorted(self.root.glob("*.yaml"))]

    def get(self, expert_id: str) -> ExpertSpec:
        """按稳定标识读取一个专家定义。"""
        normalized = expert_id.strip()
        if not EXPERT_ID_PATTERN.fullmatch(normalized):
            raise ExpertRegistryError("专家标识不合法")
        path = self.root / f"{normalized}.yaml"
        if not path.is_file():
            raise ExpertRegistryError(f"专家不存在: {expert_id}")
        return self._load(path)

    def load_selected(self, expert_id: str | None) -> ExpertContext:
        """加载用户选择的专家，并校验其状态和默认 Skill 依赖。"""
        if expert_id is None or not expert_id.strip():
            return ExpertContext()
        expert = self.get(expert_id)
        if not expert.enabled:
            raise ExpertRegistryError(f"专家已禁用: {expert.expert_id}")
        for skill_id in expert.default_skill_ids:
            try:
                self.skill_registry.get(skill_id)
            except SkillRegistryError as exc:
                raise ExpertRegistryError(
                    f"专家 {expert.expert_id} 缺少默认 Skill: {skill_id}"
                ) from exc
        return ExpertContext(expert=expert)

    def _load(self, path: Path) -> ExpertSpec:
        """读取单个 YAML，并以文件内容哈希固定版本快照。"""
        try:
            payload = path.read_bytes()
            raw = yaml.safe_load(payload.decode("utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ExpertRegistryError(f"无法读取专家定义 {path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ExpertRegistryError(f"专家定义必须是对象: {path.name}")
        expert_id = str(raw.get("expert_id") or "").strip()
        if not EXPERT_ID_PATTERN.fullmatch(expert_id):
            raise ExpertRegistryError(f"专家标识必须使用 kebab-case: {path.name}")
        if path.stem.casefold() != expert_id.casefold():
            raise ExpertRegistryError("expert_id 必须与 YAML 文件名一致")
        # 版本哈希只能由注册表根据原始文件计算，不能接受定义文件自行声明。
        raw.pop("bundle_hash", None)
        try:
            return ExpertSpec(
                **raw,
                bundle_hash=hashlib.sha256(payload).hexdigest(),
            )
        except ValidationError as exc:
            raise ExpertRegistryError(f"专家定义不合法 {path.name}: {exc}") from exc
