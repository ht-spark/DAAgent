"""使用目标 Python 解释器执行 Agent 代码的标准库 runner。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from typing import Any

MAX_CAPTURE_CHARS = 100_000


class SafeArtifacts:
    """仅允许在指定产物目录内生成文件。"""

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve(strict=True)

    def path(self, relative_path: str) -> str:
        """返回经过目录边界校验的产物路径。"""
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("产物路径不能为空")
        candidate = Path(relative_path.strip())
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("产物路径必须是目录内的相对路径")
        target = (self.root / candidate).resolve(strict=False)
        if target != self.root and self.root not in target.parents:
            raise ValueError("产物路径超出授权目录")
        target.parent.mkdir(parents=True, exist_ok=True)
        return str(target)

    def list_artifacts(self) -> list[str]:
        """列出产物目录内的全部文件。"""
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        )


def main() -> int:
    """读取 JSON 请求，执行代码并输出一次 JSON 响应。"""
    original_stdout = sys.stdout
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        request = json.loads(sys.stdin.read())
        code = request["code"]
        output_dir = Path(request["output_dir"]).resolve(strict=True)
        data_files = [str(Path(path).resolve(strict=True)) for path in request["data_files"]]
        artifacts = SafeArtifacts(str(output_dir))
        namespace: dict[str, Any] = {
            "__name__": "__agent_code__",
            "DATA_FILES": data_files,
            "DATA_FILE_MAP": {Path(path).name: path for path in data_files},
            "OUTPUT_DIR": str(output_dir),
            "WORKSPACE_DIR": request.get("workspace_root"),
            "ARTIFACTS": artifacts,
            "result": None,
        }
        compiled = compile(code, "<agent-generated-code>", "exec")
        previous_directory = Path.cwd()
        try:
            os.chdir(output_dir)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exec(compiled, namespace, namespace)
        finally:
            os.chdir(previous_directory)
        response = {
            "ok": True,
            "stdout": _trim(stdout.getvalue()),
            "stderr": _trim(stderr.getvalue()),
            "result": _json_result(namespace.get("result")),
            "artifacts": artifacts.list_artifacts(),
            "runtime": {
                "python_executable": sys.executable,
                "python_version": ".".join(map(str, sys.version_info[:3])),
            },
        }
    except (Exception, SystemExit, KeyboardInterrupt) as exc:
        captured = _captured_text(stdout, stderr)
        response = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc) or "Python 代码执行失败",
            "captured": captured,
        }
    original_stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False))
    return 0


def _json_result(value: Any) -> Any:
    """将常见分析结果转换成 JSON 值。"""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value
    except (TypeError, ValueError):
        pass
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict(orient="records")
        except TypeError:
            converted = value.to_dict()
        try:
            json.dumps(converted, ensure_ascii=False, allow_nan=False)
            return converted
        except (TypeError, ValueError):
            pass
    return _trim(json.dumps(value, ensure_ascii=False, default=str))


def _captured_text(stdout: io.StringIO, stderr: io.StringIO) -> str:
    return _trim("\n".join(
        text.strip()
        for text in (stdout.getvalue(), stderr.getvalue())
        if text.strip()
    ))


def _trim(value: str) -> str:
    if len(value) <= MAX_CAPTURE_CHARS:
        return value
    return value[:MAX_CAPTURE_CHARS] + "\n…输出已截断"


if __name__ == "__main__":
    raise SystemExit(main())
