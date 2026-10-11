"""HTTP 写操作安全边界与 RPC 透传测试。"""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from web_server.app import create_app, get_gateway


def test_write_disabled_without_configuration() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/commander/commands",
            json={"command_id": "c1", "device_id": "d1", "point_id": "p1", "value": True},
        )
    assert response.status_code == 503


def test_write_rejects_incorrect_token() -> None:
    app = create_app(write_token="test-key")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/commander/commands",
            headers={"X-API-Key": "wrong"},
            json={"command_id": "c1", "device_id": "d1", "point_id": "p1", "value": 1},
        )
    assert response.status_code == 403


def test_write_preserves_command_id() -> None:
    gateway = AsyncMock()
    gateway.commander_write.return_value = {"command_id": "c1", "success": True}
    app = create_app(write_token="test-key")
    app.dependency_overrides[get_gateway] = lambda: gateway
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/commander/commands",
            headers={"X-API-Key": "test-key"},
            json={"command_id": "c1", "device_id": "d1", "point_id": "p1", "value": False},
        )
    assert response.status_code == 200
    gateway.commander_write.assert_awaited_once_with("c1", "d1", "p1", False, 1.0)


def test_batch_rejects_empty_commands() -> None:
    app = create_app(write_token="test-key")
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/commander/commands/batch",
            headers={"X-API-Key": "test-key"},
            json={"commands": []},
        )
    assert response.status_code == 422
