"""进程级异常守卫：单个请求抛出的 BaseException 不应带走整个后端。

背景：第三方库在清理索引/临时文件时可能抛出 SystemExit（属于 BaseException），
它不会被 except Exception 捕获，会一路冒到事件循环主线程并结束 uvicorn 进程，
表现为「点一下按钮后端就没了」。这里回归验证中间件的拦截行为。
"""
from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from server import main


def _raiser_factory(exc: BaseException) -> Callable[..., None]:
    """构造一个抛指定异常的替身函数。"""

    def _raiser(*_args: object, **_kwargs: object) -> None:
        raise exc

    return _raiser


class TestProcessLevelExceptionGuard:
    def test_system_exit_in_handler_becomes_500(self, monkeypatch):
        monkeypatch.setattr(
            main, "_build_knowledge_vector_index", _raiser_factory(SystemExit(1))
        )
        client = TestClient(main.app)

        response = client.post("/api/knowledge/chunk-embed", json={"window_size": 5})

        assert response.status_code == 500
        assert "已拦截" in response.json()["detail"]

    def test_service_still_alive_after_intercepted_exception(self, monkeypatch):
        """拦截后服务必须继续可用，而不是进程退出。"""
        monkeypatch.setattr(
            main, "_build_knowledge_vector_index", _raiser_factory(SystemExit(1))
        )
        client = TestClient(main.app)

        client.post("/api/knowledge/chunk-embed", json={"window_size": 5})

        # 无关请求仍应正常返回
        assert client.get("/api/settings/general").status_code == 200

    def test_regular_exception_still_propagates(self, monkeypatch):
        """普通异常不应被该守卫吞掉：仍按原有 400 语义返回。"""
        monkeypatch.setattr(
            main,
            "_build_knowledge_vector_index",
            _raiser_factory(RuntimeError("索引构建失败")),
        )
        client = TestClient(main.app)

        response = client.post("/api/knowledge/chunk-embed", json={"window_size": 5})

        assert response.status_code == 400
        assert "索引构建失败" in response.json()["detail"]
