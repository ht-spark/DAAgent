"""基于 subprocess 的工具进程隔离演示实现。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from ..tools.registry import RegisteredTool
from ..cancellation import run_cancellable_process

__all__ = [
    "SandboxConfigurationError",
    "SandboxError",
    "SandboxProtocolError",
    "SandboxTimeoutError",
    "SandboxToolError",
    "SubprocessSandbox",
]


class SandboxError(RuntimeError):
    """subprocess 沙箱执行错误基类。"""


class SandboxConfigurationError(SandboxError):
    """沙箱或工具入口配置无效。"""


class SandboxTimeoutError(SandboxError):
    """工具执行超过允许时长。"""


class SandboxProtocolError(SandboxError):
    """沙箱子进程没有返回合法协议响应。"""


class SandboxToolError(SandboxError):
    """工具在沙箱子进程中执行失败。"""

    def __init__(self, error_type: str, message: str) -> None:
        """保存子进程中的错误类型和安全错误信息。"""
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type


class SubprocessSandbox:
    """使用独立 Python 子进程执行一个已注册工具。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        max_payload_bytes: int = 256_000,
        max_output_bytes: int = 2_000_000,
        python_executable: str | Path | None = None,
        working_directory: str | Path | None = None,
    ) -> None:
        """初始化 subprocess 沙箱。

        Args:
            timeout_seconds: 单次工具调用超时时间。
            max_payload_bytes: 发送给子进程的最大 JSON 字节数。
            max_output_bytes: 子进程标准输出的最大字节数。
            python_executable: 子进程使用的 Python，默认使用当前解释器。
            working_directory: 子进程工作目录，默认使用项目根目录。

        Raises:
            ValueError: 时间或字节限制不是正数。
            SandboxConfigurationError: 工作目录不存在。
        """
        if timeout_seconds <= 0:
            raise ValueError("沙箱超时时间必须大于 0")
        if max_payload_bytes <= 0 or max_output_bytes <= 0:
            raise ValueError("沙箱输入输出上限必须大于 0")
        default_root = Path(__file__).resolve().parents[2]
        workdir = Path(working_directory or default_root).resolve()
        if not workdir.is_dir():
            raise SandboxConfigurationError(f"沙箱工作目录不存在: {workdir}")
        self._timeout_seconds = timeout_seconds
        self._max_payload_bytes = max_payload_bytes
        self._max_output_bytes = max_output_bytes
        self._python_executable = str(python_executable or sys.executable)
        self._working_directory = workdir

    def execute(
        self,
        tool: RegisteredTool,
        arguments: Mapping[str, JsonValue],
        *,
        context_override: Mapping[str, JsonValue] | None = None,
        cancel_check: object | None = None,
    ) -> JsonValue:
        """在子进程中执行工具并返回 JSON 结果。

        Args:
            tool: 已由注册表解析的可信工具。
            arguments: LLM 生成且已经解析为对象的工具参数。
            context_override: 当前 Run 需要覆盖到工具的最新上下文。
            cancel_check: 用户请求终止时返回 True，立即杀死工具进程树。

        Returns:
            工具返回的 JSON 值。

        Raises:
            SandboxConfigurationError: 工具入口不可导入或请求无法序列化。
            SandboxTimeoutError: 工具执行超时。
            SandboxProtocolError: 子进程异常或响应无效。
            SandboxToolError: 工具自身执行失败。
        """
        try:
            entrypoint = tool.entrypoint
            payload = json.dumps(
                {
                    "entrypoint": entrypoint,
                    "arguments": dict(arguments),
                    "context": {**dict(tool.context), **dict(context_override or {})},
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise SandboxConfigurationError(str(exc)) from exc
        if len(payload.encode("utf-8")) > self._max_payload_bytes:
            raise SandboxConfigurationError("沙箱请求超过最大字节限制")

        command = [
            self._python_executable,
            "-m",
            "data_analysis.runtime._sandbox_worker",
        ]
        try:
            completed = run_cancellable_process(
                command,
                cancel_check=cancel_check,
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self._working_directory,
                env=_safe_environment(),
                timeout=self._timeout_seconds,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeoutError(
                f"工具执行超过 {self._timeout_seconds:g} 秒"
            ) from exc
        except OSError as exc:
            raise SandboxProtocolError(f"无法启动沙箱子进程: {exc}") from exc

        output_size = len(completed.stdout.encode("utf-8"))
        if output_size > self._max_output_bytes:
            raise SandboxProtocolError("沙箱响应超过最大字节限制")
        if completed.returncode != 0:
            error = completed.stderr.strip()[-500:] or "无错误输出"
            raise SandboxProtocolError(
                f"沙箱子进程异常退出，状态码 {completed.returncode}: {error}"
            )
        response = _parse_response(completed.stdout)
        if not response["ok"]:
            raise SandboxToolError(response["error_type"], response["error"])
        return response["result"]


def _parse_response(output: str) -> dict:
    try:
        response = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SandboxProtocolError("沙箱没有返回合法 JSON") from exc
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise SandboxProtocolError("沙箱响应结构无效")
    if response["ok"]:
        if "result" not in response:
            raise SandboxProtocolError("沙箱成功响应缺少结果")
    elif not isinstance(response.get("error_type"), str) or not isinstance(
        response.get("error"),
        str,
    ):
        raise SandboxProtocolError("沙箱失败响应缺少错误信息")
    return response


def _safe_environment() -> dict[str, str]:
    allowed_names = (
        "APPDATA",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    environment = {
        name: os.environ[name]
        for name in allowed_names
        if name in os.environ
    }
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment
