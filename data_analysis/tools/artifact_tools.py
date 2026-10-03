"""安全创建和管理 Agent 产物文件。"""

from __future__ import annotations

import io
import json
import os
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import JsonValue

__all__ = [
    "ArtifactExistsError",
    "ArtifactPathError",
    "ArtifactWorkspace",
    "MAX_ARTIFACT_CONTENT_BYTES",
    "UnsupportedArtifactFormatError",
    "write_artifact",
]

MAX_ARTIFACT_CONTENT_BYTES = 200_000
_TEXT_SUFFIXES = frozenset(
    {
        ".css",
        ".html",
        ".js",
        ".jsx",
        ".markdown",
        ".md",
        ".py",
        ".r",
        ".sql",
        ".tex",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_SUPPORTED_SUFFIXES = _TEXT_SUFFIXES | frozenset(
    {".csv", ".docx", ".json", ".xlsx"}
)


class ArtifactPathError(ValueError):
    """产物路径无效或越过授权目录。"""


class ArtifactExistsError(FileExistsError):
    """目标产物已存在且未允许覆盖。"""


class UnsupportedArtifactFormatError(ValueError):
    """请求的产物格式不受统一写入工具支持。"""


class ArtifactWorkspace:
    """限定在单次运行输出目录内的安全产物工作区。"""

    def __init__(self, output_dir: str | Path) -> None:
        """初始化并创建产物根目录。

        Args:
            output_dir: 当前会话授权的产物根目录。
        """
        self.root = Path(output_dir).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ArtifactPathError(f"产物目录无效: {self.root}")

    def path(
        self,
        relative_path: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """返回经过边界校验的产物路径并创建父目录。

        Args:
            relative_path: 相对于产物根目录的目标路径。
            overwrite: 是否允许目标文件已经存在。

        Returns:
            已验证的绝对路径。

        Raises:
            ArtifactPathError: 路径为空、为绝对路径、越界或指向输入目录。
            ArtifactExistsError: 文件存在且未允许覆盖。
        """
        raw_path = Path(relative_path)
        if raw_path.is_absolute() or not raw_path.parts or ".." in raw_path.parts:
            raise ArtifactPathError("产物路径必须是无 '..' 的非空相对路径")
        if raw_path.parts[0].casefold() in {"input", ".runs"}:
            raise ArtifactPathError("禁止写入或覆盖输入附件目录")
        target = (self.root / raw_path).resolve(strict=False)
        if target == self.root or self.root not in target.parents:
            raise ArtifactPathError("产物路径超出当前会话产物目录")
        if target.exists() and target.is_dir():
            raise ArtifactPathError("产物路径不能指向目录")
        if target.exists() and not overwrite:
            raise ArtifactExistsError(f"产物已存在: {raw_path.as_posix()}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def list_artifacts(self) -> list[str]:
        """返回工作区内除输入附件以外的全部文件。"""
        artifacts: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(self.root)
            if relative_path.parts[0].casefold() in {"input", ".runs"}:
                continue
            artifacts.append(relative_path.as_posix())
        return artifacts


def write_artifact(
    relative_path: str,
    content: JsonValue,
    *,
    output_dir: str | Path,
    overwrite: bool = False,
) -> dict[str, JsonValue]:
    """按文件扩展名将结构化内容写入安全产物目录。

    文本、代码和 Markdown 要求字符串内容；JSON 接受任意 JSON 值；CSV 和
    XLSX 接受记录数组、二维数组或列映射；DOCX 接受 Markdown 字符串。

    Args:
        relative_path: 相对于本轮产物目录的文件路径。
        content: 要写入的文本或 JSON 结构化内容。
        output_dir: 当前会话授权的产物目录。
        overwrite: 是否允许覆盖已有产物。

    Returns:
        已生成文件的名称、相对路径、格式、大小和覆盖状态。
    """
    _validate_content_size(content)
    workspace = ArtifactWorkspace(output_dir)
    target = workspace.path(relative_path, overwrite=overwrite)
    suffix = target.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        supported = "、".join(sorted(_SUPPORTED_SUFFIXES))
        raise UnsupportedArtifactFormatError(
            f"不支持产物格式 {suffix or '<无扩展名>'}，当前支持 {supported}"
        )

    existed = target.exists()
    temporary = target.with_name(
        f".{target.stem}.{uuid.uuid4().hex}.tmp{target.suffix}"
    )
    try:
        _write_by_suffix(temporary, suffix, content, workspace.root)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": target.relative_to(workspace.root).as_posix(),
        "name": target.name,
        "format": suffix.removeprefix("."),
        "size_bytes": target.stat().st_size,
        "overwritten": existed,
    }


def _validate_content_size(content: JsonValue) -> None:
    try:
        encoded = json.dumps(content, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("产物内容必须是合法 JSON 值") from exc
    if len(encoded) > MAX_ARTIFACT_CONTENT_BYTES:
        raise ValueError(
            f"产物内容不能超过 {MAX_ARTIFACT_CONTENT_BYTES} 字节"
        )


def _write_by_suffix(
    target: Path,
    suffix: str,
    content: JsonValue,
    output_dir: Path,
) -> None:
    if suffix in _TEXT_SUFFIXES:
        if not isinstance(content, str):
            raise TypeError(f"{suffix} 产物的 content 必须是字符串")
        target.write_text(content, encoding="utf-8")
        return
    if suffix == ".json":
        value = _normalized_json_content(content)
        target.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return
    if suffix == ".csv":
        _table_frame(content).to_csv(target, index=False, encoding="utf-8-sig")
        return
    if suffix == ".xlsx":
        sheets = _excel_sheets(content)
        with pd.ExcelWriter(target) as writer:
            for sheet_name, frame in sheets.items():
                frame.to_excel(writer, sheet_name=sheet_name, index=False)
        return
    if suffix == ".docx":
        if not isinstance(content, str):
            raise TypeError(".docx 产物的 content 必须是 Markdown 字符串")
        from .docx_writer import markdown_to_docx

        with redirect_stdout(io.StringIO()):
            markdown_to_docx(
                content,
                str(target),
                base_dir=str(output_dir),
            )


def _normalized_json_content(content: JsonValue) -> JsonValue:
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(".json 字符串内容必须是合法 JSON") from exc


def _table_frame(content: JsonValue) -> pd.DataFrame:
    if isinstance(content, list):
        return pd.DataFrame(content)
    if isinstance(content, dict):
        try:
            return pd.DataFrame(content)
        except ValueError:
            return pd.DataFrame([content])
    raise TypeError("表格产物的 content 必须是数组或对象")


def _excel_sheets(content: JsonValue) -> dict[str, pd.DataFrame]:
    if isinstance(content, dict) and set(content) == {"sheets"}:
        raw_sheets = content["sheets"]
        if not isinstance(raw_sheets, dict) or not raw_sheets:
            raise TypeError("XLSX 的 sheets 必须是非空对象")
        return {
            str(sheet_name): _table_frame(rows)
            for sheet_name, rows in raw_sheets.items()
        }
    return {"Sheet1": _table_frame(content)}
