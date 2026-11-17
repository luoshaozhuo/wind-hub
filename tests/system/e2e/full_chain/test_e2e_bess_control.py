"""E2E-03: BESS responds to PPC charge and discharge commands."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_03_ppc_charge_discharge(full_chain: Any) -> None:
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
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
        readbacks = []
        for point, value in (
            ("agc_enable", "1"),
            ("agc_power_setpoint", "-300000.0"),
            ("agc_power_setpoint", "300000.0"),
        ):
            command_time = datetime.now(UTC)
            command = await harness.cli_call(
                harness.cli_rt, "commander", "write", "--yes", "PPC_SIM01", point, "--", value
            )
            result = await harness.poll(
                harness.cli_rt,
                "commander",
                "result",
                command["data"]["command_id"],
                predicate=lambda item: item.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN"},
                timeout=60,
            )
            assert result["state"] == "SUCCEEDED" and result["outcome"] == "CONFIRMED_SUCCESS"
            await asyncio.sleep(7 if value == "300000.0" else 2)
            if point == "agc_power_setpoint":
                sign = -1 if value.startswith("-") else 1
                readback = await harness.sink.wait_redis_value(
                    harness.redis_key("BESS_SIM01", "active_power"),
                    timeout=60,
                    match=lambda item, direction=sign, cutoff=command_time: datetime.fromisoformat(
                        item["sample_time"].replace("Z", "+00:00")
                    )
                    >= cutoff
                    and direction * float(item["value"]) > 1,
                )
                assert readback["device_id"] == "BESS_SIM01"
                assert readback["point_id"] == "active_power" and readback["quality"] == "good"
                readbacks.append(readback)
        rows = harness.read_recording(generation_id)
        samples = [
            {
                p["point_id"]: float(p["value"])
                for p in row["points"]
                if p["device_id"] == "BESS_SIM01" and p["point_id"] in {"active_power", "soc"}
            }
            for row in rows
        ]
        samples = [item for item in samples if len(item) == 2]
        negative = [item for item in samples if item["active_power"] < -1]
        positive = [item for item in samples if item["active_power"] > 1]
        assert len(negative) >= 2 and len(positive) >= 2
        assert negative[-1]["soc"] > negative[0]["soc"]
        assert positive[-1]["soc"] < positive[0]["soc"]
        assert all(
            10 <= item["soc"] <= 90 and abs(item["active_power"]) <= 500_000 for item in samples
        )
        harness.record(
            "E2E-03-CONTROL",
            generation_id=generation_id,
            charge_samples=len(negative),
            discharge_samples=len(positive),
            redis_samples=[item["sample_id"] for item in readbacks],
        )
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "BESS_SIM01", "--yes", check=False
        )
