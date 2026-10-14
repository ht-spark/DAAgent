"""执行策略解析：从服务端配置（环境变量）构建 ``SandboxPolicy``。

对齐设计方案 §6.1/§6.2：
- 配置项全部来自服务端环境变量（受信任来源），LLM 与客户端输入不可覆盖；
- ``profile`` 区分 development / production，控制降级许可与最低隔离要求；
- 配置非法时 fail-fast（抛 ``ValueError``），不静默回退到不安全默认值。
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .base import ExecutionLimits, IsolationRequirement, SandboxPolicy

__all__ = [
    "SandboxSettings",
    "load_settings",
    "resolve_policy",
    "default_mxc_executable",
]

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_VALID_BACKENDS = frozenset({"local", "mxc", "auto"})
_VALID_PROFILES = frozenset({"development", "production"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class SandboxSettings:
    """服务端沙箱配置（从环境变量一次解析）。"""

    backend: str = "local"
    profile: str = "development"
    allow_local_fallback: bool = False
    require_supported_host: bool = False
    mxc_executable: Path | None = None

    def __post_init__(self) -> None:
        """校验取值合法性（fail-fast）。"""
        if self.backend not in _VALID_BACKENDS:
            raise ValueError(f"DAAGENT_SANDBOX_BACKEND 不支持: {self.backend}")
        if self.profile not in _VALID_PROFILES:
            raise ValueError(f"DAAGENT_SANDBOX_PROFILE 不支持: {self.profile}")


def default_mxc_executable() -> Path:
    """返回默认的 MXC 执行器路径（按当前架构选择 vendor 子目录）。"""
    machine = platform.machine().casefold()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    return _PROJECT_ROOT / "vendor" / "mxc" / "bin" / arch / "wxc-exec.exe"


def _parse_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    """解析布尔型环境变量，非法值直接报错。"""
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    normalized = str(raw).strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} 不是合法布尔值: {raw!r}")


def load_settings(environ: Mapping[str, str] | None = None) -> SandboxSettings:
    """从环境变量解析沙箱配置（默认读取 ``os.environ``）。

    Args:
        environ: 可选的环境映射，便于测试注入。

    Returns:
        已校验的 ``SandboxSettings``。

    Raises:
        ValueError: 任一配置项取值非法。
    """
    source = os.environ if environ is None else environ
    backend = (source.get("DAAGENT_SANDBOX_BACKEND") or "local").strip().casefold()
    profile = (source.get("DAAGENT_SANDBOX_PROFILE") or "development").strip().casefold()
    fallback = _parse_bool(source, "DAAGENT_SANDBOX_ALLOW_UNSAFE_FALLBACK", False)
    # 生产环境默认要求受支持宿主（可由显式配置覆盖，仅影响准入判定而非执行本身）
    require_supported = _parse_bool(
        source,
        "DAAGENT_MXC_REQUIRE_SUPPORTED_HOST",
        default=(profile == "production"),
    )
    raw_executable = (source.get("DAAGENT_MXC_EXECUTABLE") or "").strip()
    executable = Path(raw_executable).expanduser() if raw_executable else default_mxc_executable()
    return SandboxSettings(
        backend=backend,
        profile=profile,
        allow_local_fallback=fallback,
        require_supported_host=require_supported,
        mxc_executable=executable,
    )


def resolve_policy(settings: SandboxSettings) -> SandboxPolicy:
    """把沙箱配置解析为一次运行使用的 ``SandboxPolicy``。

    隔离要求推导（设计方案 §6.2 路由矩阵的输入）：
    - ``local`` 后端：``legacy_allowed``（仅兼容任务）；
    - ``mxc`` / ``auto`` 后端：开发环境 ``mxc_required``，
      生产环境 ``supported_mxc_required``。

    降级许可只在 development 且显式开启 ``ALLOW_UNSAFE_FALLBACK`` 时成立。
    """
    if settings.backend == "local":
        required: IsolationRequirement = "legacy_allowed"
    elif settings.profile == "production":
        required = "supported_mxc_required"
    else:
        required = "mxc_required"
    return SandboxPolicy(
        required_isolation=required,
        allow_local_fallback=(
            settings.allow_local_fallback and settings.profile == "development"
        ),
        allow_network=False,
        allow_child_processes=False,
        limits=ExecutionLimits(),
    )
