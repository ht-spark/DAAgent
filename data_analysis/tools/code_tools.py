"""供 Agent 动态生成并执行 Python 分析代码（经 SandboxService 路由）。

设计说明（对齐《MXC 集成设计方案 V2.2》§7 的递归陷阱提醒）：

- 实际执行逻辑已下沉至 ``runtime/sandbox_backends`` 的 local / MXC 后端；
  本模块只负责构建请求并路由，**不得回调自身**，避免形成递归调用；
- ``CodeExecutionError`` 自 ``sandbox_backends.errors`` 迁移并在此 re-export，
  保持 ``from data_analysis.tools import CodeExecutionError`` 的既有导入兼容；
- ``sandbox_backend`` 为运行级后端标识，仅允许由服务端（``ExecutionContext``）
  注入；为空时使用进程级配置（默认 ``local``，行为与迁移前完全一致）。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from pydantic import JsonValue

from ..runtime.sandbox_backends import (
    CodeExecutionError,
    PythonRunRequest,
    get_sandbox_service,
)

__all__ = ["CodeExecutionError", "execute_python_code"]

_DEFAULT_TIMEOUT_SECONDS = 7_100


def execute_python_code(
    code: str,
    *,
    data_paths: Sequence[str | Path],
    output_dir: str | Path,
    workspace_root: str | Path | None = None,
    python_executable: str | Path | None = None,
    environment_source: str = "host",
    environment_name: str = "MMAgent",
    expected_artifacts: Sequence[str] = (),
    skill_assets: Mapping[str, str] | None = None,
    sandbox_backend: str | None = None,
) -> dict[str, JsonValue]:
    """执行模型生成的 Python 代码并收集观察结果。

    代码运行时可以使用 ``DATA_FILES``、``DATA_FILE_MAP``、``OUTPUT_DIR`` 和
    ``ARTIFACTS``。若需要向下一轮返回结构化结果，应给全局变量 ``result``
    赋值。生成文件时优先通过 ``ARTIFACTS.path`` 获取经过校验的目标路径。

    Args:
        code: 要运行的 Python 源代码。
        data_paths: 当前运行授权的数据文件路径。
        output_dir: 当前会话授权的产物目录。
        workspace_root: 当前项目目录；为空表示会话未绑定项目。
        python_executable: 执行分析代码的 Python 解释器。
        environment_source: 环境来源，用于向 Agent 报告。
        environment_name: 环境显示名称，用于向 Agent 报告。
        expected_artifacts: 本次代码承诺创建或更新的相对产物路径。
        skill_assets: 已由 SkillRuntime 物化并授权的资产标识到路径映射。
        sandbox_backend: 可选运行级后端标识（``local`` / ``mxc`` / ``auto``），
            仅由服务端注入；为空时使用进程级配置。

    Returns:
        标准输出、标准错误、结构化结果及当前产物清单；``runtime`` 中包含
        实际生效的后端与隔离 tier 信息。

    Raises:
        ValueError: 代码为空或运行目录无效。
        CodeExecutionError: 代码编译或执行失败。
        SandboxPolicyError: 后端标识非法或策略不允许执行。
        SandboxUnavailableError: 所选后端不可用且不允许降级。
    """
    request = PythonRunRequest(
        run_id=uuid.uuid4().hex,
        code=code,
        data_paths=tuple(Path(path) for path in data_paths),
        output_dir=Path(output_dir),
        workspace_root=Path(workspace_root) if workspace_root is not None else None,
        python_executable=Path(python_executable or sys.executable),
        environment_source=environment_source,
        environment_name=environment_name,
        expected_artifacts=tuple(expected_artifacts),
        skill_assets=dict(skill_assets or {}),
        timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
    )
    return get_sandbox_service().run_python(request, backend=sandbox_backend)
