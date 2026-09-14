"""为 Agent 提供受控的视觉模型调用。"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from .registry import ToolArgumentsError

__all__ = ["IMAGE_SUFFIXES", "inspect_image"]

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

    Raises:
        ToolArgumentsError: 参数、文件名或授权范围不符合约定。
        RuntimeError: 当前模型不能完成视觉调用。
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
    suffix = image_path.suffix.lower()
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    data_url = f"data:{_MIME_TYPES[suffix]};base64,{encoded}"
    content = [
        {
            "type": "text",
            "text": (
                "请直接查看图片并回答问题。应明确区分图片中可确认的内容与不确定内容；"
                "涉及表格、公式、坐标轴或约束条件时尽量准确转录。\n\n"
                f"问题：{question.strip()}"
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
