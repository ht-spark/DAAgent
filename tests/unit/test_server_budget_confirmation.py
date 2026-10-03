"""旧建模运行接口移除后的兼容行为回归测试。"""

from fastapi.testclient import TestClient

from server import main


def test_removed_legacy_run_endpoint_returns_gone() -> None:
    """删除 scr 后，旧运行接口应明确返回 410，而不是导致服务导入失败。"""
    client = TestClient(main.app)

    response = client.post("/api/runs", json={})

    assert response.status_code == 410
    assert "统一 Agent" in response.json()["detail"]
