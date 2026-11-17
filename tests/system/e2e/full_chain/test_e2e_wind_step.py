"""E2E-01: change wind input in one running physical generation."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_01_wind_step(full_chain: Any) -> None:
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
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
        request_id = str(uuid4())
        updated = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "variables",
            "update",
            "--generation-id",
            generation_id,
            "--device-id",
            "WTG_SIM01",
            "--request-id",
            request_id,
            "--point-id",
            "wind_speed_mean",
            "--value",
            "12.0",
            "--yes",
        )
        assert updated["data"]["accepted"] is True
        assert int(updated["data"]["effective_step"]) > 0
        after = await harness.sink.wait_redis_value(
            key,
            timeout=90,
            match=lambda item: abs(float(item["value"]) - 12.0) < 1e-3
            and item["sample_id"] != before["sample_id"],
        )
        rows = harness.read_recording(generation_id)
        wind = [
            p["value"]
            for row in rows
            for p in row["points"]
            if p["device_id"] == "WTG_SIM01" and p["point_id"] == "wind_speed"
        ]
        assert 10.0 in wind and 12.0 in wind and wind.index(10.0) < wind.index(12.0)
        harness.record(
            "E2E-01-STEP",
            generation_id=generation_id,
            request_id=request_id,
            effective_step=updated["data"]["effective_step"],
            before=before["value"],
            after=after["value"],
        )
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )
