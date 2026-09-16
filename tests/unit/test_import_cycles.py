from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_server_main_imports_in_fresh_python_process() -> None:
    """服务入口在全新进程中导入时不得触发工具与运行时循环依赖。"""
    project_root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "-c", "from server.main import app; assert app is not None"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=project_root,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
