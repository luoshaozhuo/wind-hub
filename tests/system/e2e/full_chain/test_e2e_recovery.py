"""E2E-10: full-chain Redis interruption and realtime recovery."""

from __future__ import annotations

import asyncio
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
