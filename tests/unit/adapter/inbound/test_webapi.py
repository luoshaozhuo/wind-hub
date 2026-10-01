"""Unit tests for the Web API endpoints — exercised against mock services."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.application.app_context import AppContext, clear_context, set_context
from wind_hub.application.runtime.task_instance import TaskInstanceState
from wind_hub.application.usecase import (
    SystemStatus,
    TaskBatchResult,
    TaskDetail,
    TaskInstanceDetail,
)
from wind_hub.domain.model.command import CommandResult
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.errors import CommandError
from wind_hub.domain.model.point import PointValue, Quality
from wind_hub.domain.model.reload import ConfigDiff, ReloadResult, TaskDiff


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
    query: AsyncMock | None = None,
    config: AsyncMock | None = None,
    tasks: AsyncMock | None = None,
    runtime: AsyncMock | None = None,
) -> AppContext:
    """AppContext 新模型：command/query/config/tasks/runtime（无 route_query/jobs）。"""
    ctx = AppContext(
        command=command or AsyncMock(),
        query=query or AsyncMock(),
        config=config,
        tasks=tasks,
        runtime=runtime,
    )
    set_context(ctx)
    return ctx


def _client() -> TestClient:
    return TestClient(build_api())


def test_build_api_returns_fastapi() -> None:
    assert isinstance(build_api(), FastAPI)


def test_health_returns_200() -> None:
    query = AsyncMock()
    query.status.return_value = SystemStatus(
        running=True,
        device_count=3,
        sink_count=1,
        points_collected=42,
        points_routed=40,
        points_dropped=2,
    )
    _install_context(query=query)

    resp = _client().get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["device_count"] == 3
    # 决策 7：/health 暴露运行时点位统计
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


# ---------------------------------------------------------------------------
# /config/reload —— tasks diff 取代 routing_rebuilt
# ---------------------------------------------------------------------------


def test_config_reload_returns_tasks_diff() -> None:
    config = AsyncMock()
    config.reload.return_value = ReloadResult(
        success=True,
        diff=ConfigDiff(tasks=TaskDiff(added=["t2"], removed=["t0"], updated=["t1"])),
        errors=[],
        duration_ms=1.0,
    )
    _install_context(config=config)

    resp = _client().post("/config/reload")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["tasks_added"] == ["t2"]
    assert body["tasks_removed"] == ["t0"]
    assert body["tasks_updated"] == ["t1"]
    assert body["devices_added"] == []
    # 旧路由/处理链模型字段已删除
    assert "routing_rebuilt" not in body
    assert "pipeline_rebuilt" not in body


def test_config_reload_unavailable_returns_503() -> None:
    _install_context(config=None)

    resp = _client().post("/config/reload")
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /tasks —— 采集 Task / Task Instance 生命周期
# ---------------------------------------------------------------------------

_INSTANCE_ID = "fast:d1"


def _task_detail(task_id: str = "fast") -> TaskDetail:
    return TaskDetail(
        task_id=task_id,
        device="d1",
        device_group=None,
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        enabled=True,
    )


def _instance_detail(
    instance_id: str = _INSTANCE_ID, state: TaskInstanceState = TaskInstanceState.STOPPED
) -> TaskInstanceDetail:
    return TaskInstanceDetail(
        instance_id=instance_id,
        task_id="fast",
        device_id="d1",
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        state=state,
    )


def test_tasks_list() -> None:
    tasks = AsyncMock()
    tasks.list_tasks.return_value = [_task_detail()]
    _install_context(tasks=tasks)

    resp = _client().get("/tasks")

    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["task_id"] == "fast"
    assert body[0]["device"] == "d1"
    assert body[0]["device_group"] is None
    assert body[0]["point_group"] == "fast"
    assert body[0]["interval"] == 1.0
    assert body[0]["targets"] == ["archive"]
    assert body[0]["enabled"] is True


def test_tasks_instances_list() -> None:
    tasks = AsyncMock()
    tasks.list_instances.return_value = [_instance_detail()]
    _install_context(tasks=tasks)

    resp = _client().get("/tasks/instances")

    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["instance_id"] == _INSTANCE_ID
    assert body[0]["task_id"] == "fast"
    assert body[0]["device_id"] == "d1"
    assert body[0]["point_group"] == "fast"
    assert body[0]["state"] == "stopped"


def test_tasks_instance_detail() -> None:
    tasks = AsyncMock()
    tasks.get_instance.return_value = _instance_detail(state=TaskInstanceState.RUNNING)
    _install_context(tasks=tasks)

    resp = _client().get(f"/tasks/instances/{_INSTANCE_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["instance_id"] == _INSTANCE_ID
    assert body["state"] == "running"
    tasks.get_instance.assert_awaited_once_with(_INSTANCE_ID)


def test_tasks_instance_detail_unknown_returns_404() -> None:
    tasks = AsyncMock()
    tasks.get_instance.side_effect = KeyError("nope:d1")
    _install_context(tasks=tasks)

    resp = _client().get("/tasks/instances/nope:d1")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_tasks_instance_start() -> None:
    tasks = AsyncMock()
    tasks.start_instance.return_value = _instance_detail(state=TaskInstanceState.RUNNING)
    _install_context(tasks=tasks)

    resp = _client().post(f"/tasks/instances/{_INSTANCE_ID}/start")

    assert resp.status_code == 200
    assert resp.json()["state"] == "running"
    tasks.start_instance.assert_awaited_once_with(_INSTANCE_ID)


def test_tasks_instance_start_unknown_returns_404() -> None:
    tasks = AsyncMock()
    tasks.start_instance.side_effect = KeyError("nope:d1")
    _install_context(tasks=tasks)

    resp = _client().post("/tasks/instances/nope:d1/start")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_tasks_instance_stop() -> None:
    tasks = AsyncMock()
    tasks.stop_instance.return_value = _instance_detail()
    _install_context(tasks=tasks)

    resp = _client().post(f"/tasks/instances/{_INSTANCE_ID}/stop")

    assert resp.status_code == 200
    assert resp.json()["state"] == "stopped"
    tasks.stop_instance.assert_awaited_once_with(_INSTANCE_ID)


def test_tasks_instance_stop_unknown_returns_404() -> None:
    tasks = AsyncMock()
    tasks.stop_instance.side_effect = KeyError("nope:d1")
    _install_context(tasks=tasks)

    resp = _client().post("/tasks/instances/nope:d1/stop")

    assert resp.status_code == 404


def test_tasks_start_all() -> None:
    tasks = AsyncMock()
    tasks.start_all_instances.return_value = TaskBatchResult(total=3, changed=2, unchanged=1)
    _install_context(tasks=tasks)

    resp = _client().post("/tasks/start-all")

    assert resp.status_code == 200
    assert resp.json() == {"total": 3, "changed": 2, "unchanged": 1}
    tasks.start_all_instances.assert_awaited_once()


def test_tasks_stop_all() -> None:
    tasks = AsyncMock()
    tasks.stop_all_instances.return_value = TaskBatchResult(total=3, changed=3, unchanged=0)
    _install_context(tasks=tasks)

    resp = _client().post("/tasks/stop-all")

    assert resp.status_code == 200
    assert resp.json()["changed"] == 3
    tasks.stop_all_instances.assert_awaited_once()


def test_tasks_unavailable_returns_503() -> None:
    _install_context(tasks=None)

    resp = _client().get("/tasks")

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 旧路由已删除：/jobs 与 /routes 全部 404
# ---------------------------------------------------------------------------


def test_jobs_routes_removed() -> None:
    _install_context()

    client = _client()
    assert client.get("/jobs").status_code == 404
    assert client.get("/jobs/poll:d1:fast").status_code == 404
    assert client.post("/jobs/poll:d1:fast/start").status_code == 404
    assert client.post("/jobs/poll:d1:fast/stop").status_code == 404
    assert client.post("/jobs/start-all").status_code == 404
    assert client.post("/jobs/stop-all").status_code == 404


def test_routes_explain_removed() -> None:
    _install_context()

    resp = _client().get("/routes/explain", params={"device_id": "d1", "point_id": "p1"})

    assert resp.status_code == 404
