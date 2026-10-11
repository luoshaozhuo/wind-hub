"""Web API REST 到 RPC 的契约及故障测试。"""

from unittest.mock import AsyncMock

import grpc
from fastapi.testclient import TestClient

from web_server.app import create_app, get_gateway


def test_readonly_routes_forward_to_gateway() -> None:
    gateway = AsyncMock()
    gateway.collector_status.return_value = {"running": True}
    gateway.collector_latest_values.return_value = {"values": []}
    gateway.commander_read.return_value = {"point_id": "p1"}
    app = create_app()
    app.dependency_overrides[get_gateway] = lambda: gateway
    with TestClient(app) as client:
        status = client.get("/api/v1/collector/status")
        latest = client.get("/api/v1/collector/latest-values?device_id=d1")
        point = client.get("/api/v1/commander/devices/d1/points/p1")

    assert status.json() == {"running": True}
    assert latest.json() == {"values": []}
    assert point.json() == {"point_id": "p1"}
    gateway.collector_latest_values.assert_awaited_once_with("d1")
    gateway.commander_read.assert_awaited_once_with("d1", "p1")


def test_gateway_unavailable_maps_to_503() -> None:
    gateway = AsyncMock()
    error = grpc.aio.AioRpcError(
        grpc.StatusCode.UNAVAILABLE,
        initial_metadata=grpc.aio.Metadata(),
        trailing_metadata=grpc.aio.Metadata(),
        details="collector offline",
    )
    gateway.collector_status.side_effect = error
    app = create_app()
    app.dependency_overrides[get_gateway] = lambda: gateway
    with TestClient(app) as client:
        response = client.get("/api/v1/collector/status")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "UNAVAILABLE"
