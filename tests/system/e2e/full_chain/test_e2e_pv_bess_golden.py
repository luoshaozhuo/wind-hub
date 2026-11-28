"""E2E-02/03：PV 与 BESS 四出口的定向 Golden Data 对应证据。"""

from __future__ import annotations

import asyncio
import math
import struct
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from .conftest import (
    GOLDEN_DEVICES,
    SINK_TARGET_POINT,
    decode_kafka_wide_point,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


def _number(value: Any) -> float:
    if type(value) in (int, float):
        result = float(value)
    elif isinstance(value, dict):
        candidates = (
            value.get(key) for key in ("double_value", "float_value", "int_value", "uint_value")
        )
        result = next((float(v) for v in candidates if type(v) in (int, float)), math.nan)
    else:
        result = math.nan
    assert math.isfinite(result), f"invalid Golden numeric value: {value!r}"
    return result


def _time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _source_truth(
    harness: Any, generation_id: str, device_id: str, point_id: str, sample_time: datetime
) -> tuple[datetime, float, float, float]:
    truth = [
        (_time(point["source_time"]), _number(point["value"]))
        for row in harness.read_recording(generation_id)
        for point in row.get("points", [])
        if point.get("device_id") == device_id and point.get("point_id") == point_id
    ]
    eligible = [(timestamp, value) for timestamp, value in truth if timestamp <= sample_time]
    assert eligible, (device_id, point_id, sample_time)
    source_time, source_value = max(eligible, key=lambda item: item[0])
    delta = (sample_time - source_time).total_seconds()
    assert 0 <= delta <= 1.0, (device_id, point_id, delta)
    tolerance = max(1e-3, abs(source_value) * 2e-3)
    return source_time, source_value, delta, tolerance


def _source_observation(
    harness: Any,
    generation_id: str,
    device_id: str,
    point_id: str,
    observed_at: datetime,
    actual: float,
) -> tuple[datetime, float, float, float]:
    """Correlate an asynchronous output with its own recent DG source sample."""
    candidates = [
        (timestamp, value)
        for row in harness.read_recording(generation_id)
        for point in row.get("points", [])
        if point.get("device_id") == device_id
        and point.get("point_id") == point_id
        and 0 <= (observed_at - (timestamp := _time(point["source_time"]))).total_seconds() <= 1.0
        and math.isfinite(value := _number(point["value"]))
    ]
    matching = [
        (timestamp, value, max(1e-3, abs(value) * 2e-3))
        for timestamp, value in candidates
        if abs(actual - value) <= max(1e-3, abs(value) * 2e-3)
    ]
    assert matching, (device_id, point_id, observed_at, actual, candidates[-5:])
    source_time, source_value, tolerance = min(matching, key=lambda item: abs(actual - item[1]))
    return source_time, source_value, (observed_at - source_time).total_seconds(), tolerance


async def _wait_modbus_source(
    harness: Any, generation_id: str, device_id: str, point_id: str, port: int
) -> tuple[float, int, datetime, datetime, float, float, float]:
    """Read Modbus independently and correlate its value at the observation instant."""
    from pymodbus.client import AsyncModbusTcpClient

    deadline = asyncio.get_running_loop().time() + 90
    latest: Any = None
    while asyncio.get_running_loop().time() < deadline:
        client = AsyncModbusTcpClient("127.0.0.1", port=port)
        try:
            await client.connect()
            values = await client.read_holding_registers(1000, count=2, device_id=1)
            quality = await client.read_holding_registers(2000, count=1, device_id=1)
            observed_at = datetime.now(UTC)
            if values.isError() or quality.isError():
                raise RuntimeError(f"sink modbus read failed: {values!r}, {quality!r}")
            value = struct.unpack(">f", struct.pack(">HH", *values.registers))[0]
            quality_code = int(quality.registers[0])
            latest = (value, quality_code, observed_at)
            if quality_code == 0:
                try:
                    source = _source_observation(
                        harness, generation_id, device_id, point_id, observed_at, value
                    )
                except AssertionError as error:
                    latest = error
                else:
                    source_time, source_value, source_delta, tolerance = source
                    return (
                        value,
                        quality_code,
                        observed_at,
                        source_time,
                        source_value,
                        source_delta,
                        tolerance,
                    )
        except (OSError, RuntimeError) as error:
            latest = error
        finally:
            client.close()
        await asyncio.sleep(0.2)
    raise AssertionError(f"Modbus output did not correlate with DG source: {latest}")


async def test_pv_bess_four_output_golden(full_chain: Any) -> None:
    """只启动 PV/BESS 任务，不重跑 WTG；四出口均绑定本次 Generation。"""
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    started: list[tuple[Any, str]] = []
    try:
        for config in (harness.cli_rt, harness.cli_rel):
            for device_id in GOLDEN_DEVICES:
                await harness.cli_call(config, "collector", "start", device_id, "--yes")
                started.append((config, device_id))
        started_generation = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "e2e-pv-1000",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        assert started_generation["data"]["accepted"] is True
        generation_id = str(started_generation["data"]["generation_id"])
        harness.generation_id = generation_id
        for _ in range(100):
            path = harness.recording_path(generation_id)
            if path.is_file() and path.stat().st_size:
                break
            await asyncio.sleep(0.1)
        first_row = harness.read_recording(generation_id)[0]
        generation_start = _time(first_row["produced_at"])
        golden_start = generation_start + timedelta(seconds=20)
        for _ in range(150):
            rows = harness.read_recording(generation_id)
            if _time(rows[-1]["produced_at"]) >= golden_start:
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError("PV source did not reach its solar-noon Golden window")

        for device_id, point_id in GOLDEN_DEVICES.items():
            redis = await harness.sink.wait_redis_value(
                harness.redis_key(device_id, point_id),
                timeout=90,
                match=lambda value: _time(value["sample_time"]) >= generation_start,
            )
            assert redis["device_id"] == device_id
            assert redis["point_id"] == point_id
            assert redis["quality"] == "good"
            redis_sample_id = str(redis["sample_id"])
            assert redis_sample_id
            redis_value = _number(redis["value"])
            redis_sample_time = _time(redis["sample_time"])
            source_time, source_value, source_delta, tolerance = _source_truth(
                harness, generation_id, device_id, point_id, redis_sample_time
            )
            assert abs(redis_value - source_value) <= tolerance

            gateway = await harness.poll(
                harness.cli_rt,
                "gateway",
                "read",
                f"gw-{device_id.lower()}",
                SINK_TARGET_POINT,
                predicate=lambda data: bool(data.get("values"))
                and data["values"][0].get("quality") == "GOOD"
                and _time(data["values"][0]["sample_timestamp"]) >= generation_start
                and math.isfinite(_number(data["values"][0]["value"])),
                timeout=90,
            )
            gateway_sample_time = _time(gateway["values"][0]["sample_timestamp"])
            gateway_value = _number(gateway["values"][0]["value"])
            (
                gateway_source_time,
                gateway_source_value,
                gateway_source_delta,
                gateway_tolerance,
            ) = _source_observation(
                harness,
                generation_id,
                device_id,
                point_id,
                gateway_sample_time,
                gateway_value,
            )
            (
                modbus_value,
                modbus_quality,
                modbus_observed_at,
                modbus_source_time,
                modbus_source_value,
                modbus_source_delta,
                modbus_tolerance,
            ) = await _wait_modbus_source(
                harness,
                generation_id,
                device_id,
                point_id,
                harness.sink_modbus_ports[device_id],
            )
            assert modbus_quality == 0

            def is_golden_record(
                record: Any, device_id: str = device_id, point_id: str = point_id
            ) -> bool:
                item = decode_kafka_wide_point(record, device_id=device_id, point_id=point_id)
                return item is not None and _time(item["sample_time"]) >= generation_start

            records = await harness.sink.wait_kafka_messages(
                min_messages=1, timeout=90, offset_reset="earliest", match=is_golden_record
            )
            matching = [
                item
                for record in records
                if is_golden_record(record)
                and (
                    item := decode_kafka_wide_point(record, device_id=device_id, point_id=point_id)
                )
                is not None
            ]
            assert matching, (device_id, point_id)
            kafka = matching[0]
            assert len({item["sample_time"] for item in matching}) == len(matching)
            kafka_sample_time = _time(kafka["sample_time"])
            kafka_value = _number(kafka["value"])
            (
                kafka_source_time,
                kafka_source_value,
                kafka_source_delta,
                kafka_tolerance,
            ) = _source_truth(harness, generation_id, device_id, point_id, kafka_sample_time)
            assert abs(kafka_value - kafka_source_value) <= kafka_tolerance

            harness.record(
                "E2E-02" if device_id == "PV_SIM01" else "E2E-03",
                status="PASS",
                assertion="four outputs correspond to source truth within declared tolerances",
                generation_id=generation_id,
                device_id=device_id,
                point_id=point_id,
                redis={
                    "sample_id": redis_sample_id,
                    "sample_time": redis_sample_time.isoformat(),
                    "source_time": source_time.isoformat(),
                    "source_delta_seconds": source_delta,
                    "expected": source_value,
                    "actual": redis_value,
                    "tolerance": tolerance,
                },
                gateway={
                    "sample_time": gateway_sample_time.isoformat(),
                    "source_time": gateway_source_time.isoformat(),
                    "source_delta_seconds": gateway_source_delta,
                    "expected": gateway_source_value,
                    "actual": gateway_value,
                    "tolerance": gateway_tolerance,
                    "quality": gateway["values"][0]["quality"],
                },
                modbus={
                    "observed_at": modbus_observed_at.isoformat(),
                    "source_time": modbus_source_time.isoformat(),
                    "source_delta_seconds": modbus_source_delta,
                    "expected": modbus_source_value,
                    "actual": modbus_value,
                    "tolerance": modbus_tolerance,
                    "encoding": "float32",
                    "quality_code": modbus_quality,
                },
                kafka={
                    "sample_time": kafka_sample_time.isoformat(),
                    "device_config_id": kafka["device_config_id"],
                    "source_time": kafka_source_time.isoformat(),
                    "source_delta_seconds": kafka_source_delta,
                    "expected": kafka_source_value,
                    "actual": kafka_value,
                    "tolerance": kafka_tolerance,
                    "data_level": kafka["data_level"],
                },
            )
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt,
                "data-generator",
                "stop",
                generation_id,
                "--yes",
                check=False,
            )
            harness.generation_id = None
        for config, device_id in reversed(started):
            await harness.cli_call(config, "collector", "stop", device_id, "--yes", check=False)
