"""双 Gateway 与 Collector/Sink 的真实协议测量端到端验收。"""

from __future__ import annotations

import asyncio
import json
import os
import struct
from pathlib import Path

import pytest
from pymodbus.client import AsyncModbusTcpClient

from tests.third_party.dc_ems.check_full_chain import POINTS, STATION_POINTS


@pytest.fixture(scope="module", autouse=True)
def live_only() -> None:
    """只有显式启用真实链路，才读取独立运行中的协议端口。"""
    if os.environ.get("DCEMS_MEASUREMENT_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_MEASUREMENT_LIVE=1 required for real measurement")


def _decode_float(registers: list[int]) -> float:
    """按独立点表约定将 F32 ABCD 解码为浮点数。"""
    return float(struct.unpack(">f", struct.pack(">HH", *registers))[0])


async def _read_points(client: AsyncModbusTcpClient, first: bool) -> dict[str, float | bool]:
    """从实际 TCP 连接读取动态 P/Q、风速与安全状态。"""
    measurements: dict[str, float | bool] = {}
    offsets = {
        "wind_speed": 0,
        "sim_active_power": 8,
        "sim_reactive_power": 10,
    }
    if first:
        offsets.update(
            {
                "sim_plant_active_power": 2,
                "sim_plant_reactive_power": 4,
            }
        )
    for name, offset in offsets.items():
        response = await client.read_input_registers(offset, count=2, device_id=1)
        assert not response.isError(), f"{name}: Modbus read failure {response}"
        measurements[name] = _decode_float(response.registers)
    if first:
        mode = await client.read_discrete_inputs(0, count=1, device_id=1)
        assert not mode.isError()
        measurements["primary_frequency_mode"] = bool(mode.bits[0])
    return measurements


@pytest.mark.asyncio
async def test_source_to_target_ten_turbine_realtime() -> None:
    """逐机验证真实源端到目标端交付，不能仅以 Source 动态判为成功。"""
    host = os.environ.get("DCEMS_MEASUREMENT_HOST", "127.0.0.1")
    source_clients = [
        AsyncModbusTcpClient(host, port=15221 + index, timeout=3)
        for index in range(10)
    ]
    target_clients = [
        AsyncModbusTcpClient(host, port=15121 + index, timeout=3)
        for index in range(10)
    ]
    records: dict[str, dict[str, dict[str, list[float | bool]]]] = {}
    try:
        connections = await asyncio.gather(
            *(client.connect() for client in [*source_clients, *target_clients])
        )
        assert all(connections), "source or target Gateway listener unavailable"
        for index in range(10):
            device_id = f"WTG{index + 1:02d}"
            points = POINTS | STATION_POINTS if index == 0 else POINTS
            records[device_id] = {
                name: {"source": [], "target": []}
                for name in points
                if name in {
                    "wind_speed",
                    "sim_active_power",
                    "sim_reactive_power",
                    "sim_plant_active_power",
                    "sim_plant_reactive_power",
                    "primary_frequency_mode",
                }
            }
        for _ in range(45):
            for index in range(10):
                device_id = f"WTG{index + 1:02d}"
                source = await _read_points(source_clients[index], index == 0)
                target = await _read_points(target_clients[index], index == 0)
                for point, history in records[device_id].items():
                    history["source"].append(source[point])
                    history["target"].append(target[point])
            await asyncio.sleep(0.25)
    finally:
        for client in [*source_clients, *target_clients]:
            client.close()

    failures: list[str] = []
    for device_id, points in records.items():
        for point, history in points.items():
            source = history["source"]
            target = history["target"]
            if point == "primary_frequency_mode":
                if source != [False] * len(source) or target != [False] * len(target):
                    failures.append(f"{device_id}/{point}: unsafe mode")
                continue
            source_values = {round(float(item), 1) for item in source}
            target_values = {round(float(item), 1) for item in target}
            if len(source_values) < 2 or len(target_values) < 2:
                failures.append(f"{device_id}/{point}: frozen")
            if not source_values.intersection(target_values):
                failures.append(f"{device_id}/{point}: values never reconciled")
    evidence_dir = Path(os.environ.get(
        "DCEMS_MEASUREMENT_EVIDENCE_DIR", "run/dcems-measurement/evidence"
    ))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "source-target-reconciliation.json").write_text(
        json.dumps(
            {"failures": failures, "records": records},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert not failures, failures
