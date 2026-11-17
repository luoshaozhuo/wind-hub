"""E2E-02: compare PV physical output for two irradiance scenarios."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_02_pv_irradiance_and_capacity(full_chain: Any) -> None:
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    observed: dict[int, tuple[float, float]] = {}
    try:
        await harness.cli_call(harness.cli_rt, "collector", "start", "PV_SIM01", "--yes")
        for level in (800, 1000):
            started = await harness.cli_call(
                harness.cli_rt,
                "data-generator",
                "start",
                f"e2e-pv-{level}",
                "--scenario-version",
                "1.0.0",
                "--yes",
            )
            generation_id = str(started["data"]["generation_id"])
            harness.generation_id = generation_id
            await asyncio.sleep(3)
            rows = harness.read_recording(generation_id)
            assert len(rows) >= 30
            source_start = datetime.fromisoformat(rows[0]["produced_at"].replace("Z", "+00:00"))
            point_values = {
                p["point_id"]: p["value"]
                for p in rows[29]["points"]
                if p["device_id"] == "PV_SIM01"
            }
            available = float(point_values["pv_available_power"])
            power = float(point_values["active_power"])
            assert 0 < available <= 1_000_000
            assert 0 <= power <= 900_000
            redis = await harness.sink.wait_redis_value(
                harness.redis_key("PV_SIM01", "active_power"),
                timeout=90,
                match=lambda item, cutoff=source_start: float(item["value"]) > 0
                and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00"))
                >= cutoff,
            )
            assert redis["device_id"] == "PV_SIM01" and redis["point_id"] == "active_power"
            assert redis["quality"] == "good" and 0 < float(redis["value"]) <= 900_000
            observed[level] = (available, power)
            harness.record(
                "E2E-02-IRRADIANCE",
                generation_id=generation_id,
                level=level,
                source_available=available,
                source_power=power,
                redis=redis["value"],
            )
            await harness.cli_call(harness.cli_rt, "data-generator", "stop", generation_id, "--yes")
            harness.generation_id = None
            generation_id = None
        assert observed[1000][0] > observed[800][0]
        assert observed[1000][1] >= observed[800][1]
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "PV_SIM01", "--yes", check=False
        )
