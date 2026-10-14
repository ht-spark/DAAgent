"""MXC 沙箱内任务入口的宿主侧生成器。

设计说明（对齐设计方案 §5.1/§5.5）：
- 沙箱内执行的 ``task.py`` 是**完全静态**的 wrapper 源码，仅依赖标准库，
  不 import 项目包；全部参数经同目录 ``task_context.json`` 传入，避免任何
  字符串拼接注入。
- wrapper 负责：注入既有命名空间（``DATA_FILES`` / ``DATA_FILE_MAP`` /
  ``OUTPUT_DIR`` / ``WORKSPACE_DIR`` / ``SKILL_ASSETS`` / ``ARTIFACTS`` /
  ``result``）→ 执行 ``payload.py`` → 把结果写入 sidecar。
- **sidecar 是不可信业务输出**：宿主只把它当数据，最终执行状态以宿主观察
  （退出码/超时/取消）与产物校验为准（§5.5）。
- wrapper 不做 monkey-patch：写边界由 MXC 的 OS 级隔离承担，本文件的
  ``_SafeArtifacts`` 仅为兼容原有 ``ARTIFACTS.path()`` 用法的提示性边界。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = [
    "WRAPPER_SOURCE",
    "PAYLOAD_FILE_NAME",
    "WRAPPER_FILE_NAME",
    "CONTEXT_FILE_NAME",
    "SIDECAR_SCHEMA_VERSION",
    "write_task_files",
]

PAYLOAD_FILE_NAME = "payload.py"
WRAPPER_FILE_NAME = "task.py"
CONTEXT_FILE_NAME = "task_context.json"
SIDECAR_SCHEMA_VERSION = 1

# 沙箱内执行的静态 wrapper 源码（宿主原样写入控制目录，不做占位符替换）
#
# 注意：沙箱内只被授权目标目录本身，父级目录不可枚举（AppContainer 语义），
# 因此本源码**禁止使用 Path.resolve()/realpath**（会逐级解析父目录而触发
# PermissionError）；路径边界用纯字符串校验（normpath + parents 比较）实现，
# 强制边界由 MXC 的 OS 级隔离承担。
WRAPPER_SOURCE = '''\
"""MXC 沙箱内任务入口（宿主生成；静态模板，参数见同目录 task_context.json）。"""

import json
import os
from pathlib import Path


class _SafeArtifacts:
    """仅允许在指定产物目录内生成文件（提示性边界；强制边界由 MXC 承担）。

    根目录来自只读控制目录中的 task_context.json（宿主生成、沙箱不可篡改），
    路径边界使用纯字符串校验，不做文件系统级 realpath（沙箱内父目录不可枚举）。
    """

    def __init__(self, root: str) -> None:
        if not isinstance(root, str) or not root.strip():
            raise ValueError("产物根目录不能为空")
        self.root = Path(os.path.normpath(root))

    def path(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise ValueError("产物路径不能为空")
        candidate = Path(relative_path.strip())
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("产物路径必须是目录内的相对路径")
        if any(part.casefold() == ".runtime" for part in candidate.parts):
            raise ValueError("禁止写入运行时内部目录")
        target = Path(os.path.normpath(self.root / candidate))
        if target != self.root and self.root not in target.parents:
            raise ValueError("产物路径超出授权目录")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


def _json_result(value):
    """把常见分析结果转换为 JSON 值（与既有 runner 行为保持一致）。"""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value
    except (TypeError, ValueError):
        pass
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            converted = to_dict(orient="records")
        except TypeError:
            converted = to_dict()
        try:
            json.dumps(converted, ensure_ascii=False, allow_nan=False)
            return converted
        except (TypeError, ValueError):
            pass
    return json.dumps(value, ensure_ascii=False, default=str)[:100000]


def _write_sidecar(sidecar_path: str, payload):
    """把结构化结果写入 sidecar（不可信输出，仅供宿主做业务解析）。"""
    target = Path(sidecar_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    # __file__ 为宿主传入的绝对路径；直接取 parent（避免 realpath 枚举父目录）
    base = Path(__file__).parent
    context = json.loads((base / "task_context.json").read_text(encoding="utf-8"))
    sidecar = context["sidecar_path"]
    namespace = {
        "__name__": "__agent_code__",
        "DATA_FILES": list(context["data_files"]),
        "DATA_FILE_MAP": {Path(path).name: path for path in context["data_files"]},
        "OUTPUT_DIR": context["output_dir"],
        "WORKSPACE_DIR": context.get("workspace_root"),
        "SKILL_ASSETS": dict(context.get("skill_assets") or {}),
        "ARTIFACTS": _SafeArtifacts(context["output_dir"]),
        "result": None,
    }
    try:
        code = (base / "payload.py").read_text(encoding="utf-8")
        compiled = compile(code, "<agent-generated-code>", "exec")
        exec(compiled, namespace, namespace)
        _write_sidecar(sidecar, {
            "schema_version": 1,
            "run_id": context["run_id"],
            "ok": True,
            "result": _json_result(namespace.get("result")),
            "error_type": None,
            "error": None,
        })
    except (Exception, SystemExit, KeyboardInterrupt) as exc:
        _write_sidecar(sidecar, {
            "schema_version": 1,
            "run_id": context["run_id"],
            "ok": False,
            "result": None,
            "error_type": type(exc).__name__,
            "error": str(exc) or "Python 代码执行失败",
        })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_task_files(
    control_dir: Path,
    *,
    run_id: str,
    code: str,
    data_files: list[str],
    output_dir: str,
    workspace_root: str | None,
    skill_assets: dict[str, str],
    sidecar_path: str,
) -> None:
    """把沙箱任务所需的 payload / wrapper / context 三件套写入控制目录。

    Args:
        control_dir: 宿主私有控制目录（不得位于任何沙箱授权读写路径内）。
        run_id: 本次执行唯一标识（用于关联，非防伪凭证）。
        code: 用户生成的 Python 源码。
        data_files: 本任务授权的数据文件绝对路径列表。
        output_dir: 本任务唯一授权读写输出目录。
        workspace_root: 当前项目目录；未绑定时为 ``None``。
        skill_assets: 已物化的 Skill 资产标识到路径映射。
        sidecar_path: 结果回传文件路径（位于授权输出目录内）。
    """
    control_dir.mkdir(parents=True, exist_ok=True)
    (control_dir / PAYLOAD_FILE_NAME).write_text(code, encoding="utf-8")
    (control_dir / WRAPPER_FILE_NAME).write_text(WRAPPER_SOURCE, encoding="utf-8")
    context = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "run_id": run_id,
        "data_files": list(data_files),
        "output_dir": output_dir,
        "workspace_root": workspace_root,
        "skill_assets": dict(skill_assets),
        "sidecar_path": sidecar_path,
    }
    (control_dir / CONTEXT_FILE_NAME).write_text(
        json.dumps(context, ensure_ascii=False),
        encoding="utf-8",
    )
