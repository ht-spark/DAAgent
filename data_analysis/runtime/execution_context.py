"""维护单次 Agent Run 可被所有工具继承的执行上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agent.loop import Action
from ..python_environment import PythonEnvironment, resolve_python_environment

__all__ = ["ExecutionContext"]


@dataclass
class ExecutionContext:
    """保存一次 Run 的目录与执行环境，并为工具调用自动补全环境。"""

    output_dir: Path
    workspace_root: Path | None = None
    python_environment: PythonEnvironment | None = None

    def __post_init__(self) -> None:
        self.output_dir = self.output_dir.expanduser().resolve(strict=False)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.workspace_root is not None:
            self.workspace_root = self.workspace_root.expanduser().resolve(strict=True)

    def tool_context(self) -> dict[str, Any]:
        """返回覆盖注册时静态值的最新工具上下文。"""
        environment = self.python_environment
        return {
            "output_dir": str(self.output_dir),
            "workspace_root": str(self.workspace_root) if self.workspace_root else "",
            "python_executable": str(environment.executable) if environment else "",
            "python_environment_source": environment.source if environment else "host",
            "python_environment_name": environment.name if environment else "MMAgent",
        }

    def inherit(self, action: Action) -> Action:
        """让 Python 相关工具默认继承 Run 已选定的环境。"""
        environment = self.python_environment
        if (
            action.tool_name not in {"execute_python", "run_project_command"}
            or "python_environment" in action.arguments
            or environment is None
            or environment.source == "host"
        ):
            return action
        arguments = dict(action.arguments)
        arguments["python_environment"] = str(environment.executable)
        return action.model_copy(update={"arguments": arguments})

    def select_python_environment(self, configured_path: str | Path) -> None:
        """校验并持久化本 Run 后续工具使用的 Python 环境。"""
        self.python_environment = resolve_python_environment(
            self.workspace_root,
            configured_path,
        )

    def refresh_project_environment(self) -> None:
        """环境创建命令成功后重新发现项目环境。"""
        if self.workspace_root is None:
            return
        environment = resolve_python_environment(self.workspace_root)
        if environment.source != "host":
            self.python_environment = environment