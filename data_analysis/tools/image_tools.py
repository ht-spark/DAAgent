"""为 Agent 提供受控的视觉模型调用。"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from .registry import ToolArgumentsError

__all__ = ["IMAGE_SUFFIXES", "inspect_generated_image", "inspect_image"]

IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
_MIME_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
VISION_UNSUPPORTED_ERROR = "当前模型不支持识图"


def inspect_image(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, Any],
) -> JsonValue:
    """使用当前模型分析一张已授权图片。

    Args:
        arguments: Agent 提供的文件名和分析问题。
        context: 包含当前聊天模型和本轮授权图片路径的运行上下文。

    Returns:
        包含文件名和模型分析文本的 JSON 结果。
    """
    if set(arguments) != {"file_name", "question"}:
        raise ToolArgumentsError("inspect_image 只接受必填参数 file_name 和 question")
    file_name = arguments.get("file_name")
    question = arguments.get("question")
    if not isinstance(file_name, str) or not file_name.strip():
        raise ToolArgumentsError("inspect_image 缺少有效的 file_name")
    if not isinstance(question, str) or not question.strip():
        raise ToolArgumentsError("inspect_image 缺少有效的 question")

    raw_paths = context.get("authorized_paths")
    llm = context.get("llm")
    if not isinstance(raw_paths, Sequence) or llm is None:
        raise RuntimeError(VISION_UNSUPPORTED_ERROR)
    image_path = _resolve_authorized_image(file_name.strip(), raw_paths)
    return _analyze_image(llm, image_path, question.strip())


def inspect_generated_image(
    arguments: Mapping[str, JsonValue],
    context: Mapping[str, Any],
) -> JsonValue:
    """使用当前模型检查一张已经生成的图片产物。

    Args:
        arguments: Agent 提供的产物相对路径和检查问题。
        context: 包含当前聊天模型和产物目录的运行上下文。

    Returns:
        包含产物路径和模型分析文本的 JSON 结果。
    """
    if set(arguments) != {"relative_path", "question"}:
        raise ToolArgumentsError(
            "inspect_generated_image 只接受必填参数 relative_path 和 question"
        )
    relative_path = arguments.get("relative_path")
    question = arguments.get("question")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ToolArgumentsError("inspect_generated_image 缺少有效的 relative_path")
    if not isinstance(question, str) or not question.strip():
        raise ToolArgumentsError("inspect_generated_image 缺少有效的 question")
    output_dir = context.get("output_dir")
    llm = context.get("llm")
    if not isinstance(output_dir, str) or not output_dir.strip() or llm is None:
        raise RuntimeError(VISION_UNSUPPORTED_ERROR)

    from .artifact_inspection import resolve_generated_artifact

    image_path = resolve_generated_artifact(output_dir, relative_path.strip())
    if image_path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ToolArgumentsError(f"产物不是支持的图片格式: {relative_path}")
    result = _analyze_image(llm, image_path, question.strip())
    result["path"] = image_path.relative_to(Path(output_dir).resolve()).as_posix()
    return result


def _analyze_image(llm: Any, image_path: Path, question: str) -> dict[str, str]:
    """把本地图片发送给当前模型并提取视觉分析文本。"""
    suffix = image_path.suffix.lower()
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    data_url = f"data:{_MIME_TYPES[suffix]};base64,{encoded}"
    content = [
        {
            "type": "text",
            "text": (
                "请直接查看图片并回答问题。应明确区分图片中可确认的内容与不确定内容；"
                "涉及表格、公式、坐标轴、图例或布局时尽量准确检查。\n\n"
                f"问题：{question}"
            ),
        },
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    try:
        response = llm.invoke([("user", content)])
        analysis = _response_text(response)
    except Exception as exc:  # 不向 Agent 暴露不同供应商的底层错误细节
        raise RuntimeError(VISION_UNSUPPORTED_ERROR) from exc
    if not analysis:
        raise RuntimeError(VISION_UNSUPPORTED_ERROR)
    return {"file_name": image_path.name, "analysis": analysis}


def _resolve_authorized_image(
    file_name: str,
    authorized_paths: Sequence[str | Path],
) -> Path:
    """按文件名解析唯一的已授权图片。"""
    matches = [
        Path(path).resolve()
        for path in authorized_paths
        if Path(path).name.casefold() == file_name.casefold()
    ]
    if not matches:
        raise ToolArgumentsError(f"图片未获授权或不存在: {file_name}")
    image_path = matches[0]
    if image_path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ToolArgumentsError(f"文件不是支持的图片格式: {file_name}")
    if not image_path.is_file():
        raise ToolArgumentsError(f"图片未获授权或不存在: {file_name}")
    return image_path


def _response_text(response: Any) -> str:
    """从常见 LangChain 响应结构提取文本。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "\n".join(part for part in parts if part).strip()
    return str(content).strip() if content is not None else ""
