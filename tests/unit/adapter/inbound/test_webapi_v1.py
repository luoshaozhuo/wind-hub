"""Admin API v1 单元测试。

验证阶段：unit。UseCase 使用 AsyncMock/真实 OperationManager，验证 wire 契约、
分页、404/409 和 Task 级启停映射；不启动真实 Runtime 或网络监听。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from wind_hub.adapter.inbound.webapi.app import build_api
from wind_hub.application.app_context import AppContext, clear_context, set_context
from wind_hub.application.operation import OperationManager
from wind_hub.application.usecase.device import DeviceSnapshot
from wind_hub.application.usecase.overview import OverviewSnapshot
from wind_hub.application.usecase.task import TaskSummary


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
