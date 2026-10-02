"""Admin API v1 单元测试。

验证阶段：unit。UseCase 使用 AsyncMock/真实 OperationManager，验证 wire 契约、
分页、404/409 和 Task 级启停映射；不启动真实 Runtime 或网络监听。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.application.app_context import AppContext, clear_context, set_context
from wind_hub_server.application.operation import OperationManager
from wind_hub_server.application.usecase.device import DeviceSnapshot
from wind_hub_server.application.usecase.device_control import DeviceCommandResult
from wind_hub_server.application.usecase.device_data import DeviceDataItem, TrendSeries
from wind_hub_collector.domain.model.point import PointValue, Quality
from wind_hub_server.application.usecase.overview import OverviewSnapshot
from wind_hub_server.application.usecase.worker_tasks import TaskSummary


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    """隔离进程级 AppContext。"""
    clear_context()
    yield
    clear_context()


def _client(ctx: AppContext) -> TestClient:
    """安装上下文并创建 in-process FastAPI 客户端。"""
    set_context(ctx)
    return TestClient(build_api())


def _task_summary(task_id: str = "t1", *, enabled: bool = True) -> TaskSummary:
    """构造 V1 Task 聚合快照。"""
    return TaskSummary(
        task_id=task_id,
        assigned_worker_id="collector-a",
        placement_state="assigned",
        device="d1",
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        enabled=enabled,
        runtime_state="stopped",
        instance_count=1,
        running_instances=0,
        stopped_instances=1,
        failed_instances=0,
    )


def test_v1_overview_returns_aggregate_snapshot() -> None:
    """Overview V1 应直接暴露聚合 Read Model。"""
    overview = AsyncMock()
    overview.snapshot.return_value = OverviewSnapshot(
        site_id="farm-a",
        site_name="Farm A",
        runtime_running=True,
        runtime_state="running",
        workers_unavailable=[],
        device_count=48,
        devices_connected=47,
        devices_offline=1,
        sink_count=2,
        sinks_healthy=2,
        task_count=3,
        task_instances=48,
        task_instances_running=47,
        task_instances_failed=1,
        points_collected=1000,
        points_routed=990,
        points_dropped=10,
    )
    client = _client(AppContext(overview=overview))

    response = client.get("/api/v1/overview")

    assert response.status_code == 200
    assert response.json()["devices_offline"] == 1
    assert response.json()["task_instances_failed"] == 1


def test_v1_devices_search_then_paginate() -> None:
    """设备搜索应先作用完整结果集，再执行分页。"""
    devices = AsyncMock()
    devices.list_devices.return_value = [
        DeviceSnapshot(
            device_id="wtg-001", protocol="ads", host="192.168.151.1", port=801,
            point_table="wtg", enabled=True, connected=True,
        ),
        DeviceSnapshot(
            device_id="wtg-002", protocol="ads", host="192.168.151.2", port=801,
            point_table="wtg", enabled=True, connected=False,
        ),
    ]
    client = _client(AppContext(devices=devices))

    response = client.get("/api/v1/devices?page=1&page_size=1&search=wtg")

    assert response.status_code == 200
    assert response.json()["page"] == {"page": 1, "page_size": 1, "total": 2}
    assert len(response.json()["items"]) == 1
    devices.list_devices.assert_awaited_once_with("wtg")


def test_v1_task_start_and_disabled_conflict() -> None:
    """Task 级启停返回聚合状态；禁用冲突映射为 409。"""
    tasks = AsyncMock()
    tasks.start_task.return_value = _task_summary("t1")
    client = _client(AppContext(tasks=tasks))

    response = client.post("/api/v1/tasks/t1/start")

    assert response.status_code == 200
    assert response.json()["task_id"] == "t1"

    tasks.start_task.side_effect = ValueError("task 't2' is disabled")
    response = client.post("/api/v1/tasks/t2/start")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_DISABLED"


def test_v1_operation_query() -> None:
    """Operation 查询应返回统一生命周期状态。"""
    operations = OperationManager()
    created = operations.create("devices.verify_all", total=48)
    operations.mark_running(created.operation_id)
    client = _client(AppContext(operations=operations))

    response = client.get(f"/api/v1/operations/{created.operation_id}")

    assert response.status_code == 200
    assert response.json()["state"] == "running"


def test_v1_unknown_operation_returns_404() -> None:
    """未知 Operation 必须返回稳定 NOT_FOUND。"""
    client = _client(AppContext(operations=OperationManager()))

    response = client.get("/api/v1/operations/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_v1_device_data_is_paginated_cache_view() -> None:
    data = AsyncMock()
    data.list_data.return_value = [
        DeviceDataItem(
            point_id="p1", point_groups=["fast"], data_type="float32",
            unit="none", unit_symbol="", value=1.5, quality=Quality.GOOD,
        )
    ]
    client = _client(AppContext(device_data=data))

    response = client.get("/api/v1/devices/d1/data?page_size=100")

    assert response.status_code == 200
    assert response.json()["items"][0]["quality"] == "good"
    assert response.json()["items"][0]["value"] == 1.5


def test_v1_device_trend_returns_samples() -> None:
    data = AsyncMock()
    data.trend.return_value = [
        TrendSeries(
            point_id="p1", unit="none", unit_symbol="",
            samples=[PointValue(device_id="d1", point_id="p1", value=2.5)],
        )
    ]
    client = _client(AppContext(device_data=data))

    response = client.get("/api/v1/devices/d1/trend?point_id=p1")

    assert response.status_code == 200
    assert response.json()[0]["samples"][0]["value"] == 2.5


def test_v1_device_command_returns_readback_contract() -> None:
    control = AsyncMock()
    control.send.return_value = DeviceCommandResult(
        command_id="c1", requested=80.0, success=True,
        sent_at=PointValue(device_id="d", point_id="p", value=0).timestamp,
        finished_at=PointValue(device_id="d", point_id="p", value=0).timestamp,
        latency_ms=12.0, readback=79.8, readback_quality="good",
    )
    client = _client(AppContext(device_control=control))

    response = client.post(
        "/api/v1/devices/d1/commands",
        json={"point_id": "limit", "value": 80.0, "command_id": "c1"},
    )

    assert response.status_code == 200
    assert response.json()["readback"] == 79.8
    control.send.assert_awaited_once_with(
        "d1", "limit", 80.0, timeout=5.0, command_id="c1"
    )


def test_v1_phase5_quality_endpoint() -> None:
    quality = MagicMock()
    from datetime import UTC, datetime
    from wind_hub_server.application.usecase.quality import QualitySnapshot

    now = datetime.now(UTC)
    quality.snapshot.return_value = QualitySnapshot(
        window="24h", sampled_from=now, sampled_to=now,
        acquisition_channels=[], delivery_channels=[],
        channel_summary=[], data_metrics=[], dimensions=[], issues=[], events=[],
    )
    client = _client(AppContext(quality=quality))

    response = client.get("/api/v1/quality?window=24h")

    assert response.status_code == 200
    assert response.json()["window"] == "24h"


def test_v1_phase5_logs_endpoint() -> None:
    logs = MagicMock()
    from wind_hub_server.application.usecase.logs import LogPage

    logs.list_logs.return_value = LogPage(items=[], page=1, page_size=20, total=0)
    client = _client(AppContext(logs=logs))

    response = client.get("/api/v1/logs")

    assert response.status_code == 200
    assert response.json()["page"]["total"] == 0


def test_v1_phase5_system_health_endpoint() -> None:
    health = MagicMock()
    from datetime import UTC, datetime
    from wind_hub_server.application.usecase.system_health import (
        ResourceSeries,
        SystemHealthSnapshot,
    )

    now = datetime.now(UTC)
    health.snapshot.return_value = SystemHealthSnapshot(
        range="24h", sampled_at=now, uptime_seconds=1.0, cpu_count=4,
        load_average=None, risks=[], mounts=[],
        series=ResourceSeries(
            timestamps=[], memory_host_gb=[], memory_rss_gb=[],
            cpu_host_pct=[], cpu_process_pct=[], cpu_temp_c=[], disk_free_gb=[],
        ),
        current={},
    )
    client = _client(AppContext(system_health=health))

    response = client.get("/api/v1/system-health?range=24h")

    assert response.status_code == 200
    assert response.json()["cpu_count"] == 4


def test_v1_admin_state_atomic_apply() -> None:
    from wind_hub_server.application.usecase.config_admin import ConfigApplyResult

    admin_state = AsyncMock()
    admin_state.replace_all.return_value = ConfigApplyResult(
        success=True,
        revision=9,
    )
    client = _client(AppContext(admin_state=admin_state))

    response = client.put(
        "/api/v1/admin-state",
        json={
            "devices": [],
            "tasks": [],
            "sinks": [],
            "definitions": {
                "units": {},
                "device_types": {},
                "device_models": {},
                "point_tables": {},
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["revision"] == 9
    admin_state.replace_all.assert_awaited_once()

def test_v1_request_validation_uses_422_envelope() -> None:
    client = _client(AppContext())

    response = client.post(
        "/api/v1/diagnostics/ports",
        json={"host": "127.0.0.1", "ports": [0]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_v1_admin_tasks_reject_ambiguous_selector() -> None:
    client = _client(AppContext())

    response = client.put(
        "/api/v1/admin-state/tasks",
        json={
            "items": [
                {
                    "task_id": "t1",
                    "device": "d1",
                    "device_group": "g1",
                    "point_group": "fast",
                }
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
