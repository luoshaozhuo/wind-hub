"""E2E-04: GRID voltage disturbance and PPC AVC closed-loop feedback."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def _command(harness: Any, point_id: str, value: str) -> dict[str, Any]:
    args = ["commander", "write", "--yes", "PPC_SIM01", point_id]
    args.extend(("--", value) if value.startswith("-") else (value,))
    submitted = await harness.cli_call(harness.cli_rt, *args)
    return await harness.poll(
        harness.cli_rt,
        "commander",
        "result",
        submitted["data"]["command_id"],
        predicate=lambda item: item.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN"},
        timeout=60,
    )


async def test_e2e_04_avc_voltage_and_reactive_feedback(full_chain: Any) -> None:
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
        disturbance_time = datetime.now(UTC)
        voltage = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "variables",
            "update",
            "--generation-id",
            generation_id,
            "--device-id",
            "GRID_SIM01",
            "--request-id",
            str(uuid4()),
            "--point-id",
            "grid_voltage_source",
            "--value",
            "720",
            "--yes",
        )
        assert voltage["data"]["accepted"] is True
        enabled = await _command(harness, "avc_enable", "1")
        setpoint = await _command(harness, "avc_reactive_setpoint", "250000")
        assert enabled["state"] == "SUCCEEDED" and enabled["outcome"] == "CONFIRMED_SUCCESS"
        assert setpoint["state"] == "SUCCEEDED" and setpoint["outcome"] == "CONFIRMED_SUCCESS"

        deadline = asyncio.get_running_loop().time() + 30
        feedback: dict[str, float] | None = None
        ppc_required = {
            "grid_voltage_feedback",
            "avc_allocated_reactive_power",
            "plant_reactive_power",
            "avc_tracking_error",
        }
        grid_required = {"grid_voltage", "grid_voltage_source"}
        while asyncio.get_running_loop().time() < deadline:
            rows = harness.read_recording(generation_id)
            for row in reversed(rows):
                if datetime.fromisoformat(row["produced_at"].replace("Z", "+00:00")) < (
                    disturbance_time
                ):
                    break
                ppc = {
                    point["point_id"]: float(point["value"])
                    for point in row["points"]
                    if point["device_id"] == "PPC_SIM01" and point["point_id"] in ppc_required
                }
                grid = {
                    point["point_id"]: float(point["value"])
                    for point in row["points"]
                    if point["device_id"] == "GRID_SIM01" and point["point_id"] in grid_required
                }
                if (
                    set(ppc) == ppc_required
                    and set(grid) == grid_required
                    and abs(grid["grid_voltage_source"] - 720.0) < 1e-3
                    and abs(ppc["grid_voltage_feedback"] - grid["grid_voltage"]) < 1e-6
                    and grid["grid_voltage"] > 0
                    and abs(ppc["avc_allocated_reactive_power"] - 250000.0) < 1.0
                    and 0 < ppc["plant_reactive_power"] <= 250000.0
                    and abs(
                        ppc["avc_tracking_error"]
                        - (ppc["avc_allocated_reactive_power"] - ppc["plant_reactive_power"])
                    )
                    < 1.0
                ):
                    feedback = {**ppc, **grid}
                    break
            if feedback is not None:
                break
            await asyncio.sleep(0.2)
        assert feedback is not None

        redis_voltage = await harness.sink.wait_redis_value(
            harness.redis_key("PPC_SIM01", "grid_voltage_feedback"),
            timeout=60,
            match=lambda item: datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00"))
            >= disturbance_time
            and feedback is not None
            and abs(float(item["value"]) - feedback["grid_voltage"]) < 1e-3,
        )
        redis_reactive = await harness.sink.wait_redis_value(
            harness.redis_key("PPC_SIM01", "plant_reactive_power"),
            timeout=60,
            match=lambda item: datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00"))
            >= disturbance_time
            and feedback is not None
            and abs(float(item["value"]) - feedback["plant_reactive_power"]) < 1.0,
        )

        rejected = await harness.cli_call(
            harness.cli_rt,
            "commander",
            "write",
            "--yes",
            "PPC_SIM01",
            "avc_voltage_setpoint",
            "--",
            "-1",
            check=False,
        )
        assert rejected["status"] == "SUCCESS" and rejected["data"]["accepted"] is True
        rejected_result = await harness.poll(
            harness.cli_rt,
            "commander",
            "result",
            rejected["data"]["command_id"],
            predicate=lambda item: item.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN"},
            timeout=60,
        )
        assert rejected_result["state"] == "FAILED"
        assert rejected_result["outcome"] != "CONFIRMED_SUCCESS"
        harness.record(
            "E2E-04-AVC",
            generation_id=generation_id,
            voltage_effective_step=voltage["data"]["effective_step"],
            feedback=feedback,
            redis_voltage_sample=redis_voltage["sample_id"],
            redis_reactive_sample=redis_reactive["sample_id"],
            rejected_outcome=rejected_result["outcome"],
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
            harness.cli_rt, "collector", "stop", "PPC_SIM01", "--yes", check=False
        )
