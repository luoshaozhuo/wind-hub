"""E2E-05: real CSV replay through the CLI-managed Generator and Gateway."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


@pytest.mark.parametrize("speed", [1, 2])
async def test_e2e_05_csv_replay_recording(full_chain: Any, speed: int) -> None:
    harness = full_chain
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
    try:
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
        points = [point for row in rows for point in row["points"]]
        assert [(p["device_id"], p["point_id"], p["value"]) for p in points] == [
            ("WTG_SIM01", "wind_speed", 5.5),
            ("WTG_SIM01", "wind_speed", 6.0),
            ("WTG_SIM01", "wind_speed", 7.5),
        ]
        gateway = await harness.poll(
            harness.cli_rt,
            "gateway",
            "read",
            "gw-wtg_sim01",
            "wind_speed",
            predicate=lambda data: bool(data.get("values"))
            and data["values"][0]["value"].get("double_value") == 7.5,
            timeout=30,
        )
        assert gateway["values"][0]["quality"] == "GOOD"
        harness.record(
            "E2E-05-CSV", generation_id=generation_id, speed=speed, values=[5.5, 6.0, 7.5]
        )
    finally:
        await harness.cli_call(
            harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
        )
        harness.generation_id = None
