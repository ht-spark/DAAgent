"""可打断的任务取消原语。

协作式取消标志只能在边界生效；本模块提供：
  - 可杀死子进程树的进程执行
  - 可立即放弃等待的阻塞调用包装

供数据 Agent 与建模 Agent 共用，保证「终止」能尽快打断 LLM 与长命令。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import copy_context
from typing import Any

__all__ = [
    "TaskCancelledError",
    "invoke_cancellable",
    "kill_process_tree",
    "raise_if_cancelled",
    "run_cancellable_process",
]


class TaskCancelledError(RuntimeError):
    """用户请求终止任务时抛出，调用方应立即停止后续步骤。"""


def raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    """在关键边界检查取消意图。"""
    if cancel_check is not None and cancel_check():
        raise TaskCancelledError("任务已终止")


def kill_process_tree(proc: subprocess.Popen[Any]) -> None:
    """强杀进程及其子孙进程，避免终止后残留 npm/pytest/分析脚本。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        pass


def run_cancellable_process(
    command: Sequence[str],
    *,
    cancel_check: Callable[[], bool] | None = None,
    timeout: float | None = None,
    poll_interval: float = 0.2,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """启动子进程并在取消时立刻杀死进程树。

    Args:
        command: 不经 shell 解释的命令列表。
        cancel_check: 返回 True 表示用户已请求终止。
        timeout: 超时秒数；None 表示不限制（仍受取消检查约束）。
        poll_interval: 取消检查间隔秒数。
        **popen_kwargs: 透传给 ``subprocess.Popen``。

    Returns:
        与 ``subprocess.run`` 兼容的完成结果。

    Raises:
        TaskCancelledError: 用户请求终止。
        subprocess.TimeoutExpired: 超过 timeout。
        OSError: 进程无法启动。
    """
    raise_if_cancelled(cancel_check)
    # Popen 不接受 input=，这里拆出来交给 communicate
    stdin_data = popen_kwargs.pop("input", None)
    if stdin_data is not None:
        popen_kwargs.setdefault("stdin", subprocess.PIPE)
    if popen_kwargs.get("capture_output"):
        popen_kwargs.pop("capture_output")
        popen_kwargs.setdefault("stdout", subprocess.PIPE)
        popen_kwargs.setdefault("stderr", subprocess.PIPE)
    proc = subprocess.Popen(command, **popen_kwargs)
    deadline = None if timeout is None else time.monotonic() + timeout
    stdout: Any = None
    stderr: Any = None
    pending_input = stdin_data
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(input=pending_input, timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                # 超时后 stdin 可能已部分写入，重试时不再重复发送
                pending_input = None
                if cancel_check is not None and cancel_check():
                    kill_process_tree(proc)
                    raise TaskCancelledError("任务已终止") from None
                if deadline is not None and time.monotonic() >= deadline:
                    kill_process_tree(proc)
                    raise
    except TaskCancelledError:
        raise
    return subprocess.CompletedProcess(
        list(command),
        proc.returncode if proc.returncode is not None else -1,
        stdout,
        stderr,
    )


def invoke_cancellable(
    fn: Callable[[], Any],
    *,
    cancel_check: Callable[[], bool] | None = None,
    poll_interval: float = 0.25,
) -> Any:
    """执行阻塞调用，并在用户取消时立即返回控制权。

    实现方式：工作线程中执行 fn，主线程轮询取消标志。取消后抛出
    ``TaskCancelledError`` 并放弃等待后台线程（HTTP 请求会自行超时结束）。
    """
    if cancel_check is None:
        return fn()
    raise_if_cancelled(cancel_check)
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 透传给等待方
            error["exc"] = exc

    # 继承调用分支的追踪上下文，避免工具内部的模型调用丢失父节点。
    context = copy_context()
    thread = threading.Thread(target=lambda: context.run(_worker), daemon=True, name="cancellable-invoke")
    thread.start()
    while thread.is_alive():
        if cancel_check():
            raise TaskCancelledError("任务已终止")
        thread.join(timeout=poll_interval)
    if "exc" in error:
        raise error["exc"]
    return result.get("value")
