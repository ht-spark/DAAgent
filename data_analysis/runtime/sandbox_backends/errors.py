"""沙箱后端错误分类。

设计说明（对齐《MXC 集成设计方案 V2.2》§4.3 兼容性契约）：
- ``CodeExecutionError`` 自 ``tools/code_tools.py`` 迁移至此，语义与错误分类不变，
  原位置保留 re-export 以兼容既有导入方（``tools/__init__.py`` 与测试）。
- 其余错误均继承 ``RuntimeError``，确保 Harness 的既有捕获链
  （``ToolNotFoundError, SandboxError, ValueError, RuntimeError``）能够把它们
  转换为失败的 Observation。
"""

from __future__ import annotations

__all__ = [
    "CodeExecutionError",
    "SandboxBackendError",
    "SandboxUnavailableError",
    "SandboxPolicyError",
    "SidecarProtocolError",
]


class CodeExecutionError(RuntimeError):
    """动态 Python 代码执行失败。"""


class SandboxBackendError(RuntimeError):
    """沙箱后端基础设施错误基类。"""


class SandboxUnavailableError(SandboxBackendError):
    """后端不可用：执行器缺失、探测失败或宿主能力不满足。"""


class SandboxPolicyError(SandboxBackendError):
    """当前策略不允许执行该请求（例如 local 后端 + mxc_required 隔离要求）。"""


class SidecarProtocolError(SandboxBackendError):
    """沙箱结果回传协议异常（sidecar 缺失、畸形或超出体积上限）。"""
