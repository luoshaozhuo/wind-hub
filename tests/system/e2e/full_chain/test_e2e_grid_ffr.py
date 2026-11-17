"""E2E-04: frequency disturbance reaches PPC FFR and Redis."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_04_frequency_disturbance_ffr(full_chain: Any) -> None:
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    try:
        await harness.cli_call(harness.cli_rt, "collector", "start", "PPC_SIM01", "--yes")
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
        command = await harness.cli_call(
            harness.cli_rt, "commander", "write", "PPC_SIM01", "ffr_enable", "1", "--yes"
        )
        result = await harness.poll(
            harness.cli_rt,
            "commander",
            "result",
            command["data"]["command_id"],
            predicate=lambda data: data.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN"},
        )
        assert result["state"] == "SUCCEEDED" and result["outcome"] == "CONFIRMED_SUCCESS"
        request_id = str(uuid4())
        updated = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "variables",
            "update",
            "--generation-id",
            generation_id,
            "--device-id",
            "GRID_SIM01",
            "--request-id",
            request_id,
            "--point-id",
            "grid_frequency_input",
            "--value",
            "49.5",
            "--yes",
        )
        assert updated["data"]["accepted"] is True and int(updated["data"]["effective_step"]) > 0
        # 0.5 Hz deviation less the configured 0.05 Hz deadband, at 300 kW/Hz.
        expected = 135_000.0
        value = await harness.sink.wait_redis_value(
            harness.redis_key("PPC_SIM01", "ffr_power_command"),
            timeout=90,
            match=lambda item: abs(float(item["value"]) - expected) < 1.0,
        )
        assert value["quality"] == "good"
        rows = harness.read_recording(generation_id)
        assert any(
            p["device_id"] == "PPC_SIM01"
            and p["point_id"] == "ffr_power_command"
            and abs(float(p["value"]) - expected) < 1e-6
            for row in rows
            for p in row["points"]
        )
        harness.record(
            "E2E-04-FFR",
            generation_id=generation_id,
            request_id=request_id,
            effective_step=updated["data"]["effective_step"],
            redis=value["value"],
        )
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "PPC_SIM01", "--yes", check=False
        )
