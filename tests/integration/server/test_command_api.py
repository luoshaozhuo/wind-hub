"""端到端测试 —— 指令下发：写可写点 + 幂等去重。"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from wind_hub.assembly import AssembledRuntime


async def test_command_write_and_read_back(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
) -> None:
    """写 setpoint.power 后能实时读回写入值。"""
    resp = await api_client.post(
        "/commands",
        json={"device_id": "modbus-1", "point_id": "setpoint.power", "value": 55.5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    command_id = body["command_id"]

    read = await api_client.get("/points/modbus-1/setpoint.power")
    assert read.status_code == 200
    assert read.json()["value"] == pytest.approx(55.5)
    assert command_id


async def test_command_idempotency_deduplicates_replay(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
) -> None:
    """同一 command_id 重放不会二次执行：第二次（不同值）返回缓存结果。"""
    command_id = "e2e-idem-1"

    first = await api_client.post(
        "/commands",
        json={
            "device_id": "modbus-1",
            "point_id": "setpoint.power",
            "value": 11.0,
            "command_id": command_id,
        },
    )
    assert first.json()["success"] is True

    # 重放相同 command_id 但换成不同值——应命中幂等缓存、不真正写入 99.0
    replay = await api_client.post(
        "/commands",
        json={
            "device_id": "modbus-1",
            "point_id": "setpoint.power",
            "value": 99.0,
            "command_id": command_id,
        },
    )
    assert replay.json()["success"] is True
    assert replay.json()["command_id"] == command_id

    read = await api_client.get("/points/modbus-1/setpoint.power")
    assert read.json()["value"] == pytest.approx(11.0)


async def test_command_unknown_device_reports_failure(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
) -> None:
    """未知设备由 Dispatcher 内联为 success=False，而非 500。"""
    resp = await api_client.post(
        "/commands",
        json={"device_id": "no-such-device", "point_id": "p", "value": 1},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["error"]
