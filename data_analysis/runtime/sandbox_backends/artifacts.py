"""宿主侧产物审计：快照、变化检测与结构化描述（设计方案 §5.6）。

与 ``_python_code_runner.py`` 内的沙箱内审计保持语义一致，但运行在宿主侧，
供 MXC 后端使用（local 后端沿用 runner 内既有实现，不做改动）：

- 以 ``size + mtime_ns`` 识别疑似变化；
- 对变化文件计算 SHA-256，用于可靠内容对比；
- 枚举时排除 ``.runtime`` 内部目录、符号链接与 Windows 重解析点（junction 等），
  并核对最终落点仍在授权目录内；
- 产物数量与总体积受 ``ExecutionLimits`` 约束，超限抛结构化错误。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from pathlib import Path

from .base import ExecutionLimits

__all__ = [
    "RUNTIME_DIR_NAME",
    "ArtifactLimitError",
    "normalize_relative_path",
    "snapshot_artifacts",
    "changed_artifacts",
    "describe_artifacts",
]

# 运行时内部目录名：不作为用户产物，不参与快照与枚举
RUNTIME_DIR_NAME = ".runtime"
# 枚举最大目录深度（防御异常目录结构导致的长耗时扫描）
_MAX_SCAN_DEPTH = 8

_KNOWN_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


class ArtifactLimitError(ValueError):
    """产物数量或体积超出策略上限。"""


def normalize_relative_path(relative_path: str) -> str:
    """校验并规范化产物相对路径。

    Raises:
        ValueError: 路径为空、为绝对路径、含 ``..`` 或指向运行时内部目录。
    """
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("产物路径不能为空")
    candidate = Path(relative_path.strip())
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("产物路径必须是目录内的相对路径")
    if any(part.casefold() == RUNTIME_DIR_NAME for part in candidate.parts):
        raise ValueError("禁止写入运行时内部目录")
    return candidate.as_posix()


def _is_link_or_reparse(path: Path) -> bool:
    """判断路径是否为符号链接或 Windows 重解析点（junction 等）。"""
    try:
        info = path.lstat()
    except OSError:
        return True  # 无法读取的条目一律视为不可信
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def snapshot_artifacts(root: Path) -> dict[str, tuple[int, int]]:
    """递归快照 ``root`` 下的常规文件，返回 ``{相对路径: (size, mtime_ns)}``。

    排除 ``.runtime`` 内部目录与符号链接/重解析点；只读扫描，不修改任何文件。
    """
    resolved_root = root.resolve(strict=True)
    snapshot: dict[str, tuple[int, int]] = {}

    def walk(directory: Path, depth: int) -> None:
        if depth > _MAX_SCAN_DEPTH:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if entry.name.casefold() == RUNTIME_DIR_NAME:
                    continue
                if _is_link_or_reparse(path):
                    continue
                walk(path, depth + 1)
                continue
            if not entry.is_file(follow_symlinks=False) or _is_link_or_reparse(path):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            snapshot[path.relative_to(resolved_root).as_posix()] = (
                info.st_size,
                info.st_mtime_ns,
            )

    walk(resolved_root, 0)
    return snapshot


def changed_artifacts(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> list[str]:
    """返回本次执行新增或修改的产物。

    与既有 runner 语义保持一致：只报告新增与修改，不报告删除。
    """
    return sorted(
        relative
        for relative, signature in after.items()
        if before.get(relative) != signature
    )


def _file_sha256(path: Path) -> str:
    """计算文件 SHA-256（分块读取，避免大文件占用内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_artifacts(
    root: Path,
    relative_paths: list[str],
    *,
    limits: ExecutionLimits | None = None,
    with_sha256: bool = True,
) -> list[dict]:
    """生成结构化产物元数据（存在性、边界、链接、非空与上限校验）。

    Args:
        root: 当前运行授权目录。
        relative_paths: 本次执行声明或检测到的相对路径列表。
        limits: 可选的产物数量/体积上限。
        with_sha256: 是否为每个产物计算 SHA-256。

    Returns:
        形如 ``{"path", "media_type", "size_bytes"[, "sha256"]}`` 的列表。

    Raises:
        ValueError: 路径越界、是链接/重解析点或为空文件。
        ArtifactLimitError: 超出数量或体积上限。
    """
    resolved_root = root.resolve(strict=True)
    details: list[dict] = []
    total_bytes = 0
    for relative_path in relative_paths:
        raw = resolved_root / relative_path
        if _is_link_or_reparse(raw):
            raise ValueError(f"产物为链接或重解析点，已拒绝: {relative_path}")
        target = raw.resolve(strict=True)
        if resolved_root not in target.parents or not target.is_file():
            raise ValueError(f"产物不在当前运行目录: {relative_path}")
        size = target.stat().st_size
        if size <= 0:
            raise ValueError(f"产物为空文件: {relative_path}")
        media_type = (
            _KNOWN_MEDIA_TYPES.get(target.suffix.lower())
            or mimetypes.guess_type(target.name)[0]
            or "application/octet-stream"
        )
        item: dict = {
            "path": relative_path,
            "media_type": media_type,
            "size_bytes": size,
        }
        if with_sha256:
            item["sha256"] = _file_sha256(target)
        details.append(item)
        total_bytes += size
    if limits is not None:
        if len(details) > limits.max_artifact_files:
            raise ArtifactLimitError(
                f"产物文件数超出上限（{len(details)} > {limits.max_artifact_files}）"
            )
        if total_bytes > limits.max_artifact_bytes:
            raise ArtifactLimitError(
                f"产物总体积超出上限（{total_bytes} > {limits.max_artifact_bytes} 字节）"
            )
    return details
