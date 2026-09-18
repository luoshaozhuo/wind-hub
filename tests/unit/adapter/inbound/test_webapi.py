"""Unit tests for the Web API endpoints — exercised against mock services."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wind_hub.adapter.inbound.cli.context import AppContext, clear_context, set_context
from wind_hub.adapter.inbound.webapi.app import build_api
from wind_hub.domain.model.command import CommandResult
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.errors import CommandError
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.model.reload import ConfigDiff, ReloadResult
from wind_hub.domain.model.route import RouteDecision
from wind_hub.domain.port.inbound import SystemStatus


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_context()
    yield
    clear_context()


def _device_info(device_id: str = "d1") -> DeviceInfo:
    return DeviceInfo(device_id=device_id, protocol="modbus", connected=True)


def _install_context(
    *,
    command: AsyncMock | None = None,
    task: AsyncMock | None = None,
    query: AsyncMock | None = None,
    config: AsyncMock | None = None,
    router: MagicMock | None = None,
) -> AppContext:
    ctx = AppContext(
        command_service=command or AsyncMock(),
        task_service=task or AsyncMock(),
        query_service=query or AsyncMock(),
        config_service=config,
        router=router,
    )
    set_context(ctx)
    return ctx


def _client() -> TestClient:
    return TestClient(build_api())


def test_build_api_returns_fastapi() -> None:
    assert isinstance(build_api(), FastAPI)


def test_health_returns_200() -> None:
    task = AsyncMock()
    task.status.return_value = SystemStatus(
        running=True,
        device_count=3,
        sink_count=1,
        points_collected=42,
        points_routed=40,
        points_dropped=2,
    )
    _install_context(task=task)

    resp = _client().get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["device_count"] == 3
    # 决策 7：/health 暴露调度器点位统计
    assert body["points_collected"] == 42
    assert body["points_routed"] == 40
    assert body["points_dropped"] == 2


def test_devices_list() -> None:
    query = AsyncMock()
    query.list_devices.return_value = [_device_info("d1"), _device_info("d2")]
    _install_context(query=query)

    resp = _client().get("/devices")
    assert resp.status_code == 200
    assert [d["device_id"] for d in resp.json()] == ["d1", "d2"]


def test_device_detail() -> None:
    query = AsyncMock()
    query.get_device_info.return_value = _device_info("d1")
    _install_context(query=query)

    resp = _client().get("/devices/d1")
    assert resp.status_code == 200
    assert resp.json()["device_id"] == "d1"


def test_device_detail_unknown_returns_404() -> None:
    query = AsyncMock()
    query.get_device_info.side_effect = CommandError("unknown device", "")
    _install_context(query=query)

    resp = _client().get("/devices/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_point_read() -> None:
    query = AsyncMock()
    query.read_point.return_value = PointValue(
        device_id="d1", point_id="rotor.speed", value=1500.5, quality=Quality.GOOD, source="modbus"
    )
    _install_context(query=query)

    resp = _client().get("/points/d1/rotor.speed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["value"] == 1500.5
    assert body["quality"] == "good"


def test_send_command_returns_command_response() -> None:
    command = AsyncMock()
    command.send.return_value = CommandResult(command_id="c1", success=True)
    _install_context(command=command)

    resp = _client().post(
        "/commands", json={"device_id": "d1", "point_id": "setpoint", "value": 42.5}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["command_id"] == "c1"
    assert body["success"] is True


def test_send_command_generates_id_when_omitted() -> None:
    import uuid

    command = AsyncMock()
    command.send.return_value = CommandResult(command_id="server-generated", success=True)
    _install_context(command=command)

    resp = _client().post("/commands", json={"device_id": "d1", "point_id": "p", "value": 1})
    assert resp.status_code == 200
    sent = command.send.call_args.args[0]
    # The request must carry a freshly-generated UUID idempotency key.
    assert uuid.UUID(sent.command_id)  # raises ValueError if not a valid UUID


def test_send_command_preserves_provided_id() -> None:
    command = AsyncMock()
    command.send.return_value = CommandResult(command_id="my-id", success=True)
    _install_context(command=command)

    resp = _client().post(
        "/commands", json={"device_id": "d1", "point_id": "p", "value": 1, "command_id": "my-id"}
    )
    assert resp.status_code == 200
    sent = command.send.call_args.args[0]
    assert sent.command_id == "my-id"


def test_send_command_bad_request_returns_400() -> None:
    _install_context()
    resp = _client().post("/commands", json={"point_id": "p", "value": 1})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_config_reload_returns_200() -> None:
    config = AsyncMock()
    config.reload.return_value = ReloadResult(
        success=True, diff=ConfigDiff(), errors=[], duration_ms=1.0
    )
    _install_context(config=config)

    resp = _client().post("/config/reload")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["devices_added"] == []
    assert body["routing_rebuilt"] is False


def test_route_explain_returns_decision() -> None:
    router = MagicMock()
    router.explain.return_value = RouteDecision(
        device_id="d1",
        point_id="rotor.speed",
        targets=["s1"],
        matched_rule="default",
        source="rule",
    )
    _install_context(router=router)

    resp = _client().get("/routes/explain", params={"device_id": "d1", "point_id": "rotor.speed"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["targets"] == ["s1"]
    assert body["source"] == "rule"
