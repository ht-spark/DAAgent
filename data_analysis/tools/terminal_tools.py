"""提供需要人工批准的项目终端执行能力。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..cancellation import kill_process_tree

# 单条命令最大 100,000 字符，避免超长提示意外占满沙箱请求，同时允许脚本型命令。
MAX_TERMINAL_COMMAND_CHARS = 100_000
# 工具内超时最多 7,190 秒，略短于服务端两小时沙箱上限，确保由本工具负责清理进程树。
MAX_TERMINAL_TIMEOUT_SECONDS = 7_190.0
# 返回给模型的终端尾部最多 2,000 行，与 Pi Agent 的默认截断策略一致。
MAX_TERMINAL_OUTPUT_LINES = 2_000
# 返回给模型的终端尾部最多 50 KiB；更长输出保留完整日志路径，避免挤占上下文。
MAX_TERMINAL_OUTPUT_BYTES = 50 * 1024
# 命令前后最多扫描 5,000 个项目文件，避免大型仓库的审计成为主要耗时。
MAX_TERMINAL_AUDIT_FILES = 5_000
# 文件变更列表最多返回 200 项，兼顾常规构建审计与模型上下文大小。
MAX_TERMINAL_CHANGED_FILES = 200
# Windows 终止进程树后可能短暂保留日志句柄；最多重试 10 次，每次间隔 50 毫秒。
TERMINAL_LOG_DELETE_RETRIES = 10
TERMINAL_LOG_DELETE_INTERVAL_SECONDS = 0.05

TERMINAL_TOOL_NAMES = frozenset({"run_terminal_command"})
TERMINAL_SHELLS = frozenset({"powershell", "bash", "git-bash"})
DEFAULT_TERMINAL_SHELL = "powershell"
_AUDIT_IGNORED_PARTS = frozenset(
    {".git", ".runtime", ".venv", "node_modules", "__pycache__", "outputs"}
)


def run_terminal_command(
    workspace_root: str | Path,
    command: str,
    *,
    shell: str = DEFAULT_TERMINAL_SHELL,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """在项目目录启动一次独立 Shell，并返回合并输出和文件变更审计。

    Args:
        workspace_root: 已由项目上下文绑定的工作目录。
        command: 交给所选 Shell 原样解释的命令文本。
        shell: ``powershell``、``bash`` 或 ``git-bash``。
        timeout_seconds: 可选的命令超时秒数；为空时只受外层沙箱上限约束。

    Returns:
        命令、实际 Shell、退出码、合并输出、完整日志位置和文件变更列表。

    Raises:
        ValueError: 参数或工作目录无效。
        FileNotFoundError: 请求的 Shell 在当前系统不存在。
        OSError: 无法启动 Shell 或读写输出日志。

    Note:
        本函数不自行决定授权。调用它的 Harness 必须在启动进程前完成人工审批。
    """
    root = Path(workspace_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("终端工作目录必须是现有目录")
    normalized_command = _validate_command(command)
    normalized_shell = _validate_shell(shell)
    normalized_timeout = _validate_timeout(timeout_seconds)
    argv, resolved_shell = _shell_argv(normalized_shell, normalized_command)

    before, before_truncated = _snapshot_project_files(root)
    log_path = _new_output_log_path()
    started_at = time.monotonic()
    timed_out = False
    exit_code = -1
    retain_log = False
    try:
        with log_path.open("wb") as output_file:
            process = subprocess.Popen(
                argv,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                env=_terminal_environment(),
                shell=False,
                **_process_group_options(),
            )
            try:
                exit_code = process.wait(timeout=normalized_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_tree(process)
        output, output_truncated = _read_output_tail(log_path)

        after, after_truncated = _snapshot_project_files(root)
        changed, changed_truncated = _changed_project_files(before, after)
        if timed_out:
            output = f"{output}\n命令运行超时，进程树已终止。".lstrip()
        result: dict[str, Any] = {
            "command": normalized_command,
            "shell": resolved_shell,
            "exit_code": exit_code,
            "output": output,
            "output_truncated": output_truncated,
            "timed_out": timed_out,
            "duration_ms": max(0, round((time.monotonic() - started_at) * 1000)),
            "changed_files": changed,
            "changes_truncated": (
                before_truncated or after_truncated or changed_truncated
            ),
        }
        if output_truncated:
            retain_log = True
            result["full_output_path"] = str(log_path)
        return result
    finally:
        if not retain_log:
            _remove_output_log(log_path)


def run_terminal_command_tool(
    arguments: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """校验工具参数后执行一次需要人工批准的终端命令。"""
    if set(arguments) - {"command", "timeout_seconds"}:
        raise ValueError("run_terminal_command 收到未声明参数")
    command = arguments.get("command")
    shell = context.get("terminal_shell", DEFAULT_TERMINAL_SHELL)
    timeout_seconds = arguments.get("timeout_seconds")
    workspace_root = context.get("workspace_root")
    if not isinstance(command, str):
        raise ValueError("run_terminal_command 的 command 必须是字符串")
    if not isinstance(shell, str):
        raise ValueError("run_terminal_command 缺少有效的终端类型设置")
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        raise ValueError("run_terminal_command 缺少项目目录上下文")
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
    ):
        raise ValueError("run_terminal_command 的 timeout_seconds 必须是数字")
    return run_terminal_command(
        workspace_root,
        command,
        shell=shell,
        timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None,
    )


def _validate_command(command: str) -> str:
    """拒绝空命令、NUL 字节和超过协议上限的命令。"""
    if not command.strip():
        raise ValueError("终端命令不能为空")
    if "\x00" in command:
        raise ValueError("终端命令不能包含 NUL 字节")
    if len(command) > MAX_TERMINAL_COMMAND_CHARS:
        raise ValueError(
            f"终端命令不能超过 {MAX_TERMINAL_COMMAND_CHARS:,} 个字符"
        )
    return command


def _validate_shell(shell: str) -> str:
    """规范化并校验 Shell 名称。"""
    normalized = shell.strip().lower()
    if normalized not in TERMINAL_SHELLS:
        raise ValueError("shell 只支持 powershell、bash 或 git-bash")
    return normalized


def _validate_timeout(timeout_seconds: float | None) -> float | None:
    """校验工具内超时，避免负值或越过外层沙箱硬上限。"""
    if timeout_seconds is None:
        return None
    if not 0 < timeout_seconds <= MAX_TERMINAL_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds 必须大于 0 且不超过 {MAX_TERMINAL_TIMEOUT_SECONDS:g}"
        )
    return timeout_seconds


def _shell_argv(shell: str, command: str) -> tuple[list[str], str]:
    """把 Shell 类型与命令转换为不经 Python shell 再解释的参数列表。"""
    if shell == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable is None:
            raise FileNotFoundError("当前环境找不到 PowerShell（pwsh 或 powershell）")
        utf8_command = (
            "$OutputEncoding = [Console]::OutputEncoding = "
            "[System.Text.UTF8Encoding]::new(); "
            f"{command}"
        )
        return [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            utf8_command,
        ], shell

    executable = _find_git_bash() if shell == "git-bash" else _find_bash()
    return [executable, "-c", command], shell


def _find_bash() -> str:
    """查找系统 Bash；Windows 下由 PATH 决定使用 WSL 或其他 Bash。"""
    if os.name != "nt" and Path("/bin/bash").is_file():
        return "/bin/bash"
    executable = shutil.which("bash")
    if executable is None:
        raise FileNotFoundError("当前环境找不到 Bash")
    return executable


def _find_git_bash() -> str:
    """只查找 Git for Windows 自带的 Bash，不回退到 WSL Bash。"""
    candidates: list[Path] = []
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    git = shutil.which("git")
    if git:
        candidates.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    candidates.extend(
        [
            Path("C:/Program Files/Git/bin/bash.exe"),
            Path("C:/Program Files (x86)/Git/bin/bash.exe"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("当前环境找不到 Git Bash")


def _terminal_environment() -> dict[str, str]:
    """构造终端子进程环境，并显式启用 UTF-8 与基础颜色能力。"""
    environment = dict(os.environ)
    environment.setdefault("TERM", "xterm-256color")
    environment.setdefault("COLORTERM", "truecolor")
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def _process_group_options() -> dict[str, Any]:
    """让终端进程拥有可被整体终止的独立进程组。"""
    if os.name == "nt":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        }
    return {"start_new_session": True}


def _new_output_log_path() -> Path:
    """为当前命令创建独立的完整输出日志文件。"""
    descriptor, raw_path = tempfile.mkstemp(
        prefix="daagent-terminal-",
        suffix=".log",
    )
    os.close(descriptor)
    return Path(raw_path)


def _remove_output_log(path: Path) -> None:
    """尽力清理输出日志，不让 Windows 句柄释放延迟覆盖命令结果。"""
    for attempt in range(TERMINAL_LOG_DELETE_RETRIES):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt + 1 < TERMINAL_LOG_DELETE_RETRIES:
                time.sleep(TERMINAL_LOG_DELETE_INTERVAL_SECONDS)


def _read_output_tail(path: Path) -> tuple[str, bool]:
    """按 Pi Agent 规则返回最多 2,000 行或 50 KiB 的输出尾部。"""
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size > MAX_TERMINAL_OUTPUT_BYTES:
            stream.seek(-MAX_TERMINAL_OUTPUT_BYTES, os.SEEK_END)
        data = stream.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    line_truncated = len(lines) > MAX_TERMINAL_OUTPUT_LINES
    if line_truncated:
        text = "".join(lines[-MAX_TERMINAL_OUTPUT_LINES:])
    return text, size > MAX_TERMINAL_OUTPUT_BYTES or line_truncated


def _snapshot_project_files(root: Path) -> tuple[dict[str, tuple[int, int]], bool]:
    """记录项目文件大小和修改时间，供终端命令执行前后审计。"""
    snapshot: dict[str, tuple[int, int]] = {}
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name.casefold() not in _AUDIT_IGNORED_PARTS
            and not (Path(current_root) / name).is_symlink()
        )
        for file_name in sorted(file_names):
            path = Path(current_root) / file_name
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[path.relative_to(root).as_posix()] = (
                stat.st_size,
                stat.st_mtime_ns,
            )
            if len(snapshot) >= MAX_TERMINAL_AUDIT_FILES:
                return snapshot, True
    return snapshot, False


def _changed_project_files(
    before: Mapping[str, tuple[int, int]],
    after: Mapping[str, tuple[int, int]],
) -> tuple[list[str], bool]:
    """比较终端执行前后快照，返回新增、删除或发生变化的路径。"""
    changed = sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )
    truncated = len(changed) > MAX_TERMINAL_CHANGED_FILES
    return changed[:MAX_TERMINAL_CHANGED_FILES], truncated
