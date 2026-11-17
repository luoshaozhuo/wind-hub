"""E2E-03: drive the physical BESS to both configured SOC boundaries."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_03_bess_reaches_physical_soc_boundary(bess_boundary_chain: Any) -> None:
    harness = bess_boundary_chain
    assert harness.sink is not None
    generation_id: str | None = None
    boundary = 90.0 if harness.bess_boundary == "upper" else 10.0
    power = float(harness.bess_boundary_power)
    try:
        await harness.cli_call(harness.cli_rt, "collector", "start", "BESS_SIM01", "--yes")
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
        for point_id, value in (
            ("agc_enable", "1"),
            ("agc_power_setpoint", str(power)),
        ):
            command_args = ["commander", "write", "--yes", "PPC_SIM01", point_id]
            command_args.extend(("--", value) if value.startswith("-") else (value,))
            command = await harness.cli_call(
                harness.cli_rt,
                *command_args,
            )
            result = await harness.poll(
                harness.cli_rt,
                "commander",
                "result",
                command["data"]["command_id"],
                predicate=lambda item: item.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN"},
                timeout=60,
            )
            assert result["state"] == "SUCCEEDED"
            assert result["outcome"] == "CONFIRMED_SUCCESS"

        command_time = datetime.now(UTC)
        deadline = asyncio.get_running_loop().time() + 20
        boundary_samples: list[dict[str, float]] = []
        while asyncio.get_running_loop().time() < deadline:
            rows = harness.read_recording(generation_id)
            boundary_samples = [
                {
                    point["point_id"]: float(point["value"])
                    for point in row["points"]
                    if point["device_id"] == "BESS_SIM01"
                    and point["point_id"] in {"soc", "active_power"}
                }
                for row in rows
                if datetime.fromisoformat(row["produced_at"].replace("Z", "+00:00")) >= command_time
            ]
            boundary_samples = [item for item in boundary_samples if len(item) == 2]
            if (
                boundary_samples
                and abs(boundary_samples[-1]["soc"] - boundary) < 1e-5
                and abs(boundary_samples[-1]["active_power"]) < 1.0
            ):
                break
            await asyncio.sleep(0.2)
        assert boundary_samples, "no post-command BESS source samples"
        assert abs(boundary_samples[-1]["soc"] - boundary) < 1e-5, boundary_samples[-5:]
        assert abs(boundary_samples[-1]["active_power"]) < 1.0, boundary_samples[-5:]
        assert all(10.0 <= item["soc"] <= 90.0 for item in boundary_samples)
        soc_values = [item["soc"] for item in boundary_samples]
        if boundary == 90.0:
            assert soc_values[-1] >= soc_values[0]
        else:
            assert soc_values[-1] <= soc_values[0]

        redis = await harness.sink.wait_redis_value(
            harness.redis_key("BESS_SIM01", "soc"),
            timeout=60,
            match=lambda item: datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00"))
            >= command_time
            and abs(float(item["value"]) - boundary) < 1e-4,
        )
        assert redis["quality"] == "good"
        harness.record(
            "E2E-03-BOUNDARY",
            generation_id=generation_id,
            boundary=harness.bess_boundary,
            requested_power=power,
            source_final=boundary_samples[-1],
            redis_sample_id=redis["sample_id"],
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
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "BESS_SIM01", "--yes", check=False
        )
