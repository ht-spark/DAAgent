"""subprocess 沙箱使用的内部工具执行入口。"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import JsonValue, TypeAdapter, ValidationError

_JSON_ADAPTER = TypeAdapter(JsonValue)
_ALLOWED_MODULE_PREFIX = "data_analysis.tools."


def main() -> int:
    """读取一次 JSON 请求，执行工具并输出一次 JSON 响应。"""
    try:
        request = json.loads(sys.stdin.read())
        entrypoint, arguments, context = _validate_request(request)
        handler = _load_handler(entrypoint)
        result = _JSON_ADAPTER.validate_python(handler(arguments, context))
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc) or "工具执行失败",
        }
    try:
        output = json.dumps(response, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        output = json.dumps(
            {
                "ok": False,
                "error_type": "SandboxProtocolError",
                "error": "工具结果无法序列化为 JSON",
            },
            ensure_ascii=False,
        )
    sys.stdout.write(output)
    return 0


def _validate_request(
    request: Any,
) -> tuple[str, Mapping[str, JsonValue], Mapping[str, JsonValue]]:
    if not isinstance(request, dict):
        raise ValueError("沙箱请求必须是 JSON 对象")
    entrypoint = request.get("entrypoint")
    arguments = request.get("arguments")
    context = request.get("context")
    if not isinstance(entrypoint, str) or not entrypoint:
        raise ValueError("沙箱请求缺少工具入口")
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    if not isinstance(context, dict):
        raise ValueError("工具上下文必须是 JSON 对象")
    try:
        validated_arguments = _JSON_ADAPTER.validate_python(arguments)
        validated_context = _JSON_ADAPTER.validate_python(context)
    except ValidationError as exc:
        raise ValueError("工具参数或上下文不是合法 JSON") from exc
    if not isinstance(validated_arguments, dict) or not isinstance(
        validated_context,
        dict,
    ):
        raise ValueError("工具参数和上下文必须是 JSON 对象")
    return entrypoint, validated_arguments, validated_context


def _load_handler(entrypoint: str) -> Callable[..., JsonValue]:
    module_name, separator, function_name = entrypoint.partition(":")
    if not separator or not function_name or "." in function_name:
        raise ValueError("工具入口格式无效")
    if not module_name.startswith(_ALLOWED_MODULE_PREFIX):
        raise PermissionError("沙箱只允许执行 data_analysis.tools 下的工具")
    module = importlib.import_module(module_name)
    handler = getattr(module, function_name, None)
    if not callable(handler):
        raise ValueError("工具入口不可调用")
    return handler


if __name__ == "__main__":
    raise SystemExit(main())
