"""E2E-06: measure real reliable samples at four configured task periods."""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]

PERIODS = {
    "WTG_SIM01": ("wind_speed", 0.05),
    "PV_SIM01": ("active_power", 0.1),
    "BESS_SIM01": ("soc", 0.2),
    "GRID_SIM01": ("grid_frequency", 1.0),
}


@pytest.mark.parametrize(
    ("scenario", "source_period"), [("wind-plant-physical", 0.05), ("e2e-wind-100ms", 0.1)]
)
async def test_e2e_06_reliable_multirate(
    multirate_chain: Any, scenario: str, source_period: float
) -> None:
    """Observe actual downstream sampling cadence rather than configured values alone."""
    harness = multirate_chain
    assert harness.sink is not None
    consumer = AIOKafkaConsumer(
        harness.sink.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="latest",
        group_id=f"multirate-{uuid4()}",
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        for device_id in PERIODS:
            await harness.cli_call(harness.cli_rel, "collector", "start", device_id, "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            scenario,
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        harness.generation_id = str(started["data"]["generation_id"])
        for _ in range(100):
            path = harness.recording_path(harness.generation_id)
            if path.is_file() and path.stat().st_size:
                break
            await asyncio.sleep(0.1)
        first_row = harness.read_recording(harness.generation_id)[0]
        source_start = datetime.fromisoformat(first_row["produced_at"].replace("Z", "+00:00"))
        samples: dict[str, list[datetime]] = {device_id: [] for device_id in PERIODS}
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            batches = await consumer.getmany(timeout_ms=1000, max_records=1000)
            for records in batches.values():
                for record in records:
                    item = json.loads(record.value)
                    device_id = item.get("device_id")
                    if (
                        device_id in PERIODS
                        and item.get("collector_id") == harness.reliable_collector
                        and item.get("point_id") == PERIODS[device_id][0]
                    ):
                        sample_time = datetime.fromisoformat(
                            item["sample_time"].replace("Z", "+00:00")
                        )
                        if sample_time >= source_start:
                            samples[device_id].append(sample_time)
        rows = harness.read_recording(harness.generation_id)
        source_times = [
            datetime.fromisoformat(row["produced_at"].replace("Z", "+00:00")) for row in rows
        ]
        assert len(source_times) >= 4
        source_median = statistics.median(
            (right - left).total_seconds()
            for left, right in zip(source_times, source_times[1:], strict=False)
        )
        assert source_period * 0.5 <= source_median <= source_period * 1.8
        for device_id, (_, period) in PERIODS.items():
            times = sorted(set(samples[device_id]))
            assert len(times) >= 4, (device_id, len(times))
            intervals = [
                (right - left).total_seconds()
                for left, right in zip(times, times[1:], strict=False)
            ]
            median = statistics.median(intervals)
            assert period * 0.5 <= median <= period * 1.8, (device_id, median, period)
            harness.record(
                "E2E-06",
                scenario=scenario,
                source_median=source_median,
                device_id=device_id,
                period=period,
                median=median,
                count=len(times),
            )
    finally:
        await consumer.stop()
        if harness.generation_id is not None:
            await harness.cli_call(
                harness.cli_rt,
                "data-generator",
                "stop",
                harness.generation_id,
                "--yes",
                check=False,
            )
            harness.generation_id = None
        for device_id in PERIODS:
            await harness.cli_call(
                harness.cli_rel, "collector", "stop", device_id, "--yes", check=False
            )
