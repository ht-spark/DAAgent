"""从项目技能目录发现、校验、导入和导出 Skill。"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

import yaml

from .models import ActiveSkill, SkillContext, SkillMetadata, SkillResource, SkillScript

SKILL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_FILES = 500
MAX_MEMBER_BYTES = 25 * 1024 * 1024
UPLOADED_MARKER = ".daagent-uploaded"


class SkillRegistryError(ValueError):
    """Skill 包格式、路径或清单不合法。"""


class SkillRegistry:
    """管理独立于 ToolRegistry 的文件化 Skill 注册表。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)

    def discover(self) -> list[SkillMetadata]:
        """扫描一级子目录并返回通过校验的 Skill 元数据。"""
        self.root.mkdir(parents=True, exist_ok=True)
        skills: list[SkillMetadata] = []
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name.casefold()):
            if not directory.is_dir() or directory.name.startswith((".", "_")):
                continue
            if (directory / "SKILL.md").is_file():
                skills.append(self._load_metadata(directory))
        return skills

    def get(self, skill_id: str) -> SkillMetadata:
        """按标识读取一个已安装 Skill。"""
        directory = self._skill_directory(skill_id)
        if not (directory / "SKILL.md").is_file():
            raise SkillRegistryError(f"Skill 不存在: {skill_id}")
        return self._load_metadata(directory)

    def load_selected(self, skill_ids: list[str] | tuple[str, ...]) -> SkillContext:
        """加载用户手动选择的 Skill 正文并固定版本快照。"""
        active: list[ActiveSkill] = []
        seen: set[str] = set()
        for skill_id in skill_ids:
            identity = skill_id.strip().casefold()
            if not identity or identity in seen:
                continue
            metadata = self.get(skill_id)
            instructions = (self._skill_directory(metadata.skill_id) / "SKILL.md").read_text(
                encoding="utf-8"
            )
            active.append(ActiveSkill(metadata=metadata, instructions=instructions))
            seen.add(identity)
        return SkillContext(active_skills=tuple(active))

    def resolve_resource(
        self,
        metadata: SkillMetadata,
        kind: str,
        identifier: str,
    ) -> Path:
        """把稳定资源标识解析成已校验的真实路径。"""
        if self.get(metadata.skill_id).bundle_hash != metadata.bundle_hash:
            raise SkillRegistryError("Skill 内容在运行期间发生变化，请重新加载")
        if kind == "reference":
            entries = metadata.references
            expected_root = "references"
            key = "resource_id"
        elif kind == "asset":
            entries = metadata.assets
            expected_root = "assets"
            key = "resource_id"
        elif kind == "script":
            entries = metadata.scripts
            expected_root = "scripts"
            key = "entrypoint_id"
        else:
            raise SkillRegistryError(f"未知 Skill 资源类型: {kind}")
        for entry in entries:
            if getattr(entry, key).casefold() == identifier.casefold():
                return self._safe_declared_path(
                    self._skill_directory(metadata.skill_id), entry.path, expected_root
                )
        raise SkillRegistryError(f"Skill 未声明该{kind}: {identifier}")

    def install_archive(self, payload: bytes) -> SkillMetadata:
        """安全导入一个 ZIP Skill 包并替换同名版本。"""
        if not payload or len(payload) > MAX_ARCHIVE_BYTES:
            raise SkillRegistryError("Skill 压缩包为空或超过 50 MB")
        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="daagent-skill-") as temporary:
            unpacked = Path(temporary)
            try:
                with ZipFile(io.BytesIO(payload)) as archive:
                    members = [item for item in archive.infolist() if not item.is_dir()]
                    if len(members) > MAX_ARCHIVE_FILES:
                        raise SkillRegistryError("Skill 压缩包文件数量超过 500")
                    for member in members:
                        relative = PurePosixPath(member.filename.replace("\\", "/"))
                        if relative.is_absolute() or ".." in relative.parts:
                            raise SkillRegistryError("Skill 压缩包包含越界路径")
                        if member.file_size > MAX_MEMBER_BYTES:
                            raise SkillRegistryError("Skill 压缩包包含超过 25 MB 的文件")
                        target = unpacked.joinpath(*relative.parts).resolve(strict=False)
                        if unpacked.resolve() not in target.parents:
                            raise SkillRegistryError("Skill 压缩包包含非法路径")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, target.open("wb") as destination:
                            shutil.copyfileobj(source, destination)
            except SkillRegistryError:
                raise
            except Exception as exc:
                raise SkillRegistryError(f"无法读取 Skill 压缩包: {exc}") from exc
            candidates = [path.parent for path in unpacked.rglob("SKILL.md")]
            if len(candidates) != 1:
                raise SkillRegistryError("压缩包必须且只能包含一个 SKILL.md")
            metadata = self._load_metadata(candidates[0])
            target_dir = self._skill_directory(metadata.skill_id)
            staging = self.root / f".{metadata.skill_id}-{next(tempfile._get_candidate_names())}"
            shutil.copytree(candidates[0], staging)
            # 用户上传的 Skill 脚本一律强制人工审批。
            (staging / UPLOADED_MARKER).write_text("uploaded\n", encoding="utf-8")
            if target_dir.exists():
                shutil.rmtree(target_dir)
            staging.replace(target_dir)
        return self.get(metadata.skill_id)

    def export_archive(self, skill_id: str) -> bytes:
        """把一个已安装 Skill 导出为 ZIP 字节流。"""
        metadata = self.get(skill_id)
        directory = self._skill_directory(metadata.skill_id)
        buffer = io.BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, f"{metadata.skill_id}/{path.relative_to(directory).as_posix()}")
        return buffer.getvalue()

    def _load_metadata(self, directory: Path) -> SkillMetadata:
        skill_file = directory / "SKILL.md"
        front_matter, _ = _parse_skill_markdown(skill_file)
        skill_id = str(front_matter.get("name") or directory.name).strip()
        if not SKILL_ID_PATTERN.fullmatch(skill_id) or len(skill_id) > 64:
            raise SkillRegistryError(f"Skill 名称必须是 kebab-case 且不超过 64 字符: {skill_id}")
        if skill_id.casefold() != directory.name.casefold():
            raise SkillRegistryError("SKILL.md 的 name 必须与目录名一致")
        description = str(front_matter.get("description") or "").strip()
        if not description:
            raise SkillRegistryError(f"Skill {skill_id} 缺少 description")
        manifest = _read_manifest(directory / "DAAGENT.yaml")
        references = tuple(
            self._resource(directory, item, "references")
            for item in _manifest_list(manifest, "references")
        )
        assets = tuple(
            self._resource(directory, item, "assets")
            for item in _manifest_list(manifest, "assets")
        )
        scripts = tuple(
            self._script(directory, item)
            for item in _manifest_list(manifest, "scripts")
        )
        if (directory / UPLOADED_MARKER).is_file():
            scripts = tuple(
                item.model_copy(update={"requires_approval": True})
                for item in scripts
            )
        _require_unique([item.resource_id for item in references], "reference id")
        _require_unique([item.resource_id for item in assets], "asset id")
        _require_unique([item.entrypoint_id for item in scripts], "script id")
        return SkillMetadata(
            skill_id=skill_id,
            name=str(front_matter.get("title") or skill_id).strip(),
            description=description,
            bundle_hash=_bundle_hash(directory),
            references=references,
            assets=assets,
            scripts=scripts,
        )

    def _resource(self, directory: Path, raw: object, expected_root: str) -> SkillResource:
        if not isinstance(raw, dict):
            raise SkillRegistryError(f"{expected_root} 清单项必须是对象")
        identifier = str(raw.get("id") or "").strip()
        path = str(raw.get("path") or "").strip()
        self._safe_declared_path(directory, path, expected_root)
        return SkillResource(
            resource_id=identifier,
            path=path,
            description=str(raw.get("description") or "").strip(),
            media_type=str(raw.get("media_type") or "").strip()
            or mimetypes.guess_type(path)[0],
        )

    def _script(self, directory: Path, raw: object) -> SkillScript:
        if not isinstance(raw, dict):
            raise SkillRegistryError("scripts 清单项必须是对象")
        path = str(raw.get("path") or "").strip()
        self._safe_declared_path(directory, path, "scripts")
        permissions = raw.get("permissions") or {}
        if not isinstance(permissions, dict):
            raise SkillRegistryError("script.permissions 必须是对象")
        return SkillScript(
            entrypoint_id=str(raw.get("id") or "").strip(),
            path=path,
            description=str(raw.get("description") or "").strip(),
            runtime=str(raw.get("runtime") or "python"),
            timeout_seconds=int(raw.get("timeout_seconds") or 30),
            requires_approval=bool(raw.get("requires_approval", True)),
            read_data_files=bool(permissions.get("read_data_files", False)),
            read_assets=tuple(str(item) for item in permissions.get("read_assets", [])),
            write_artifacts=bool(permissions.get("write_artifacts", False)),
            network=bool(permissions.get("network", False)),
        )

    def _skill_directory(self, skill_id: str) -> Path:
        normalized = skill_id.strip()
        if not SKILL_ID_PATTERN.fullmatch(normalized):
            raise SkillRegistryError("Skill 标识不合法")
        candidate = (self.root / normalized).resolve(strict=False)
        if candidate.parent != self.root:
            raise SkillRegistryError("Skill 路径越界")
        return candidate

    @staticmethod
    def _safe_declared_path(directory: Path, raw_path: str, expected_root: str) -> Path:
        relative = Path(raw_path)
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise SkillRegistryError("Skill 资源路径必须是安全相对路径")
        if not relative.parts or relative.parts[0].casefold() != expected_root.casefold():
            raise SkillRegistryError(f"资源必须位于 {expected_root}/ 目录")
        target = (directory / relative).resolve(strict=True)
        root = directory.resolve(strict=True)
        if root not in target.parents or not target.is_file() or target.is_symlink():
            raise SkillRegistryError("Skill 资源不存在或路径越界")
        return target


