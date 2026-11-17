"""E2E-05: real CSV replay through the CLI-managed Generator and Gateway."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


@pytest.mark.parametrize("speed", [1, 2])
async def test_e2e_05_csv_replay_recording(full_chain: Any, speed: int) -> None:
    harness = full_chain
    assert harness.sink is not None
    restart_pids: tuple[int, int] | None = None
    if speed == 2:
        before = await harness.cli_call(harness.cli_rt, "service", "status", "data-generator")
        old_pid = before["data"][0]["process"]["pid"]
        restarted = await harness.cli_call(
            harness.cli_rt, "service", "restart", "data-generator", "--yes"
        )
        new_pid = restarted["data"]["process"]["pid"]
        assert isinstance(old_pid, int) and isinstance(new_pid, int) and old_pid != new_pid
        assert restarted["data"]["business"]["health"] == "HEALTHY"
        restart_pids = (old_pid, new_pid)
    consumer = AIOKafkaConsumer(
        harness.sink.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="latest",
        group_id=f"replay-{uuid4()}",
        enable_auto_commit=False,
    )
    await consumer.start()
    generation_id: str | None = None
    try:
        await harness.cli_call(harness.cli_rel, "collector", "start", "GW02", "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            f"e2e-replay-{speed}x",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(started["data"]["generation_id"])
        harness.generation_id = generation_id
        state = await harness.poll(
            harness.cli_rt,
            "data-generator",
            "status",
            generation_id,
            predicate=lambda data: data.get("generation", {}).get("actual_state")
            in {"COMPLETED", "FAILED"},
            timeout=30,
        )
        assert state["generation"]["actual_state"] == "COMPLETED"
        rows = harness.read_recording(generation_id)
        started_at = datetime.fromisoformat(rows[0]["produced_at"].replace("Z", "+00:00"))
        points = [point for row in rows for point in row["points"]]
        assert [(p["device_id"], p["point_id"], p["value"]) for p in points] == [
            ("GW02", "a02_dm_pitchangle1", 5.5),
            ("GW02", "a02_dm_pitchangle1", 6.0),
            ("GW02", "a02_dm_pitchangle1", 7.5),
        ]
        gateway = await harness.poll(
            harness.cli_rt,
            "gateway",
            "read",
            "gw-gw02",
            "a02_dm_pitchangle1",
            predicate=lambda data: bool(data.get("values"))
            and data["values"][0]["value"].get("double_value") == 7.5,
            timeout=30,
        )
        assert gateway["values"][0]["quality"] == "GOOD"
        values: list[float] = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and (not values or values[-1] != 7.5):
            batches = await consumer.getmany(timeout_ms=1000, max_records=1000)
            for records in batches.values():
                for record in records:
                    item = json.loads(record.value)
                    if (
                        item.get("collector_id") == harness.reliable_collector
                        and item.get("device_id") == "GW02"
                        and item.get("point_id") == "a02_dm_pitchangle1"
                        and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00"))
                        >= started_at
                    ):
                        value = float(item["value"])
                        if value in {5.5, 6.0, 7.5} and (not values or values[-1] != value):
                            values.append(value)
        assert values == [5.5, 6.0, 7.5]
        harness.record(
            "E2E-05-CSV",
            generation_id=generation_id,
            speed=speed,
            values=values,
            restart_pids=restart_pids,
        )
    finally:
        await consumer.stop()
        await harness.cli_call(harness.cli_rel, "collector", "stop", "GW02", "--yes", check=False)
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
        harness.generation_id = None
