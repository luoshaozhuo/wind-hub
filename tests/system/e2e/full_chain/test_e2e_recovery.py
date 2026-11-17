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


async def test_e2e_10_nats_recovers_both_channels(full_chain: Any) -> None:
    """Pause only this test's NATS and observe new data on both downstream channels."""
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    paused = False
    try:
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "start", "WTG_SIM01", "--yes")
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
        baseline = await harness.sink.wait_kafka_messages(min_messages=10, timeout=90)
        assert baseline
        await asyncio.to_thread(harness.stack.pause, "nats")
        paused = True
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(harness.sink.stream_pending(), timeout=3)
        await asyncio.to_thread(harness.stack.unpause, "nats")
        paused = False
        restored_at = datetime.now(UTC)
        after = await harness.sink.wait_redis_value(
            key,
            timeout=90,
            match=lambda item: item["sample_id"] != before["sample_id"]
            and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= restored_at,
        )
        records = await harness.sink.wait_kafka_messages(
            min_messages=20, timeout=90, offset_reset="latest"
        )
        payloads = [json.loads(record.value) for record in records]
        assert any(
            item.get("collector_id") == harness.reliable_collector
            and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= restored_at
            for item in payloads
        )
        harness.record(
            "E2E-10-NATS",
            generation_id=generation_id,
            before=before["sample_id"],
            after=after["sample_id"],
        )
    finally:
        if paused:
            await asyncio.to_thread(harness.stack.unpause, "nats")
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "stop", "WTG_SIM01", "--yes", check=False)


async def test_e2e_10_gateway_restart_recovers_data(full_chain: Any) -> None:
    """Restart Gateway during collection and verify a fresh generation reaches Redis."""
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    gateway_stopped = False
    try:
        await harness.cli_call(harness.cli_rt, "collector", "start", "WTG_SIM01", "--yes")
        first = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(first["data"]["generation_id"])
        harness.generation_id = generation_id
        key = harness.redis_key("WTG_SIM01", "wind_speed")
        before = await harness.sink.wait_redis_value(
            key, timeout=90, match=lambda item: abs(float(item["value"]) - 10.0) < 1e-3
        )
        stopped = await harness.cli_call(harness.cli_rt, "service", "stop", "gateway", "--yes")
        gateway_stopped = True
        assert stopped["data"]["process"]["state"] == "STOPPED"
        await harness.cli_call(
            harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
        )
        restarted = await harness.cli_call(harness.cli_rt, "service", "start", "gateway", "--yes")
        gateway_stopped = False
        assert restarted["data"]["business"]["health"] == "HEALTHY"
        await harness.poll(
            harness.cli_rt,
            "data-generator",
            "status",
            generation_id,
            predicate=lambda data: data.get("generation", {}).get("actual_state") == "STOPPED",
            timeout=90,
        )
        harness.generation_id = None
        generation_id = None
        second = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(second["data"]["generation_id"])
        assert generation_id != first["data"]["generation_id"]
        harness.generation_id = generation_id
        restored_at = datetime.now(UTC)
        after = await harness.sink.wait_redis_value(
            key,
            timeout=90,
            match=lambda item: item["sample_id"] != before["sample_id"]
            and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= restored_at
            and abs(float(item["value"]) - 10.0) < 1e-3,
        )
        harness.record("E2E-10-GATEWAY", generation_id=generation_id, sample_id=after["sample_id"])
    finally:
        if gateway_stopped:
            await harness.cli_call(
                harness.cli_rt, "service", "start", "gateway", "--yes", check=False
            )
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )
