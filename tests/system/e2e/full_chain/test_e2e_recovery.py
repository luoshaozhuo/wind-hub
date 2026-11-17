"""E2E-10: full-chain Redis interruption and realtime recovery."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_10_redis_recovers_latest_value(full_chain: Any) -> None:
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    paused = False
    try:
        await harness.cli_call(harness.cli_rt, "collector", "start", "WTG_SIM01", "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(started["data"]["generation_id"])
        harness.generation_id = generation_id
        key = harness.redis_key("WTG_SIM01", "wind_speed")
        before = await harness.sink.wait_redis_value(
            key, timeout=90, match=lambda item: abs(float(item["value"]) - 10.0) < 1e-3
        )
        await asyncio.to_thread(harness.stack.pause, "redis")
        paused = True
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(harness.sink.wait_redis_value(key, timeout=10), timeout=3)
        await asyncio.to_thread(harness.stack.unpause, "redis")
        paused = False
        after = await harness.sink.wait_redis_value(
            key,
            timeout=90,
            match=lambda item: item["sample_id"] != before["sample_id"]
            and item["sample_time"] > before["sample_time"]
            and abs(float(item["value"]) - 10.0) < 1e-3,
        )
        assert after["collector_id"] == harness.rt_collector
        assert after["quality"] == "good"
        harness.record(
            "E2E-10-REDIS",
            generation_id=generation_id,
            before=before["sample_id"],
            after=after["sample_id"],
        )
    finally:
        if paused:
            await asyncio.to_thread(harness.stack.unpause, "redis")
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )


async def test_e2e_10_kafka_outage_holds_and_recovers(full_chain: Any) -> None:
    """Actual Collector traffic remains pending while the Kafka broker is paused."""
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    paused = False
    try:
        await harness.cli_call(harness.cli_rel, "collector", "start", "WTG_SIM01", "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(started["data"]["generation_id"])
        harness.generation_id = generation_id
        baseline = await harness.sink.wait_kafka_messages(min_messages=10, timeout=90)
        assert baseline
        outage_at = datetime.now(UTC)
        await asyncio.to_thread(harness.stack.pause, "kafka")
        paused = True
        await asyncio.sleep(10)
        held = await harness.sink.stream_pending()
        assert held["consumer_pending"] is not None and held["consumer_pending"] > 0, held
        await harness.cli_call(harness.cli_rel, "collector", "stop", "WTG_SIM01", "--yes")
        await asyncio.to_thread(harness.stack.unpause, "kafka")
        paused = False
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            state = await harness.sink.stream_pending()
            if state["consumer_pending"] == 0:
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError(f"Reliable backlog did not drain: {state}")
        records = await harness.sink.wait_kafka_messages(min_messages=len(baseline) + 1, timeout=90)
        payloads = [json.loads(record.value) for record in records]
        assert any(
            item.get("collector_id") == harness.reliable_collector
            and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= outage_at
            for item in payloads
        )
        harness.record("E2E-10-KAFKA", generation_id=generation_id, held=held, drained=state)
    finally:
        if paused:
            await asyncio.to_thread(harness.stack.unpause, "kafka")
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rel, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )
