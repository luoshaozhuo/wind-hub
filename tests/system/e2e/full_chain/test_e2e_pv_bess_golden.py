"""E2E-02/03：PV 与 BESS 四出口的定向 Golden Data 对应证据。"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta
from typing import Any

import pytest

from .conftest import GOLDEN_DEVICES, SINK_TARGET_POINT, wait_sink_modbus_float

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
                await harness.poll(
                    config,
                    "collector",
                    "task",
                    device_id,
                    predicate=lambda data: data["task"]["actual_state"] == "RUNNING",
                    timeout=60,
                )
                started.append((config, device_id))
        for device_id in GOLDEN_DEVICES:
            delivery_id = f"gateway_output_{device_id.lower()}"
            await harness.cli_call(
                harness.cli_rt,
                "sink",
                "start",
                delivery_id,
                "--yes",
            )
            await harness.poll(
                harness.cli_rt,
                "sink",
                "delivery",
                delivery_id,
                predicate=lambda data: data["delivery"]["actual_state"] == "RUNNING",
                timeout=60,
            )
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
            delivery_id = f"gateway_output_{device_id.lower()}"
            delivery = await harness.cli_call(harness.cli_rt, "sink", "delivery", delivery_id)
            assert delivery["data"]["delivery"]["actual_state"] == "RUNNING", delivery
            assert delivery["data"]["delivery"]["last_error"] == "", delivery

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
                predicate=lambda data, expected=redis_value, limit=tolerance: bool(
                    data.get("values")
                )
                and data["values"][0].get("quality") == "GOOD"
                and abs(_number(data["values"][0]["value"]) - expected) <= limit,
                timeout=90,
            )
            gateway_value = _number(gateway["values"][0]["value"])
            modbus_value, modbus_quality = await wait_sink_modbus_float(
                harness.sink_modbus_ports[device_id],
                redis_value,
                timeout=90,
                tolerance=tolerance,
            )
            assert modbus_quality == 0

            def is_golden_record(
                record: Any, device_id: str = device_id, point_id: str = point_id
            ) -> bool:
                item = json.loads(record.value.decode("utf-8"))
                return (
                    item.get("collector_id") == harness.reliable_collector
                    and item.get("device_id") == device_id
                    and item.get("point_id") == point_id
                    and _time(item["sample_time"]) >= generation_start
                )

            records = await harness.sink.wait_kafka_messages(
                min_messages=1, timeout=90, offset_reset="earliest", match=is_golden_record
            )
            matching = [
                json.loads(record.value.decode("utf-8"))
                for record in records
                if is_golden_record(record)
            ]
            assert matching, (device_id, point_id)
            kafka = matching[0]
            kafka_sample_id = str(kafka["sample_id"])
            assert kafka_sample_id
            assert len({item["sample_id"] for item in matching}) == len(matching)
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
                    "correlated_redis_sample_id": redis_sample_id,
                    "expected": redis_value,
                    "actual": gateway_value,
                    "tolerance": tolerance,
                    "quality": gateway["values"][0]["quality"],
                },
                modbus={
                    "correlated_redis_sample_id": redis_sample_id,
                    "expected": redis_value,
                    "actual": modbus_value,
                    "tolerance": tolerance,
                    "encoding": "float32",
                    "quality_code": modbus_quality,
                },
                kafka={
                    "sample_id": kafka_sample_id,
                    "sample_time": kafka_sample_time.isoformat(),
                    "source_time": kafka_source_time.isoformat(),
                    "source_delta_seconds": kafka_source_delta,
                    "expected": kafka_source_value,
                    "actual": kafka_value,
                    "tolerance": kafka_tolerance,
                    "collector_id": kafka["collector_id"],
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
