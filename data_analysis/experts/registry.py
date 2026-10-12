"""从项目专家目录发现、校验并加载文件化专家。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

import yaml
from pydantic import ValidationError

from data_analysis.skills import SkillRegistry, SkillRegistryError

from .models import ExpertContext, ExpertEditableSpec, ExpertSpec

EXPERT_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


class ExpertRegistryError(ValueError):
    """专家定义格式、状态或依赖不合法。"""


class ExpertRegistry:
    """管理独立于 Skill 的文件化专家注册表。"""

    def __init__(self, root: str | Path, skill_registry: SkillRegistry) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.skill_registry = skill_registry
        self.draft_root = self.root / ".drafts"
        self.version_root = self.root / ".versions"
        self._write_lock = threading.RLock()

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
        self._validate_dependencies(expert)
        return ExpertContext(expert=expert)

    def list_drafts(self) -> list[ExpertEditableSpec]:
        """返回全部专家草稿。"""
        if not self.draft_root.is_dir():
            return []
        return [
            self._load_editable(path)
            for path in sorted(self.draft_root.glob("*.yaml"))
        ]

    def get_draft(self, expert_id: str) -> ExpertEditableSpec:
        """读取一个专家草稿。"""
        path = self._expert_path(self.draft_root, expert_id)
        if not path.is_file():
            raise ExpertRegistryError(f"专家草稿不存在: {expert_id}")
        return self._load_editable(path)

    def create_draft(self, draft: ExpertEditableSpec) -> ExpertEditableSpec:
        """创建新草稿，不覆盖同标识的现有草稿。"""
        self._validate_editable(draft)
        path = self._expert_path(self.draft_root, draft.expert_id)
        with self._write_lock:
            if path.exists():
                raise ExpertRegistryError(f"专家草稿已存在: {draft.expert_id}")
            self._write_editable(path, draft)
        return self.get_draft(draft.expert_id)

    def update_draft(
        self,
        expert_id: str,
        draft: ExpertEditableSpec,
    ) -> ExpertEditableSpec:
        """更新已有草稿，且不允许在更新时更换稳定标识。"""
        if expert_id.strip() != draft.expert_id:
            raise ExpertRegistryError("路径中的专家标识必须与草稿一致")
        self._validate_editable(draft)
        path = self._expert_path(self.draft_root, expert_id)
        with self._write_lock:
            if not path.is_file():
                raise ExpertRegistryError(f"专家草稿不存在: {expert_id}")
            self._write_editable(path, draft)
        return self.get_draft(expert_id)

    def delete_draft(self, expert_id: str) -> None:
        """删除一个未发布草稿。"""
        path = self._expert_path(self.draft_root, expert_id)
        with self._write_lock:
            if not path.is_file():
                raise ExpertRegistryError(f"专家草稿不存在: {expert_id}")
            path.unlink()

    def delete_expert(self, expert_id: str) -> None:
        """彻底删除已发布专家，连同其同名草稿与不可变版本归档。

        归档只服务于发布校验与追溯，删除后同一标识可以按原版本号重新发布。
        """
        path = self._expert_path(self.root, expert_id)
        with self._write_lock:
            if not path.is_file():
                raise ExpertRegistryError(f"专家不存在: {expert_id}")
            path.unlink()
            draft_path = self._expert_path(self.draft_root, expert_id)
            if draft_path.is_file():
                draft_path.unlink()
            archive_dir = self.version_root / expert_id
            if archive_dir.is_dir():
                shutil.rmtree(archive_dir)

    def draft_from_published(self, expert_id: str) -> ExpertEditableSpec:
        """从已发布专家创建草稿；已有草稿时直接返回。"""
        draft_path = self._expert_path(self.draft_root, expert_id)
        if draft_path.is_file():
            return self.get_draft(expert_id)
        expert = self.get(expert_id)
        draft = ExpertEditableSpec.model_validate(
            expert.model_dump(exclude={"bundle_hash"})
        )
        return self.create_draft(draft)

    def validate_draft(self, expert_id: str) -> ExpertEditableSpec:
        """校验草稿结构、标识、版本及所有 Skill 依赖。"""
        draft = self.get_draft(expert_id)
        self._validate_editable(draft)
        self._validate_dependencies(draft)
        return draft

    def publish_draft(self, expert_id: str) -> ExpertSpec:
        """发布草稿并保留不可变版本副本。"""
        with self._write_lock:
            draft = self.validate_draft(expert_id)
            current_path = self._expert_path(self.root, expert_id)
            current = self._load(current_path) if current_path.is_file() else None
            if current is not None and current.version == draft.version:
                raise ExpertRegistryError("发布前请提高专家版本号")

            archive_dir = self.version_root / expert_id
            archive_path = archive_dir / f"{draft.version}.yaml"
            if archive_path.exists():
                raise ExpertRegistryError(
                    f"专家版本已发布，不能覆盖: {draft.version}"
                )

            # 首次经过管理器发布时，补存仓库内已有版本，保证后续可追溯。
            if current is not None:
                current_archive = archive_dir / f"{current.version}.yaml"
                if not current_archive.exists():
                    current_editable = ExpertEditableSpec.model_validate(
                        current.model_dump(exclude={"bundle_hash"})
                    )
                    self._write_editable(current_archive, current_editable)

            self._write_editable(archive_path, draft)
            self._write_editable(current_path, draft)
        return self.get(expert_id)

    def _load(self, path: Path) -> ExpertSpec:
        """读取单个 YAML，并以文件内容哈希固定版本快照。"""
        try:
            payload = path.read_bytes()
            raw = yaml.safe_load(payload.decode("utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ExpertRegistryError(f"无法读取专家定义 {path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ExpertRegistryError(f"专家定义必须是对象: {path.name}")
        self._validate_file_identity(path, raw)
        # 版本哈希只能由注册表根据原始文件计算，不能接受定义文件自行声明。
        raw.pop("bundle_hash", None)
        try:
            expert = ExpertSpec(
                **raw,
                bundle_hash=hashlib.sha256(payload).hexdigest(),
            )
        except ValidationError as exc:
            raise ExpertRegistryError(f"专家定义不合法 {path.name}: {exc}") from exc
        self._validate_editable(expert)
        return expert

    def _load_editable(self, path: Path) -> ExpertEditableSpec:
        """读取一个不包含运行哈希的可编辑专家定义。"""
        raw = self._read_mapping(path)
        self._validate_file_identity(path, raw)
        raw.pop("bundle_hash", None)
        try:
            expert = ExpertEditableSpec.model_validate(raw)
        except ValidationError as exc:
            raise ExpertRegistryError(f"专家草稿不合法 {path.name}: {exc}") from exc
        self._validate_editable(expert)
        return expert

    def _read_mapping(self, path: Path) -> dict[str, object]:
        """读取 UTF-8 YAML 对象。"""
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ExpertRegistryError(f"无法读取专家定义 {path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ExpertRegistryError(f"专家定义必须是对象: {path.name}")
        return raw

    def _validate_file_identity(self, path: Path, raw: dict[str, object]) -> None:
        """校验 YAML 内标识与文件名一致。"""
        expert_id = str(raw.get("expert_id") or "").strip()
        if not EXPERT_ID_PATTERN.fullmatch(expert_id):
            raise ExpertRegistryError(f"专家标识必须使用 kebab-case: {path.name}")
        if path.stem.casefold() != expert_id.casefold():
            raise ExpertRegistryError("expert_id 必须与 YAML 文件名一致")

    def _validate_editable(self, expert: ExpertEditableSpec) -> None:
        """校验会参与文件路径或版本归档的字段。"""
        if not EXPERT_ID_PATTERN.fullmatch(expert.expert_id):
            raise ExpertRegistryError("专家标识必须使用 kebab-case")
        if not VERSION_PATTERN.fullmatch(expert.version):
            raise ExpertRegistryError("专家版本只能包含字母、数字、点、下划线和连字符")
        for stage in expert.sop_stages:
            if not EXPERT_ID_PATTERN.fullmatch(stage.stage_id):
                raise ExpertRegistryError("SOP 阶段标识必须使用 kebab-case")

    def _validate_dependencies(self, expert: ExpertEditableSpec) -> None:
        """确认专家引用的全部 Skill 均已安装。"""
        skill_ids = list(expert.default_skill_ids)
        skill_ids.extend(binding.skill_id for binding in expert.skill_bindings)
        for skill_id in dict.fromkeys(skill_ids):
            try:
                self.skill_registry.get(skill_id)
            except SkillRegistryError as exc:
                raise ExpertRegistryError(
                    f"专家 {expert.expert_id} 缺少 Skill: {skill_id}"
                ) from exc

    def _expert_path(self, directory: Path, expert_id: str) -> Path:
        """在校验稳定标识后生成专家 YAML 路径。"""
        normalized = expert_id.strip()
        if not EXPERT_ID_PATTERN.fullmatch(normalized):
            raise ExpertRegistryError("专家标识不合法")
        return directory / f"{normalized}.yaml"

    def _write_editable(self, path: Path, expert: ExpertEditableSpec) -> None:
        """以同目录临时文件原子写入专家 YAML。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(
            expert.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.stem}-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        except OSError as exc:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise ExpertRegistryError(f"无法保存专家定义 {path.name}: {exc}") from exc
