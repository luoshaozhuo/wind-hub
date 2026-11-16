"""Full-chain 冒烟：六服务 READY → 物理链路首值贯通（所有场景测试的前置证据）。"""

from __future__ import annotations

import pytest

from .conftest import FARM

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_full_chain_physical_first_value(full_chain) -> None:  # noqa: ANN001
    """E2E 冒烟：Generation 运行 → Gateway 变量 → 双 Collector → Redis。"""
    harness = full_chain
    # 双 Collector 五设备任务全部经 CLI 启动（N-CLI-1 令牌链）。
    for config in (harness.cli_rt, harness.cli_rel):
        for device_id in ("WTG_SIM01", "PV_SIM01", "BESS_SIM01", "GRID_SIM01", "PPC_SIM01"):
            await harness.cli_call(config, "collector", "start", device_id, "--yes")

    started = await harness.cli_call(
        harness.cli_rt,
        "data-generator",
        "start",
        "wind-plant-physical",
        "--scenario-version",
        "1.0.0",
        "--yes",
    )
    generation_id = started["data"]["generation_id"]
    assert started["data"]["accepted"] is True
    harness.generation_id = generation_id

    # Gateway 变量读回（独立 gRPC 经 CLI）：TI=0 ⇒ 风速恒 10 m/s。
    gateway_value = await harness.poll(
        harness.cli_rt,
        "gateway",
        "read",
        "gw-wtg_sim01",
        "wind_speed",
        predicate=lambda data: bool(data.get("values"))
        and abs(_number(data["values"][0]["value"]) - 10.0) < 0.6,
        timeout=90,
    )
    assert gateway_value["values"][0]["quality"] == "GOOD"

    # 双 Collector 采集读回（经 CLI latest）。
    for config in (harness.cli_rt, harness.cli_rel):
        latest = await harness.poll(
            config,
            "collector",
            "latest",
            "WTG_SIM01",
            "wind_speed",
            predicate=lambda data: any(
                sample.get("point_id") == "wind_speed"
                and abs(_number(sample.get("value")) - 10.0) < 0.6
                for sample in data.get("samples", [])
            ),
            timeout=90,
        )
        assert latest["device_id"] == "WTG_SIM01"

    # Sink OUT-03：Redis 最新值（独立客户端）。
    assert harness.sink is not None
    redis_value = await harness.sink.wait_redis_value(
        harness.redis_key("WTG_SIM01", "wind_speed"), timeout=90
    )
    assert abs(redis_value["value"] - 10.0) < 0.6
    assert redis_value["wind_farm_id"] == FARM
    assert redis_value["collector_id"] == harness.rt_collector

    # 源真值：Recording JSONL（设计 §9.1 字段）。
    rows = harness.read_recording(generation_id)
    wind_rows = [
        point
        for row in rows
        for point in row.get("points", [])
        if point.get("device_id") == "WTG_SIM01" and point.get("point_id") == "wind_speed"
    ]
    assert wind_rows, "recording JSONL has no WTG wind_speed truth"
    assert abs(wind_rows[-1]["value"] - 10.0) < 0.6
    harness.record("E2E-SMOKE", generation_id=generation_id, rows=len(rows))

    # 停止 Generation 与任务（业务面全部经 CLI）。
    await harness.cli_call(harness.cli_rt, "data-generator", "stop", generation_id, "--yes")
    harness.generation_id = None
    for config in (harness.cli_rt, harness.cli_rel):
        for device_id in ("WTG_SIM01", "PV_SIM01", "BESS_SIM01", "GRID_SIM01", "PPC_SIM01"):
            await harness.cli_call(config, "collector", "stop", device_id, "--yes")


def _number(raw: object) -> float:
    if isinstance(raw, bool):
        return -1.0
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, dict):
        for key in ("double_value", "float_value", "int_value", "uint_value"):
            if isinstance(raw.get(key), int | float) and not isinstance(raw[key], bool):
                return float(raw[key])
    return -1.0
