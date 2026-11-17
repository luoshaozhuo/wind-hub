"""Physical WTG/PV/BESS full-chain evidence, independent of field hardware.

Services are real CLI-managed OS processes; Modbus servers and physical models
belong to Industrial Gateway and Data Generator in the current worktree.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from typing import Any

import pytest

from .conftest import SIM_DEVICES

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


def _scalar(value: Any) -> float:
    if isinstance(value, bool):
        raise AssertionError("boolean is not a numeric sensor value")
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, dict):
        candidates = (
            value.get(key) for key in ("double_value", "float_value", "int_value", "uint_value")
        )
        number = next((float(v) for v in candidates if type(v) in (int, float)), math.nan)
    else:
        number = math.nan
    assert math.isfinite(number), f"invalid numeric observation: {value!r}"
    return number


@pytest.mark.parametrize(
    ("case_id", "device_id", "point_id"),
    [
        ("E2E-01", "WTG_SIM01", "wind_speed"),
        ("E2E-02", "PV_SIM01", "active_power"),
        ("E2E-03", "BESS_SIM01", "soc"),
    ],
)
async def test_physical_device_reaches_gateway_and_redis(
    full_chain: Any, case_id: str, device_id: str, point_id: str
) -> None:
    """Verify actual DG truth, Gateway value, Collector and downstream Redis."""
    harness = full_chain
    assert device_id in SIM_DEVICES
    generation_id: str | None = None
    started_tasks: list[str] = []
    try:
        for device in SIM_DEVICES:
            await harness.cli_call(harness.cli_rt, "collector", "start", device, "--yes")
            started_tasks.append(device)

        generation = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        assert generation["data"]["accepted"] is True
        generation_id = str(generation["data"]["generation_id"])
        harness.generation_id = generation_id

        for _ in range(100):
            path = harness.recording_path(generation_id)
            if path.is_file() and path.stat().st_size:
                break
            await asyncio.sleep(0.1)
        first_row = harness.read_recording(generation_id)[0]
        first_source_time = datetime.fromisoformat(first_row["produced_at"].replace("Z", "+00:00"))

        assert harness.sink is not None
        redis_value = await harness.sink.wait_redis_value(
            harness.redis_key(device_id, point_id),
            timeout=90,
            match=lambda value: datetime.fromisoformat(value["sample_time"].replace("Z", "+00:00"))
            >= first_source_time
            and (case_id != "E2E-01" or abs(_scalar(value["value"]) - 10.0) < 1e-3),
        )
        observed = _scalar(redis_value["value"])
        assert redis_value["device_id"] == device_id
        assert redis_value["point_id"] == point_id

        gateway_name = f"gw-{device_id.lower()}"
        gateway = await harness.poll(
            harness.cli_rt,
            "gateway",
            "read",
            gateway_name,
            point_id,
            predicate=lambda data: bool(data.get("values")),
            timeout=60,
        )
        gateway_value = _scalar(gateway["values"][0]["value"])

        latest = await harness.poll(
            harness.cli_rt,
            "collector",
            "latest",
            device_id,
            point_id,
            predicate=lambda data: any(
                point.get("point_id") == point_id for point in data.get("samples", [])
            ),
            timeout=60,
        )
        assert latest["device_id"] == device_id

        rows = harness.read_recording(generation_id)
        truth = [
            (
                datetime.fromisoformat(point["source_time"].replace("Z", "+00:00")),
                _scalar(point["value"]),
            )
            for row in rows
            for point in row.get("points", [])
            if point.get("device_id") == device_id and point.get("point_id") == point_id
        ]
        assert truth, f"{case_id}: missing recorded source truth"
        sample_time = datetime.fromisoformat(redis_value["sample_time"].replace("Z", "+00:00"))
        eligible = [(timestamp, value) for timestamp, value in truth if timestamp <= sample_time]
        assert eligible, f"{case_id}: no source sample preceding Redis acquisition"
        source_time, source_value = max(eligible, key=lambda item: item[0])
        assert (sample_time - source_time).total_seconds() <= 1.0
        tolerance = max(1e-3, abs(source_value) * 2e-3)
        assert abs(observed - source_value) <= tolerance
        harness.record(
            case_id,
            generation_id=generation_id,
            device=device_id,
            point=point_id,
            truth_samples=len(truth),
            gateway_value=gateway_value,
            redis_value=observed,
            source_time=source_time.isoformat(),
            sample_time=sample_time.isoformat(),
            source_value=source_value,
            tolerance=tolerance,
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
        for device in reversed(started_tasks):
            await harness.cli_call(
                harness.cli_rt, "collector", "stop", device, "--yes", check=False
            )
