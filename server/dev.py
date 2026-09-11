"""受控的本地开发服务器启动入口。

仅监听服务端与智能体源码，避免建模任务写入 ``artifacts/`` 时触发
Uvicorn 自动重载并中断后台任务。
"""
from __future__ import annotations

from pathlib import Path

import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELOAD_DIRS = (
    PROJECT_ROOT / "server",
    PROJECT_ROOT / "scr",
    PROJECT_ROOT / "data_analysis",
)
RELOAD_EXCLUDES = (PROJECT_ROOT / "artifacts",)


def run() -> None:
    """启动仅监听源码目录的本地开发服务器。"""
    uvicorn.run(
        "server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=[str(path) for path in RELOAD_DIRS],
        reload_excludes=[str(path) for path in RELOAD_EXCLUDES],
    )


if __name__ == "__main__":
    run()
