"""跨服务配置阶段在失败时暴露状态且不会隐藏部分激活。"""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from web_server.app import create_app, get_gateway


def test_prepare_failure_aborts_both_candidates() -> None:
    gateway = AsyncMock()
    gateway.collector_prepare_config.return_value = {"success": True}
    gateway.commander_prepare_config.return_value = {"success": False}
    gateway.collector_abort_config.return_value = {"success": True}
    gateway.commander_abort_config.return_value = {"success": True}
    app = create_app(write_token="secret")
    app.dependency_overrides[get_gateway] = lambda: gateway
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/config/prepare",
            headers={"X-API-Key": "secret"},
            json={"revision_id": "r1", "config_hash": "hash"},
        )
    assert response.status_code == 409
    gateway.collector_abort_config.assert_awaited_once_with("r1")
    gateway.commander_abort_config.assert_awaited_once_with("r1")


def test_activation_reports_partial_failure_without_rollback() -> None:
    gateway = AsyncMock()
    gateway.collector_activate_config.return_value = {"success": True}
    gateway.commander_activate_config.return_value = {"success": False}
    app = create_app(write_token="secret")
    app.dependency_overrides[get_gateway] = lambda: gateway
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/config/activate",
            headers={"X-API-Key": "secret"},
            json={"revision_id": "r1"},
        )
    assert response.status_code == 409
    assert response.json()["detail"]["success"] is False
    assert response.json()["detail"]["results"]["collector"]["success"] is True
    gateway.collector_abort_config.assert_not_awaited()
