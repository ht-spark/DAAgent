"""维护单次 Agent Run 可被所有工具继承的执行上下文。"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    authorized_paths: list[Path] = field(default_factory=list)
    skill_assets: dict[str, str] = field(default_factory=dict)
    # 运行级沙箱后端标识（服务端注入；空串表示使用进程级默认配置）
    sandbox_backend: str = ""

    def __post_init__(self) -> None:
        self.output_dir = self.output_dir.expanduser().resolve(strict=False)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.workspace_root is not None:
            self.workspace_root = self.workspace_root.expanduser().resolve(strict=True)
        self.authorized_paths = [
            path.expanduser().resolve(strict=True)
            for path in self.authorized_paths
        ]

    def tool_context(self) -> dict[str, Any]:
        """返回覆盖注册时静态值的最新工具上下文。"""
        environment = self.python_environment
        context: dict[str, Any] = {
            "output_dir": str(self.output_dir),
            "workspace_root": str(self.workspace_root) if self.workspace_root else "",
            "python_executable": str(environment.executable) if environment else "",
            "python_environment_source": environment.source if environment else "host",
            "python_environment_name": environment.name if environment else "MMAgent",
        }
        # 仅有运行级新增授权时才覆盖 ToolRegistry 的静态附件路径。
        if self.authorized_paths:
            context["authorized_paths"] = [str(path) for path in self.authorized_paths]
        if self.skill_assets:
            context["skill_assets"] = dict(self.skill_assets)
        if self.sandbox_backend:
            context["sandbox_backend"] = self.sandbox_backend
        return context

    def register_skill_asset(
        self,
        skill_id: str,
        resource_id: str,
        path: str | Path,
    ) -> None:
        """把已物化的 Skill 资产加入普通工具的运行级授权上下文。"""
        target = Path(path).expanduser().resolve(strict=True)
        if self.output_dir not in target.parents or not target.is_file():
            raise ValueError("Skill 资产不在当前 Run 输出目录")
        if target not in self.authorized_paths:
            self.authorized_paths.append(target)
        self.skill_assets[f"{skill_id}:{resource_id}"] = str(target)

    def inherit(self, action: Action) -> Action:
        """让 Python 相关工具默认继承 Run 已选定的环境。"""
        environment = self.python_environment
        if (
            action.tool_name != "execute_python"
            or "python_environment" in action.arguments
            or environment is None
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
