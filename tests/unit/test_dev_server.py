"""本地开发服务器重载范围测试。"""
from __future__ import annotations

from server import dev


def test_dev_server_watches_only_source_directories(monkeypatch):
    """产物目录不能作为自动重载监控目标。"""
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(dev.uvicorn, "run", fake_run)
    dev.run()

    reload_dirs = captured["kwargs"]["reload_dirs"]
    assert str(dev.PROJECT_ROOT / "server") in reload_dirs
    assert str(dev.PROJECT_ROOT / "scr") in reload_dirs
    assert str(dev.PROJECT_ROOT / "artifacts") not in reload_dirs
    assert captured["kwargs"]["reload_excludes"] == [
        str(dev.PROJECT_ROOT / "artifacts")
    ]
