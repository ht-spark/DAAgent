"""MXC 真机集成测试（Phase 1 验收，对齐设计方案 §9.1 测试矩阵核心行）。

无 ``vendor/mxc`` 资产或非 Windows 时自动跳过。
覆盖：基础执行与结果回传、只读数据 + 产物审计、越界写/读拒绝、
网络强制拒绝、承诺产物校验、提前退出协议错误。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MXC_EXECUTABLE = PROJECT_ROOT / "vendor" / "mxc" / "bin" / "x64" / "wxc-exec.exe"

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="MXC 集成测试仅运行在 Windows"),
    pytest.mark.skipif(
        not MXC_EXECUTABLE.is_file(),
        reason="缺少 vendor/mxc 资产（先运行 scripts/fetch_mxc.py）",
    ),
]

from data_analysis.tools.code_tools import (  # noqa: E402
    CodeExecutionError,
    execute_python_code,
)


@pytest.fixture()
def workdir(tmp_path: Path) -> dict:
    """准备输出目录与一份只读数据文件。"""
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    data_file = tmp_path / "sample.csv"
    data_file.write_text("name,value\nalpha,10\nbeta,20\n", encoding="utf-8")
    return {"output_dir": output_dir, "data_file": data_file, "root": tmp_path}


def _run(code: str, workdir: dict, *, expected: tuple[str, ...] = ()) -> dict:
    """经 mxc 后端执行一段代码。"""
    return execute_python_code(
        code,
        data_paths=[workdir["data_file"]],
        output_dir=workdir["output_dir"],
        expected_artifacts=list(expected),
        sandbox_backend="mxc",
    )


def test_mxc_basic_execution_and_sidecar(workdir: dict) -> None:
    """基础执行：stdout 透传、result 回传、runtime 反映真实后端与 tier。"""
    result = _run(
        "print('hello-mxc')\nresult = {'value': 21 * 2}",
        workdir,
    )
    assert "hello-mxc" in result["stdout"]
    assert result["result"] == {"value": 42}
    assert result["changed_artifacts"] == []
    runtime = result["runtime"]
    assert runtime["backend"] == "mxc"
    assert runtime["isolation_tier"] == "appcontainer-dacl"
    assert runtime["python_executable"].endswith("python.exe")


def test_mxc_readonly_data_and_artifact_audit(workdir: dict) -> None:
    """只读数据读取 + 产物写入 + 承诺校验 + SHA-256 描述。"""
    code = (
        "import pandas as pd\n"
        "df = pd.read_csv(DATA_FILES[0])\n"
        "df['double'] = df['value'] * 2\n"
        "df.to_csv(ARTIFACTS.path('result.csv'), index=False)\n"
        "result = {'rows': len(df), 'sum': int(df['value'].sum())}\n"
    )
    result = _run(code, workdir, expected=("result.csv",))
    assert result["result"] == {"rows": 2, "sum": 30}
    assert result["changed_artifacts"] == ["result.csv"]
    details = result["artifact_details"]
    assert details[0]["path"] == "result.csv"
    assert details[0]["media_type"] == "text/csv"
    assert len(details[0]["sha256"]) == 64
    # 产物内容逐字节核对
    content = (workdir["output_dir"] / "result.csv").read_text(encoding="utf-8")
    assert content.splitlines()[0] == "name,value,double"
    assert content.splitlines()[1] == "alpha,10,20"


def test_mxc_blocks_write_outside_authorized_dir(workdir: dict) -> None:
    """越界写入被 OS 级隔离拒绝（宿主侧确认文件不存在）。"""
    escape_target = workdir["root"] / "escape.txt"
    code = (
        "with open(" + repr(str(escape_target)) + ", 'w') as fh:\n"
        "    fh.write('should-not-exist')\n"
        "result = 'unexpected-success'\n"
    )
    with pytest.raises(CodeExecutionError) as exc_info:
        _run(code, workdir)
    assert "PermissionError" in str(exc_info.value)
    assert not escape_target.exists()


def test_mxc_blocks_read_outside_authorized_dir(workdir: dict) -> None:
    """越界读取同样被拒绝（授权模型对读、写同时生效）。"""
    secret = workdir["root"] / "secret.txt"
    secret.write_text("outside-authorization", encoding="utf-8")
    code = (
        "with open(" + repr(str(secret)) + ") as fh:\n"
        "    result = fh.read()\n"
    )
    with pytest.raises(CodeExecutionError) as exc_info:
        _run(code, workdir)
    assert "PermissionError" in str(exc_info.value)


def test_mxc_blocks_network_egress(workdir: dict) -> None:
    """网络默认拒绝：socket 连接抛出 PermissionError。"""
    code = (
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 443), timeout=3).close()\n"
        "result = 'unexpected-network-success'\n"
    )
    with pytest.raises(CodeExecutionError) as exc_info:
        _run(code, workdir)
    assert "PermissionError" in str(exc_info.value)


def test_mxc_expected_artifact_missing_fails(workdir: dict) -> None:
    """承诺产物未生成时任务判失败（与 local 后端语义一致）。"""
    with pytest.raises(CodeExecutionError) as exc_info:
        _run("result = 1", workdir, expected=("promised.csv",))
    assert "没有在当前运行目录生成承诺产物" in str(exc_info.value)


def test_mxc_early_exit_is_reported(workdir: dict) -> None:
    """用户代码提前终止进程（os._exit）时返回明确错误，不视为成功。"""
    with pytest.raises(CodeExecutionError) as exc_info:
        _run("import os\nos._exit(3)", workdir)
    assert "异常退出" in str(exc_info.value)
