"""E2E-04: PPC AGC/AVC/FFR commands traverse the real control chain."""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc

from .conftest import SIM_DEVICES

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


@pytest.mark.parametrize(
    ("point_id", "value"),
    [
        ("agc_enable", "1"),
        ("agc_power_setpoint", "1500.0"),
        ("avc_enable", "1"),
        ("avc_voltage_setpoint", "1.02"),
        ("avc_reactive_setpoint", "250.0"),
        ("ffr_enable", "1"),
    ],
)
async def test_e2e_04_ppc_control_applied(full_chain: Any, point_id: str, value: str) -> None:
    """A formal CLI command is successful only after Generator APPLIED evidence."""
    harness = full_chain
    generation_id: str | None = None
    try:
        for device_id in SIM_DEVICES:
            await harness.cli_call(harness.cli_rt, "collector", "start", device_id, "--yes")

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

        result = await harness.cli_call(
            harness.cli_rt,
            "commander",
            "write",
            "PPC_SIM01",
            point_id,
            value,
            "--yes",
            timeout=60,
        )
        data = result["data"]
        assert data["accepted"] is True
        command_id = data["command_id"]
        completed = await harness.poll(
            harness.cli_rt,
            "commander",
            "result",
            command_id,
            predicate=lambda item: item.get("state")
            in {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"},
            timeout=60,
        )
        assert completed["device_id"] == "PPC_SIM01"
        assert completed["point_id"] == point_id
        assert completed["outcome"] == "CONFIRMED_SUCCESS"
        assert completed["state"] == "SUCCEEDED"
        assert completed["io_settled"] is True
        # Generator evidence is scoped to the authenticated Gateway caller and
        # the durable Commander command ID passed through the control chain.
        token = harness.tokens["WIND_HUB_TEST_GATEWAY_GENERATOR_TOKEN"]
        async with grpc.aio.insecure_channel(harness.endpoints["data-generator"]) as channel:
            execution = await dg_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
                dg.GetInputExecutionRequest(generation_id=generation_id, request_id=command_id),
                timeout=10,
                metadata=(("authorization", f"Bearer {token}"),),
            )
        assert execution.request_id == command_id
        assert execution.state == "APPLIED"
        assert execution.effective_step > 0
        harness.record(
            "E2E-04",
            generation_id=generation_id,
            point_id=point_id,
            requested_value=value,
            request_id=data["request_id"],
            command_id=command_id,
            result_state=completed["state"],
            effective_step=execution.effective_step,
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
        for device_id in reversed(SIM_DEVICES):
            await harness.cli_call(
                harness.cli_rt, "collector", "stop", device_id, "--yes", check=False
            )