def _parse_skill_markdown(path: Path) -> tuple[dict[str, object], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillRegistryError(f"无法读取 SKILL.md: {exc}") from exc
    if not text.startswith("---"):
        raise SkillRegistryError("SKILL.md 必须包含 YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillRegistryError("SKILL.md frontmatter 未闭合")
    metadata = yaml.safe_load(parts[1]) or {}
    if not isinstance(metadata, dict):
        raise SkillRegistryError("SKILL.md frontmatter 必须是对象")
    return metadata, parts[2].strip()


def _read_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SkillRegistryError(f"无法读取 DAAGENT.yaml: {exc}") from exc
    if not isinstance(value, dict) or value.get("version", 1) != 1:
        raise SkillRegistryError("DAAGENT.yaml 仅支持 version: 1")
    return value


def _manifest_list(manifest: dict[str, object], key: str) -> list[object]:
    value = manifest.get(key, [])
    if not isinstance(value, list):
        raise SkillRegistryError(f"DAAGENT.yaml 的 {key} 必须是列表")
    return value


def _require_unique(values: list[str], label: str) -> None:
    normalized = [value.casefold() for value in values]
    if any(not value for value in normalized) or len(normalized) != len(set(normalized)):
        raise SkillRegistryError(f"{label} 不能为空或重复")


def _bundle_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    files = [path for path in directory.rglob("*") if path.is_file() and not path.is_symlink()]
    if len(files) > MAX_ARCHIVE_FILES:
        raise SkillRegistryError("Skill 文件数量超过 500")
    for path in sorted(files, key=lambda item: item.relative_to(directory).as_posix()):
        if path.stat().st_size > MAX_MEMBER_BYTES:
            raise SkillRegistryError("Skill 包含超过 25 MB 的文件")
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
