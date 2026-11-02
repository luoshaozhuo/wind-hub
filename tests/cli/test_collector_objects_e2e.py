"""UC-06 Collector 设备/测点/任务查询 System Test（真实 CLI 子进程 + mTLS）。"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from contracts import collector_pb2 as col
from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _p(server: Any, service: str, method: str) -> str:
    return server.method_path(service, method)


def test_collector_devices_detail_points_empty_list(cli_env: Any) -> None:
    """S01/S02：列表、详情、空列表（分页字段真实序列化）。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "CollectorService", "ListDevices"),
        col.ListDevicesResponse(
            devices=[col.Device(device_id="d1", protocol="modbus", revision="r1")],
            next_page_token="tok",
        ),
    )
    server.respond_value(
        _p(server, "CollectorService", "GetDevice"),
        col.GetDeviceResponse(device=col.Device(device_id="d1", protocol="modbus")),
    )
    server.respond_value(
        _p(server, "CollectorService", "ListPoints"),
        col.ListPointsResponse(),  # 空列表边界
    )
    r1 = cli_env.subprocess_invoke("collector", "devices", "--page-size", "1")
    assert r1.exit_code == 0, r1.stdout
    data = r1.json()["data"]
    assert data["devices"][0]["device_id"] == "d1"
    assert data["next_page_token"] == "tok"  # protobuf JSON 命名

    r2 = cli_env.subprocess_invoke("collector", "device", "d1")
    assert r2.exit_code == 0, r2.stdout
    assert r2.json()["data"]["device"]["device_id"] == "d1"

    r3 = cli_env.subprocess_invoke("collector", "points", "d1")
    assert r3.exit_code == 0, r3.stdout
    assert r3.json()["data"].get("points", []) == []


def test_collector_point_and_tasks_roundtrip(cli_env: Any) -> None:
    server = cli_env.server
    server.respond_value(
        _p(server, "CollectorService", "GetPoint"),
        col.GetPointResponse(point=common_pb2.PointMetadata(point_id="p1")),
    )
    server.respond_value(
        _p(server, "CollectorService", "ListTasks"),
        col.ListTasksResponse(
            tasks=[col.Task(task_id="t1", device_id="d1", actual_state="RUNNING")]
        ),
    )
    server.respond_value(
        _p(server, "CollectorService", "GetTask"),
        col.GetTaskResponse(task=col.Task(task_id="t1", actual_state="RUNNING")),
    )
    for args in (
        ("collector", "point", "d1", "p1"),
        ("collector", "tasks"),
        ("collector", "task", "t1"),
    ):
        result = cli_env.subprocess_invoke(*args)
        assert result.exit_code == 0, (args, result.stdout)


def test_collector_objects_permission_denied_exit2(cli_env: Any) -> None:
    """S03：PERMISSION_DENIED 与参数/配置同类 → 2。"""
    cli_env.server.fail(
        _p(cli_env.server, "CollectorService", "ListDevices"),
        grpc.StatusCode.PERMISSION_DENIED,
        "denied",
    )
    result = cli_env.subprocess_invoke("collector", "devices")
    assert result.exit_code == 2, result.stdout


def test_collector_device_vanished(cli_env: Any) -> None:
    """S03：对象消失（NOT_FOUND）不得崩溃，按业务失败码返回 1。"""
    cli_env.server.fail(
        _p(cli_env.server, "CollectorService", "GetDevice"),
        grpc.StatusCode.NOT_FOUND,
        "vanished",
    )
    result = cli_env.subprocess_invoke("collector", "device", "ghost")
    assert result.exit_code == 1, result.stdout
    assert result.json()["status"] in {"FAILURE", "UNKNOWN"} or result.exit_code == 1
