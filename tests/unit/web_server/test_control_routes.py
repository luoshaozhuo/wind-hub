"""控制接口到 RPC gateway 的路由契约测试。"""

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from web_server.app import create_app, get_gateway


def test_collector_control_routes_forward_arguments() -> None:
    gateway = AsyncMock()
    gateway.collector_start_task.return_value = {"task_id": "task1"}
    gateway.collector_test_sink.return_value = {"success": True}
    app = create_app(write_token="test-key")
    app.dependency_overrides[get_gateway] = lambda: gateway
    with TestClient(app) as client:
        task = client.post("/api/v1/collector/tasks/task1/start", headers={"X-API-Key": "test-key"})
        sink = client.post("/api/v1/collector/sinks/csv/test", headers={"X-API-Key": "test-key"})

    assert task.status_code == 200
    assert sink.json() == {"success": True}
    gateway.collector_start_task.assert_awaited_once_with("task1")
    gateway.collector_test_sink.assert_awaited_once_with("csv")


def test_commander_bulk_read_requires_nonempty_points() -> None:
    gateway = AsyncMock()
    gateway.commander_read_many.return_value = {"values": []}
    app = create_app()
    app.dependency_overrides[get_gateway] = lambda: gateway
    with TestClient(app) as client:
        invalid = client.post(
            "/api/v1/commander/devices/d1/read-many", json={"point_ids": []}
        )
        valid = client.post(
            "/api/v1/commander/devices/d1/read-many", json={"point_ids": ["p1"]}
        )

    assert invalid.status_code == 422
    assert valid.status_code == 200
    gateway.commander_read_many.assert_awaited_once_with("d1", ["p1"])
