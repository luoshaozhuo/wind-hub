"""E2E-02: verify PV zero/800/1000 irradiance and temperature boundaries."""

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
    observed: dict[int, dict[str, float]] = {}
    try:
        await harness.cli_call(harness.cli_rt, "collector", "start", "PV_SIM01", "--yes")
        for level in (0, 800, 1000):
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
            values = {
                point_id: float(point_values[point_id])
                for point_id in (
                    "pv_available_power",
                    "dc_power",
                    "active_power",
                    "cell_temperature",
                    "inverter_temperature",
                )
            }
            available = values["pv_available_power"]
            power = values["active_power"]
            if level == 0:
                assert available == pytest.approx(0.0, abs=1e-9)
                assert values["dc_power"] == pytest.approx(0.0, abs=1e-9)
                assert power == pytest.approx(0.0, abs=1e-9)
                assert values["cell_temperature"] == pytest.approx(25.0, abs=0.01)
                assert values["inverter_temperature"] == pytest.approx(25.0, abs=0.01)
            else:
                assert 0 < available <= 1_000_000
                assert 0 < values["dc_power"] <= available
                assert 0 < power <= 900_000
                assert 25.0 <= values["cell_temperature"] < 45.0
                assert 25.0 <= values["inverter_temperature"] < 45.0
            redis = await harness.sink.wait_redis_value(
                harness.redis_key("PV_SIM01", "active_power"),
                timeout=90,
                match=lambda item, cutoff=source_start, expected_positive=level > 0: (
                    float(item["value"]) > 0
                    if expected_positive
                    else abs(float(item["value"])) < 1e-9
                )
                and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= cutoff,
            )
            assert redis["device_id"] == "PV_SIM01" and redis["point_id"] == "active_power"
            assert redis["quality"] == "good" and 0 <= float(redis["value"]) <= 900_000
            observed[level] = values
            harness.record(
                "E2E-02-IRRADIANCE",
                generation_id=generation_id,
                level=level,
                source=values,
                redis=redis["value"],
            )
            await harness.cli_call(harness.cli_rt, "data-generator", "stop", generation_id, "--yes")
            harness.generation_id = None
            generation_id = None
        assert observed[1000]["pv_available_power"] > observed[800]["pv_available_power"]
        assert observed[1000]["active_power"] >= observed[800]["active_power"]
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "PV_SIM01", "--yes", check=False
        )
