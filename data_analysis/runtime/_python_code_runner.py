"""使用目标 Python 解释器执行 Agent 代码的标准库 runner。"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_CAPTURE_CHARS = 100_000


class SafeArtifacts:
    """仅允许在指定产物目录内生成文件。"""

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve(strict=True)

    def path(self, relative_path: str) -> Path:
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
        return target

    def list_artifacts(self) -> list[str]:
        """列出产物目录内的全部文件。"""
        return sorted(self.snapshot())

    def snapshot(self) -> dict[str, tuple[int, int]]:
        """记录当前产物的大小与修改时间，用于识别本次代码真实改动。"""
        return {
            path.relative_to(self.root).as_posix(): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
            )
            for path in self.root.rglob("*")
            if path.is_file()
            and path.relative_to(self.root).parts[0].casefold() != ".runtime"
        }

    def describe(self, relative_paths: list[str]) -> list[dict[str, Any]]:
        """返回经过存在性、边界和非空校验的结构化产物元数据。"""
        details: list[dict[str, Any]] = []
        for relative_path in relative_paths:
            target = (self.root / relative_path).resolve(strict=True)
            if self.root not in target.parents or not target.is_file():
                raise ValueError(f"产物不在当前运行目录: {relative_path}")
            size = target.stat().st_size
            if size <= 0:
                raise ValueError(f"产物为空文件: {relative_path}")
            known_types = {
                ".csv": "text/csv",
                ".json": "application/json",
                ".md": "text/markdown",
                ".markdown": "text/markdown",
            }
            media_type = (
                known_types.get(target.suffix.lower())
                or mimetypes.guess_type(target.name)[0]
                or "application/octet-stream"
            )
            details.append({
                "path": relative_path,
                "media_type": media_type,
                "size_bytes": size,
            })
        return details


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
        before = artifacts.snapshot()
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
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
                _restrict_writes(output_dir),
            ):
                exec(compiled, namespace, namespace)
        finally:
            os.chdir(previous_directory)
        after = artifacts.snapshot()
        changed = sorted(
            relative_path
            for relative_path, signature in after.items()
            if before.get(relative_path) != signature
        )
        response = {
            "ok": True,
            "stdout": _trim(stdout.getvalue()),
            "stderr": _trim(stderr.getvalue()),
            "result": _json_result(namespace.get("result")),
            "artifacts": sorted(after),
            "changed_artifacts": changed,
            "artifact_details": artifacts.describe(changed),
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


@contextlib.contextmanager
def _restrict_writes(output_dir: Path):
    """把动态代码的文件变更限制在当前 Run 产物目录。"""
    root = output_dir.resolve(strict=True)
    original_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open

    def ensure_allowed(raw_path: Any) -> None:
        if isinstance(raw_path, int):
            return
        candidate = Path(os.fspath(raw_path)).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        resolved = candidate.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise PermissionError(f"动态代码禁止写入当前运行目录之外: {resolved}")

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if any(flag in mode for flag in "wax+"):
            ensure_allowed(file)
        return original_open(file, mode, *args, **kwargs)

    def guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any):
        if any(flag in mode for flag in "wax+"):
            ensure_allowed(file)
        return original_io_open(file, mode, *args, **kwargs)

    def guarded_os_open(file: Any, flags: int, *args: Any, **kwargs: Any):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        if flags & write_flags:
            ensure_allowed(file)
        return original_os_open(file, flags, *args, **kwargs)

    def one_path(function):
        def guarded(path: Any, *args: Any, **kwargs: Any):
            ensure_allowed(path)
            return function(path, *args, **kwargs)
        return guarded

    def two_paths(function):
        def guarded(source: Any, destination: Any, *args: Any, **kwargs: Any):
            ensure_allowed(source)
            ensure_allowed(destination)
            return function(source, destination, *args, **kwargs)
        return guarded

    def blocked_process(*_args: Any, **_kwargs: Any):
        raise PermissionError("动态 Python 禁止启动子进程；请使用受控项目命令工具")

    with contextlib.ExitStack() as stack:
        stack.enter_context(contextlib.redirect_stdout(sys.stdout))
        from unittest.mock import patch
        patches = [
            patch("builtins.open", guarded_open),
            patch("io.open", guarded_io_open),
            patch("os.open", guarded_os_open),
            patch("os.mkdir", one_path(os.mkdir)),
            patch("os.makedirs", one_path(os.makedirs)),
            patch("os.remove", one_path(os.remove)),
            patch("os.unlink", one_path(os.unlink)),
            patch("os.rmdir", one_path(os.rmdir)),
            patch("os.rename", two_paths(os.rename)),
            patch("os.replace", two_paths(os.replace)),
            patch("os.system", blocked_process),
            patch("os.popen", blocked_process),
            patch("subprocess.Popen", blocked_process),
            patch("subprocess.run", blocked_process),
            patch("subprocess.call", blocked_process),
            patch("subprocess.check_call", blocked_process),
            patch("subprocess.check_output", blocked_process),
        ]
        for current_patch in patches:
            stack.enter_context(current_patch)
        yield


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
