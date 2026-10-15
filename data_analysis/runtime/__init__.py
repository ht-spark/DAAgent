"""Agent 工具执行运行时。

导出按 PEP 562 惰性加载，避免工具、Agent 和运行时模块的导入链形成循环依赖；
首次访问后缓存于模块命名空间，行为与 direct import 等价。
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "Harness",
    "ExecutionContext",
    "FailureCircuitOpen",
    "FailureTracker",
    "MUTATING_TOOLS",
    "PythonEnvironment",
    "PythonEnvironmentError",
    "RepeatGuard",
    "RepeatNotice",
    "RepeatedToolFailureError",
    "SandboxConfigurationError",
    "SandboxError",
    "SandboxProtocolError",
    "SandboxTimeoutError",
    "SandboxToolError",
    "SubprocessSandbox",
    "TaskCancelledError",
    "invoke_cancellable",
    "kill_process_tree",
    "raise_if_cancelled",
    "resolve_python_environment",
    "run_cancellable_process",
]

# 属性名 → 相对模块路径（相对本包）
_LAZY_EXPORTS: dict[str, str] = {
    "TaskCancelledError": "..cancellation",
    "invoke_cancellable": "..cancellation",
    "kill_process_tree": "..cancellation",
    "raise_if_cancelled": "..cancellation",
    "run_cancellable_process": "..cancellation",
    "Harness": ".harness",
    "ExecutionContext": ".execution_context",
    "FailureCircuitOpen": ".failure_tracker",
    "FailureTracker": ".failure_tracker",
    "RepeatedToolFailureError": ".failure_tracker",
    "MUTATING_TOOLS": ".repeat_guard",
    "RepeatGuard": ".repeat_guard",
    "RepeatNotice": ".repeat_guard",
    "PythonEnvironment": "..python_environment",
    "PythonEnvironmentError": "..python_environment",
    "resolve_python_environment": "..python_environment",
    "SandboxConfigurationError": ".sandbox",
    "SandboxError": ".sandbox",
    "SandboxProtocolError": ".sandbox",
    "SandboxTimeoutError": ".sandbox",
    "SandboxToolError": ".sandbox",
    "SubprocessSandbox": ".sandbox",
}


def __getattr__(name: str) -> object:
    """按需加载并缓存运行时导出（PEP 562）。

    Raises:
        AttributeError: 名称不在公开导出清单中。
    """
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_path, __package__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """返回稳定的公开导出清单。"""
    return sorted(__all__)
