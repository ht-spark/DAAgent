"""执行已激活 Skill 声明的资源和脚本操作。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ..cancellation import run_cancellable_process
from .models import SkillAction, SkillContext, SkillMetadata, SkillObservation, SkillScript
from .registry import SkillRegistry

MAX_SCRIPT_INPUT_BYTES = 1 * 1024 * 1024
MAX_SCRIPT_OUTPUT_CHARS = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    ".csv", ".json", ".jsonl", ".md", ".markdown", ".py", ".txt", ".yaml", ".yml"
}


class SkillRuntime:
    """在独立于 Harness 的边界内处理 SkillAction。"""

    def __init__(
        self,
        registry: SkillRegistry,
        context: SkillContext,
        *,
        output_dir: str | Path,
        data_paths: list[str] | tuple[str, ...] = (),
        python_executable: str | Path = sys.executable,
        approval: Callable[[SkillAction], bool] | None = None,
        on_asset_materialized: Callable[[str, str, Path], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.registry = registry
        self.context = context
        self.output_dir = Path(output_dir).expanduser().resolve(strict=False)
        self.data_paths = tuple(str(Path(path).resolve(strict=True)) for path in data_paths)
        self.python_executable = str(python_executable)
        self.approval = approval
        self.on_asset_materialized = on_asset_materialized
        self.cancel_check = cancel_check

    def execute(self, action: SkillAction) -> SkillObservation:
        """执行一次受控 Skill 操作并把异常转换为标准观察。"""
        started = time.perf_counter()
        try:
            active = self.context.require(action.skill_id)
            if action.operation == "read_reference":
                result = self._read_text(active.metadata, "reference", action)
            elif action.operation == "read_asset":
                result = self._read_text(active.metadata, "asset", action)
            elif action.operation == "materialize_asset":
                result = self._materialize(active.metadata, action)
            else:
                result = self._run_script(active.metadata, action)
            return SkillObservation(
                operation=action.operation,
                skill_id=active.metadata.skill_id,
                success=True,
                resource_id=action.resource_id,
                entrypoint_id=action.entrypoint_id,
                result=result,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return SkillObservation(
                operation=action.operation,
                skill_id=action.skill_id,
                success=False,
                resource_id=action.resource_id,
                entrypoint_id=action.entrypoint_id,
                error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    def requires_approval(self, action: SkillAction) -> bool:
        """判断脚本入口是否要求本次人工批准。"""
        if action.operation != "run_script" or not action.entrypoint_id:
            return False
        active = self.context.require(action.skill_id)
        return self._script(active.metadata.scripts, action.entrypoint_id).requires_approval

    def _read_text(
        self,
        metadata: SkillMetadata,
        kind: str,
        action: SkillAction,
    ) -> dict[str, object]:
        if not action.resource_id:
            raise ValueError("读取 Skill 资源必须提供 resource_id")
        path = self.registry.resolve_resource(metadata, kind, action.resource_id)
        if path.suffix.casefold() not in TEXT_SUFFIXES:
            raise ValueError("该资源不是可注入上下文的文本，请使用 materialize_asset")
        text = path.read_text(encoding="utf-8")
        content = text[action.offset:action.offset + action.max_chars]
        next_offset = action.offset + len(content)
        truncated = next_offset < len(text)
        return {
            "content": content,
            "offset": action.offset,
            "next_offset": next_offset if truncated else None,
            "truncated": truncated,
        }

    def _materialize(
        self,
        metadata: SkillMetadata,
        action: SkillAction,
    ) -> dict[str, object]:
        if not action.resource_id:
            raise ValueError("复制 Skill 资产必须提供 resource_id")
        path = self.registry.resolve_resource(metadata, "asset", action.resource_id)
        target_dir = self.output_dir / ".skill-assets" / metadata.skill_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        shutil.copy2(path, target)
        if self.on_asset_materialized is not None:
            self.on_asset_materialized(metadata.skill_id, action.resource_id, target)
        resource = next(
            item for item in metadata.assets
            if item.resource_id.casefold() == action.resource_id.casefold()
        )
        return {
            "asset_id": action.resource_id,
            "runtime_path": str(target.resolve(strict=True)),
            "media_type": resource.media_type,
            "bundle_hash": metadata.bundle_hash,
        }

    def _run_script(self, metadata: SkillMetadata, action: SkillAction) -> object:
        if not action.entrypoint_id:
            raise ValueError("执行 Skill 脚本必须提供 entrypoint_id")
        script = self._script(metadata.scripts, action.entrypoint_id)
        if script.network:
            raise ValueError("当前运行时尚未提供可验证的网络隔离，拒绝 network: true")
        if script.requires_approval and (self.approval is None or not self.approval(action)):
            raise PermissionError("Skill 脚本未获得本次人工批准")
        entrypoint = self.registry.resolve_resource(metadata, "script", action.entrypoint_id)
        assets: dict[str, str] = {}
        for asset_id in script.read_assets:
            asset_action = SkillAction(
                operation="materialize_asset",
                skill_id=metadata.skill_id,
                resource_id=asset_id,
            )
            materialized = self._materialize(metadata, asset_action)
            assets[asset_id] = str(materialized["runtime_path"])
        artifact_dir = self.output_dir / "skill-artifacts" / metadata.skill_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if script.runtime == "python-cli":
            return self._run_cli_script(entrypoint, script, action, artifact_dir)
        payload = {
            "arguments": action.arguments,
            "runtime": {
                "data_files": list(self.data_paths) if script.read_data_files else [],
                "assets": assets,
                "output_dir": str(artifact_dir) if script.write_artifacts else None,
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_SCRIPT_INPUT_BYTES:
            raise ValueError("Skill 脚本输入超过 1 MB")
        process_environment = {
            key: os.environ[key]
            for key in (
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "PATH",
                "PATHEXT",
                "COMSPEC",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
            )
            if key in os.environ
        }
        process_environment["PYTHONIOENCODING"] = "utf-8"
        completed = run_cancellable_process(
            [self.python_executable, "-I", str(entrypoint)],
            timeout=script.timeout_seconds,
            cancel_check=self.cancel_check,
            input=encoded,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=str(entrypoint.parent),
            env=process_environment,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or "脚本执行失败").strip()[-2_000:]
            raise RuntimeError(detail)
        stdout = (completed.stdout or "").strip()
        if len(stdout) > MAX_SCRIPT_OUTPUT_CHARS:
            raise ValueError("Skill 脚本输出超过 2 MB")
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("Skill 脚本 stdout 必须且只能包含一个 JSON 对象") from exc
        if not isinstance(result, dict):
            raise ValueError("Skill 脚本输出必须是 JSON 对象")
        if result.get("ok") is not True:
            raise RuntimeError(str(result.get("error") or "Skill 脚本返回失败"))
        artifact_paths = self._validate_artifacts(
            result.get("artifacts"), artifact_dir, script
        )
        normalized = dict(result)
        normalized["artifacts"] = [
            path.relative_to(self.output_dir).as_posix()
            for path in artifact_paths
        ]
        normalized["artifact_details"] = [
            {
                "path": path.relative_to(self.output_dir).as_posix(),
                "absolute_path": str(path),
            }
            for path in artifact_paths
        ]
        return normalized

    def _run_cli_script(
        self,
        entrypoint: Path,
        script: SkillScript,
        action: SkillAction,
        artifact_dir: Path,
    ) -> dict[str, object]:
        """执行自动发现的传统命令行 Python 脚本。"""
        if set(action.arguments) != {"argv"}:
            raise ValueError("python-cli 脚本的 arguments 必须且只能包含 argv")
        argv = action.arguments.get("argv")
        if (
            not isinstance(argv, list)
            or len(argv) > 100
            or not all(isinstance(item, str) and len(item) <= 4_000 for item in argv)
        ):
            raise ValueError("argv 必须是不超过 100 项的字符串数组")
        normalized_argv = [self._validate_cli_argument(item) for item in argv]
        before = self._snapshot_files(self.output_dir)
        completed = run_cancellable_process(
            [self.python_executable, "-I", str(entrypoint), *normalized_argv],
            timeout=script.timeout_seconds,
            cancel_check=self.cancel_check,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=str(artifact_dir),
            env=self._process_environment(),
            shell=False,
        )
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            raise RuntimeError((stderr or stdout or "脚本执行失败")[-2_000:])
        if len(stdout) > MAX_SCRIPT_OUTPUT_CHARS:
            raise ValueError("Skill 脚本输出超过 2 MB")
        after = self._snapshot_files(self.output_dir)
        artifact_paths = [
            path
            for path, signature in after.items()
            if before.get(path) != signature and ".skill-assets" not in path.parts
        ]
        try:
            parsed: object = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = stdout
        return {
            "ok": True,
            "result": parsed,
            "stderr": stderr,
            "artifacts": [
                path.relative_to(self.output_dir).as_posix()
                for path in artifact_paths
            ],
            "artifact_details": [
                {
                    "path": path.relative_to(self.output_dir).as_posix(),
                    "absolute_path": str(path),
                }
                for path in artifact_paths
            ],
        }

    def _validate_cli_argument(self, value: str) -> str:
        """允许普通参数，但限制其中的绝对路径只能位于本轮授权范围。"""
        candidate_text = value.split("=", 1)[1] if value.startswith("-") and "=" in value else value
        candidate = Path(candidate_text).expanduser()
        if not candidate.is_absolute():
            return value
        resolved = candidate.resolve(strict=False)
        output_root = self.output_dir.resolve(strict=False)
        allowed_files = {Path(path).resolve(strict=True) for path in self.data_paths}
        if resolved not in allowed_files and resolved != output_root and output_root not in resolved.parents:
            raise ValueError(f"命令行参数包含未授权绝对路径: {candidate_text}")
        return value

    @staticmethod
    def _snapshot_files(root: Path) -> dict[Path, tuple[int, int]]:
        """记录输出目录中文件的大小和修改时间，用于识别脚本产物。"""
        return {
            path.resolve(strict=True): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    @staticmethod
    def _process_environment() -> dict[str, str]:
        """返回不包含凭据的最小子进程环境。"""
        environment = {
            key: os.environ[key]
            for key in (
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "PATH",
                "PATHEXT",
                "COMSPEC",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
            )
            if key in os.environ
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment

    @staticmethod
    def _script(scripts: tuple[SkillScript, ...], entrypoint_id: str) -> SkillScript:
        for script in scripts:
            if script.entrypoint_id.casefold() == entrypoint_id.casefold():
                return script
        raise ValueError(f"Skill 未声明脚本入口: {entrypoint_id}")

    @staticmethod
    def _validate_artifacts(raw: object, root: Path, script: SkillScript) -> list[Path]:
        if raw in (None, []):
            return []
        if not script.write_artifacts or not isinstance(raw, list):
            raise ValueError("脚本未获写产物权限或 artifacts 格式无效")
        resolved_root = root.resolve(strict=True)
        artifacts: list[Path] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError("脚本产物路径必须是字符串")
            relative = Path(item)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("脚本产物路径越界")
            target = (resolved_root / relative).resolve(strict=True)
            if resolved_root not in target.parents or not target.is_file():
                raise ValueError("脚本声明的产物不存在或越界")
            artifacts.append(target)
        return artifacts
