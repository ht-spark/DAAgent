"""通过 SkillRuntime 的 JSON 协议转换授权附件并返回 Markdown 产物。"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from data_analysis.tools.markitdown import convert_attachment_to_markdown
from server.document_tools import DocumentConversionError


logger = logging.getLogger(__name__)


def convert_request(payload: object) -> dict[str, Any]:
    """把技能请求映射到共享转换层，并声明完整 Markdown 产物。

    Args:
        payload: SkillRuntime 注入的 arguments 与可信 runtime 上下文。

    Returns:
        包含成功标记、分页结果和产物相对路径的 JSON 对象。

    Raises:
        ValueError: 请求结构、输出目录或转换参数非法。
        PermissionError: 附件未在本轮授权范围内。
        OSError: 源文件或产物无法读写。
        DocumentConversionError: 文档损坏、依赖缺失或没有提取到文本。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("Skill 请求必须是 JSON 对象")
    arguments = payload.get("arguments")
    runtime = payload.get("runtime")
    if not isinstance(arguments, Mapping) or not isinstance(runtime, Mapping):
        raise ValueError("Skill 请求必须包含 arguments 和 runtime 对象")
    data_files = runtime.get("data_files")
    output_dir = runtime.get("output_dir")
    if not isinstance(data_files, list) or not all(isinstance(path, str) for path in data_files):
        raise ValueError("runtime.data_files 必须是授权文件路径列表")
    if not isinstance(output_dir, str) or not output_dir.strip() or not Path(output_dir).is_absolute():
        raise ValueError("缺少有效的 runtime.output_dir，无法保存 Markdown 产物")

    result = convert_attachment_to_markdown(
        arguments,
        {"authorized_paths": data_files, "converted_dir": output_dir},
    )
    relative_path = Path(result["markdown_path"]).relative_to(Path(output_dir).resolve())
    return {"ok": True, "result": result, "artifacts": [relative_path.as_posix()]}


def main() -> None:
    """从标准输入读取一次请求，仅向标准输出写入一个 JSON 对象。"""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        payload = json.load(sys.stdin)
        # 第三方转换器可能输出诊断信息，统一转入 stderr，保持协议输出可解析。
        with contextlib.redirect_stdout(sys.stderr):
            response = convert_request(payload)
    except (ValueError, PermissionError, OSError, DocumentConversionError) as exc:
        logger.warning("文档转换失败：%s", exc)
        response = {"ok": False, "error": str(exc), "artifacts": []}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
