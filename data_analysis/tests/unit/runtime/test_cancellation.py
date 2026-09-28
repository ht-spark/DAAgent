"""可打断取消原语的单元测试。"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from data_analysis.cancellation import (
    TaskCancelledError,
    invoke_cancellable,
    raise_if_cancelled,
    run_cancellable_process,
)


def test_raise_if_cancelled_triggers_on_true() -> None:
    with pytest.raises(TaskCancelledError):
        raise_if_cancelled(lambda: True)


def test_raise_if_cancelled_passes_when_false() -> None:
    raise_if_cancelled(lambda: False)
    raise_if_cancelled(None)


def test_run_cancellable_process_kills_on_cancel() -> None:
    """取消后应立即杀死长命令进程树。"""
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    started = time.monotonic()
    flag = {"cancelled": False}

    def cancel_after_03s() -> bool:
        # 0.3 秒后请求取消
        if not flag["cancelled"] and time.monotonic() - started > 0.3:
            flag["cancelled"] = True
        return flag["cancelled"]

    with pytest.raises(TaskCancelledError):
        run_cancellable_process(
            command,
            cancel_check=cancel_after_03s,
            timeout=20,
            poll_interval=0.05,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 5


def test_run_cancellable_process_returns_output() -> None:
    completed = run_cancellable_process(
        [sys.executable, "-c", "print('ok')"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0
    assert "ok" in (completed.stdout or "")


def test_invoke_cancellable_abandons_blocking_call() -> None:
    """取消后应立即返回控制权，不再等待阻塞调用。"""
    started = time.monotonic()
    flag = {"cancelled": False}

    def blocking() -> str:
        time.sleep(30)
        return "done"

    def cancel_check() -> bool:
        if not flag["cancelled"] and time.monotonic() - started > 0.2:
            flag["cancelled"] = True
        return flag["cancelled"]

    with pytest.raises(TaskCancelledError):
        invoke_cancellable(blocking, cancel_check=cancel_check, poll_interval=0.05)
    assert time.monotonic() - started < 5


def test_invoke_cancellable_returns_result() -> None:
    assert invoke_cancellable(lambda: 42, cancel_check=lambda: False) == 42


def test_invoke_cancellable_propagates_error() -> None:
    def boom() -> None:
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        invoke_cancellable(boom, cancel_check=lambda: False)
