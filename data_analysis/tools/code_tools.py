"""供 Agent 动态生成并执行 Python 分析代码。"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Sequence

from pydantic import JsonValue, TypeAdapter, ValidationError

from .artifact_tools import ArtifactWorkspace

__all__ = ["CodeExecutionError", "execute_python_code"]

_JSON_ADAPTER = TypeAdapter(JsonValue)
_MAX_CAPTURE_CHARS = 100_000


class CodeExecutionError(RuntimeError):
    """动态 Python 代码执行失败。"""


def execute_python_code(
    code: str,
    *,
    data_paths: Sequence[str | Path],
    output_dir: str | Path,
) -> dict[str, JsonValue]:
    """执行模型生成的 Python 代码并收集观察结果。

    代码运行时可以使用 ``DATA_FILES``、``DATA_FILE_MAP``、``OUTPUT_DIR`` 和
    ``ARTIFACTS``。若需要向下一轮返回结构化结果，应给全局变量 ``result``
    赋值。生成文件时优先通过 ``ARTIFACTS.path`` 获取经过校验的目标路径。

    Args:
        code: 要运行的 Python 源代码。
        data_paths: 当前运行授权的数据文件路径。
        output_dir: 当前会话授权的产物目录。

    Returns:
        标准输出、标准错误、结构化结果及当前产物清单。

    Raises:
        ValueError: 代码为空或运行目录无效。
        CodeExecutionError: 代码编译或执行失败。
    """
    if not code.strip():
        raise ValueError("Python 代码不能为空")
    workspace = ArtifactWorkspace(output_dir)
    target_dir = workspace.root

    resolved_paths = [
        Path(path).expanduser().resolve(strict=False)
        for path in data_paths
    ]
    missing_paths = [path.name for path in resolved_paths if not path.is_file()]
    if missing_paths:
        raise FileNotFoundError(f"授权数据文件不存在: {', '.join(missing_paths)}")
    data_file_map = {path.name: str(path) for path in resolved_paths}
    namespace: dict[str, Any] = {
        "__name__": "__agent_code__",
        "DATA_FILES": [str(path) for path in resolved_paths],
        "DATA_FILE_MAP": data_file_map,
        "OUTPUT_DIR": str(target_dir),
        "ARTIFACTS": workspace,
        "result": None,
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    previous_directory = Path.cwd()
    try:
        compiled = compile(code, "<agent-generated-code>", "exec")
        os.chdir(target_dir)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exec(compiled, namespace, namespace)
    except (Exception, SystemExit, KeyboardInterrupt) as exc:
        captured = _captured_text(stdout, stderr)
        detail = f"{type(exc).__name__}: {exc}"
        if captured:
            detail = f"{detail}\n运行输出:\n{captured}"
        raise CodeExecutionError(detail) from exc
    finally:
        os.chdir(previous_directory)

    return {
        "stdout": _trim_text(stdout.getvalue()),
        "stderr": _trim_text(stderr.getvalue()),
        "result": _json_result(namespace.get("result")),
        "artifacts": workspace.list_artifacts(),
    }


def _json_result(value: Any) -> JsonValue:
    """优先保留结构化结果，无法序列化时返回有限长度文本。"""
    try:
        validated = _JSON_ADAPTER.validate_python(value)
        json.dumps(validated, ensure_ascii=False, allow_nan=False)
        return validated
    except (ValidationError, TypeError, ValueError):
        pass
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict(orient="records")
        except TypeError:
            converted = value.to_dict()
        try:
            validated = _JSON_ADAPTER.validate_python(converted)
            json.dumps(validated, ensure_ascii=False, allow_nan=False)
            return validated
        except (ValidationError, TypeError, ValueError):
            pass
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return _trim_text(text)


def _captured_text(stdout: io.StringIO, stderr: io.StringIO) -> str:
    parts = [
        text.strip()
        for text in (stdout.getvalue(), stderr.getvalue())
        if text.strip()
    ]
    return _trim_text("\n".join(parts))


def _trim_text(value: str) -> str:
    if len(value) <= _MAX_CAPTURE_CHARS:
        return value
    return value[:_MAX_CAPTURE_CHARS] + "\n…输出已截断"
